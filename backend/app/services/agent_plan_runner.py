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
import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, NamedTuple

from sqlalchemy import select
from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.api.common import verify_project_access
from app.config import settings
from app.core.errors import ApiError
from app.database import get_engine
from app.logger import get_logger
from app.models.analysis_task import AnalysisTask
from app.models.background_task import BackgroundTask
from app.models.batch_generation_task import BatchGenerationTask
from app.models.project_agent import (
    AgentConversation,
    AgentExecutionStep,
    AgentMessage,
    AgentToolCall,
    _naive_utc_now,
)
from app.services.agent_plan_schema import PROPOSE_PLAN_TOOL_NAME
from app.services.language_resolver import resolve_user_generation_language
from app.services.project_agent_service import agent_system_prompt
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
CANCEL_SETTLE_TIMEOUT_SECONDS = 10.0

SUMMARY_MAX_CHARS = 8000        # PR-2c 聚合消息裁剪；键名 agent_plan_summary_max_chars

PLAN_SUMMARY_TOOL_NAME = "plan_run_summary"   # 单条聚合 tool 行的哨兵（幂等按它过滤）
_SUMMARY_FIELD_MAX_CHARS = 300                # 单字段上限，绝不透传 step 原文

PLAN_CLOSING_INSTRUCTION = (
    "下面是后台计划执行器生成的结构化执行摘要（JSON）。请用一段简洁的总结向用户说明："
    "计划整体结果、已完成步数与失败位置；只依据摘要内容，不得编造。"
    "章节级分析结论不在摘要里，如需查看详情，提示用户在后续对话中使用 "
    "get_chapter_analysis 工具。不要调用任何工具，直接输出总结文本。\n"
)

_DEFAULT_LIMITS: dict[str, Any] = {
    "agent_plan_max_steps": MAX_PLAN_STEPS,
    "agent_plan_wall_clock_seconds": PLAN_WALL_CLOCK_SECONDS,
    "agent_plan_step_poll_timeout_seconds": STEP_POLL_TIMEOUT_SECONDS,
    "agent_plan_poll_interval_seconds": POLL_INTERVAL_SECONDS,
    "agent_plan_step_grace_seconds": STEP_GRACE_SECONDS,
    "agent_plan_status_message_max_chars": STATUS_MESSAGE_MAX_CHARS,
    "agent_plan_summary_max_chars": SUMMARY_MAX_CHARS,
}


def _limit(key: str, module_value: Any) -> Any:
    """§3 配置面唯一读取口。

    规则：调用方传进来的 module_value 若已被 monkeypatch 成非默认值 ⇒ 以它为准
    （PR-2b 的用例靠 patch 模块常量驱动预算）；否则取 settings。
    两者都不满足时直接返回默认，绝不抛错——runner 是 detached 任务，
    配置读不出来不该让计划静默失败。
    """
    default = _DEFAULT_LIMITS.get(key)
    if default is not None and module_value != default:
        return module_value
    value = getattr(settings, key, default)
    if value is None:
        return default
    if isinstance(default, bool) or isinstance(module_value, bool):
        return bool(value)
    if isinstance(default, int) and not isinstance(module_value, float):
        return int(value)
    return value


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
    max_chars = _limit("agent_plan_status_message_max_chars", STATUS_MESSAGE_MAX_CHARS) if limit is None else limit
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


def _tool_call_entries(raw: Any) -> list[dict]:
    """provider 原始 tool_calls 载荷 -> 良构 entry 列表；畸形输入返回空表，绝不抛错。"""
    entries = raw
    if isinstance(entries, str):
        try:
            entries = json.loads(entries)
        except (TypeError, ValueError):
            return []
    if not isinstance(entries, list):
        return []
    return [entry for entry in entries if isinstance(entry, dict)]


def _entry_function(entry: dict) -> dict:
    function = entry.get("function")
    return function if isinstance(function, dict) else {}


def _entry_arguments_dict(entry: dict) -> dict | None:
    arguments = _entry_function(entry).get("arguments")
    if isinstance(arguments, str):
        try:
            arguments = json.loads(arguments)
        except (TypeError, ValueError):
            return None
    return arguments if isinstance(arguments, dict) else None


