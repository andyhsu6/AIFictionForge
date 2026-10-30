#!/usr/bin/env python3
"""档案事实去重测量（只读）：把「为什么不用 embedding」变成可复现证据。

issue #201 第 2 项：``render_profile`` 只按 ``normalize_quote(claim)`` 精确去重，
同一特质的换说法会并存。本脚本对比两种候选判重口径在真库 ``characters.profile_facts``
上的表现，**只输出文件 + stdout 摘要，不写库、不改档案、不进任何生产路径**。

两种模式：

- ``containment``（默认）：同一字段内按 ``normalize_claim_form``（去空白/标点/结构
  助词）归一后是否互为子串，列出全部「可合并对」及保留哪条——这是现网采用的规则。
- ``embedding``：用本地 ONNX 模型（``ONNX_EMBEDDING_MODEL_DIR`` 可覆盖）算同字段内
  全配对余弦相似度，按阈值列出可疑重复对，并给出 0.90/0.92/0.95 各自的合并条数。

用法::

    DATABASE_URL="sqlite+aiosqlite:////path/to/mumuai_novel.db" PYTHONPATH=backend \\
        backend/.venv/bin/python backend/scripts/measure_fact_dedupe.py \\
        --out-dir /tmp/dedupe-contain --similarity containment
"""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
from collections import Counter
from pathlib import Path
from typing import Any

_project_root = Path(__file__).resolve().parents[1]
if str(_project_root) not in sys.path:
    sys.path.insert(0, str(_project_root))

from app.services.profile_facts import (  # noqa: E402
    FACT_FIELDS,
    _folded_contains,
    normalize_claim_form,
    normalize_quote,
)

DEFAULT_DB = _project_root / "data" / "mumuai_novel.db"
EMBEDDING_REPORT_THRESHOLDS = (0.90, 0.92, 0.95)


# --------------------------------------------------------------------------- #
# 参数
# --------------------------------------------------------------------------- #
def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="档案事实去重测量（只读）")
    parser.add_argument("--out-dir", required=True, help="输出目录（pairs.tsv / summary.md）")
    parser.add_argument(
        "--similarity",
        choices=("containment", "embedding"),
        default="containment",
        help="判重口径，默认 containment（归一后互为子串）",
    )
    parser.add_argument(
        "--threshold",
        type=float,
        default=0.90,
        help="仅 embedding 模式：pairs.tsv 收录的相似度下限，默认 0.90",
    )
    args = parser.parse_args(argv)
    if not 0.0 < args.threshold <= 1.0:
        parser.error("--threshold 必须在 (0, 1] 之间")
    return args


# --------------------------------------------------------------------------- #
# 只读取数
# --------------------------------------------------------------------------- #
def _db_path() -> Path:
    """DATABASE_URL（sqlite 文件库）优先，缺省用 backend/data 下的开发库。"""
    raw = (os.getenv("DATABASE_URL") or "").strip()
    if not raw:
        return DEFAULT_DB
    from sqlalchemy.engine import make_url

    url = make_url(raw)
    if url.get_backend_name() != "sqlite" or not url.database:
        raise SystemExit(f"DATABASE_URL 不是 sqlite 文件库：{raw}")
    return Path(url.database).expanduser()


def _sidecars(path: Path) -> dict[Path, bool]:
    """记录 -wal/-shm 是否本来就存在：只清理本次运行新产生的文件。"""
    sidecars = {}
    for suffix in ("-wal", "-shm"):
        sidecar = path.with_name(path.name + suffix)
        sidecars[sidecar] = sidecar.exists()
    return sidecars


def _open_readonly(path: Path) -> sqlite3.Connection:
    if not path.is_file():
        raise SystemExit(f"找不到数据库：{path}")
    # mode=ro：连接只允许 SELECT，任何写入都会被 SQLite 拒绝
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def _load_entities(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT id, project_id, name, is_organization, profile_facts FROM characters "
        "WHERE profile_facts IS NOT NULL AND TRIM(profile_facts) != '' "
        "ORDER BY project_id, name"
    ).fetchall()


