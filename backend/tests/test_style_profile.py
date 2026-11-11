"""issue #254：文笔画像的抽样、校验、渲染与本地兜底（纯函数，不碰 DB/AI）。"""
from types import SimpleNamespace

import pytest

from app.services.style_profile import (
    DO_DONT_ITEM_MAX_CHARS,
    build_style_sample,
    is_profile_preset_id,
    local_stats_fallback,
    profile_preset_id,
    render_style_profile,
    validate_style_profile,
)


def _chapter(number, paragraphs, paragraph_len=20):
    content = "\n\n".join(
        f"占位段落{index}：" + "占位文字" * paragraph_len + "。"
        for index in range(paragraphs)
    )
    return SimpleNamespace(chapter_number=number, title=f"占位标题{number}", content=content)


def _profile():
    return {
        "version": 1,
        "confidence": 0.7,
        "metrics": {
            "avg_sentence_chars": 16.4,
            "median_sentence_chars": 15,
            "short_sentence_ratio": 0.32,
            "dialogue_ratio": 0.41,
            "paragraph_chars_p50": 48,
        },
        "narrative": {"pov": "第三人称有限视角", "tense": "过去时", "distance": "贴近人物内心"},
        "diction": {"register": "书面偏口语", "concreteness": "高", "idiom_density": "低"},
        "dialogue": {"attribution": "前置说话人", "tag_density": "低"},
        "rhetoric": ["比喻：以日常器物作比"],
        "do": ["平均句长控制在 12–18 字", "对话占比约 40%", "每段不超过 4 句"],
        "dont": ["避免连续出现超过 40 字的长句", "避免对话标签次数超过 2 次", "避免每段都写环境"],
        "evidence_chapters": [1, 2, 5],
    }


def test_preset_id_helpers():
    preset_id = profile_preset_id("proj-1")
    assert preset_id == "profile:proj-1"
    assert is_profile_preset_id(preset_id) is True
    assert is_profile_preset_id("custom-1") is False
    assert is_profile_preset_id(None) is False


def test_build_style_sample_picks_layered_chapters():
    chapters = [_chapter(number, 3) for number in range(1, 11)]
    sample = build_style_sample(chapters)
    numbers = sample.meta["chapter_numbers"]
    assert sample.meta["strategy"] == "layered"
    assert sample.meta["tail"] is False
    assert 4 <= len(numbers) <= 6
    assert numbers == sorted(numbers)
    assert numbers[0] == 1
    assert numbers[-1] == 10
    assert sample.meta["char_count"] == sum(entry["chars"] for entry in sample.meta["ranges"])


def test_build_style_sample_takes_all_when_few_chapters():
    chapters = [_chapter(number, 2) for number in range(1, 4)]
    sample = build_style_sample(chapters, mode="tail")
    assert sample.meta["strategy"] == "all"
    assert sample.meta["chapter_numbers"] == [1, 2, 3]
    assert sample.meta["tail"] is True


def test_build_style_sample_single_chapter():
    sample = build_style_sample([_chapter(7, 2)])
    assert sample.meta["strategy"] == "single"
    assert sample.meta["chapter_numbers"] == [7]


def test_build_style_sample_skips_empty_content():
    chapters = [
        _chapter(1, 2),
        SimpleNamespace(chapter_number=2, title="空", content="   \n\n"),
        SimpleNamespace(chapter_number=3, title="缺", content=None),
    ]
    sample = build_style_sample(chapters)
    assert sample.meta["chapter_numbers"] == [1]
    assert all(entry["chapter"] == 1 for entry in sample.meta["ranges"])


def test_long_chapter_windows_align_to_paragraph_boundaries():
    content_chapter = _chapter(1, 40)
    sample = build_style_sample([content_chapter], per_chapter_budget=900)
    content = content_chapter.content
    ranges = sample.meta["ranges"]
    assert len(ranges) == 3
    assert sample.meta["truncated"] is True
    boundaries = "\n。！？…!?；;"
    for entry in ranges:
        start, end = entry["start"], entry["end"]
        assert entry["chars"] == end - start
        assert start == 0 or content[start - 1] in boundaries
        assert end == len(content) or content[end - 1] in boundaries
    assert ranges[0]["start"] == 0
    assert ranges[-1]["end"] == len(content)


