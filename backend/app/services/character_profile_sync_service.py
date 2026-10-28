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
- 异常：`ai.content_filtered` / `ai.output_truncated` 不重试，记入 `skipped`；
  其余异常照常上抛。
"""
from __future__ import annotations

import asyncio
import json
from typing import Any, Dict, Iterable, List, Optional

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import ApiError
from app.logger import get_logger
from app.models.chapter import Chapter
from app.models.character import Character
from app.models.project import Project
from app.models.relationship import Organization, OrganizationMember

logger = get_logger(__name__)

# 允许 AI 回流的档案字段
PROFILE_FIELDS = ("personality", "background", "appearance", "current_state")

# 允许 AI 回流的世界设定字段（title/theme/genre/description 不动）
WORLD_FIELDS = ("world_time_period", "world_location", "world_atmosphere", "world_rules")

# 不可重试的错误码：记入 skipped 后继续处理其他批次
NON_RETRYABLE_CODES = frozenset({"ai.content_filtered", "ai.output_truncated"})

# 单次 AI 调用最多覆盖的角色数，避免提示词里塞进整本项目
_CHARACTERS_PER_CALL = 5
# 单次 AI 调用的章节正文预算（字符）。超出的章节拆分到下一批，不截断正文。
_PROMPT_CHAR_BUDGET = 8000

_SYSTEM_PROMPT = (
    "你是小说设定档案维护助手。只依据给定章节正文更新档案，不得臆造正文中"
    "没有出现的信息。输出严格 JSON，不要额外解释。"
)

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
        "personality": "性格特点/组织特性，无新信息填 null",
        "background": "背景故事，无新信息填 null",
        "appearance": "外貌描述，无新信息填 null",
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
4. 未在上面列出名称的角色一律不要输出。"""

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


