"""issue #262：会话标题模型生成 + 用户重命名保护。"""
import pytest
from pydantic import ValidationError
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.database import Base
from app.models.project_agent import AgentConversation
from app.schemas.project_agent import AgentConversationUpdate
from app.services.agent_conversation_title import (
    TITLE_MAX_CHARS,
    clean_generated_title,
    generate_title_with,
)


class FakeAI:
    def __init__(self, content: str | None):
        self.content = content
        self.calls = 0

    async def generate_text(self, **kwargs):
        self.calls += 1
        return {"content": self.content}


async def _session(tmp_path):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path}/title-gen.db")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(bind=engine, expire_on_commit=False)
    return engine, factory()


def test_clean_generated_title_strips_wrappers_and_caps():
    assert clean_generated_title("  「续写  走向讨论」  ") == "续写 走向讨论"
    assert clean_generated_title('"大纲重排"\n第二行') == "大纲重排"
    assert clean_generated_title("标题。" * 20) == ("标题。" * 20)[:TITLE_MAX_CHARS]
    assert clean_generated_title("   ") == ""
    assert clean_generated_title(None) == ""


def test_rename_payload_normalises_whitespace():
    assert AgentConversationUpdate(title="  两个   词  ").title == "两个 词"
    with pytest.raises(ValidationError):
        AgentConversationUpdate(title="   ")


@pytest.mark.anyio
async def test_generate_title_overwrites_rule_placeholder(tmp_path):
    engine, session = await _session(tmp_path)
    try:
        conversation = AgentConversation(
            user_id="u1", project_id="p1", title="占位规则标题"
        )
        session.add(conversation)
        await session.commit()
        ai = FakeAI("续写走向讨论")

        title = await generate_title_with(
            session, ai, conversation.id, first_message="这本书后续剧情怎么走"
        )

        assert title == "续写走向讨论"
        await session.refresh(conversation)
        assert conversation.title == "续写走向讨论"
        assert conversation.title_user_edited is False
        assert ai.calls == 1
    finally:
        await session.close()
        await engine.dispose()


@pytest.mark.anyio
async def test_user_renamed_title_is_never_overwritten(tmp_path):
    engine, session = await _session(tmp_path)
    try:
        conversation = AgentConversation(
            user_id="u1",
            project_id="p1",
            title="我的标题",
            title_user_edited=True,
        )
        session.add(conversation)
        await session.commit()
        ai = FakeAI("模型标题")

        title = await generate_title_with(
            session, ai, conversation.id, first_message="随便问问"
        )

        assert title == ""
        assert ai.calls == 0
        await session.refresh(conversation)
        assert conversation.title == "我的标题"
    finally:
        await session.close()
        await engine.dispose()


@pytest.mark.anyio
async def test_empty_model_output_keeps_rule_title(tmp_path):
    engine, session = await _session(tmp_path)
    try:
        conversation = AgentConversation(
            user_id="u1", project_id="p1", title="占位规则标题"
        )
        session.add(conversation)
        await session.commit()

        title = await generate_title_with(
            session, FakeAI("   "), conversation.id, first_message="占位问题"
        )

        assert title == ""
        await session.refresh(conversation)
        assert conversation.title == "占位规则标题"
    finally:
        await session.close()
        await engine.dispose()
