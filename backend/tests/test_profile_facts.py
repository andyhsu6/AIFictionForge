"""profile_facts 纯函数层测试（issue #190 / P1）。"""

from app.services.profile_facts import (
    FACT_FIELDS,
    MAX_CLAIM_CHARS,
    MAX_FACTS_PER_FIELD,
    MIN_QUOTE_CHARS,
    ProfileFactResult,
    locate_quote,
    normalize_quote,
    render_profile,
    validate_profile_facts,
)

# 一批"原文"：第 3 章有可定位引文，第 5 章另有一句
CHAPTERS = {
    3: "他听完只是点了点头，没有说话。\n随后转身离开。",
    5: "雨下了一整夜，院子里空无一人。",
}


def _fact(**overrides):
    base = {
        "field": "personality",
        "claim": "性情沉稳",
        "quote": "他听完只是点了点头",
        "chapter_number": 3,
        "inferred": True,
    }
    base.update(overrides)
    return base


# 1. normalize_quote：折叠空白，不归一标点
def test_normalize_quote_collapses_whitespace_only():
    assert normalize_quote("他听完\n\t只是 点了\r点头\u3000") == "他听完只是点了点头"
    assert normalize_quote("他，点头") != normalize_quote("他。点头")
    assert normalize_quote(None) == ""  # type: ignore[arg-type]


# 2. 正常事实通过，chapter_number 保留
def test_valid_fact_accepted():
    result = validate_profile_facts({"name": "角色甲", "facts": [_fact()]}, CHAPTERS)
    assert isinstance(result, ProfileFactResult)
    assert result.entity == "角色甲"
    assert len(result.facts) == 1
    fact = result.facts[0]
    assert fact["field"] == "personality"
    assert fact["claim"] == "性情沉稳"
    assert fact["chapter_number"] == 3
    assert fact["inferred"] is True
    assert result.stats["facts_in"] == 1
    assert result.stats["facts_accepted"] == 1
    assert result.stats["chapter_corrected"] == 0


# 3. 编造的引文
def test_fabricated_quote_rejected():
    result = validate_profile_facts(
        {"name": "角色甲", "facts": [_fact(quote="这句话在原文里根本不存在")]},
        CHAPTERS,
    )
    assert result.facts == []
    assert result.stats["quote_not_found"] == 1
    assert result.stats["facts_accepted"] == 0


# 4. 章号写错但引文在别的章存在 → 纠正章号
def test_chapter_number_corrected():
    result = validate_profile_facts(
        {"name": "角色甲", "facts": [_fact(chapter_number=5)]},
        CHAPTERS,
    )
    assert len(result.facts) == 1
    assert result.facts[0]["chapter_number"] == 3
    assert result.stats["chapter_corrected"] == 1
    assert result.stats["facts_accepted"] == 1


def test_locate_quote_prefers_claimed_chapter():
    assert locate_quote("他听完只是点了点头", CHAPTERS, 3) == 3
    assert locate_quote("雨下了一整夜", CHAPTERS, 3) == 5
    assert locate_quote("完全不存在的一句话", CHAPTERS, 3) is None


# 5. 引文过短
def test_short_quote_rejected():
    assert len(normalize_quote("他点头")) < MIN_QUOTE_CHARS
    result = validate_profile_facts({"name": "角色甲", "facts": [_fact(quote="他点头")]}, CHAPTERS)
    assert result.facts == []
    assert result.stats["quote_invalid"] == 1


# 6. claim 含句号 → 叙述残留
def test_claim_with_period_rejected():
    result = validate_profile_facts(
        {"name": "角色甲", "facts": [_fact(claim="性情沉稳。")]},
        CHAPTERS,
    )
    assert result.facts == []
    assert result.stats["claim_invalid"] == 1


def test_claim_with_ellipsis_or_too_long_rejected():
    result = validate_profile_facts(
        {"name": "角色甲", "facts": [_fact(claim="他……"), _fact(claim="性" * (MAX_CLAIM_CHARS + 1))]},
        CHAPTERS,
    )
    assert result.facts == []
    assert result.stats["claim_invalid"] == 2


# 7. 关系当性格
def test_kinship_claim_in_personality_rejected():
    result = validate_profile_facts(
        {"name": "角色甲", "facts": [_fact(claim="狄宏伟之妻")]},
        CHAPTERS,
    )
    assert result.facts == []
    assert result.stats["claim_invalid"] == 1


