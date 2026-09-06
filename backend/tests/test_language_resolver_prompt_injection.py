"""生成语言解析链与提示词注入测试（i18n plan todo 17）。

覆盖：
- 优先级链（resolve_generation_language）：per-generation override > 用户
  preferences.content_language > UI 语言（preferences.language）> "zh"；
  None/"auto"/非法值逐级回落，非法输入安全回落不抛错。
- resolve_user_generation_language（DB 版）：从 Settings.preferences 读取偏好后
  走同一链条；per-gen override 覆盖偏好；无用户/无行回落 zh。
- 提示词注入（append_language_instruction / PromptService.format_prompt）：
  指令追加在模板渲染结果之外；en → "Please respond in English."；
  zh → "请使用简体中文回复。"（zh 同样注入，计划接受）；None 不注入
  （输出与历史行为逐字节一致）；真实类模板（含 {var} 与 {{...}} 转义花括号）
  格式化不报错且仍追加尾部指令。
- 灵创助手（project_agent_service）：SYSTEM_PROMPT 不再硬编码"回答使用中文"，
  agent_system_prompt 产出含解析语言指令的系统提示词。
"""
import json
import os
import uuid
from types import SimpleNamespace

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.database import Base
from app.models.settings import Settings
from app.services.language_resolver import (
    LANGUAGE_INSTRUCTIONS,
    append_language_instruction,
    language_instruction,
    normalize_content_language,
    resolve_generation_language,
    resolve_user_generation_language,
)
from app.services.prompt_service import PromptService
from app.services.project_agent_service import SYSTEM_PROMPT, agent_system_prompt

ZH_INSTRUCTION = LANGUAGE_INSTRUCTIONS["zh"]
EN_INSTRUCTION = LANGUAGE_INSTRUCTIONS["en"]


# ========== 优先级链：resolve_generation_language ==========


@pytest.mark.parametrize("per_gen,prefs,expected", [
    # 1. per-generation override 最高优先（zh 与 en 双向）
    ("zh", {"content_language": "en"}, "zh"),
    ("en", {"content_language": "zh"}, "en"),
    # 2. per-gen "auto"/None 视为未指定，回落用户偏好
    ("auto", {"content_language": "en"}, "en"),
    (None, {"content_language": "en"}, "en"),
    ("auto", {"content_language": "zh"}, "zh"),
    # 3. 偏好 content_language "auto"/None 回落 UI 语言
    (None, {"content_language": "auto", "language": "en"}, "en"),
    (None, {"content_language": None, "language": "en"}, "en"),
    (None, {"language": "en"}, "en"),
    # 4. UI 语言 "auto"/缺失回落 zh
    ("auto", {"language": "auto"}, "zh"),
    (None, {}, "zh"),
    (None, None, "zh"),
], ids=[
    "per-gen-zh-beats-prefs-en", "per-gen-en-beats-prefs-zh",
    "per-gen-auto-falls-to-prefs", "per-gen-none-falls-to-prefs", "per-gen-auto-prefs-zh",
    "prefs-auto-falls-to-ui-lang", "prefs-none-falls-to-ui-lang", "ui-lang-only",
    "ui-lang-auto-falls-to-zh", "empty-prefs-zh", "no-prefs-zh",
])
def test_resolve_generation_language_priority_chain(per_gen, prefs, expected):
    """优先级链：per-gen > preferences.content_language > preferences.language > zh。"""
    assert resolve_generation_language(per_gen, prefs) == expected


def test_per_gen_case_insensitive_normalization():
    """per-gen 值大小写不敏感（schema 层 422 拦截之前的宽容归一化）。"""
    assert resolve_generation_language("ZH", {"content_language": "en"}) == "zh"
    assert resolve_generation_language("EN", {"content_language": "zh"}) == "en"
    assert resolve_generation_language("AUTO", {"content_language": "en"}) == "en"


@pytest.mark.parametrize("bad", ["fr", "", "zh-CN", 123, object()])
def test_invalid_per_gen_falls_through(bad):
    """per-gen 非法值（区域变体/空串/非字符串）不抛错，安全回落偏好链。"""
    assert resolve_generation_language(bad, {"content_language": "en"}) == "en"
    # 无偏好可用时一路回落到 zh
    assert resolve_generation_language(bad, None) == "zh"


