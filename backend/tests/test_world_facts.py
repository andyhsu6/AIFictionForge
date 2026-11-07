"""issue #242 P1：世界设定事实（world_facts）的构建、合并与读取。"""
import json

from app.services.world_facts import (
    build_world_facts,
    load_world_facts,
    merge_world_facts,
    normalize_world_facts,
    render_world_facts,
    validate_world_facts_claims,
)


def _world_data():
    return {
        "time_period": "占位纪元第七年",
        "location": "占位城",
        "atmosphere": "原文未提及",
        "rules": "占位议会规定",
        "evidence": {
            "time_period": [{"chapter": 1, "quote": "占位战争结束为元年"}],
            "location": [{"chapter": 1, "quote": "占位城由占位议会统治"}],
            "rules": [{"chapter": 2, "quote": "所有外来者必须登记"}, {"chapter": 2, "quote": ""}],
            "atmosphere": [],
        },
    }


def test_build_world_facts_keeps_valid_evidence_only():
    facts = build_world_facts(
        _world_data(),
        {
            "world_time_period": "占位纪元第七年",
            "world_location": "占位城",
            "world_rules": "占位议会规定",
        },
    )
    assert [fact["field"] for fact in facts] == [
        "world_time_period",
        "world_location",
        "world_rules",
    ]
    rules_fact = facts[-1]
    assert rules_fact["source"] == "original"
    assert rules_fact["evidence"] == [{"chapter": 2, "quote": "所有外来者必须登记"}]


def test_merge_world_facts_dedupes_by_field_and_text():
    first = [{"field": "world_rules", "text": "旧规则", "evidence": [], "source": "original"}]
    second = [
        {"field": "world_rules", "text": "旧规则", "evidence": [], "source": "original"},
        {"field": "world_rules", "text": "新规则", "evidence": [], "source": "original"},
    ]
    merged = merge_world_facts(first, second)
    assert [fact["text"] for fact in merged] == ["旧规则", "新规则"]


def test_merge_world_facts_appends_new_evidence_for_same_fact():
    first = [
        {
            "field": "world_rules",
            "text": "占位规则",
            "evidence": [{"chapter": 1, "quote": "占位原文一"}],
            "source": "original",
        }
    ]
    second = [
        {
            "field": "world_rules",
            "text": "占位规则",
            "evidence": [
                {"chapter": 1, "quote": "占位原文一"},
                {"chapter": 6, "quote": "占位原文六"},
            ],
            "source": "original",
        }
    ]
    merged = merge_world_facts(first, second)
    assert len(merged) == 1
    assert merged[0]["evidence"] == [
        {"chapter": 1, "quote": "占位原文一"},
        {"chapter": 6, "quote": "占位原文六"},
    ]


def test_build_world_facts_filters_invalid_evidence_entries():
    data = {
        "time_period": "占位纪元第七年",
        "evidence": {
            "time_period": [
                {"chapter": "1", "quote": "章节号不是整数"},
                {"chapter": 1, "quote": "占位战争结束为元年"},
                "junk",
                {"chapter": 2, "quote": ""},
            ]
        },
    }
    facts = build_world_facts(data, {"world_time_period": "占位纪元第七年"})
    assert facts[0]["evidence"] == [{"chapter": 1, "quote": "占位战争结束为元年"}]


def test_validate_claims_keeps_only_verbatim_evidence():
    chapters = {1: "占位城由占位议会统治。占位战争结束为元年。"}
    claims = [
        {
            "field": "world_rules",
            "text": "占位议会统治占位城",
            "evidence": [{"chapter": 1, "quote": "占位城由占位议会统治"}],
        },
        {
            "field": "world_rules",
            "text": "自造事实",
            "evidence": [{"chapter": 1, "quote": "占位城外有黑森林"}],
        },
        {
            "field": "world_rules",
            "text": "无证据事实",
            "evidence": [],
        },
        {
            "field": "unknown_field",
            "text": "字段不合法",
            "evidence": [{"chapter": 1, "quote": "占位城由占位议会统治"}],
        },
        {
            "field": "world_rules",
            "text": "章号越界",
            "evidence": [{"chapter": 99, "quote": "占位城由占位议会统治"}],
        },
    ]
    valid, rejected = validate_world_facts_claims(claims, chapters)
    assert rejected == 4
    assert len(valid) == 1
    assert valid[0]["text"] == "占位议会统治占位城"
    assert valid[0]["evidence"] == [{"chapter": 1, "quote": "占位城由占位议会统治"}]
    assert valid[0]["source"] == "original"


