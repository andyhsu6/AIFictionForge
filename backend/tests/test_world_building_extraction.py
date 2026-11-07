"""issue #242 P0：拆书导入世界设定抽取校验与模板（无网络、无 AI）。"""
from types import SimpleNamespace

import pytest

from app.services.book_import_service import _render_existing_world
from app.services.import_validators import (
    WORLD_EXTRACTION_NOT_MENTIONED,
    validate_world_building_extraction,
)
from app.services.prompt_service import PromptService


CHAPTER_TEXT = (
    "占位城由占位议会统治。占位战争结束为元年，城市开始重建。"
    "夜色下的占位城常年弥漫着铁锈味。占位议会规定，所有外来者必须登记。"
)


def _chapters():
    return [SimpleNamespace(chapter_number=1, content=CHAPTER_TEXT)]


def _valid_payload():
    return {
        "time_period": "占位纪元第七年，以占位战争结束为元年，占位议会开始重建占位城，社会秩序仍未稳定。",
        "location": "占位城（原文地名）位于占位平原，由占位议会管辖，城墙分内外两层，夜间封锁外城。",
        "atmosphere": "夜色下的占位城常年弥漫铁锈味，压抑而冷硬，占位议会控制夜间通行，居民情绪紧绷。",
        "rules": "占位议会规定所有外来者必须登记；违反者将被占位卫队驱逐出占位城，城内禁止私斗。",
        "evidence": {
            "time_period": [{"chapter": 1, "quote": "占位战争结束为元年"}],
            "location": [{"chapter": 1, "quote": "占位城由占位议会统治"}],
            "atmosphere": [{"chapter": 1, "quote": "夜色下的占位城常年弥漫着铁锈味"}],
            "rules": [{"chapter": 1, "quote": "所有外来者必须登记"}],
        },
    }


def test_valid_extraction_passes():
    validate_world_building_extraction(_valid_payload(), _chapters())


def test_quote_must_appear_verbatim_in_chapter():
    payload = _valid_payload()
    payload["evidence"]["location"][0]["quote"] = "占位城外有一片黑森林"
    with pytest.raises(ValueError, match="逐字找到"):
        validate_world_building_extraction(payload, _chapters())


def test_missing_evidence_is_rejected():
    payload = _valid_payload()
    payload["evidence"].pop("rules")
    with pytest.raises(ValueError, match="缺少 evidence"):
        validate_world_building_extraction(payload, _chapters())


def test_generic_label_is_rejected():
    payload = _valid_payload()
    payload["atmosphere"] = "一个充满冒险与未知的世界，读者会感到紧张。"
    with pytest.raises(ValueError, match="空泛标签"):
        validate_world_building_extraction(payload, _chapters())


def test_not_mentioned_field_is_exempt_from_evidence():
    payload = _valid_payload()
    payload["atmosphere"] = WORLD_EXTRACTION_NOT_MENTIONED
    payload["evidence"].pop("atmosphere")
    validate_world_building_extraction(payload, _chapters())


def test_unknown_chapter_number_is_rejected():
    payload = _valid_payload()
    payload["evidence"]["rules"][0]["chapter"] = 99
    with pytest.raises(ValueError, match="不在本次导入章节范围内"):
        validate_world_building_extraction(payload, _chapters())


def test_extraction_template_formats_and_is_registered():
    prompt = PromptService.format_prompt(
        PromptService.WORLD_BUILDING_EXTRACTION,
        title="占位书名",
        genre="占位类型",
        theme="占位主题",
        description="占位简介",
        full_book_context="【第1章 占位】占位正文",
        existing_world="",
    )
    assert "占位书名" in prompt
    assert "原文未提及" in prompt
    assert '"evidence"' in prompt


def test_extraction_template_is_listed_in_metadata():
    definitions = PromptService.get_all_system_templates()
    keys = {item.get("template_key") for item in definitions}
    assert "WORLD_BUILDING_EXTRACTION" in keys


def test_render_existing_world_skips_empty_fields():
    project = SimpleNamespace(
        world_time_period="占位纪元第七年",
        world_location="",
        world_atmosphere=None,
        world_rules="占位议会规定",
    )
    rendered = _render_existing_world(project)
    assert "占位纪元第七年" in rendered
    assert "占位议会规定" in rendered
    assert "地理位置" not in rendered