def _field_claims(raw: Any) -> tuple[dict[str, list[str]], int]:
    """把一条 ``profile_facts`` JSON 拆成 {field: [claim, ...]}，并返回原始条目数。

    与 ``render_profile`` 阶段 1 对齐：未知字段/非字符串/空串丢弃，按
    ``normalize_quote`` 先到先留去重。
    """
    try:
        facts = json.loads(raw) if isinstance(raw, str) else raw
    except (TypeError, ValueError):
        return {}, 0
    if not isinstance(facts, list):
        return {}, 0

    claims: dict[str, list[str]] = {field: [] for field in FACT_FIELDS}
    seen: dict[str, set[str]] = {field: set() for field in FACT_FIELDS}
    for fact in facts:
        if not isinstance(fact, dict):
            continue
        field = fact.get("field")
        claim = fact.get("claim")
        if field not in FACT_FIELDS or not isinstance(claim, str):
            continue
        key = normalize_quote(claim)
        if not key or key in seen[field]:
            continue
        seen[field].add(key)
        text = claim.strip()
        if text:
            claims[field].append(text)
    return claims, len(facts)


def _tsv_field(text: str) -> str:
    return " ".join((text or "").split())


# --------------------------------------------------------------------------- #
# containment 模式
# --------------------------------------------------------------------------- #
def _containment_pairs(claims: list[str]) -> list[tuple[str, str, str]]:
    """返回 [(kind, dropped, kept)]，kind = ``contained`` / ``form_equal``。"""
    forms = [(claim, normalize_claim_form(claim)) for claim in claims]
    pairs: list[tuple[str, str, str]] = []
    for index, (claim_a, form_a) in enumerate(forms):
        for other_index, (claim_b, form_b) in enumerate(forms):
            if index == other_index or not form_a or not form_b:
                continue
            if form_a == form_b and index < other_index:
                # 只差标点/结构助词：合并为一条。生产的 render_profile 保留原文
                # 较长者，这里按同一准则报告「被丢的那条 / 留下的那条」。
                dropped, kept = (
                    (claim_b, claim_a)
                    if len(claim_a.replace("、", "")) >= len(claim_b.replace("、", ""))
                    else (claim_a, claim_b)
                )
                pairs.append(("form_equal", dropped, kept))
            elif form_a != form_b and _folded_contains(form_b, form_a):
                # 与生产同一判据（复用 _folded_contains，含否定/反问与 ASCII 闸），
                # 否则报告会列出 render_profile 根本不会合并的对。
                pairs.append(("contained", claim_a, claim_b))
    return pairs


def _run_containment(rows: list[sqlite3.Row]) -> dict[str, Any]:
    pairs: list[dict[str, str]] = []
    claim_total = 0

    for row in rows:
        claims_by_field, _ = _field_claims(row["profile_facts"])
        for field in FACT_FIELDS:
            claims = claims_by_field[field]
            claim_total += len(claims)
            for kind, dropped, kept in _containment_pairs(claims):
                pairs.append(
                    {
                        "entity_id": row["id"],
                        "entity": row["name"],
                        "kind": "organization" if row["is_organization"] else "character",
                        "field": field,
                        "pair_kind": kind,
                        "dropped_claim": dropped,
                        "kept_claim": kept,
                    }
                )

    pairs.sort(key=lambda item: (item["entity"], item["field"], item["pair_kind"], item["dropped_claim"]))
    return {"pairs": pairs, "claim_total": claim_total, "entity_total": len(rows)}