def test_sample_respects_global_char_budget():
    chapters = [_chapter(number, 40) for number in range(1, 7)]
    sample = build_style_sample(chapters, char_budget=4000, per_chapter_budget=2000)
    assert sample.meta["char_count"] <= 4000
    assert sample.meta["truncated"] is True
    assert len(sample.text) < 4200


def test_sample_meta_contains_no_original_text():
    sample = build_style_sample([_chapter(1, 3)], mode="tail")
    serialized = str(sample.meta)
    assert "占位文字" not in serialized
    assert "占位段落" not in serialized
    assert set(sample.meta) == {
        "chapter_numbers",
        "ranges",
        "char_count",
        "strategy",
        "tail",
        "truncated",
    }


def test_validate_accepts_valid_profile():
    validate_style_profile(_profile())


@pytest.mark.parametrize(
    "mutate",
    [
        lambda data: data.update({"version": 2}),
        lambda data: data.update({"confidence": 1.4}),
        lambda data: data["metrics"].update({"avg_sentence_chars": 0}),
        lambda data: data["metrics"].update({"median_sentence_chars": 260}),
        lambda data: data["metrics"].update({"dialogue_ratio": 1.2}),
        lambda data: data["narrative"].update({"pov": "  "}),
        lambda data: data["diction"].update({"register": 3}),
        lambda data: data["dialogue"].update({"tag_density": ""}),
        lambda data: data.update({"rhetoric": ["修辞"] * 9}),
        lambda data: data.update({"do": data["do"][:2]}),
        lambda data: data.update({"do": data["do"] + ["a", "b", "c", "d"]}),
        lambda data: data.update({"dont": ["占" * (DO_DONT_ITEM_MAX_CHARS + 1)]}),
        lambda data: data.update({"do": ["文笔优美", "对话占比约 40%", "每段不超过 4 句"]}),
        lambda data: data.update({"evidence_chapters": ["1"]}),
        lambda data: data["narrative"].update({"distance": "“" + "占" * 21 + "”"}),
        lambda data: data["narrative"].update({"distance": "占" * 1500}),
    ],
)
def test_validate_rejects_invalid_profile(mutate):
    data = _profile()
    mutate(data)
    with pytest.raises(ValueError):
        validate_style_profile(data)


def test_validate_rejects_non_object():
    with pytest.raises(ValueError):
        validate_style_profile(None)
    with pytest.raises(ValueError):
        validate_style_profile([])


def test_render_is_deterministic_and_has_all_sections():
    data = _profile()
    meta = {
        "chapter_numbers": [1, 2, 5],
        "ranges": [],
        "char_count": 8200,
        "strategy": "layered",
        "tail": False,
        "truncated": False,
    }
    first = render_style_profile(data, meta)
    second = render_style_profile(data, meta)
    assert first == second
    for section in ["本书文笔画像", "第1–2、5章", "8200", "叙事视角与距离", "句式与节奏", "用词与修辞", "对话", "必须做到", "避免"]:
        assert section in first
    assert "不得复述或引用样本中的任何句子、人名或书名" in first
    assert "占位文字" not in first
    assert "占位段落" not in first


def test_render_english_when_requested():
    rendered = render_style_profile(_profile(), None, language="en")
    assert "Book Style Profile" in rendered
    assert "Must do" in rendered
    assert "do not reproduce or quote" in rendered


def test_render_does_not_leak_sample_text():
    sample = build_style_sample([_chapter(1, 5)])
    data = local_stats_fallback(sample)
    rendered = render_style_profile(data, sample.meta)
    for line in sample.text.split("\n\n"):
        assert line.strip() not in rendered