def _plan_match_key(args: Any) -> tuple[str, tuple[tuple[str, str, str], ...]] | None:
    """Raw 与 validate_plan 后的计划 dict 配对的稳定键。

    validate_plan 会给每一步补 action/note 等归一化字段（raw 侧没有），全量 dict
    相等在生产恒不成立；但 objective、各步 (id, tool) 与步 arguments 两侧都保留，
    前两者 strip、arguments 用 sort_keys 规范化，所以这几样能跨 raw/validated 形状
    配对（validate_plan 原样透传 raw arguments，见 agent_plan_schema.py:157）。
    """
    if not isinstance(args, dict):
        return None
    steps = args.get("steps")
    if not isinstance(steps, list):
        return None
    return (
        str(args.get("objective") or "").strip(),
        tuple(
            (
                str(step.get("id") or "").strip(),
                str(step.get("tool") or "").strip(),
                json.dumps(step.get("arguments") or {}, sort_keys=True),
            )
            for step in steps
            if isinstance(step, dict)
        ),
    )


def _propose_plan_entry_id(entry: dict) -> str:
    if _entry_function(entry).get("name") != PROPOSE_PLAN_TOOL_NAME:
        return ""
    return str(entry.get("id") or "").strip()


async def resolve_provider_call_id(
    session_factory: async_sessionmaker,
    *,
    tool_call_id: str | None,
    conversation_id: str,
    plan_task_input: dict[str, Any] | None = None,
) -> str:
    """聚合收尾消息该挂哪个 tool_call_id（架构 §0）。

    顺序：① task_input 里显式写的 provider_call_id（前瞻：PR-2a 若补写则无缝生效）
    ② 该 AgentToolCall 关联的 assistant 消息里 propose_plan 那条原始 id
    ③ 会话内扫描：真实 propose-plan 流把 record.message_id 指向 tool_calls=NULL
       的计划卡（project_agent_service.py:950-958），provider id 只存在于更早那条
       带 tool_calls 的 assistant 消息里 ⇒ 按会话从新到旧扫，先按 arguments 配对，
       配不上再取最新一条同名 entry
    ④ 兜底 = AgentToolCall.id。
    ⚠️ :906 的回退分支意味着 provider 未回传 id 时 ②/③ 与 ④ **同值**，
    这是合法状态，下游一律不得写成 "if provider_id != record_id" 的分支。
    """
    if not tool_call_id:
        return ""
    async with session_factory() as session:
        record = await session.get(AgentToolCall, tool_call_id)
        if record is None:
            return ""
        if plan_task_input:
            explicit = str(plan_task_input.get("provider_call_id") or "").strip()
            if explicit:
                return explicit
        message_id = getattr(record, "message_id", None)
        if message_id:
            message = await session.get(AgentMessage, message_id)
            raw_entries = getattr(message, "tool_calls", None) if message else None
            for entry in _tool_call_entries(raw_entries):
                entry_id = _propose_plan_entry_id(entry)
                if entry_id:
                    return entry_id
        messages = (await session.execute(
            select(AgentMessage)
            .where(
                AgentMessage.conversation_id == conversation_id,
                AgentMessage.role == "assistant",
            )
            .order_by(AgentMessage.created_at.desc())
        )).scalars().all()
        parsed = [
            _tool_call_entries(getattr(message, "tool_calls", None))
            for message in messages
        ]
        wanted = _plan_match_key(record.arguments)
        if wanted is not None:
            for entries in parsed:
                for entry in entries:
                    entry_id = _propose_plan_entry_id(entry)
                    if entry_id and _plan_match_key(_entry_arguments_dict(entry)) == wanted:
                        return entry_id
        for entries in parsed:
            for entry in reversed(entries):
                entry_id = _propose_plan_entry_id(entry)
                if entry_id:
                    return entry_id
        return record.id


