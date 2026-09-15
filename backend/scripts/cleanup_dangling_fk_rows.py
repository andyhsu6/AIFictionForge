#!/usr/bin/env python3
"""清理 SQLite 中的悬空外键行（事务性、幂等、默认 dry-run）。

背景：应用未启用 `PRAGMA foreign_keys=ON`，模型里声明的 CASCADE/SET NULL
在运行时不会生效，历史删除操作可能留下悬空外键行（`PRAGMA foreign_key_check`
可检出）。本脚本按固定策略清理这些行：

- 整行删除：character_relationship_type_links / story_memories / plot_analysis /
  analysis_tasks / regeneration_tasks / project_default_styles /
  relationship_types（project_id 悬空）/ organization_members（有条件的，
  见 plan_actions 注释）；
- 置空外键（对应声明的 SET NULL）：chapters.outline_id 指向已删除大纲；
  generation_history.chapter_id 指向已删除章节；
  organizations.parent_org_id 指向已删除父组织（自引用）；
  character_relationships.relationship_type_id 指向将删除的孤立关系类型
  （该列可空且未声明 ondelete，必须先置空再删类型，否则在
  `PRAGMA foreign_keys=ON` 下整个 `--apply` 事务会被 FK 错误中止）；
- 其余表（含 organizations 本身与 agent_* 表）一律不动：organizations 行永不删除，
  组织自身悬空时其成员关系只报告不删除，交人工判断。

默认 dry-run（只打印计划，不写入）；`--apply` 才写入，且整个写入在单个事务内
完成，运行前后各打印一次 `PRAGMA foreign_key_check` 汇总。

用法（从项目根目录或 backend/ 目录运行均可）：
    python backend/scripts/cleanup_dangling_fk_rows.py            # dry-run
    python backend/scripts/cleanup_dangling_fk_rows.py --apply
    python backend/scripts/cleanup_dangling_fk_rows.py --db /path/to.db --apply
"""
import argparse
import sqlite3
import sys
from collections import Counter, defaultdict
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parents[1]
DEFAULT_DB_PATH = BACKEND_DIR / "data" / "mumuai_novel.db"

# 整行删除策略：这些表的悬空行直接删除。
# - relationship_types：project_id 悬空 = 所属项目已不存在，项目级定义无法再被
#   任何项目读取（系统预置的 project_id 为 NULL，永不悬空），属明确垃圾。
# - organization_members：仅在“缺失父行是 characters”时删除（成员角色已被删除、
#   组织仍在，成员关系无意义）。若缺失父行是 organizations，或该成员所属组织自身
#   悬空，则转 unhandled 交人工判断 —— organizations 行永不删除，不能因为组织缺失
#   就静默抹掉成员证据。该条件在 plan_actions 中实现。
DELETE_TABLES = frozenset(
    {
        "character_relationship_type_links",
        "story_memories",
        "plot_analysis",
        "analysis_tasks",
        "regeneration_tasks",
        "project_default_styles",
        "relationship_types",
        "organization_members",
    }
)

# 置空外键策略：(子表, 父表) -> (子表, 外键列)。处理 chapters.outline_id、
# generation_history.chapter_id，以及 organizations 的自引用 parent_org_id
# （声明的 SET NULL：父组织已删除时子组织保留、仅断开指针）；chapters 与
# organizations 行永不删除。
NULL_POLICY = {
    ("chapters", "outlines"): ("chapters", "outline_id"),
    ("generation_history", "chapters"): ("generation_history", "chapter_id"),
    ("organizations", "organizations"): ("organizations", "parent_org_id"),
}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="清理 SQLite 悬空外键行（默认 dry-run，--apply 才写入）"
    )
    parser.add_argument(
        "--db",
        default=str(DEFAULT_DB_PATH),
        help=f"SQLite 数据库路径（默认 {DEFAULT_DB_PATH}）",
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="实际写入清理结果（缺省仅打印计划）",
    )
    return parser


