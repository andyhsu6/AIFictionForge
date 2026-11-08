"""批量补全大纲结构化元数据。

大纲的 `structure` 列（Text JSON）承载 scenes/characters/key_points/emotion/goal
五个结构化字段。AI 生成或手工导入后常出现空值、空列表或"未设定/未知/待定"这类
占位值，导致下游（章节生成、上下文注入、agent 工具）拿不到可用信息。本服务按批
（BATCH_SIZE 条/次）向 AI 请求**仅缺失字段**，合并写回，已有非空值一律不覆盖。

设计约束：
- 只补缺失，不重写已有内容（避免二次生成把用户手工修改冲掉）；
- 单批失败只累加 failed 计数，不中断整个任务；
- 不新增数据库列：写回仍是 `structure` 的 JSON 文本。
"""
import json
from typing import Any, Awaitable, Callable, Dict, List, Optional, Sequence, Tuple

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import ApiError
from app.logger import get_logger
from app.models.character import Character
from app.models.outline import Outline
from app.models.project import Project
from app.services.language_resolver import (
    append_language_instruction,
    resolve_user_generation_language,
)

logger = get_logger(__name__)

# 视为"未填写"的占位值（比较前统一 strip + casefold）
PLACEHOLDER_VALUES = {"", "未设定", "未知", "待定", "无", "none", "n/a"}

# 检查顺序固定，返回值顺序随之固定（测试与调用方都依赖）
METADATA_FIELDS: Tuple[str, ...] = ("scenes", "characters", "key_points", "emotion", "goal")

BATCH_SIZE = 8
MAX_EXISTING_CHARACTERS = 30

_ORG_KEYWORDS = (
    "宗", "门", "派", "阁", "会", "帮", "盟", "国", "军", "司", "院", "族", "府",
    "殿", "教", "堂", "队", "集团", "组织", "势力",
)

ProgressCallback = Callable[[str, int, int], Awaitable[None]]


def _is_placeholder_text(value: Any) -> bool:
    """字符串是否为占位值（None 也算）。"""
    if value is None:
        return True
    if not isinstance(value, str):
        return False
    return value.strip().casefold() in PLACEHOLDER_VALUES


def _is_missing_value(value: Any) -> bool:
    """单个字段值是否等同于"未填写"：None/空串/占位值/空列表/仅占位值的列表。"""
    if value is None:
        return True
    if isinstance(value, str):
        return _is_placeholder_text(value)
    if isinstance(value, (list, tuple, set)):
        for item in value:
            if isinstance(item, dict):
                if not _is_placeholder_text(item.get("name")):
                    return False
            elif not _is_placeholder_text(item if isinstance(item, str) else str(item)):
                return False
        return True
    if isinstance(value, dict):
        return not value
    return not value


def _character_name(entry: Any) -> str:
    """取角色条目名称：dict 取 name，字符串条目自身即名称。"""
    if isinstance(entry, dict):
        raw = entry.get("name")
    else:
        raw = entry
    return str(raw or "").strip()


def _has_named_character(value: Any) -> bool:
    """characters 是否含至少一个有名有姓的条目（空 name 一律过滤）。"""
    if not isinstance(value, (list, tuple)):
        return False
    for entry in value:
        name = _character_name(entry)
        if name and not _is_placeholder_text(name):
            return True
    return False


def missing_metadata_fields(structure: dict) -> list[str]:
    """返回缺失的结构化字段名（固定顺序 scenes→characters→key_points→emotion→goal）。

    `key_points` 与 `key_events` 是同一份内容的两套键名：只要其中任一有非占位值
    就算已填写，否则只写了 `key_events` 的 structure 会被判缺失、补全任务重复生成。
    """
    if not isinstance(structure, dict):
        return list(METADATA_FIELDS)

    missing: List[str] = []
    for field in METADATA_FIELDS:
        value = structure.get(field)
        if field == "characters":
            if not _has_named_character(value):
                missing.append(field)
        elif field == "key_points":
            if _is_missing_value(value) and _is_missing_value(structure.get("key_events")):
                missing.append(field)
        elif _is_missing_value(value):
            missing.append(field)
    return missing


def _normalize_text_list(value: Any) -> List[str]:
    """字符串列表规范化：去空、去占位值、去重（保序）。"""
    if isinstance(value, str):
        candidates: Sequence[Any] = [value]
    elif isinstance(value, (list, tuple)):
        candidates = value
    else:
        return []

    normalized: List[str] = []
    seen = set()
    for item in candidates:
        text = str(item).strip() if not isinstance(item, dict) else ""
        if not text or _is_placeholder_text(text):
            continue
        key = text.casefold()
        if key in seen:
            continue
        seen.add(key)
        normalized.append(text)
    return normalized


