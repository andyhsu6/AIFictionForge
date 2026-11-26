"""派生状态的章节上限钳制与一致性自检（issue #288）。

为什么需要
----------
角色画像、`profile_facts`、`world_profile_synced_through_chapter` 这类聚合数据是
**按章节号推进**的。章节被删除后它们不会自动回退，于是会出现「事实引用的章节已经
不存在」的状态：轻则跳过同步、重则在提示词里把已删内容当正史。

本模块只做两件事，都不涉及删除策略（硬删/软删都适用）：
1. `clamp_derived_state_after_delete`：删章后把聚合状态钳到「当前最大存活章」；
2. `find_stale_derived_state`：扫描仍然越界的事实/标记，供自检与运维排查。
"""

from __future__ import annotations

import json
import logging
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.chapter import Chapter
from app.models.character import Character
from app.models.project import Project

logger = logging.getLogger(__name__)


async def _max_written_chapter(db: AsyncSession, project_id: str) -> int:
    """最后一个**有正文**的章号。

    不能直接用 `max(chapter_number)`：批量生成会先建空章节壳子，壳子没有正文，
    也就不会产生任何派生事实；用壳子的章号当上限会让已删章节的事实继续合法
    （实测：补齐 59–70 之后，58–62 的世界设定事实仍全部被保留）。
    """
    value = (
        await db.execute(
            select(func.max(Chapter.chapter_number)).where(
                Chapter.project_id == project_id,
                Chapter.content.is_not(None),
                func.trim(Chapter.content) != "",
            )
        )
    ).scalar()
    return int(value or 0)


def _split_facts(raw: Any, cutoff: int) -> tuple[list[dict], int]:
    """返回 (保留的事实, 丢弃数量)。无法解析时按空处理。"""
    try:
        facts = json.loads(raw or "[]")
    except (TypeError, ValueError):
        return [], 0
    if not isinstance(facts, list):
        return [], 0
    kept: list[dict] = []
    dropped = 0
    for item in facts:
        if not isinstance(item, dict):
            continue
        try:
            number = int(item.get("chapter_number") or 0)
        except (TypeError, ValueError):
            number = 0
        if number > cutoff:
            dropped += 1
            continue
        kept.append(item)
    return kept, dropped


def _prune_world_facts(raw: Any, cutoff: int) -> tuple[list[dict], int]:
    """丢弃「证据全部来自越界章节」的世界设定 claim；无证据的 claim 保留。

    只清理 provenance，不重写四个文本列：那四列是导入/同步时的渲染产物，逐条
    还原会改写既有内容（实测渲染长度与列内容并不等价），而事实列的清理能保证
    后续同步/渲染不会再把已删章节的设定带回来。
    """
    try:
        facts = json.loads(raw or "[]")
    except (TypeError, ValueError):
        return [], 0
    if not isinstance(facts, list):
        return [], 0
    kept: list[dict] = []
    dropped = 0
    for claim in facts:
        if not isinstance(claim, dict):
            continue
        evidence = claim.get("evidence")
        if isinstance(evidence, list) and evidence:
            remaining = [
                item for item in evidence
                if not (isinstance(item, dict) and int(item.get("chapter") or 0) > cutoff)
            ]
            if not remaining:
                dropped += 1
                continue
            if len(remaining) != len(evidence):
                claim = {**claim, "evidence": remaining}
        kept.append(claim)
    return kept, dropped


def _strip_future_relationship_lines(description: Optional[str], cutoff: int) -> tuple[str, int]:
    """移除 `[第N章]` 中 N > cutoff 的关系变化行，返回 (新描述, 移除行数)。"""
    if not description:
        return "", 0
    lines = description.splitlines()
    kept: list[str] = []
    removed = 0
    for line in lines:
        stripped = line.lstrip()
        if stripped.startswith("[第"):
            end_idx = stripped.find("章]")
            if end_idx > 2:
                num_str = stripped[2:end_idx]
                if num_str.isdigit() and int(num_str) > cutoff:
                    removed += 1
                    continue
        kept.append(line)
    return "\n".join(kept), removed


def _parse_chapter_marker(value: Optional[str]) -> Optional[int]:
    """解析 `'第62章'` 格式的章号；无法解析时返回 None。"""
    if not value or not isinstance(value, str):
        return None
    text = value.strip()
    if text.startswith("第") and text.endswith("章"):
        num_str = text[1:-1].strip()
        if num_str.isdigit():
            return int(num_str)
    return None


