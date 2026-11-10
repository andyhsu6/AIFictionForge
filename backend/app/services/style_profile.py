"""文笔风格画像（issue #254）：抽样、校验、渲染与本地统计兜底。

本模块只处理纯数据结构，不触碰数据库会话；导入服务负责 AI 调用与落库。
画像通过 ``WritingStyle`` 承载：``preset_id = "profile:<project_id>"``，
``style_type = "profile"``（用户编辑后为 ``"profile_edited"``）。
"""
from __future__ import annotations

import json
import re
import statistics
from dataclasses import dataclass, field
from typing import Any, Optional

# ---- 项目归属标记（API/导出/清理统一使用，勿在别处硬编码前缀） ----
PROFILE_PRESET_PREFIX = "profile:"
PROFILE_STYLE_TYPE = "profile"
PROFILE_EDITED_STYLE_TYPE = "profile_edited"

# 输出约束（与提示词模板、校验器保持一致）
PROFILE_JSON_MAX_CHARS = 1500
DO_DONT_MIN = 3
DO_DONT_MAX = 6
DO_DONT_ITEM_MAX_CHARS = 40

# 抽样预算
SAMPLE_CHAR_BUDGET = 10000
SAMPLE_PER_CHAPTER_BUDGET = 2000
SAMPLE_MIN_CHAPTERS = 4
SAMPLE_MAX_CHAPTERS = 6
# 少于该字数视为样本不足：不生成画像（避免本地统计默认值顶掉项目默认风格）
SAMPLE_MIN_CHARS = 200

# 空泛形容词黑名单：命中且不含任何量化信息时判为不可观察要求
VAGUE_ADJECTIVES = (
    "优美",
    "生动",
    "流畅",
    "细腻",
    "华丽",
    "幽默",
    "紧张",
    "吸引人",
    "引人入胜",
    "精彩",
    "出色",
    "自然",
    "简洁",
    "丰富",
    "有感染力",
    # P2：词表外的高频空泛评价词（同样只在不带量化信息时判失败）
    "漂亮",
    "好看",
    "高级",
    "有张力",
    "有画面感",
    "节奏感强",
    "语言功底好",
    "文笔好",
    "富有感染力",
    "真实",
    "立体",
    "鲜明",
)
_QUANTIFIER_RE = re.compile(r"\d|[≤≥><=%]|每|不超|至少|至多|最多|最少|约|倍|控制在")

# 原文复述检测：N 字滑窗（去空白/标点后比较），窗口越短越易误报
SOURCE_OVERLAP_MIN_WINDOW = 24

# 画像来源白名单（缺省允许；写入方只应使用这三个值）
PROFILE_SOURCES = ("model", "local", "user_edited")

_SENTENCE_END_CHARS = "。！？…!?；;"
_SENTENCE_SPLIT_RE = re.compile(r"(?<=[。！？…!?；;])")
_DIALOGUE_RE = re.compile(r"“[^”]*”|「[^」]*」|\"[^\"]*\"")
_QUOTE_RE = re.compile(r"“[^”]{21,}”|「[^」]{21,}」")
_NON_WORD_RE = re.compile(r"[\s\W_]+")

_UNDETERMINED = "未识别（以样本统计为准）"


@dataclass
class StyleSample:
    """抽样结果：正文只用于本次 AI 调用，不落库、不写日志。"""

    text: str
    meta: dict[str, Any] = field(default_factory=dict)


def profile_preset_id(project_id: str) -> str:
    """项目画像的 preset_id（长度：8 + UUID 36 = 44，符合列宽 50）。"""
    return f"{PROFILE_PRESET_PREFIX}{project_id}"


def is_profile_preset_id(preset_id: Optional[str]) -> bool:
    """按前缀判定画像行（不依赖 style_type：用户编辑后类型会变）。"""
    return isinstance(preset_id, str) and preset_id.startswith(PROFILE_PRESET_PREFIX)