def collect_dangling(conn: sqlite3.Connection) -> list[tuple]:
    """返回 PRAGMA foreign_key_check 的原始行：(table, rowid, parent, fkid)。"""
    return conn.execute("PRAGMA foreign_key_check").fetchall()


def _dangling_organization_ids(conn: sqlite3.Connection, rows: list[tuple]) -> set[str]:
    """返回 foreign_key_check 中自身悬空的 organizations 行的业务 id。"""
    rowids = [rowid for table, rowid, _parent, _fkid in rows if table == "organizations"]
    if not rowids:
        return set()
    placeholders = ",".join("?" * len(rowids))
    return {
        row[0]
        for row in conn.execute(
            f'SELECT id FROM organizations WHERE rowid IN ({placeholders})',
            tuple(rowids),
        )
    }


def _member_rowids_of_organizations(
    conn: sqlite3.Connection, organization_ids: set[str]
) -> set:
    """返回属于给定组织的 organization_members 行 rowid（用于保守地转 unhandled）。"""
    if not organization_ids:
        return set()
    placeholders = ",".join("?" * len(organization_ids))
    return {
        row[0]
        for row in conn.execute(
            f'SELECT rowid FROM organization_members WHERE organization_id IN ({placeholders})',
            tuple(organization_ids),
        )
    }


def _dangling_relationship_type_ids(
    conn: sqlite3.Connection, rowids: set
) -> set[int]:
    """把 relationship_types 的 rowid 集合解析为业务 id。"""
    if not rowids:
        return set()
    placeholders = ",".join("?" * len(rowids))
    return {
        row[0]
        for row in conn.execute(
            f"SELECT id FROM relationship_types WHERE rowid IN ({placeholders})",
            tuple(rowids),
        )
    }


def _relationship_rowids_using_type_ids(
    conn: sqlite3.Connection, type_ids: set[int]
) -> set:
    """返回 relationship_type_id 命中给定类型 id 的 character_relationships rowid。"""
    if not type_ids:
        return set()
    placeholders = ",".join("?" * len(type_ids))
    return {
        row[0]
        for row in conn.execute(
            f"SELECT rowid FROM character_relationships WHERE relationship_type_id IN ({placeholders})",
            tuple(type_ids),
        )
    }


def plan_actions(conn: sqlite3.Connection, rows: list[tuple]) -> tuple[dict, dict, Counter]:
    """把悬空行分组为删除计划、置空计划与忽略计数（不触碰策略外内容）。

    `organization_members` 的删除是条件式的：只有“缺失父行包含 characters 且
    所属组织未自身悬空”才视为明确垃圾；组织自身悬空、或仅缺失 organizations
    的行一律计入 unhandled 保留。注意一行可能在 `foreign_key_check` 中出现多次
    （同时悬空于 organizations 与 characters），本函数按 rowid 归并，保证每行
    只进一个桶（此前同一行会既计入 unhandled 又被删除，属报告不一致）。
    """
    deletes: dict[str, set] = defaultdict(set)
    nulls: dict[tuple, set] = defaultdict(set)
    unhandled: Counter = Counter()
    guarded_member_rowids = _member_rowids_of_organizations(
        conn, _dangling_organization_ids(conn, rows)
    )
    member_parents: dict = defaultdict(set)
    for table, rowid, parent, _fkid in rows:
        if table == "organization_members":
            member_parents[rowid].add(parent)
        elif table in DELETE_TABLES:
            deletes[table].add(rowid)
        elif (table, parent) in NULL_POLICY:
            nulls[NULL_POLICY[(table, parent)]].add(rowid)
        else:
            unhandled[table] += 1

    # 被“存在但自身悬空的组织”保护的行，即使未出现在 foreign_key_check 中也要计数
    for rowid in set(member_parents) | guarded_member_rowids:
        if rowid in guarded_member_rowids or "characters" not in member_parents.get(rowid, set()):
            unhandled["organization_members"] += 1
        else:
            deletes["organization_members"].add(rowid)

    # 孤立 relationship_types 可能仍被活的 character_relationships 引用（该列可空、
    # 未声明 ondelete，不会出现在 foreign_key_check 中）。删除类型前先置空引用，
    # 否则 PRAGMA foreign_keys=ON 下整个事务会被 FK 错误中止。
    orphan_type_ids = _dangling_relationship_type_ids(
        conn, deletes.get("relationship_types", set())
    )
    orphan_rel_rowids = _relationship_rowids_using_type_ids(conn, orphan_type_ids)
    if orphan_rel_rowids:
        nulls[("character_relationships", "relationship_type_id")].update(
            orphan_rel_rowids
        )
    return deletes, nulls, unhandled


