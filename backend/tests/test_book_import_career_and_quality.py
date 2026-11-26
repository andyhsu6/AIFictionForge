import inspect
from types import SimpleNamespace

import pytest

from app.schemas.book_import import BookImportApplyRequest
from app.services.book_import_service import (
    BookImportService,
    _format_known_characters,
    _is_generic_epithet,
    _merge_chapter_relationship_note,
    _slice_chapter_head_tail,
)
from app.services.import_validators import validate_career_system
from app.services.prompt_service import PromptService


def test_validate_career_system_allow_empty_and_force():
    empty_payload = {"main_careers": [], "sub_careers": []}
    # auto 模式允许非升级流题材返回空职业数组
    validate_career_system(empty_payload, allow_empty=True)

    # force 模式要求至少生成 1 个主职业
    with pytest.raises(ValueError):
        validate_career_system(empty_payload, allow_empty=False)

    valid_payload = {
        "main_careers": [
            {
                "name": "剑修",
                "type": "main",
                "max_stage": 5,
                "stages": [{"level": i, "name": f"第{i}境"} for i in range(1, 6)],
            }
        ],
        "sub_careers": [],
    }
    validate_career_system(valid_payload, allow_empty=False)


def test_strip_empty_career_section_removes_tags_and_rules():
    sample = (
        "<chapter_context>\n"
        "<careers priority=\"P2\">\n"
        "【本章职业】\n"
        "暂无职业信息\n"
        "</careers>\n"
        "<characters>林默</characters>\n"
        "</chapter_context>\n"
        "- 保持人物性格一致\n"
        "- 角色的能力表现须符合其职业和阶段设定\n"
        "❌ 角色行为超出其职业阶段的能力范围\n"
        "❌ 人物性格前后矛盾\n"
    )
    cleaned = PromptService.strip_empty_career_section(sample)
    assert "<careers" not in cleaned
    assert "【本章职业】" not in cleaned
    assert "暂无职业信息" not in cleaned
    assert "角色的能力表现须符合其职业和阶段设定" not in cleaned
    assert "角色行为超出其职业阶段的能力范围" not in cleaned
    assert "<characters>林默</characters>" in cleaned
    assert "- 保持人物性格一致" in cleaned
    assert "❌ 人物性格前后矛盾" in cleaned


def test_relationship_extraction_prompt_backward_compatible_when_known_characters_omitted():
    template = PromptService.RELATIONSHIP_EXTRACTION
    assert "{known_characters}" in template
    rendered = PromptService.format_prompt(
        template,
        title="测试作品",
        genre="武侠",
        chapters_text="第1章内容",
        existing_relationships="暂无",
    )
    assert "（暂无）" in rendered
    assert "测试作品" in rendered


def test_slice_chapter_head_tail_preserves_head_and_tail():
    short = "短章节正文"
    assert _slice_chapter_head_tail(short, 100) == short

    head = "A" * 1000
    mid = "M" * 2000
    tail = "Z" * 1000
    full = head + mid + tail
    sliced = _slice_chapter_head_tail(full, 1000)
    assert len(sliced) <= 1000
    assert "……（中段略）……" in sliced
    assert sliced.startswith("A" * 100)
    assert sliced.endswith("Z" * 100)


def test_extract_relationship_batch_default_per_chapter_chars():
    sig = inspect.signature(BookImportService._extract_relationship_batch)
    assert sig.parameters["per_chapter_chars"].default == 3600


def test_is_generic_epithet_blocks_dirty_names_without_false_positives():
    dirty_names = [
        "",
        "   ",
        "黑衣人",
        "蒙面人",
        "灰衣老者",
        "店小二",
        "路人甲",
        "神秘客",
        "领头刺客",
        "第一号杀手",
    ]
    for name in dirty_names:
        assert _is_generic_epithet(name) is True, f"should filter {name!r}"

    normal_names = [
        "张无忌",
        "陈平安",
        "欧阳锋",
        "上官婉儿",
        "林诗音",
        "李寻欢",
        "青云宗",
        "锦衣卫",
    ]
    for name in normal_names:
        assert _is_generic_epithet(name) is False, f"should keep {name!r}"


def test_format_known_characters_and_merge_chapter_note():
    chars = [
        SimpleNamespace(name="林诗音", aliases='["诗音", "林姑娘"]'),
        SimpleNamespace(name="李寻欢", aliases=None),
    ]
    formatted = _format_known_characters(chars)
    assert "林诗音（别名：诗音、林姑娘）" in formatted
    assert "李寻欢" in formatted

    merged_1 = _merge_chapter_relationship_note(None, 3, "初次相遇")
    assert merged_1 == "[第3章] 初次相遇"

    merged_2 = _merge_chapter_relationship_note(merged_1, 3, "更新：实为伪装试探")
    assert merged_2 == "[第3章] 更新：实为伪装试探"
    assert merged_2.count("[第3章]") == 1

    merged_3 = _merge_chapter_relationship_note(merged_2, 5, "并肩作战")
    assert merged_3 == "[第3章] 更新：实为伪装试探\n[第5章] 并肩作战"


def test_book_import_apply_request_career_system_mode_schema():
    base = {
        "project_suggestion": {
            "title": "测试书",
            "narrative_perspective": "第三人称",
            "target_words": 100000,
        },
        "chapters": [{"title": "第1章", "content": "正文", "chapter_number": 1}],
        "outlines": [],
    }
    req_default = BookImportApplyRequest(**base)
    assert req_default.career_system_mode == "auto"

    for mode in ("auto", "skip", "force"):
        req = BookImportApplyRequest(**base, career_system_mode=mode)
        assert req.career_system_mode == mode
