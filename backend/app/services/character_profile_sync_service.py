"""角色/组织档案同步服务（issue #174）。

档案此前「写一次就不再更新」：导入时生成，之后续写的章节不会回流。本服务把
新写出的章节里的信息回流到 `characters` 的档案字段，并且**不覆盖用户手改过的内容**：

- 水位：`profile_synced_through_chapter` 记录该档案已同步到的章节号，只处理
  `profile_synced_through_chapter < 章节号 <= through_chapter` 的新章节；
  没有新章节的角色不发起任何 AI 调用。
- 写回：逐字段比较「当前值」与 `ai_profile_snapshot` 里的旧值。两者相等说明
  字段没被手改过，允许覆盖并把新值写进快照；不相等说明用户改过，跳过该字段并
  计入 `fields_preserved`。快照缺失（首次同步）时允许覆盖。
- 幂等：同一 `through_chapter` 再跑一次是 no-op（水位已越过，零 AI 调用零写入）。
- 异常：`ai.content_filtered` 按「多章对半拆批 → 单章正文二分」降级重试（issue #193），
  降到最小粒度仍被拦才记入 `skipped`；`ai.output_truncated` 不重试、直接记 `skipped`；
  其余异常照常上抛。
- 水位只按连续覆盖推进：某章的切片全部成功才算 covered，水位停在该档案提及章节序列里
  「从起点连续成功的最长前缀」的最大章号，失败章节下一轮 `start = marker + 1` 会重新覆盖。
"""
from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Awaitable, Callable, Dict, Iterable, List, Optional, Tuple

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import ApiError
from app.logger import get_logger
from app.models.chapter import Chapter
from app.models.character import Character
from app.models.project import Project
from app.models.relationship import Organization, OrganizationMember
from app.services.import_validators import looks_like_profile_narration
from app.services.profile_prompt_rules import (
    FACTS_FIELD_BOUNDARIES,
    PROSE_FIELD_BOUNDARIES,
)
from app.services.profile_facts import (
    PROFILE_FACTS_PROMPT_ENABLED,
    merge_profile_facts,
    resolve_profile_fields,
)

logger = get_logger(__name__)

# 允许 AI 回流的档案字段
PROFILE_FIELDS = ("personality", "background", "appearance", "current_state")

# 允许 AI 回流的世界设定字段（title/theme/genre/description 不动）
WORLD_FIELDS = ("world_time_period", "world_location", "world_atmosphere", "world_rules")

# 不可重试的错误码：记入 skipped 后继续处理其他批次
NON_RETRYABLE_CODES = frozenset({"ai.content_filtered", "ai.output_truncated"})

# 只有内容审核拦截允许降级重试；`ai.output_truncated` 语义不变（不重试）
CONTENT_FILTERED_CODE = "ai.content_filtered"

# 单次 AI 调用最多覆盖的角色数，避免提示词里塞进整本项目
_CHARACTERS_PER_CALL = 5
# 单次 AI 调用的章节正文预算（字符）。超出的章节拆分到下一批，不截断正文。
_PROMPT_CHAR_BUDGET = 8000
# 单章正文二分后的最小切片长度（字符）：短于等于该值不再切，直接记 skipped。
_MIN_SLICE_CHARS = 600

_SYSTEM_PROMPT = (
    "你是小说设定档案维护助手。只依据给定章节正文更新档案，不得臆造正文中"
    "没有出现的信息。输出严格 JSON，不要额外解释。\n"
    "档案字段硬约束：\n"
    "1. personality 只写稳定的性格特质（形容词或短句），禁止复述情节、对话、"
    "他人的评价事件；\n"
    "2. appearance 只写稳定的外貌与常驻装束，禁止写「被…之后露出」「曾在…时」"
    "这类一次性事件或状态变化；\n"
    "3. current_state 才是承载「最近发生了什么/当前处境」的字段；\n"
    "4. 正文确实没有描写时宁可留空（\"\"），不要拿情节凑数。\n"
) + "\n" + PROSE_FIELD_BOUNDARIES

_PROMPT_TEMPLATE = """请根据下列章节正文，更新角色/组织档案。

【待更新档案】
{profiles}

【章节正文】
{chapters}

输出 JSON（顶层对象）：
{{
  "characters": [
    {{
      "name": "档案名称（必须与上面列出的名称完全一致）",
      "fields": {{
        "personality": "稳定的性格特质（形容词/短句），无新信息填 null",
        "background": "背景故事，无新信息填 null",
        "appearance": "稳定的外貌与常驻装束，无新信息填 null",
        "current_state": "当前处境与心理状态，无新信息填 null"
      }},
      "organizations": [
        {{"organization_name": "组织名称", "position": "职位", "status": "active"}}
      ]
    }}
  ]
}}

规则：
1. 只输出相对现有档案有实质变化的字段；没有变化或正文无依据的字段填 null。
2. 不要把现有档案原样复述一遍。
3. organizations 只列出本章新出现或发生变化的组织从属关系；没有则给空数组。
4. 未在上面列出名称的角色一律不要输出。
5. personality/appearance 只写稳定特质与常驻样貌；事件、对话、他人评价、状态变化
   写进 current_state 或 background，不要写进这两个字段。正文没有描写时宁可留空，
   不要用情节凑数。"""