def _rerender_character_fields_from_facts(character: Character, kept_facts: list[dict]) -> None:
    """事实被裁剪后，按保留事实重渲染非手改的文本字段，防止已删章节剧透留在文本列。"""
    try:
        from app.services.profile_facts import filter_org_profile_values, render_profile
    except Exception:
        return

    kind = "organization" if getattr(character, "is_organization", False) else "character"
    rendered = filter_org_profile_values(
        render_profile(kept_facts, kind=kind),
        bool(getattr(character, "is_organization", False)),
    )
    try:
        snapshot = json.loads(character.ai_profile_snapshot or "{}")
    except (TypeError, ValueError):
        snapshot = {}
    if not isinstance(snapshot, dict):
        snapshot = {}

    snapshot_changed = False
    for field in ("personality", "background", "appearance"):
        current = getattr(character, field, None)
        if not current:
            continue
        # 若存在快照且与当前值不等，说明用户手改过，保留用户值
        if field in snapshot and current != snapshot.get(field):
            continue
        new_val = rendered.get(field)
        new_val = new_val.strip() if isinstance(new_val, str) and new_val.strip() else None
        if current != new_val:
            setattr(character, field, new_val)
            if field in snapshot:
                snapshot[field] = new_val
                snapshot_changed = True

    if snapshot_changed:
        character.ai_profile_snapshot = json.dumps(snapshot, ensure_ascii=False)


async def clamp_derived_state_after_delete(db: AsyncSession, project_id: str) -> dict[str, int]:
    """把派生状态钳到当前最大存活章；返回本次修正的计数（供日志/测试）。

    处理范围：
    - `characters.profile_facts[].chapter_number`（裁剪后同步重渲染未手改的文本列）
    - `characters.state_updated_chapter` / `profile_synced_through_chapter` / `status_changed_chapter`
    - `characters.current_state`（来源章被删时清空）
    - `character_relationships` 中 `[第N章]` 超前行、已删章新建的关系及 `ended_at`
    - `foreshadows` 中在已删章节被标记为 `resolved` 的伏笔及分析自动埋入的超前伏笔
    - `projects.world_facts` 与 `projects.world_profile_synced_through_chapter`
    """
    from app.models.foreshadow import Foreshadow
    from app.models.relationship import CharacterRelationship
    from app.services.cascade_cleanup import delete_relationship_links

    cutoff = await _max_written_chapter(db, project_id)
    stats = {
        "cutoff": cutoff,
        "dropped_facts": 0,
        "dropped_world_facts": 0,
        "cleared_state": 0,
        "clamped_markers": 0,
        "cleaned_relationships": 0,
        "rolled_back_foreshadows": 0,
    }

    characters = (
        await db.execute(select(Character).where(Character.project_id == project_id))
    ).scalars().all()
    for character in characters:
        kept, dropped = _split_facts(character.profile_facts, cutoff)
        stats["dropped_facts"] += dropped

        state_changed = False
        current_state = character.current_state
        state_chapter = character.state_updated_chapter
        if state_chapter is not None and int(state_chapter) > cutoff:
            # 置 None 而不是 cutoff：状态更新服务会用「新章号更小就跳过」防回退，
            # 置成 cutoff 会让之后对更早章节的重跑分析永远被跳过，状态无法重建。
            current_state = None
            state_chapter = None
            stats["cleared_state"] += 1
            state_changed = True

        status_changed = False
        status_chapter = character.status_changed_chapter
        if status_chapter is not None and int(status_chapter) > cutoff:
            character.status = "active"
            character.status_changed_chapter = None
            stats["clamped_markers"] += 1
            status_changed = True

        synced = character.profile_synced_through_chapter
        if synced is not None and int(synced) > cutoff:
            synced = cutoff
            stats["clamped_markers"] += 1

        if dropped or state_changed or status_changed or synced != character.profile_synced_through_chapter:
            if dropped:
                character.profile_facts = json.dumps(kept, ensure_ascii=False)
                _rerender_character_fields_from_facts(character, kept)
            if state_changed:
                character.current_state = current_state
                character.state_updated_chapter = state_chapter
            character.profile_synced_through_chapter = synced

    # 清理关系表中的超前章节行与已删章节标记
    relationships = (
        await db.execute(
            select(CharacterRelationship).where(CharacterRelationship.project_id == project_id)
        )
    ).scalars().all()
    rels_to_delete: list[str] = []
    for rel in relationships:
        new_desc, removed_lines = _strip_future_relationship_lines(rel.description, cutoff)
        ended_ch = _parse_chapter_marker(rel.ended_at)
        ended_future = ended_ch is not None and ended_ch > cutoff
        if removed_lines and not new_desc.strip() and (rel.source or "") == "analysis":
            rels_to_delete.append(rel.id)
            await db.delete(rel)
            stats["cleaned_relationships"] += 1
            continue
        if removed_lines or ended_future:
            if removed_lines:
                rel.description = new_desc
            if ended_future:
                rel.status = "active"
                rel.ended_at = None
            stats["cleaned_relationships"] += 1
    if rels_to_delete:
        await delete_relationship_links(db, rels_to_delete)

    # 回滚在已删章节被回收的伏笔，并删除分析来源的超前伏笔
    foreshadows = (
        await db.execute(select(Foreshadow).where(Foreshadow.project_id == project_id))
    ).scalars().all()
    for fs in foreshadows:
        plant_ch = fs.plant_chapter_number
        resolve_ch = fs.actual_resolve_chapter_number
        if plant_ch is not None and int(plant_ch) > cutoff and (fs.source_type or "") == "analysis":
            await db.delete(fs)
            stats["rolled_back_foreshadows"] += 1
            continue
        if resolve_ch is not None and int(resolve_ch) > cutoff:
            fs.status = "planted"
            fs.actual_resolve_chapter_id = None
            fs.actual_resolve_chapter_number = None
            fs.resolved_at = None
            fs.resolution_notes = None
            stats["rolled_back_foreshadows"] += 1

    project = (
        await db.execute(select(Project).where(Project.id == project_id))
    ).scalar_one_or_none()
    if project is not None:
        pruned, world_dropped = _prune_world_facts(project.world_facts, cutoff)
        stats["dropped_world_facts"] = world_dropped
        if world_dropped:
            project.world_facts = json.dumps(pruned, ensure_ascii=False)
        synced_chapter = project.world_profile_synced_through_chapter
        if synced_chapter is not None and int(synced_chapter) > cutoff:
            project.world_profile_synced_through_chapter = cutoff
            stats["clamped_markers"] += 1

    if (
        stats["dropped_facts"]
        or stats["dropped_world_facts"]
        or stats["cleared_state"]
        or stats["clamped_markers"]
        or stats["cleaned_relationships"]
        or stats["rolled_back_foreshadows"]
    ):
        logger.info(
            "派生状态已钳制到第 %s 章：丢弃角色事实 %s 条、世界设定事实 %s 条、"
            "清空状态 %s 个、钳制标记 %s 处、清理关系 %s 条、回滚伏笔 %s 条",
            cutoff,
            stats["dropped_facts"],
            stats["dropped_world_facts"],
            stats["cleared_state"],
            stats["clamped_markers"],
            stats["cleaned_relationships"],
            stats["rolled_back_foreshadows"],
        )
    return stats