@pytest.mark.parametrize("bad", ["fr", "zh-CN", "", 123])
def test_invalid_prefs_fall_through(bad):
    """偏好 content_language 非法值回落 UI 语言；UI 语言同样非法时回落 zh。"""
    assert resolve_generation_language(None, {"content_language": bad, "language": "en"}) == "en"
    assert resolve_generation_language(None, {"content_language": bad, "language": "fr"}) == "zh"


@pytest.mark.parametrize("raw,lang", [("zh-CN", "zh"), ("en-US", "en"), ("zh", "zh"), ("en", "en")])
def test_ui_language_region_variants(raw, lang):
    """UI 语言宽容处理历史区域变体（preferences 写入侧已限定 zh/en）。"""
    assert resolve_generation_language(None, {"language": raw}) == lang


def test_preferences_json_string_accepted():
    """preferences 可传 Settings.preferences 的 JSON 字符串（与 DB 列一致）。"""
    assert resolve_generation_language(None, '{"content_language": "en"}') == "en"
    assert resolve_generation_language("auto", '{"content_language": "auto", "language": "zh"}') == "zh"
    assert resolve_generation_language(None, '{"language": "en"}') == "en"


@pytest.mark.parametrize("broken", ["{broken json", "[1, 2]", "null", "123"])
def test_malformed_preferences_json_safe(broken):
    """损坏的 preferences JSON/非 Mapping 值按无偏好处理，回落 zh，不抛错。"""
    assert resolve_generation_language(None, broken) == "zh"


def test_normalize_content_language_vocabulary():
    """归一化词表：仅小写 zh/en 显式生效；None/auto/其余返回 None（回落）。"""
    assert normalize_content_language("zh") == "zh"
    assert normalize_content_language("en") == "en"
    assert normalize_content_language("auto") is None
    assert normalize_content_language(None) is None
    assert normalize_content_language("fr") is None


# ========== DB 版解析：resolve_user_generation_language ==========


@pytest.fixture
async def db_session():
    """临时文件 SQLite，测试后清理（与既有 settings 测试约定一致）。"""
    db_path = f"/tmp/test_lang_resolver_{uuid.uuid4().hex}.db"
    engine = create_async_engine(f"sqlite+aiosqlite:///{db_path}")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    Session = async_sessionmaker(bind=engine, expire_on_commit=False)
    async with Session() as session:
        yield session
    await engine.dispose()
    if os.path.exists(db_path):
        os.remove(db_path)


def make_user(user_id: str = "u-lang") -> SimpleNamespace:
    return SimpleNamespace(user_id=user_id, is_admin=False)


@pytest.mark.anyio
async def test_resolve_user_generation_language_from_settings(db_session):
    """DB 版：读 Settings.preferences.content_language；per-gen override 覆盖偏好。"""
    user = make_user()
    db_session.add(Settings(
        user_id=user.user_id,
        preferences=json.dumps({"content_language": "en", "language": "zh"}),
    ))
    await db_session.commit()

    # 无 per-gen：偏好 content_language=en 生效（优先于 UI 语言 zh）
    assert await resolve_user_generation_language(db_session, user.user_id) == "en"
    # per-gen zh 覆盖偏好 en
    assert await resolve_user_generation_language(db_session, user.user_id, "zh") == "zh"
    # per-gen auto 回落偏好 en
    assert await resolve_user_generation_language(db_session, user.user_id, "auto") == "en"


@pytest.mark.anyio
async def test_resolve_user_generation_language_fallbacks(db_session):
    """无 db/无 user/无 Settings 行时按无偏好处理，回落链仍生效。"""
    assert await resolve_user_generation_language(None, None) == "zh"
    assert await resolve_user_generation_language(None, "u-x", "en") == "en"  # per-gen 仍生效
    assert await resolve_user_generation_language(db_session, "no-such-user") == "zh"


# ========== 提示词注入：append_language_instruction / format_prompt ==========