def test_validate_claims_tolerates_non_list_input():
    assert validate_world_facts_claims(None, {}) == ([], 0)
    assert validate_world_facts_claims({"facts": []}, {}) == ([], 0)


def test_build_world_facts_splits_sentences_and_render_is_lossless():
    text = "占位纪元第七年，占位城重建。占位议会统治占位城。"
    facts = build_world_facts(
        {
            "time_period": text,
            "evidence": {"time_period": [{"chapter": 1, "quote": "占位城重建"}]},
        },
        {"world_time_period": text},
    )
    assert [fact["text"] for fact in facts] == [
        "占位纪元第七年，占位城重建。",
        "占位议会统治占位城。",
    ]
    assert facts[0]["evidence"] == [{"chapter": 1, "quote": "占位城重建"}]
    assert facts[1]["evidence"] == [{"chapter": 1, "quote": "占位城重建"}]
    assert render_world_facts(facts)["world_time_period"] == text


def test_render_joins_unpunctuated_claims_with_separator():
    facts = [
        {"field": "world_rules", "text": "占位规则一", "evidence": [], "source": "original"},
        {"field": "world_rules", "text": "占位规则二。", "evidence": [], "source": "original"},
        {"field": "world_rules", "text": "占位规则三", "evidence": [], "source": "original"},
    ]
    rendered = render_world_facts(facts)
    assert rendered["world_rules"] == "占位规则一；占位规则二。占位规则三"


def test_render_world_facts_groups_and_deduplicates():
    facts = [
        {"field": "world_rules", "text": "占位规则一", "evidence": [], "source": "original"},
        {"field": "world_rules", "text": "占位规则一", "evidence": [], "source": "original"},
        {"field": "world_rules", "text": "占位规则二", "evidence": [], "source": "original"},
        {"field": "world_location", "text": "占位城", "evidence": [], "source": "original"},
        {"field": "unknown_field", "text": "忽略", "evidence": [], "source": "original"},
    ]
    rendered = render_world_facts(facts)
    assert rendered["world_rules"] == "占位规则一；占位规则二"
    assert rendered["world_location"] == "占位城"
    assert "unknown_field" not in rendered


def test_merge_normalizes_legacy_whole_field_facts():
    """旧版整段 fact 与新原子 claim 合并时先归一化，渲染不重复同一句。"""
    legacy = [
        {
            "field": "world_rules",
            "text": "占位规则一。占位规则二。",
            "evidence": [],
            "source": "original",
        }
    ]
    atomic = [
        {
            "field": "world_rules",
            "text": "占位规则一。",
            "evidence": [],
            "source": "original",
        }
    ]
    merged = merge_world_facts(legacy, atomic)
    assert [fact["text"] for fact in merged] == ["占位规则一。", "占位规则二。"]
    assert render_world_facts(merged)["world_rules"] == "占位规则一。占位规则二。"


def test_split_treats_ellipsis_as_sentence_end():
    facts = build_world_facts(
        {"time_period": "占位规则一…占位规则二。"},
        {"world_time_period": "占位规则一…占位规则二。"},
    )
    assert [fact["text"] for fact in facts] == ["占位规则一…", "占位规则二。"]


def test_render_normalizes_legacy_whole_field_input():
    rendered = render_world_facts(
        [
            {
                "field": "world_rules",
                "text": "占位规则一。占位规则二。",
                "evidence": [],
                "source": "original",
            }
        ]
    )
    assert rendered["world_rules"] == "占位规则一。占位规则二。"


def test_load_world_facts_tolerates_legacy_and_dirty_values():
    assert load_world_facts(None) == []
    assert load_world_facts("") == []
    assert load_world_facts("not-json") == []
    assert load_world_facts(json.dumps({"a": 1})) == []
    loaded = load_world_facts(json.dumps([{"field": "world_rules"}, "junk"]))
    assert loaded == [{"field": "world_rules"}]