def _normalize_characters(value: Any) -> List[Dict[str, str]]:
    """characters 规范化：type 仅允许 character/organization，按名称去重。"""
    if not isinstance(value, list):
        return []

    normalized: List[Dict[str, str]] = []
    seen = set()
    for entry in value:
        name = _character_name(entry)
        if not name or _is_placeholder_text(name):
            continue

        if isinstance(entry, dict):
            item_type = str(entry.get("type") or "character").strip().lower()
        else:
            item_type = "organization" if any(k in name for k in _ORG_KEYWORDS) else "character"
        if item_type not in ("character", "organization"):
            item_type = "character"

        key = name.casefold()
        if key in seen:
            continue
        seen.add(key)
        normalized.append({"name": name, "type": item_type})
    return normalized


def _normalize_scalar(value: Any) -> str:
    """单值字段规范化：占位值归空串。"""
    if value is None:
        return ""
    text = str(value).strip()
    return "" if _is_placeholder_text(text) else text


def normalize_structure_metadata(raw: dict) -> dict:
    """规范化 AI 返回的元数据字段，输出恒含五个键（缺/无效值为空串或空列表）。"""
    if not isinstance(raw, dict):
        raw = {}

    key_points_source = raw.get("key_points")
    if key_points_source is None:
        key_points_source = raw.get("key_events")

    return {
        "scenes": _normalize_text_list(raw.get("scenes")),
        "characters": _normalize_characters(raw.get("characters")),
        "key_points": _normalize_text_list(key_points_source),
        "emotion": _normalize_scalar(raw.get("emotion") if raw.get("emotion") is not None else raw.get("emotional_tone")),
        "goal": _normalize_scalar(raw.get("goal") if raw.get("goal") is not None else raw.get("narrative_goal")),
    }


def _load_structure(raw_structure: Any) -> dict:
    """把 Outline.structure 解析成 dict；损坏/非对象内容按空结构处理。"""
    if isinstance(raw_structure, dict):
        return dict(raw_structure)
    if not isinstance(raw_structure, str) or not raw_structure.strip():
        return {}
    try:
        parsed = json.loads(raw_structure)
    except (json.JSONDecodeError, TypeError, ValueError):
        logger.warning("大纲 structure JSON 解析失败，按空结构补全")
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _build_characters_info(characters: Sequence[Character]) -> str:
    """项目已有角色/组织清单，供 AI 对齐人名。"""
    if not characters:
        return "暂无已有角色"

    lines = []
    for char in characters:
        kind = "组织" if getattr(char, "is_organization", False) else "角色"
        lines.append(f"- {char.name}（{kind}）")
    return "\n".join(lines)


def _build_batch_prompt(
    project: Project,
    characters: Sequence[Character],
    batch: Sequence[Tuple[Outline, dict]],
    language: str,
) -> str:
    """构造单批补全提示词：只要求返回缺失字段的 JSON 数组。"""
    chapter_lines = []
    for outline, structure in batch:
        missing = missing_metadata_fields(structure)
        overview = (outline.content or "").strip()
        if len(overview) > 300:
            overview = overview[:300] + "…"
        chapter_lines.append(
            f"- order_index={outline.order_index}《{outline.title}》缺失字段：{'、'.join(missing)}\n"
            f"  章节概要：{overview or '（无）'}"
        )

    prompt = f"""你在为一部小说批量补全章节大纲的结构化元数据。

【项目信息】
书名：{project.title}
主题：{project.theme or '未提供'}
类型：{project.genre or '未提供'}
时间背景：{project.world_time_period or '未提供'}
地理位置：{project.world_location or '未提供'}
氛围基调：{project.world_atmosphere or '未提供'}
世界规则：{project.world_rules or '未提供'}

【已有角色与组织】
{_build_characters_info(characters)}

【字段定义】
- key_points：本章必须发生的关键事件，可验证（谁做了什么、导致什么结果）
- scenes：具体地点与场景功能（该场景承担什么叙事作用）
- characters：出场角色与组织；type 为 "character" 表示个人，"organization" 表示组织/势力/门派/帮派等，必须区分，不要把组织当作角色
- emotion：本章情绪起点→终点（如"戒备→信任"）
- goal：本章结束时主角/读者状态发生的变化

【待补全章节】
{chr(10).join(chapter_lines)}

【输出格式】
只返回一个 JSON 数组，每项形如：
[{{"order_index": 1, "scenes": ["场景1"], "characters": [{{"name": "角色名", "type": "character"}}], "key_points": ["关键事件1"], "emotion": "起点→终点", "goal": "状态变化"}}]
每项只包含该章缺失的字段；已有非空值的字段不要返回，更不要改写。
characters 中角色名优先使用【已有角色与组织】里的现成名称。

【禁止】
禁止使用"未设定""未知""待定"等占位值填充任何字段；无法确定某字段时，省略该字段。"""

    return append_language_instruction(prompt, language)