def _cut_positions(text: str) -> list[int]:
    """段落/句子边界（偏移=边界之后的位置），用于把窗口对齐到完整段落。"""
    cuts = {0, len(text)}
    for index, char in enumerate(text):
        if char == "\n" or char in _SENTENCE_END_CHARS:
            cuts.add(index + 1)
    return sorted(cuts)


def _snap_to_cut(cuts: list[int], target: int, lo: int, hi: int, max_distance: int = 0) -> int:
    """在 [lo, hi] 内取距 target 最近的边界。

    没有边界、或最近边界距离超过 max_distance 时退回 target：无句末标点/无换行
    的长文本不能被整体丢弃，只能近似按预算硬切（max_distance=0 表示不限制）。
    """
    best: Optional[int] = None
    best_distance: Optional[int] = None
    for cut in cuts:
        if cut < lo or cut > hi:
            continue
        distance = abs(cut - target)
        if best_distance is None or distance < best_distance:
            best, best_distance = cut, distance
    if best is None:
        return max(lo, min(target, hi))
    if max_distance and best_distance is not None and best_distance > max_distance:
        return max(lo, min(target, hi))
    return best


def _chapter_windows(content: str, per_chapter_budget: int) -> list[tuple[int, int]]:
    """单章切窗口：不超预算取整章；超预算取头/中/尾三段，边界对齐完整段落。"""
    length = len(content)
    if length <= per_chapter_budget:
        return [(0, length)]

    cuts = _cut_positions(content)
    window = max(1, per_chapter_budget // 3)
    head_end = _snap_to_cut(cuts, window, 1, length, max_distance=window)
    middle_start = _snap_to_cut(
        cuts, length // 2 - window // 2, 0, length, max_distance=window
    )
    middle_end = _snap_to_cut(
        cuts, length // 2 + window // 2, middle_start + 1, length, max_distance=window
    )
    tail_start = _snap_to_cut(cuts, length - window, 0, length - 1, max_distance=window)

    merged: list[tuple[int, int]] = []
    for start, end in sorted([(0, head_end), (middle_start, middle_end), (tail_start, length)]):
        if start >= end:
            continue
        if merged and start <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], end))
        else:
            merged.append((start, end))

    total = sum(end - start for start, end in merged)
    if total > per_chapter_budget:
        limited: list[tuple[int, int]] = []
        remaining = per_chapter_budget
        for start, end in merged:
            if remaining <= 0:
                break
            take = min(end - start, remaining)
            limited.append((start, start + take))
            remaining -= take
        merged = limited
    return merged


def build_corpus_sample(chapters: list[Any]) -> StyleSample:
    """把传入章节的**全部正文**拼成一份样本，只用于本地统计（不送给模型）。

    抽样（`build_style_sample`）的意义是控制模型输入的 token 成本；统计口径没有
    这个约束。用 5 章抽样算出的句长/对话占比会被样本的偶然分布放大——实测同一本书
    抽样得对话占比 0.45，全书实测 0.23——所以 `metrics` 一律按全量正文统计，
    抽样只服务于模型的质性字段（issue #279 后续）。
    """
    valid = [
        chapter
        for chapter in (chapters or [])
        if str(getattr(chapter, "content", None) or "").strip()
    ]
    valid.sort(key=lambda chapter: getattr(chapter, "chapter_number", 0))
    parts: list[str] = []
    numbers: list[int] = []
    used = 0
    for chapter in valid:
        content = str(getattr(chapter, "content") or "")
        parts.append(content)
        used += len(content)
        try:
            numbers.append(int(getattr(chapter, "chapter_number", 0)))
        except (TypeError, ValueError):
            continue
    return StyleSample(
        text="\n\n".join(parts),
        meta={
            "chapter_numbers": numbers,
            "ranges": [],
            "char_count": used,
            "strategy": "corpus",
            "tail": False,
            "truncated": False,
        },
    )


