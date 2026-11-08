#!/usr/bin/env python3
"""风格漂移指标报表（计划 §15）：本地统计，不调用 AI/embedding。

口径
----
- 输入：项目画像（``WritingStyle.preset_id == "profile:<project_id>"`` 的
  ``profile_json.metrics``）+ 该项目章节正文；每章用
  ``style_consistency.compute_text_metrics`` 计算指标。
- 汇总（按项目）：均值与画像目标的差、绝对差、相对偏差；``cliche_density``
  仅在画像包含该字段时比较。
- KS：画像目标是单点，无法做两样本 KS，因此改为**章级分布**对比——把选中的
  章节按顺序分两半，比较两半的句长（去空白字符数）经验分布。``mean_shift``
  = 后半均值 − 前半均值，只用于表达漂移方向（KS 本身对称、无方向）。
- 阈值：相对偏差 > 25% 或 KS > 0.2 记 warning；只输出、不回写任何数据。
- 只读：只有 SELECT，结束时统一 ``rollback()``；不 import ``app.main``。

脱敏
----
只 SELECT 章节 ``content``（不读标题/摘要），输出只有项目 id、章数与指标数值，
不会出现章节标题、正文、角色名。

用法
----
    cd backend
    PYTHONPATH=. .venv/bin/python scripts/style_drift_report.py            # 全部有画像项目
    PYTHONPATH=. .venv/bin/python scripts/style_drift_report.py --project-id <uuid>
    PYTHONPATH=. .venv/bin/python scripts/style_drift_report.py --last-n 10 --json
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any, Optional, Sequence

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

# 允许 `python scripts/style_drift_report.py` 直接运行（sys.path[0] 是 scripts/）
BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

import app.database  # noqa: E402,F401 —— 必须先于 models 导入，避免 app.models 循环导入
from app.models.chapter import Chapter  # noqa: E402
from app.models.project import Project  # noqa: E402
from app.models.writing_style import WritingStyle  # noqa: E402
from app.services.style_consistency import (  # noqa: E402
    DEFAULT_ABSOLUTE_TOLERANCE,
    DEFAULT_TOLERANCE,
    OPTIONAL_METRIC_KEYS,
    _SENTENCE_SPLIT_RE,
    _WHITESPACE_RE,
    compute_text_metrics,
)
from app.services.style_profile import profile_preset_id  # noqa: E402

# 参与汇总的画像指标（与计划 §15 一致；median 不单独汇总）
REPORT_METRIC_KEYS = (
    "avg_sentence_chars",
    "dialogue_ratio",
    "short_sentence_ratio",
    "paragraph_chars_p50",
)

# 两半句长分布的 KS 告警阈值
KS_WARNING_THRESHOLD = 0.2


def _is_number(value: Any) -> bool:
    """排除 bool（Python 里 True 会被当作数字）。"""
    return isinstance(value, (int, float)) and not isinstance(value, bool)


# ---------------------------------------------------------------------------
# 纯函数：句长分布与 KS
# ---------------------------------------------------------------------------

def sentence_lengths(text: str) -> list[int]:
    """按 ``compute_text_metrics`` 的切句口径返回每句去空白字符数。"""
    raw = text if isinstance(text, str) else ""
    lengths: list[int] = []
    for part in _SENTENCE_SPLIT_RE.split(raw):
        stripped = part.strip()
        if not stripped:
            continue
        length = len(_WHITESPACE_RE.sub("", stripped))
        if length > 0:
            lengths.append(length)
    return lengths


def two_sample_ks(sample_a: Sequence[float], sample_b: Sequence[float]) -> float:
    """两样本 Kolmogorov–Smirnov 统计量（纯 Python，值域 0–1）。

    逐唯一值累加经验 CDF；同值一次性推进两侧，避免并列时把差异放大。
    任一样本为空返回 0.0（无分布可比，不制造告警）。
    """
    a = [float(value) for value in sample_a]
    b = [float(value) for value in sample_b]
    if not a or not b:
        return 0.0

    counts_a = Counter(a)
    counts_b = Counter(b)
    na, nb = len(a), len(b)
    cum_a = cum_b = 0
    statistic = 0.0
    for value in sorted(counts_a.keys() | counts_b.keys()):
        cum_a += counts_a.get(value, 0)
        cum_b += counts_b.get(value, 0)
        statistic = max(statistic, abs(cum_a / na - cum_b / nb))
    return round(statistic, 4)


def _mean(values: Sequence[float]) -> float:
    return sum(values) / len(values) if values else 0.0


# ---------------------------------------------------------------------------
# 单项目汇总
# ---------------------------------------------------------------------------

def _metric_block(actual: float, expected: float) -> dict[str, Any]:
    """均值 vs 画像目标：差值、绝对差与相对偏差。

    expected == 0 时相对偏差无定义：实际也是 0 记 0，否则记 1.0（满偏差），
    与 ``style_consistency`` 口径一致。
    """
    diff = actual - expected
    abs_diff = abs(diff)
    if expected == 0:
        rel_dev = 0.0 if actual == 0 else 1.0
    else:
        rel_dev = abs_diff / abs(expected)
    return {
        "actual": round(actual, 2),
        "expected": round(expected, 2),
        "diff": round(diff, 2),
        "abs_diff": round(abs_diff, 2),
        # 阈值比较用未舍入值，展示才 round（避免 0.054 → 0.05 的边界误判）
        "abs_diff_raw": abs_diff,
        "rel_dev": round(rel_dev, 3),
    }


def build_project_report(
    project_id: str,
    texts: Sequence[str],
    profile_metrics: dict[str, Any],
) -> dict[str, Any]:
    """汇总单项目的均值偏差与前后半 KS（纯函数，不碰数据库）。"""
    clean_texts = [text for text in texts if isinstance(text, str) and text.strip()]
    per_chapter = [compute_text_metrics(text) for text in clean_texts]

    keys = list(REPORT_METRIC_KEYS) + [
        key for key in OPTIONAL_METRIC_KEYS if key in profile_metrics
    ]
    metrics: dict[str, dict[str, Any]] = {}
    for key in keys:
        expected = profile_metrics.get(key)
        values = [
            float(chapter[key])
            for chapter in per_chapter
            if _is_number(chapter.get(key))
        ]
        if not values or not _is_number(expected):
            continue
        metrics[key] = _metric_block(_mean(values), float(expected))

    # 章级句长分布：按顺序分两半（奇数章时后半多一章）
    sentence_lists = [sentence_lengths(text) for text in clean_texts]
    half = len(sentence_lists) // 2
    first = [length for lengths in sentence_lists[:half] for length in lengths]
    second = [length for lengths in sentence_lists[half:] for length in lengths]

    ks: Optional[dict[str, Any]] = None
    if first and second:
        ks = {
            "value": two_sample_ks(first, second),
            "mean_shift": round(_mean(second) - _mean(first), 2),
            "first_half_sentences": len(first),
            "second_half_sentences": len(second),
        }

    warnings: list[dict[str, Any]] = []
    for key, block in metrics.items():
        # 与核心一致性检查同口径：相对偏差和绝对差都必须超阈值
        if (
            block["rel_dev"] > DEFAULT_TOLERANCE
            and block.get("abs_diff_raw", block["abs_diff"]) > DEFAULT_ABSOLUTE_TOLERANCE
        ):
            warnings.append(
                {
                    "kind": "metric",
                    "metric": key,
                    "actual": block["actual"],
                    "expected": block["expected"],
                    "rel_dev": block["rel_dev"],
                }
            )
    if ks is not None and ks["value"] > KS_WARNING_THRESHOLD:
        warnings.append(
            {
                "kind": "ks",
                "value": ks["value"],
                "mean_shift": ks["mean_shift"],
            }
        )

    return {
        "project_id": project_id,
        "chapter_count": len(clean_texts),
        "metrics": metrics,
        "ks": ks,
        "warnings": warnings,
    }


# ---------------------------------------------------------------------------
# 数据加载（只读）
# ---------------------------------------------------------------------------

async def _load_profile_metrics(
    db: AsyncSession, project_id: str
) -> Optional[dict[str, Any]]:
    """按 ``profile:<project_id>`` 读取画像 metrics；缺失/损坏返回 None。"""
    profile_json = (
        await db.execute(
            select(WritingStyle.profile_json)
            .where(WritingStyle.preset_id == profile_preset_id(project_id))
            .order_by(WritingStyle.id.desc())
            .limit(1)
        )
    ).scalar_one_or_none()
    if not profile_json:
        return None
    try:
        profile = json.loads(profile_json)
    except (TypeError, ValueError):
        return None
    metrics = profile.get("metrics") if isinstance(profile, dict) else None
    return metrics if isinstance(metrics, dict) and metrics else None


async def _load_chapter_texts(
    db: AsyncSession, project_id: str, last_n: Optional[int]
) -> list[str]:
    """按章号取正文（**不读标题/摘要**，从数据层杜绝标题泄漏）。

    ``last_n`` 只保留最近 N 个正文非空的章节。
    """
    rows = (
        await db.execute(
            select(Chapter.content)
            .where(Chapter.project_id == project_id)
            .order_by(Chapter.chapter_number, Chapter.id)
        )
    ).scalars().all()
    texts = [content for content in rows if isinstance(content, str) and content.strip()]
    if last_n is not None:
        texts = texts[-last_n:]
    return texts


async def build_report(
    db: AsyncSession,
    *,
    project_id: Optional[str] = None,
    last_n: Optional[int] = None,
    limit: Optional[int] = None,
) -> dict[str, Any]:
    """扫描项目并返回报表；无画像/无正文的项目进 ``skipped``。"""
    query = select(Project.id).order_by(Project.id)
    if project_id:
        query = query.where(Project.id == project_id)
    if limit is not None:
        query = query.limit(limit)
    project_ids = list((await db.execute(query)).scalars().all())

    report: dict[str, Any] = {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "thresholds": {
            "rel_dev": DEFAULT_TOLERANCE,
            "ks": KS_WARNING_THRESHOLD,
        },
        "projects": [],
        "skipped": [],
    }

    for pid in project_ids:
        profile_metrics = await _load_profile_metrics(db, pid)
        if profile_metrics is None:
            report["skipped"].append({"project_id": pid, "reason": "no_profile"})
            continue
        texts = await _load_chapter_texts(db, pid, last_n)
        if not texts:
            report["skipped"].append({"project_id": pid, "reason": "no_chapters"})
            continue
        report["projects"].append(build_project_report(pid, texts, profile_metrics))

    return report


# ---------------------------------------------------------------------------
# 输出
# ---------------------------------------------------------------------------

def render_text(report: dict[str, Any]) -> str:
    """文本表：只有项目 id、章数与指标数值（无标题/正文）。"""
    lines: list[str] = [
        "style drift report",
        f"generated_at: {report['generated_at']}",
        (
            "thresholds: rel_dev > "
            f"{report['thresholds']['rel_dev']:.0%} or ks > "
            f"{report['thresholds']['ks']}"
        ),
        f"projects: {len(report['projects'])}, skipped: {len(report['skipped'])}",
    ]

    for project in report["projects"]:
        lines.append("")
        lines.append(
            f"[{project['project_id']}] chapters={project['chapter_count']}"
        )
        lines.append(f"  {'metric':<24}{'actual':>10}{'expected':>10}{'diff':>10}{'rel_dev':>10}")
        for key, block in project["metrics"].items():
            lines.append(
                f"  {key:<24}{block['actual']:>10}{block['expected']:>10}"
                f"{block['diff']:>10}{block['rel_dev']:>10.1%}"
            )

        ks = project["ks"]
        if ks is None:
            lines.append("  ks: n/a (need >= 2 chapters with text)")
        else:
            lines.append(
                f"  ks(sentence-length halves): {ks['value']:.3f}, "
                f"mean_shift: {ks['mean_shift']:+.2f}"
            )

        if project["warnings"]:
            parts = []
            for warning in project["warnings"]:
                if warning["kind"] == "metric":
                    parts.append(
                        f"{warning['metric']}(rel_dev={warning['rel_dev']:.1%})"
                    )
                else:
                    parts.append(f"ks={warning['value']:.3f}")
            lines.append("  warnings: " + ", ".join(parts))
        else:
            lines.append("  warnings: none")

    if report["skipped"]:
        lines.append("")
        lines.append("skipped:")
        for item in report["skipped"]:
            lines.append(f"  [{item['project_id']}] {item['reason']}")

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

class _ArgumentParser(argparse.ArgumentParser):
    """参数错误按约定返回 1（argparse 默认是 2）。"""

    def error(self, message: str) -> None:  # type: ignore[override]
        self.print_usage(sys.stderr)
        self.exit(1, f"{self.prog}: error: {message}\n")


def _parse_args(argv: Optional[list[str]] = None) -> argparse.Namespace:
    parser = _ArgumentParser(
        description="按项目汇总生成文本与文笔画像的偏差（本地统计，不调用模型）",
    )
    parser.add_argument(
        "--project-id",
        default=None,
        help="只统计指定项目；缺省 = 所有带画像的项目",
    )
    parser.add_argument(
        "--last-n",
        type=int,
        default=None,
        help="只取最近 N 个正文非空章节参与统计",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        dest="as_json",
        help="输出 JSON（默认文本表）",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=None,
        help="最多扫描 N 个项目",
    )
    args = parser.parse_args(argv)

    if args.last_n is not None and args.last_n <= 0:
        parser.error("--last-n 必须为正整数")
    if args.limit is not None and args.limit <= 0:
        parser.error("--limit 必须为正整数")
    return args


def _build_engine():
    import app.config as app_config

    db_url = app_config.settings.database_url
    engine_args: dict[str, Any] = {"future": True}
    if "sqlite" in db_url.lower():
        engine_args["connect_args"] = {"check_same_thread": False, "timeout": 30.0}
    return create_async_engine(db_url, **engine_args)


async def _run(args: argparse.Namespace) -> int:
    engine = _build_engine()
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with session_factory() as db:
            try:
                report = await build_report(
                    db,
                    project_id=args.project_id,
                    last_n=args.last_n,
                    limit=args.limit,
                )
            finally:
                # 只读承诺：无论成功失败都不提交
                await db.rollback()
    finally:
        await engine.dispose()

    if args.as_json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        print(render_text(report))
    return 0


def main(argv: Optional[list[str]] = None) -> int:
    args = _parse_args(argv)
    try:
        return asyncio.run(_run(args))
    except Exception as exc:  # 参数之外的问题一律退出码 1
        print(f"ERROR {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