def build_plan_summary_payload(
    handle: _PlanHandle, outcome: str, summary: str
) -> dict[str, Any]:
    """服务端白名单聚合 payload：step 原文（result/detail/arguments 等）一律不透传。"""
    results = {
        entry.get("index"): entry
        for entry in handle.step_results
        if isinstance(entry, dict)
    }
    steps: list[dict[str, Any]] = []
    for index, raw_step in enumerate(handle.steps, start=1):
        if not isinstance(raw_step, dict):
            continue
        result = results.get(index) or {}
        item: dict[str, Any] = {
            "id": _clip(raw_step.get("id"), _SUMMARY_FIELD_MAX_CHARS),
            "tool": _clip(raw_step.get("tool"), _SUMMARY_FIELD_MAX_CHARS),
            "status": _clip(result.get("status") or "pending", 40),
        }
        action = raw_step.get("action")
        if action:
            item["action"] = _clip(action, _SUMMARY_FIELD_MAX_CHARS)
        for source_key, target_key in (
            ("sub_task_id", "sub_task_id"),
            ("sub_task_type", "task_type"),
            ("error_code", "error_code"),
        ):
            value = result.get(source_key)
            if value:
                item[target_key] = _clip(value, _SUMMARY_FIELD_MAX_CHARS)
        steps.append(item)
    task_input = handle.task_input if isinstance(handle.task_input, dict) else {}
    return {
        "plan_task_id": handle.plan_task_id,
        "objective": _clip(task_input.get("objective") or "", _SUMMARY_FIELD_MAX_CHARS),
        "outcome": outcome,
        "steps_total": len(handle.steps),
        "steps_done": handle.steps_done,
        "failed_at_step": handle.failed_at_step,
        "cancelled": handle.cancel_requested or outcome == "cancelled",
        "steps": steps,
        "server_note": _clip(summary, _SUMMARY_FIELD_MAX_CHARS),
        "detail_source": "AgentExecutionStep; 需要章节/分析结论时调用只读工具，不要臆造",
    }


async def _insert_plan_summary_message(
    session_factory: async_sessionmaker,
    *,
    handle: _PlanHandle,
    provider_call_id: str,
    outcome: str,
    summary: str,
) -> str:
    """写唯一一条 role=tool 聚合消息（DB 级幂等：同会话/同 tool_call_id 已有则不重复写）。"""
    content = _clip(
        json.dumps(
            {
                "tool": PLAN_SUMMARY_TOOL_NAME,
                "error": None,
                "result": build_plan_summary_payload(handle, outcome, summary),
            },
            ensure_ascii=False,
            default=str,
        ),
        _limit("agent_plan_summary_max_chars", SUMMARY_MAX_CHARS),
    )
    before = _naive_utc_now()          # AgentMessage.created_at 基准 = naive UTC
    conversation_now = datetime.now()  # last_message_at 全库基准 = 本地墙钟
    async with session_factory() as session:
        existing = (await session.execute(
            select(AgentMessage.id)
            .where(
                AgentMessage.conversation_id == handle.conversation_id,
                AgentMessage.role == "tool",
                AgentMessage.tool_call_id == provider_call_id,
                AgentMessage.content.like(f"%{PLAN_SUMMARY_TOOL_NAME}%"),
            )
            .order_by(AgentMessage.created_at.asc())
            .limit(1)
        )).scalar_one_or_none()
        if existing is not None:
            return existing
        message = AgentMessage(
            conversation_id=handle.conversation_id,
            role="tool",
            content=content,
            tool_call_id=provider_call_id,
            created_at=before,
        )
        session.add(message)
        await session.execute(
            update(AgentConversation)
            .where(AgentConversation.id == handle.conversation_id)
            .values(last_message_at=conversation_now)
        )
        await session.commit()
        return message.id


async def _insert_plan_assistant_message(
    session_factory: async_sessionmaker,
    *,
    conversation_id: str,
    content: str,
    model: str | None,
    prompt_tokens: int,
    completion_tokens: int,
) -> str | None:
    """落收尾 role=assistant 消息；空 content 不写。绝不写 role=user。"""
    if not content or not content.strip():
        return None
    before = _naive_utc_now()          # AgentMessage.created_at 基准 = naive UTC
    conversation_now = datetime.now()  # last_message_at 全库基准 = 本地墙钟
    async with session_factory() as session:
        message = AgentMessage(
            conversation_id=conversation_id,
            role="assistant",
            content=_clip(content, _limit("agent_plan_summary_max_chars", SUMMARY_MAX_CHARS)),
            model=model,
            prompt_tokens=prompt_tokens or None,
            completion_tokens=completion_tokens or None,
            created_at=before,
        )
        session.add(message)
        await session.execute(
            update(AgentConversation)
            .where(AgentConversation.id == conversation_id)
            .values(last_message_at=conversation_now)
        )
        await session.commit()
        return message.id