def _write_containment(out_dir: Path, result: dict[str, Any]) -> None:
    pairs = result["pairs"]
    lines = ["entity_id\tentity\tkind\tfield\tpair_kind\tdropped_claim\tkept_claim"]
    for item in pairs:
        lines.append(
            "\t".join(
                [
                    item["entity_id"],
                    _tsv_field(item["entity"]),
                    item["kind"],
                    item["field"],
                    item["pair_kind"],
                    _tsv_field(item["dropped_claim"]),
                    _tsv_field(item["kept_claim"]),
                ]
            )
        )
    (out_dir / "pairs.tsv").write_text("\n".join(lines) + "\n", encoding="utf-8")

    by_field: Counter = Counter(item["field"] for item in pairs)
    by_kind: Counter = Counter(item["pair_kind"] for item in pairs)
    entities_with = len({item["entity_id"] for item in pairs})

    summary = [
        "# 档案事实去重测量：containment 规则",
        "",
        "> 只读测量：仅 SELECT `characters.profile_facts`，未写库、未改档案。",
        "",
        f"- 口径：同一字段内，`normalize_claim_form`（去空白/标点/结构助词「的地得」）"
        f"归一后互为子串；`form_equal` = 归一后完全相同（只差标点/助词）。",
        f"- 参与测量的实体数：{result['entity_total']}",
        f"- 参与测量的 claim 数（精确去重后）：{result['claim_total']}",
        f"- 可合并对总数：{len(pairs)}（涉及 {entities_with} 个实体、"
        f"{len({(p['entity_id'], p['field']) for p in pairs})} 个字段）",
        f"- `contained`（真子串，保留较长者）：{by_kind.get('contained', 0)}",
        f"- `form_equal`（归一后相同，保留先到者）：{by_kind.get('form_equal', 0)}",
        "",
        "## 按字段",
        "",
        "| field | 对数 |",
        "| --- | --- |",
    ]
    for field in FACT_FIELDS:
        summary.append(f"| {field} | {by_field.get(field, 0)} |")
    summary += ["", "## 逐条", "", "| 实体 | 字段 | 类型 | 丢弃 | 保留 |", "| --- | --- | --- | --- | --- |"]
    for item in pairs:
        summary.append(
            f"| {item['entity']} | {item['field']} | {item['pair_kind']} | "
            f"{item['dropped_claim']} | {item['kept_claim']} |"
        )
    (out_dir / "summary.md").write_text("\n".join(summary) + "\n", encoding="utf-8")


# --------------------------------------------------------------------------- #
# embedding 模式
# --------------------------------------------------------------------------- #
def _run_embedding(rows: list[sqlite3.Row], threshold: float) -> dict[str, Any]:
    import numpy as np

    from app.services.onnx_embedding import OnnxEmbeddingModel, resolve_model_dir

    model_dir = resolve_model_dir()
    print(f"[embedding] model_dir={model_dir}", flush=True)
    model = OnnxEmbeddingModel(model_dir)

    pairs: list[dict[str, Any]] = []
    claim_total = 0
    compared = 0

    for row in rows:
        claims_by_field, _ = _field_claims(row["profile_facts"])
        for field in FACT_FIELDS:
            claims = claims_by_field[field]
            claim_total += len(claims)
            if len(claims) < 2:
                continue
            vectors = np.asarray(model.encode(claims), dtype=np.float64)
            norms = np.linalg.norm(vectors, axis=1, keepdims=True)
            unit = vectors / np.clip(norms, 1e-12, None)
            sims = unit @ unit.T
            for i in range(len(claims)):
                for j in range(i + 1, len(claims)):
                    similarity = float(sims[i, j])
                    compared += 1
                    pairs.append(
                        {
                            "entity": row["name"],
                            "kind": "organization" if row["is_organization"] else "character",
                            "field": field,
                            "claim_a": claims[i],
                            "claim_b": claims[j],
                            "similarity": similarity,
                        }
                    )

    pairs.sort(key=lambda item: item["similarity"], reverse=True)
    return {
        "pairs": pairs,
        "claim_total": claim_total,
        "entity_total": len(rows),
        "compared": compared,
        "model_dir": model_dir,
    }


