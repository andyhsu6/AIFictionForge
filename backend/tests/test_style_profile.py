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
