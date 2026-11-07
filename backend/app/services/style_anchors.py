"""本书原文语感锚点（issue #254 P1）。

只存章节定位与哈希，不复制原文：生成时按偏移从 `Chapter.content` 取片段，
哈希不符（章节被编辑/删除）则跳过。注入方式为「模板渲染后追加」，用户自定义
模板无需包含 `{style_anchors}` 变量。
"""
from __future__ import annotations

import hashlib
import json
import re
from typing import Any, Optional

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.chapter import Chapter
from app.models.project import Project
from app.models.project_default_style import ProjectDefaultStyle
from app.models.project_style_anchor import ProjectStyleAnchor
from app.models.writing_style import WritingStyle
from app.services.style_profile import profile_preset_id

# 单条锚点长度（字符）与总量上限（计划 §6.3）
ANCHOR_MIN_CHARS = 150
ANCHOR_MAX_CHARS = 400
MAX_ANCHOR_CHARS = 2000
MIN_ANCHOR_COUNT = 2
MAX_ANCHOR_COUNT = 4
DEFAULT_ANCHOR_CHARS = 800
DEFAULT_ANCHOR_COUNT = 3

# 纯对话段阈值（计划 §6.3）：默认 0.6；画像本身对话占比低（< 0.3）时降到 0.4
DEFAULT_DIALOGUE_LIMIT = 0.6
LOW_PROFILE_DIALOGUE_LIMIT = 0.4
LOW_PROFILE_DIALOGUE_RATIO = 0.3

# 目标章已有正文（续写）时的防复读检测：最近 2000 字，20 字滑窗
TARGET_CONTEXT_CHARS = 2000
OVERLAP_WINDOW = 20

_QUOTE_RE = re.compile(r"“[^”]*”|「[^」]*」|\"[^\"]*\"")
_SENTENCE_END = "。！？…!?；;"
_NON_WORD_RE = re.compile(r"[\s\W_]+")
_FIRST_PERSON_RE = re.compile(r"[我咱俺]")
_THIRD_PERSON_RE = re.compile(r"[他她它]")


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


def _override_int(override: dict[str, Any], key: str) -> Optional[int]:
    value = override.get(key)
    if value is None or isinstance(value, bool):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _clamp_settings(
    project: Project, override: Optional[dict[str, Any]] = None
) -> tuple[bool, int, int]:
    """解析锚点选项：请求覆盖 > 项目列值 > 默认，再做边界夹取（计划 §6.3）。"""
    override = override or {}

    enabled_override = override.get("enabled")
    if enabled_override is None:
        enabled = bool(getattr(project, "style_anchor_enabled", True))
    else:
        enabled = bool(enabled_override)

    chars = _override_int(override, "chars")
    if chars is None:
        chars = int(
            getattr(project, "style_anchor_chars", DEFAULT_ANCHOR_CHARS)
            or DEFAULT_ANCHOR_CHARS
        )
    count = _override_int(override, "count")
    if count is None:
        count = int(
            getattr(project, "style_anchor_count", DEFAULT_ANCHOR_COUNT)
            or DEFAULT_ANCHOR_COUNT
        )

    chars = max(ANCHOR_MIN_CHARS, min(chars, MAX_ANCHOR_CHARS))
    count = max(MIN_ANCHOR_COUNT, min(count, MAX_ANCHOR_COUNT))
    return enabled, chars, count


def _normalize_perspective(value: Any) -> Optional[str]:
    """把项目/画像里的视角表述归一为 "first"/"third"；无法判定返回 None。"""
    if not isinstance(value, str):
        return None
    text = value.strip().lower()
    if not text:
        return None
    if "第一" in text or "first" in text:
        return "first"
    if "第二" in text or "second" in text:
        return None
    if "第三" in text or "third" in text or "全知" in text or "omniscient" in text:
        return "third"
    return None


def _detect_perspective(text: str) -> Optional[str]:
    """轻量视角判断（不依赖导入服务私有方法）：人称代词密度对比。

    明确第一人称要求我方人称数量显著多于第三人称，临界/混杂样本返回 None
    （不参与同视角加权，避免误分组）。
    """
    snippet = (text or "")[:6000]
    if not snippet.strip():
        return None
    first = len(_FIRST_PERSON_RE.findall(snippet))
    third = len(_THIRD_PERSON_RE.findall(snippet))
    if first >= 3 and first > third * 1.5:
        return "first"
    if third >= 3 and third > first * 1.5:
        return "third"
    return None


def _char_windows(text: str, window: int) -> set[str]:
    """去空白/标点后的 N 字连续滑窗（用于目标章正文与锚点片段的相似检测）。"""
    normalized = _NON_WORD_RE.sub("", text or "")
    if len(normalized) < window:
        return set()
    return {normalized[index : index + window] for index in range(len(normalized) - window + 1)}