def test_render_declares_measured_metrics_priority_in_both_languages():
    """量化块声明实测优先，避免与 do/dont 里的模型估计数字冲突时被误读（#279）。"""
    zh = render_style_profile(_profile())
    en = render_style_profile(_profile(), None, language="en")

    assert (
        "- 以上量化指标为抽样实测值；若与下方条目中的数字冲突，以量化指标为准。" in zh
    )
    assert (
        "- The metrics above are measured from the sample; "
        "if a number below conflicts with them, the measured value wins." in en
    )


def test_local_stats_fallback_passes_validation():
    text = (
        "占位短句一。"
        + "占位句子略长一些内容略多但仍在常见范围内。"
        + "“占位对话内容。”"
        + "第三句占位内容。"
    )
    sample = build_style_sample([SimpleNamespace(chapter_number=1, title="t", content=text)])
    data = local_stats_fallback(sample)

    validate_style_profile(data)
    assert data["version"] == 1
    assert data["confidence"] == pytest.approx(0.3)
    assert data["metrics"]["avg_sentence_chars"] > 0
    assert data["metrics"]["median_sentence_chars"] > 0
    assert data["metrics"]["dialogue_ratio"] > 0
    assert len(data["do"]) == 3 or 3 <= len(data["do"]) <= 6
    assert 3 <= len(data["dont"]) <= 6
    assert data["evidence_chapters"] == [1]
    assert all(len(item) <= DO_DONT_ITEM_MAX_CHARS for item in data["do"] + data["dont"])


def test_local_stats_fallback_handles_empty_sample():
    from app.services.style_profile import StyleSample

    data = local_stats_fallback(StyleSample(text="", meta={}))
    validate_style_profile(data)
    assert data["metrics"]["dialogue_ratio"] == 0
    assert data["evidence_chapters"] == []


def test_unpunctuated_long_chapter_is_sampled_not_dropped():
    """无句末标点/换行的长章不能被整体丢弃（否则空样本会产出编造画像）。"""
    from app.services.style_profile import SAMPLE_MIN_CHARS

    chapter = SimpleNamespace(
        chapter_number=1,
        title="无标点章",
        content="占位文字" * 2500,  # 10000 字，无标点无换行
    )
    sample = build_style_sample([chapter])
    assert sample.text
    assert sample.meta["char_count"] >= SAMPLE_MIN_CHARS
    assert sample.meta["chapter_numbers"] == [1]


# ---- P2：原文复述检测（n-gram 滑窗） ----

_ORIGINAL_SENTENCE = "雨声变得密集起来像是有人在敲打铁皮屋顶这声音让整条巷子都安静"
_SAMPLE_TEXT = _ORIGINAL_SENTENCE + "，随后又是一阵沉默。"


def test_detect_source_overlap_finds_copied_fragment():
    from app.services.style_profile import detect_source_overlap

    data = _profile()
    data["diction"]["register"] = _ORIGINAL_SENTENCE
    assert detect_source_overlap(data, _SAMPLE_TEXT) == ["diction.register"]


def test_detect_source_overlap_empty_sample_returns_empty():
    from app.services.style_profile import detect_source_overlap

    data = _profile()
    data["diction"]["register"] = _ORIGINAL_SENTENCE
    assert detect_source_overlap(data, "") == []
    assert detect_source_overlap(data, "   \n") == []


def test_validate_rejects_source_overlap_when_sample_given():
    data = _profile()
    # 27 字原文片段 + 操作动词，确保失败原因只能是复述检测
    data["do"][0] = _ORIGINAL_SENTENCE[:27] + "，保持这个密度"
    with pytest.raises(ValueError, match="疑似复述原文"):
        validate_style_profile(data, sampled_text=_SAMPLE_TEXT)


def test_validate_skips_overlap_check_without_sample():
    """兼容旧调用：不传 sampled_text 时不跑复述检测。"""
    data = _profile()
    data["do"][0] = _ORIGINAL_SENTENCE[:27] + "，保持这个密度"
    validate_style_profile(data)