def build_style_sample(
    chapters: list[Any],
    *,
    mode: str = "full",
    char_budget: int = SAMPLE_CHAR_BUDGET,
    per_chapter_budget: int = SAMPLE_PER_CHAPTER_BUDGET,
) -> StyleSample:
    """分层抽样：开头 1-2 章、中部 1-2 章、结尾 1 章；单章超预算取头/中/尾完整段落窗口。"""
    valid = [
        chapter
        for chapter in (chapters or [])
        if str(getattr(chapter, "content", None) or "").strip()
    ]
    valid.sort(key=lambda chapter: getattr(chapter, "chapter_number", 0))

    total = len(valid)
    if total == 0:
        return StyleSample(
            text="",
            meta={
                "chapter_numbers": [],
                "ranges": [],
                "char_count": 0,
                "strategy": "all",
                "tail": mode == "tail",
                "truncated": False,
            },
        )

    if total <= SAMPLE_MIN_CHAPTERS:
        selected = list(valid)
        strategy = "single" if total == 1 else "all"
    else:
        indexes = {0, 1, total // 2 - 1, total // 2, total - 1}
        selected = [valid[index] for index in sorted(i for i in indexes if 0 <= i < total)]
        strategy = "layered"

    parts: list[str] = []
    ranges: list[dict[str, int]] = []
    chapter_numbers: list[int] = []
    used = 0
    truncated = False
    stopped = False

    for chapter in selected:
        content = getattr(chapter, "content")
        number = int(getattr(chapter, "chapter_number", 0))
        if len(content) > per_chapter_budget:
            truncated = True
        for start, end in _chapter_windows(content, per_chapter_budget):
            chunk = content[start:end]
            if not chunk.strip():
                continue
            if used + len(chunk) > char_budget:
                truncated = True
                stopped = True
                break
            parts.append(chunk)
            used += len(chunk)
            ranges.append({"chapter": number, "start": start, "end": end, "chars": len(chunk)})
            if number not in chapter_numbers:
                chapter_numbers.append(number)
        if stopped:
            break

    return StyleSample(
        text="\n\n".join(parts),
        meta={
            "chapter_numbers": chapter_numbers,
            "ranges": ranges,
            "char_count": used,
            "strategy": strategy,
            "tail": mode == "tail",
            "truncated": truncated,
        },
    )


def _require_dict(value: Any, name: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError(f"{name} 必须是对象")
    return value


def _require_text(mapping: dict[str, Any], key: str, name: str) -> str:
    value = mapping.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name}.{key} 必须是非空字符串")
    return value


def _is_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _require_number(value: Any, name: str, low: float, high: Optional[float] = None) -> float:
    if not _is_number(value):
        raise ValueError(f"{name} 必须是数字")
    if value < low or (high is not None and value > high):
        if high is None:
            raise ValueError(f"{name} 必须 >= {low}")
        raise ValueError(f"{name} 必须在 {low}..{high} 之间")
    return float(value)


def _check_do_dont(items: Any, name: str) -> list[str]:
    if not isinstance(items, list):
        raise ValueError(f"{name} 必须是数组")
    if not DO_DONT_MIN <= len(items) <= DO_DONT_MAX:
        raise ValueError(f"{name} 必须包含 {DO_DONT_MIN}-{DO_DONT_MAX} 条")
    cleaned: list[str] = []
    for item in items:
        if not isinstance(item, str) or not item.strip():
            raise ValueError(f"{name} 的条目必须是非空字符串")
        text = item.strip()
        if len(text) > DO_DONT_ITEM_MAX_CHARS:
            raise ValueError(f"{name} 的条目不得超过 {DO_DONT_ITEM_MAX_CHARS} 字：{text}")
        if any(word in text for word in VAGUE_ADJECTIVES) and not _QUANTIFIER_RE.search(text):
            raise ValueError(f"{name} 的条目不可观察（只有空泛形容词）：{text}")
        # 只硬拒"空泛形容词且无量化"；具体写作指导（如"情绪用动作外化：攥拳"）
        # 不应因不在动词白名单而被判失败（真实输出验证发现的持续误伤）。
        cleaned.append(text)
    return cleaned


def _check_no_quotes(value: Any, path: str = "root") -> None:
    if isinstance(value, str):
        if _QUOTE_RE.search(value):
            raise ValueError(f"{path} 疑似包含原文引文")
        return
    if isinstance(value, dict):
        for key, item in value.items():
            if key == "source":
                # 来源标记必须是受控枚举；任何层级的 source 都不允许承载自由文本
                if item not in PROFILE_SOURCES:
                    raise ValueError(
                        f"{path}.source 必须是 {PROFILE_SOURCES} 之一"
                    )
                continue
            _check_no_quotes(item, f"{path}.{key}")
        return
    if isinstance(value, list):
        for index, item in enumerate(value):
            _check_no_quotes(item, f"{path}[{index}]")


def _normalize_for_overlap(text: str) -> str:
    """去掉空白/标点/下划线，保留文字与数字，降低断句差异带来的漏检。"""
    return _NON_WORD_RE.sub("", text or "")


def _overlap_windows(text: str, window: int) -> set[str]:
    normalized = _normalize_for_overlap(text)
    if len(normalized) < window:
        return set()
    return {normalized[index : index + window] for index in range(len(normalized) - window + 1)}


def detect_source_overlap(
    data: Any,
    sampled_text: str,
    *,
    min_window: int = SOURCE_OVERLAP_MIN_WINDOW,
) -> list[str]:
    """检测画像 JSON 中是否复述了样本原文（N 字连续片段命中）。

    返回命中路径（如 ``["do[1]"]``），空列表表示未命中；空样本直接返回空列表。
    只做连续片段比对，不含语义近似/改写检测（长句改写仍需人工抽检）。
    """
    if not isinstance(sampled_text, str) or not sampled_text.strip():
        return []
    window = max(1, int(min_window))
    sample_windows = _overlap_windows(sampled_text, window)
    if not sample_windows:
        return []

    hits: list[str] = []

    def _walk(value: Any, path: str) -> None:
        if isinstance(value, str):
            if _overlap_windows(value, window) & sample_windows:
                hits.append(path or "root")
            return
        if isinstance(value, dict):
            for key, item in value.items():
                if key == "source" and item in PROFILE_SOURCES:
                    continue
                _walk(item, f"{path}.{key}" if path else str(key))
            return
        if isinstance(value, list):
            for index, item in enumerate(value):
                _walk(item, f"{path}[{index}]")

    _walk(data, "")
    return hits


def validate_style_profile(data: Any, *, sampled_text: Optional[str] = None) -> None:
    """校验模型输出的画像 JSON；不合法抛 ValueError（可重试）。

    ``sampled_text`` 可选：传入时额外做原文复述检测（N 字滑窗重叠）。不传则
    行为与旧版一致（兼容既有调用/本地兜底产物）。
    """
    if not isinstance(data, dict):
        raise ValueError("画像必须是 JSON 对象")

    source = data.get("source")
    if source is not None and source not in PROFILE_SOURCES:
        raise ValueError(f"source 只能是 {'/'.join(PROFILE_SOURCES)}")

    version = data.get("version")
    if not isinstance(version, int) or isinstance(version, bool) or version != 1:
        raise ValueError("version 必须为整数 1")

    _require_number(data.get("confidence"), "confidence", 0, 1)

    metrics = _require_dict(data.get("metrics"), "metrics")
    _require_number(
        metrics.get("avg_sentence_chars"), "metrics.avg_sentence_chars", 0, 200
    )
    if metrics.get("avg_sentence_chars") <= 0:
        raise ValueError("metrics.avg_sentence_chars 必须大于 0")
    _require_number(
        metrics.get("median_sentence_chars"), "metrics.median_sentence_chars", 0, 200
    )
    if metrics.get("median_sentence_chars") <= 0:
        raise ValueError("metrics.median_sentence_chars 必须大于 0")
    _require_number(metrics.get("short_sentence_ratio"), "metrics.short_sentence_ratio", 0, 1)
    _require_number(metrics.get("dialogue_ratio"), "metrics.dialogue_ratio", 0, 1)
    _require_number(metrics.get("paragraph_chars_p50"), "metrics.paragraph_chars_p50", 0)

    narrative = _require_dict(data.get("narrative"), "narrative")
    for key in ("pov", "tense", "distance"):
        _require_text(narrative, key, "narrative")

    diction = _require_dict(data.get("diction"), "diction")
    for key in ("register", "concreteness", "idiom_density"):
        _require_text(diction, key, "diction")

    dialogue = _require_dict(data.get("dialogue"), "dialogue")
    for key in ("attribution", "tag_density"):
        _require_text(dialogue, key, "dialogue")

    rhetoric = data.get("rhetoric")
    if not isinstance(rhetoric, list):
        raise ValueError("rhetoric 必须是数组")
    if len(rhetoric) > 8:
        raise ValueError("rhetoric 不得超过 8 条")
    for item in rhetoric:
        if not isinstance(item, str) or not item.strip():
            raise ValueError("rhetoric 的条目必须是非空字符串")

    _check_do_dont(data.get("do"), "do")
    _check_do_dont(data.get("dont"), "dont")

    evidence = data.get("evidence_chapters")
    if not isinstance(evidence, list):
        raise ValueError("evidence_chapters 必须是数组")
    for item in evidence:
        if not isinstance(item, int) or isinstance(item, bool):
            raise ValueError("evidence_chapters 只能包含整数章号")

    _check_no_quotes(data)

    if sampled_text:
        overlap_paths = detect_source_overlap(data, sampled_text)
        if overlap_paths:
            raise ValueError(f"疑似复述原文：{', '.join(overlap_paths)}")

    serialized = json.dumps(data, ensure_ascii=False)
    if len(serialized) > PROFILE_JSON_MAX_CHARS:
        raise ValueError(f"画像 JSON 不得超过 {PROFILE_JSON_MAX_CHARS} 字")


def _fmt_number(value: float) -> str:
    if isinstance(value, float):
        return f"{value:.2f}".rstrip("0").rstrip(".")
    return str(value)


def _fmt_percent(ratio: float) -> str:
    return f"{_fmt_number(round(float(ratio) * 100, 1))}%"


def _chapter_span(meta: Optional[dict[str, Any]]) -> str:
    if not meta:
        return ""
    numbers = [
        int(number)
        for number in (meta.get("chapter_numbers") or [])
        if isinstance(number, int) and not isinstance(number, bool)
    ]
    if not numbers:
        return ""
    numbers = sorted(set(numbers))
    runs: list[tuple[int, int]] = []
    for number in numbers:
        if runs and number == runs[-1][1] + 1:
            runs[-1] = (runs[-1][0], number)
        else:
            runs.append((number, number))
    return "、".join(
        f"{start}–{end}" if start != end else str(start) for start, end in runs
    )


def _render_zh(data: dict[str, Any], meta: Optional[dict[str, Any]], title: str) -> str:
    metrics = data.get("metrics") or {}
    narrative = data.get("narrative") or {}
    diction = data.get("diction") or {}
    dialogue = data.get("dialogue") or {}
    rhetoric = data.get("rhetoric") or []

    heading = f"本书文笔画像（{title.strip()}）" if title.strip() else "本书文笔画像"
    span = _chapter_span(meta)
    char_count = (meta or {}).get("char_count")
    if span and isinstance(char_count, int):
        basis = f"依据本书原文样本归纳，样本范围：第{span}章（共{char_count}字）"
    elif span:
        basis = f"依据本书原文样本归纳，样本范围：第{span}章"
    else:
        basis = "依据本书原文样本归纳"

    lines = [
        heading,
        "",
        basis,
        "",
        "## 叙事视角与距离",
        f"- 视角：{narrative.get('pov', '')}",
        f"- 时态：{narrative.get('tense', '')}",
        f"- 叙述距离：{narrative.get('distance', '')}",
        "",
        "## 句式与节奏",
        (
            f"- 平均句长：{_fmt_number(metrics.get('avg_sentence_chars', 0))} 字"
            f"（中位 {_fmt_number(metrics.get('median_sentence_chars', 0))} 字）"
        ),
        f"- 短句占比：{_fmt_percent(metrics.get('short_sentence_ratio', 0))}",
        f"- 段落长度中位：{_fmt_number(metrics.get('paragraph_chars_p50', 0))} 字",
        "",
        "## 用词与修辞",
        f"- 语体：{diction.get('register', '')}",
        f"- 具象程度：{diction.get('concreteness', '')}",
        f"- 成语与套语密度：{diction.get('idiom_density', '')}",
        f"- 修辞：{'、'.join(item.strip() for item in rhetoric) if rhetoric else '无明显偏好'}",
        "",
        "## 对话",
        f"- 说话人标注：{dialogue.get('attribution', '')}",
        f"- 对话标签密度：{dialogue.get('tag_density', '')}",
        f"- 对话占比：{_fmt_percent(metrics.get('dialogue_ratio', 0))}",
        "- 以上量化指标为抽样实测值；若与下方条目中的数字冲突，以量化指标为准。",
        "",
        "## 必须做到",
    ]
    lines.extend(f"{index}. {item}" for index, item in enumerate(data.get("do") or [], 1))
    lines.extend(["", "## 避免"])
    lines.extend(f"{index}. {item}" for index, item in enumerate(data.get("dont") or [], 1))
    lines.extend(
        [
            "",
            "模仿上述语感，不得复述或引用样本中的任何句子、人名或书名。",
        ]
    )
    return "\n".join(lines)


def _render_en(data: dict[str, Any], meta: Optional[dict[str, Any]], title: str) -> str:
    metrics = data.get("metrics") or {}
    narrative = data.get("narrative") or {}
    diction = data.get("diction") or {}
    dialogue = data.get("dialogue") or {}
    rhetoric = data.get("rhetoric") or []

    heading = f"Book Style Profile ({title.strip()})" if title.strip() else "Book Style Profile"
    span = _chapter_span(meta)
    char_count = (meta or {}).get("char_count")
    if span and isinstance(char_count, int):
        basis = (
            "Derived from sampled passages of this book; sample chapters: "
            f"{span} ({char_count} characters)"
        )
    elif span:
        basis = f"Derived from sampled passages of this book; sample chapters: {span}"
    else:
        basis = "Derived from sampled passages of this book"

    lines = [
        heading,
        "",
        basis,
        "",
        "## Narrative perspective and distance",
        f"- POV: {narrative.get('pov', '')}",
        f"- Tense: {narrative.get('tense', '')}",
        f"- Narrative distance: {narrative.get('distance', '')}",
        "",
        "## Sentence rhythm",
        (
            f"- Average sentence length: {_fmt_number(metrics.get('avg_sentence_chars', 0))} characters"
            f" (median {_fmt_number(metrics.get('median_sentence_chars', 0))})"
        ),
        f"- Short-sentence ratio: {_fmt_percent(metrics.get('short_sentence_ratio', 0))}",
        f"- Median paragraph length: {_fmt_number(metrics.get('paragraph_chars_p50', 0))} characters",
        "",
        "## Diction and rhetoric",
        f"- Register: {diction.get('register', '')}",
        f"- Concreteness: {diction.get('concreteness', '')}",
        f"- Idiom density: {diction.get('idiom_density', '')}",
        f"- Rhetoric: {'; '.join(item.strip() for item in rhetoric) if rhetoric else 'no clear preference'}",
        "",
        "## Dialogue",
        f"- Speaker attribution: {dialogue.get('attribution', '')}",
        f"- Dialogue tag density: {dialogue.get('tag_density', '')}",
        f"- Dialogue ratio: {_fmt_percent(metrics.get('dialogue_ratio', 0))}",
        "- The metrics above are measured from the sample; if a number below conflicts with them, the measured value wins.",
        "",
        "## Must do",
    ]
    lines.extend(f"{index}. {item}" for index, item in enumerate(data.get("do") or [], 1))
    lines.extend(["", "## Avoid"])
    lines.extend(f"{index}. {item}" for index, item in enumerate(data.get("dont") or [], 1))
    lines.extend(
        [
            "",
            "Imitate the voice above; do not reproduce or quote any sentence, name, or title from the sample.",
        ]
    )
    return "\n".join(lines)


def render_style_profile(
    data: dict[str, Any],
    meta: Optional[dict[str, Any]] = None,
    *,
    title: str = "",
    language: str = "zh",
) -> str:
    """把结构化画像确定性渲染为 prompt_content（不直接使用模型自由文本）。"""
    if language == "en":
        return _render_en(data, meta, title)
    return _render_zh(data, meta, title)


def _median(values: list[float], default: float) -> float:
    return float(statistics.median(values)) if values else default


def local_stats_fallback(sample: StyleSample) -> dict[str, Any]:
    """模型不可用/输出不合法时的本地统计兜底；返回与 validate_style_profile 兼容的 dict。"""
    text = sample.text or ""
    plain = re.sub(r"\s+", "", text)

    sentences = [part.strip() for part in _SENTENCE_SPLIT_RE.split(text) if part.strip()]
    lengths = [len(re.sub(r"\s+", "", part)) for part in sentences]
    lengths = [length for length in lengths if length > 0]
    if lengths:
        avg = sum(lengths) / len(lengths)
        median = float(statistics.median(lengths))
        short_ratio = sum(1 for length in lengths if length <= 15) / len(lengths)
    else:
        avg, median, short_ratio = 20.0, 19.0, 0.0

    dialogue_chars = sum(
        len(re.sub(r"\s+", "", match)) for match in _DIALOGUE_RE.findall(text)
    )
    dialogue_ratio = min(1.0, dialogue_chars / len(plain)) if plain else 0.0

    paragraphs = [
        re.sub(r"\s+", "", part)
        for part in re.split(r"\n\s*\n|\n", text)
        if part.strip()
    ]
    paragraph_p50 = _median([len(part) for part in paragraphs], 40.0) if paragraphs else 40.0

    chapter_numbers = [
        int(number)
        for number in ((sample.meta or {}).get("chapter_numbers") or [])
        if isinstance(number, int) and not isinstance(number, bool)
    ]

    avg_round = int(round(avg))
    low, high = max(1, avg_round - 3), max(1, avg_round + 3)

    return {
        "version": 1,
        "confidence": 0.3,
        "metrics": {
            "avg_sentence_chars": round(avg, 1),
            "median_sentence_chars": round(median, 1),
            "short_sentence_ratio": round(short_ratio, 2),
            "dialogue_ratio": round(dialogue_ratio, 2),
            "paragraph_chars_p50": int(round(paragraph_p50)),
        },
        "narrative": {"pov": _UNDETERMINED, "tense": _UNDETERMINED, "distance": _UNDETERMINED},
        "diction": {
            "register": _UNDETERMINED,
            "concreteness": _UNDETERMINED,
            "idiom_density": _UNDETERMINED,
        },
        "dialogue": {"attribution": _UNDETERMINED, "tag_density": _UNDETERMINED},
        "rhetoric": [],
        "do": [
            f"平均句长控制在 {low}–{high} 字",
            f"中位句长保持在 {int(round(median))} 字左右",
            f"对话占比约 {int(round(dialogue_ratio * 100))}%",
            f"短句（≤15 字）占比约 {int(round(short_ratio * 100))}%",
        ],
        "dont": [
            "避免连续出现超过 40 字的长句",
            f"避免段落长度长期偏离 {int(round(paragraph_p50))} 字",
            "避免无标记地切换叙述人称或时态",
        ],
        "evidence_chapters": chapter_numbers,
    }