def apply_actions(conn: sqlite3.Connection, deletes: dict, nulls: dict) -> Counter:
    """在调用方开启的事务内执行计划，返回每表改动行数。

    先置空、再删除：`character_relationships.relationship_type_id` 的置空必须早于
    `relationship_types` 删除（该列未声明 ondelete，PRAGMA foreign_keys=ON 下类型行
    先删会触发 FK 错误并中止整个事务）。其余置空策略先执行同样安全。
    """
    changes: Counter = Counter()
    for (table, column), rowids in nulls.items():
        for rowid in rowids:
            cur = conn.execute(
                f'UPDATE "{table}" SET "{column}" = NULL WHERE rowid = ?', (rowid,)
            )
            changes[table] += cur.rowcount
    for table, rowids in deletes.items():
        for rowid in rowids:
            cur = conn.execute(f'DELETE FROM "{table}" WHERE rowid = ?', (rowid,))
            changes[table] += cur.rowcount
    return changes


def _print_counts(title: str, counts: Counter) -> None:
    print(f"{title}: {sum(counts.values())} 行")
    for table in sorted(counts):
        print(f"  - {table}: {counts[table]}")


def run(db_path: str, apply: bool) -> dict:
    """执行一次清理；返回 {planned, applied, before, after} 计数。"""
    conn = sqlite3.connect(db_path)
    try:
        conn.execute("PRAGMA foreign_keys=ON")

        before_rows = collect_dangling(conn)
        before_counts = Counter(row[0] for row in before_rows)
        deletes, nulls, unhandled = plan_actions(conn, before_rows)
        planned = sum(len(v) for v in deletes.values()) + sum(len(v) for v in nulls.values())

        _print_counts("[before] foreign_key_check 检出悬空行（按子表）", before_counts)
        _print_counts("[plan] 将删除的行（按表）", Counter({k: len(v) for k, v in deletes.items()}))
        _print_counts("[plan] 将置空外键的行（按表）", Counter({k[0]: len(v) for k, v in nulls.items()}))
        if unhandled:
            _print_counts("[plan] 策略外、忽略的悬空行（按表）", unhandled)

        result = {
            "planned": planned,
            "applied": 0,
            "before": dict(before_counts),
            "after": {},
            "unhandled": dict(unhandled),
        }
        if not apply:
            print(f"[dry-run] 共计划 {planned} 处改动，未写入。使用 --apply 执行。")
            return result

        conn.execute("BEGIN")
        changes = apply_actions(conn, deletes, nulls)
        conn.commit()
        result["applied"] = sum(changes.values())

        after_counts = Counter(row[0] for row in collect_dangling(conn))
        result["after"] = dict(after_counts)
        _print_counts("[apply] 实际改动行数（按表）", changes)
        _print_counts("[after] foreign_key_check 检出悬空行（按子表）", after_counts)
        return result
    finally:
        conn.close()


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    db_path = Path(args.db)
    if not db_path.exists():
        print(f"错误：数据库文件不存在：{db_path}", file=sys.stderr)
        return 1
    try:
        run(str(db_path), args.apply)
        return 0
    except Exception as e:  # noqa: BLE001
        print(f"错误：{e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