def test_local_stats_fallback_passes_overlap_check():
    """本地兜底只含统计值，必须不命中复述检测。"""
    text = "占位短句一。" + "占位句子略长一些内容略多但仍在常见范围内。" * 40
    sample = build_style_sample([SimpleNamespace(chapter_number=1, title="t", content=text)])
    validate_style_profile(local_stats_fallback(sample), sampled_text=sample.text)


# ---- P2：空泛词覆盖 + 可执行约束 ----

def test_validate_rejects_new_vague_word_without_quantifier():
    from app.services.style_profile import VAGUE_ADJECTIVES

    for word in ("漂亮", "好看", "高级", "有张力", "有画面感", "节奏感强", "文笔好", "真实", "立体", "鲜明"):
        assert word in VAGUE_ADJECTIVES

    data = _profile()
    data["do"][0] = "语言漂亮"
    with pytest.raises(ValueError, match="空泛形容词"):
        validate_style_profile(data)


def test_validate_accepts_quantified_item_with_vague_word():
    data = _profile()
    data["do"][0] = "平均句长控制在 12–18 字"
    validate_style_profile(data)


def test_validate_allows_specific_craft_guidance_without_quantifier():
    """具体写作指导（无数字、无白名单动词）不应被硬拒。"""
    data = _profile()
    data["do"][2] = "白描手法为主"
    validate_style_profile(data)

    data = _profile()
    data["do"][2] = "情绪用动作外化：攥拳、挠头、戳胸口"
    validate_style_profile(data)


def test_validate_rejects_vague_adjective_without_quantifier():
    data = _profile()
    data["do"][2] = "语言要优美"
    with pytest.raises(ValueError, match="空泛形容词"):
        validate_style_profile(data)


def test_validate_source_whitelist():
    data = _profile()
    data["source"] = "weird"
    with pytest.raises(ValueError):
        validate_style_profile(data)

    data = _profile()
    data["source"] = "local"
    validate_style_profile(data)


def test_validate_rejects_nested_source_free_text():
    """任意层级的 source 都必须是受控枚举，不能借它绕过引文/复述检测。"""
    data = _profile()
    data["diction"]["source"] = "“这是一段足够长的原文片段用来绕过检查”"
    with pytest.raises(ValueError, match="source"):
        validate_style_profile(data)

    data = _profile()
    data["diction"]["source"] = "model"
    validate_style_profile(data)


# ---- issue #279 后续：do/dont 里的量化条目必须与实测 metrics 一致 ----

def test_reconcile_advice_drops_model_numbers_and_keeps_measured():
    from app.services.style_profile import reconcile_advice_with_metrics

    metrics = {
        "avg_sentence_chars": 29.7,
        "median_sentence_chars": 28.0,
        "short_sentence_ratio": 0.28,
        "dialogue_ratio": 0.23,
        "paragraph_chars_p50": 38,
    }
    data = {
        "do": ["平均句长控制在 12-20 字", "对话占比保持 35-45%", "心理疑问用连续短句排比"],
        "dont": ["避免连续 3 句超过 40 字", "避免无标记地切换叙述人称或时态"],
    }
    updated = reconcile_advice_with_metrics(data, metrics)

    joined = " ".join(updated["do"])
    assert "12-20" not in joined and "35-45" not in joined
    assert "短句排比" not in joined, "实测句长 29.7 字时不得保留“连续短句”类建议"
    # 实测 29.7 字 ⇒ 区间 28–32
    assert any("28–32" in item for item in updated["do"]), f"应换成实测句长区间: {updated['do']}"
    assert any("23%" in item for item in updated["do"]), "应带上实测对话占比"
    assert "避免无标记地切换叙述人称或时态" in updated["dont"], "质性条目必须保留"
    assert 3 <= len(updated["do"]) <= 6
    assert 3 <= len(updated["dont"]) <= 6


