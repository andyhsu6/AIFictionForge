"""Skill 加载器语言注入测试（i18n plan todo 18）。

覆盖 build_skill_system_prompt（skill["content"] → 最终系统提示词的唯一收口）：
- zh 偏好用户 → 系统提示词以中文指令结尾，且指令追加在 Skill 正文（含 references
  附录）之后（独立尾部段）；
- en 偏好用户（Settings.preferences.content_language="en"）→ 英文指令；
- 注入不改变 Skill 本体：缓存中的 skill["content"]（SKILL.md body + 附录拼接产物）
  注入前后逐字节一致（永不翻译/改写/持久化）；
- 无 db / 无 user_id / 无 Settings 行时安全回落 zh，不抛错。

行为边界（硬约定）：SKILL.md 内容永不翻译；en 用户 + 中文 Skill 可能产生混合输出
（已知限制，不修复）；本注入只约束输出语言。
"""
import json
import os
import uuid

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.database import Base
from app.models.settings import Settings
from app.services.language_resolver import LANGUAGE_INSTRUCTIONS
from app.services.skill_loader import build_skill_system_prompt, get_all_skills_cached

ZH_INSTRUCTION = LANGUAGE_INSTRUCTIONS["zh"]
EN_INSTRUCTION = LANGUAGE_INSTRUCTIONS["en"]


@pytest.fixture
async def db_session():
    """临时文件 SQLite，测试后清理（与既有 language_resolver 测试约定一致）。"""
    db_path = f"/tmp/test_skill_lang_{uuid.uuid4().hex}.db"
    engine = create_async_engine(f"sqlite+aiosqlite:///{db_path}")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    Session = async_sessionmaker(bind=engine, expire_on_commit=False)
    async with Session() as session:
        yield session
    await engine.dispose()
    if os.path.exists(db_path):
        os.remove(db_path)


def make_skill(content: str = "## 工作流指令\n\n1. 先阅读参考资料\n2. 按结构输出") -> dict:
    """构造与 load_skills() 产物同构的 Skill 模板 dict（最小字段集）。"""
    return {
        "template_key": "SKILL_TEST",
        "name": "test",
        "template_name": "测试 Skill",
        "category": "Skill",
        "description": "测试用",
        "parameters": ["user_input"],
        "content": content,
        "references": {},
        "triggers": ["/test"],
        "is_skill": True,
    }


# ========== (a) zh 偏好 → 中文指令追加在 Skill 正文之后 ==========


@pytest.mark.anyio
async def test_zh_preference_appends_chinese_instruction_after_body():
    """默认（无偏好）解析为 zh：指令作为独立尾部段追加在 Skill 正文之后。"""
    skill = make_skill()
    prompt = await build_skill_system_prompt(skill)

    assert prompt.endswith(ZH_INSTRUCTION)
    # 正文完整保留在前（rstrip 收敛尾随空白 + 一个空行分隔），指令只在最后一段
    assert prompt == skill["content"].rstrip() + f"\n\n{ZH_INSTRUCTION}"
    assert EN_INSTRUCTION not in prompt


@pytest.mark.anyio
async def test_zh_preference_user_via_db_appends_chinese_instruction(db_session):
    """Settings.preferences（UI language zh / 无 content_language）→ 中文指令。"""
    db_session.add(Settings(
        user_id="u-skill-zh",
        preferences=json.dumps({"language": "zh"}),
    ))
    await db_session.commit()

    prompt = await build_skill_system_prompt(
        make_skill("指令正文"), db=db_session, user_id="u-skill-zh"
    )
    assert prompt == f"指令正文\n\n{ZH_INSTRUCTION}"


# ========== (b) en 偏好 → 英文指令 ==========


@pytest.mark.anyio
async def test_en_preference_user_gets_english_instruction(db_session):
    """preferences.content_language=en → 英文指令（Skill 聊天无 per-gen override）。"""
    db_session.add(Settings(
        user_id="u-skill-en",
        preferences=json.dumps({"content_language": "en", "language": "zh"}),
    ))
    await db_session.commit()

    prompt = await build_skill_system_prompt(
        make_skill(), db=db_session, user_id="u-skill-en"
    )
    assert prompt.endswith(EN_INSTRUCTION)
    assert ZH_INSTRUCTION not in prompt
    # 中文 Skill 正文原样保留（只约束输出语言，不翻译正文）
    assert prompt.startswith("## 工作流指令")
    assert "1. 先阅读参考资料" in prompt


# ========== (c) Skill 本体逐字节不变（永不翻译/改写） ==========


@pytest.mark.anyio
async def test_skill_body_byte_identical_real_cache():
    """真实 Skill 缓存：注入后缓存中 content 与磁盘解析产物逐字节一致。"""
    skills = get_all_skills_cached()
    assert skills, "仓库内应至少加载到一个 Skill"
    skill = skills[0]
    before = skill["content"]

    prompt = await build_skill_system_prompt(skill)

    # 缓存未被注入污染（其他用户/模板列表仍拿到纯正文）
    assert skill["content"] == before
    # 返回的最终系统提示词 = 纯正文（原样）+ 尾部指令
    assert prompt == before.rstrip() + f"\n\n{ZH_INSTRUCTION}"
    assert before in prompt


@pytest.mark.anyio
async def test_synthetic_skill_body_byte_identical_with_references_appendix():
    """content 含 references 附录拼接产物时同样整体保留：注入只发生在尾部。"""
    body = "工作流正文\n\n---\n\n## 附录：参考资料知识库\n\n### 参考资料：风格\n\n参考内容"
    skill = make_skill(body)
    prompt = await build_skill_system_prompt(skill, db=None, user_id=None)

    assert prompt == body + f"\n\n{ZH_INSTRUCTION}"
    assert skill["content"] == body


# ========== (d) 无 db / 无 user → zh 回落，安全不抛错 ==========


@pytest.mark.anyio
async def test_no_db_no_user_falls_back_to_chinese():
    """无 db / 无 user_id / 无 Settings 行：回落 zh 指令，不抛错。"""
    skill = make_skill()
    assert await build_skill_system_prompt(skill) == skill["content"].rstrip() + f"\n\n{ZH_INSTRUCTION}"
    assert await build_skill_system_prompt(skill, db=None, user_id="u-x") == skill["content"].rstrip() + f"\n\n{ZH_INSTRUCTION}"


@pytest.mark.anyio
async def test_db_user_without_settings_row_falls_back_to_chinese(db_session):
    """有 db 但无 Settings 行：按无偏好处理，回落 zh。"""
    prompt = await build_skill_system_prompt(
        make_skill(), db=db_session, user_id="no-such-user"
    )
    assert prompt.endswith(ZH_INSTRUCTION)


@pytest.mark.anyio
async def test_empty_skill_content_still_gets_instruction():
    """content 缺失/为空的防御路径：不抛错（空正文经 append 语义产出指令段）。"""
    assert await build_skill_system_prompt({"template_key": "SKILL_EMPTY"}) == f"\n\n{ZH_INSTRUCTION}"
