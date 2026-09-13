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
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, NamedTuple

from sqlalchemy import select
from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.api.common import verify_project_access
from app.core.errors import ApiError
from app.database import get_engine
from app.logger import get_logger
from app.models.analysis_task import AnalysisTask
from app.models.background_task import BackgroundTask
from app.models.batch_generation_task import BatchGenerationTask
from app.models.project_agent import AgentExecutionStep, AgentToolCall
from app.services.project_agent_tools import ProjectAgentToolRegistry
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


class PlanStepError(RuntimeError):
    """步骤级失败（发起报错、子任务失败/超时、子任务不存在）。一律导致失败即停。"""


@dataclass
class _PlanHandle:
    """一次计划运行的可变状态。

    它同时是三样东西：asyncio.Task 的强引用（防 GC）、取消路由表的一元、
    以及最终步数的唯一真相源（计划行只能由它写，才能在被取消后仍写进步数）。
    """

    plan_task_id: str
    user_id: str
    project_id: str
    conversation_id: str
    steps: list[dict[str, Any]]
    ai_service: Any = None          # PR-2c 收尾消费；PR-2b 一次都不调用
    tool_call_id: str | None = None  # propose_plan 锚点，run_plan 里查一次就缓存
    task: "asyncio.Task | None" = None
    cancel_requested: bool = False
    cancel_reason: str | None = None
    steps_done: int = 0
    failed_at_step: int | None = None
    in_flight: "tuple[str, str] | None" = None      # (task_type, task_id)
    propagated_code: str | None = None
    propagated_params: dict[str, Any] | None = None
    cancelled_sub_tasks: list[str] = field(default_factory=list)
    uncancellable_sub_tasks: list[str] = field(default_factory=list)
    step_results: list[dict[str, Any]] = field(default_factory=list)


# plan_task_id -> handle：模块级强引用（范式同 api/chapters.py:77 + :116-121 的
# analysis_background_tasks），并兼作取消路由表。架构计划原文写的是"强引用集合"，
# 这里用 dict 是因为它必须同时承担 task_id -> Task 的查找，两份状态会漂移。
_PLAN_HANDLES: dict[str, _PlanHandle] = {}

# 信号量按 loop 分组：asyncio.Semaphore 会记住首次 acquire 的 loop，之后换 loop 使用
# 直接抛错，而 anyio 每个用例一个新 loop。留住 loop 的强引用是为了让 id() 不被复用。
_LOOP_SEMAPHORES: dict[int, "tuple[asyncio.AbstractEventLoop, dict[str, asyncio.Semaphore]]"] = {}


async def _per_user_semaphore(user_id: str) -> asyncio.Semaphore:
    loop = asyncio.get_running_loop()
    entry = next((item for item in _LOOP_SEMAPHORES.values() if item[0] is loop), None)
    if entry is None:
        entry = (loop, {})
        _LOOP_SEMAPHORES[id(loop)] = entry
    table = entry[1]
    semaphore = table.get(user_id)
    if semaphore is None:
        semaphore = asyncio.Semaphore(1)
        table[user_id] = semaphore
    return semaphore


async def _default_session_factory(user_id: str) -> async_sessionmaker:
    """生产路径自造依赖，范式照 background_task_service.py:27-31。"""
    engine = await get_engine(user_id)
    return async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)


def _details(handle: _PlanHandle, stage: str, summary: str) -> dict[str, Any]:
    """progress_details 的固定形状；步数/取消原因/失败位置只能放这里。"""
    return {
        "stage": stage,
        "message": _clip(summary, 200),
        "outcome": stage,
        "steps_total": len(handle.steps),
        "steps_done": handle.steps_done,
        "failed_at_step": handle.failed_at_step,
        "cancel": {
            "requested": handle.cancel_requested,
            "reason": handle.cancel_reason,
            "cancelled_sub_tasks": list(handle.cancelled_sub_tasks),
            "uncancellable_sub_tasks": list(handle.uncancellable_sub_tasks),
        },
        "step_results": list(handle.step_results),
    }


