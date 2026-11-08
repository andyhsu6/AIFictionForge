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


def test_rename_payload_strips_edges_and_rejects_blank():
    assert AgentConversationUpdate(title="  两个   词  ").title == "两个   词"
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

def test_rename_conversation_endpoint_updates_title_and_flag(tmp_path):
    """PATCH 重命名：空白归一化、title_user_edited=true、权限校验走项目归属。"""
    import asyncio

    from fastapi import FastAPI, Request
    from fastapi.testclient import TestClient
    from sqlalchemy import select
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    from app.api.project_agent import router as agent_router
    from app.core.errors import register_exception_handlers
    from app.database import Base, get_db
    from app.models.project import Project
    from app.models.project_agent import AgentConversation

    db_path = tmp_path / "rename.db"
    engine = create_async_engine(f"sqlite+aiosqlite:///{db_path}")

    async def _seed():
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        factory = async_sessionmaker(bind=engine, expire_on_commit=False)
        async with factory() as session:
            session.add(Project(id="p-rename", user_id="u-rename", title="占位项目"))
            session.add(
                AgentConversation(
                    id="c-rename",
                    user_id="u-rename",
                    project_id="p-rename",
                    title="占位旧标题",
                )
            )
            await session.commit()

    asyncio.run(_seed())

    async def _override_db():
        factory = async_sessionmaker(bind=engine, expire_on_commit=False)
        async with factory() as session:
            yield session

    app = FastAPI()
    register_exception_handlers(app)
    app.include_router(agent_router, prefix="/api")
    app.dependency_overrides[get_db] = _override_db

    @app.middleware("http")
    async def _fake_auth(request: Request, call_next):
        request.state.user_id = "u-rename"
        return await call_next(request)

    client = TestClient(app, raise_server_exceptions=False)
    try:
        response = client.patch(
            "/api/projects/p-rename/agent/conversations/c-rename",
            json={"title": "  两个   词  "},
        )
        assert response.status_code == 200
        body = response.json()
        assert body["title"] == "两个 词"

        async def _fetch():
            factory = async_sessionmaker(bind=engine, expire_on_commit=False)
            async with factory() as session:
                return (
                    await session.execute(
                        select(AgentConversation).where(
                            AgentConversation.id == "c-rename"
                        )
                    )
                ).scalar_one()

        conversation = asyncio.run(_fetch())
        assert conversation.title == "两个 词"
        assert conversation.title_user_edited is True

        blank = client.patch(
            "/api/projects/p-rename/agent/conversations/c-rename",
            json={"title": "   "},
        )
        assert blank.status_code == 422
    finally:
        app.dependency_overrides.clear()
        asyncio.run(engine.dispose())
