"""文笔画像一致性自检（issue #254 P2）：本地统计比较，不调用模型。

把生成文本的五项统计指标与画像 ``metrics`` 目标比较，相对偏差超过容差只产出
告警，不修改任何数据（不回写画像、不改章节）。统计口径与
``style_profile.local_stats_fallback`` 一致（句末标点切句、按去空白字符计数、
引号内字符计为对话），此处独立实现以避免服务层之间互相 import。

除五项画像指标外，另算两项只作提示的指标（``punctuation_habit`` 标点习惯、
``cliche_density`` 套话密度）：它们不进 ``METRIC_KEYS``，默认不参与告警——
现有画像 ``metrics`` 没有这些字段，强行比较只会制造噪声；画像若显式带了
``cliche_density`` 才纳入比较。

不依赖 embedding；``build_style_check_for_project`` 只用传入的数据库会话
（不 import ``app.main`` / ``memory_service``）。
"""
from __future__ import annotations

import json
import logging
import re
import statistics
from typing import Any, Optional, Sequence

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from .style_cliche_lexicon import DEFAULT_CLICHE_PHRASES
from .style_profile import is_profile_preset_id, profile_preset_id

# NOTE: 模型模块按需在函数内导入——``app.models`` 与 ``app.database`` 互相
# import，本模块被单独 import 时（如测试/脚本）顶层导入会触发循环导入失败；
# 本模块其余部分保持纯函数，不依赖数据库。

logger = logging.getLogger(__name__)

# 参与比较的指标（与 validate_style_profile 的 metrics 字段一致）
METRIC_KEYS = (
    "avg_sentence_chars",
    "median_sentence_chars",
    "short_sentence_ratio",
    "dialogue_ratio",
    "paragraph_chars_p50",
)

# 仅当画像 metrics 显式包含同名字段时才参与比较的可选指标
OPTIONAL_METRIC_KEYS = ("cliche_density",)

# 默认相对偏差容差（25%）；超过即告警
DEFAULT_TOLERANCE = 0.25
# 绝对差下限：expected 很小时避免相对偏差放大成噪声
DEFAULT_ABSOLUTE_TOLERANCE = 0.05

# 短句判定上限（含）：与画像提示词口径一致
SHORT_SENTENCE_MAX_CHARS = 15

_SENTENCE_SPLIT_RE = re.compile(r"(?<=[。！？…!?；;])")
_DIALOGUE_RE = re.compile(r"“[^”]*”|「[^」]*」|\"[^\"]*\"")
_WHITESPACE_RE = re.compile(r"\s+")

# 标点习惯：省略号先剥离（避免 ASCII 的 "..." 被重复计入句号）
_ELLIPSIS_RE = re.compile(r"…+|⋯+|\.{3,}")
_PUNCTUATION_RES = {
    "comma": re.compile(r"[，,]"),
    "period": re.compile(r"[。.]"),
    "exclaim": re.compile(r"[！!]"),
    "question": re.compile(r"[？?]"),
}
PUNCTUATION_KEYS = ("comma", "period", "exclaim", "question", "ellipsis")


def _is_number(value: Any) -> bool:
    """排除 bool（Python 里 True 会被当作数字）。"""
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _per_thousand(count: int, chars: int) -> float:
    """每千字频率；空文本记 0。"""
    if chars <= 0:
        return 0.0
    return round(count * 1000 / chars, 2)


def _punctuation_habit(raw: str, chars: int) -> dict[str, float]:
    """标点每千字频率（``……``/``...`` 连续出现算一次省略号）。"""
    without_ellipsis = _ELLIPSIS_RE.sub("", raw)
    counts = {
        key: len(pattern.findall(without_ellipsis))
        for key, pattern in _PUNCTUATION_RES.items()
    }
    counts["ellipsis"] = len(_ELLIPSIS_RE.findall(raw))
    return {key: _per_thousand(counts[key], chars) for key in PUNCTUATION_KEYS}


def _count_phrase_hits(text: str, phrases: Sequence[str]) -> int:
    """统计套话命中次数：长词优先并抹掉已匹配片段，重叠短语只计一次。"""
    ordered = sorted(
        {p.strip() for p in phrases if isinstance(p, str) and p.strip()},
        key=len,
        reverse=True,
    )
    hits = 0
    remaining = text
    for phrase in ordered:
        count = remaining.count(phrase)
        if count:
            hits += count
            # 占位符与原字符等长：保持后续匹配区间正确
            remaining = remaining.replace(phrase, "\0" * len(phrase))
    return hits


def compute_text_metrics(
    text: str,
    cliche_phrases: Optional[Sequence[str]] = None,
) -> dict[str, Any]:
    """计算文本的统计指标；空文本返回全 0（不抛）。

    返回五项画像指标（见 ``METRIC_KEYS``）加两项提示指标：
    ``punctuation_habit``（每千字标点频率）与 ``cliche_density``（每千字套话
    命中次数）。``cliche_phrases`` 为 None 时用 ``DEFAULT_CLICHE_PHRASES``。
    """
    raw = text if isinstance(text, str) else ""
    plain = _WHITESPACE_RE.sub("", raw)
    chars = len(plain)

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
    dialogue_ratio = min(1.0, dialogue_chars / chars) if chars else 0.0

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

    phrases = DEFAULT_CLICHE_PHRASES if cliche_phrases is None else cliche_phrases
    cliche_density = _per_thousand(_count_phrase_hits(raw, phrases), chars)

    return {
        "avg_sentence_chars": round(avg, 1),
        "median_sentence_chars": round(median, 1),
        "short_sentence_ratio": round(short_ratio, 2),
        "dialogue_ratio": round(dialogue_ratio, 2),
        "paragraph_chars_p50": int(round(paragraph_p50)),
        "punctuation_habit": _punctuation_habit(raw, chars),
        "cliche_density": cliche_density,
    }