async def find_stale_derived_state(db: AsyncSession, project_id: str) -> dict[str, Any]:
    """扫描仍然引用「不存在的章节」的派生数据（自检用，只读）。"""
    cutoff = await _max_written_chapter(db, project_id)
    offending: list[dict[str, Any]] = []
    facts_beyond = 0
    unparseable_characters = 0

    characters = (
        await db.execute(select(Character).where(Character.project_id == project_id))
    ).scalars().all()
    for character in characters:
        try:
            facts = json.loads(character.profile_facts or "[]")
        except (TypeError, ValueError):
            facts = []
            unparseable_characters += 1
        beyond = 0
        if isinstance(facts, list):
            for item in facts:
                if isinstance(item, dict):
                    try:
                        beyond += 1 if int(item.get("chapter_number") or 0) > cutoff else 0
                    except (TypeError, ValueError):
                        continue
        facts_beyond += beyond

        state_chapter = character.state_updated_chapter
        synced = character.profile_synced_through_chapter
        stale_state = state_chapter is not None and int(state_chapter) > cutoff
        stale_sync = synced is not None and int(synced) > cutoff
        if beyond or stale_state or stale_sync:
            offending.append(
                {
                    "character_id": character.id,
                    "name": character.name,
                    "facts_beyond_cutoff": beyond,
                    "state_updated_chapter": state_chapter,
                    "profile_synced_through_chapter": synced,
                    "stale_current_state": bool(stale_state and character.current_state),
                }
            )

    project = (
        await db.execute(select(Project).where(Project.id == project_id))
    ).scalar_one_or_none()
    project_sync = getattr(project, "world_profile_synced_through_chapter", None)
    world_beyond = 0
    if project is not None:
        _, world_beyond = _prune_world_facts(getattr(project, "world_facts", None), cutoff)

    total_offending = len(offending)
    detail_limit = 20
    return {
        "max_written_chapter": cutoff,
        "project_world_sync_chapter": project_sync,
        "facts_beyond_cutoff": facts_beyond,
        "world_facts_beyond_cutoff": world_beyond,
        "unparseable_characters": unparseable_characters,
        "characters": offending[:detail_limit],
        "total_offending": total_offending,
        "ok": total_offending == 0
        and world_beyond == 0
        and not (project_sync is not None and int(project_sync) > cutoff),
    }
