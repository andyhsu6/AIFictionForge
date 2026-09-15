"""删除父行时显式清理子行的共享助手。

背景：`backend/app/database.py` 只为 SQLite 设置了 WAL/同步/超时等 PRAGMA，
没有启用 `PRAGMA foreign_keys=ON`；因此模型里声明的 `ondelete="CASCADE"` /
`ondelete="SET NULL"` 在删除路径上不会生效，必须由代码显式清理（或断开）子行，
否则会留下悬空外键行（`PRAGMA foreign_key_check` 可检出）。

本模块只做批量 SQL，不做业务判断与提交；调用方负责事务边界与权限校验。
空 id 列表直接返回，避免发出空 `IN ()` 查询。
"""
from __future__ import annotations

from typing import Iterable, Optional

from sqlalchemy import delete, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.analysis_task import AnalysisTask
from app.models.generation_history import GenerationHistory
from app.models.memory import PlotAnalysis, StoryMemory
from app.models.project_default_style import ProjectDefaultStyle
from app.models.regeneration_task import RegenerationTask
from app.models.relationship import CharacterRelationship, RelationshipTypeLink


def _dedupe_ids(values: Optional[Iterable[str]]) -> list[str]:
    """去重并过滤空值，保证不发出空 `IN` 查询。"""
    if not values:
        return []
    seen: dict[str, None] = {}
    for value in values:
        if value:
            seen[str(value)] = None
    return list(seen)


async def _delete_by_ids(db: AsyncSession, model, column, ids: list[str]) -> int:
    result = await db.execute(delete(model).where(column.in_(ids)))
    return result.rowcount or 0


async def delete_relationship_links(
    db: AsyncSession, relationship_ids: Optional[Iterable[str]]
) -> int:
    """删除角色关系的多对多类型关联行（character_relationship_type_links）。"""
    ids = _dedupe_ids(relationship_ids)
    if not ids:
        return 0
    return await _delete_by_ids(
        db, RelationshipTypeLink, RelationshipTypeLink.relationship_id, ids
    )


async def delete_chapter_children(
    db: AsyncSession, chapter_ids: Optional[Iterable[str]]
) -> dict[str, int]:
    """清理给定章节的所有子行，并断开声明为 SET NULL 的章节外键。

    - 删除 plot_analysis / story_memories / analysis_tasks / regeneration_tasks；
    - `generation_history.chapter_id` 按声明置空（保留历史行本身）。
    """
    ids = _dedupe_ids(chapter_ids)
    counts = {
        "plot_analysis": 0,
        "story_memories": 0,
        "analysis_tasks": 0,
        "regeneration_tasks": 0,
        "generation_history_unlinked": 0,
    }
    if not ids:
        return counts

    counts["plot_analysis"] = await _delete_by_ids(
        db, PlotAnalysis, PlotAnalysis.chapter_id, ids
    )
    counts["story_memories"] = await _delete_by_ids(
        db, StoryMemory, StoryMemory.chapter_id, ids
    )
    counts["analysis_tasks"] = await _delete_by_ids(
        db, AnalysisTask, AnalysisTask.chapter_id, ids
    )
    counts["regeneration_tasks"] = await _delete_by_ids(
        db, RegenerationTask, RegenerationTask.chapter_id, ids
    )
    history_result = await db.execute(
        update(GenerationHistory)
        .where(GenerationHistory.chapter_id.in_(ids))
        .values(chapter_id=None)
    )
    counts["generation_history_unlinked"] = history_result.rowcount or 0
    return counts


async def delete_project_children(
    db: AsyncSession, project_id: str, *, include_default_styles: bool = True
) -> dict[str, int]:
    """清理项目维度的子行（项目删除与覆盖导入共用）。

    - 删除 plot_analysis / story_memories / analysis_tasks / regeneration_tasks
      （按 project_id）；
    - `include_default_styles=True`（默认，项目删除）时一并删除
      project_default_styles；覆盖导入传入 `False`，因为项目仍然存活，
      其默认写作风格不是悬空行，必须保留；
    - `generation_history.chapter_id` 置空（该项目所有章节即将被删除）；
    - 删除项目下所有角色关系对应的类型关联行。
    """
    counts = {
        "project_default_styles": 0,
        "plot_analysis": 0,
        "story_memories": 0,
        "analysis_tasks": 0,
        "regeneration_tasks": 0,
        "generation_history_unlinked": 0,
        "relationship_type_links": 0,
    }
    if not project_id:
        return counts

    if include_default_styles:
        counts["project_default_styles"] = (
            await db.execute(
                delete(ProjectDefaultStyle).where(ProjectDefaultStyle.project_id == project_id)
            )
        ).rowcount or 0
    counts["plot_analysis"] = (
        await db.execute(
            delete(PlotAnalysis).where(PlotAnalysis.project_id == project_id)
        )
    ).rowcount or 0
    counts["story_memories"] = (
        await db.execute(
            delete(StoryMemory).where(StoryMemory.project_id == project_id)
        )
    ).rowcount or 0
    counts["analysis_tasks"] = (
        await db.execute(
            delete(AnalysisTask).where(AnalysisTask.project_id == project_id)
        )
    ).rowcount or 0
    counts["regeneration_tasks"] = (
        await db.execute(
            delete(RegenerationTask).where(RegenerationTask.project_id == project_id)
        )
    ).rowcount or 0
    counts["generation_history_unlinked"] = (
        await db.execute(
            update(GenerationHistory)
            .where(GenerationHistory.project_id == project_id)
            .values(chapter_id=None)
        )
    ).rowcount or 0

    relationship_ids = (
        await db.execute(
            select(CharacterRelationship.id).where(
                CharacterRelationship.project_id == project_id
            )
        )
    ).scalars().all()
    counts["relationship_type_links"] = await delete_relationship_links(
        db, relationship_ids
    )
    return counts
