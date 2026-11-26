"""outline.structure / chapter.expansion_plan 统一读取适配层测试。

覆盖四象限（只有 structure / 只有 plan / 两者都有 / 都没有）、plot_summary
以 Chapter.summary 为准（legacy 值仅兜底）、scenes 两种形态、character_focus
两种形态、占位值丢弃、统一渲染顺序，以及写入侧的键名归一与 estimated_words 补齐。
"""
import json

import pytest

from app.services.outline_plan_adapter import (
    PLACEHOLDER_VALUES,
    from_expansion_plan,
    from_structure,
    render_plan_text,
    resolve_plan,
    to_storage_plan,
)


class _Chapter:
    def __init__(self, summary=None, expansion_plan=None):
        self.summary = summary
        self.expansion_plan = expansion_plan


class _Outline:
    def __init__(self, structure=None, content=""):
        self.structure = structure
        self.content = content


# ==================== 四象限 ====================


def test_only_structure_quadrant():
    outline = _Outline(json.dumps({
        "summary": "结构概要",
        "scenes": ["城门口", "议事厅"],
        "characters": [
            "甲",
            {"name": "乙", "type": "character"},
            {"name": "某组织", "type": "organization"},
        ],
        "key_points": ["事件一", "事件二"],
        "emotion": "紧张",
        "goal": "推进主线",
    }))

    plan = resolve_plan(_Chapter(), outline, target_word_count=3200)

    assert plan["plot_summary"] == "结构概要"  # 无章节摘要时回退 structure.summary
    assert plan["key_events"] == ["事件一", "事件二"]
    assert plan["character_focus"] == ["甲", "乙"]
    assert plan["organization_focus"] == ["某组织"]
    assert plan["emotional_tone"] == "紧张"
    assert plan["narrative_goal"] == "推进主线"
    assert plan["conflict_type"] == ""
    assert plan["estimated_words"] == 3200
    assert plan["scenes"] == ["城门口", "议事厅"]


def test_only_plan_quadrant():
    chapter = _Chapter("章节摘要", json.dumps({
        "key_events": ["事件A"],
        "character_focus": [
            {"name": "甲", "type": "character"},
            {"name": "乙", "type": "organization"},
        ],
        "emotional_tone": "压抑",
        "narrative_goal": "收束支线",
        "conflict_type": "外部冲突",
        "estimated_words": 2500,
        "scenes": [{"location": "雨夜街道", "characters": ["甲"], "purpose": "相遇"}],
    }))
    outline = _Outline(json.dumps({"emotion": "欢快", "key_points": ["结构事件"]}))

    plan = resolve_plan(chapter, outline)

    assert plan["plot_summary"] == "章节摘要"
    assert plan["key_events"] == ["事件A"]  # structure 侧不参与
    assert plan["character_focus"] == ["甲"]
    assert plan["organization_focus"] == ["乙"]
    assert plan["emotional_tone"] == "压抑"
    assert plan["narrative_goal"] == "收束支线"
    assert plan["conflict_type"] == "外部冲突"
    assert plan["estimated_words"] == 2500


def test_both_sources_plan_wins():
    chapter = _Chapter("", json.dumps({"emotional_tone": "计划内情绪"}))
    outline = _Outline(json.dumps({"emotion": "结构情绪", "key_points": ["结构事件"]}))

    plan = resolve_plan(chapter, outline)

    assert plan["emotional_tone"] == "计划内情绪"
    assert plan["key_events"] == []  # 有 plan 就不再读 structure


def test_neither_quadrant_returns_empty_canonical_plan():
    assert resolve_plan(_Chapter(), None) == {
        "plot_summary": "",
        "key_events": [],
        "character_focus": [],
        "emotional_tone": "",
        "narrative_goal": "",
        "conflict_type": "",
        "estimated_words": 0,
        "scenes": [],
    }


# ==================== plot_summary 来源 ====================


def test_plot_summary_falls_back_to_chapter_summary():
    plan = resolve_plan(_Chapter("摘要列内容", json.dumps({"key_events": ["A"]})))
    assert plan["plot_summary"] == "摘要列内容"


def test_chapter_summary_wins_over_legacy_plot_summary_in_expansion_plan():
    """Chapter.summary 是唯一事实源，expansion_plan 里的旧 plot_summary 不能盖掉它。"""
    plan = resolve_plan(_Chapter("列摘要", json.dumps({"plot_summary": "计划内摘要"})))
    assert plan["plot_summary"] == "列摘要"


def test_legacy_plot_summary_is_used_when_summary_is_empty():
    plan = resolve_plan(_Chapter(None, json.dumps({"plot_summary": "计划内摘要"})))
    assert plan["plot_summary"] == "计划内摘要"
    assert from_expansion_plan({"plot_summary": "计划内摘要"})["plot_summary"] == "计划内摘要"


def test_invalid_plan_json_falls_back_to_structure():
    chapter = _Chapter("摘要", "{not json")
    outline = _Outline(json.dumps({"goal": "结构目标"}))

    plan = resolve_plan(chapter, outline)

    assert plan["plot_summary"] == "摘要"
    assert plan["narrative_goal"] == "结构目标"


