"""W3 / plan B2: PARTIAL_CONTINUE prompt template.

Baseline characterization tests pin the CURRENT behavior of PARTIAL_REGENERATE
(template retrievable, format_prompt substitutes its placeholders, registry
entry intact) and must pass on unchanged code. The new tests pin the expected
behavior of PARTIAL_CONTINUE.

Note on content_language: PromptService.format_prompt captures content_language
as a named parameter and injects the language instruction as an appended tail
segment (append_language_instruction), exactly like PARTIAL_REGENERATE. It is
therefore NOT a str.format placeholder in the template; the 7th semantic input
still flows through every format_prompt call.
"""

import string

import pytest

from app.services.prompt_service import PromptService


class _EmptyResult:
    def scalar_one_or_none(self):
        return None


class _FakeDB:
    """DB with no user-custom templates → get_template must fall back to the
    class attribute."""

    async def execute(self, _stmt):
        return _EmptyResult()


def _template_placeholders(template: str) -> list:
    return [
        field
        for _literal, field, _spec, _conv in string.Formatter().parse(template)
        if field is not None
    ]


# ---------- baseline: PARTIAL_REGENERATE current behavior (must pass pre-change) ----------


@pytest.mark.anyio
async def test_baseline_partial_regenerate_retrievable():
    template = await PromptService.get_template_with_fallback("PARTIAL_REGENERATE")
    assert template == PromptService.PARTIAL_REGENERATE
    assert "{selected_text}" in template
    assert "{anchor_text}" not in template


def test_baseline_partial_regenerate_format_substitutes_all_placeholders():
    rendered = PromptService.format_prompt(
        PromptService.PARTIAL_REGENERATE,
        context_before="CTX_BEFORE",
        original_word_count=123,
        selected_text="SELECTED_TEXT",
        context_after="CTX_AFTER",
        user_instructions="USER_INSTR",
        length_requirement="LEN_REQ",
        style_content="STYLE_CONTENT",
    )
    for value in (
        "CTX_BEFORE", "123", "SELECTED_TEXT", "CTX_AFTER",
        "USER_INSTR", "LEN_REQ", "STYLE_CONTENT",
    ):
        assert value in rendered
    for placeholder in _template_placeholders(PromptService.PARTIAL_REGENERATE):
        assert "{" + placeholder + "}" not in rendered


def test_baseline_partial_regenerate_registry_entry():
    info = PromptService.get_system_template_info("PARTIAL_REGENERATE")
    assert info is not None
    assert info["category"] == "章节重写"
    assert info["parameters"] == [
        "context_before", "original_word_count", "selected_text", "context_after",
        "user_instructions", "length_requirement", "style_content",
    ]


def test_baseline_format_prompt_missing_placeholder_raises():
    # Characterization: a placeholder the template needs but kwargs omits → ValueError.
    with pytest.raises(ValueError):
        PromptService.format_prompt(PromptService.PARTIAL_REGENERATE)


# ---------- new: PARTIAL_CONTINUE ----------

CONTINUE_KWARGS = dict(
    context_before="CTX_BEFORE",
    anchor_text="ANCHOR_TEXT",
    context_after="CTX_AFTER",
    target_chars=300,
    user_instructions="USER_INSTR",
    style_content="STYLE_CONTENT",
)


@pytest.mark.anyio
async def test_partial_continue_get_template_falls_back_to_class_attribute():
    template = await PromptService.get_template("PARTIAL_CONTINUE", "user-1", _FakeDB())
    assert template == PromptService.PARTIAL_CONTINUE
    assert template


@pytest.mark.anyio
async def test_partial_continue_retrievable_via_fallback():
    template = await PromptService.get_template_with_fallback("PARTIAL_CONTINUE")
    assert template == PromptService.PARTIAL_CONTINUE
    assert template


def test_partial_continue_format_prompt_substitutes_all_inputs_no_keyerror():
    rendered = PromptService.format_prompt(
        PromptService.PARTIAL_CONTINUE,
        content_language="zh",
        **CONTINUE_KWARGS,
    )
    for value in ("CTX_BEFORE", "ANCHOR_TEXT", "CTX_AFTER", "USER_INSTR", "STYLE_CONTENT"):
        assert value in rendered
    # target char value present in rendered prompt
    assert "300" in rendered
    # no unsubstituted placeholders remain
    for placeholder in _template_placeholders(PromptService.PARTIAL_CONTINUE):
        assert "{" + placeholder + "}" not in rendered
    # content_language is injected as appended tail instruction (same mechanism as
    # PARTIAL_REGENERATE), not as a str.format placeholder
    assert "请使用简体中文回复。" in rendered
    assert "content_language" not in _template_placeholders(PromptService.PARTIAL_CONTINUE)


def test_partial_continue_registry_entry():
    info = PromptService.get_system_template_info("PARTIAL_CONTINUE")
    assert info is not None
    assert info["category"] == "章节续写"
    assert info["parameters"] == [
        "context_before", "anchor_text", "context_after", "target_chars",
        "user_instructions", "style_content", "content_language",
    ]
    assert info["content"] == PromptService.PARTIAL_CONTINUE


def test_partial_continue_does_not_disturb_partial_regenerate():
    # The new template must not alter the old one's placeholder set.
    assert "anchor_text" not in _template_placeholders(PromptService.PARTIAL_REGENERATE)