def _comparable_keys(profile: dict[str, Any]) -> tuple[str, ...]:
    """可比较的指标键：固定五项 + 画像里显式存在的可选指标。"""
    expected = profile.get("metrics") if isinstance(profile, dict) else None
    if not isinstance(expected, dict):
        return METRIC_KEYS
    return METRIC_KEYS + tuple(
        key for key in OPTIONAL_METRIC_KEYS if key in expected
    )


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
    for key in _comparable_keys(profile):
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
    cliche_phrases: Optional[Sequence[str]] = None,
) -> dict[str, Any]:
    """一次性完成统计 + 比较。

    返回 ``{"metrics", "alerts", "score", "ok", "compared_metrics"}``；``score`` 为
    ``1 - 平均相对偏差``（夹在 0–1）。无可比较项（空文本/空画像）时
    ``alerts=[]``、``score=1.0``、``ok=True``、``compared_metrics=0``——调用方
    不应把该结果当作"文本与画像一致"的正向证据。
    """
    metrics = compute_text_metrics(text, cliche_phrases=cliche_phrases)
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


# ---------------------------------------------------------------------------
# 数据库辅助：项目默认画像 + 剧情分析基线（只读，不写任何数据）
# ---------------------------------------------------------------------------

async def _load_project_profile(db: AsyncSession, project_id: str) -> dict | None:
    """读取项目默认风格的画像 JSON；无画像/非本项目画像/JSON 损坏返回 None。"""
    from ..models.project_default_style import ProjectDefaultStyle
    from ..models.writing_style import WritingStyle

    style_id = (
        await db.execute(
            select(ProjectDefaultStyle.style_id).where(
                ProjectDefaultStyle.project_id == project_id
            )
        )
    ).scalar_one_or_none()
    if style_id is None:
        return None

    style = (
        await db.execute(select(WritingStyle).where(WritingStyle.id == style_id))
    ).scalar_one_or_none()
    if style is None or not style.profile_json:
        return None
    # 画像只在所属项目上下文生效（issue #254）：必须是本项目画像行
    if not is_profile_preset_id(style.preset_id):
        return None
    if style.preset_id != profile_preset_id(project_id):
        return None

    try:
        profile = json.loads(style.profile_json)
    except (TypeError, ValueError):
        return None
    return profile if isinstance(profile, dict) else None


def _opt_float(value: Any) -> Optional[float]:
    return float(value) if _is_number(value) else None


async def _load_plot_baseline(db: AsyncSession, project_id: str) -> dict[str, Any]:
    """该项目最近一章的剧情分析基线（只作对照，不参与告警）。"""
    from ..models.chapter import Chapter
    from ..models.memory import PlotAnalysis

    row = (
        await db.execute(
            select(
                PlotAnalysis.dialogue_ratio,
                PlotAnalysis.description_ratio,
                PlotAnalysis.chapter_id,
            )
            .join(Chapter, Chapter.id == PlotAnalysis.chapter_id)
            .where(PlotAnalysis.project_id == project_id)
            .order_by(Chapter.chapter_number.desc(), PlotAnalysis.created_at.desc())
            .limit(1)
        )
    ).first()
    if row is None:
        return {"dialogue_ratio": None, "description_ratio": None, "chapter_id": None}
    return {
        "dialogue_ratio": _opt_float(row[0]),
        "description_ratio": _opt_float(row[1]),
        "chapter_id": row[2],
    }


async def build_style_check_for_project(
    db: AsyncSession,
    project: Any,
    text: str,
    *,
    cliche_phrases: Optional[Sequence[str]] = None,
) -> dict[str, Any]:
    """Generation 流程用的一致性自检结果（含 ``plot_baseline`` 对照）。

    无画像返回 ``{"available": False}``；任何异常同样降级为不可用，绝不抛出
    ——调用方是生成主流程，自检失败不能阻断生成。项目归属校验由 API 层完成。
    """
    try:
        project_id = getattr(project, "id", None)
        if not project_id:
            return {"available": False}

        profile = await _load_project_profile(db, project_id)
        if profile is None:
            return {"available": False}

        result = check_text_against_profile(
            text, profile, cliche_phrases=cliche_phrases
        )
        try:
            plot_baseline = await _load_plot_baseline(db, project_id)
        except Exception:  # 基线只是对照，取不到不影响主结果
            logger.warning("读取剧情分析基线失败", exc_info=True)
            plot_baseline = {
                "dialogue_ratio": None,
                "description_ratio": None,
                "chapter_id": None,
            }
        return {"available": True, **result, "plot_baseline": plot_baseline}
    except Exception:
        logger.warning("文笔一致性自检失败，降级为不可用", exc_info=True)
        return {"available": False}
