"""确定性计划执行器（架构计划 A / PR-2b）：执行阶段零 LLM 调用。

LLM 只出现在两个端点：规划（PR-2a 的 propose_plan）与收尾（PR-2c）。本模块负责
中间整段——顺序、确定性、可取消、可轮询。

设计约束（架构计划 §3，改动前先读原文）：
- 执行模式＝「registry 发起 + 轮询任务终态」，不直 await handler：长任务的工作体是
  handler 内闭包（project_agent_operational_tools.py 的 run_stream、api/outlines.py、
  api/chapters.py），外部没有入口。
- 必须 detached 运行，且**绝不能**作为 background_task_service.spawn_background_task
  的 task_func：那会占死每用户单 worker（_user_worker_loop 串行 await），饿死它自己
  轮询的子任务。
- 自开 AsyncSession + verify_project_access，绝不复用请求态会话。
"""
from __future__ import annotations

import asyncio
from datetime import datetime
from typing import Any, NamedTuple

from sqlalchemy import select
from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.logger import get_logger
from app.models.analysis_task import AnalysisTask
from app.models.background_task import BackgroundTask
from app.models.batch_generation_task import BatchGenerationTask
from app.models.project_agent import AgentExecutionStep, AgentToolCall
from app.services.task_resources import AGENT_TASK_ACTION_TYPES

logger = get_logger(__name__)

PLAN_TASK_TYPE = "agent_plan"
BACKGROUND_LAUNCH_TOOL = "start_project_task"
TERMINAL_STATUSES = frozenset({"completed", "failed", "cancelled"})

# 常量在 PR-2c 换成 settings 读取（键名保持同名）。下面 5 个预算常量测试会 monkeypatch，
# 所以下游一律"用时读模块全局"，禁止写成函数默认值（默认值在 def 时求值，patch 不掉）；
# CANCEL_SETTLE_TIMEOUT_SECONDS 不在此列（没有用例 patch 它）。
MAX_PLAN_STEPS = 30
PLAN_WALL_CLOCK_SECONDS = 7200.0
STEP_POLL_TIMEOUT_SECONDS = 900.0
POLL_INTERVAL_SECONDS = 2.0
STEP_GRACE_SECONDS = 0.0            # PR-4 调成 3.0（SQLite WAL 可见性）
STATUS_MESSAGE_MAX_CHARS = 120      # status_message 是 String(500)，PG 超长直接报错


class TaskSnapshot(NamedTuple):
    """轮询用的任务快照。

    前三个字段就是架构计划 §3 规定的 ``(status, progress, finished)``，位置不变，
    可按三元组解包使用。后面三个字段是与计划 B 的接口契约所要求的（anchor-audit
    「与计划 B 的接口契约」第 4 条）：子任务失败时它把 ``validation.ai_model_below_minimum``
    写在**自己那一行**的 ``status_code`` 上，runner 必须把同一个码原样上送到计划行，
    而不是笼统报 ``task.failed`` —— 拿不到 code/params 就做不到，所以三元组必须扩展。
    """

    status: str
    progress: int
    finished: bool
    status_code: str | None = None
    status_params: dict[str, Any] | None = None
    error_message: str | None = None


def _model_for_task_type(task_type: str) -> type:
    """按 task_type 反查表；**调用方永远不需要传表名**（三表 id 无跨表唯一性）。"""
    if "analysis" in task_type:
        return AnalysisTask
    if task_type == "chapter_batch":
        return BatchGenerationTask
    if task_type == PLAN_TASK_TYPE or task_type in AGENT_TASK_ACTION_TYPES.values():
        return BackgroundTask
    raise ValueError(f"未知的计划步骤任务类型：{task_type}")