async def run_plan(
    *,
    plan_task_id: str,
    user_id: str,
    project_id: str,
    conversation_id: str,
    steps: list[dict[str, Any]],
    ai_service: Any = None,
    session_factory: async_sessionmaker | None = None,
) -> "asyncio.Task":
    """调度一次计划执行，返回 runner 自持的 asyncio.Task。

    ⚠️ 返回 Task + 暴露 ai_service/session_factory 注入点是硬性要求（架构计划 §3 修 H3）：
    detached 任务拦不到请求态 monkeypatch（tests/test_agent_tool_persistence.py:159-188
    的 prompt 采集法只 patch 得到请求内的 service 实例），若不给句柄与注入点，
    「执行阶段零 LLM 调用」与「中途取消」两条验收根本不可证伪。
    """
    factory = session_factory or await _default_session_factory(user_id)
    tool_call_id = await _resolve_tool_call_id(
        factory, plan_task_id=plan_task_id, project_id=project_id, user_id=user_id
    )
    handle = _PlanHandle(
        plan_task_id=plan_task_id,
        user_id=user_id,
        project_id=project_id,
        conversation_id=conversation_id,
        steps=[dict(step) for step in (steps or []) if isinstance(step, dict)],
        ai_service=ai_service,
        tool_call_id=tool_call_id,
    )
    task = asyncio.create_task(_supervise(handle, factory))
    handle.task = task
    _PLAN_HANDLES[plan_task_id] = handle

    def _forget_handle(_completed: "asyncio.Task") -> None:
        # 身份守卫：完成回调是延迟执行的，同 id 的 re-run 可能已经把新 handle 放进
        # 表里；不按身份比对就会把活句柄清掉，让 Task 5/6 的取消路由查不到它。
        if _PLAN_HANDLES.get(plan_task_id) is handle:
            _PLAN_HANDLES.pop(plan_task_id, None)

    task.add_done_callback(_forget_handle)
    return task


async def _supervise(handle: _PlanHandle, factory: async_sessionmaker) -> None:
    """唯一顶层协程：任何异常都在此收口成计划行终态，绝不冒泡成未取回的 Task 异常。"""
    semaphore = await _per_user_semaphore(handle.user_id)
    outcome = "failed"
    summary = "计划执行失败"
    try:
        async with semaphore:                 # per-user 并发=1
            outcome, summary = await _run_plan_steps(handle, factory)
    except asyncio.CancelledError:
        outcome, summary = "cancelled", (handle.cancel_reason or "计划已取消")
        await _write_final_state(handle, factory, outcome, summary)
        raise
    except Exception as exc:                  # noqa: BLE001 —— 含权限校验失败
        outcome, summary = "failed", str(exc)
        logger.error(f"❌ 计划执行异常 {handle.plan_task_id[:8]}: {exc}", exc_info=True)
    await _write_final_state(handle, factory, outcome, summary)


def _status_fields(handle: _PlanHandle, outcome: str, summary: str) -> dict[str, Any]:
    if outcome == "cancelled":
        code = "task.cancelled"
    elif handle.propagated_code:
        code = handle.propagated_code         # 计划 B 契约：原样上送子任务的码
    elif outcome == "failed":
        code = "task.failed"
    else:
        code = "progress.done"
    total = max(len(handle.steps), 1)
    progress = 100 if outcome == "completed" else int(handle.steps_done / total * 100)
    return {
        "status": outcome,
        "progress": progress,
        "status_message": _clip(summary),
        "status_code": code,
        "status_params": (
            handle.propagated_params
            if code == handle.propagated_code and handle.propagated_params
            else {}
        ),
        "progress_details": _details(handle, outcome, summary),
        "task_result": {
            "outcome": outcome,
            "steps_total": len(handle.steps),
            "steps_done": handle.steps_done,
            "failed_at_step": handle.failed_at_step,
            "step_results": list(handle.step_results),
        },
        "completed": True,
    }