# issue #190：facts 版模板。字段本体与引文规则必须与拆书导入的
# CHARACTERS_BATCH_GENERATION_FACTS 保持一致，否则两条写入路径会产出不同口径的档案。
_PROMPT_TEMPLATE_FACTS = """请根据下列章节正文，更新角色/组织档案。

【待更新档案】
{profiles}

【章节正文】
{chapters}

输出 JSON（顶层对象）：
{{
  "characters": [
    {{
      "name": "档案名称（必须与上面列出的名称完全一致）",
      "profile_facts": [
        {{
          "field": "personality",
          "claim": "性情沉稳",
          "quote": "他听完只是点了点头，没有说话",
          "chapter_number": 12,
          "inferred": false
        }}
      ],
      "organizations": [
        {{"organization_name": "组织名称", "position": "职位", "status": "active"}}
      ]
    }}
  ]
}}

""" + FACTS_FIELD_BOUNDARIES + """

规则：
1. 只输出相对现有档案有实质变化的事实；某角色没有新依据就返回空数组，不要复述现有档案。
2. quote 必须从【章节正文】里逐字复制，至少 8 个字；禁止改写、拼接、补字，禁止含省略号。
   正文找不到就只能不写。
3. chapter_number 必须是该引文实际所在的章号。
4. inferred 只有 personality 允许为 true（由行为推断性格，quote 仍必须真实）；其余字段必须 false。
5. 每条 claim 只表达一个特质或一个事实，不写成叙述句，不含句末标点。
6. 未在上面列出名称的角色一律不要输出。"""

_WORLD_SYSTEM_PROMPT = (
    "你是小说世界设定维护助手。只依据给定章节正文归纳世界设定，不得臆造正文中"
    "没有出现的信息。输出严格 JSON，不要额外解释。"
)

_WORLD_PROMPT_TEMPLATE = """请根据下列章节正文，归纳并更新这本书的世界设定。

【现有世界设定】
{profile}

【章节正文】
{chapters}

输出 JSON（顶层对象）：
{{
  "world": {{
    "world_time_period": "具体时代/时间线：写明年代、纪元或阶段，以及推动它的具体事件",
    "world_location": "具体地理：地名、区域、城邦、势力所在，必要时点出空间结构",
    "world_atmosphere": "基调：这本书特有的氛围与张力来源，结合具体场景与冲突",
    "world_rules": "规则约束：力量体系/技术/制度/禁忌等硬性规则，写清代价与限制"
  }}
}}

规则：
1. 四个字段都必须给出非空值，每段 40 字以上，要写这本书**具体的专名、势力、地点、事件、规则**。
2. 严禁类型标签式空话（如「古代架空时代」「一个充满冒险的世界」「氛围紧张」），
   宁可写正文里出现的具体名词。
3. 如果正文提供了比现有设定更具体的信息，就把它合并进来；不要退回到更笼统的表述。
4. 正文没有涉及的方面，保留现有设定里已有的内容，不要清空。"""


@dataclass(frozen=True)
class _ChapterSlice:
    """喂给模型的一段正文：整章，或单章正文二分后的一片。

    切片保留原章节的 `id` 与 `chapter_number`，水位仍按章节身份计算；`part/parts`
    只在二分后出现，渲染时标注「第 x/y 段」，避免模型把两片当成两章。
    """

    id: str
    chapter_number: int
    title: Optional[str]
    content: str
    part: Optional[int] = None
    parts: Optional[int] = None


def _as_slices(chapters: Iterable[Chapter]) -> List[_ChapterSlice]:
    return [
        _ChapterSlice(
            id=chapter.id,
            chapter_number=chapter.chapter_number,
            title=chapter.title,
            content=chapter.content or "",
        )
        for chapter in chapters
    ]


def _split_content(content: str) -> Optional[Tuple[str, str]]:
    """在段落边界把单章正文切成两片；正文不长于切片下限时返回 None（不再切）。"""
    if len(content) <= _MIN_SLICE_CHARS:
        return None
    mid = len(content) // 2
    newlines = [index for index, char in enumerate(content) if char == "\n"]
    if newlines:
        split_at = min(newlines, key=lambda index: abs(index - mid))
        left = content[:split_at].rstrip()
        right = content[split_at:].lstrip()
        if left and right:
            return left, right
    return content[:mid], content[mid:]