def _index_response_items(raw: Any) -> Dict[int, dict]:
    """把 AI 返回（数组，或包着数组的对象）按 order_index 建索引。"""
    items = raw
    if isinstance(raw, dict):
        items = next((value for value in raw.values() if isinstance(value, list)), None)
    if not isinstance(items, list):
        return {}

    indexed: Dict[int, dict] = {}
    for item in items:
        if not isinstance(item, dict):
            continue
        try:
            order_index = int(item.get("order_index"))
        except (TypeError, ValueError):
            continue
        indexed[order_index] = item
    return indexed


def _merge_metadata(structure: dict, item: dict) -> bool:
    """把 AI 返回项合并进 structure：只写仍缺失的键，返回是否有实际写入。"""
    normalized = normalize_structure_metadata(item)
    updated = False
    for field in missing_metadata_fields(structure):
        value = normalized.get(field)
        if _is_missing_value(value):
            continue
        structure[field] = value
        updated = True
    return updated


async def fill_missing_metadata(
    db: AsyncSession,
    project_id: str,
    outline_ids: Optional[Sequence[str]],
    ai_service: Any,
    *,
    provider: Optional[str],
    model: Optional[str],
    content_language: Any,
    progress: Optional[ProgressCallback] = None,
) -> dict:
    """批量补全项目大纲的缺失元数据。

    Args:
        db: 数据库会话（每批 commit）。
        project_id: 项目 ID，用于定位大纲、项目上下文与已有角色。
        outline_ids: 指定大纲 ID；为空时处理该项目全部大纲。
        ai_service: 需提供 `call_with_json_retry`。
        provider/model: 透传给 AI 服务。
        content_language: 生成语言覆盖值（"auto"/"zh"/"en"/None）。
        progress: 可选回调 `async (message, current, total)`。

    Returns:
        {"total": n, "filled": n, "skipped": n, "failed": n}
        total = skipped（已完整）+ filled（补全成功）+ failed（未补全/批次失败）。
    """
    project_result = await db.execute(select(Project).where(Project.id == project_id))
    project = project_result.scalar_one_or_none()
    if not project:
        raise ApiError(code="not_found.project")

    query = select(Outline).where(Outline.project_id == project_id)
    if outline_ids:
        query = query.where(Outline.id.in_(list(outline_ids)))
    query = query.order_by(Outline.order_index)
    outline_result = await db.execute(query)
    outlines = list(outline_result.scalars().all())

    total = len(outlines)
    targets: List[Tuple[Outline, dict]] = []
    skipped = 0
    for outline in outlines:
        structure = _load_structure(outline.structure)
        if missing_metadata_fields(structure):
            targets.append((outline, structure))
        else:
            skipped += 1

    filled = 0
    failed = 0
    if not targets:
        return {"total": total, "filled": filled, "skipped": skipped, "failed": failed}

    language = await resolve_user_generation_language(
        db, getattr(project, "user_id", None), content_language
    )

    characters_result = await db.execute(
        select(Character)
        .where(Character.project_id == project_id)
        .order_by(Character.name)
        .limit(MAX_EXISTING_CHARACTERS)
    )
    existing_characters = list(characters_result.scalars().all())

    for batch_start in range(0, len(targets), BATCH_SIZE):
        batch = targets[batch_start:batch_start + BATCH_SIZE]
        prompt = _build_batch_prompt(project, existing_characters, batch, language)

        try:
            raw_response = await ai_service.call_with_json_retry(
                prompt=prompt,
                max_retries=2,
                provider=provider,
                model=model,
                expected_type="array",
            )
        except Exception as exc:  # 单批失败不打断整个任务
            logger.warning(f"大纲元数据补全批次失败（{len(batch)} 条）: {exc}")
            raw_response = None

        if raw_response is None:
            failed += len(batch)
        else:
            indexed = _index_response_items(raw_response)
            batch_filled = 0
            for outline, structure in batch:
                item = indexed.get(outline.order_index) if outline.order_index is not None else None
                if item is not None and _merge_metadata(structure, item):
                    outline.structure = json.dumps(structure, ensure_ascii=False)
                    batch_filled += 1
            try:
                await db.commit()
            except Exception as exc:
                logger.warning(f"大纲元数据写回失败，本批按失败计（{len(batch)} 条）: {exc}")
                await db.rollback()
                batch_filled = 0
            filled += batch_filled
            failed += len(batch) - batch_filled

        if progress is not None:
            processed = min(batch_start + len(batch), len(targets))
            try:
                await progress(
                    f"已处理 {processed}/{len(targets)} 条大纲元数据",
                    processed,
                    len(targets),
                )
            except Exception as exc:  # 进度回调异常不影响任务结果
                logger.warning(f"大纲元数据进度回调失败: {exc}")

    return {"total": total, "filled": filled, "skipped": skipped, "failed": failed}