def test_format_prompt_en_appends_english_instruction():
    """en 解析结果 → 渲染结果尾部追加英文指令（独立尾部段）。"""
    prompt = PromptService.format_prompt(
        "第{chapter_number}章：{{字面花括号保持}}\n{content}",
        content_language="en",
        chapter_number=3,
        content="正文",
    )
    assert prompt.startswith("第3章：{字面花括号保持}\n正文")
    assert prompt.endswith(EN_INSTRUCTION)
    assert ZH_INSTRUCTION not in prompt


def test_format_prompt_zh_appends_chinese_instruction():
    """zh 解析结果同样追加中文指令（计划接受的改动）。"""
    prompt = PromptService.format_prompt("你好{name}", content_language="zh", name="世界")
    assert prompt.endswith(ZH_INSTRUCTION)
    assert prompt.startswith("你好世界")


def test_format_prompt_none_keeps_legacy_output():
    """content_language=None 不注入：输出与 template.format(**kwargs) 逐字节一致。"""
    template = "第{chapter_number}章：{{字面花括号}}\n{content}\n\n"
    kwargs = {"chapter_number": 3, "content": "正文"}
    assert PromptService.format_prompt(template, **kwargs) == template.format(**kwargs)
    assert "Please respond in English" not in PromptService.format_prompt(template, **kwargs)
    # 非法值（非 zh/en）同样不注入，不抛错
    assert PromptService.format_prompt(template, content_language="fr", **kwargs) == template.format(**kwargs)


def test_class_template_with_braces_formats_and_gets_tail_instruction():
    """真实类模板（WORLD_BUILDING：含 {var} 占位符与 {{...}} 转义花括号）：
    格式化不报错，尾部指令仍追加，模板正文不受影响。"""
    prompt = PromptService.format_prompt(
        PromptService.WORLD_BUILDING,
        content_language="en",
        title="示例书名",
        theme="示例主题",
        genre="奇幻",
        description="示例简介",
        full_book_context="",
    )
    assert prompt.endswith(EN_INSTRUCTION)
    assert "示例书名" in prompt
    # 模板正文中的 {{...}} 转义 JSON 片段保持原样（未被语言注入破坏）
    assert '"time_period"' in prompt


def test_per_gen_override_beats_preferences_in_prompt():
    """端到端链：per-gen zh 覆盖偏好 en → 注入的是中文指令而非英文。"""
    language = resolve_generation_language("zh", {"content_language": "en"})
    prompt = append_language_instruction("正文内容", language)
    assert prompt.endswith(ZH_INSTRUCTION)
    assert EN_INSTRUCTION not in prompt


def test_append_language_instruction_detached_tail():
    """指令作为独立尾部段追加：模板尾随空白收敛为一个空行分隔。"""
    assert append_language_instruction("正文", "en") == f"正文\n\n{EN_INSTRUCTION}"
    # 模板末尾已有换行/空白时不产生连续空行
    assert append_language_instruction("正文\n\n\n", "zh") == f"正文\n\n{ZH_INSTRUCTION}"
    assert language_instruction("en") == EN_INSTRUCTION


# ========== 灵创助手：project_agent_service ==========


def test_system_prompt_has_no_hardcoded_chinese_directive():
    """SYSTEM_PROMPT 规则 5 不再硬编码"回答使用中文"（todo 17 替换为解析语言指令）。"""
    assert "回答使用中文" not in SYSTEM_PROMPT


@pytest.mark.parametrize("lang,expected", [("zh", ZH_INSTRUCTION), ("en", EN_INSTRUCTION)])
def test_agent_system_prompt_gets_resolved_instruction(lang, expected):
    """agent_system_prompt 产出包含解析语言指令的系统提示词。"""
    prompt = agent_system_prompt(lang)
    assert prompt.startswith(SYSTEM_PROMPT)
    assert prompt.endswith(expected)


def test_agent_system_prompt_instruction_stays_last_with_skill():
    """拼接批准模式说明与 Skill 工作流后，语言指令仍是系统提示词最后一段。"""
    prompt = agent_system_prompt(
        "en",
        approval_prompt="\n\n当前为手动批准模式：写入工具生成预览后必须等待用户在界面确认。",
        skill_content="1. 先查数据再回答",
    )
    assert prompt.endswith(EN_INSTRUCTION)
    assert "Skill" in prompt
    assert "手动批准模式" in prompt