async def resolve_task_snapshot(
    db: AsyncSession, *, task_type: str, task_id: str
) -> TaskSnapshot | None:
    """读取一条任务行的轮询三态；行不存在返回 None（调用方按步骤失败处理）。

    一律用**列投影**而不是 ``select(Model)``：runner 的会话带 ``expire_on_commit=False``，
    实体查询会命中 SQLAlchemy 身份映射、返回陈旧属性，于是轮询会永远看不见别的会话
    写的终态。列投影每次都从库里取新值（同 background_task_service._is_task_cancelled:302-310
    的做法）。进度推导形状复用 api/tasks.py 的三个 _xxx_task_data。
    """
    model = _model_for_task_type(task_type)

    if model is BackgroundTask:
        row = (await db.execute(
            select(
                BackgroundTask.status,
                BackgroundTask.progress,
                BackgroundTask.status_code,
                BackgroundTask.status_params,
                BackgroundTask.error_message,
            ).where(BackgroundTask.id == task_id)
        )).first()
        if row is None:
            return None
        status, progress, code, params, error = row
        return TaskSnapshot(
            status=status or "pending",
            progress=int(progress or 0),
            finished=status in TERMINAL_STATUSES,
            status_code=code,
            status_params=params if isinstance(params, dict) else None,
            error_message=error,
        )

    if model is BatchGenerationTask:
        row = (await db.execute(
            select(
                BatchGenerationTask.status,
                BatchGenerationTask.total_chapters,
                BatchGenerationTask.completed_chapters,
                BatchGenerationTask.error_message,
            ).where(BatchGenerationTask.id == task_id)
        )).first()
        if row is None:
            return None
        status, total, done, error = row
        progress = 100 if status == "completed" else (
            int((done or 0) / total * 100) if total else 0
        )
        return TaskSnapshot(
            status=status or "pending",
            progress=progress,
            finished=status in TERMINAL_STATUSES,
            status_code=None,
            status_params=None,
            error_message=error,
        )

    row = (await db.execute(
        select(
            AnalysisTask.status,
            AnalysisTask.progress,
            AnalysisTask.error_message,
        ).where(AnalysisTask.id == task_id)
    )).first()
    if row is None:
        return None
    status, progress, error = row
    return TaskSnapshot(
        status=status or "pending",
        progress=int(progress or 0),
        finished=status in TERMINAL_STATUSES,
        status_code=None,
        status_params=None,
        error_message=error,
    )


def _clip(text: Any, limit: int | None = None) -> str:
    """压成一行并把长度钉在 limit 内（默认取模块常量，不用函数默认值绑死）。

    status_message 是 String(500)，且项目支持 Postgres：SQLite 静默截断、PG 直接报错。
    所以面向用户那一列只放摘要，详情一律进 progress_details（JSON）。
    """
    max_chars = STATUS_MESSAGE_MAX_CHARS if limit is None else limit
    normalized = " ".join(str(text if text is not None else "").split())
    if len(normalized) <= max_chars:
        return normalized
    return normalized[: max(max_chars - 1, 1)] + "…"


async def _write_plan_row(
    session_factory: async_sessionmaker,
    plan_task_id: str,
    *,
    status: str | None = None,
    progress: int | None = None,
    status_message: str | None = None,
    status_code: str | None = None,
    status_params: dict[str, Any] | None = None,
    progress_details: dict[str, Any] | None = None,
    task_result: dict[str, Any] | None = None,
    error_message: str | None = None,
    started: bool = False,
    completed: bool = False,
) -> None:
    """用直接 UPDATE 写计划行；**刻意不使用 TaskProgressTracker**。

    TaskProgressTracker._update_task（background_task_service.py:24-45）开头就
    ``if task.status == "cancelled" or task.cancel_requested: return``，而通用取消接口
    cancel_task(:377-399) 会立刻把 status 置成 cancelled ⇒ 一旦走 tracker，取消后的
    最终步数永远写不进计划行。直接 UPDATE 既绕过冻结，也绕过身份映射。
    """
    now = datetime.now()
    values: dict[str, Any] = {"updated_at": now}
    if status is not None:
        values["status"] = status
    if progress is not None:
        values["progress"] = int(progress)
    if status_message is not None:
        values["status_message"] = _clip(status_message)
    if status_code is not None:
        values["status_code"] = status_code
    if status_params is not None:
        values["status_params"] = status_params
    if progress_details is not None:
        values["progress_details"] = progress_details
    if task_result is not None:
        values["task_result"] = task_result
    if error_message is not None:
        values["error_message"] = error_message
    if started:
        values["started_at"] = now
    if completed:
        values["completed_at"] = now
    async with session_factory() as session:
        await session.execute(
            update(BackgroundTask)
            .where(BackgroundTask.id == plan_task_id)
            .values(**values)
        )
        await session.commit()


