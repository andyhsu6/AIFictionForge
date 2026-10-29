#!/usr/bin/env python3
"""黄金集评测：读人工标注的 TSV，输出按来源/字段分组的错填率（issue #190）。

用法：
    python3 backend/scripts/eval_golden_profile_facts.py <golden-set.tsv>

输入列：id, source, entity, field, text, quote, verdict, note
- source: facts（新流水线，有引文）/ legacy（旧提示词长文本）
- verdict: 通过 / 不通过 / 拿不准（空 = 未标注）

验收口径：
- facts 组错填率 `不通过 / (通过 + 不通过)` ≤ 5%
- 拿不准单列出来人工复核，不计入分母
未标注的行只统计数量，不参与比率。
"""
from __future__ import annotations

import csv
import sys
from collections import defaultdict

PASS = "通过"
FAIL = "不通过"
UNSURE = "拿不准"
TARGET = 0.05


def _rate(rows: list[dict]) -> tuple[int, int, float | None]:
    ok = sum(1 for row in rows if row["verdict"] == PASS)
    bad = sum(1 for row in rows if row["verdict"] == FAIL)
    total = ok + bad
    return ok, bad, (bad / total if total else None)


def main(path: str) -> int:
    with open(path, encoding="utf-8", newline="") as fh:
        rows = list(csv.DictReader(fh, delimiter="\t"))

    by_source: dict[str, list[dict]] = defaultdict(list)
    by_source_field: dict[tuple[str, str], list[dict]] = defaultdict(list)
    pending = [row for row in rows if (row.get("verdict") or "").strip() not in {PASS, FAIL, UNSURE}]
    unsure = [row for row in rows if (row.get("verdict") or "").strip() == UNSURE]

    for row in rows:
        row["verdict"] = (row.get("verdict") or "").strip()
        if row["verdict"] in {PASS, FAIL}:
            by_source[row["source"]].append(row)
            by_source_field[(row["source"], row["field"])].append(row)

    print(f"总数 {len(rows)}｜已判 {len(rows) - len(pending) - len(unsure)}"
          f"｜拿不准 {len(unsure)}｜未标注 {len(pending)}")

    failed = False
    for source in sorted({row["source"] for row in rows}):
        ok, bad, rate = _rate(by_source[source])
        shown = "n/a" if rate is None else f"{rate:.1%}"
        print(f"\n[{source}] 通过 {ok} / 不通过 {bad} → 错填率 {shown}")
        for (src, field), items in sorted(by_source_field.items()):
            if src != source:
                continue
            f_ok, f_bad, f_rate = _rate(items)
            f_shown = "n/a" if f_rate is None else f"{f_rate:.1%}"
            print(f"    {field:<12} 通过 {f_ok:>2} / 不通过 {f_bad:>2} → {f_shown}")
        if source == "facts" and rate is not None and rate > TARGET:
            failed = True

    if unsure:
        print("\n拿不准（需人工复核）：")
        for row in unsure:
            print(f"  #{row['id']} [{row['source']}/{row['field']}] {row['text'][:40]}")

    if pending:
        print(f"\n还有 {len(pending)} 行未标注，比率不完整。")

    facts_ok, facts_bad, facts_rate = _rate(by_source["facts"])
    if facts_rate is None:
        print("\n结论：facts 组尚未标注，无法验收。")
        return 2
    passed = facts_rate <= TARGET
    print(f"\n结论：facts 组错填率 {facts_rate:.1%}（目标 ≤{TARGET:.0%}）→ "
          f"{'达标' if passed else '未达标'}")
    return 0 if passed and not failed else 1


if __name__ == "__main__":
    if len(sys.argv) != 2:
        print(__doc__)
        sys.exit(2)
    sys.exit(main(sys.argv[1]))