def _loads_json(raw: Optional[str], fallback: Any) -> Any:
    if not raw:
        return fallback
    try:
        return json.loads(raw)
    except (TypeError, ValueError):
        return fallback


def _character_names(character: Character) -> List[str]:
    """名称 + 别名（aliases 存 JSON 数组，也容忍逗号分隔）。"""
    names = [character.name or ""]
    aliases = _loads_json(character.aliases, [])
    if isinstance(aliases, str):
        aliases = [part.strip() for part in aliases.split(",")]
    if isinstance(aliases, list):
        names.extend(str(alias) for alias in aliases)
    return [name.strip() for name in names if name and len(name.strip()) >= 2]


def _mentioned_in(character: Character, content: str) -> bool:
    return any(name in content for name in _character_names(character))


def _batch_by_budget(chapters: List[Chapter]) -> List[List[Chapter]]:
    """按字符预算切分章节；单章超预算时独占一批（不截断正文）。"""
    batches: List[List[Chapter]] = []
    current: List[Chapter] = []
    current_size = 0
    for chapter in chapters:
        size = len(chapter.content or "")
        if current and current_size + size > _PROMPT_CHAR_BUDGET:
            batches.append(current)
            current, current_size = [], 0
        current.append(chapter)
        current_size += size
    if current:
        batches.append(current)
    return batches


def _render_profiles(characters: Iterable[Character]) -> str:
    lines: List[str] = []
    for character in characters:
        kind = "组织" if character.is_organization else "角色"
        lines.append(f"- 名称：{character.name}（{kind}）")
        aliases = [name for name in _character_names(character) if name != character.name]
        if aliases:
            lines.append(f"  别名：{'、'.join(aliases)}")
        for field in PROFILE_FIELDS:
            value = getattr(character, field, None)
            if value:
                lines.append(f"  {field}：{value}")
    return "\n".join(lines)


def _render_chapters(chapters: Iterable[_ChapterSlice]) -> str:
    blocks: List[str] = []
    for chapter in chapters:
        part = chapter.part or 0
        parts = chapter.parts or 0
        marker = f"（第 {part}/{parts} 段）" if parts > 1 and part else ""
        blocks.append(
            f"【第{chapter.chapter_number}章 {chapter.title or ''}{marker}】\n{chapter.content or ''}"
        )
    return "\n\n".join(blocks)


def _render_world_profile(project: Any) -> str:
    lines = [
        f"- {field}：{getattr(project, field)}"
        for field in WORLD_FIELDS
        if getattr(project, field, None)
    ]
    return "\n".join(lines) or "- （尚无设定，需要从正文归纳）"


def _apply_profile_values(
    target: Any,
    values: Dict[str, Any],
    snapshot: Dict[str, Any],
    fields: Iterable[str],
    stats: Dict[str, Any],
    *,
    label: str,
    force: bool = False,
) -> bool:
    """逐字段写回：默认保护历史值，只有确认是 AI 写入过的字段才允许覆盖。

    判定顺序：
    1. 模型返回空 → 跳过（不覆盖、不计数）；
    2. `force=True` → 整段覆盖并刷新快照；
    3. 有快照：当前值 == 快照值 → 覆盖；不相等 → 判定用户手改，保留并计 `fields_preserved`；
    4. 无快照（历史数据）：当前值为空 → 填入；非空 → 保留并计 `fields_preserved`
       （不写快照，将来 force 时仍能识别为旧值）。
    """
    changed = False
    for field in fields:
        new_value = values.get(field)
        if not isinstance(new_value, str) or not new_value.strip():
            continue
        new_value = new_value.strip()
        # issue #188：档案字段不得是剧情复述/原文抄录（例如把「因为她和丈夫不仅是夫妻，
        # 还是师兄妹……」写进性格特点）。这类内容一律不落库，宁可留空等下一轮。
        if looks_like_profile_narration(new_value):
            stats["fields_rejected"] = stats.get("fields_rejected", 0) + 1
            logger.info("丢弃疑似剧情复述的档案字段: %s.%s", label, field)
            continue
        current = getattr(target, field, None)

        if force:
            if current != new_value:
                setattr(target, field, new_value)
                stats["fields_updated"] += 1
                changed = True
            snapshot[field] = new_value
            continue

        if field not in snapshot:
            if current:
                # 历史数据：没有 AI 快照可对照，无法证明是 AI 写的 → 保护用户值
                stats["fields_preserved"] += 1
                logger.debug("保留历史字段（无快照）: %s.%s", label, field)
                continue
            setattr(target, field, new_value)
            snapshot[field] = new_value
            stats["fields_updated"] += 1
            changed = True
            continue

        previous_ai = snapshot.get(field)
        if current != previous_ai:
            # 用户手改过该字段：保留用户值，也不动快照（否则下次会误判为 AI 值）
            stats["fields_preserved"] += 1
            logger.debug("保留用户手改字段: %s.%s", label, field)
            continue
        if current != new_value:
            setattr(target, field, new_value)
            stats["fields_updated"] += 1
            changed = True
        snapshot[field] = new_value
    return changed


