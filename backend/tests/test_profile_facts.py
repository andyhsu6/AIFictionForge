"""profile_facts 纯函数层测试（issue #190 / P1）。"""

import json

from app.services.profile_facts import (
    FACT_FIELDS,
    FACT_FIELD_LIMITS,
    MAX_CLAIM_CHARS,
    MAX_FACTS_PER_FIELD,
    MIN_QUOTE_CHARS,
    ProfileFactResult,
    _claim_matches_field,
    locate_quote,
    normalize_quote,
    parse_profile_payload,
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


# 9. 关系断言混在更长短语里
def test_kinship_token_inside_longer_claim_rejected():
    result = validate_profile_facts(
        {"name": "角色甲", "facts": [_fact(claim="武功高强，与丈夫并称双刀")]},
        CHAPTERS,
    )
    assert result.facts == []
    assert result.stats["claim_invalid"] == 1


# 9b. 谓词层不得误杀合法性格/外貌（单字标记会杀掉「杀伐果断」这类成语）
def test_legit_trait_phrases_pass_predicate():
    legit = [
        ("personality", "重情重义，视下属如兄弟"),
        ("personality", "杀伐果断，行事果决"),
        ("personality", "冷面杀手"),
        ("personality", "婚姻观保守"),
        ("personality", "生性多疑"),
        ("appearance", "身披战袍"),
        ("appearance", "杀气凛然"),
        ("appearance", "身形瘦削，面色蜡黄"),
    ]
    for field, claim in legit:
        assert _claim_matches_field(field, claim) is True, (field, claim)


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


# 15. parse_profile_payload：事实格式优先，原始条目原样返回
def test_parse_payload_facts_preferred():
    payload = {
        "name": "角色甲",
        "profile_facts": [
            {"field": "personality", "claim": "性情沉稳", "quote": "他听完只是点了点头", "chapter_number": 3, "inferred": True},
        ],
        "personality": "旧格式文本",
    }
    parsed = parse_profile_payload(payload)
    assert parsed.mode == "facts"
    assert parsed.entity == "角色甲"
    assert parsed.facts == payload["profile_facts"]
    assert parsed.facts[0]["inferred"] is True
    assert parsed.facts[0]["chapter_number"] == 3
    assert parsed.legacy == {}


def test_parse_payload_accepts_short_facts_key():
    payload = {"name": "角色甲", "facts": [{"field": "appearance", "claim": "身材修长"}]}
    parsed = parse_profile_payload(payload)
    assert parsed.mode == "facts"
    assert parsed.facts == payload["facts"]


# 16. 旧三字段格式：只收非空字段，值已 strip
def test_parse_payload_legacy_fields():
    parsed = parse_profile_payload(
        {
            "name": "角色甲",
            "personality": "  性情沉稳  ",
            "background": "出身寒门",
            "appearance": "   ",
        }
    )
    assert parsed.mode == "legacy"
    assert parsed.facts == []
    assert parsed.legacy == {"personality": "性情沉稳", "background": "出身寒门"}


# 17. 两种格式同时存在 → facts 优先
def test_parse_payload_facts_beat_legacy_when_both_present():
    parsed = parse_profile_payload(
        {
            "name": "角色甲",
            "profile_facts": [{"field": "personality", "claim": "性情沉稳"}],
            "personality": "性情沉稳",
            "background": "出身寒门",
        }
    )
    assert parsed.mode == "facts"
    assert parsed.legacy == {}


# 18. 空数组不能把旧文本判成 empty
def test_parse_payload_empty_facts_list_falls_back_to_legacy():
    parsed = parse_profile_payload(
        {"name": "角色甲", "profile_facts": [], "personality": "性情沉稳"}
    )
    assert parsed.mode == "legacy"
    assert parsed.facts == []
    assert parsed.legacy == {"personality": "性情沉稳"}


def test_parse_payload_empty_facts_list_without_text_is_empty():
    parsed = parse_profile_payload({"name": "角色甲", "profile_facts": []})
    assert parsed.mode == "empty"
    assert parsed.facts == []
    assert parsed.legacy == {}


# 19. 非 dict 入参不抛异常
def test_parse_payload_non_dict_is_empty():
    for payload in (None, "字符串", [1, 2]):
        parsed = parse_profile_payload(payload)
        assert parsed.mode == "empty"
        assert parsed.entity == ""
        assert parsed.facts == []
        assert parsed.legacy == {}


# 20. 旧字段值非法（空白/非字符串）被忽略
def test_parse_payload_ignores_blank_and_non_str_legacy_values():
    parsed = parse_profile_payload(
        {"name": "角色甲", "personality": "   ", "background": 123, "appearance": "身材修长"}
    )
    assert parsed.mode == "legacy"
    assert parsed.legacy == {"appearance": "身材修长"}


# 21. name 缺失或非字符串
def test_parse_payload_entity_defaults_to_empty_string():
    assert parse_profile_payload({"personality": "性情沉稳"}).entity == ""
    assert parse_profile_payload({"name": 123, "personality": "性情沉稳"}).entity == ""


def test_render_profile_uses_per_field_limits():
    """issue #190：background 承载身份/经历，限额高于 personality；appearance 只留 4 条。"""
    from app.services.profile_facts import FACT_FIELD_LIMITS

    facts = []
    for field, limit in FACT_FIELD_LIMITS.items():
        facts.extend(
            {"field": field, "claim": f"{field}特征{i}"} for i in range(limit + 3)
        )

    rendered = render_profile(facts)

    assert set(rendered) == set(FACT_FIELD_LIMITS)
    for field, limit in FACT_FIELD_LIMITS.items():
        assert len(rendered[field].split("、")) == limit


def test_merge_profile_facts_dedupes_and_prefers_new_value():
    """issue #190：同步回流时事实按 (field, claim) 去重，新值覆盖旧值。"""
    from app.services.profile_facts import merge_profile_facts

    existing = json.dumps(
        [{"field": "personality", "claim": "性情沉稳", "quote": "旧引文", "chapter_number": 1}],
        ensure_ascii=False,
    )
    merged = json.loads(
        merge_profile_facts(
            existing,
            [{"field": "personality", "claim": "性情沉稳", "quote": "新引文", "chapter_number": 9}],
        )
    )

    assert len(merged) == 1
    assert merged[0]["quote"] == "新引文"
    assert merged[0]["chapter_number"] == 9


def test_merge_profile_facts_keeps_only_recent_per_field():
    from app.services.profile_facts import FACT_FIELD_LIMITS, merge_profile_facts

    limit = FACT_FIELD_LIMITS["appearance"]
    existing = [
        {"field": "appearance", "claim": f"外貌特征{i}", "quote": f"引文{i}", "chapter_number": i}
        for i in range(limit)
    ]
    new = [{"field": "appearance", "claim": "新增外貌", "quote": "新引文", "chapter_number": 99}]

    merged = json.loads(merge_profile_facts(json.dumps(existing, ensure_ascii=False), new))

    assert len(merged) == limit
    assert merged[-1]["claim"] == "新增外貌"
    assert "外貌特征0" not in [fact["claim"] for fact in merged]


def test_merge_profile_facts_ignores_invalid_and_empty():
    from app.services.profile_facts import merge_profile_facts

    assert merge_profile_facts(None, []) is None
    assert merge_profile_facts("not json", []) is None
    assert merge_profile_facts(None, [{"field": "current_state", "claim": "处境"}]) is None
    assert merge_profile_facts(None, [{"field": "personality"}]) is None


# --------------------------------------------------------------------------- #
# issue #201 第 2 项：render_profile 的嵌套去重（确定性规则，不用语义相似度）
# --------------------------------------------------------------------------- #
def _claims(field: str, *claims: str) -> list[dict]:
    return [{"field": field, "claim": claim} for claim in claims]


def test_normalize_claim_form_strips_punct_and_particles():
    from app.services.profile_facts import normalize_claim_form

    # 只差标点与结构助词 → 同一特质的措辞差异
    assert normalize_claim_form("山庄的管家、") == normalize_claim_form("山庄管家")
    assert normalize_claim_form(None) == ""  # type: ignore[arg-type]
    # 引文校验用的 normalize_quote 语义不受影响（不归一标点）
    assert normalize_quote("他，点头") != normalize_quote("他。点头")


def test_render_profile_nested_dedup_keeps_longer():
    rendered = render_profile(_claims("personality", "隐忍克制", "克制"))
    assert rendered == {"personality": "隐忍克制"}


def test_render_profile_nested_dedup_merges_equal_form():
    # 归一后完全相同（只差结构助词）也合并为一条；保留原文较长者，
    # 与「子串折叠保留最长」保持同一准则（谁活下来不该取决于输入顺序）
    rendered = render_profile(_claims("background", "甲峰管家", "甲峰的管家"))
    assert rendered == {"background": "甲峰的管家"}


def test_render_profile_nested_three_levels_keeps_longest():
    rendered = render_profile(
        _claims("background", "神捕", "六扇门神捕", "六扇门神捕，办案二十年")
    )
    assert rendered == {"background": "六扇门神捕，办案二十年"}


def test_render_profile_nested_dedup_keeps_earliest_position():
    # 被丢弃项的位置让给保留下来的那条：长串虽在第 3 位，仍占据簇内最早的第 1 位
    rendered = render_profile(_claims("personality", "克制", "寡言", "隐忍克制", "沉默"))
    assert rendered == {"personality": "隐忍克制、寡言、沉默"}


def test_render_profile_limit_applies_after_dedup():
    from app.services.profile_facts import FACT_FIELD_LIMITS

    limit = FACT_FIELD_LIMITS["appearance"]
    assert limit == 4
    facts = _claims(
        "appearance",
        "黑衣",  # 被下一条吸收，不应占用配额
        "黑衣与黑长衫",
        "身形瘦削",
        "面无血色",
        "手指纤细",  # 去重后落在第 4 位，必须能进入渲染结果
    )
    rendered = render_profile(facts)
    items = rendered["appearance"].split("、")
    assert items == ["黑衣与黑长衫", "身形瘦削", "面无血色", "手指纤细"]


def test_render_profile_similar_but_unrelated_phrases_not_merged():
    # 防误合并回归：共字但不互为子串的短语是不同特质
    rendered = render_profile(_claims("personality", "甲家公子", "乙峰之孙"))
    assert rendered == {"personality": "甲家公子、乙峰之孙"}


def test_render_profile_punctuation_only_claims_ignored():
    facts = _claims("personality", "。", "！！", "  ", "的地得")
    facts.append({"field": "appearance", "claim": "。"})
    rendered = render_profile(facts)
    assert "personality" not in rendered
    assert "appearance" not in rendered


def test_render_profile_dedup_is_per_field():
    # 不同字段互不去重：personality 的短串不该吃掉 background 的长串
    facts = _claims("personality", "克制") + _claims("background", "隐忍克制")
    rendered = render_profile(facts)
    assert rendered == {"personality": "克制", "background": "隐忍克制"}


def test_render_profile_is_deterministic():
    facts = _claims("personality", "克制", "寡言", "隐忍克制", "沉默")
    facts += _claims("background", "甲峰管家", "甲峰的管家")
    first = render_profile(facts)
    second = render_profile(facts)
    assert first == second


def test_render_profile_keeps_negated_claim():
    """否定形式不是「措辞差异」：按字面吸收会把档案写成相反结论。

    独立复核给出的反例：短串是长串的子串，但长串带否定标记。
    """
    rendered = render_profile(_claims("personality", "克制", "不克制"))
    assert rendered == {"personality": "克制、不克制"}
    rendered = render_profile(_claims("personality", "耐心", "没有耐心"))
    assert rendered == {"personality": "耐心、没有耐心"}


def test_render_profile_does_not_fold_ascii_claims():
    """纯英文没有可靠词边界，子串折叠会误并不同词。"""
    rendered = render_profile(_claims("personality", "care", "careless"))
    assert rendered == {"personality": "care、careless"}
    rendered = render_profile(_claims("personality", "friend", "boyfriend"))
    assert rendered == {"personality": "friend、boyfriend"}


def test_render_profile_does_not_swallow_single_char_claim():
    """单字表述几乎总是更长的串的子串，吸收它只会丢信息。"""
    rendered = render_profile(_claims("personality", "高", "高大威猛"))
    assert rendered == {"personality": "高、高大威猛"}


def test_render_profile_multi_parent_cluster_does_not_starve_independent_fact():
    """多条长串共享同一个短串时，位置继承不能把独立事实挤出配额。

    旧实现把「凡包含我的都算我的簇」合并，几条存活项全部继承短串的最早位置，
    排在后面的独立事实因此跌出上限。
    """
    limit = FACT_FIELD_LIMITS["appearance"]
    facts = _claims(
        "appearance",
        "黑衣",
        "黑衣与长发",
        "甲之独立特征",
        "黑衣与瘦削",
        "黑衣与苍白",
        "黑衣与纹路",
    )
    items = render_profile(facts)["appearance"].split("、")
    assert len(items) == limit
    assert "甲之独立特征" in items
    # 短串被吸收后不占配额
    assert "黑衣" not in items


def test_render_profile_equal_form_prefers_longer_text_in_any_order():
    """归一串相同的两条，谁活下来不应由输入顺序决定。"""
    forward = render_profile(_claims("background", "甲家管家", "甲家的管家"))
    backward = render_profile(_claims("background", "甲家的管家", "甲家管家"))
    assert forward == backward == {"background": "甲家的管家"}


def test_render_profile_nesting_rejects_postposed_and_detached_negation():
    """否定不总紧贴在中心语前面：后置补语、能愿动词、副词插入都会把意思改反。

    只按「紧邻前后缀」判否定会漏掉这一整类，所以判定改成通篇扫描极性标记。
    """
    for short, long in (
        ("克制", "克制不了"),
        ("耐心", "耐心全无"),
        ("克制", "并不十分克制"),
        ("耐心", "不太耐心"),
        ("克制", "不能克制"),
        ("克制", "未必克制"),
        ("克制", "何曾克制"),
    ):
        rendered = render_profile(_claims("personality", short, long))
        assert rendered == {"personality": f"{short}、{long}"}, (short, long)


def test_render_profile_nesting_skips_mixed_ascii_claim():
    """混排 claim 里的 ASCII 片段同样没有词边界，作为被吸收方一律豁免。"""
    rendered = render_profile(_claims("personality", "AI", "AI与术法"))
    assert rendered == {"personality": "AI、AI与术法"}


def test_render_profile_form_equal_ignores_join_separator_in_choice():
    """择优长度不数「、」：连接符不是信息，且会与渲染分隔符混淆。"""
    forward = render_profile(_claims("personality", "克制隐忍", "克制、隐忍"))
    backward = render_profile(_claims("personality", "克制、隐忍", "克制隐忍"))
    assert forward == backward == {"personality": "克制隐忍"}


# 组织不设 personality（issue #210）：代码层按 kind 硬丢弃，不依赖提示词
def test_organization_personality_fact_is_dropped():
    payload = {
        "name": "组织甲",
        "facts": [
            _fact(),
            _fact(
                field="background",
                claim="雨夜独行",
                quote="雨下了一整夜，院子里空无一人",
                chapter_number=5,
                inferred=False,
            ),
        ],
    }
    result = validate_profile_facts(payload, CHAPTERS, kind="organization")
    assert [f["field"] for f in result.facts] == ["background"]
    assert result.stats["org_personality_dropped"] == 1
    assert result.stats["facts_accepted"] == 1


def test_character_keeps_personality_fact():
    result = validate_profile_facts(
        {"name": "角色甲", "facts": [_fact()]}, CHAPTERS, kind="character"
    )
    assert [f["field"] for f in result.facts] == ["personality"]
    assert result.stats["org_personality_dropped"] == 0


def test_render_profile_omits_personality_for_organization():
    facts = [
        {
            "field": "personality",
            "claim": "隐秘不露",
            "quote": "他听完只是点了点头",
            "chapter_number": 3,
            "inferred": False,
        },
        {
            "field": "background",
            "claim": "雨夜独行",
            "quote": "雨下了一整夜，院子里空无一人",
            "chapter_number": 5,
            "inferred": False,
        },
    ]
    assert "personality" not in render_profile(facts, kind="organization")
    assert render_profile(facts, kind="character")["personality"] == "隐秘不露"


# issue #210：组织 personality 的两处落库前剥离（book import / legacy 回流）
def test_strip_org_personality_drops_field_and_facts():
    from app.services.profile_facts import strip_org_personality

    item = {"name": "组织甲", "personality": "性情沉稳", "background": "山门"}
    facts = [
        {"field": "personality", "claim": "性情沉稳"},
        {"field": "background", "claim": "山门"},
    ]
    kept = strip_org_personality(item, facts)
    assert "personality" not in item
    assert [f["field"] for f in kept] == ["background"]
    assert item["background"] == "山门"


def test_filter_org_profile_values_only_touches_organizations():
    from app.services.profile_facts import filter_org_profile_values

    values = {"personality": "性情沉稳", "background": "山门"}
    assert filter_org_profile_values(values, True) == {"background": "山门"}
    assert filter_org_profile_values(values, False) == values
