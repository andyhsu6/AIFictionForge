#!/usr/bin/env python3
"""只读批量试跑：把「结构化档案事实」流水线对库里所有项目跑一遍，产出新旧对照。

只读承诺（硬约束）：
- 只执行 SELECT；不 insert / update / commit；结束前统一 ``rollback()``。
- 不打印任何 API key / token（只从 Settings 读，不复述）。
- 只新增本文件，不修改任何既有文件。

为什么用 ``_PROMPT_TEMPLATE_FACTS`` 而不是 ``CHARACTERS_BATCH_GENERATION_FACTS``：
后者是「自己发现实体」的拆书提示词，模型会自由起名，与库里现有档案对不上，无法做
新旧对照；前者是生产正在用的「给已有实体更新档案」模板，占位符只有
``{profiles}`` 与 ``{chapters}``，模型只回档案名与事实，才能逐实体对齐。

用法示例::

    DATABASE_URL="sqlite+aiosqlite:////abs/path/backend/data/mumuai_novel.db" \\
    PYTHONPATH=backend \\
    backend/.venv/bin/python backend/scripts/sweep_profiles_readonly.py \\
        --user-id <uid> --project-id <uuid> --out-dir /tmp/sweep
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
import traceback
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path
from typing import Any

# 与 backend/scripts/migrate.py 一致：允许直接 `python backend/scripts/<name>.py`
_project_root = Path(__file__).resolve().parents[1]
if str(_project_root) not in sys.path:
    sys.path.insert(0, str(_project_root))

from sqlalchemy import select  # noqa: E402
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker  # noqa: E402

from app.database import get_engine  # noqa: E402
from app.models import Chapter, Character, Project, Settings  # noqa: E402
from app.services.book_import_service import BookImportService  # noqa: E402
from app.services.character_profile_sync_service import (  # noqa: E402
    _PROMPT_TEMPLATE_FACTS,
    _SYSTEM_PROMPT,
)
from app.services.profile_facts import (  # noqa: E402
    FACT_FIELDS,
    merge_profile_facts,
    parse_profile_payload,
    render_profile,
    validate_profile_facts,
)
from app.services.profile_mentions import (  # noqa: E402
    count_mentions,
    mention_tokens,
    parse_aliases,
    unique_tokens_by_entity,
)
from app.services.prompt_service import PromptService  # noqa: E402

# 丢弃原因（与 profile_facts.validate_profile_facts 的统计键一致）
DISCARD_REASON_KEYS = (
    "payload_invalid",
    "field_invalid",
    "claim_invalid",
    "quote_invalid",
    "quote_not_found",
    "chapter_invalid",
    "inferred_not_allowed",
    "org_personality_dropped",
)

# 每个实体最多重试 1 次（首次失败 + 1 次重试 = 每批最多 2 次调用）
MAX_ATTEMPTS_PER_BATCH = 2
# call_with_json_retry 自身的 JSON 解析重试次数，与生产 _sync_chunk 保持一致
JSON_RETRIES = 2

ENTITY_STATUS_OK = "ok"
ENTITY_STATUS_LEGACY = "legacy_format"
ENTITY_STATUS_LEGACY_SKIPPED = "legacy_skipped"
ENTITY_STATUS_EMPTY = "empty"
ENTITY_STATUS_NOT_IN_TEXT = "not_in_text"
ENTITY_STATUS_FAILED = "failed"


# --------------------------------------------------------------------------- #
# 参数
# --------------------------------------------------------------------------- #
def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="结构化档案事实流水线的只读批量试跑（绝不写库）",
    )
    parser.add_argument("--user-id", required=True, help="用户 ID（读取 settings 里的 provider/model）")
    parser.add_argument(
        "--project-id",
        action="append",
        default=None,
        help="项目 UUID，可重复；缺省 = 库里所有项目",
    )
    parser.add_argument(
        "--chapters-per-window",
        type=int,
        default=15,
        help="每个章节窗口包含的章数，默认 15",
    )
    parser.add_argument(
        "--entities-per-call",
        type=int,
        default=8,
        help="单次模型调用处理的实体数，默认 8",
    )
    parser.add_argument(
        "--mode",
        choices=("sync", "backfill"),
        default="sync",
        help="sync = 现有增量模板（每实体只进一个窗口）；backfill = 整体整理模板 + 跨窗口合并事实，默认 sync",
    )
    parser.add_argument("--out-dir", required=True, help="输出目录（comparison.tsv / report.md / raw.json）")
    parser.add_argument(
        "--skip-legacy-ok",
        action="store_true",
        help="模型返回旧长文本格式时静默跳过（默认记为 legacy_format 作为异常提示）",
    )
    args = parser.parse_args(argv)

    if args.chapters_per_window <= 0:
        parser.error("--chapters-per-window 必须为正数")
    if args.entities_per_call <= 0:
        parser.error("--entities-per-call 必须为正数")
    return args


# --------------------------------------------------------------------------- #
# 只读数据加载
# --------------------------------------------------------------------------- #
async def _user_settings_exists(db: AsyncSession, user_id: str) -> bool:
    """只做 SELECT：确认 settings 存在，避免 _build_user_ai_service 走「缺配置即 insert」。"""
    row = (await db.execute(select(Settings.id).where(Settings.user_id == user_id))).scalar_one_or_none()
    return row is not None


async def _load_projects(db: AsyncSession, project_ids: list[str] | None) -> list[Project]:
    stmt = select(Project).order_by(Project.id)
    if project_ids:
        stmt = select(Project).where(Project.id.in_(project_ids)).order_by(Project.id)
    return list((await db.execute(stmt)).scalars().all())


async def _load_chapters(db: AsyncSession, project_id: str) -> list[Chapter]:
    rows = (
        await db.execute(
            select(Chapter).where(Chapter.project_id == project_id).order_by(Chapter.chapter_number)
        )
    ).scalars().all()
    return [c for c in rows if (c.content or "").strip()]


async def _load_entities(db: AsyncSession, project_id: str) -> list[Any]:
    """只取用到的列（不读整行）：旧库缺 `profile_facts` 等新列时 ORM 整行 SELECT 会失败，
    而本脚本只做只读对照，不需要这些列。"""
    rows = await db.execute(
        select(
            Character.id,
            Character.name,
            Character.aliases,
            Character.is_organization,
            Character.personality,
            Character.background,
            Character.appearance,
        )
        .where(Character.project_id == project_id)
        .order_by(Character.name)
    )
    return list(rows.all())


# --------------------------------------------------------------------------- #
# 窗口与实体分配
# --------------------------------------------------------------------------- #
def _split_windows(chapters: list[Chapter], size: int) -> list[list[Chapter]]:
    return [chapters[i : i + size] for i in range(0, len(chapters), size)]


def _assign_entities(
    entities: list[Any], windows: list[list[Chapter]]
) -> tuple[dict[int, list[Any]], list[Any]]:
    """每个实体只进一个窗口：出现次数最多的窗口；并列取最早。

    匹配口径与生产同步链路一致：``name + aliases``（别名只在项目内唯一时才参与，
    见 ``profile_mentions.unique_tokens_by_entity``）——正文只用简称/称谓的实体
    以前会被误判成"从未出现"。

    从未在任何窗口正文中出现的实体不参与调用（无依据可更新），单独返回，
    仍会在 comparison.tsv 里占位（new 为空），保证「行数 = 实体数 × 3」。
    """
    window_texts = ["".join(c.content or "" for c in window) for window in windows]
    unique_map = unique_tokens_by_entity(entities)
    assigned: dict[int, list[Any]] = defaultdict(list)
    unmentioned: list[Any] = []

    for entity in entities:
        tokens = mention_tokens(entity, unique_map)
        best_index: int | None = None
        best_count = 0
        if tokens:
            for index, text in enumerate(window_texts):
                count = count_mentions(text, tokens)
                # 严格大于：并列时保留更早的窗口（首次出现优先）
                if count > best_count:
                    best_count, best_index = count, index
        if best_index is None:
            unmentioned.append(entity)
        else:
            assigned[best_index].append(entity)
    return assigned, unmentioned


def _render_profiles(entities: list[Any]) -> str:
    """该批实体现有档案的 JSON（与模板 {profiles} 占位符对应）。

    ``aliases`` 用全部别名（不做唯一性过滤）：这里是给模型看的上下文，
    与生产 ``_render_profiles`` 列出别名的做法一致；唯一性只影响"正文命中归属"判定。
    """
    payload = [
        {
            "name": entity.name,
            "is_organization": bool(entity.is_organization),
            "aliases": parse_aliases(entity.aliases),
            "personality": entity.personality or "",
            "background": entity.background or "",
            "appearance": entity.appearance or "",
        }
        for entity in entities
    ]
    return json.dumps(payload, ensure_ascii=False, indent=2)


def _build_backfill_prompt(entities: list[Any], chapters: list[Chapter]) -> str:
    """backfill 模式：整体整理语境（不是增量补充），跨窗口覆盖同一实体。"""
    return PromptService.PROFILE_BACKFILL_FACTS.format(
        profiles=_render_profiles(entities),
        chapters=BookImportService._build_chapter_excerpt(chapters),
    )


def _build_prompt(entities: list[Any], chapters: list[Chapter]) -> str:
    # 生产 _sync_chunk 用同一个模板 + 同样两个占位符
    return _PROMPT_TEMPLATE_FACTS.format(
        profiles=_render_profiles(entities),
        chapters=BookImportService._build_chapter_excerpt(chapters),
    )


# --------------------------------------------------------------------------- #
# 单批调用
# --------------------------------------------------------------------------- #
async def _call_batch(
    *,
    ai_service: Any,
    entities: list[Any],
    chapters: list[Chapter],
    build_prompt: Any = _build_prompt,
    system_prompt: str = _SYSTEM_PROMPT,
) -> dict[str, Any]:
    """调用模型，失败重试 1 次；仍失败则返回 error，不抛异常。"""
    prompt = build_prompt(entities, chapters)
    started = time.perf_counter()
    last_error = ""
    data: Any = None
    attempts = 0

    for attempt in range(1, MAX_ATTEMPTS_PER_BATCH + 1):
        attempts = attempt
        try:
            data = await ai_service.call_with_json_retry(
                prompt=prompt,
                system_prompt=system_prompt,
                max_retries=JSON_RETRIES,
                expected_type="object",
                auto_mcp=False,
            )
            if not isinstance(data, dict):
                raise ValueError(f"返回顶层类型不是 object: {type(data).__name__}")
            last_error = ""
            break
        except Exception as exc:  # 含 ApiError(ai.content_filtered) 等上游失败
            last_error = f"{type(exc).__name__}: {exc}"
            data = None

    seconds = round(time.perf_counter() - started, 1)
    out_chars = len(json.dumps(data, ensure_ascii=False, default=str)) if data is not None else 0
    return {
        "prompt_chars": len(prompt),
        "out_chars": out_chars,
        "seconds": seconds,
        "attempts": attempts,
        "error": last_error,
        "raw": data,
        "entries": (data or {}).get("characters") if isinstance(data, dict) else None,
    }


def _entry_map(entries: Any, batch: list[Any]) -> tuple[dict[str, dict], list[str]]:
    """按档案名对齐批次实体；批次外的名字单独记下（不改库、不猜）。

    模型有时回的是正文里的简称/称谓而不是档案主名，先按 name 精确命中，未命中时
    按**本批次内唯一**的 token（别名）解析回实体主名。同名或歧义 token 不解析
    （``unique_tokens_by_entity`` 已把歧义 token 排除），仍进 ``unmatched``。
    """
    by_name = {entity.name: entity for entity in batch}
    unique_map = unique_tokens_by_entity(batch)
    by_token: dict[str, Any] = {}
    for entity in batch:
        for token in unique_map.get(entity.id) or []:
            by_token[token] = entity

    matched: dict[str, dict] = {}
    unmatched: list[str] = []
    if not isinstance(entries, list):
        return matched, unmatched
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        name = entry.get("name")
        if not isinstance(name, str):
            continue
        entity = by_name.get(name)
        if entity is None:
            entity = by_token.get(name)
        if entity is None:
            unmatched.append(name)
        else:
            matched.setdefault(entity.name, entry)
    return matched, unmatched


def _entity_result(
    *,
    entity: Any,
    project_id: str,
    window_index: int | None,
    status: str,
    entry: Any,
    chapters_by_number: dict[int, str],
) -> dict[str, Any]:
    old = {field: (getattr(entity, field, None) or "") for field in FACT_FIELDS}
    new: dict[str, str] = {}
    stats: dict[str, int] = {}
    facts_by_field: dict[str, int] = {}
    parsed_mode = "none"

    if isinstance(entry, dict):
        parsed = parse_profile_payload(entry)
        parsed_mode = parsed.mode
        if parsed.mode == "facts":
            kind = "organization" if entity.is_organization else "character"
            result = validate_profile_facts(
                {"name": parsed.entity, "facts": parsed.facts},
                chapters_by_number,
                kind=kind,
            )
            stats = dict(result.stats)
            new = render_profile(result.facts, kind=kind)
            facts_by_field = dict(Counter(fact["field"] for fact in result.facts))
        elif parsed.mode == "legacy":
            # 旧长文本格式：不写库、不参与新旧对照，只记录格式
            stats = {"payloads_legacy": 1}

    facts_in = stats.get("facts_in", 0)
    accepted = stats.get("facts_accepted", 0)
    return {
        "project_id": project_id,
        "name": entity.name,
        "kind": "organization" if entity.is_organization else "person",
        "status": status,
        "window": window_index,
        "parsed_mode": parsed_mode,
        "old": old,
        "new": new,
        "facts_by_field": facts_by_field,
        "facts_count": accepted,
        "facts_in": facts_in,
        "accepted": accepted,
        "discarded": {key: stats.get(key, 0) for key in DISCARD_REASON_KEYS if stats.get(key, 0)},
        "stats": stats,
        "hit_rate": (accepted / facts_in) if facts_in else None,
        "raw_entry": entry,
    }


# --------------------------------------------------------------------------- #
# 单项目执行
# --------------------------------------------------------------------------- #
async def _run_project(
    *,
    db: AsyncSession,
    project: Project,
    ai_service: Any,
    args: argparse.Namespace,
) -> dict[str, Any]:
    chapters = await _load_chapters(db, project.id)
    entities = await _load_entities(db, project.id)

    record: dict[str, Any] = {
        "project_id": project.id,
        "title": project.title,
        "chapter_count": len(chapters),
        "entity_count": len(entities),
        "windows": 0,
        "calls": 0,
        "failures": 0,
        "seconds": 0.0,
        "prompt_chars": 0,
        "out_chars": 0,
        "unmatched_names": [],
        "entities": [],
        "error": "",
    }
    if not chapters or not entities:
        record["error"] = "skip: 项目没有正文非空的章节或没有实体"
        return record

    windows = _split_windows(chapters, args.chapters_per_window)
    record["windows"] = len(windows)
    assigned, unmentioned = _assign_entities(entities, windows)

    for entity in unmentioned:
        record["entities"].append(
            _entity_result(
                entity=entity,
                project_id=project.id,
                window_index=None,
                status=ENTITY_STATUS_NOT_IN_TEXT,
                entry=None,
                chapters_by_number={},
            )
        )

    started = time.perf_counter()
    for window_index, window_chapters in enumerate(windows):
        window_entities = assigned.get(window_index) or []
        if not window_entities:
            continue
        # 引文校验必须用整章全文（模型看到的是摘录，命中说明这句话确实在该章里）
        chapters_by_number = {c.chapter_number: (c.content or "") for c in window_chapters}

        for start in range(0, len(window_entities), args.entities_per_call):
            batch = window_entities[start : start + args.entities_per_call]
            call = await _call_batch(
                ai_service=ai_service, entities=batch, chapters=window_chapters
            )
            record["calls"] += 1
            record["prompt_chars"] += call["prompt_chars"]
            record["out_chars"] += call["out_chars"]

            if call["error"]:
                record["failures"] += 1
                for entity in batch:
                    row = _entity_result(
                        entity=entity,
                        project_id=project.id,
                        window_index=window_index,
                        status=ENTITY_STATUS_FAILED,
                        entry=None,
                        chapters_by_number=chapters_by_number,
                    )
                    row["error"] = call["error"]
                    record["entities"].append(row)
                continue

            matched, unmatched = _entry_map(call["entries"], batch)
            record["unmatched_names"].extend(unmatched)
            for entity in batch:
                entry = matched.get(entity.name)
                if entry is None:
                    status = ENTITY_STATUS_EMPTY
                else:
                    mode = parse_profile_payload(entry).mode
                    if mode == "facts":
                        status = ENTITY_STATUS_OK
                    elif mode == "legacy":
                        status = (
                            ENTITY_STATUS_LEGACY_SKIPPED
                            if args.skip_legacy_ok
                            else ENTITY_STATUS_LEGACY
                        )
                    else:
                        status = ENTITY_STATUS_EMPTY
                record["entities"].append(
                    _entity_result(
                        entity=entity,
                        project_id=project.id,
                        window_index=window_index,
                        status=status,
                        entry=entry,
                        chapters_by_number=chapters_by_number,
                    )
                )

    record["seconds"] = round(time.perf_counter() - started, 1)
    return record


async def _run_project_backfill(
    *,
    db: AsyncSession,
    project: Project,
    ai_service: Any,
    args: argparse.Namespace,
) -> dict[str, Any]:
    """backfill 模式：窗口内实体全量跑（不去重）+ 跨窗口事实合并。

    与 `_run_project` 的差异只有两处：不做"每实体只进一个窗口"的分配，
    以及同一实体跨窗口的通过校验事实按 `merge_profile_facts` 语义合并后再渲染。
    """
    chapters = await _load_chapters(db, project.id)
    entities = await _load_entities(db, project.id)

    record: dict[str, Any] = {
        "project_id": project.id,
        "title": project.title,
        "chapter_count": len(chapters),
        "entity_count": len(entities),
        "windows": 0,
        "calls": 0,
        "failures": 0,
        "seconds": 0.0,
        "prompt_chars": 0,
        "out_chars": 0,
        "unmatched_names": [],
        "entities": [],
        "error": "",
    }
    if not chapters or not entities:
        record["error"] = "skip: 项目没有正文非空的章节或没有实体"
        return record

    windows = _split_windows(chapters, args.chapters_per_window)
    record["windows"] = len(windows)
    # 唯一性判定必须在**整个项目的实体列表**上做（不是窗口内的子集）：某个 token
    # 是否歧义跟本次只看哪些实体无关；唯一性拿不到时 mention_tokens 退回只用 name。
    unique_map = unique_tokens_by_entity(entities)

    # 实体名 → 跨窗口累积槽位
    slots: dict[str, dict[str, Any]] = {}
    started = time.perf_counter()

    for window_index, window_chapters in enumerate(windows):
        window_text = "".join(c.content or "" for c in window_chapters)
        window_entities = [
            entity
            for entity in entities
            if count_mentions(window_text, mention_tokens(entity, unique_map)) > 0
        ]
        if not window_entities:
            continue
        # 引文校验必须用整章全文（模型看到的是摘录）
        chapters_by_number = {c.chapter_number: (c.content or "") for c in window_chapters}

        for start in range(0, len(window_entities), args.entities_per_call):
            batch = window_entities[start : start + args.entities_per_call]
            call = await _call_batch(
                ai_service=ai_service,
                entities=batch,
                chapters=window_chapters,
                build_prompt=_build_backfill_prompt,
                system_prompt=PromptService.PROFILE_BACKFILL_SYSTEM,
            )
            record["calls"] += 1
            record["prompt_chars"] += call["prompt_chars"]
            record["out_chars"] += call["out_chars"]

            if call["error"]:
                record["failures"] += 1
                for entity in batch:
                    slot = _slot(slots, entity.name)
                    slot["tried"] += 1
                    slot["failed"] += 1
                    slot["error"] = call["error"]
                continue

            matched, unmatched = _entry_map(call["entries"], batch)
            record["unmatched_names"].extend(unmatched)
            for entity in batch:
                slot = _slot(slots, entity.name)
                slot["tried"] += 1
                entry = matched.get(entity.name)
                slot["entries"].append({"window": window_index, "entry": entry})
                parsed = parse_profile_payload(entry) if entry is not None else None
                if parsed is None or parsed.mode != "facts":
                    if parsed is not None and parsed.mode == "legacy":
                        slot["legacy"] += 1
                    continue
                slot["ok"] += 1
                kind = "organization" if entity.is_organization else "character"
                result = validate_profile_facts(
                    {"name": parsed.entity, "facts": parsed.facts},
                    chapters_by_number,
                    kind=kind,
                )
                slot["stats"].update(result.stats)
                if result.facts:
                    # merge_profile_facts 语义：按 (field, normalize(claim)) 去重、新值覆盖、
                    # 每字段只留最近 FACT_FIELD_LIMITS 条；传入/传出都是 JSON 串
                    slot["facts"] = merge_profile_facts(slot["facts"], result.facts)

    for entity in entities:
        slot = slots.get(entity.name)
        record["entities"].append(
            _backfill_entity_result(
                entity=entity,
                project_id=project.id,
                slot=slot,
                skip_legacy_ok=args.skip_legacy_ok,
            )
        )

    record["seconds"] = round(time.perf_counter() - started, 1)
    return record


def _slot(slots: dict[str, dict[str, Any]], name: str) -> dict[str, Any]:
    return slots.setdefault(
        name,
        {
            "tried": 0,
            "ok": 0,
            "failed": 0,
            "legacy": 0,
            "facts": None,
            "stats": Counter(),
            "entries": [],
            "error": "",
        },
    )


def _backfill_entity_result(
    *,
    entity: Any,
    project_id: str,
    slot: dict[str, Any] | None,
    skip_legacy_ok: bool,
) -> dict[str, Any]:
    old = {field: (getattr(entity, field, None) or "") for field in FACT_FIELDS}
    # 对照列用 person/organization（与 sync 模式一致）；校验用 character/organization
    kind = "organization" if entity.is_organization else "person"
    validate_kind = "organization" if entity.is_organization else "character"

    if slot is None:
        row = _entity_result(
            entity=entity,
            project_id=project_id,
            window_index=None,
            status=ENTITY_STATUS_NOT_IN_TEXT,
            entry=None,
            chapters_by_number={},
        )
        row["windows_tried"] = 0
        row["windows_ok"] = 0
        row["raw_entries"] = []
        return row

    facts = json.loads(slot["facts"]) if slot["facts"] else []
    stats = dict(slot["stats"])
    facts_in = stats.get("facts_in", 0)
    accepted = stats.get("facts_accepted", 0)

    if facts:
        status = ENTITY_STATUS_OK
    elif slot["ok"]:
        status = ENTITY_STATUS_EMPTY
    elif slot["legacy"] and slot["legacy"] == slot["tried"]:
        status = ENTITY_STATUS_LEGACY_SKIPPED if skip_legacy_ok else ENTITY_STATUS_LEGACY
    else:
        status = ENTITY_STATUS_FAILED

    row = {
        "project_id": project_id,
        "name": entity.name,
        "kind": kind,
        "status": status,
        "window": None,
        "parsed_mode": "facts" if slot["ok"] else ("legacy" if slot["legacy"] else "none"),
        "old": old,
        "new": render_profile(facts, kind=validate_kind),
        "facts_by_field": dict(Counter(fact["field"] for fact in facts)),
        "facts_count": len(facts),
        "facts_in": facts_in,
        "accepted": accepted,
        "discarded": {key: stats.get(key, 0) for key in DISCARD_REASON_KEYS if stats.get(key, 0)},
        "stats": stats,
        "hit_rate": (accepted / facts_in) if facts_in else None,
        "raw_entry": None,
        "raw_entries": slot["entries"],
        "windows_tried": slot["tried"],
        "windows_ok": slot["ok"],
        "error": slot["error"],
    }
    return row


# --------------------------------------------------------------------------- #
# 输出
# --------------------------------------------------------------------------- #
def _tsv_field(text: str) -> str:
    """TSV 单元格：换行/制表符压成空格，避免破坏列结构。"""
    return " ".join((text or "").split())


def _write_comparison(path: Path, projects: list[dict[str, Any]]) -> int:
    lines = ["project_id\tname\tkind\tfield\told_chars\tnew_chars\tfacts\told_text\tnew_text"]
    for project in projects:
        for entity in project["entities"]:
            for field in FACT_FIELDS:
                old_text = entity["old"].get(field, "")
                new_text = entity["new"].get(field, "")
                lines.append(
                    "\t".join(
                        [
                            project["project_id"],
                            _tsv_field(entity["name"]),
                            entity["kind"],
                            field,
                            str(len(old_text)),
                            str(len(new_text)),
                            str(entity["facts_by_field"].get(field, 0)),
                            _tsv_field(old_text),
                            _tsv_field(new_text),
                        ]
                    )
                )
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return len(lines) - 1


def _md_table(headers: list[str], rows: list[list[Any]]) -> str:
    lines = [
        "| " + " | ".join(headers) + " |",
        "| " + " | ".join("---" for _ in headers) + " |",
    ]
    for row in rows:
        lines.append("| " + " | ".join(str(cell) for cell in row) + " |")
    return "\n".join(lines)


def _fmt_rate(rate: float | None) -> str:
    return "n/a" if rate is None else f"{rate:.3f}"


def _project_metrics(project: dict[str, Any]) -> dict[str, Any]:
    entities = project["entities"]
    totals: Counter = Counter()
    discard: Counter = Counter()
    for entity in entities:
        totals.update(entity["stats"] or {})
        discard.update(entity["discarded"])

    facts_in = totals.get("facts_in", 0)
    accepted = totals.get("facts_accepted", 0)
    field_slots = len(entities) * len(FACT_FIELDS)
    return {
        "facts_in": facts_in,
        "accepted": accepted,
        "hit_rate": (accepted / facts_in) if facts_in else None,
        "facts_per_field": round(accepted / field_slots, 2) if field_slots else 0.0,
        "discard": dict(discard),
        "status_counts": dict(Counter(entity["status"] for entity in entities)),
    }


DIFF_BUCKETS = (
    "old 有 → new 空",
    "old 空 → new 空",
    "old 有 → new 有（变长）",
    "old 有 → new 有（变短）",
    "old 空 → new 有",
)


def _diff_rows(projects: list[dict[str, Any]]) -> dict[str, int]:
    """五类差异：把「老内容被清空」单独拎出来，它是最危险的一类。"""
    buckets: Counter = Counter({key: 0 for key in DIFF_BUCKETS})
    for project in projects:
        for entity in project["entities"]:
            for field in FACT_FIELDS:
                old_text = entity["old"].get(field, "")
                new_text = entity["new"].get(field, "")
                old_has = bool(old_text.strip())
                new_has = bool(new_text.strip())
                if old_has and not new_has:
                    buckets["old 有 → new 空"] += 1
                elif not old_has and not new_has:
                    buckets["old 空 → new 空"] += 1
                elif old_has and new_has:
                    if len(new_text) > len(old_text):
                        buckets["old 有 → new 有（变长）"] += 1
                    else:
                        buckets["old 有 → new 有（变短）"] += 1
                else:
                    buckets["old 空 → new 有"] += 1
    return {key: buckets[key] for key in DIFF_BUCKETS}


def _loss_rows(projects: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """逐字段列出「old 有 → new 空」的行，供按 project / kind / field 分组。"""
    rows: list[dict[str, Any]] = []
    for project in projects:
        for entity in project["entities"]:
            for field in FACT_FIELDS:
                old_text = entity["old"].get(field, "")
                new_text = entity["new"].get(field, "")
                if old_text.strip() and not new_text.strip():
                    rows.append(
                        {
                            "project_id": project["project_id"],
                            "name": entity["name"],
                            "kind": entity["kind"],
                            "field": field,
                            "old_chars": len(old_text),
                            "status": entity["status"],
                        }
                    )
    return rows


def _group_counts(rows: list[dict[str, Any]], key: str) -> dict[str, int]:
    return dict(Counter(row[key] for row in rows).most_common())



def _write_report(
    path: Path,
    *,
    args: argparse.Namespace,
    projects: list[dict[str, Any]],
    summary: dict[str, Any],
    started_at: str,
) -> None:
    lines: list[str] = ["# 结构化档案事实流水线：只读批量试跑", ""]
    lines.append(f"生成时间：{started_at}")
    lines.append("")
    lines.append(
        "> 本脚本只读：全程只有 SELECT，结束时统一 `rollback()`，不写任何行。"
        "已跑项目里的 `new_text` 是本次模型返回经校验后的**假设**渲染结果，"
        "库里的 `characters` 表未被修改。"
    )
    lines.append("")
    lines.append("## 运行参数")
    lines.append("")
    lines.append(
        _md_table(
            ["参数", "值"],
            [
                ["user_id", args.user_id],
                ["project_id", ",".join(args.project_id) if args.project_id else "（全部项目）"],
                ["mode", args.mode],
                ["chapters_per_window", args.chapters_per_window],
                ["entities_per_call", args.entities_per_call],
                ["skip_legacy_ok", args.skip_legacy_ok],
                [
                    "prompt_template",
                    "`PROFILE_BACKFILL_FACTS` + `PROFILE_BACKFILL_SYSTEM`（占位符 profiles / chapters，整体整理语境）"
                    if args.mode == "backfill"
                    else "`_PROMPT_TEMPLATE_FACTS`（占位符 profiles / chapters）",
                ],
                ["out_dir", args.out_dir],
            ],
        )
    )
    lines.append("")
    lines.append("## 总览")
    lines.append("")
    diff = _diff_rows(projects)
    lost = diff["old 有 → new 空"]
    total_slots = sum(len(project["entities"]) for project in projects) * len(FACT_FIELDS)
    loss_rows = _loss_rows(projects)
    calls = summary["calls"]
    lines.append(
        _md_table(
            ["指标", "值"],
            [
                ["项目数", summary["projects"]],
                ["实体数", summary["entities"]],
                ["模型调用次数（call_with_json_retry 次数）", calls],
                ["失败批次数", summary["failures"]],
                ["调用成功率", _fmt_rate(summary["success_rate"])],
                ["引文命中率 (accepted / facts_in)", _fmt_rate(summary["hit_rate"])],
                ["facts_in / accepted", f"{summary['facts_in']} / {summary['accepted']}"],
                [
                    "平均 facts/字段",
                    round(summary["accepted"] / total_slots, 2) if total_slots else 0.0,
                ],
                [
                    "**数据丢失（old 有 → new 空）**",
                    f"**{lost} 条 / {total_slots} 个字段槽位（占比 "
                    f"{_fmt_rate(lost / total_slots if total_slots else None)}），"
                    f"涉及 {len({row['name'] for row in loss_rows})} 个实体**",
                ],
                ["总耗时（秒）", summary["seconds"]],
                [
                    "总 token 估算（粗估：字符数 / 2，中英混排）",
                    f"prompt≈{summary['prompt_tokens']} / output≈{summary['out_tokens']}",
                ],
            ],
        )
    )
    lines.append("")
    lines.append("状态分布：" + json.dumps(summary["status_counts"], ensure_ascii=False))
    lines.append("")
    if summary["failed_projects"]:
        lines.append("**项目级失败：**")
        for item in summary["failed_projects"]:
            lines.append(f"- `{item['project_id']}`：{item['error']}")
        lines.append("")

    lines.append("## 逐项目指标")
    lines.append("")
    rows = []
    for project in projects:
        metrics = _project_metrics(project)
        loss = sum(
            1
            for entity in project["entities"]
            for field in FACT_FIELDS
            if entity["old"].get(field, "").strip() and not entity["new"].get(field, "").strip()
        )
        rows.append(
            [
                f"`{project['project_id']}`",
                project["chapter_count"],
                project["windows"],
                project["entity_count"],
                project["calls"],
                _fmt_rate(metrics["hit_rate"]),
                metrics["facts_per_field"],
                loss,
                json.dumps(metrics["discard"], ensure_ascii=False) if metrics["discard"] else "-",
                project["failures"],
            ]
        )
    lines.append(
        _md_table(
            [
                "project_id",
                "章节数",
                "窗口数",
                "实体数",
                "调用数",
                "引文命中率",
                "平均 facts/字段",
                "数据丢失",
                "丢弃原因分布",
                "失败批次",
            ],
            rows,
        )
    )
    lines.append("")
    lines.append("状态分布（逐项目）：")
    lines.append("")
    lines.append(
        _md_table(
            ["project_id", "状态分布"],
            [
                [f"`{project['project_id']}`", json.dumps(_project_metrics(project)["status_counts"], ensure_ascii=False)]
                for project in projects
            ],
        )
    )
    lines.append("")

    lines.append("## 数据丢失：old 有 → new 空")
    lines.append("")
    lines.append(
        f"- **合计 {lost} 条 / {total_slots} 个字段槽位（占比 {_fmt_rate(lost / total_slots if total_slots else None)}）**，"
        f"涉及实体 {len({row['name'] for row in loss_rows})} 个"
    )
    lines.append("")
    lines.append("按 project：")
    lines.append("")
    lines.append(
        _md_table(
            ["project_id", "条数"],
            [[f"`{pid}`", count] for pid, count in _group_counts(loss_rows, "project_id").items()] or [["-", 0]],
        )
    )
    lines.append("")
    lines.append("按 kind：")
    lines.append("")
    lines.append(
        _md_table(
            ["kind", "条数"],
            [[kind, count] for kind, count in _group_counts(loss_rows, "kind").items()] or [["-", 0]],
        )
    )
    lines.append("")
    lines.append("按 field：")
    lines.append("")
    lines.append(
        _md_table(
            ["field", "条数"],
            [[field, diff_lost] for field, diff_lost in _group_counts(loss_rows, "field").items()] or [["-", 0]],
        )
    )
    lines.append("")
    if loss_rows:
        lines.append("逐条（old_chars 越大损失越多）：")
        lines.append("")
        lines.append(
            _md_table(
                ["project_id", "实体", "kind", "field", "old_chars", "状态"],
                [
                    [
                        f"`{row['project_id']}`",
                        row["name"],
                        row["kind"],
                        row["field"],
                        row["old_chars"],
                        row["status"],
                    ]
                    for row in sorted(loss_rows, key=lambda item: -item["old_chars"])
                ],
            )
        )
        lines.append("")

    lines.append("## 新旧差异分布（三个字段 × 全部实体）")
    lines.append("")
    lines.append(
        _md_table(
            ["类别", "字段条数"],
            [
                ["old 有 → new 空（数据丢失，最危险）", diff["old 有 → new 空"]],
                ["old 空 → new 空", diff["old 空 → new 空"]],
                ["old 有 → new 有（变长）", diff["old 有 → new 有（变长）"]],
                ["old 有 → new 有（变短）", diff["old 有 → new 有（变短）"]],
                ["old 空 → new 有（新增覆盖）", diff["old 空 → new 有"]],
            ],
        )
    )
    lines.append("")
    lines.append("（「变短」含等长的新值；无归纳字段的实体计在 `new 空` 一侧。）")
    lines.append("")
    lines.append("逐算子项（每项 = 一个实体的一个字段）：")
    lines.append("")
    lines.append(
        _md_table(
            ["project_id", "实体", "kind", "窗口", "窗口 tried/ok", "状态", "facts_in", "accepted", "丢弃原因"],
            [
                [
                    f"`{project['project_id']}`",
                    entity["name"],
                    entity["kind"],
                    entity["window"] if entity.get("window") is not None else "-",
                    (
                        f"{entity['windows_tried']}/{entity['windows_ok']}"
                        if "windows_tried" in entity
                        else "-"
                    ),
                    entity["status"],
                    entity["facts_in"],
                    entity["accepted"],
                    json.dumps(entity["discarded"], ensure_ascii=False) if entity["discarded"] else "-",
                ]
                for project in projects
                for entity in sorted(project["entities"], key=lambda item: (item["name"] or ""))
            ],
        )
    )
    lines.append("")
    path.write_text("\n".join(lines), encoding="utf-8")


# --------------------------------------------------------------------------- #
# 主流程
# --------------------------------------------------------------------------- #
async def run(args: argparse.Namespace) -> int:
    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    started_at = datetime.now().isoformat(timespec="seconds")
    run_started = time.perf_counter()

    projects: list[dict[str, Any]] = []
    failed_projects: list[dict[str, str]] = []
    ai_error = ""

    engine = await get_engine(args.user_id)
    session_factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

    async with session_factory() as db:
        try:
            ai_service = None
            if await _user_settings_exists(db, args.user_id):
                ai_service = await BookImportService()._build_user_ai_service(
                    db=db, user_id=args.user_id
                )
            else:
                ai_error = (
                    f"用户 {args.user_id} 没有 Settings 记录。为保证不写库，脚本不会让 "
                    "_build_user_ai_service 走 insert 分支；请先在应用里配置一次 AI 设置后再跑。"
                )

            for project in await _load_projects(db, args.project_id):
                if ai_service is None:
                    failed_projects.append({"project_id": project.id, "error": ai_error})
                    continue
                try:
                    runner = _run_project if args.mode == "sync" else _run_project_backfill
                    projects.append(
                        await runner(
                            db=db, project=project, ai_service=ai_service, args=args
                        )
                    )
                except Exception as exc:  # 项目级失败不中断整个扫描
                    failed_projects.append(
                        {
                            "project_id": project.id,
                            "error": f"{type(exc).__name__}: {exc}",
                            "traceback": traceback.format_exc(),
                        }
                    )
                    print(f"[project-failed] {project.id}: {type(exc).__name__}: {exc}", file=sys.stderr)
        finally:
            # 只读承诺：无论成功失败都不提交，统一回滚
            try:
                await db.rollback()
            except Exception:
                pass

    all_entities = [entity for project in projects for entity in project["entities"]]
    totals: Counter = Counter()
    for entity in all_entities:
        totals.update(entity["stats"] or {})
    facts_in = totals.get("facts_in", 0)
    accepted = totals.get("facts_accepted", 0)
    calls = sum(project["calls"] for project in projects)
    failures = sum(project["failures"] for project in projects)
    prompt_chars = sum(project["prompt_chars"] for project in projects)
    out_chars = sum(project["out_chars"] for project in projects)
    seconds = round(time.perf_counter() - run_started, 1)

    summary = {
        "projects": len(projects),
        "entities": len(all_entities),
        "calls": calls,
        "failures": failures,
        "success_rate": ((calls - failures) / calls) if calls else None,
        "hit_rate": (accepted / facts_in) if facts_in else None,
        "facts_in": facts_in,
        "accepted": accepted,
        "seconds": seconds,
        "prompt_tokens": round(prompt_chars / 2),
        "out_tokens": round(out_chars / 2),
        "status_counts": dict(Counter(entity["status"] for entity in all_entities)),
        "failed_projects": failed_projects,
    }

    _write_comparison(out_dir / "comparison.tsv", projects)
    _write_report(
        out_dir / "report.md",
        args=args,
        projects=projects,
        summary=summary,
        started_at=started_at,
    )
    (out_dir / "raw.json").write_text(
        json.dumps(
            {
                "run": {
                    "started_at": started_at,
                    "seconds": seconds,
                    "args": {
                        "user_id": args.user_id,
                        "project_id": args.project_id,
                        "mode": args.mode,
                        "chapters_per_window": args.chapters_per_window,
                        "entities_per_call": args.entities_per_call,
                        "out_dir": args.out_dir,
                        "skip_legacy_ok": args.skip_legacy_ok,
                    },
                    "summary": summary,
                },
                "projects": projects,
            },
            ensure_ascii=False,
            indent=2,
            default=str,
        ),
        encoding="utf-8",
    )

    print(f"OUT {out_dir}")
    print(
        f"SUMMARY projects={summary['projects']} entities={summary['entities']} "
        f"calls={summary['calls']} failures={summary['failures']} "
        f"hit_rate={_fmt_rate(summary['hit_rate'])} seconds={summary['seconds']}"
    )

    total_projects = summary["projects"] + len(failed_projects)
    if total_projects == 0:
        print("ERROR 没有可跑的项目（--project-id 未匹配任何项目）", file=sys.stderr)
        return 1
    return 1 if summary["projects"] == 0 else 0


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    return asyncio.run(run(args))


if __name__ == "__main__":
    sys.exit(main())