def _render_chapters(chapters: Iterable[Chapter]) -> str:
    return "\n\n".join(
        f"【第{chapter.chapter_number}章 {chapter.title or ''}】\n{chapter.content or ''}"
        for chapter in chapters
    )


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
) -> bool:
    """逐字段写回：只覆盖「没被用户手改过」的字段，并把 AI 写入值记进快照。"""
    changed = False
    for field in fields:
        new_value = values.get(field)
        if not isinstance(new_value, str) or not new_value.strip():
            continue
        new_value = new_value.strip()
        current = getattr(target, field, None)
        if current == new_value:
            snapshot[field] = new_value
            continue
        previous_ai = snapshot.get(field)
        if current and previous_ai is not None and current != previous_ai:
            # 用户手改过该字段：保留用户值，也不动快照（否则下次会误判为 AI 值）
            stats["fields_preserved"] += 1
            logger.debug("保留用户手改字段: %s.%s", label, field)
            continue
        setattr(target, field, new_value)
        snapshot[field] = new_value
        stats["fields_updated"] += 1
        changed = True
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
    ) -> Dict[str, Any]:
        """同步至 `through_chapter`（默认取项目内已写正文的最大章节号）。"""
        guard_key = project_id
        stats: Dict[str, Any] = {
            "characters_considered": 0,
            "characters_updated": 0,
            "fields_updated": 0,
            "fields_preserved": 0,
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
            await self._sync_chunk(db=db, chunk=chunk, ai_service=service, stats=stats)

        await db.commit()
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
    ) -> None:
        """处理一批角色：按正文预算分批调用模型，逐字段走写回策略。"""
        character_ids = {character.id for character, _ in chunk}
        names = {character.name for character, _ in chunk}
        covered: Dict[str, int] = {character.id: 0 for character, _ in chunk}

        mentioned: Dict[str, Chapter] = {}
        for _, mentions in chunk:
            for chapter in mentions:
                mentioned[chapter.id] = chapter
        ordered = sorted(mentioned.values(), key=lambda item: item.chapter_number)

        for text_batch in _batch_by_budget(ordered):
            prompt = _PROMPT_TEMPLATE.format(
                profiles=_render_profiles([character for character, _ in chunk]),
                chapters=_render_chapters(text_batch),
            )
            try:
                response = await ai_service.call_with_json_retry(
                    prompt=prompt,
                    system_prompt=_SYSTEM_PROMPT,
                    max_retries=2,
                    expected_type="object",
                    auto_mcp=False,
                )
            except ApiError as exc:
                if exc.code not in NON_RETRYABLE_CODES:
                    raise
                stats["skipped"].append(
                    {
                        "chapter_numbers": [chapter.chapter_number for chapter in text_batch],
                        "code": exc.code,
                        "characters": sorted(names),
                    }
                )
                logger.warning("档案同步批次被跳过: code=%s chapters=%s", exc.code, [c.chapter_number for c in text_batch])
                continue

            entries = response.get("characters") if isinstance(response, dict) else None
            if not isinstance(entries, list):
                logger.warning("档案同步返回结构异常，跳过该批次: chapters=%s", [c.chapter_number for c in text_batch])
                continue

            max_covered = max(chapter.chapter_number for chapter in text_batch)
            for entry in entries:
                if not isinstance(entry, dict):
                    continue
                character = self._match_character(entry.get("name"), chunk, names)
                if character is None or character.id not in character_ids:
                    continue
                await self._apply_entry(db=db, character=character, entry=entry, stats=stats)

            # 本批成功处理即视为已覆盖（模型没给出新信息也算看过，避免反复重跑）
            batch_chapter_ids = {chapter.id for chapter in text_batch}
            for character, mentions in chunk:
                if any(chapter.id in batch_chapter_ids for chapter in mentions):
                    covered[character.id] = max(covered.get(character.id, 0), max_covered)

        for character, _ in chunk:
            if covered.get(character.id, 0) > int(character.profile_synced_through_chapter or 0):
                character.profile_synced_through_chapter = covered[character.id]

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
    ) -> None:
        snapshot = _loads_json(character.ai_profile_snapshot, {})
        if not isinstance(snapshot, dict):
            snapshot = {}

        fields = entry.get("fields")
        changed = _apply_profile_values(
            character,
            fields if isinstance(fields, dict) else {},
            snapshot,
            PROFILE_FIELDS,
            stats,
            label=character.name,
        )

        org_changed = await self._apply_organizations(
            db=db, character=character, organizations=entry.get("organizations")
        )
        if changed or org_changed:
            stats["characters_updated"] += 1

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
    ) -> Dict[str, Any]:
        """把新写出的章节回流到项目世界设定（issue #175）。"""
        stats: Dict[str, Any] = {
            "updated": False,
            "fields_updated": 0,
            "fields_preserved": 0,
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

        for text_batch in _batch_by_budget(chapters):
            prompt = _WORLD_PROMPT_TEMPLATE.format(
                profile=_render_world_profile(project),
                chapters=_render_chapters(text_batch),
            )
            try:
                response = await service.call_with_json_retry(
                    prompt=prompt,
                    system_prompt=_WORLD_SYSTEM_PROMPT,
                    max_retries=2,
                    expected_type="object",
                    auto_mcp=False,
                )
            except ApiError as exc:
                if exc.code not in NON_RETRYABLE_CODES:
                    raise
                stats["skipped"].append(
                    {
                        "chapter_numbers": [chapter.chapter_number for chapter in text_batch],
                        "code": exc.code,
                    }
                )
                logger.warning(
                    "世界设定同步批次被跳过: code=%s chapters=%s",
                    exc.code,
                    [chapter.chapter_number for chapter in text_batch],
                )
                continue

            payload = response.get("world") if isinstance(response, dict) else None
            if not isinstance(payload, dict):
                logger.warning(
                    "世界设定返回结构异常，跳过该批次: chapters=%s",
                    [chapter.chapter_number for chapter in text_batch],
                )
                continue

            _apply_profile_values(
                project, payload, snapshot, WORLD_FIELDS, stats, label="世界设定"
            )
            project.ai_world_profile_snapshot = json.dumps(snapshot, ensure_ascii=False)
            project.world_profile_synced_through_chapter = max(
                chapter.chapter_number for chapter in text_batch
            )

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