async def _profile_dialogue_ratio(
    db: AsyncSession, project: Project
) -> Optional[float]:
    """项目默认画像的 metrics.dialogue_ratio；画像缺失/解析失败返回 None。"""
    profile_json = (
        await db.execute(
            select(WritingStyle.profile_json)
            .join(ProjectDefaultStyle, ProjectDefaultStyle.style_id == WritingStyle.id)
            .where(ProjectDefaultStyle.project_id == project.id)
        )
    ).scalar_one_or_none()
    if profile_json is None:
        # 默认风格可能被用户换成非画像行，画像行仍按 preset_id 约定兜底
        profile_json = (
            await db.execute(
                select(WritingStyle.profile_json).where(
                    WritingStyle.preset_id == profile_preset_id(project.id)
                )
            )
        ).scalar_one_or_none()
    if not profile_json:
        return None
    try:
        data = json.loads(profile_json)
        ratio = data["metrics"]["dialogue_ratio"]
    except (TypeError, ValueError, KeyError):
        return None
    if isinstance(ratio, bool) or not isinstance(ratio, (int, float)):
        return None
    return float(ratio)


def _dialogue_limit(profile_ratio: Optional[float]) -> float:
    if profile_ratio is not None and profile_ratio < LOW_PROFILE_DIALOGUE_RATIO:
        return LOW_PROFILE_DIALOGUE_LIMIT
    return DEFAULT_DIALOGUE_LIMIT


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
    override: Optional[dict[str, Any]] = None,
) -> list[str]:
    """读取锚点片段；哈希不匹配（章节被编辑）的锚点自动跳过。

    选择策略（计划 §6.3）：同视角优先（只降权不排除）→ 章节邻近（目标章之前
    越近越前）→ order_index。目标章已有正文时，与最近 2000 字重叠的锚点跳过
    （防复读）。纯对话段阈值随项目画像的 dialogue_ratio 动态化。
    """
    enabled, char_budget, count = _clamp_settings(project, override)
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

    target_perspective = _normalize_perspective(
        getattr(project, "narrative_perspective", None)
    )
    target_windows: set[str] = set()
    if before_chapter_number is not None:
        target_chapter = (
            await db.execute(
                select(Chapter).where(
                    Chapter.project_id == project.id,
                    Chapter.chapter_number == before_chapter_number,
                )
            )
        ).scalars().first()
        if target_chapter is not None and target_chapter.content:
            # 目标章正文的视角优先于项目设置（续写时以实际写作为准）
            target_perspective = (
                _detect_perspective(target_chapter.content) or target_perspective
            )
            target_windows = _char_windows(
                target_chapter.content[-TARGET_CONTEXT_CHARS:], OVERLAP_WINDOW
            )

    dialogue_limit = _dialogue_limit(await _profile_dialogue_ratio(db, project))

    candidates: list[tuple[bool, int, int, str]] = []
    chapter_perspectives: dict[int, Optional[str]] = {}
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
        if _dialogue_ratio(fragment) > dialogue_limit:
            continue
        # 目标章已写过同样的内容：注入只会诱发复读
        if target_windows and (_char_windows(fragment, OVERLAP_WINDOW) & target_windows):
            continue
        seen_hashes.add(anchor.text_hash)

        number = chapter.chapter_number
        if number not in chapter_perspectives:
            chapter_perspectives[number] = _detect_perspective(content)
        same_perspective = (
            target_perspective is not None
            and chapter_perspectives[number] == target_perspective
        )
        candidates.append((same_perspective, number, anchor.order_index, fragment))

    # 同视角分组在前；组内按目标章之前最近→更远；同章按 order_index
    candidates.sort(key=lambda item: (0 if item[0] else 1, -item[1], item[2]))

    selected: list[str] = []
    used = 0
    for _, _, _, fragment in candidates:
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
    override: Optional[dict[str, Any]] = None,
) -> str:
    """把项目锚点块追加到风格内容；关闭/无锚点时原样返回。

    `override` 支持 `enabled`/`chars`/`count` 的每请求覆盖（请求值 > 项目列值 >
    默认），边界仍按 char/count 上下限夹取。

    未选任何写作风格时（``style_content`` 为空），锚点块单独作为 P1 风格内容
    返回——锚点是本书级语感约束，不应因用户未选风格而静默失效。
    """
    fragments = await load_style_anchor_fragments(
        db,
        project=project,
        before_chapter_number=before_chapter_number,
        override=override,
    )
    block = build_style_anchor_block(fragments)
    if not block:
        return style_content
    if not (style_content or "").strip():
        return block.strip()
    return f"{style_content.rstrip()}\n{block}"
