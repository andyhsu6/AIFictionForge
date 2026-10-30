"""档案字段语义必须只有一处来源（issue #190）。

历史教训：同一套"性格该写什么"的规则曾经散落在 6 个提示词里，改了一处、
其余 5 处继续用旧口径，产出质量随入口不同而不同。本文件钉死这条：
所有会产出档案字段的提示词都必须带上共享规则里的同一句话。
"""

from __future__ import annotations

from pathlib import Path

from app.services import character_profile_sync_service as sync_service
from app.services import profile_prompt_rules
from app.services.prompt_service import PromptService

# 共享规则里最能识别"形态要求"的一句话
CANONICAL = "接在「他是"


def _formatted(template: str) -> str:
    return PromptService.format_prompt(
        template,
        count=1,
        time_period="x",
        location="x",
        atmosphere="x",
        rules="x",
        theme="x",
        genre="x",
        requirements="x",
        project_context="x",
        user_input="x",
    )


def test_facts_field_boundaries_carry_trait_rule():
    assert CANONICAL in profile_prompt_rules.FACTS_FIELD_BOUNDARIES
    assert "组织特性" in profile_prompt_rules.FACTS_FIELD_BOUNDARIES


def test_prose_field_boundaries_carry_trait_rule():
    assert CANONICAL in profile_prompt_rules.PROSE_FIELD_BOUNDARIES
    assert "组织特性" in profile_prompt_rules.PROSE_FIELD_BOUNDARIES


def test_batch_prompts_share_field_rules():
    for name in (
        "CHARACTERS_BATCH_GENERATION",
        "CHARACTERS_BATCH_GENERATION_FACTS",
        "SINGLE_CHARACTER_GENERATION",
        "SINGLE_ORGANIZATION_GENERATION",
    ):
        rendered = _formatted(getattr(PromptService, name))
        assert CANONICAL in rendered, name
        assert "组织特性" in rendered, name


def test_sync_prompts_share_field_rules():
    facts = sync_service._PROMPT_TEMPLATE_FACTS.format(profiles="x", chapters="x")
    assert CANONICAL in facts
    assert "组织特性" in facts
    assert CANONICAL in sync_service._SYSTEM_PROMPT
    assert "组织特性" in sync_service._SYSTEM_PROMPT


def test_repair_prompt_uses_shared_hint():
    """第 7 处（薄字段补全）直接引用共享常量，不再手写一份。"""
    import app.services.book_import_service as bis

    source = Path(bis.__file__).read_text(encoding="utf-8")
    assert "PROSE_TRAIT_HINT" in source
    assert CANONICAL in profile_prompt_rules.PROSE_TRAIT_HINT or "能形容人的短语" in profile_prompt_rules.PROSE_TRAIT_HINT
