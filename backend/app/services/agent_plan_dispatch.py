"""计划任务落库 + 执行器调度接缝（PR-2a 建立，PR-2b 注入实现）。

`_PLAN_RUNNER` 未注入时批准返回 501 —— 这就是回滚判据的物理形态：
PR-2b 只需在 main 的 lifespan 里 `register_plan_runner(run_plan)`，不注册即回到
本 PR 之前的行为（规划回合只出卡片，没人执行）。

两个硬约束：
1. **先判可用，再建任务行**（`plan_runner()`）：未注册时若已建了
   `BackgroundTask(status='pending')`，它就是一条永远不会被执行的孤儿行，
   任务面板会一直显示"进行中"。
2. **先提交，再调度**：runner 用自己的 session 反查任务行，未提交的行读不到。
"""
from __future__ import annotations

from typing import Any, Awaitable, Callable, Optional

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import ApiError
from app.logger import get_logger
from app.models.background_task import BackgroundTask

logger = get_logger(__name__)

PLAN_TASK_TYPE = "agent_plan"
PLAN_RUNNER_UNAVAILABLE_CODE = "internal.agent_plan_not_available"

_RunPlan = Callable[..., Awaitable[Any]]
_PLAN_RUNNER: Optional[_RunPlan] = None


def register_plan_runner(runner_func: _RunPlan) -> None:
    """PR-2b 在应用启动时注入执行器；不注入即保持 501。"""
    global _PLAN_RUNNER
    _PLAN_RUNNER = runner_func


def plan_runner() -> Optional[_RunPlan]:
    """供 service 与批准端点在**建任务行之前**判断执行器是否可用。"""
    return _PLAN_RUNNER


def plan_tool_names(plan: dict[str, Any]) -> set[str]:
    """已存计划里出现过的工具名。

    批准时不再查 MCP loader：计划里的 MCP 步骤若届时不可用，由 PR-2b 逐步失败并记录，
    而不是让整个计划被 400 拒收（那样用户连重试的卡片都没有）。
    """
    names: set[str] = set()
    for step in (plan or {}).get("steps") or []:
        if isinstance(step, dict) and isinstance(step.get("tool"), str):
            names.add(step["tool"])
    return names


def build_plan_task_input(
    *, conversation_id: str, tool_call_id: str, plan: dict[str, Any]
) -> dict[str, Any]:
    """task_input 的两个锚点缺一个就不许建任务行。

    PR-2b 靠 `tool_call_id` 反查计划归属、靠 `conversation_id` 把进度写回会话；
    缺任何一个都会留下一条无法收尾的任务行。
    """
    if not conversation_id or not tool_call_id:
        raise ValueError("plan task_input 必须同时携带 conversation_id 与 tool_call_id")
    return {
        "tool_call_id": tool_call_id,
        "conversation_id": conversation_id,
        "objective": plan["objective"],
        "steps": plan["steps"],
    }


async def create_plan_task(
    db: AsyncSession,
    *,
    project_id: str,
    user_id: str,
    conversation_id: str,
    tool_call_id: str,
    plan: dict[str, Any],
) -> BackgroundTask:
    """建 agent_plan 任务行（只 flush 不 commit，由调用方与状态变更同事务提交）。"""
    task = BackgroundTask(
        user_id=user_id,
        project_id=project_id,
        task_type=PLAN_TASK_TYPE,
        status="pending",
        progress=0,
        cancel_requested=False,
        task_input=build_plan_task_input(
            conversation_id=conversation_id, tool_call_id=tool_call_id, plan=plan
        ),
    )
    db.add(task)
    await db.flush()
    return task


async def dispatch_plan(
    *,
    plan_task_id: str,
    user_id: str,
    project_id: str,
    conversation_id: str,
    steps: list[dict[str, Any]],
) -> Any:
    """把已批准的计划交给执行器；未注册 ⇒ 抛带错误码的 501。"""
    runner = _PLAN_RUNNER
    if runner is None:
        raise ApiError(code=PLAN_RUNNER_UNAVAILABLE_CODE)
    return await runner(
        plan_task_id=plan_task_id,
        user_id=user_id,
        project_id=project_id,
        conversation_id=conversation_id,
        steps=steps,
    )


async def build_closing_ai_service(user_id: str) -> Any:
    """收尾 LLM 用的用户级 AIService：detached runner 拿不到请求态实例。

    构建失败返回 None（runner 收尾降级为只写聚合消息），绝不阻断计划调度。
    """
    if not user_id:
        return None
    try:
        from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

        from app.api.settings import get_user_ai_service_from_db
        from app.database import get_engine

        engine = await get_engine(user_id)
        factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
        async with factory() as session:
            service = await get_user_ai_service_from_db(user_id, session)
        # 收尾调用强制 auto_mcp=False / handle_tool_calls=False，不会再碰这个 session。
        service.db_session = None
        return service
    except Exception as exc:                  # noqa: BLE001 —— 收尾缺失不得阻断调度
        logger.warning(f"计划收尾 AI 服务构建失败（收尾降级为聚合消息）: {exc}")
        return None
