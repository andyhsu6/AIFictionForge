"""issue #260：创作助手会话标题必须来自首条用户消息。"""
from types import SimpleNamespace

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.database import Base
from app.models.project_agent import AgentConversation, AgentMessage
from app.services.project_agent_service import (
    DEFAULT_CONVERSATION_TITLE,
    ProjectAgentService,
    derive_conversation_title,
)


async def _make_service(tmp_path):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path}/agent-title.db")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    session_factory = async_sessionmaker(bind=engine, expire_on_commit=False)
    db = session_factory()
    service = ProjectAgentService(
        db=db,
        ai_service=SimpleNamespace(),
        project=SimpleNamespace(id="proj-title-1"),
        user_id="user-title-1",
    )
    return engine, db, service


@pytest.mark.anyio
async def test_precreated_conversation_gets_title_from_first_message(tmp_path):
    engine, db, service = await _make_service(tmp_path)
    try:
        conversation = AgentConversation(
            user_id="user-title-1", project_id="proj-title-1", title=DEFAULT_CONVERSATION_TITLE
        )
        db.add(conversation)
        await db.flush()

        returned = await service.get_or_create_conversation(
            conversation.id, "  占位第一句话   带空格  "
        )

        assert returned.id == conversation.id
        assert returned.title == "占位第一句话 带空格"
    finally:
        await db.close()
        await engine.dispose()


@pytest.mark.anyio
async def test_historical_default_title_uses_first_stored_user_message(tmp_path):
    engine, db, service = await _make_service(tmp_path)
    try:
        conversation = AgentConversation(
            user_id="user-title-1", project_id="proj-title-1", title=DEFAULT_CONVERSATION_TITLE
        )
        db.add(conversation)
        await db.flush()
        db.add(
            AgentMessage(
                conversation_id=conversation.id,
                role="user",
                content="占位历史第一条消息",
            )
        )
        await db.flush()

        returned = await service.get_or_create_conversation(
            conversation.id, "占位本轮新消息"
        )

        assert returned.title == "占位历史第一条消息"
    finally:
        await db.close()
        await engine.dispose()


@pytest.mark.anyio
async def test_user_title_is_never_overwritten(tmp_path):
    engine, db, service = await _make_service(tmp_path)
    try:
        conversation = AgentConversation(
            user_id="user-title-1", project_id="proj-title-1", title="占位自定义标题"
        )
        db.add(conversation)
        await db.flush()

        returned = await service.get_or_create_conversation(
            conversation.id, "占位第一句话"
        )

        assert returned.title == "占位自定义标题"
    finally:
        await db.close()
        await engine.dispose()


@pytest.mark.anyio
async def test_implicit_conversation_creation_derives_title(tmp_path):
    engine, db, service = await _make_service(tmp_path)
    try:
        returned = await service.get_or_create_conversation(None, "占位新会话第一句")

        assert returned.title == "占位新会话第一句"
    finally:
        await db.close()
        await engine.dispose()


def test_derive_title_normalises_and_caps_length():
    assert derive_conversation_title("  多  空格   单行  ") == "多 空格 单行"
    assert derive_conversation_title(" ") == DEFAULT_CONVERSATION_TITLE
    assert len(derive_conversation_title("甲" * 60)) == 40


def test_derive_title_uses_first_non_empty_line():
    message = "\n- 一、项目概况\n项目：占位\nID：占位\n"
    assert derive_conversation_title(message) == "一、项目概况"


def test_backfill_script_plans_only_default_titles(tmp_path):
    import sqlite3

    from scripts.backfill_agent_conversation_titles import plan_updates

    db_path = tmp_path / "backfill.db"
    conn = sqlite3.connect(db_path)
    try:
        conn.execute("CREATE TABLE agent_conversations (id TEXT PRIMARY KEY, title TEXT)")
        conn.execute(
            "CREATE TABLE agent_messages (conversation_id TEXT, role TEXT, content TEXT, created_at TEXT)"
        )
        conn.executemany(
            "INSERT INTO agent_conversations (id, title) VALUES (?, ?)",
            [
                ("c-default", DEFAULT_CONVERSATION_TITLE),
                ("c-custom", "占位自定义标题"),
                ("c-empty", ""),
                ("c-no-msg", DEFAULT_CONVERSATION_TITLE),
            ],
        )
        conn.executemany(
            "INSERT INTO agent_messages (conversation_id, role, content, created_at) VALUES (?, ?, ?, ?)",
            [
                ("c-default", "user", "占位历史消息", "2026-01-01T00:00:00"),
                ("c-custom", "user", "占位另一条消息", "2026-01-01T00:00:00"),
            ],
        )
        conn.commit()

        planned = plan_updates(conn)
        assert dict(planned) == {"c-default": "占位历史消息"}
    finally:
        conn.close()
