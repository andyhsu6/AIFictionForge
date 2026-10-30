#!/usr/bin/env python
"""清除组织中残留的 personality（issue #210）。

组织不再有 personality 字段：``validate_profile_facts`` 会丢弃新产出的性格事实，
`book_import_service` / 三条组织创建路径也不再写入。但**存量行仍带着旧值**——
档案同步对空值一律跳过（只写不清），所以历史数据的性格文本与溯源事实会一直留在库里、
一直显示在组织详情页。

本脚本做一次性的清理：

- ``characters.personality``（仅 ``is_organization = 1``）置 NULL；
- ``characters.profile_facts`` 中 ``field == "personality"`` 的条目摘除；
- 其余字段（background / appearance / 其他实体）一律不动。

默认 dry-run：只统计、只报数量，不打印任何正文内容。加 ``--apply`` 才写库。
重复运行是幂等的（第二次应当全部报 0）。
"""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
from pathlib import Path

DEFAULT_DB = Path(__file__).resolve().parent.parent / "data" / "mumuai_novel.db"


def _db_path(explicit: str | None) -> Path:
    if explicit:
        return Path(explicit)
    url = os.getenv("DATABASE_URL", "")
    if url.startswith("sqlite"):
        raw = url.split("///", 1)[-1]
        if raw and raw != ":memory:":
            return Path(raw)
    return DEFAULT_DB


def _count(sql: str, conn: sqlite3.Connection) -> int:
    row = conn.execute(sql).fetchone()
    return int(row[0]) if row else 0


def _load_org_rows(conn: sqlite3.Connection) -> list[tuple[str, str, str | None, str | None]]:
    return list(
        conn.execute(
            "SELECT id, name, personality, profile_facts FROM characters "
            "WHERE is_organization = 1 AND ("
            "  (personality IS NOT NULL AND TRIM(personality) != '')"
            "  OR profile_facts LIKE '%\"personality\"%'"
            ")"
        )
    )


def _strip_facts(raw: str | None) -> tuple[str | None, int]:
    """返回 (新 JSON, 摘除条数)；解析失败时原样返回，不猜。"""
    if not raw:
        return raw, 0
    try:
        facts = json.loads(raw)
    except (ValueError, TypeError):
        return raw, 0
    if not isinstance(facts, list):
        return raw, 0
    kept = [f for f in facts if not (isinstance(f, dict) and f.get("field") == "personality")]
    removed = len(facts) - len(kept)
    if removed == 0:
        return raw, 0
    return json.dumps(kept, ensure_ascii=False), removed


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="清除组织中残留的 personality（issue #210）")
    parser.add_argument("--db", help="数据库文件路径（默认取 DATABASE_URL 或 backend/data/mumuai_novel.db）")
    parser.add_argument("--apply", action="store_true", help="真正写库（默认只统计）")
    args = parser.parse_args(argv)

    path = _db_path(args.db)
    if not path.exists():
        print(f"数据库不存在：{path}", file=sys.stderr)
        return 2

    conn = sqlite3.connect(str(path), timeout=30)
    try:
        conn.execute("PRAGMA busy_timeout = 30000")
        rows = _load_org_rows(conn)
        prose = [r for r in rows if (r[2] or "").strip()]
        facts_hits: list[tuple[str, str | None, int]] = []
        for cid, _name, _personality, facts_raw in rows:
            _new, removed = _strip_facts(facts_raw)
            if removed:
                facts_hits.append((cid, facts_raw, removed))

        print(f"数据库：{path}")
        print(f"组织总数：{_count('SELECT COUNT(*) FROM characters WHERE is_organization = 1', conn)}")
        print(f"带 personality 文本的组织：{len(prose)}")
        print(f"profile_facts 里含 personality 条目的组织：{len(facts_hits)}"
              f"（共 {sum(item[2] for item in facts_hits)} 条）")

        if not args.apply:
            print("dry-run：未写库。确认后加 --apply。")
            return 0

        updated = 0
        for cid, _name, _personality, facts_raw in rows:
            new_facts, removed = _strip_facts(facts_raw)
            conn.execute(
                "UPDATE characters SET personality = NULL, profile_facts = ? WHERE id = ?",
                (new_facts, cid),
            )
            updated += 1
            if removed:
                print(f"  {cid}: 摘除 {removed} 条 personality 事实")
        conn.commit()
        print(f"已更新 {updated} 行。")
        return 0
    finally:
        conn.close()


if __name__ == "__main__":
    raise SystemExit(main())
