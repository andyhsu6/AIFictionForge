"""PR-1：创建 AgentToolCall 之前的运行期 risk 判定。

两件事：
1. action 级 risk —— 同一工具内不同 action 风险不同（见 action_risk_level）。
2. 条件免确认 —— analyze_chapter 只在"该章尚无分析结果/故事记忆"时免确认；
   探测失败或章节无法解析时一律按需要确认处理（fail-closed 到保守侧，
   与计划 B 的"未知即不合格"同向）。

判定结果写进 AgentToolCall.risk_level / requires_confirmation 与
AgentExecutionStep.detail["risk"] 以便审计。detail.risk.reason 是**审计码**
（前端按码取本地化文案，见 frontend/src/locales/*/projectAgentPanel.json 的
riskReason.*），本模块只产出这几个码：
  action_policy / overwrite_existing_analysis /
  chapter_unresolvable（模型给的章节号在本项目里对不上）/
  analysis_probe_failed（探测本身的异常，含 DB 故障）。
后两个成因不同，必须分开：前者是提示词/参数问题，后者是数据层问题，
混成一个码就没法从日志里分辨。两者都记 warning 并都 fail-closed 到 risk 2。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.logger import get_logger
from app.models.chapter import Chapter
from app.models.memory import PlotAnalysis, StoryMemory
from app.models.project import Project
from app.services.project_agent_selectors import find_chapter
from app.services.project_agent_tools import ProjectAgentTool, action_risk_level

logger = get_logger(__name__)

CONFIRMATION_RISK = 2


class _ChapterUnresolvable(Exception):
    """章节选择器解析不出目标章（模型给了项目里不存在的章节号/ID）。

    私有包装：find_chapter 用 ValueError 表达"参数对不上"，而 ValueError 也可能
    来自探测里的其他代码。只有这个类能保证 reason 分流按**结构**而不是按
    "异常类型猜成因"来判——except 顺序因此不可调换。
    """


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


async def _resolve_chapter(
    db: AsyncSession, project_id: str, arguments: dict[str, Any]
) -> Chapter:
    """find_chapter 的 ValueError 翻译成 _ChapterUnresolvable（成因分类用）。"""
    try:
        return await find_chapter(db, project_id, arguments)
    except ValueError as exc:
        raise _ChapterUnresolvable(str(exc)) from exc


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
                chapter = await _resolve_chapter(db, project.id, arguments)
                has_results = await _chapter_has_analysis_results(
                    db, project_id=project.id, chapter_id=chapter.id
                )
            if has_results:
                risk = CONFIRMATION_RISK
                reason = "overwrite_existing_analysis"
        except _ChapterUnresolvable as exc:
            # 成因一：模型给的章节号/ID 在本项目里对不上（提示词/参数问题）。
            # 与探测故障同样不得放行覆盖，但审计码必须分开，否则日志里
            # "模型在瞎报章节" 和 "数据库读不出来" 长得一模一样。
            logger.warning(
                "灵创助手 analyze_chapter 风险探测无法解析章节，fail-closed 到需要确认："
                "project=%s action=%s error=%s",
                project.id, action, exc, exc_info=True,
            )
            risk = CONFIRMATION_RISK
            reason = "chapter_unresolvable"
        except Exception as exc:
            # 成因二：探测本身出错（含真实 SQL 异常）。探测不出结论时不得放行覆盖：
            # 重新分析会替换该章既有的 PlotAnalysis
            # （chapter_id 唯一，见 app/models/memory.py:86），并清理由分析产生的
            # 旧伏笔（foreshadow_service.py:1074/1195），属于破坏性写入。
            logger.warning(
                "灵创助手 analyze_chapter 风险探测失败，fail-closed 到需要确认："
                "project=%s action=%s error=%s",
                project.id, action, exc, exc_info=True,
            )
            risk = CONFIRMATION_RISK
            reason = "analysis_probe_failed"

    return RiskDecision(
        risk_level=risk,
        requires_confirmation=risk >= CONFIRMATION_RISK,
        action=action,
        reason=reason,
    )