def test_resolve_plan_accepts_dict_expansion_plan():
    plan = resolve_plan(_Chapter("摘要", {"key_events": ["A"]}))
    assert plan["key_events"] == ["A"]


# ==================== 字段形态兼容 ====================


def test_key_points_and_key_events_are_interchangeable():
    assert from_structure({"key_points": ["P"]})["key_events"] == ["P"]
    assert from_structure({"key_events": ["E"]})["key_events"] == ["E"]
    assert from_expansion_plan({"key_points": ["P"]})["key_events"] == ["P"]
    assert from_expansion_plan({"key_events": ["E"]})["key_events"] == ["E"]


def test_key_points_placeholder_falls_back_to_key_events():
    """key_points 只有占位值时不能挡住同内容的 key_events。"""
    assert from_structure({"key_points": ["未设定"], "key_events": ["E"]})["key_events"] == ["E"]
    assert from_expansion_plan({"key_points": ["未知"], "key_events": ["E"]})["key_events"] == ["E"]


def test_character_focus_accepts_string_and_object_forms():
    from_strings = from_expansion_plan({"character_focus": ["甲", "乙"]})
    from_objects = from_expansion_plan({"character_focus": [
        {"name": "甲", "type": "character"},
        {"name": "某组织", "type": "organization"},
    ]})

    assert from_strings["character_focus"] == ["甲", "乙"]
    assert "organization_focus" not in from_strings
    assert from_objects["character_focus"] == ["甲"]
    assert from_objects["organization_focus"] == ["某组织"]


@pytest.mark.parametrize("placeholder", sorted(PLACEHOLDER_VALUES))
def test_placeholder_values_are_dropped(placeholder):
    plan = from_expansion_plan({
        "emotional_tone": placeholder,
        "narrative_goal": placeholder,
        "conflict_type": placeholder,
        "key_events": [placeholder, "真实事件"],
        "character_focus": [placeholder, "甲"],
    })

    assert plan["emotional_tone"] == ""
    assert plan["narrative_goal"] == ""
    assert plan["conflict_type"] == ""
    assert plan["key_events"] == ["真实事件"]
    assert plan["character_focus"] == ["甲"]


# ==================== 渲染 ====================


def test_render_plan_text_handles_both_scene_shapes():
    plan = from_structure({
        "scenes": ["城门口", {"location": "议事厅", "characters": ["甲"], "purpose": "密谈"}],
    })

    text = render_plan_text(plan)

    assert "- 城门口" in text
    assert "- 议事厅（甲）：密谈" in text


def test_render_plan_text_skips_empty_fields_and_keeps_order():
    plan = from_expansion_plan({
        "key_events": ["A", "B"],
        "narrative_goal": "推进",
        "emotional_tone": "未设定",
    })

    text = render_plan_text(plan)

    assert text.index("【关键事件】") < text.index("【叙事目标】")
    assert "【情感基调】" not in text
    assert "【章节概要】" not in text
    assert "- A\n- B" in text


def test_render_plan_text_accepts_label_overrides():
    plan = from_expansion_plan({"narrative_goal": "推进"})
    text = render_plan_text(plan, {"narrative_goal": "【Goal】"})
    assert "【Goal】\n推进" in text


def test_render_plan_text_renders_character_focus_as_bullets():
    plan = from_expansion_plan({"character_focus": ["甲", "乙"]})
    text = render_plan_text(plan)
    assert "【角色焦点】\n- 甲\n- 乙" in text


# ==================== 写入侧归一 ====================


def test_to_storage_plan_drops_plot_summary_and_keeps_unknown_keys():
    stored = to_storage_plan({
        "plot_summary": "旧摘要",
        "key_points": ["A"],
        "custom_flag": True,
    })

    assert "plot_summary" not in stored
    assert stored["key_events"] == ["A"]
    assert "key_points" not in stored
    assert stored["custom_flag"] is True


def test_to_storage_plan_can_drop_unknown_keys_for_full_rewrite():
    stored = to_storage_plan({"key_events": ["A"], "title": "旧标题"}, keep_unknown=False)
    assert stored["key_events"] == ["A"]
    assert "title" not in stored


def test_to_storage_plan_fills_missing_estimated_words():
    """写入侧契约要求正数（请求模型 ge=500），缺失不能落 0。"""
    assert to_storage_plan({})["estimated_words"] == 3000
    assert to_storage_plan({"estimated_words": 0})["estimated_words"] == 3000
    assert to_storage_plan({}, default_estimated_words=1800)["estimated_words"] == 1800


def test_to_storage_plan_keeps_explicit_estimated_words():
    assert to_storage_plan({"estimated_words": 2500})["estimated_words"] == 2500


def test_read_side_does_not_fake_estimated_words():
    """读取侧保持「缺失=0」，只有写入侧补默认值。"""
    assert resolve_plan(_Chapter(None, json.dumps({"key_events": ["A"]})))["estimated_words"] == 0
    assert from_expansion_plan({"key_events": ["A"]})["estimated_words"] == 0