async def _closing_stage(
    handle: _PlanHandle, factory: async_sessionmaker, outcome: str, summary: str
) -> None:
    """收尾：恒写 1 条聚合 tool 消息；非取消且有 ai_service 时恰好 1 次 headless LLM。

    幂等经 handle.closing_done；所有异常在此吞掉并 log —— 收尾失败不得改计划终态，
    也不得让计划行卡在 running（ai_service 缺失只降级为聚合消息）。
    """
    if handle.closing_done:
        return
    handle.closing_done = True
    try:
        provider_call_id = await resolve_provider_call_id(
            factory,
            tool_call_id=handle.tool_call_id,
            conversation_id=handle.conversation_id,
            plan_task_input=handle.task_input,
        )
    except Exception as exc:                  # noqa: BLE001 —— 锚点解析失败不得吞掉聚合行
        logger.warning(
            f"计划 {handle.plan_task_id[:8]} 解析 provider_call_id 失败，回退锚点: {exc}"
        )
        provider_call_id = handle.tool_call_id or ""
    handle.summary_message_id = await _insert_plan_summary_message(
        factory, handle=handle, provider_call_id=provider_call_id,
        outcome=outcome, summary=summary,
    )
    if outcome == "cancelled":
        logger.info(f"计划已取消，跳过收尾 LLM 调用 {handle.plan_task_id[:8]}")
        return
    if handle.ai_service is None:
        logger.warning(f"计划收尾缺少 ai_service，仅写聚合消息 {handle.plan_task_id[:8]}")
        return
    try:
        async with factory() as session:
            generation_language = await resolve_user_generation_language(session, handle.user_id)
        payload = build_plan_summary_payload(handle, outcome, summary)
        response = await handle.ai_service.generate_text(
            prompt=PLAN_CLOSING_INSTRUCTION + json.dumps(payload, ensure_ascii=False, default=str),
            system_prompt=agent_system_prompt(
                generation_language,
                approval_prompt=(
                    "\n\n当前为手动批准模式：写入工具生成预览后必须等待用户在界面确认，"
                    "不得提前声称修改已生效。"
                ),
            ),
            tools=None,
            auto_mcp=False,
            handle_tool_calls=False,
        )
        usage = (response or {}).get("usage") or {}
        await _insert_plan_assistant_message(
            factory,
            conversation_id=handle.conversation_id,
            content=(response or {}).get("content") or "",
            model=(response or {}).get("model") or getattr(handle.ai_service, "default_model", None),
            prompt_tokens=usage.get("prompt_tokens") or 0,
            completion_tokens=usage.get("completion_tokens") or 0,
        )
    except Exception as exc:                  # noqa: BLE001 —— 收尾失败不得影响终态
        logger.error(f"计划收尾失败（不影响终态） {handle.plan_task_id[:8]}: {exc}", exc_info=True)


async def _close_and_finalize(
    handle: _PlanHandle, factory: async_sessionmaker, outcome: str, summary: str
) -> None:
    """收尾 + 终态写入打包成一个原子单元；调用方一律 shield 它。

    取消落在收尾等待中时，外层 await 会立刻抛 CancelledError，但本协程作为 shield
    的被保护任务继续跑完：计划行绝不因为取消而停在 running（终态必须被写一次）。
    """
    try:
        await _closing_stage(handle, factory, outcome, summary)
    except Exception as exc:                  # noqa: BLE001 —— 收尾异常也要把终态写完
        logger.error(f"计划收尾异常（继续写终态） {handle.plan_task_id[:8]}: {exc}", exc_info=True)
    if handle.cancel_requested and outcome != "cancelled":
        outcome, summary = "cancelled", (handle.cancel_reason or "计划已取消")
    await _write_final_state(handle, factory, outcome, summary)


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
    task_input: dict[str, Any] | None = None   # run_plan 起跑时缓存，收尾 payload/配对复用
    summary_message_id: str | None = None      # 聚合 tool 行 id（收尾幂等锚点 + 可观测）
    closing_done: bool = False                 # 收尾一旦开跑就不再重复（含取消兜底）


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
        "summary_message_id": handle.summary_message_id,
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
    task_input: dict[str, Any] | None = None
    async with factory() as session:
        raw_input = (await session.execute(
            select(BackgroundTask.task_input).where(BackgroundTask.id == plan_task_id)
        )).scalar_one_or_none()
    if isinstance(raw_input, dict):
        task_input = raw_input
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
        task_input=task_input,
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
        await _cancel_in_flight(handle, factory)
        # shield：第二次取消不得把「收尾 + 终态写入」打断在半路。
        await asyncio.shield(_close_and_finalize(handle, factory, outcome, summary))
        raise
    except Exception as exc:                  # noqa: BLE001 —— 含权限校验失败
        outcome, summary = "failed", str(exc)
        logger.error(f"❌ 计划执行异常 {handle.plan_task_id[:8]}: {exc}", exc_info=True)
    await asyncio.shield(_close_and_finalize(handle, factory, outcome, summary))


