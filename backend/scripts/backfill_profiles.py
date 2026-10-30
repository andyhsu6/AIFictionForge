#!/usr/bin/env python3
"""档案回填工具（默认 dry-run）：把「结构化档案事实」流水线产出的档案写回 `characters`。

默认只算不写：不加 `--apply` 时全程 `rollback()`，只产出 `plan.tsv` / `report.md`。
`--apply` 才逐项目 `commit()`（本任务只跑过 dry-run）。

取数/调用/合并逻辑直接复用 `scripts/sweep_profiles_readonly.py` 的 backfill 实现
（窗口切分、`_call_batch`、`PROFILE_BACKFILL_FACTS` 提示词、`merge_profile_facts`），
不另写一套。

写入策略（逐字段独立判断，与 `ai_profile_snapshot` 语义一致）：
1. 新文本为空 → 不动该字段（绝不用空值清空老内容）；
2. 有快照且 `snapshot[field]` 存在且当前值 != 快照值 → 判定用户手改，保留现值；
3. 其余（含无快照的历史数据）→ 用新文本覆盖；文本与现值相同记 `unchanged`。

用法::

    DATABASE_URL=... PYTHONPATH=backend backend/.venv/bin/python \\
        backend/scripts/backfill_profiles.py \\
        --user-id <uid> --only-empty --out-dir /tmp/backfill-empty
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
import traceback
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any

# 与 backend/scripts/migrate.py 一致：允许直接 `python backend/scripts/<name>.py`
_project_root = Path(__file__).resolve().parents[1]
if str(_project_root) not in sys.path:
    sys.path.insert(0, str(_project_root))
# 复用同目录的只读试跑实现（不是包，直接把脚本目录放进 sys.path）
_scripts_dir = Path(__file__).resolve().parent
if str(_scripts_dir) not in sys.path:
    sys.path.insert(0, str(_scripts_dir))

from sqlalchemy import select  # noqa: E402
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker  # noqa: E402

import sweep_profiles_readonly as sweep  # noqa: E402
from app.database import get_engine  # noqa: E402
from app.models import Character  # noqa: E402
from app.services.profile_facts import (  # noqa: E402
    FACT_FIELDS,
    merge_profile_facts,
    parse_profile_payload,
    render_profile,
    validate_profile_facts,
)
from app.services.prompt_service import PromptService  # noqa: E402

ACTION_UPDATED = "updated"
ACTION_PRESERVED = "preserved_user_edit"
ACTION_SKIPPED_EMPTY = "skipped_empty"
ACTION_UNCHANGED = "unchanged"
ACTION_FAILED = "failed"


# --------------------------------------------------------------------------- #
# 参数
# --------------------------------------------------------------------------- #
def _bool_arg(value: str | None) -> bool:
    """`--apply` 开关容忍 `--apply=false` 这类显式写法（视为未加）。"""
    if value is None:
        return True
    return value.strip().lower() not in {"0", "false", "no", "off", ""}


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="档案回填（默认 dry-run，绝不写库）")
    parser.add_argument("--user-id", required=True, help="用户 ID（读取 settings 里的 provider/model）")
    parser.add_argument(
        "--project-id", action="append", default=None, help="项目 UUID，可重复；缺省 = 所有项目"
    )
    parser.add_argument("--chapters-per-window", type=int, default=15, help="每个窗口的章数，默认 15")
    parser.add_argument("--entities-per-call", type=int, default=8, help="单次调用处理的实体数，默认 8")
    parser.add_argument("--out-dir", required=True, help="输出目录（plan.tsv / report.md）")
    parser.add_argument(
        "--only-empty", action="store_true", help="只处理 personality/background/appearance 全空的实体"
    )
    parser.add_argument("--limit", type=int, default=0, help="最多处理 N 个实体（0 = 不限）")
    parser.add_argument(
        "--apply",
        nargs="?",
        const=True,
        default=False,
        type=_bool_arg,
        help="真正写库并 commit；不加（或 --apply=false）即 dry-run",
    )
    args = parser.parse_args(argv)

    if args.chapters_per_window <= 0:
        parser.error("--chapters-per-window 必须为正数")
    if args.entities_per_call <= 0:
        parser.error("--entities-per-call 必须为正数")
    if args.limit < 0:
        parser.error("--limit 不能为负")
    return args


# --------------------------------------------------------------------------- #
# 取数（显式列：档案字段 + 快照 + 溯源）
# --------------------------------------------------------------------------- #
async def _load_entity_rows(db: AsyncSession, project_id: str) -> list[Any]:
    """显式列 SELECT（不读整行）：需要现有档案、`ai_profile_snapshot`、`profile_facts`。

    写回时再用 `db.get(Character, id)` 取 ORM 对象（见 `_write_entity`）。
    """
    rows = await db.execute(
        select(
            Character.id,
            Character.name,
            Character.is_organization,
            Character.personality,
            Character.background,
            Character.appearance,
            Character.profile_facts,
            Character.ai_profile_snapshot,
        )
        .where(Character.project_id == project_id)
        .order_by(Character.name)
    )
    return list(rows.all())


def _is_blank_entity(entity: Any) -> bool:
    return not any((getattr(entity, field, None) or "").strip() for field in FACT_FIELDS)


def _loads_snapshot(raw: Any) -> dict[str, Any]:
    if not isinstance(raw, str) or not raw.strip():
        return {}
    try:
        value = json.loads(raw)
    except (ValueError, TypeError):
        return {}
    return value if isinstance(value, dict) else {}


# --------------------------------------------------------------------------- #
# 单实体写入计划
# --------------------------------------------------------------------------- #
def _plan_entity(
    *,
    entity: Any,
    facts: list[dict],
    project_id: str,
    facts_by_field: dict[str, int],
    failed: bool = False,
) -> dict[str, Any]:
    """按逐字段策略产出写入计划（不改对象）。"""
    kind = "organization" if entity.is_organization else "person"
    validate_kind = "organization" if entity.is_organization else "character"
    new_fields = render_profile(facts, kind=validate_kind) if facts else {}
    snapshot = _loads_snapshot(entity.ai_profile_snapshot)

    fields: list[dict[str, Any]] = []
    for field in FACT_FIELDS:
        old_text = getattr(entity, field, None) or ""
        new_text = (new_fields.get(field) or "").strip()

        if failed and not facts:
            # 该实体所有调用都失败：没有可用的新文本，单独标 failed 而不是伪装成「正文无依据」
            action = ACTION_FAILED
            new_text = ""
        elif not new_text:
            # 1) 新文本为空：永远不用空值清空老内容
            action = ACTION_SKIPPED_EMPTY
        elif field in snapshot and old_text != snapshot[field]:
            # 2) 快照存在且当前值 != 快照值 → 用户手改过，保留现值
            action = ACTION_PRESERVED
        elif old_text.strip() == new_text:
            action = ACTION_UNCHANGED
        else:
            # 3) 其余（含无快照的历史数据）→ 覆盖
            action = ACTION_UPDATED

        fields.append(
            {
                "project_id": project_id,
                "name": entity.name,
                "kind": kind,
                "field": field,
                "action": action,
                "old_text": old_text,
                "new_text": new_text,
                "facts_in_window": facts_by_field.get(field, 0),
            }
        )

    for row in fields:
        if row["action"] == ACTION_UPDATED:
            snapshot[row["field"]] = row["new_text"]

    return {
        "entity_id": entity.id,
        "name": entity.name,
        "kind": kind,
        "fields": fields,
        "facts": facts,
        "snapshot": snapshot,
        "changed": any(row["action"] == ACTION_UPDATED for row in fields),
    }


async def _write_entity(db: AsyncSession, plan: dict[str, Any]) -> None:
    """真正写库（只在 --apply 下调用）：ORM 对象 + 字段覆盖 + 溯源 + 快照。"""
    orm = await db.get(Character, plan["entity_id"])
    if orm is None:
        return
    for row in plan["fields"]:
        if row["action"] == ACTION_UPDATED:
            setattr(orm, row["field"], row["new_text"])
    if plan["facts"]:
        merged = merge_profile_facts(orm.profile_facts, plan["facts"])
        if merged:
            orm.profile_facts = merged
            orm.profile_facts_updated_at = datetime.utcnow()
    if plan["changed"] and plan["snapshot"]:
        orm.ai_profile_snapshot = json.dumps(plan["snapshot"], ensure_ascii=False)


# --------------------------------------------------------------------------- #
# 单项目
# --------------------------------------------------------------------------- #
async def _run_project(
    *,
    db: AsyncSession,
    project: Any,
    ai_service: Any,
    args: argparse.Namespace,
    budget: dict[str, int],
) -> dict[str, Any]:
    record: dict[str, Any] = {
        "project_id": project.id,
        "title": project.title,
        "entity_total": 0,
        "skipped_existing": 0,
        "skipped_not_empty": 0,
        "skipped_limit": 0,
        "processed": 0,
        "calls": 0,
        "failures": 0,
        "violent_loss_blocked": 0,
        "fields": [],
        "error": "",
    }

    chapters = await sweep._load_chapters(db, project.id)
    rows = await _load_entity_rows(db, project.id)
    record["entity_total"] = len(rows)
    if not chapters or not rows:
        record["error"] = "skip: 没有正文非空的章节或没有实体"
        return record

    targets: list[Any] = []
    for entity in rows:
        if (entity.profile_facts or "").strip():
            # 幂等：已有溯源事实的实体不再回填（避免重复累加）
            record["skipped_existing"] += 1
            continue
        if args.only_empty and not _is_blank_entity(entity):
            record["skipped_not_empty"] += 1
            continue
        if budget["remaining"] is not None and budget["remaining"] <= 0:
            record["skipped_limit"] += 1
            continue
        targets.append(entity)
        if budget["remaining"] is not None:
            budget["remaining"] -= 1

    record["processed"] = len(targets)
    if not targets:
        return record

    windows = sweep._split_windows(chapters, args.chapters_per_window)
    slots: dict[str, dict[str, Any]] = {}
    started = time.perf_counter()

    for window_chapters in windows:
        window_text = "".join(c.content or "" for c in window_chapters)
        window_entities = [e for e in targets if (e.name or "").strip() and e.name in window_text]
        if not window_entities:
            continue
        chapters_by_number = {c.chapter_number: (c.content or "") for c in window_chapters}

        for start in range(0, len(window_entities), args.entities_per_call):
            batch = window_entities[start : start + args.entities_per_call]
            call = await sweep._call_batch(
                ai_service=ai_service,
                entities=batch,
                chapters=window_chapters,
                build_prompt=sweep._build_backfill_prompt,
                system_prompt=PromptService.PROFILE_BACKFILL_SYSTEM,
            )
            record["calls"] += 1

            if call["error"]:
                record["failures"] += 1
                for entity in batch:
                    slot = _slot(slots, entity.name)
                    slot["failed"] += 1
                    slot["error"] = call["error"]
                continue

            matched, _unmatched = sweep._entry_map(call["entries"], batch)
            for entity in batch:
                slot = _slot(slots, entity.name)
                entry = matched.get(entity.name)
                parsed = parse_profile_payload(entry) if entry is not None else None
                if parsed is None or parsed.mode != "facts":
                    continue
                kind = "organization" if entity.is_organization else "character"
                result = validate_profile_facts(
                    {"name": parsed.entity, "facts": parsed.facts},
                    chapters_by_number,
                    kind=kind,
                )
                slot["stats"].update(result.stats)
                if result.facts:
                    slot["facts"] = merge_profile_facts(slot["facts"], result.facts)

    for entity in targets:
        slot = slots.get(entity.name) or _slot(slots, entity.name)
        facts = json.loads(slot["facts"]) if slot["facts"] else []
        plan = _plan_entity(
            entity=entity,
            facts=facts,
            project_id=project.id,
            facts_by_field=dict(Counter(fact["field"] for fact in facts)),
            failed=bool(slot["failed"]) and not facts,
        )
        plan["failed"] = slot["failed"]
        plan["error"] = slot["error"]
        record["fields"].extend(plan["fields"])
        record["violent_loss_blocked"] += sum(
            1
            for row in plan["fields"]
            if row["action"] == ACTION_SKIPPED_EMPTY and row["old_text"].strip()
        )
        if args.apply and (plan["changed"] or plan["facts"]):
            await _write_entity(db, plan)

    record["seconds"] = round(time.perf_counter() - started, 1)
    return record


def _slot(slots: dict[str, dict[str, Any]], name: str) -> dict[str, Any]:
    return slots.setdefault(name, {"facts": None, "stats": Counter(), "failed": 0, "error": ""})


# --------------------------------------------------------------------------- #
# 输出
# --------------------------------------------------------------------------- #
def _write_plan(path: Path, projects: list[dict[str, Any]]) -> int:
    header = (
        "project_id\tname\tkind\tfield\taction\told_text\tnew_text\tfacts_in_window"
    )
    lines = [header]
    for project in projects:
        for row in project["fields"]:
            lines.append(
                "\t".join(
                    [
                        project["project_id"],
                        sweep._tsv_field(row["name"]),
                        row["kind"],
                        row["field"],
                        row["action"],
                        sweep._tsv_field(row["old_text"]),
                        sweep._tsv_field(row["new_text"]),
                        str(row["facts_in_window"]),
                    ]
                )
            )
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return len(lines) - 1


def _counts(projects: list[dict[str, Any]]) -> dict[str, Any]:
    actions: Counter = Counter()
    to_update = 0
    fields_total = 0
    for project in projects:
        updated_names: set[str] = set()
        for row in project["fields"]:
            actions[row["action"]] += 1
            fields_total += 1
            if row["action"] == ACTION_UPDATED:
                updated_names.add(row["name"])
        to_update += len(updated_names)
    return {
        "actions": dict(actions),
        "to_update": to_update,
        "fields_total": fields_total,
    }


def _write_report(
    path: Path,
    *,
    args: argparse.Namespace,
    projects: list[dict[str, Any]],
    started_at: str,
    seconds: float,
) -> dict[str, Any]:
    counts = _counts(projects)
    actions = counts["actions"]
    processed = sum(project["processed"] for project in projects)
    skipped_existing = sum(project["skipped_existing"] for project in projects)
    skipped_not_empty = sum(project["skipped_not_empty"] for project in projects)
    skipped_limit = sum(project["skipped_limit"] for project in projects)
    failures = sum(project["failures"] for project in projects)
    blocked = sum(project["violent_loss_blocked"] for project in projects)

    lines = ["# 档案回填（profile_facts）", ""]
    lines.append(f"生成时间：{started_at}")
    lines.append("")
    lines.append(
        "> **本次运行 = dry-run（未写库）**：未加 `--apply`，全程只有 SELECT，结束统一 `rollback()`。"
        if not args.apply
        else "> **本次运行 = `--apply`：已按逐项目 `commit()` 写库。**"
    )
    lines.append("")
    lines.append("## 运行参数")
    lines.append("")
    lines.append(
        sweep._md_table(
            ["参数", "值"],
            [
                ["user_id", args.user_id],
                ["project_id", ",".join(args.project_id) if args.project_id else "（全部项目）"],
                ["chapters_per_window", args.chapters_per_window],
                ["entities_per_call", args.entities_per_call],
                ["only_empty", args.only_empty],
                ["limit", args.limit or "不限"],
                ["apply", args.apply],
                ["prompt", "`PROFILE_BACKFILL_FACTS` + `PROFILE_BACKFILL_SYSTEM`"],
                ["out_dir", args.out_dir],
            ],
        )
    )
    lines.append("")
    lines.append("## 总览")
    lines.append("")
    lines.append(
        sweep._md_table(
            ["指标", "值"],
            [
                ["项目数", len(projects)],
                ["实体数（本次处理）", processed],
                ["跳过：已有 profile_facts（幂等）", skipped_existing],
                ["跳过：非全空（--only-empty）", skipped_not_empty],
                ["跳过：--limit 截断", skipped_limit],
                ["需要更新实体数（≥1 字段 updated）", counts["to_update"]],
                ["字段：updated", actions.get(ACTION_UPDATED, 0)],
                ["字段：preserved_user_edit（用户手改，保留）", actions.get(ACTION_PRESERVED, 0)],
                ["字段：skipped_empty（新文本空，未清空老内容）", actions.get(ACTION_SKIPPED_EMPTY, 0)],
                ["字段：unchanged（新旧相同）", actions.get(ACTION_UNCHANGED, 0)],
                ["字段：failed（调用失败，无可用新文本）", actions.get(ACTION_FAILED, 0)],
                ["其中「老内容本会被清空、已被拦住」", blocked],
                ["字段总数", counts["fields_total"]],
                ["失败批次数", failures],
                ["耗时（秒）", round(seconds, 1)],
                ["是否写库", "是（--apply）" if args.apply else "**否（dry-run）**"],
            ],
        )
    )
    lines.append("")
    lines.append("## 逐项目分类计数")
    lines.append("")
    rows = []
    for project in projects:
        per_action: Counter = Counter(row["action"] for row in project["fields"])
        rows.append(
            [
                f"`{project['project_id']}`",
                project["entity_total"],
                project["skipped_existing"],
                project["skipped_not_empty"],
                project["processed"],
                per_action.get(ACTION_UPDATED, 0),
                per_action.get(ACTION_PRESERVED, 0),
                per_action.get(ACTION_SKIPPED_EMPTY, 0),
                per_action.get(ACTION_UNCHANGED, 0),
                per_action.get(ACTION_FAILED, 0),
                project["calls"],
                project["failures"],
                project.get("seconds", 0.0),
            ]
        )
    lines.append(
        sweep._md_table(
            [
                "project_id",
                "实体总数",
                "跳过(已有facts)",
                "跳过(非空)",
                "处理实体",
                "updated",
                "preserved",
                "skipped_empty",
                "unchanged",
                "failed",
                "调用数",
                "失败批次",
                "耗时(s)",
            ],
            rows,
        )
    )
    lines.append("")
    errors = [project for project in projects if project.get("error")]
    if errors:
        lines.append("项目级提示：")
        for project in errors:
            lines.append(f"- `{project['project_id']}`：{project['error']}")
        lines.append("")
    path.write_text("\n".join(lines), encoding="utf-8")
    return {
        "processed": processed,
        "to_update": counts["to_update"],
        "preserved": actions.get(ACTION_PRESERVED, 0),
        "skipped_empty": actions.get(ACTION_SKIPPED_EMPTY, 0),
    }


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
    budget = {"remaining": (args.limit or None)}
    ai_error = ""

    engine = await get_engine(args.user_id)
    session_factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

    async with session_factory() as db:
        try:
            ai_service = None
            if await sweep._user_settings_exists(db, args.user_id):
                ai_service = await sweep.BookImportService()._build_user_ai_service(
                    db=db, user_id=args.user_id
                )
            else:
                ai_error = (
                    f"用户 {args.user_id} 没有 Settings 记录；为保证不写库，脚本不会让 "
                    "_build_user_ai_service 走 insert 分支。"
                )

            for project in await sweep._load_projects(db, args.project_id):
                if ai_service is None:
                    failed_projects.append({"project_id": project.id, "error": ai_error})
                    continue
                try:
                    record = await _run_project(
                        db=db,
                        project=project,
                        ai_service=ai_service,
                        args=args,
                        budget=budget,
                    )
                    projects.append(record)
                    print(
                        f"[project] {project.id} entities={record['processed']} "
                        f"calls={record['calls']} failures={record['failures']} "
                        f"skipped_existing={record['skipped_existing']}",
                        flush=True,
                    )
                    if args.apply:
                        await db.commit()
                except Exception as exc:  # 项目级失败不中断整体回填
                    await db.rollback()
                    failed_projects.append(
                        {
                            "project_id": project.id,
                            "error": f"{type(exc).__name__}: {exc}",
                            "traceback": traceback.format_exc(),
                        }
                    )
                    print(f"[project-failed] {project.id}: {type(exc).__name__}: {exc}", file=sys.stderr)
        finally:
            # dry-run 只读承诺：无论成功失败都不提交，统一回滚（--apply 时回滚已提交后的空事务）
            try:
                await db.rollback()
            except Exception:
                pass

    plan_rows = _write_plan(out_dir / "plan.tsv", projects)
    summary = _write_report(
        out_dir / "report.md",
        args=args,
        projects=projects,
        started_at=started_at,
        seconds=time.perf_counter() - run_started,
    )

    print(f"OUT {out_dir} plan_rows={plan_rows}")
    for item in failed_projects:
        print(f"PROJECT_FAILED {item['project_id']} {item['error']}", file=sys.stderr)
    print(
        f"SUMMARY entities={summary['processed']} to_update={summary['to_update']} "
        f"preserved={summary['preserved']} skipped_empty={summary['skipped_empty']} "
        f"apply={1 if args.apply else 0}"
    )

    if not projects:
        print("ERROR 没有可回填的项目", file=sys.stderr)
        return 1
    return 0


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    return asyncio.run(run(args))


if __name__ == "__main__":
    sys.exit(main())