async def _write_final_state(
    handle: _PlanHandle, factory: async_sessionmaker, outcome: str, summary: str
) -> None:
    fields = _status_fields(handle, outcome, summary)
    error_message = summary if outcome == "failed" else None
    await _write_plan_row(
        factory, handle.plan_task_id, error_message=error_message, **fields
    )
    tool_call_id = handle.tool_call_id or await _resolve_tool_call_id(
        factory, plan_task_id=handle.plan_task_id,
        project_id=handle.project_id, user_id=handle.user_id,
    )
    if tool_call_id:
        await _finalize_tool_call(
            factory, tool_call_id,
            status="executed" if outcome == "completed" else "failed",
            result=fields["task_result"],
            error_message=None if outcome == "completed" else _clip(summary, 200),
        )


async def _run_plan_steps(
    handle: _PlanHandle, factory: async_sessionmaker
) -> "tuple[str, str]":
    """预检 + 打开 runner 自己的会话与权限校验，然后把主循环交给 _run_step_loop。"""
    total = len(handle.steps)
    await _write_plan_row(
        factory, handle.plan_task_id, status="running", progress=0, started=True,
        status_message=_clip(f"计划开始执行（{total} 步）"),
        progress_details=_details(handle, "running", f"计划开始执行（{total} 步）"),
    )
    if not total:
        return "completed", "计划没有需要执行的步骤"
    try:
        # 会话必须在整个循环期间持有：registry 与它绑定的 db 就是步骤的执行通道。
        # 反过来，循环里每次 _write_plan_row/_insert_step 用的是**另开**的短命会话，
        # 两者互不干扰（长会话每步 commit 后立即结束事务）。
        async with factory() as db:
            project = await verify_project_access(handle.project_id, handle.user_id, db)
            registry = ProjectAgentToolRegistry(project, db)
            return await _run_step_loop(handle, factory, db, registry)
    except ApiError as exc:
        return "failed", _clip(f"项目权限校验失败：{exc}", 200)


async def _run_step_loop(
    handle: _PlanHandle,
    factory: async_sessionmaker,
    db: AsyncSession,
    registry: ProjectAgentToolRegistry,
) -> "tuple[str, str]":
    """顺序执行每一步，失败即停。返回 (outcome, summary)。"""
    total = len(handle.steps)
    for index, step in enumerate(handle.steps, start=1):
        if handle.cancel_requested:
            return "cancelled", _clip(handle.cancel_reason or "计划已取消", 200)
        label = str(step.get("action") or step.get("tool") or f"step {index}")
        step_id = await _insert_step(
            factory,
            conversation_id=handle.conversation_id,
            tool_call_id=handle.tool_call_id,
            sequence=index,
            title=f"计划第 {index}/{total} 步：{label}",
            content="正在执行",
            detail={"index": index, "tool": step.get("tool"), "action": step.get("action"),
                    "arguments": step.get("arguments") or {}, "note": step.get("note")},
        )
        try:
            recorded = await _execute_step(handle, factory, db, registry, step)
        except Exception as exc:              # noqa: BLE001 —— 失败即停
            handle.failed_at_step = index
            handle.step_results.append(
                {"index": index, "action": label, "status": "failed", "error": _clip(exc, 200)}
            )
            await _patch_step(
                factory, step_id, status="failed",
                content=_clip(f"失败：{exc}", 500),
            )
            return "failed", _clip(f"第 {index} 步失败：{exc}", 200)
        handle.steps_done = index
        handle.step_results.append({"index": index, "action": label, "status": "completed", **recorded})
        await _patch_step(factory, step_id, status="completed", content="完成")
        await _write_plan_row(
            factory, handle.plan_task_id,
            progress=int(index / total * 100),
            status_message=_clip(f"已完成 {index}/{total} 步"),
            progress_details=_details(handle, "running", f"已完成 {index}/{total} 步"),
        )
        if STEP_GRACE_SECONDS:
            await asyncio.sleep(STEP_GRACE_SECONDS)   # PR-4：步间 grace
    return "completed", f"计划执行完成（{total}/{total} 步）"