def _status_fields(handle: _PlanHandle, outcome: str, summary: str) -> dict[str, Any]:
    if outcome == "cancelled":
        code = "task.cancelled"
    elif handle.propagated_code:
        code = handle.propagated_code         # 计划 B 契约：原样上送子任务的码
    elif outcome == "failed" and handle.failed_at_step is not None:
        # §3 失败即停：有明确失败步时用可读码 + 步数参数，取代笼统 task.failed
        code = "internal.agent_plan_step_failed"
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
            else (
                {"step": handle.failed_at_step, "total": max(len(handle.steps), 1)}
                if code == "internal.agent_plan_step_failed"
                else {}
            )
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
    max_steps = _limit("agent_plan_max_steps", MAX_PLAN_STEPS)
    await _write_plan_row(
        factory, handle.plan_task_id, status="running", progress=0, started=True,
        status_message=_clip(f"计划开始执行（{total} 步）"),
        progress_details=_details(handle, "running", f"计划开始执行（{total} 步）"),
    )
    if total > max_steps:
        return "failed", f"计划步骤数 {total} 超过上限 {max_steps}"
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
    wall_clock = _limit("agent_plan_wall_clock_seconds", PLAN_WALL_CLOCK_SECONDS)
    grace = _limit("agent_plan_step_grace_seconds", STEP_GRACE_SECONDS)
    loop = asyncio.get_running_loop()
    plan_deadline = loop.time() + wall_clock
    for index, step in enumerate(handle.steps, start=1):
        if handle.cancel_requested:
            return "cancelled", _clip(handle.cancel_reason or "计划已取消", 200)
        if loop.time() > plan_deadline:
            handle.failed_at_step = index
            return "failed", _clip(
                f"计划总时长超过上限 {int(wall_clock)} 秒，"
                f"第 {index} 步未发起", 200,
            )
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
        if grace:
            await asyncio.sleep(grace)   # PR-4：步间 grace
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
    poll_timeout = _limit("agent_plan_step_poll_timeout_seconds", STEP_POLL_TIMEOUT_SECONDS)
    poll_interval = _limit("agent_plan_poll_interval_seconds", POLL_INTERVAL_SECONDS)
    loop = asyncio.get_running_loop()
    deadline = loop.time() + poll_timeout
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
                    f"步骤轮询超过 {int(poll_timeout)} 秒"
                )
            await asyncio.sleep(poll_interval)
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


def request_plan_cancellation(plan_task_id: str, *, reason: str = "计划已取消") -> bool:
    """同步请求取消：置标记 + 取消 runner 自己的 asyncio.Task。

    架构计划 §3 取消坑②：只把计划行置 cancelled 不会打断正在等待的 600s 子任务，
    所以必须握有 Task 句柄、由 CancelledError 触发的收尾路径去级联取消在途子任务。
    取消原因写 progress_details，不写 status_message——那一列已经被 cancel_task 冻结。
    """
    handle = _PLAN_HANDLES.get(plan_task_id)
    if handle is None:
        return False
    if handle.cancel_requested:
        # 幂等：标记已置起说明 runner 正在收尾（外部首请求或轮询读到已取消的计划行），
        # 再发一次 task.cancel() 会打断 _supervise 的终态写入；首个 reason 也不该被覆盖。
        return True
    handle.cancel_requested = True
    handle.cancel_reason = reason
    if handle.task is not None and not handle.task.done():
        handle.task.cancel()
    return True


async def cancel_plan(
    plan_task_id: str, *, reason: str = "计划已取消", timeout: float = CANCEL_SETTLE_TIMEOUT_SECONDS
) -> bool:
    """给 HTTP 端点用：请求取消并等 runner 把终态行写完（PR-3 的「停止计划」）。

    handle 必须在 request_plan_cancellation 之前取：runner 一结束，done 回调就把
    handle 从 _PLAN_HANDLES 弹出，那时再查已经查不到 Task 句柄了。
    """
    handle = _PLAN_HANDLES.get(plan_task_id)
    if handle is None or not request_plan_cancellation(plan_task_id, reason=reason):
        return False
    if handle.task is not None:
        await asyncio.wait({handle.task}, timeout=timeout)
    return True
