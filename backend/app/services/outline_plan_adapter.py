"""大纲/章节规划统一读取适配层（纯函数，无数据库依赖）。

两种历史数据结构在这里收敛为同一份 **canonical plan**（键名取 1-N 侧）：

- `outline.structure`（1-1 模式）：scenes / characters / key_points / emotion / goal / summary
- `chapter.expansion_plan`（1-N 模式）：key_events / character_focus / emotional_tone /
  narrative_goal / conflict_type / estimated_words / scenes

规则：

- `plot_summary` 的唯一事实源是 `Chapter.summary` 列；旧数据若在 `expansion_plan` 里
  自带 `plot_summary`，只在 summary 为空时兜底，避免陈旧副本盖掉新摘要。
- `key_points` / `key_events` 互为输入别名，canonical 只保留 `key_events`。
- `characters` / `character_focus` 统一成名称字符串列表；组织名单独放进可选的
  `organization_focus`（为空时不出现该键）。
- 占位值（`未设定` / `未知` / `无` 等）一律视为空。
"""

from __future__ import annotations

import json
from typing import Any, Optional

PLACEHOLDER_VALUES = {"", "未设定", "未知", "待定", "无", "none", "n/a"}

#: 适配层认识的全部键（含输入别名），写入侧据此区分「未识别的旧键」。
PLAN_KEYS = frozenset({
    "plot_summary",
    "key_events",
    "key_points",
    "character_focus",
    "organization_focus",
    "emotional_tone",
    "narrative_goal",
    "conflict_type",
    "estimated_words",
    "scenes",
})

DEFAULT_LABELS = {
    "plot_summary": "【章节概要】",
    "key_events": "【关键事件】",
    "character_focus": "【角色焦点】",
    "scenes": "【场景设定】",
    "emotional_tone": "【情感基调】",
    "narrative_goal": "【叙事目标】",
    "conflict_type": "【冲突类型】",
}


# ==================== 基础归一 ====================


def _text(value: Any) -> str:
    """占位值归一为空字符串，其余去首尾空白。"""
    if value is None:
        return ""
    text = str(value).strip()
    return "" if text.lower() in PLACEHOLDER_VALUES else text


def _text_list(value: Any) -> list[str]:
    """列表项压缩为字符串列表；字典取 name/content/title/event 之一。"""
    if not value:
        return []
    items = value if isinstance(value, (list, tuple)) else [value]
    result: list[str] = []
    for item in items:
        if isinstance(item, dict):
            candidate = (
                item.get("name")
                or item.get("content")
                or item.get("title")
                or item.get("event")
            )
        else:
            candidate = item
        text = _text(candidate)
        if text:
            result.append(text)
    return result


def _to_int(value: Any) -> int:
    try:
        return max(0, int(value))
    except (TypeError, ValueError):
        return 0


def _split_characters(value: Any) -> tuple[list[str], list[str]]:
    """拆分角色/组织名称；兼容 string[] 与 [{name, type}] 两种形态。"""
    characters: list[str] = []
    organizations: list[str] = []
    if not value:
        return characters, organizations
    items = value if isinstance(value, (list, tuple)) else [value]
    for item in items:
        if isinstance(item, dict):
            name = _text(item.get("name") or item.get("content") or item.get("title"))
            if not name:
                continue
            if item.get("type") == "organization":
                organizations.append(name)
            else:
                characters.append(name)
        else:
            name = _text(item)
            if name:
                characters.append(name)
    return characters, organizations


def _normalize_scenes(value: Any) -> list:
    """scenes 原样规整：字符串保留字符串，对象保留对象，占位字符串丢弃。"""
    if not value:
        return []
    items = value if isinstance(value, (list, tuple)) else [value]
    scenes: list = []
    for item in items:
        if isinstance(item, dict):
            scenes.append(dict(item))
        else:
            text = _text(item)
            if text:
                scenes.append(text)
    return scenes


def _finalize(
    *,
    plot_summary: Any = "",
    key_events: Any = None,
    character_focus: Any = None,
    organization_focus: Any = None,
    emotional_tone: Any = "",
    narrative_goal: Any = "",
    conflict_type: Any = "",
    estimated_words: Any = 0,
    scenes: Any = None,
) -> dict:
    plan = {
        "plot_summary": _text(plot_summary),
        "key_events": _text_list(key_events),
        "character_focus": _text_list(character_focus),
        "emotional_tone": _text(emotional_tone),
        "narrative_goal": _text(narrative_goal),
        "conflict_type": _text(conflict_type),
        "estimated_words": _to_int(estimated_words),
        "scenes": _normalize_scenes(scenes),
    }
    organizations = _text_list(organization_focus)
    if organizations:
        plan["organization_focus"] = organizations
    return plan


def _parse_plan(raw: Any) -> Optional[dict]:
    """把 JSON 字符串 / dict 解析成 dict；失败或非对象返回 None。"""
    if isinstance(raw, dict):
        return raw
    if not isinstance(raw, str) or not raw.strip():
        return None
    try:
        parsed = json.loads(raw)
    except (json.JSONDecodeError, TypeError, ValueError):
        return None
    return parsed if isinstance(parsed, dict) else None


# ==================== 两个来源 ====================