async def _execute_step(
    handle: _PlanHandle,
    factory: async_sessionmaker,
    db: AsyncSession,
    registry: ProjectAgentToolRegistry,
    step: dict[str, Any],
) -> dict[str, Any]:
    """执行一步。只读/即时写工具内联完成；后台任务型步骤交给轮询（Task 4）。"""
    tool = str(step.get("tool") or "")
    arguments = dict(step.get("arguments") or {})
    action = str(step.get("action") or arguments.get("action") or "")
    if action and "action" not in arguments:
        arguments["action"] = action
    result = await registry.execute(tool, arguments)
    # 前序落库后序可见：跨步引用（chapter_number / outline.order_index）在执行期由
    # find_chapter 解析，所以每步都必须先提交、再让身份映射作废。
    await db.commit()
    db.expire_all()
    await db.refresh(registry.project)   # expire_all 会让绑定会话的 project 变成惰性刷新，
    # 下一步的同步属性访问会在非 greenlet 上下文触发 IO（MissingGreenlet）；显式刷新一次。
    if tool != BACKGROUND_LAUNCH_TOOL:
        return {
            "inline": True,
            "entity_id": str(result.get("entity_id") or "") if isinstance(result, dict) else "",
        }
    if action not in AGENT_TASK_ACTION_TYPES:
        raise PlanStepError(f"计划步骤的 action 不是可发起的后台任务：{action}")
    task_type = AGENT_TASK_ACTION_TYPES[action]
    sub_task_id = str(result.get("entity_id") or "") if isinstance(result, dict) else ""
    if not sub_task_id:
        raise PlanStepError(f"步骤未返回子任务标识：{action}")
    snapshot = await _await_sub_task(handle, factory, task_type, sub_task_id)
    if snapshot.status == "cancelled":
        raise PlanStepError(f"子任务被外部取消：{action}")
    if snapshot.status == "failed":
        handle.propagated_code = snapshot.status_code
        handle.propagated_params = snapshot.status_params
        raise PlanStepError(_clip(
            snapshot.error_message or f"{action} 执行失败", 200
        ))
    return {
        "inline": False,
        "sub_task_id": sub_task_id,
        "sub_task_type": task_type,
        "sub_task_status": snapshot.status,
        "sub_task_progress": snapshot.progress,
    }


async def _cancel_in_flight(handle: _PlanHandle, factory: async_sessionmaker) -> None:
    """取消当前在途子任务并记账（Task 5 的 _supervise 取消分支复用同一个函数）。

    级联只有两条真实入口：轮询自查（handle.cancel_requested / 计划行已被取消）先级联
    再抛；外部 ``task.cancel()`` 由 ``_await_sub_task`` 的 except-CancelledError 级联——
    finally 执行时 in_flight 已空，看不见它。级联后立即置空 in_flight 让重复调用成为
    空操作，_supervise 的 Task 5 分支因此只是无害兜底。
    """
    if handle.in_flight is None:
        return
    task_type, task_id = handle.in_flight
    handle.in_flight = None          # 幂等：级联只发生一次（轮询自查路径 + 上面的 except 都调它）
    cancelled = await _cancel_sub_task(
        factory, user_id=handle.user_id, task_type=task_type, task_id=task_id
    )
    (handle.cancelled_sub_tasks if cancelled else handle.uncancellable_sub_tasks).append(task_id)


