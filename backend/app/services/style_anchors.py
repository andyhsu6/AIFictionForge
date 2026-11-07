"""本书原文语感锚点（issue #254 P1）。

只存章节定位与哈希，不复制原文：生成时按偏移从 `Chapter.content` 取片段，
哈希不符（章节被编辑/删除）则跳过。注入方式为「模板渲染后追加」，用户自定义
模板无需包含 `{style_anchors}` 变量。
"""
from __future__ import annotations

import hashlib
import re
from typing import Any, Optional

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.chapter import Chapter
from app.models.project import Project
from app.models.project_style_anchor import ProjectStyleAnchor

# 单条锚点长度（字符）与总量上限（计划 §6.3）
ANCHOR_MIN_CHARS = 150
ANCHOR_MAX_CHARS = 400
MAX_ANCHOR_CHARS = 2000
MIN_ANCHOR_COUNT = 2
MAX_ANCHOR_COUNT = 4
DEFAULT_ANCHOR_CHARS = 800
DEFAULT_ANCHOR_COUNT = 3

_QUOTE_RE = re.compile(r"“[^”]*”|「[^」]*」|\"[^\"]*\"")
_SENTENCE_END = "。！？…!?；;"


def fragment_hash(text: str) -> str:
    return hashlib.sha256((text or "").encode("utf-8")).hexdigest()


def _dialogue_ratio(text: str) -> float:
    plain = re.sub(r"\s+", "", text or "")
    if not plain:
        return 0.0
    dialogue = sum(len(re.sub(r"\s+", "", m)) for m in _QUOTE_RE.findall(text))
    return dialogue / len(plain)


def _cut_positions(text: str) -> list[int]:
    cuts = {0, len(text)}
    for index, char in enumerate(text):
        if char == "\n" or char in _SENTENCE_END:
            cuts.add(index + 1)
    return sorted(cuts)


def _split_fragments(text: str, target: int = 260) -> list[tuple[int, int]]:
    """把一段文本按完整段落/句子切成长度接近 target 的片段。"""
    length = len(text)
    if length <= ANCHOR_MAX_CHARS:
        return [(0, length)]
    cuts = _cut_positions(text)
    fragments: list[tuple[int, int]] = []
    start = 0
    while start < length:
        target_end = min(length, start + target)
        candidate = next(
            (cut for cut in cuts if target_end <= cut <= start + ANCHOR_MAX_CHARS),
            None,
        )
        end = candidate if candidate is not None else min(length, start + ANCHOR_MAX_CHARS)
        if end - start >= ANCHOR_MIN_CHARS or not fragments:
            fragments.append((start, end))
        start = end
    return fragments


def _clamp_settings(project: Project) -> tuple[bool, int, int]:
    enabled = bool(getattr(project, "style_anchor_enabled", True))
    chars = int(getattr(project, "style_anchor_chars", DEFAULT_ANCHOR_CHARS) or DEFAULT_ANCHOR_CHARS)
    count = int(getattr(project, "style_anchor_count", DEFAULT_ANCHOR_COUNT) or DEFAULT_ANCHOR_COUNT)
    chars = max(ANCHOR_MIN_CHARS, min(chars, MAX_ANCHOR_CHARS))
    count = max(MIN_ANCHOR_COUNT, min(count, MAX_ANCHOR_COUNT))
    return enabled, chars, count


