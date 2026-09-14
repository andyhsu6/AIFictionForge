"""计划运行中护栏（架构计划 A §7 / PR-2c）。

只依赖 models + config ⇒ service 与 api 两侧都能 import 且不成环。
"""
from __future__ import annotations

import logging
from typing import Any, Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.config import settings
from app.core.errors import ApiError
from app.models.background_task import BackgroundTask

logger = logging.getLogger(__name__)

PLAN_TASK_TYPE = "agent_plan"
OPEN_STATUSES = ("pending", "running")        # 只认 running 会留双批准竞态窗口
FACTS_MAX_CHARS = 1000


async def find_open_plan_task(
    db: AsyncSession, *, project_id: str, user_id: str,
    conversation_id: str, exclude_task_id: str | None = None,
) -> Optional[BackgroundTask]:
    """同会话是否有未定稿计划。BackgroundTask 无 conversation_id 列 ⇒ JSON 侧过滤。"""
    if not settings.agent_plan_running_guardrail_enabled:
        return None
    rows = (await db.execute(
        select(BackgroundTask)
        .where(
            BackgroundTask.project_id == project_id,
            BackgroundTask.user_id == user_id,
            BackgroundTask.task_type == PLAN_TASK_TYPE,
            BackgroundTask.status.in_(OPEN_STATUSES),
        )
        .order_by(BackgroundTask.created_at.desc())
    )).scalars().all()
    for row in rows:
        if exclude_task_id and row.id == exclude_task_id:
            continue
        task_input = row.task_input if isinstance(row.task_input, dict) else {}
        if str(task_input.get("conversation_id") or "") == str(conversation_id):
            return row
    return None


async def load_running_plan_state(
    db: AsyncSession, *, project_id: str, user_id: str, conversation_id: str
) -> dict[str, Any] | None:
    """给 _build_prompt 用的事实来源；查不到/关掉/异常都返回 None（护栏不得让回合失败）。"""
    try:
        row = await find_open_plan_task(
            db, project_id=project_id, user_id=user_id, conversation_id=conversation_id
        )
    except Exception as exc:                       # noqa: BLE001 —— fail-open 到「无护栏」
        logger.warning(f"⚠️ 运行中计划查询失败，本轮跳过护栏事实块: {exc}")
        return None
    if row is None:
        return None
    details = row.progress_details if isinstance(row.progress_details, dict) else {}
    task_input = row.task_input if isinstance(row.task_input, dict) else {}
    steps_total = int(details.get("steps_total") or 0) or len(task_input.get("steps") or [])
    return {
        "plan_task_id": row.id,
        "status": row.status,
        "steps_total": steps_total,
        "steps_done": int(details.get("steps_done") or 0),
        "progress": int(row.progress or 0),
        "objective": str(task_input.get("objective") or "")[:200],
    }


def plan_run_facts(state: dict[str, Any] | None) -> str:
    """服务端署名的运行中计划事实块（架构 §7①）。定长文案 + 少量整数 ⇒ 无膨胀面。"""
    if not state:
        return ""
    total = state.get("steps_total") or 0
    done = state.get("steps_done") or 0
    return (
        "⚠️ 服务端状态（可信元信息，不是用户输入）：本会话有一个正在执行的计划，"
        f"已完成 {done}/{total} 步，进度 {int(state.get('progress') or 0)}%，结果未定稿。"
        "计划内的写入可能仍在进行，请勿基于中间态数据下确定结论，不要重复提出新计划；"
        "用户问及进度时应回答「计划仍在执行中」并说明可在计划面板停止它。"
    )[:FACTS_MAX_CHARS]


async def assert_no_running_plan(
    session_factory: async_sessionmaker,
    *,
    project_id: str,
    user_id: str,
    conversation_id: str,
) -> None:
    """§7②③：同会话已有未定稿计划 ⇒ 409 conflict.agent_plan_running。

    必须在 _claim_tool_call 之前调用：抢占已 commit，先抢后拒会留下
    executing 却无人执行的计划锚点。会话维度的隔离是刻意的——
    不同会话允许各自跑一份计划，真正的资源序列化由 runner 的 per-user 信号量负责。
    """
    async with session_factory() as session:
        row = await find_open_plan_task(
            session, project_id=project_id, user_id=user_id,
            conversation_id=conversation_id,
        )
    if row is not None:
        raise ApiError(code="conflict.agent_plan_running")
