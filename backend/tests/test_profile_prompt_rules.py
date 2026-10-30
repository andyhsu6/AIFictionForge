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
    assert "组织不生成 personality" in profile_prompt_rules.FACTS_FIELD_BOUNDARIES


def test_prose_field_boundaries_carry_trait_rule():
    assert CANONICAL in profile_prompt_rules.PROSE_FIELD_BOUNDARIES
    assert "组织不生成 personality" in profile_prompt_rules.PROSE_FIELD_BOUNDARIES


def test_organisation_has_no_personality_field():
    """issue #210：组织不设 personality，规则必须明说，且旧口径全部清除。

    三轮标注的结论：模型分不清「组织作风」与「作为 / 资源 / 层级 / 立场 / 现状评语」。
    放宽（接受手段）错误率反升到 17.4%；收紧到「只能修饰作风的形容词」后，
    11 条里仍有 9 条是这四类。三次调词都只是换近义词绕过，因此字段本身取消。
    """
    for text in (
        profile_prompt_rules.FACTS_FIELD_BOUNDARIES,
        profile_prompt_rules.PROSE_FIELD_BOUNDARIES,
    ):
        assert "组织不生成 personality" in text
        assert "只有 background 与 appearance 两个字段" in text
        # 已废弃的三套口径都不得残留
        assert "行事风格与价值取向" not in text
        assert "善用傀儡" not in text
        assert "只写「作风」" not in text


def test_organisation_appearance_excludes_location_and_interior():
    """issue #198 / #210：地点、内部陈设与建筑破败都不是组织的「外貌」。

    第三轮标注里「宅院破败，蛛网灰尘遍布」被判不通过：那是房子的现状，
    不是这个组织的识别特征（对照通过项：制服、面具、独门印记）。
    """
    text = profile_prompt_rules.FACTS_FIELD_BOUNDARIES
    assert "不写地理位置" in text
    assert "内部陈设" in text
    assert "不写建筑的破败与使用状态" in text
    assert "总部或据点所在" not in text


def test_hand_written_rule_9_carries_the_same_buckets():
    """rule 9 是模板里的手写副本，必须单独钉住。

    独立复核实测：只断言「拼装后的 prompt 含新标记」时，把 rule 9 换回旧措辞
    测试仍全绿——标记来自同一 prompt 里拼接的共享边界常量，手写副本漂移被它兜住。
    """
    rendered = _formatted(PromptService.CHARACTERS_BATCH_GENERATION_FACTS)
    start = rendered.index("9. 组织")
    end = rendered.index("\n", start)
    rule9 = rendered[start:end]
    for marker in ("不生成 personality", "background", "appearance"):
        assert marker in rule9, f"rule 9 手写副本缺少口径：{marker}"
    assert "只写能直接修饰「作风」的形容词" not in rule9


def test_org_few_shot_example_does_not_contradict_the_trait_rule():
    """主路径 few-shot 示例曾把「内部等级森严」标成组织 personality。

    issue #210 把「等级怎么划分」判为 background 之后，这个示例与新规则字面互斥；
    同一 prompt 内示例的引导力通常强于规则文字，所以必须单独钉住。
    """
    import app.services.prompt_service as ps

    source = Path(ps.__file__).read_text(encoding="utf-8")
    assert "内部等级森严" not in source
    rendered = _formatted(PromptService.CHARACTERS_BATCH_GENERATION_FACTS)
    assert "内部等级森严" not in rendered
    # 组织示例给的是 background 事实：组织不再有 personality
    assert '"claim": "论功行赏，只看当年功绩"' in rendered


def test_organisation_schemas_have_no_personality_field():
    """组织生成 schema 不再输出 personality（issue #210）。

    组织档案只剩 background 与 appearance，创作路径与拆书路径必须同口径，
    否则新建组织仍会产出一个字段、而档案同步永远不再更新它。
    """
    import app.services.prompt_service as ps

    source = Path(ps.__file__).read_text(encoding="utf-8")
    for stale in ("核心理念", "文化价值观"):
        assert stale not in source, f"组织 schema 仍在描述 personality：{stale}"
    org_personality_lines = [
        line for line in source.splitlines() if '"personality"' in line and "组织" in line
    ]
    assert org_personality_lines == [], f"组织 schema 还有 personality 字段：{org_personality_lines}"


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
    # 组织口径必须同时出现在拼装后的主路径 prompt 里（含模板内那份手写 rule 9）
    for marker in ("组织不生成 personality", "只有 background 与 appearance 两个字段"):
        assert marker in rendered, f"拼装后的 prompt 缺少组织口径：{marker}"
    for stale in ("行事风格与价值取向", "善用傀儡", "内部等级森严"):
        assert stale not in rendered


def test_batch_prompts_share_field_rules():
    for name in (
        "CHARACTERS_BATCH_GENERATION",
        "CHARACTERS_BATCH_GENERATION_FACTS",
        "SINGLE_CHARACTER_GENERATION",
        "SINGLE_ORGANIZATION_GENERATION",
    ):
        rendered = _formatted(getattr(PromptService, name))
        assert CANONICAL in rendered, name
        assert "组织不生成 personality" in rendered, name


def test_sync_prompts_share_field_rules():
    facts = sync_service._PROMPT_TEMPLATE_FACTS.format(profiles="x", chapters="x")
    assert CANONICAL in facts
    assert "组织不生成 personality" in facts
    assert CANONICAL in sync_service._SYSTEM_PROMPT
    assert "组织不生成 personality" in sync_service._SYSTEM_PROMPT


def test_repair_prompt_uses_shared_hint():
    """第 7 处（薄字段补全）直接引用共享常量，不再手写一份。"""
    import app.services.book_import_service as bis

    source = Path(bis.__file__).read_text(encoding="utf-8")
    assert "PROSE_TRAIT_HINT" in source
    assert CANONICAL in profile_prompt_rules.PROSE_TRAIT_HINT or "能形容人的短语" in profile_prompt_rules.PROSE_TRAIT_HINT
