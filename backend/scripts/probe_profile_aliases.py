#!/usr/bin/env python3
"""只读探测：为 ``not_in_text`` 实体找候选别名（默认绝不写库）。

背景：两个离线脚本（``sweep_profiles_readonly`` / ``backfill_profiles``）判「实体是否
出现在正文里」时曾只用 ``name`` 精确匹配，正文只用简称/称谓的实体被误判成
``not_in_text``。生产同步链路早已用 ``name + aliases``；本脚本的作用是**给人看证据**：
列出每个 ``not_in_text`` 实体的候选别名、命中次数/命中章数、以及项目内唯一性判定，
由人决定写哪些进 ``characters.aliases``。

只读承诺（硬约束）：

- 默认全程只有 SELECT，结束时统一 ``rollback()``；不加 ``--apply`` 时**绝不写库**。
- ``--apply`` 只与 ``--accept`` 同时给才生效，且**只写 ``characters.aliases`` 一列**，
  不动 ``profile_facts`` 或三个档案字段。
- 不调用模型、不需要 ``--user-id``；输出文件之外不打印正文片段（只打印字面候选词）。

``--accept`` 的拒绝规则（任一不满足即拒绝并打印原因）：

1. 实体必须存在于本次读取的项目里，且在 ``not_in_text`` 集合里；
2. 别名必须按 ``unique_tokens_by_entity`` 的规则判定为 **unique**；
3. 写回后 ``aliases`` 的 JSON 串长度不得超过列的 500 字符上限。

用法::

    DATABASE_URL=... PYTHONPATH=backend \\
        backend/.venv/bin/python backend/scripts/probe_profile_aliases.py \\
        --out-dir /tmp/probe-aliases

    # 确认候选后落地（只写 aliases 列）：
    ... --out-dir /tmp/probe-aliases --accept '实体名=别名' --apply
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

# 与 backend/scripts/migrate.py 一致：允许直接 `python backend/scripts/<name>.py`
_project_root = Path(__file__).resolve().parents[1]
if str(_project_root) not in sys.path:
    sys.path.insert(0, str(_project_root))
# 复用同目录的只读取数实现（不是包，直接把脚本目录放进 sys.path）
_scripts_dir = Path(__file__).resolve().parent
if str(_scripts_dir) not in sys.path:
    sys.path.insert(0, str(_scripts_dir))

from sqlalchemy import select  # noqa: E402
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker  # noqa: E402

import sweep_profiles_readonly as sweep  # noqa: E402
from app.database import get_engine  # noqa: E402
from app.models import Character  # noqa: E402
from app.services.profile_mentions import (  # noqa: E402
    MIN_TOKEN_LEN,
    character_tokens,
    parse_aliases,
)

# ``characters.aliases`` 的列宽（String(500)）：写回前必须自己卡住，SQLite 不报错但会截断
ALIASES_MAX_CHARS = 500

SOURCE_EXISTING = "existing_alias"
SOURCE_SUBSTRING = "name_substring"

CONCLUSION_RESOLVED = "resolved"
CONCLUSION_SALVAGEABLE = "salvageable"
CONCLUSION_NO_BASIS = "no_text_basis"

VERDICT_UNIQUE = "unique"
VERDICT_AMBIGUOUS = "ambiguous"


# --------------------------------------------------------------------------- #
# 参数
# --------------------------------------------------------------------------- #
def _parse_accept(value: str) -> tuple[str, str]:
    if "=" not in value:
        raise argparse.ArgumentTypeError(f"格式应为 '实体名=别名'：{value!r}")
    name, alias = value.split("=", 1)
    name, alias = name.strip(), alias.strip()
    if not name or not alias:
        raise argparse.ArgumentTypeError(f"实体名与别名都不能为空：{value!r}")
    return name, alias


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="只读探测 not_in_text 实体的候选别名（默认绝不写库）",
    )
    parser.add_argument(
        "--project-id",
        action="append",
        default=None,
        help="项目 UUID，可重复；缺省 = 库里所有项目",
    )
    parser.add_argument("--out-dir", required=True, help="输出目录（candidates.tsv / report.md）")
    parser.add_argument(
        "--accept",
        action="append",
        type=_parse_accept,
        default=[],
        help="候选落地：'实体名=别名'，可重复；不给 --apply 时只打印将要做哪些写入",
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="真正写库（只写 characters.aliases）；必须与 --accept 同时给",
    )
    return parser.parse_args(argv)


# --------------------------------------------------------------------------- #
# 取数（只读）
# --------------------------------------------------------------------------- #
async def _load_rows(db: AsyncSession, project_id: str) -> list[Any]:
    """显式列 SELECT：只取探测需要的名字、别名、类型与"有没有溯源事实"。"""
    rows = await db.execute(
        select(
            Character.id,
            Character.name,
            Character.aliases,
            Character.is_organization,
            Character.profile_facts,
        )
        .where(Character.project_id == project_id)
        .order_by(Character.name)
    )
    return list(rows.all())


# --------------------------------------------------------------------------- #
# 候选与判定
# --------------------------------------------------------------------------- #
def _name_substrings(name: str) -> list[str]:
    """name 的全部连续子串（长度 >= MIN_TOKEN_LEN 且 != name），按字典序去重。"""
    parts: set[str] = set()
    for start in range(len(name)):
        for end in range(start + MIN_TOKEN_LEN, len(name) + 1):
            part = name[start:end]
            if part != name:
                parts.add(part)
    return sorted(parts)


def _count(text: str, chapter_texts: list[tuple[int, str]], token: str) -> tuple[int, int]:
    """返回 (全文命中次数, 命中章数)。"""
    if not token:
        return 0, 0
    hits = text.count(token)
    chapters = sum(1 for _, content in chapter_texts if token in content)
    return hits, chapters


def _verdict(token: str, entity_id: Any, tokens_by_entity: dict[Any, list[str]]) -> str:
    """唯一性判定，直接复用 ``unique_tokens_by_entity`` 的规则。

    只和**别的实体**的 token 比：别的实体声明同名 token、或与它互为子串 →
    ``ambiguous``。同实体内部的 name/alias 互相包含不算冲突（如 ``大乾朝廷`` 与
    ``朝廷`` 描述同一个实体），所以按名字子串提出的候选不会被自己挡住。
    """
    for other_id, other_tokens in tokens_by_entity.items():
        if other_id == entity_id:
            continue
        for other in other_tokens:
            if other == token or other in token or token in other:
                return VERDICT_AMBIGUOUS
    return VERDICT_UNIQUE


def _probe_entity(
    *,
    project_id: str,
    row: Any,
    full_text: str,
    chapter_texts: list[tuple[int, str]],
    tokens_by_entity: dict[Any, list[str]],
) -> dict[str, Any]:
    name = (row.name or "").strip()
    aliases = parse_aliases(row.aliases)
    entity_id = row.id

    candidates: list[dict[str, Any]] = []
    seen: set[str] = set()
    for alias in aliases:
        hits, chapters = _count(full_text, chapter_texts, alias)
        candidates.append(
            {
                "candidate": alias,
                "source": SOURCE_EXISTING,
                "hits": hits,
                "chapters": chapters,
                "verdict": _verdict(alias, entity_id, tokens_by_entity),
            }
        )
        seen.add(alias)

    for part in _name_substrings(name):
        if part in seen:
            continue
        hits, chapters = _count(full_text, chapter_texts, part)
        # 全文 0 命中的子串救不回任何东西，只会把表撑满
        if hits <= 0:
            continue
        candidates.append(
            {
                "candidate": part,
                "source": SOURCE_SUBSTRING,
                "hits": hits,
                "chapters": chapters,
                "verdict": _verdict(part, entity_id, tokens_by_entity),
            }
        )

    # 命中次数降序；同次数按来源/字面稳定排序，方便人工比对
    candidates.sort(key=lambda item: (-item["hits"], item["source"], item["candidate"]))

    # 三个分类互斥，优先级 resolved > salvageable > no_text_basis：
    # - resolved：现有别名已经能救（库里有、唯一、正文有命中），不需要再落地任何东西；
    # - salvageable：入参候选（name 子串）能救，但要 --accept 落地才生效；
    # - no_text_basis：都没有，正文确实零支撑。
    resolved = any(
        item["source"] == SOURCE_EXISTING
        and item["verdict"] == VERDICT_UNIQUE
        and item["hits"] > 0
        for item in candidates
    )
    salvageable = any(
        item["source"] == SOURCE_SUBSTRING
        and item["verdict"] == VERDICT_UNIQUE
        and item["hits"] > 0
        for item in candidates
    )
    if resolved:
        conclusion = CONCLUSION_RESOLVED
    elif salvageable:
        conclusion = CONCLUSION_SALVAGEABLE
    else:
        conclusion = CONCLUSION_NO_BASIS
    return {
        "project_id": project_id,
        "entity_id": entity_id,
        "name": name,
        "kind": "organization" if row.is_organization else "person",
        "aliases": aliases,
        "candidates": candidates,
        "conclusion": conclusion,
        # 只给 --accept 做唯一性判定用，不写进 tsv/report
        "tokens_by_entity": tokens_by_entity,
    }


async def _probe_project(db: AsyncSession, project: Any) -> dict[str, Any]:
    chapters = await sweep._load_chapters(db, project.id)
    rows = await _load_rows(db, project.id)
    full_text = "".join(chapter.content or "" for chapter in chapters)
    chapter_texts = [(chapter.chapter_number, chapter.content or "") for chapter in chapters]

    # character_tokens 是"实体声明的全部 token"；唯一性判定要与其他实体逐个比
    tokens_by_entity = {row.id: character_tokens(row) for row in rows}

    entities: list[dict[str, Any]] = []
    for row in rows:
        name = (row.name or "").strip()
        if not name:
            continue
        # not_in_text 判定与离线脚本改造前一致：name 在项目全文 0 次命中，且无溯源事实
        if full_text.count(name) > 0:
            continue
        if (row.profile_facts or "").strip():
            continue
        entities.append(
            _probe_entity(
                project_id=project.id,
                row=row,
                full_text=full_text,
                chapter_texts=chapter_texts,
                tokens_by_entity=tokens_by_entity,
            )
        )

    return {
        "project_id": project.id,
        "title": project.title,
        "chapter_count": len(chapters),
        "entity_count": len(rows),
        "not_in_text": entities,
    }


# --------------------------------------------------------------------------- #
# 输出
# --------------------------------------------------------------------------- #
def _write_candidates(path: Path, projects: list[dict[str, Any]]) -> int:
    lines = ["project_id\tname\tkind\tcandidate\tsource\thits\tchapters\tverdict"]
    for project in projects:
        for entity in project["not_in_text"]:
            for item in entity["candidates"]:
                lines.append(
                    "\t".join(
                        [
                            project["project_id"],
                            sweep._tsv_field(entity["name"]),
                            entity["kind"],
                            sweep._tsv_field(item["candidate"]),
                            item["source"],
                            str(item["hits"]),
                            str(item["chapters"]),
                            item["verdict"],
                        ]
                    )
                )
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return len(lines) - 1


def _write_report(
    path: Path,
    *,
    args: argparse.Namespace,
    projects: list[dict[str, Any]],
    writes: list[dict[str, Any]],
    started_at: str,
) -> dict[str, int]:
    totals = {
        "not_in_text": 0,
        CONCLUSION_RESOLVED: 0,
        CONCLUSION_SALVAGEABLE: 0,
        CONCLUSION_NO_BASIS: 0,
    }
    summary_rows = []
    for project in projects:
        entities = project["not_in_text"]
        resolved = sum(1 for e in entities if e["conclusion"] == CONCLUSION_RESOLVED)
        salvageable = sum(1 for e in entities if e["conclusion"] == CONCLUSION_SALVAGEABLE)
        no_basis = len(entities) - resolved - salvageable
        totals["not_in_text"] += len(entities)
        totals[CONCLUSION_RESOLVED] += resolved
        totals[CONCLUSION_SALVAGEABLE] += salvageable
        totals[CONCLUSION_NO_BASIS] += no_basis
        summary_rows.append(
            [
                f"`{project['project_id']}`",
                project["entity_count"],
                len(entities),
                resolved,
                salvageable,
                no_basis,
            ]
        )

    lines = ["# 只读探测：not_in_text 实体的候选别名", ""]
    lines.append(f"生成时间：{started_at}")
    lines.append("")
    lines.append(
        "> 本脚本默认只读：全程只有 SELECT，结束统一 `rollback()`。"
        + (
            " 本次加了 `--apply`，只写 `characters.aliases`（不动 `profile_facts` 与档案字段）。"
            if args.apply and args.accept
            else " 未加 `--apply`，`characters` 表未被修改。"
        )
    )
    lines.append("")
    lines.append(
        "> **`not_in_text` 是主名口径**（`name` 在项目全文 0 命中且无 `profile_facts`），"
        "别名已经能救回的实体仍会出现在这里，请对照 `resolved` 列"
        "（现有别名唯一且有命中 → 匹配脚本已经能用，无需再落地任何东西）。"
        "`salvageable` = 需要 `--accept` 落地候选别名；`no_text_basis` = 正文零支撑。"
    )
    lines.append("")
    lines.append("## 运行参数")
    lines.append("")
    lines.append(
        sweep._md_table(
            ["参数", "值"],
            [
                ["project_id", ",".join(args.project_id) if args.project_id else "（全部项目）"],
                ["out_dir", args.out_dir],
                ["accept", len(args.accept)],
                ["apply", bool(args.apply and args.accept)],
                ["MIN_TOKEN_LEN", MIN_TOKEN_LEN],
            ],
        )
    )
    lines.append("")
    lines.append("## 每项目汇总")
    lines.append("")
    lines.append(
        sweep._md_table(
            [
                "project_id",
                "实体数",
                "not_in_text",
                "resolved",
                "salvageable",
                "no_text_basis",
            ],
            summary_rows or [["-", 0, 0, 0, 0, 0]],
        )
    )
    lines.append("")
    lines.append(
        f"合计：not_in_text {totals['not_in_text']}，"
        f"resolved {totals[CONCLUSION_RESOLVED]}，"
        f"salvageable {totals[CONCLUSION_SALVAGEABLE]}，"
        f"no_text_basis {totals[CONCLUSION_NO_BASIS]}"
    )
    lines.append("")
    lines.append("## 逐实体候选")
    lines.append("")
    for project in projects:
        if not project["not_in_text"]:
            continue
        lines.append(f"### `{project['project_id']}`")
        lines.append("")
        for entity in project["not_in_text"]:
            existing = "、".join(entity["aliases"]) if entity["aliases"] else "（无）"
            lines.append(
                f"- **{entity['name']}**（{entity['kind']}，现有别名：{existing}）"
                f" → `{entity['conclusion']}`"
            )
            if not entity["candidates"]:
                lines.append("  - （无命中 > 0 的候选）")
                continue
            lines.append("")
            lines.append(
                sweep._md_table(
                    ["候选", "来源", "命中次数", "命中章数", "唯一性"],
                    [
                        [item["candidate"], item["source"], item["hits"], item["chapters"], item["verdict"]]
                        for item in entity["candidates"]
                    ],
                )
            )
            lines.append("")
        lines.append("")

    if args.accept:
        lines.append("## 候选落地（--accept）")
        lines.append("")
        lines.append(
            sweep._md_table(
                ["实体", "别名", "结果", "说明"],
                [
                    [w["name"], w["alias"], w["result"], w.get("reason", "")]
                    for w in writes
                ]
                or [["-", "-", "-", ""]],
            )
        )
        lines.append("")
    path.write_text("\n".join(lines), encoding="utf-8")
    return totals


# --------------------------------------------------------------------------- #
# 写库（只在 --apply + --accept 下）
# --------------------------------------------------------------------------- #
def _accepted_targets(
    projects: list[dict[str, Any]], accepts: list[tuple[str, str]]
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """把 ``--accept`` 解析成待写目标；不合规的项带原因返回（绝不猜）。"""
    index: dict[str, list[dict[str, Any]]] = {}
    for project in projects:
        for entity in project["not_in_text"]:
            index.setdefault(entity["name"], []).append(entity)

    targets: list[dict[str, Any]] = []
    rejected: list[dict[str, Any]] = []

    def _reject(name: str, alias: str, reason: str) -> None:
        rejected.append({"name": name, "alias": alias, "result": "rejected", "reason": reason})
        print(f"{name}: 拒绝写入别名 {alias}（{reason}）", file=sys.stderr)

    for name, alias in accepts:
        matches = index.get(name) or []
        if not matches:
            _reject(name, alias, "不在 not_in_text 集合里（name 在正文有命中或已有 profile_facts）")
            continue
        if len(matches) > 1:
            _reject(name, alias, "同名实体出现在多个项目的 not_in_text 集合里，无法确定写哪个")
            continue
        entity = matches[0]
        if len(alias) < MIN_TOKEN_LEN:
            _reject(name, alias, f"别名短于 MIN_TOKEN_LEN={MIN_TOKEN_LEN}，写了也不会被匹配")
            continue
        # 唯一性判定用实体所在项目的 token 规则（与 unique_tokens_by_entity 同口径）
        if _verdict(alias, entity["entity_id"], entity["tokens_by_entity"]) != VERDICT_UNIQUE:
            _reject(name, alias, "别名不是项目内唯一（被别的实体声明或与别的实体 token 互为子串）")
            continue
        targets.append({"name": name, "alias": alias, "entity_id": entity["entity_id"]})
    return targets, rejected


async def _apply_targets(
    db: AsyncSession, targets: list[dict[str, Any]], apply: bool
) -> list[dict[str, Any]]:
    """逐条打印 ``name: aliases [old] -> [new]``；``apply`` 为真时写库并提交。"""
    results: list[dict[str, Any]] = []
    written = False
    for target in targets:
        orm = await db.get(Character, target["entity_id"])
        if orm is None:
            results.append({**target, "result": "rejected", "reason": "实体已不存在"})
            print(f"{target['name']}: 实体已不存在，跳过", file=sys.stderr)
            continue
        old_aliases = parse_aliases(orm.aliases)
        if target["alias"] in old_aliases:
            results.append({**target, "result": "skipped_exists", "reason": "别名已存在"})
            print(f"{target['name']}: 别名 {target['alias']} 已存在，跳过")
            continue
        new_aliases = old_aliases + [target["alias"]]
        payload = json.dumps(new_aliases, ensure_ascii=False)
        print(f"{target['name']}: aliases {old_aliases} -> {new_aliases}")
        if len(payload) > ALIASES_MAX_CHARS:
            results.append(
                {
                    **target,
                    "result": "rejected",
                    "reason": f"写回后 aliases 长度 {len(payload)} > {ALIASES_MAX_CHARS}",
                }
            )
            print(
                f"{target['name']}: 拒绝写入（aliases 长度 {len(payload)} 超过列宽 {ALIASES_MAX_CHARS}）",
                file=sys.stderr,
            )
            continue
        if not apply:
            results.append({**target, "result": "planned", "reason": "未加 --apply，仅打印"})
            continue
        orm.aliases = payload
        written = True
        results.append({**target, "result": "written", "reason": ""})
        print(f"{target['name']}: aliases {old_aliases} -> {new_aliases}（已写入）")
    if written:
        await db.commit()
        print("COMMITTED characters.aliases")
    return results


# --------------------------------------------------------------------------- #
# 主流程
# --------------------------------------------------------------------------- #
async def run(args: argparse.Namespace) -> int:
    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    started_at = datetime.now().isoformat(timespec="seconds")

    engine = await get_engine("probe-profile-aliases")
    session_factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

    projects: list[dict[str, Any]] = []
    writes: list[dict[str, Any]] = []

    async with session_factory() as db:
        try:
            for project in await sweep._load_projects(db, args.project_id):
                record = await _probe_project(db, project)
                # 唯一性判定结果不进输出文件，但要留给 --accept 用
                projects.append(record)
        finally:
            # 只读承诺：读取阶段无论成功失败都不提交，统一回滚
            try:
                await db.rollback()
            except Exception:
                pass

        if args.accept:
            targets, rejected = _accepted_targets(projects, args.accept)
            writes.extend(rejected)
            writes.extend(await _apply_targets(db, targets, args.apply))
        else:
            writes = []

    total_rows = _write_candidates(out_dir / "candidates.tsv", projects)
    totals = _write_report(
        out_dir / "report.md",
        args=args,
        projects=projects,
        writes=writes,
        started_at=started_at,
    )

    print(f"OUT {out_dir} candidate_rows={total_rows}")
    for project in projects:
        entities = project["not_in_text"]
        resolved = sum(1 for e in entities if e["conclusion"] == CONCLUSION_RESOLVED)
        salvageable = sum(1 for e in entities if e["conclusion"] == CONCLUSION_SALVAGEABLE)
        print(
            f"[project] {project['project_id']} entities={project['entity_count']} "
            f"not_in_text={len(entities)} resolved={resolved} salvageable={salvageable} "
            f"no_text_basis={len(entities) - resolved - salvageable}"
        )
    for project in projects:
        for entity in project["not_in_text"]:
            unique_candidates = sum(
                1
                for item in entity["candidates"]
                if item["source"] == SOURCE_SUBSTRING
                and item["verdict"] == VERDICT_UNIQUE
                and item["hits"] > 0
            )
            unique_aliases = sum(
                1
                for item in entity["candidates"]
                if item["source"] == SOURCE_EXISTING
                and item["verdict"] == VERDICT_UNIQUE
                and item["hits"] > 0
            )
            print(
                f"[not_in_text] {entity['name']} kind={entity['kind']} "
                f"conclusion={entity['conclusion']} unique_aliases={unique_aliases} "
                f"unique_candidates={unique_candidates} "
                f"existing_aliases={len(entity['aliases'])}"
            )
    print(
        f"SUMMARY not_in_text={totals['not_in_text']} "
        f"resolved={totals[CONCLUSION_RESOLVED]} "
        f"salvageable={totals[CONCLUSION_SALVAGEABLE]} "
        f"no_text_basis={totals[CONCLUSION_NO_BASIS]} apply={1 if args.apply and args.accept else 0}"
    )

    if not projects:
        print("ERROR 没有可探测的项目（--project-id 未匹配任何项目）", file=sys.stderr)
        return 1
    return 0


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    return asyncio.run(run(args))


if __name__ == "__main__":
    sys.exit(main())
