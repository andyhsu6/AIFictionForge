#!/usr/bin/env python3
"""修复创作助手会话标题（issue #260，默认 dry-run）。

历史会话里标题仍为空/「新对话」的行，按该会话**第一条用户消息**回填标题；
只处理默认标题，用户自定义标题不动。默认只打印计划，加 `--apply` 才写库。

用法::

    backend/.venv/bin/python backend/scripts/backfill_agent_conversation_titles.py
    backend/.venv/bin/python backend/scripts/backfill_agent_conversation_titles.py --apply
    backend/.venv/bin/python backend/scripts/backfill_agent_conversation_titles.py --db /path/to/db --apply
"""
from __future__ import annotations

import argparse
import sqlite3
import sys
from pathlib import Path

DEFAULT_TITLE = "新对话"
DEFAULT_DB = "backend/data/mumuai_novel.db"


def derive_title(first_message: str | None) -> str:
    """与 app.services.project_agent_service.derive_conversation_title 同口径。"""
    for raw_line in (first_message or "").splitlines():
        line = raw_line.strip().lstrip("#>-* ").strip()
        normalized = " ".join(line.split())
        if normalized:
            return normalized[:40]
    return DEFAULT_TITLE


def plan_updates(conn: sqlite3.Connection) -> list[tuple[str, str]]:
    rows = conn.execute(
        "SELECT id, title FROM agent_conversations "
        "WHERE title IS NULL OR trim(title) = '' OR title = ?",
        (DEFAULT_TITLE,),
    ).fetchall()
    planned: list[tuple[str, str]] = []
    for conversation_id, _title in rows:
        first = conn.execute(
            "SELECT content FROM agent_messages "
            "WHERE conversation_id = ? AND role = 'user' "
            "ORDER BY created_at ASC, id ASC LIMIT 1",
            (conversation_id,),
        ).fetchone()
        if not first:
            continue
        title = derive_title(first[0])
        if title == DEFAULT_TITLE:
            continue
        planned.append((conversation_id, title))
    return planned


def count_unrepairable(conn: sqlite3.Connection) -> int:
    """统计仍为默认标题但推导不出有效标题的会话数（无消息/首条消息为空）。"""
    rows = conn.execute(
        "SELECT id FROM agent_conversations "
        "WHERE title IS NULL OR trim(title) = '' OR title = ?",
        (DEFAULT_TITLE,),
    ).fetchall()
    planned_ids = {conversation_id for conversation_id, _ in plan_updates(conn)}
    return sum(1 for (conversation_id,) in rows if conversation_id not in planned_ids)


def apply_updates(conn: sqlite3.Connection, planned: list[tuple[str, str]]) -> int:
    """写入计划好的标题（幂等）；返回实际写入条数。"""
    if not planned:
        return 0
    conn.executemany(
        "UPDATE agent_conversations SET title = ? WHERE id = ?",
        [(title, conversation_id) for conversation_id, title in planned],
    )
    conn.commit()
    return len(planned)


def main() -> int:
    parser = argparse.ArgumentParser(description="回填创作助手会话标题（issue #260）")
    parser.add_argument("--db", default=DEFAULT_DB, help=f"SQLite 路径（默认 {DEFAULT_DB}）")
    parser.add_argument("--apply", action="store_true", help="真正写库（默认 dry-run）")
    args = parser.parse_args()

    db_path = Path(args.db)
    if not db_path.exists():
        print(f"错误：数据库不存在：{args.db}", file=sys.stderr)
        return 2
    conn = sqlite3.connect(args.db)
    try:
        try:
            planned = plan_updates(conn)
            unrepairable = count_unrepairable(conn)
        except sqlite3.OperationalError as exc:
            print(f"错误：读取会话表失败：{exc}", file=sys.stderr)
            return 2
        print(f"待修复会话：{len(planned)} 个（db={args.db}）")
        for conversation_id, title in planned[:10]:
            print(f"  {conversation_id} -> {title[:24]}{'…' if len(title) > 24 else ''}")
        if len(planned) > 10:
            print(f"  …其余 {len(planned) - 10} 个")
        if unrepairable:
            print(f"无法推导标题：{unrepairable} 个（无用户消息或首条消息为空，将在首条消息后自愈）")
        if not args.apply:
            print("dry-run：未写库；加 --apply 执行。")
            return 0
        written = apply_updates(conn, planned)
        print(f"已修复 {written} 个会话标题。")
        return 0
    finally:
        conn.close()


if __name__ == "__main__":
    raise SystemExit(main())