def _write_embedding(out_dir: Path, result: dict[str, Any], threshold: float) -> None:
    pairs = result["pairs"]
    reported = [item for item in pairs if item["similarity"] >= threshold]

    lines = ["entity\tkind\tfield\tclaim_a\tclaim_b\tsimilarity"]
    for item in reported:
        lines.append(
            "\t".join(
                [
                    _tsv_field(item["entity"]),
                    item["kind"],
                    item["field"],
                    _tsv_field(item["claim_a"]),
                    _tsv_field(item["claim_b"]),
                    f"{item['similarity']:.4f}",
                ]
            )
        )
    (out_dir / "pairs.tsv").write_text("\n".join(lines) + "\n", encoding="utf-8")

    by_threshold = {
        level: sum(1 for item in pairs if item["similarity"] >= level)
        for level in EMBEDDING_REPORT_THRESHOLDS
    }
    summary = [
        "# 档案事实去重测量：embedding 余弦相似度",
        "",
        "> 只读测量：仅 SELECT `characters.profile_facts`，未写库、未改档案。",
        "",
        f"- 模型：`paraphrase-multilingual-MiniLM-L12-v2`（ONNX），目录 `{result['model_dir']}`",
        f"- 参与测量的实体数：{result['entity_total']}",
        f"- 参与测量的 claim 数（精确去重后）：{result['claim_total']}",
        f"- 同字段内全配对数：{result['compared']}",
        f"- pairs.tsv 收录阈值：{threshold:.2f}（{len(reported)} 对）",
        "",
        "## 各阈值下的合并条数",
        "",
        "| 阈值 | 对数（= 合并条数） |",
        "| --- | --- |",
    ]
    for level in EMBEDDING_REPORT_THRESHOLDS:
        summary.append(f"| {level:.2f} | {by_threshold[level]} |")
    summary += [
        "",
        "> 对照结论（issue #201 第 2 项）：0.90 档收录的对需逐条人工核对——"
        "前序实测中该档多数是误合并（不同特质被判成重复），而人工确认过的真实重复"
        "相似度落在收录线以下，0.95 档则一条都抓不到。因此判重不走语义相似度，"
        "改用确定性嵌套规则（见 `--similarity containment`）。"
        "（逐对明细与相似度数值属本地语料，不入库，见 `.omo/`。）",
        "",
        "## 明细（相似度降序）",
        "",
        "| 实体 | 字段 | claim A | claim B | 相似度 |",
        "| --- | --- | --- | --- | --- |",
    ]
    for item in reported:
        summary.append(
            f"| {item['entity']} | {item['field']} | {item['claim_a']} | "
            f"{item['claim_b']} | {item['similarity']:.4f} |"
        )
    (out_dir / "summary.md").write_text("\n".join(summary) + "\n", encoding="utf-8")


# --------------------------------------------------------------------------- #
# 主流程
# --------------------------------------------------------------------------- #
def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    path = _db_path()
    sidecars = _sidecars(path)
    print(f"[db] {path}（只读）", flush=True)

    try:
        conn = _open_readonly(path)
        try:
            rows = _load_entities(conn)
        finally:
            conn.close()
    finally:
        # 只删本次运行新产生的 -wal/-shm；运行中服务自己的文件保持不动
        for sidecar, existed in sidecars.items():
            if not existed and sidecar.exists():
                sidecar.unlink(missing_ok=True)

    if not rows:
        print("ERROR characters.profile_facts 为空，无可测量数据", file=sys.stderr)
        return 1

    if args.similarity == "containment":
        result = _run_containment(rows)
        _write_containment(out_dir, result)
        pairs = result["pairs"]
        by_kind: Counter = Counter(item["pair_kind"] for item in pairs)
        print(
            f"SUMMARY mode=containment entities={result['entity_total']} "
            f"claims={result['claim_total']} pairs={len(pairs)} "
            f"contained={by_kind.get('contained', 0)} form_equal={by_kind.get('form_equal', 0)}"
        )
        for item in pairs:
            print(
                f"PAIR {item['entity']} | {item['field']} | {item['pair_kind']} | "
                f"{item['dropped_claim']} -> {item['kept_claim']}"
            )
    else:
        result = _run_embedding(rows, args.threshold)
        _write_embedding(out_dir, result, args.threshold)
        pairs = result["pairs"]
        counts = {
            level: sum(1 for item in pairs if item["similarity"] >= level)
            for level in EMBEDDING_REPORT_THRESHOLDS
        }
        print(
            f"SUMMARY mode=embedding entities={result['entity_total']} "
            f"claims={result['claim_total']} compared={result['compared']} "
            + " ".join(f"ge{level:.2f}={counts[level]}" for level in EMBEDDING_REPORT_THRESHOLDS)
        )
        for item in pairs:
            if item["similarity"] >= args.threshold:
                print(
                    f"PAIR {item['similarity']:.4f} | {item['entity']} | {item['field']} | "
                    f"{item['claim_a']} || {item['claim_b']}"
                )

    print(f"OUT {out_dir} pairs.tsv summary.md")
    return 0


if __name__ == "__main__":
    sys.exit(main())