# 8. 事件当性格
def test_event_claim_in_personality_rejected():
    result = validate_profile_facts(
        {"name": "角色甲", "facts": [_fact(claim="因救主角而死")]},
        CHAPTERS,
    )
    assert result.facts == []
    assert result.stats["claim_invalid"] == 1


# 9. 亲属称谓出现在性格里
def test_kinship_token_inside_longer_claim_rejected():
    result = validate_profile_facts(
        {"name": "角色甲", "facts": [_fact(claim="武功高强，与丈夫并称双刀")]},
        CHAPTERS,
    )
    assert result.facts == []
    assert result.stats["claim_invalid"] == 1


# 10. background 不接受 inferred
def test_inferred_not_allowed_outside_personality():
    result = validate_profile_facts(
        {"name": "角色甲", "facts": [_fact(field="background", claim="出身寒门", inferred=True)]},
        CHAPTERS,
    )
    assert result.facts == []
    assert result.stats["inferred_not_allowed"] == 1


def test_background_without_inferred_accepted():
    result = validate_profile_facts(
        {"name": "角色甲", "facts": [_fact(field="background", claim="出身寒门", inferred=False)]},
        CHAPTERS,
    )
    assert len(result.facts) == 1
    assert result.facts[0]["field"] == "background"


# 11. 外貌里写动作
def test_action_claim_in_appearance_rejected():
    result = validate_profile_facts(
        {"name": "角色甲", "facts": [_fact(field="appearance", claim="挥刀杀敌", inferred=False)]},
        CHAPTERS,
    )
    assert result.facts == []
    assert result.stats["claim_invalid"] == 1


# 12. 未知字段
def test_unknown_field_rejected():
    result = validate_profile_facts(
        {"name": "角色甲", "facts": [_fact(field="current_state")]},
        CHAPTERS,
    )
    assert result.facts == []
    assert result.stats["field_invalid"] == 1
    assert "current_state" not in FACT_FIELDS


def test_invalid_chapter_number_rejected():
    result = validate_profile_facts(
        {"name": "角色甲", "facts": [_fact(chapter_number=0), _fact(chapter_number="3"), _fact(chapter_number=True)]},
        CHAPTERS,
    )
    assert result.facts == []
    assert result.stats["chapter_invalid"] == 3


# 13. render_profile：去重、顿号连接、每字段上限、空输入
def test_render_profile_dedup_and_join():
    facts = [
        {"field": "personality", "claim": "性情沉稳"},
        {"field": "personality", "claim": "性情 沉稳"},  # normalize_quote 后与上一条相同
        {"field": "personality", "claim": "寡言少语。"},
        {"field": "appearance", "claim": "身材修长"},
        {"field": "background", "claim": "   "},
        {"field": "current_state", "claim": "正在逃亡"},
    ]
    rendered = render_profile(facts)
    assert rendered == {
        "personality": "性情沉稳、寡言少语",
        "appearance": "身材修长",
    }


def test_render_profile_caps_per_field():
    facts = [{"field": "personality", "claim": f"特质{i}"} for i in range(MAX_FACTS_PER_FIELD + 3)]
    rendered = render_profile(facts)
    assert len(rendered["personality"].split("、")) == MAX_FACTS_PER_FIELD


def test_render_profile_empty():
    assert render_profile([]) == {}
    assert render_profile([{"field": "personality", "claim": "。"}]) == {}


# 14. payload 形状不对不抛异常
def test_invalid_payload_shapes():
    for payload in ("不是对象", None, 42, [], {"facts": "不是列表"}):
        result = validate_profile_facts(payload, CHAPTERS)
        assert isinstance(result, ProfileFactResult)
        assert result.facts == []
        assert result.stats["payload_invalid"] == 1
        assert result.stats["facts_accepted"] == 0


def test_stats_always_contain_all_keys():
    result = validate_profile_facts({"name": "角色甲", "facts": []}, CHAPTERS)
    for key in (
        "facts_in",
        "facts_accepted",
        "field_invalid",
        "claim_invalid",
        "quote_invalid",
        "quote_not_found",
        "chapter_invalid",
        "inferred_not_allowed",
        "chapter_corrected",
    ):
        assert result.stats[key] == 0