async def _insert_step(
    session_factory: async_sessionmaker,
    *,
    conversation_id: str,
    tool_call_id: str | None,
    sequence: int,
    title: str,
    content: str,
    detail: dict[str, Any] | None,
    status: str = "running",
) -> str:
    """新建一条步骤行。列语义对齐 project_agent_service._create_step:1186-1216
    （title[:200]、step_type="tool"/category="project" 同 :896-897），但不要求
    user_message 存在——detached runner 没有用户消息，那三列都可空。
    """
    step = AgentExecutionStep(
        conversation_id=conversation_id,
        tool_call_id=tool_call_id,
        sequence=sequence,
        step_type="tool",
        category="project",
        title=title[:200],
        content=content,
        status=status,
        detail=detail,
    )
    async with session_factory() as session:
        session.add(step)
        await session.commit()
    return step.id


async def _patch_step(
    session_factory: async_sessionmaker,
    step_id: str,
    *,
    status: str | None = None,
    content: str | None = None,
    detail: dict[str, Any] | None = None,
) -> None:
    values: dict[str, Any] = {"updated_at": datetime.now()}   # 对齐 _update_step:1234
    if status is not None:
        values["status"] = status
    if content is not None:
        values["content"] = content
    if detail is not None:
        values["detail"] = detail
    async with session_factory() as session:
        await session.execute(
            update(AgentExecutionStep)
            .where(AgentExecutionStep.id == step_id)
            .values(**values)
        )
        await session.commit()


async def _finalize_tool_call(
    session_factory: async_sessionmaker,
    tool_call_id: str,
    *,
    status: str,
    result: dict[str, Any] | None,
    error_message: str | None,
) -> bool:
    """把计划那条 AgentToolCall 从 executing 推到终态。

    _claim_tool_call（api/project_agent.py:297-341）抢占后它是 executing；§3 若只写
    step 与进度，这行就永久停在 executing，而 finalize_interrupted_turn(:1127) 只作用
    在同请求实例、headless 够不着。条件 UPDATE 让收尾幂等。
    """
    async with session_factory() as session:
        res = await session.execute(
            update(AgentToolCall)
            .where(
                AgentToolCall.id == tool_call_id,
                AgentToolCall.status == "executing",
            )
            .values(
                status=status,
                result=result,
                error_message=error_message,
                executed_at=datetime.now(),
            )
        )
        await session.commit()
        return (res.rowcount or 0) == 1


async def _resolve_tool_call_id(
    session_factory: async_sessionmaker,
    *,
    plan_task_id: str,
    project_id: str,
    user_id: str,
) -> str | None:
    """计划 → propose_plan 锚点。首选 PR-2a 写进 task_input 的 tool_call_id。"""
    async with session_factory() as session:
        raw = (await session.execute(
            select(BackgroundTask.task_input).where(BackgroundTask.id == plan_task_id)
        )).scalar_one_or_none()
        if isinstance(raw, dict) and raw.get("tool_call_id"):
            return str(raw["tool_call_id"])
        return (await session.execute(
            select(AgentToolCall.id)
            .where(
                AgentToolCall.project_id == project_id,
                AgentToolCall.user_id == user_id,
                AgentToolCall.tool_name == "propose_plan",
                AgentToolCall.status == "executing",
            )
            .order_by(AgentToolCall.created_at.desc())
            .limit(1)
        )).scalar_one_or_none()