async def create_project_style_anchors(
    db: AsyncSession,
    *,
    project_id: str,
    ranges: list[dict[str, Any]],
    chapter_contents: Optional[dict[int, str]] = None,
) -> int:
    """按抽样区间重建项目锚点（幂等：先删后插）。

    `ranges` 形如 `[{"chapter": 1, "start": 0, "end": 1800}]`，来自
    `build_style_sample().meta["ranges"]`。`chapter_contents` 可选：提供时按
    章号取正文，否则从 `Chapter` 表读取。
    """
    chapters = (
        await db.execute(select(Chapter).where(Chapter.project_id == project_id))
    ).scalars().all()
    chapter_by_number = {chapter.chapter_number: chapter for chapter in chapters}
    if not chapter_by_number:
        return 0
    # 先清旧锚点再判断：重抽后样本区间可能为空，不能残留上一轮定位
    await db.execute(
        delete(ProjectStyleAnchor).where(ProjectStyleAnchor.project_id == project_id)
    )

    if not ranges:
        return 0

    created = 0
    for entry in ranges:
        if not isinstance(entry, dict):
            continue
        number = entry.get("chapter")
        if not isinstance(number, int):
            continue
        chapter = chapter_by_number.get(number)
        if chapter is None:
            continue
        content = (
            (chapter_contents or {}).get(number)
            if chapter_contents is not None
            else chapter.content
        ) or ""
        if not content:
            continue
        start = max(0, int(entry.get("start") or 0))
        end = min(len(content), int(entry.get("end") or 0))
        if end <= start:
            continue
        for frag_start, frag_end in _split_fragments(content[start:end]):
            absolute_start = start + frag_start
            absolute_end = start + frag_end
            fragment = content[absolute_start:absolute_end]
            if len(fragment.strip()) < 20:
                continue
            # 纯对话段不作为锚点（避免模型复读对白）
            if _dialogue_ratio(fragment) > 0.6:
                continue
            db.add(
                ProjectStyleAnchor(
                    project_id=project_id,
                    chapter_id=chapter.id,
                    start_offset=absolute_start,
                    end_offset=absolute_end,
                    text_hash=fragment_hash(fragment),
                    order_index=created,
                )
            )
            created += 1

    await db.flush()
    return created


async def load_style_anchor_fragments(
    db: AsyncSession,
    *,
    project: Project,
    before_chapter_number: Optional[int] = None,
) -> list[str]:
    """读取锚点片段；哈希不匹配（章节被编辑）的锚点自动跳过。"""
    enabled, char_budget, count = _clamp_settings(project)
    if not enabled:
        return []

    rows = (
        await db.execute(
            select(ProjectStyleAnchor, Chapter)
            .join(Chapter, Chapter.id == ProjectStyleAnchor.chapter_id)
            .where(ProjectStyleAnchor.project_id == project.id)
            .order_by(ProjectStyleAnchor.order_index)
        )
    ).all()

    candidates: list[tuple[int, str]] = []
    seen_hashes: set[str] = set()
    for anchor, chapter in rows:
        if before_chapter_number is not None and chapter.chapter_number >= before_chapter_number:
            continue
        content = chapter.content or ""
        fragment = content[anchor.start_offset:anchor.end_offset]
        if not fragment or fragment_hash(fragment) != anchor.text_hash:
            continue
        if anchor.text_hash in seen_hashes:
            continue
        seen_hashes.add(anchor.text_hash)
        candidates.append((chapter.chapter_number, fragment))

    # 优先目标章之前最近的章节
    candidates.sort(key=lambda item: item[0], reverse=True)

    selected: list[str] = []
    used = 0
    for _, fragment in candidates:
        if len(selected) >= count:
            break
        remaining = char_budget - used
        if remaining <= 0:
            break
        if len(fragment) > remaining:
            fragment = fragment[:remaining]
        if len(fragment.strip()) < 20:
            continue
        selected.append(fragment)
        used += len(fragment)
    return selected


def build_style_anchor_block(fragments: list[str]) -> str:
    if not fragments:
        return ""
    lines = [
        "",
        "【本书原文语感锚点（仅模仿句长、节奏与叙述距离；禁止复述、改写或引用其中内容）】",
    ]
    for index, fragment in enumerate(fragments, 1):
        lines.append(f"[锚点 {index}]")
        lines.append(fragment)
    return "\n".join(lines)


async def append_project_style_anchors(
    db: AsyncSession,
    *,
    project: Project,
    style_content: str,
    before_chapter_number: Optional[int] = None,
) -> str:
    """把项目锚点块追加到风格内容；关闭/无锚点时原样返回。"""
    if not style_content:
        return style_content
    fragments = await load_style_anchor_fragments(
        db, project=project, before_chapter_number=before_chapter_number
    )
    block = build_style_anchor_block(fragments)
    if not block:
        return style_content
    return f"{style_content.rstrip()}\n{block}"
