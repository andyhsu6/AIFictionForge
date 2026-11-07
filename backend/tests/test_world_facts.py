"""issue #242 P1：世界设定事实（world_facts）的构建、合并与读取。"""
import json

from app.services.book_import_service import (
    _build_world_facts,
    _load_world_facts,
    _merge_world_facts,
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
    facts = _build_world_facts(
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
    merged = _merge_world_facts(first, second)
    assert [fact["text"] for fact in merged] == ["旧规则", "新规则"]


def test_load_world_facts_tolerates_legacy_and_dirty_values():
    assert _load_world_facts(None) == []
    assert _load_world_facts("") == []
    assert _load_world_facts("not-json") == []
    assert _load_world_facts(json.dumps({"a": 1})) == []
    loaded = _load_world_facts(json.dumps([{"field": "world_rules"}, "junk"]))
    assert loaded == [{"field": "world_rules"}]