def test_reconcile_advice_keeps_short_sentence_hint_for_a_short_sentence_book():
    from app.services.style_profile import reconcile_advice_with_metrics

    metrics = {
        "avg_sentence_chars": 16.0,
        "median_sentence_chars": 15.0,
        "short_sentence_ratio": 0.55,
        "dialogue_ratio": 0.35,
        "paragraph_chars_p50": 60,
    }
    updated = reconcile_advice_with_metrics(
        {"do": ["心理疑问用连续短句推进"], "dont": ["避免长句堆叠"]}, metrics
    )
    assert "心理疑问用连续短句推进" in updated["do"]


# ---- 生成期「写作规格」尾块：把实测指标贴到用户提示词末尾 ----

def test_render_style_tail_block_carries_measured_numbers():
    from app.services.style_profile import render_style_tail_block

    block = render_style_tail_block(
        {
            "avg_sentence_chars": 29.7,
            "short_sentence_ratio": 28,
            "dialogue_ratio": 23,
            "paragraph_chars_p50": 38,
        }
    )
    assert "平均句长 29.7 字" in block
    assert "短句(≤15字)占比 28%" in block
    assert "对话占比 23%" in block
    assert "段落中位 38 字" in block
    assert "不得单句成段" in block
    assert "常规段落 27–49 字" in block  # 38 * 0.7 / 1.3
    assert "优先级高于上文" in block


def test_render_style_tail_block_is_empty_without_avg_sentence():
    from app.services.style_profile import render_style_tail_block

    assert render_style_tail_block({}) == ""
    assert render_style_tail_block({"dialogue_ratio": 23}) == ""


def test_extract_rendered_metrics_round_trips_the_renderer():
    from app.services.style_profile import (
        extract_rendered_metrics,
        render_style_profile,
    )

    profile = {
        "version": 1,
        "confidence": 0.8,
        "metrics": {
            "avg_sentence_chars": 29.7,
            "median_sentence_chars": 28,
            "short_sentence_ratio": 0.28,
            "dialogue_ratio": 0.23,
            "paragraph_chars_p50": 38,
        },
        "narrative": {"pov": "第三人称限知", "tense": "过去时", "distance": "贴近"},
        "diction": {"register": "书面", "concreteness": "高", "idiom_density": "中"},
        "dialogue": {"attribution": "动作引出", "tag_density": "中"},
        "rhetoric": ["白描"],
        "do": ["保持信息密度"],
        "dont": ["避免抒情"],
        "evidence_chapters": [1],
    }
    rendered = render_style_profile(profile, {"chapter_numbers": [1], "char_count": 3000}, title="t", language="zh")
    metrics = extract_rendered_metrics(rendered)
    assert metrics["avg_sentence_chars"] == 29.7
    assert metrics["paragraph_chars_p50"] == 38
    # 渲染层把比例写成百分数
    assert metrics["dialogue_ratio"] == 23
    assert metrics["short_sentence_ratio"] == 28


def test_extract_rendered_metrics_ignores_unrelated_text():
    from app.services.style_profile import extract_rendered_metrics

    assert extract_rendered_metrics("") == {}
    assert extract_rendered_metrics("没有任何指标的一段文字") == {}


def test_render_style_tail_block_english_variant():
    from app.services.style_profile import render_style_tail_block

    block = render_style_tail_block(
        {"avg_sentence_chars": 29.7, "dialogue_ratio": 23, "paragraph_chars_p50": 38},
        language="en",
    )
    assert "Writing spec" in block
    assert "average sentence length 29.7 characters" in block
    assert "dialogue ratio 23%" in block
    assert "regular paragraphs 27-49 characters" in block
    assert "不得单句成段" not in block


def test_style_content_language_detects_english_rendering():
    from app.services.style_profile import style_content_language

    assert style_content_language("【本书文笔画像】平均句长：29.7 字") == "zh"
    assert style_content_language("Book style profile: Average sentence length: 29.7") == "en"
    assert style_content_language("") == "zh"
