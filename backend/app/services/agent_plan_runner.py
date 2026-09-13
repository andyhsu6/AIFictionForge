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
from typing import Any, NamedTuple

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.logger import get_logger
from app.models.analysis_task import AnalysisTask
from app.models.background_task import BackgroundTask
from app.models.batch_generation_task import BatchGenerationTask
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
