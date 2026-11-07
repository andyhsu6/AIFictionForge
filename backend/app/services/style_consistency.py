"""文笔画像一致性自检（issue #254 P2）：本地统计比较，不调用模型。

把生成文本的五项统计指标与画像 ``metrics`` 目标比较，相对偏差超过容差只产出
告警，不修改任何数据（不回写画像、不改章节）。统计口径与
``style_profile.local_stats_fallback`` 一致（句末标点切句、按去空白字符计数、
引号内字符计为对话），此处独立实现以避免服务层之间互相 import。

纯函数模块：不依赖数据库会话、embedding 或第三方 NLP。
"""
from __future__ import annotations

import re
import statistics
from typing import Any

# 参与比较的指标（与 validate_style_profile 的 metrics 字段一致）
METRIC_KEYS = (
    "avg_sentence_chars",
    "median_sentence_chars",
    "short_sentence_ratio",
    "dialogue_ratio",
    "paragraph_chars_p50",
)

# 默认相对偏差容差（25%）；超过即告警
DEFAULT_TOLERANCE = 0.25
# 绝对差下限：expected 很小时避免相对偏差放大成噪声
DEFAULT_ABSOLUTE_TOLERANCE = 0.05

# 短句判定上限（含）：与画像提示词口径一致
SHORT_SENTENCE_MAX_CHARS = 15

_SENTENCE_SPLIT_RE = re.compile(r"(?<=[。！？…!?；;])")
_DIALOGUE_RE = re.compile(r"“[^”]*”|「[^」]*」|\"[^\"]*\"")
_WHITESPACE_RE = re.compile(r"\s+")


def _is_number(value: Any) -> bool:
    """排除 bool（Python 里 True 会被当作数字）。"""
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def compute_text_metrics(text: str) -> dict[str, Any]:
    """计算文本的五项统计指标；空文本返回全 0（不抛）。"""
    raw = text if isinstance(text, str) else ""
    plain = _WHITESPACE_RE.sub("", raw)

    sentences = [part.strip() for part in _SENTENCE_SPLIT_RE.split(raw) if part.strip()]
    lengths = [
        len(_WHITESPACE_RE.sub("", part))
        for part in sentences
    ]
    lengths = [length for length in lengths if length > 0]

    if lengths:
        avg = sum(lengths) / len(lengths)
        median = float(statistics.median(lengths))
        short_ratio = (
            sum(1 for length in lengths if length <= SHORT_SENTENCE_MAX_CHARS)
            / len(lengths)
        )
    else:
        avg, median, short_ratio = 0.0, 0.0, 0.0

    dialogue_chars = sum(
        len(_WHITESPACE_RE.sub("", match)) for match in _DIALOGUE_RE.findall(raw)
    )
    dialogue_ratio = min(1.0, dialogue_chars / len(plain)) if plain else 0.0

    paragraphs = [
        _WHITESPACE_RE.sub("", part)
        for part in re.split(r"\n\s*\n|\n", raw)
        if part.strip()
    ]
    paragraph_p50 = (
        float(statistics.median([len(part) for part in paragraphs]))
        if paragraphs
        else 0.0
    )

    return {
        "avg_sentence_chars": round(avg, 1),
        "median_sentence_chars": round(median, 1),
        "short_sentence_ratio": round(short_ratio, 2),
        "dialogue_ratio": round(dialogue_ratio, 2),
        "paragraph_chars_p50": int(round(paragraph_p50)),
    }


def _metric_deviations(
    metrics: dict[str, Any],
    profile: dict[str, Any],
) -> list[tuple[str, float, float, float]]:
    """逐项计算相对偏差；缺字段/非数值的指标跳过。

    ``expected == 0`` 时相对偏差无定义：实际也是 0 记 0，否则记 1.0（满偏差）。
    """
    expected_metrics = profile.get("metrics") if isinstance(profile, dict) else None
    if not isinstance(expected_metrics, dict) or not isinstance(metrics, dict):
        return []

    deviations: list[tuple[str, float, float, float]] = []
    for key in METRIC_KEYS:
        expected = expected_metrics.get(key)
        actual = metrics.get(key)
        if not _is_number(expected) or not _is_number(actual):
            continue

        expected_f = float(expected)
        actual_f = float(actual)
        if expected_f == 0:
            deviation = 0.0 if actual_f == 0 else 1.0
        else:
            deviation = abs(actual_f - expected_f) / abs(expected_f)
        deviations.append((key, expected_f, actual_f, deviation))
    return deviations


def compare_with_profile(
    metrics: dict[str, Any],
    profile: dict[str, Any],
    *,
    tolerance: float = DEFAULT_TOLERANCE,
    absolute_tolerance: float = DEFAULT_ABSOLUTE_TOLERANCE,
) -> list[dict[str, Any]]:
    """比较指标与画像目标，返回超容差的告警列表（不抛）。

    同时要求相对偏差与绝对差都超阈值：避免 expected 很小时（如 0.04）
    一个微小波动就被放大成噪声告警。
    """
    alerts: list[dict[str, Any]] = []
    for key, expected, actual, deviation in _metric_deviations(metrics, profile):
        if deviation <= tolerance:
            continue
        if abs(actual - expected) <= absolute_tolerance:
            continue
        alerts.append(
            {
                "metric": key,
                "expected": expected,
                "actual": actual,
                "deviation": round(deviation, 3),
                "level": "warning",
            }
        )
    return alerts


def check_text_against_profile(
    text: str,
    profile: dict[str, Any],
    *,
    tolerance: float = DEFAULT_TOLERANCE,
) -> dict[str, Any]:
    """一次性完成统计 + 比较。

    返回 ``{"metrics", "alerts", "score", "ok", "compared_metrics"}``；``score`` 为
    ``1 - 平均相对偏差``（夹在 0–1）。无可比较项（空文本/空画像）时
    ``alerts=[]``、``score=1.0``、``ok=True``、``compared_metrics=0``——调用方
    不应把该结果当作"文本与画像一致"的正向证据。
    """
    metrics = compute_text_metrics(text)
    raw = text if isinstance(text, str) else ""
    if not raw.strip():
        return {
            "metrics": metrics,
            "alerts": [],
            "score": 1.0,
            "ok": True,
            "compared_metrics": 0,
        }

    deviations = _metric_deviations(metrics, profile)
    alerts = compare_with_profile(metrics, profile, tolerance=tolerance)

    if deviations:
        mean_deviation = sum(item[3] for item in deviations) / len(deviations)
        score = max(0.0, min(1.0, 1.0 - mean_deviation))
    else:
        score = 1.0

    return {
        "metrics": metrics,
        "alerts": alerts,
        "score": round(score, 3),
        "ok": not alerts,
        "compared_metrics": len(deviations),
    }
