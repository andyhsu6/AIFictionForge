#!/usr/bin/env python3
"""从生产库已存的 `characters.profile_facts` 抽样，生成新一轮人工标注集（issue #198）。

为什么抽样库里已存的 facts 而不是重跑模型：`profile_facts` 就是流水线真正写进用户档案的
内容，抽样它最贴近"用户实际看到的分类质量"，且零模型成本、可复现。

例外：验证**新提示词规则**（issue #198）时，库里的历史 facts 是旧规则产物，不能用来
验证；此时用 `--facts-tsv` 直接读 `backfill_profiles.py --dump-facts` 的 dry-run 导出
（新提示词生成、已通过校验的原子事实），完全不连数据库。

口径（与上一轮 #190 的集子一致）：
- 只测**字段分类**（这段文字放在该字段里是否合适）。表达形态、主体归属这两轴测不到。
- 本集子回答"当前流水线分类准不准"，不回答"比历史现状好多少"。

用法：
    DATABASE_URL="sqlite+aiosqlite:////abs/path/app.db" PYTHONPATH=backend \\
        python backend/scripts/sample_golden_profile_facts.py --out-dir .omo/evidence/golden-198 \\
        --count 80 --exclude-tsv old/golden-set.tsv --label-template old/label.html

    # 不读库：直接从 backfill_profiles.py --dump-facts 的新产物抽样
    python backend/scripts/sample_golden_profile_facts.py --out-dir /tmp/set \\
        --facts-tsv /tmp/facts.tsv --label-template old/label.html

输出：
- <out-dir>/golden-set-198.tsv  列：id, source, entity, field, text, quote, verdict, note
- <out-dir>/label.html          仅当给了 --label-template；除 payload 行外与模板逐字节相同
- <out-dir>/README.md           口径与分布说明

数据库只读：先把主库**连同 -wal** 一起快照到临时目录再打开（只复制主库会漏掉尚未
checkpoint 的写入；直接只读打开 WAL 库又会在库目录生成 -wal/-shm 旁文件），因此
既拿到完整数据，也不改动原库目录。
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import os
import random
import shutil
import sqlite3
import tempfile
from collections import defaultdict
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_DB = REPO_ROOT / "backend" / "data" / "mumuai_novel.db"
DEFAULT_URL = "sqlite+aiosqlite:///" + str(DEFAULT_DB)

FIELDS = ("personality", "background", "appearance")
TSV_COLUMNS = ["id", "source", "entity", "field", "text", "quote", "verdict", "note"]
PAYLOAD_MARK = '<script id="payload" type="application/json">'


def _db_path(database_url: str) -> Path:
    """从 SQLAlchemy 风格的 URL（或裸路径）解析出 sqlite 文件路径。"""
    raw = database_url.strip()
    if "://" in raw:
        scheme, rest = raw.split("://", 1)
        if not scheme.startswith("sqlite"):
            raise SystemExit(f"只支持 sqlite，收到 scheme={scheme!r}")
        rest = rest.split("?", 1)[0]
        # sqlite+aiosqlite:////abs/path -> "//abs/path"；sqlite:///rel.db -> "rel.db"
        if rest.startswith("//"):
            raw = rest[1:]
        else:
            raw = rest.lstrip("/")
    path = Path(raw).expanduser()
    if not path.is_absolute():
        path = (REPO_ROOT / path).resolve()
    if not path.exists():
        raise SystemExit(f"数据库不存在：{path}")
    return path


def _read_facts(db_path: Path) -> list[dict]:
    """只读快照读取 facts；返回 [{entity_id, entity, is_organization, field, text, quote}]。

    快照把主库**连同 -wal 一起**复制到临时目录再打开，两个理由：
    1. 应用常驻开着 WAL，未 checkpoint 的写入只在 -wal 里，只复制主库会漏掉它们，
       抽样集就偏向旧状态；
    2. 直接对原库开连接（哪怕 `mode=ro`）也会在库目录生成 -wal/-shm，这条路只在
       临时目录里留痕，不自作主张改用户的库目录。
    复制期间若有写入进程正在活动，理论上可能拿到半截 WAL；抽样是只读测量用途，
    这种情况重跑即可，不会影响数据。
    """
    tmp = Path(tempfile.mkdtemp(prefix="golden-facts-"))
    try:
        snapshot = tmp / "snapshot.db"
        shutil.copy2(db_path, snapshot)
        wal = db_path.with_name(db_path.name + "-wal")
        if wal.exists():
            shutil.copy2(wal, snapshot.with_name(snapshot.name + "-wal"))
        conn = sqlite3.connect(f"file:{snapshot}?mode=ro", uri=True)
        try:
            rows = conn.execute(
                "SELECT id, name, is_organization, profile_facts FROM characters "
                "WHERE profile_facts IS NOT NULL AND profile_facts != ''"
            ).fetchall()
        finally:
            conn.close()
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    items: list[dict] = []
    for entity_id, name, is_org, raw in rows:
        try:
            facts = json.loads(raw)
        except (TypeError, ValueError):
            continue
        if not isinstance(facts, list):
            continue
        for fact in facts:
            if not isinstance(fact, dict):
                continue
            field = fact.get("field")
            text = (fact.get("claim") or "").strip()
            if field not in FIELDS or not text:
                continue
            items.append({
                "entity_id": entity_id,
                "entity": name or f"entity-{entity_id}",
                "is_organization": 1 if is_org else 0,
                "field": field,
                "text": text,
                "quote": (fact.get("quote") or "").strip(),
            })
    return items


def _read_facts_tsv(path: Path) -> tuple[list[dict], bool]:
    """从 `backfill_profiles.py --dump-facts` 的 TSV 读候选，返回 (items, 是否有 kind 列)。

    为什么要有这条路：验证新提示词规则时必须用**新提示词重新生成**的 facts，库里旧
    facts 是旧规则产物；而回填脚本本身支持 dry-run 导出，所以不落库也能抽样
    （导出时需配 `--include-existing`，否则已有 facts 的实体会被幂等跳过）。

    去重键用 `(project_id, entity)`：同一份 TSV 可能来自多个项目，跨项目的同名实体
    不是同一个实体，不合并。
    """
    if not path.exists():
        raise SystemExit(f"--facts-tsv 文件不存在：{path}")

    items: list[dict] = []
    with path.open(encoding="utf-8", newline="") as fh:
        reader = csv.DictReader(fh, delimiter="\t")
        has_kind = "kind" in (reader.fieldnames or [])
        for row in reader:
            field = (row.get("field") or "").strip()
            text = (row.get("claim") or "").strip()
            if field not in FIELDS or not text:
                continue
            entity = (row.get("entity") or "").strip() or "entity-unknown"
            project_id = (row.get("project_id") or "").strip()
            # 没有 kind 列的历史 TSV 一律按人物分层（README 会注明）
            kind = ((row.get("kind") or "").strip().lower() if has_kind else "person")
            items.append({
                # TSV 里没有数据库 id；去重按 (project_id, entity)，避免跨项目同名实体合并
                "entity_id": (project_id, entity),
                "entity": entity,
                "is_organization": 1 if kind == "organization" else 0,
                "field": field,
                "text": text,
                "quote": row.get("quote") or "",
            })
    return items, has_kind


def _read_excluded(paths: list[str]) -> set[str]:
    excluded: set[str] = set()
    for raw in paths:
        p = Path(raw).expanduser()
        if not p.exists():
            raise SystemExit(f"--exclude-tsv 文件不存在：{p}")
        with p.open(encoding="utf-8", newline="") as fh:
            for row in csv.DictReader(fh, delimiter="\t"):
                text = (row.get("text") or "").strip()
                if text:
                    excluded.add(text)
    return excluded


def _allocate(sizes: dict[tuple[int, str], int], total: int) -> dict[tuple[int, str], int]:
    """按候选量优先分配配额，尽量让每个非空分层拿到 1 条（条数少于分层数时无法保证）。"""
    keys = [k for k in sorted(sizes) if sizes[k] > 0]
    total = min(total, sum(sizes[k] for k in keys))
    quota = {k: 0 for k in sizes}

    # 保底：候选量大的分层优先拿那 1 条（total 小于分层数时也只有部分分层能拿到）
    for k in sorted(keys, key=lambda k: (-sizes[k], k))[:total]:
        quota[k] = 1

    remaining = total - sum(quota.values())
    caps = {k: sizes[k] - 1 for k in keys}
    cap_total = sum(caps.values())
    if remaining > 0 and cap_total > 0:
        ideal = {k: remaining * caps[k] / cap_total for k in keys}
        added = {k: min(int(ideal[k]), caps[k]) for k in keys}
        leftover = remaining - sum(added.values())
        while leftover > 0:
            order = sorted(
                (k for k in keys if added[k] < caps[k]),
                key=lambda k: (-(ideal[k] - int(ideal[k])), -caps[k], k),
            )
            if not order:  # 候选不够，拿不满 requested count
                break
            for k in order:
                if leftover == 0:
                    break
                added[k] += 1
                leftover -= 1
        for k in keys:
            quota[k] += added[k]
    return quota


def _build_rows(items: list[dict], count: int, seed: int) -> tuple[list[dict], dict]:
    # 同一实体内同一 claim 去重；不同实体重复的文本不算重复（保留）
    seen: set[tuple] = set()
    deduped: list[dict] = []
    for item in items:
        key = (item["entity_id"], item["text"])
        if key in seen:
            continue
        seen.add(key)
        deduped.append(item)

    strata: dict[tuple[int, str], list[dict]] = defaultdict(list)
    for item in deduped:
        strata[(item["is_organization"], item["field"])].append(item)

    sizes = {k: len(v) for k, v in strata.items()}
    quota = _allocate(sizes, count)

    rng = random.Random(seed)
    picked: list[dict] = []
    for key in sorted(strata):
        bucket = sorted(strata[key], key=lambda i: (i["entity_id"], i["text"]))
        rng.shuffle(bucket)
        picked.extend(bucket[: quota.get(key, 0)])

    rng.shuffle(picked)  # 标注时不按分层扎堆，避免顺序暗示
    for idx, item in enumerate(picked, start=1):
        item["id"] = idx
    return picked, {"sizes": sizes, "quota": quota, "candidates": len(deduped)}


def _render_payload(rows: list[dict]) -> str:
    payload = [
        {
            "id": row["id"],
            "source": "facts",
            "entity": row["entity"],
            "field": row["field"],
            "text": row["text"],
            "quote": row["quote"],
        }
        for row in rows
    ]
    # "</" 转义成 "<\/"，避免正文里的 "</script>" 提前闭合
    return json.dumps(payload, ensure_ascii=False, indent=None).replace("</", "<\\/")


def _write_html(template: Path, rows: list[dict], out_path: Path) -> None:
    original = template.read_text(encoding="utf-8")
    lines = original.splitlines(keepends=True)
    hits = [i for i, line in enumerate(lines) if line.lstrip().startswith(PAYLOAD_MARK)]
    if len(hits) != 1:
        raise SystemExit(f"模板里 payload 行不唯一（命中 {len(hits)} 行），拒绝生成：{template}")
    idx = hits[0]
    ending = "\n" if lines[idx].endswith("\n") else ""
    payload = _render_payload(rows)
    lines[idx] = f"{PAYLOAD_MARK}{payload}</script>{ending}"

    # 自动保存键必须按集区分：所有轮次的勾选页都从 id=1 开始，共用一个键会让新集
    # 一打开就套上上一轮的判定（表现为「弹出的还是那份标注过的」）。
    digest = hashlib.sha1(payload.encode("utf-8")).hexdigest()[:8]
    key_hits = [i for i, line in enumerate(lines) if "const KEY =" in line]
    if len(key_hits) != 1:
        raise SystemExit(f"模板里 `const KEY =` 行不唯一（命中 {len(key_hits)} 行），拒绝生成：{template}")
    key_ending = "\n" if lines[key_hits[0]].endswith("\n") else ""
    lines[key_hits[0]] = f"const KEY = 'golden-verdicts-{digest}';{key_ending}"

    # 标题写明是哪一轮哪一份，避免再次把两套集子看成同一份
    set_name = out_path.parent.name
    title_hits = [i for i, line in enumerate(lines) if "<h1>" in line]
    if title_hits:
        title_ending = "\n" if lines[title_hits[0]].endswith("\n") else ""
        lines[title_hits[0]] = (
            f"  <h1>档案字段标注 · {set_name} · {len(rows)} 条</h1>{title_ending}"
        )

    # 浏览器标签页上显示的是 <title>，不改它等于让两轮标注看起来同名
    doc_title_hits = [i for i, line in enumerate(lines) if "<title>" in line]
    if doc_title_hits:
        doc_ending = "\n" if lines[doc_title_hits[0]].endswith("\n") else ""
        lines[doc_title_hits[0]] = (
            f"<title>档案字段标注 · {set_name} · {len(rows)} 条</title>{doc_ending}"
        )

    out_path.write_text("".join(lines), encoding="utf-8")


def _write_tsv(rows: list[dict], out_path: Path) -> None:
    with out_path.open("w", encoding="utf-8", newline="") as fh:
        writer = csv.writer(fh, delimiter="\t", lineterminator="\n")
        writer.writerow(TSV_COLUMNS)
        for row in rows:
            writer.writerow([row["id"], "facts", row["entity"], row["field"],
                             row["text"], row["quote"], "", ""])


def _write_readme(out_path: Path, rows: list[dict], stats: dict, args: argparse.Namespace,
                  excluded: int) -> None:
    sizes, quota = stats["sizes"], stats["quota"]

    def label(is_org: int, field: str) -> str:
        return f"{'组织' if is_org else '人物'}/{field}"

    # 来源不同会直接影响这批标注能回答什么问题，README 必须写清是哪一种
    if args.facts_tsv:
        intro = [
            f"{len(rows)} 条 claim，来自 `backfill_profiles.py --dump-facts` 导出的 TSV"
            f"（`{args.facts_tsv}`，**新提示词重跑**产物），不重跑模型。",
            "",
            "这与「抽样库里的历史 facts」不同：TSV 是本次按新提示词生成、"
            "并通过 `validate_profile_facts` 的原子事实，用来验证新规则本身。"
            "生成 TSV 时须用 `--include-existing`：已有 `profile_facts` 属于旧规则产物，"
            "既不能当新样本，也会被幂等跳过；`--include-existing` 下 TSV 只含本次新生成内容。",
        ]
        source_line = (
            f"- 来源：`--facts-tsv` 里 `claim` 非空、`field` 合法的条目"
            f"（{stats['candidates']} 条候选，去重后）"
        )
    else:
        intro = [
            f"{len(rows)} 条 claim，直接抽样自**生产库已存的** `characters.profile_facts`"
            "（当前流水线真正写进用户档案的内容），不重跑模型。",
        ]
        source_line = (
            f"- 来源：`characters.profile_facts` 里 `claim` 非空、`field` 合法的条目"
            f"（{stats['candidates']} 条候选，去重后）"
        )

    lines = [
        "# 档案字段黄金集（第 2 轮，issue #198 复测）",
        "",
        "## 这是什么",
        "",
        *intro,
        "",
        "数据来自本地 `.omo/`（已 gitignore），**不要提交到仓库**。",
        "",
        "## 抽样口径",
        "",
        source_line,
        f"- 分层：`(kind, field)` 共 {len([k for k in sizes if sizes[k]])} 个非空分层，"
        "配额按候选量优先分配，尽量让每个分层都有 1 条（条数不足时无法保证）",
        f"- 随机种子：`{args.seed}`（`random.Random(seed)`，可复现）",
        f"- 目标条数：{args.count}，实际 {len(rows)} 条",
        f"- 已排除上一轮集子里的 {excluded} 条 `text`（避免重复判定）",
    ]
    if args.facts_tsv:
        lines.append("- 去重键：`(project_id, entity)`；跨项目同名实体不合并")
    if not stats.get("has_kind", True):
        lines.append("- **输入 TSV 没有 `kind` 列：全部条目按「人物」分层**")
    lines += [
        "",
        "| 分层 | 候选量 | 抽中 |",
        "|---|---|---|",
    ]
    for key in sorted(sizes):
        if sizes[key] == 0 and quota.get(key, 0) == 0:
            continue
        lines.append(f"| {label(*key)} | {sizes[key]} | {quota.get(key, 0)} |")

    lines += [
        "",
        "## 重要：这个集子只测一件事",
        "",
        "标注只回答**字段分类**，所以它算出来的数字是**分类错误率**。下面三件事它都测不到，不要混成一个数字：",
        "",
        "| 轴 | 能不能用这套标注 | 怎么测 |",
        "|---|---|---|",
        "| 字段分类（放错字段） | ✅ 就是它 | `eval_golden_profile_facts.py` |",
        "| 表达形态（写成行为句而不是特质短语） | ❌ | 逐条读 claim，判\"能否接在『他是…』后面\" |",
        "| 主体归属（把 A 的事写成 B 的） | ❌ | 对照原文，确认这句话是不是关于这个实体 |",
        "",
        "另外：本集子回答**当前流水线分类准不准**，不回答**比历史现状好多少**——后者需要把库里的"
        "历史档案作为独立 `source` 加进来才可比。",
        "",
        "## 怎么标",
        "",
        "只填 `verdict` 和（可选）`note` 两列，其它列不要改。",
        "",
        "- `verdict` 取值：`通过` / `不通过` / `拿不准`",
        "- 判定只看一件事：**这段文字放在 `field` 指定的字段里是否合适**",
        "  - `personality` 只收性情、脾气、态度、处事风格",
        "  - `background` 收出身、师承、身份与归属（门派/职务/排行/亲属）、经历、能力与实力",
        "  - `appearance` 只收外形（体型、容貌、衣着、气质外观）",
        "- `拿不准` 不要硬判，留给人一起看。",
        "",
        "本集子全部 `source` 都是 `facts`（带 `quote` 原文引用）。",
        "推荐用勾选页 `label.html`（浏览器打开即用，键盘 1/2/3，自动保存，底部导出 TSV）。",
        "",
        "## 标完怎么用",
        "",
        "```bash",
        "python3 backend/scripts/eval_golden_profile_facts.py <golden-set-198.tsv>",
        "```",
        "",
        "输出按 `source` / `field` 分组的**分类错误率**，以及 `拿不准` 清单。",
        "",
    ]
    out_path.write_text("\n".join(lines), encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(description="从生产 profile_facts 抽样生成黄金集")
    parser.add_argument("--out-dir", required=True, help="输出目录（不存在则创建）")
    parser.add_argument("--count", type=int, default=80, help="目标条数（默认 80）")
    parser.add_argument("--seed", type=int, default=20261030, help="随机种子（默认 20261030）")
    parser.add_argument("--exclude-tsv", action="append", default=[],
                        help="把该 TSV 里已有的 text 全部排除（可重复）")
    parser.add_argument("--facts-tsv", default=None,
                        help=("改从该 TSV（backfill_profiles.py --dump-facts 产物）读候选，不连数据库；"
                              "该 TSV 应是配 --include-existing 按新提示词重新生成的产物"))
    parser.add_argument("--label-template", default=None, help="label.html 模板，用它的样式生成勾选页")
    args = parser.parse_args()

    if args.count <= 0:
        raise SystemExit("--count 必须为正数")

    out_dir = Path(args.out_dir).expanduser()
    out_dir.mkdir(parents=True, exist_ok=True)

    if args.facts_tsv:
        # 明确不构造引擎/快照：运行环境不需要库文件
        source = f"TSV（{args.facts_tsv}）"
        items, has_kind = _read_facts_tsv(Path(args.facts_tsv).expanduser())
    else:
        db_path = _db_path(os.environ.get("DATABASE_URL") or DEFAULT_URL)
        source = f"数据库（{db_path}）"
        items = _read_facts(db_path)
        has_kind = True
    excluded_texts = _read_excluded(args.exclude_tsv)
    if excluded_texts:
        before = len(items)
        items = [item for item in items if item["text"] not in excluded_texts]
        excluded_hits = before - len(items)
    else:
        excluded_hits = 0

    rows, stats = _build_rows(items, args.count, args.seed)
    stats["has_kind"] = has_kind

    tsv_path = out_dir / "golden-set-198.tsv"
    _write_tsv(rows, tsv_path)
    _write_readme(out_dir / "README.md", rows, stats, args, excluded_hits)

    print(f"候选来源：{source}")
    print(f"候选（去重后）：{stats['candidates']}｜已排除上一轮文本：{excluded_hits} 条")
    print(f"抽中：{len(rows)} 条（目标 {args.count}）")
    print("\n分层分布（候选量 → 抽中）：")
    for key in sorted(stats["sizes"]):
        is_org, field = key
        size = stats["sizes"][key]
        got = stats["quota"].get(key, 0)
        if size == 0 and got == 0:
            continue
        print(f"  {'组织' if is_org else '人物'}/{field:<12} {size:>4} → {got:>3}")

    if len(rows) < args.count:
        print(f"\n[警告] 候选不足，只抽到 {len(rows)} 条（目标 {args.count}）")

    print(f"\nTSV：{tsv_path}")
    print(f"README：{out_dir / 'README.md'}")
    if args.label_template:
        template = Path(args.label_template).expanduser()
        if not template.exists():
            raise SystemExit(f"--label-template 不存在：{template}")
        html_path = out_dir / "label.html"
        _write_html(template, rows, html_path)
        print(f"勾选页：{html_path}")
    else:
        print("勾选页：跳过（未给 --label-template）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
