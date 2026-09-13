"""PR-1：创建 AgentToolCall 之前的运行期 risk 判定。

两件事：
1. action 级 risk —— 同一工具内不同 action 风险不同（见 action_risk_level）。
2. 条件免确认 —— analyze_chapter 只在"该章尚无分析结果/故事记忆"时免确认；
   探测失败或章节无法解析时一律按需要确认处理（fail-closed 到保守侧，
   与计划 B 的"未知即不合格"同向）。

判定结果写进 AgentToolCall.risk_level / requires_confirmation 与
AgentExecutionStep.detail["risk"] 以便审计。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.memory import PlotAnalysis, StoryMemory
from app.models.project import Project
from app.services.project_agent_selectors import find_chapter
from app.services.project_agent_tools import ProjectAgentTool, action_risk_level

CONFIRMATION_RISK = 2


@dataclass(frozen=True)
class RiskDecision:
    risk_level: int
    requires_confirmation: bool
    action: str | None
    reason: str


def _action(arguments: dict[str, Any]) -> str | None:
    value = arguments.get("action")
    if isinstance(value, str) and value.strip():
        return value
    return None


async def _chapter_has_analysis_results(
    db: AsyncSession, *, project_id: str, chapter_id: str
) -> bool:
    """只读探测：该章是否已有 PlotAnalysis 或非空 StoryMemory。

    谓词与 ProjectAgentOperationalTools._get_chapter_analysis 一致，但只回布尔，
    不把分析全文和记忆全文塞进判定路径。
    """
    analysis_row = await db.execute(
        select(PlotAnalysis.id).where(
            PlotAnalysis.project_id == project_id,
            PlotAnalysis.chapter_id == chapter_id,
        ).limit(1)
    )
    if analysis_row.scalar_one_or_none() is not None:
        return True
    memory_row = await db.execute(
        select(func.count(StoryMemory.id)).where(
            StoryMemory.project_id == project_id,
            StoryMemory.chapter_id == chapter_id,
        )
    )
    return (memory_row.scalar_one() or 0) > 0


async def resolve_tool_risk(
    db: AsyncSession,
    *,
    project: Project,
    tool: ProjectAgentTool,
    arguments: dict[str, Any],
) -> RiskDecision:
    """工具级 + action 级 + 条件判定；任何异常都收口到"需要确认"。"""
    action = _action(arguments)
    reason = "action_policy"
    risk = action_risk_level(tool, arguments)

    if (
        tool.name == "start_project_task"
        and action == "analyze_chapter"
        and risk < CONFIRMATION_RISK
    ):
        # 探针必须整体跑在 SAVEPOINT 里，不能只靠 except 收口：
        # app/config.py 的默认 DATABASE_URL 是 postgresql+asyncpg，而 PostgreSQL
        # 上一条语句报错会**中止整个事务**，此后主流程的 flush/commit 直接抛
        # PendingRollbackError ⇒ 用户拿到 500，而不是设计要求的确认卡。
        # begin_nested() 只回滚保存点（SAVEPOINT 在报错语句之前发出，本轮已 flush
        # 的 user 消息与 step 留在外层事务里不受影响）；改用 db.rollback() 虽然也
        # 能清掉中止状态，但会把本轮已写的行一起丢掉，是更糟的修法。
        try:
            async with db.begin_nested():
                chapter = await find_chapter(db, project.id, arguments)
                has_results = await _chapter_has_analysis_results(
                    db, project_id=project.id, chapter_id=chapter.id
                )
            if has_results:
                risk = CONFIRMATION_RISK
                reason = "overwrite_existing_analysis"
        except Exception:
            # 探测不出结论时不得放行覆盖：重新分析会替换该章既有的 PlotAnalysis
            # （chapter_id 唯一，见 app/models/memory.py:86），并清理由分析产生的
            # 旧伏笔（foreshadow_service.py:1074/1195），属于破坏性写入。
            risk = CONFIRMATION_RISK
            reason = "analysis_probe_failed"

    return RiskDecision(
        risk_level=risk,
        requires_confirmation=risk >= CONFIRMATION_RISK,
        action=action,
        reason=reason,
    )