async def _await_sub_task(
    handle: _PlanHandle, factory: async_sessionmaker, task_type: str, task_id: str
) -> TaskSnapshot:
    """轮询子任务直到终态；单步有超时上限，超时就把子任务取消掉再失败。

    每轮流询都开短命会话（先查计划行取消位、再读子任务快照，各一个）：runner 那个长会话
    带 expire_on_commit=False，靠它轮询会读到陈旧身份映射（见 resolve_task_snapshot 的
    注释）。
    """
    loop = asyncio.get_running_loop()
    deadline = loop.time() + STEP_POLL_TIMEOUT_SECONDS
    handle.in_flight = (task_type, task_id)
    try:
        while True:
            if handle.cancel_requested:
                # 先级联再抛：_supervise 的取消分支拿不到已被 finally 清空的 in_flight。
                await _cancel_in_flight(handle, factory)
                raise asyncio.CancelledError()
            if await _plan_cancel_requested(factory, handle.plan_task_id):
                handle.cancel_requested = True
                handle.cancel_reason = handle.cancel_reason or "计划行已被外部取消"
                await _cancel_in_flight(handle, factory)
                raise asyncio.CancelledError()
            async with factory() as session:
                snapshot = await resolve_task_snapshot(
                    session, task_type=task_type, task_id=task_id
                )
            if snapshot is None:
                raise PlanStepError(f"子任务行不存在：{task_type}")
            if snapshot.finished:
                return snapshot
            if loop.time() > deadline:
                await _cancel_in_flight(handle, factory)
                raise PlanStepError(
                    f"步骤轮询超过 {int(STEP_POLL_TIMEOUT_SECONDS)} 秒"
                )
            await asyncio.sleep(POLL_INTERVAL_SECONDS)
    except asyncio.CancelledError:
        # 外部 task.cancel()：异常穿过 finally 前 in_flight 还在，这里级联；轮询自查路径
        # 已经级联过（_cancel_in_flight 已把 in_flight 置空）⇒ 这里是空操作。
        await _cancel_in_flight(handle, factory)
        raise
    finally:
        handle.in_flight = None


async def _plan_cancel_requested(factory: async_sessionmaker, plan_task_id: str) -> bool:
    """轮询期间也要看计划行：用户可能走通用 POST /api/tasks/{id}/cancel。"""
    async with factory() as session:
        row = (await session.execute(
            select(BackgroundTask.cancel_requested, BackgroundTask.status)
            .where(BackgroundTask.id == plan_task_id)
        )).first()
    if row is None:
        return False
    return bool(row[0]) or row[1] == "cancelled"


async def _cancel_sub_task(
    factory: async_sessionmaker, *, user_id: str, task_type: str, task_id: str
) -> bool:
    """取消在途子任务；返回是否真的取消掉。

    字段写入与 ProjectAgentOperationalTools._manage_background_task_cancel 保持一致，
    但不复用那个方法：它要经 _find_task -> _all_tasks 把项目里四张任务表全捞一遍，
    而 runner 已经确切知道 (task_type, task_id)。AnalysisTask 没有可取消状态
    （api/tasks.py:128 的 can_cancel=False），只能停止轮询，调用方按 False 记账。
    """
    model = _model_for_task_type(task_type)
    if model is BackgroundTask:
        from app.services.background_task_service import background_task_service

        async with factory() as session:
            return await background_task_service.cancel_task(task_id, user_id, session)
    if model is BatchGenerationTask:
        async with factory() as session:
            res = await session.execute(
                update(BatchGenerationTask)
                .where(
                    BatchGenerationTask.id == task_id,
                    BatchGenerationTask.status.in_(("pending", "running")),
                )
                # 列是 naive-UTC（models/project_agent.py 约定 + Postgres TimeZone=UTC）；
                # 用本地墙钟会写出 +8h 的偏差。
                .values(
                    status="cancelled",
                    completed_at=datetime.now(timezone.utc).replace(tzinfo=None),
                )
            )
            await session.commit()
            return (res.rowcount or 0) == 1
    return False