class CharacterProfileSyncService:
    """把新章节回流到角色/组织档案。"""

    # 同项目并发 sync 的防重入集合（键：project_id）
    _in_progress: set[str] = set()
    # 世界设定同步单独的防重入集合，避免与角色档案互相阻塞
    _world_in_progress: set[str] = set()

    async def sync(
        self,
        *,
        db: AsyncSession,
        user_id: str,
        project_id: str,
        through_chapter: Optional[int] = None,
        ai_service: Any = None,
        force: bool = False,
    ) -> Dict[str, Any]:
        """同步至 `through_chapter`（默认取项目内已写正文的最大章节号）。"""
        guard_key = project_id
        stats: Dict[str, Any] = {
            "characters_considered": 0,
            "characters_updated": 0,
            "fields_updated": 0,
            "fields_preserved": 0,
            "fields_rejected": 0,
            "skipped": [],
            "through_chapter": 0,
        }

        if guard_key in self._in_progress:
            logger.info("档案同步已在进行中，本次跳过: project=%s", project_id)
            stats["skipped"].append({"reason": "sync_in_progress", "project_id": project_id})
            return stats

        self._in_progress.add(guard_key)
        try:
            return await self._sync(
                db=db,
                user_id=user_id,
                project_id=project_id,
                through_chapter=through_chapter,
                ai_service=ai_service,
                stats=stats,
                force=force,
            )
        finally:
            self._in_progress.discard(guard_key)

    async def _sync(
        self,
        *,
        db: AsyncSession,
        user_id: str,
        project_id: str,
        through_chapter: Optional[int],
        ai_service: Any,
        stats: Dict[str, Any],
        force: bool = False,
    ) -> Dict[str, Any]:
        through = through_chapter
        if through is None:
            max_number = await db.scalar(
                select(func.max(Chapter.chapter_number)).where(
                    Chapter.project_id == project_id,
                    Chapter.content.isnot(None),
                    Chapter.content != "",
                )
            )
            through = int(max_number or 0)
        stats["through_chapter"] = int(through or 0)
        if not through:
            return stats

        chapters = list(
            (
                await db.execute(
                    select(Chapter)
                    .where(
                        Chapter.project_id == project_id,
                        Chapter.chapter_number <= through,
                        Chapter.content.isnot(None),
                        Chapter.content != "",
                    )
                    .order_by(Chapter.chapter_number)
                )
            ).scalars()
        )
        if not chapters:
            return stats

        characters = list(
            (await db.execute(select(Character).where(Character.project_id == project_id))).scalars()
        )

        # 只挑「有新章节且正文里确实出现过」的档案；匹配不上就跳过，不硬猜。
        candidates: List[tuple[Character, List[Chapter]]] = []
        for character in characters:
            start = int(character.profile_synced_through_chapter or 0) + 1
            if start > through:
                continue
            mentions = [
                chapter
                for chapter in chapters
                if chapter.chapter_number >= start and _mentioned_in(character, chapter.content or "")
            ]
            if mentions:
                candidates.append((character, mentions))

        stats["characters_considered"] = len(candidates)
        if not candidates:
            return stats

        service = ai_service or await self._resolve_ai_service(user_id, db)

        for offset in range(0, len(candidates), _CHARACTERS_PER_CALL):
            chunk = candidates[offset: offset + _CHARACTERS_PER_CALL]
            await self._sync_chunk(
                db=db, chunk=chunk, ai_service=service, stats=stats, force=force
            )

        await db.commit()
        stats["characters_updated"] = len(stats.pop("_updated_ids", ()))
        logger.info(
            "档案同步完成: project=%s through=%s 角色%s 更新%s 字段%s 保留%s 跳过%s",
            project_id,
            stats["through_chapter"],
            stats["characters_considered"],
            stats["characters_updated"],
            stats["fields_updated"],
            stats["fields_preserved"],
            len(stats["skipped"]),
        )
        return stats

    async def _sync_chunk(
        self,
        *,
        db: AsyncSession,
        chunk: List[tuple[Character, List[Chapter]]],
        ai_service: Any,
        stats: Dict[str, Any],
        force: bool = False,
    ) -> None:
        """处理一批角色：按正文预算分批调用模型，逐字段走写回策略。"""
        character_ids = {character.id for character, _ in chunk}
        names = {character.name for character, _ in chunk}

        mentioned: Dict[str, Chapter] = {}
        for _, mentions in chunk:
            for chapter in mentions:
                mentioned[chapter.id] = chapter
        ordered = sorted(mentioned.values(), key=lambda item: item.chapter_number)
        profiles = _render_profiles([character for character, _ in chunk])
        # issue #190：引文校验用**整章全文**（模型看到的是切片，校验可以放宽到整章：
        # 命中说明这句话确实在该章正文里，只是模型看到的部分比切片多）。
        chapters_by_number = {
            chapter.chapter_number: (chapter.content or "") for chapter in ordered
        }
        prompt_template = (
            _PROMPT_TEMPLATE_FACTS if PROFILE_FACTS_PROMPT_ENABLED else _PROMPT_TEMPLATE
        )

        # 章节 id → 该章所有切片是否都成功（issue #193）
        covered: Dict[str, bool] = {}

        async def _apply(response: Any) -> bool:
            entries = response.get("characters") if isinstance(response, dict) else None
            if not isinstance(entries, list):
                logger.warning("档案同步返回结构异常，跳过该批次")
                return False
            for entry in entries:
                if not isinstance(entry, dict):
                    continue
                character = self._match_character(entry.get("name"), chunk, names)
                if character is None or character.id not in character_ids:
                    continue
                await self._apply_entry(
                    db=db,
                    character=character,
                    entry=entry,
                    stats=stats,
                    chapters_by_number=chapters_by_number,
                    force=force,
                )
            return True

        def _on_skipped(pieces: List[_ChapterSlice], exc: ApiError) -> None:
            numbers = sorted({piece.chapter_number for piece in pieces})
            stats["skipped"].append(
                {"chapter_numbers": numbers, "code": exc.code, "characters": sorted(names)}
            )
            logger.warning("档案同步批次被跳过: code=%s chapters=%s", exc.code, numbers)

        for text_batch in _batch_by_budget(ordered):
            results = await self._call_with_bisect(
                pieces=_as_slices(text_batch),
                build_prompt=lambda pieces: prompt_template.format(
                    profiles=profiles, chapters=_render_chapters(pieces)
                ),
                system_prompt=_SYSTEM_PROMPT,
                ai_service=ai_service,
                apply_response=_apply,
                on_skipped=_on_skipped,
            )
            for chapter_id, ok in results.items():
                covered[chapter_id] = covered.get(chapter_id, True) and ok

        for character, mentions in chunk:
            # 水位只推进到「该角色提及章节序列里从起点连续成功的最长前缀」的最大章号，
            # 中间失败就停在它前面，下一轮 start = marker + 1 会重新覆盖失败章节。
            contiguous = 0
            for chapter in mentions:
                if not covered.get(chapter.id, False):
                    break
                contiguous = chapter.chapter_number
            if contiguous > int(character.profile_synced_through_chapter or 0):
                character.profile_synced_through_chapter = contiguous

    async def _call_with_bisect(
        self,
        *,
        pieces: List[_ChapterSlice],
        build_prompt: Callable[[List[_ChapterSlice]], str],
        system_prompt: str,
        ai_service: Any,
        apply_response: Callable[[Any], Awaitable[bool]],
        on_skipped: Callable[[List[_ChapterSlice], ApiError], None],
    ) -> Dict[str, bool]:
        """调用模型处理一段正文，返回 {章节 id: 该章是否全部切片都成功}。

        `ai.content_filtered` 时二分降级（issue #193）：多章对半拆批，单章按段落边界
        把正文切成两片；降到最小粒度仍被拦才记 `skipped` 并返回 False。
        `ai.output_truncated` 语义不变：不重试、记 `skipped`。
        """
        try:
            response = await ai_service.call_with_json_retry(
                prompt=build_prompt(pieces),
                system_prompt=system_prompt,
                max_retries=2,
                expected_type="object",
                auto_mcp=False,
            )
        except ApiError as exc:
            if exc.code not in NON_RETRYABLE_CODES:
                raise
            halves = self._bisect(pieces) if exc.code == CONTENT_FILTERED_CODE else None
            if halves is None:
                on_skipped(pieces, exc)
                return {piece.id: False for piece in pieces}

            logger.info(
                "档案同步命中内容审核，二分降级: chapters=%s -> %s / %s",
                sorted({piece.chapter_number for piece in pieces}),
                sorted({piece.chapter_number for piece in halves[0]}),
                sorted({piece.chapter_number for piece in halves[1]}),
            )
            results: Dict[str, bool] = {}
            for half in halves:
                sub = await self._call_with_bisect(
                    pieces=half,
                    build_prompt=build_prompt,
                    system_prompt=system_prompt,
                    ai_service=ai_service,
                    apply_response=apply_response,
                    on_skipped=on_skipped,
                )
                for chapter_id, ok in sub.items():
                    results[chapter_id] = results.get(chapter_id, True) and ok
            return results

        ok = await apply_response(response)
        return {piece.id: ok for piece in pieces}

    @staticmethod
    def _bisect(
        pieces: List[_ChapterSlice],
    ) -> Optional[Tuple[List[_ChapterSlice], List[_ChapterSlice]]]:
        """把一段正文对半拆开；已到最小粒度（单章且正文不超下限）返回 None。"""
        if not pieces:
            return None

        groups: List[List[_ChapterSlice]] = []
        for piece in pieces:
            if groups and groups[-1][0].id == piece.id:
                groups[-1].append(piece)
            else:
                groups.append([piece])
        if len(groups) > 1:
            mid = len(groups) // 2
            return (
                [piece for group in groups[:mid] for piece in group],
                [piece for group in groups[mid:] for piece in group],
            )

        piece = pieces[0]
        if piece.part is not None:
            return None
        split = _split_content(piece.content)
        if split is None:
            return None
        left_text, right_text = split
        return (
            [
                _ChapterSlice(
                    id=piece.id,
                    chapter_number=piece.chapter_number,
                    title=piece.title,
                    content=left_text,
                    part=1,
                    parts=2,
                )
            ],
            [
                _ChapterSlice(
                    id=piece.id,
                    chapter_number=piece.chapter_number,
                    title=piece.title,
                    content=right_text,
                    part=2,
                    parts=2,
                )
            ],
        )

    @staticmethod
    def _match_character(
        name: Any,
        chunk: List[tuple[Character, List[Chapter]]],
        names: set[str],
    ) -> Optional[Character]:
        if not isinstance(name, str):
            return None
        for character, _ in chunk:
            if character.name == name:
                return character
        # 模型偶尔回别名：只在批次内唯一命中时接受
        for character, _ in chunk:
            if name in _character_names(character):
                return character
        logger.debug("档案同步返回了批次外名称，忽略: %s (批次 %s)", name, sorted(names))
        return None

    async def _apply_entry(
        self,
        *,
        db: AsyncSession,
        character: Character,
        entry: Dict[str, Any],
        stats: Dict[str, Any],
        chapters_by_number: Optional[Dict[int, str]] = None,
        force: bool = False,
    ) -> None:
        snapshot = _loads_json(character.ai_profile_snapshot, {})
        if not isinstance(snapshot, dict):
            snapshot = {}

        fields = entry.get("fields")
        values: Dict[str, Any] = fields if isinstance(fields, dict) else {}

        # issue #190：返回里有 profile_facts 时，档案文本只能由通过引文校验的事实渲染。
        # 一条事实都没通过时 values 为空 dict——宁可这轮不更新，也不落无依据文本。
        if PROFILE_FACTS_PROMPT_ENABLED and chapters_by_number:
            rendered, facts_stats, facts = resolve_profile_fields(entry, chapters_by_number)
            if facts_stats.get("payloads_facts"):
                values = rendered
                bucket = stats.setdefault("profile_facts", {})
                for key, value in facts_stats.items():
                    bucket[key] = bucket.get(key, 0) + value
                if facts:
                    character.profile_facts = merge_profile_facts(character.profile_facts, facts)
                    character.profile_facts_updated_at = datetime.utcnow()

        changed = _apply_profile_values(
            character,
            values,
            snapshot,
            PROFILE_FIELDS,
            stats,
            label=character.name,
            force=force,
        )

        org_changed = await self._apply_organizations(
            db=db, character=character, organizations=entry.get("organizations")
        )
        if changed or org_changed:
            # 同一角色可能出现在多个章节批次里，按人去重后再计入统计（issue #191）。
            stats.setdefault("_updated_ids", set()).add(character.id)

        if snapshot:
            character.ai_profile_snapshot = json.dumps(snapshot, ensure_ascii=False)

    async def _apply_organizations(
        self,
        *,
        db: AsyncSession,
        character: Character,
        organizations: Any,
    ) -> bool:
        """新增/变更组织从属关系。source='manual' 的成员关系视为用户维护，不覆盖。"""
        if not isinstance(organizations, list) or not organizations:
            return False

        changed = False
        for item in organizations:
            if not isinstance(item, dict):
                continue
            org_name = item.get("organization_name")
            if not isinstance(org_name, str) or not org_name.strip():
                continue

            org_character = (
                await db.execute(
                    select(Character).where(
                        Character.project_id == character.project_id,
                        Character.name == org_name.strip(),
                        Character.is_organization.is_(True),
                    )
                )
            ).scalars().first()
            if org_character is None:
                continue
            organization = (
                await db.execute(
                    select(Organization).where(Organization.character_id == org_character.id)
                )
            ).scalars().first()
            if organization is None:
                continue

            position = (item.get("position") or "").strip() or None
            status = (item.get("status") or "").strip() or None
            membership = (
                await db.execute(
                    select(OrganizationMember).where(
                        OrganizationMember.organization_id == organization.id,
                        OrganizationMember.character_id == character.id,
                    )
                )
            ).scalars().first()

            if membership is None:
                if not position:
                    continue
                db.add(
                    OrganizationMember(
                        organization_id=organization.id,
                        character_id=character.id,
                        position=position,
                        status=status or "active",
                        source="ai",
                    )
                )
                changed = True
                continue

            if (membership.source or "ai") == "manual":
                continue
            if position and membership.position != position:
                membership.position = position
                changed = True
            if status and membership.status != status:
                membership.status = status
                changed = True

        return changed

    @staticmethod
    async def _resolve_ai_service(user_id: str, db: AsyncSession) -> Any:
        from app.api.settings import get_user_ai_service_from_db

        return await get_user_ai_service_from_db(user_id, db)

    async def sync_world(
        self,
        *,
        db: AsyncSession,
        user_id: str,
        project_id: str,
        through_chapter: Optional[int] = None,
        ai_service: Any = None,
        force: bool = False,
    ) -> Dict[str, Any]:
        """把新写出的章节回流到项目世界设定（issue #175）。"""
        stats: Dict[str, Any] = {
            "updated": False,
            "fields_updated": 0,
            "fields_preserved": 0,
            "fields_rejected": 0,
            "skipped": [],
            "through_chapter": 0,
        }

        if project_id in self._world_in_progress:
            logger.info("世界设定同步已在进行中，本次跳过: project=%s", project_id)
            stats["skipped"].append({"reason": "sync_in_progress", "project_id": project_id})
            return stats

        self._world_in_progress.add(project_id)
        try:
            return await self._sync_world(
                db=db,
                user_id=user_id,
                project_id=project_id,
                through_chapter=through_chapter,
                ai_service=ai_service,
                stats=stats,
                force=force,
            )
        finally:
            self._world_in_progress.discard(project_id)

    async def sync_all(
        self,
        *,
        db: AsyncSession,
        user_id: str,
        project_id: str,
        through_chapter: Optional[int] = None,
        ai_service: Any = None,
        include_world: bool = True,
        force: bool = False,
    ) -> Dict[str, Any]:
        """手动入口：世界设定 + 角色档案。世界设定失败不影响角色档案结果。"""
        world: Optional[Dict[str, Any]] = None
        if include_world:
            try:
                world = await self.sync_world(
                    db=db,
                    user_id=user_id,
                    project_id=project_id,
                    through_chapter=through_chapter,
                    ai_service=ai_service,
                    force=force,
                )
            except Exception as exc:  # noqa: BLE001 - 世界设定失败不得影响角色档案
                logger.warning("世界设定同步失败（角色档案继续）: %s", exc, exc_info=True)
                world = {
                    "updated": False,
                    "fields_updated": 0,
                    "fields_preserved": 0,
                    "skipped": [{"reason": "world_sync_failed", "error": str(exc)[:200]}],
                    "through_chapter": int(through_chapter or 0),
                }

        stats = await self.sync(
            db=db,
            user_id=user_id,
            project_id=project_id,
            through_chapter=through_chapter,
            ai_service=ai_service,
            force=force,
        )
        stats["world"] = world
        return stats

    async def _sync_world(
        self,
        *,
        db: AsyncSession,
        user_id: str,
        project_id: str,
        through_chapter: Optional[int],
        ai_service: Any,
        stats: Dict[str, Any],
        force: bool = False,
    ) -> Dict[str, Any]:
        project = (
            await db.execute(select(Project).where(Project.id == project_id))
        ).scalars().first()
        if project is None:
            return stats

        through = through_chapter
        if through is None:
            max_number = await db.scalar(
                select(func.max(Chapter.chapter_number)).where(
                    Chapter.project_id == project_id,
                    Chapter.content.isnot(None),
                    Chapter.content != "",
                )
            )
            through = int(max_number or 0)
        stats["through_chapter"] = int(through or 0)

        start = int(project.world_profile_synced_through_chapter or 0) + 1
        if not through or start > through:
            return stats

        chapters = list(
            (
                await db.execute(
                    select(Chapter)
                    .where(
                        Chapter.project_id == project_id,
                        Chapter.chapter_number >= start,
                        Chapter.chapter_number <= through,
                        Chapter.content.isnot(None),
                        Chapter.content != "",
                    )
                    .order_by(Chapter.chapter_number)
                )
            ).scalars()
        )
        if not chapters:
            return stats

        service = ai_service or await self._resolve_ai_service(user_id, db)
        snapshot = _loads_json(project.ai_world_profile_snapshot, {})
        if not isinstance(snapshot, dict):
            snapshot = {}

        # 章节 id → 该章所有切片是否都成功（issue #193）
        covered: Dict[str, bool] = {}

        async def _apply(response: Any) -> bool:
            payload = response.get("world") if isinstance(response, dict) else None
            if not isinstance(payload, dict):
                logger.warning("世界设定返回结构异常，跳过该批次")
                return False
            _apply_profile_values(
                project,
                payload,
                snapshot,
                WORLD_FIELDS,
                stats,
                label="世界设定",
                force=force,
            )
            project.ai_world_profile_snapshot = json.dumps(snapshot, ensure_ascii=False)
            return True

        def _on_skipped(pieces: List[_ChapterSlice], exc: ApiError) -> None:
            numbers = sorted({piece.chapter_number for piece in pieces})
            stats["skipped"].append({"chapter_numbers": numbers, "code": exc.code})
            logger.warning("世界设定同步批次被跳过: code=%s chapters=%s", exc.code, numbers)

        for text_batch in _batch_by_budget(chapters):
            results = await self._call_with_bisect(
                pieces=_as_slices(text_batch),
                build_prompt=lambda pieces: _WORLD_PROMPT_TEMPLATE.format(
                    profile=_render_world_profile(project),
                    chapters=_render_chapters(pieces),
                ),
                system_prompt=_WORLD_SYSTEM_PROMPT,
                ai_service=service,
                apply_response=_apply,
                on_skipped=_on_skipped,
            )
            for chapter_id, ok in results.items():
                covered[chapter_id] = covered.get(chapter_id, True) and ok

        # 世界设定按章节顺序全量喂：水位只推进到连续成功的最长前缀。
        contiguous = 0
        for chapter in chapters:
            if not covered.get(chapter.id, False):
                break
            contiguous = chapter.chapter_number
        if contiguous > int(project.world_profile_synced_through_chapter or 0):
            project.world_profile_synced_through_chapter = contiguous

        stats["updated"] = stats["fields_updated"] > 0
        await db.commit()
        logger.info(
            "世界设定同步完成: project=%s through=%s 更新%s 保留%s 跳过%s",
            project_id,
            stats["through_chapter"],
            stats["fields_updated"],
            stats["fields_preserved"],
            len(stats["skipped"]),
        )
        return stats

    @classmethod
    async def sync_after_chapter(
        cls, *, user_id: str, project_id: str, chapter_id: str
    ) -> Optional[Dict[str, Any]]:
        """章节写完后按该章节号刷新档案（独立会话，失败只记日志）。"""
        from app.database import get_engine
        from sqlalchemy.ext.asyncio import async_sessionmaker

        try:
            engine = await get_engine(user_id)
            session_factory = async_sessionmaker(engine, expire_on_commit=False)
            async with session_factory() as db:
                chapter_number = await db.scalar(
                    select(Chapter.chapter_number).where(Chapter.id == chapter_id)
                )
        except Exception as exc:  # noqa: BLE001 - 后台收尾，失败不得影响主任务
            logger.warning("读取章节号失败，跳过档案刷新: %s", exc)
            return None
        if not chapter_number:
            return None
        return await cls.sync_standalone(
            user_id=user_id,
            project_id=project_id,
            through_chapter=int(chapter_number),
        )

    @classmethod
    async def sync_standalone(
        cls,
        *,
        user_id: str,
        project_id: str,
        through_chapter: Optional[int] = None,
    ) -> Optional[Dict[str, Any]]:
        """后台收尾用入口：独立会话 + 独立 AI 服务，任何异常只记日志。

        调用方（章节生成/导入收尾）不应因档案同步失败改变任务终态。
        """
        from app.database import get_engine
        from sqlalchemy.ext.asyncio import async_sessionmaker

        try:
            engine = await get_engine(user_id)
            session_factory = async_sessionmaker(engine, expire_on_commit=False)
            async with session_factory() as db:
                ai_service = await cls._resolve_ai_service(user_id, db)
                return await character_profile_sync_service.sync_all(
                    db=db,
                    user_id=user_id,
                    project_id=project_id,
                    through_chapter=through_chapter,
                    ai_service=ai_service,
                )
        except Exception as exc:  # noqa: BLE001 - 后台收尾，失败不得影响主任务
            logger.warning("刷新角色档案失败（已忽略）: %s", exc, exc_info=True)
            return None


character_profile_sync_service = CharacterProfileSyncService()
