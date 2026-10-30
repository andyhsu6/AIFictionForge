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


def test_organisation_rules_and_stances_stay_out_of_personality():
    """issue #198：门规/立场/伦理属制度，不是性格，规则必须明说排除。

    两轮标注（153 条）里组织的分类失分全部落在这两类内容上——规则允许
    「价值取向」，评审却一致判为放错字段。这条测试钉住修正后的口径。
    """
    for text in (
        profile_prompt_rules.FACTS_FIELD_BOUNDARIES,
        profile_prompt_rules.PROSE_FIELD_BOUNDARIES,
    ):
        assert "一律不写进 personality" in text
        assert "行事风格" in text
        # 旧口径：把「价值取向」列为组织特性，正是误判来源
        assert "行事风格与价值取向" not in text


def test_organisation_appearance_excludes_location_and_interior():
    """issue #198：地点与内部陈设不是组织的「外貌」。"""
    text = profile_prompt_rules.FACTS_FIELD_BOUNDARIES
    assert "不写地理位置" in text
    assert "内部陈设" in text
    assert "总部或据点所在" not in text


def test_no_template_keeps_a_private_copy_of_the_old_organisation_wording():
    """拼装后的完整提示词里不能再残留旧口径。

    独立复核（换了一个模型）发现的漏网：共享常量改对了，但事实抽取主路径的
    模板里另有一份**手写副本**仍写「行事风格与价值取向」，同一 prompt 内新旧
    指令互相抵消——只断言常量内容看不到这类副本。所以这里直接扫整个提示词
    模块的源码，以及拼装后的主路径 prompt。
    """
    import app.services.prompt_service as ps

    source = Path(ps.__file__).read_text(encoding="utf-8")
    for stale in ("行事风格与价值取向", "总部位置"):
        assert stale not in source, f"提示词模块里仍有旧口径：{stale}"

    rendered = _formatted(PromptService.CHARACTERS_BATCH_GENERATION_FACTS)
    assert "一律不写进 personality" in rendered
    assert "行事风格与价值取向" not in rendered


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