def from_structure(
    structure: Optional[dict],
    *,
    summary: str = "",
    target_word_count: int = 0,
) -> dict:
    """把 1-1 的 `outline.structure` 转成 canonical plan。"""
    data = structure if isinstance(structure, dict) else {}
    characters, organizations = _split_characters(data.get("characters"))
    return _finalize(
        # 章节摘要优先；structure 自带的 summary 仅作兜底
        plot_summary=_text(summary) or data.get("summary"),
        # key_points 是 canonical 键，占位值不算；缺失时才回退 key_events
        key_events=_text_list(data.get("key_points")) or data.get("key_events"),
        character_focus=characters,
        organization_focus=organizations,
        emotional_tone=data.get("emotion") or data.get("emotional_tone"),
        narrative_goal=data.get("goal") or data.get("narrative_goal"),
        conflict_type=data.get("conflict_type"),
        estimated_words=target_word_count,
        scenes=data.get("scenes"),
    )


def from_expansion_plan(plan: Optional[dict], *, summary: str = "") -> dict:
    """把 1-N 的 `chapter.expansion_plan` 归一成 canonical plan。

    `plot_summary` 以 `summary`（Chapter.summary）为准；旧数据自带的
    `plot_summary` 只在 summary 为空时兜底。
    """
    data = plan if isinstance(plan, dict) else {}
    characters, organizations = _split_characters(data.get("character_focus"))
    organizations = organizations or _text_list(data.get("organization_focus"))
    return _finalize(
        plot_summary=_text(summary) or data.get("plot_summary"),
        key_events=_text_list(data.get("key_events")) or data.get("key_points"),
        character_focus=characters,
        organization_focus=organizations,
        emotional_tone=data.get("emotional_tone"),
        narrative_goal=data.get("narrative_goal"),
        conflict_type=data.get("conflict_type"),
        estimated_words=data.get("estimated_words"),
        scenes=data.get("scenes"),
    )


def to_storage_plan(
    plan: Optional[dict],
    *,
    summary: str = "",
    keep_unknown: bool = True,
    default_estimated_words: int = 3000,
) -> dict:
    """写入侧归一：canonical 键名 + 保留未识别的旧键，且不落 `plot_summary`。

    `plot_summary` 的唯一来源是 `Chapter.summary` 列，写进 expansion_plan 会形成
    第二份可陈旧的副本（摘要更新后旧值仍被优先读到），因此这里剔除。

    `estimated_words` 缺失/非正数时写成 `default_estimated_words`：写入侧契约要求
    正数（请求模型 `ge=500`），而 0 是「未知」的读取态，不该落库。

    `keep_unknown=False` 用于整份重写的场景（如剧情展开生成），只保留 canonical 键。
    """
    data = plan if isinstance(plan, dict) else {}
    stored = from_expansion_plan(data, summary=summary)
    stored.pop("plot_summary", None)
    if stored.get("estimated_words", 0) <= 0:
        stored["estimated_words"] = default_estimated_words
    if keep_unknown:
        for key, value in data.items():
            if key not in PLAN_KEYS:
                stored[key] = value
    return stored


def resolve_plan(chapter: Any, outline: Any = None, target_word_count: int = 0) -> dict:
    """唯一读取入口：有 expansion_plan 用 plan，否则用 outline.structure。"""
    summary = _text(getattr(chapter, "summary", None))
    plan = _parse_plan(getattr(chapter, "expansion_plan", None))
    if plan is not None:
        return from_expansion_plan(plan, summary=summary)

    structure = _parse_plan(getattr(outline, "structure", None))
    fallback_summary = summary or _text(getattr(outline, "content", None))
    return from_structure(
        structure,
        summary=fallback_summary,
        target_word_count=target_word_count,
    )


# ==================== 渲染 ====================


def _format_scene(scene: Any) -> str:
    if isinstance(scene, dict):
        location = _text(scene.get("location"))
        characters = _text_list(scene.get("characters"))
        purpose = _text(scene.get("purpose"))
        head = location
        if characters:
            joined = "、".join(characters)
            head = f"{head}（{joined}）" if head else f"角色：{joined}"
        if purpose:
            return f"{head}：{purpose}" if head else purpose
        return head
    return _text(scene)


def render_plan_text(plan: dict, labels: Optional[dict] = None) -> str:
    """把 canonical plan 渲染成提示词文本；空字段跳过，顺序稳定。"""
    data = plan if isinstance(plan, dict) else {}
    titles = {**DEFAULT_LABELS, **(labels or {})}

    blocks: list[str] = []

    def add(key: str, body: str) -> None:
        if body:
            blocks.append(f"{titles[key]}\n{body}")

    add("plot_summary", _text(data.get("plot_summary")))
    add("key_events", "\n".join(f"- {item}" for item in _text_list(data.get("key_events"))))
    add("character_focus", "\n".join(f"- {item}" for item in _text_list(data.get("character_focus"))))
    scene_lines = [_format_scene(item) for item in _normalize_scenes(data.get("scenes"))]
    add("scenes", "\n".join(f"- {line}" for line in scene_lines if line))
    add("emotional_tone", _text(data.get("emotional_tone")))
    add("narrative_goal", _text(data.get("narrative_goal")))
    add("conflict_type", _text(data.get("conflict_type")))
    return "\n\n".join(blocks)
