"""会话读取路径必须把持久化的 tool_calls 交给前端（永久转圈缺陷回归护栏）。

事故：仅含工具调用的 assistant 轮次 content==""，工具负载写在
agent_messages.tool_calls（TEXT，JSON 字符串）。AgentMessageResponse 此前没有
tool_calls 字段，GET /conversations/{id} 序列化时静默丢掉该列，前端无法区分
「历史工具轮」与「本轮仍在流式」，于是历史消息永久渲染 <Spin>。

契约（与前端 AgentMessage.tool_calls?: string 对齐）：线上形态就是该列的原始
JSON 字符串（前端 countToolCalls 自己 JSON.parse），后端只做透传；NULL/空串/
非法 JSON 一律不得抛错，无工具调用的消息行为保持不变。

夹具一律中性占位，不含任何导入原文、角色人名或书名（AGENTS.md 脱敏硬约束）。
"""
import json
import os
import uuid

import pytest
from fastapi import FastAPI, Request
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import app.models  # noqa: F401 —— 注册全部表，create_all 需要
from app.api.project_agent import router
from app.database import Base, get_db
from app.models.project import Project
from app.models.project_agent import AgentConversation, AgentMessage

PROJECT_ID = "proj-toolcalls"
CONVERSATION_ID = "conv-toolcalls"

# 4 条工具调用（与事故中 4 条的那条消息同形），全部为占位工具名/参数。
TOOL_CALLS = [
    {
        "id": f"call-{index}",
        "type": "function",
        "function": {"name": "read_project_overview", "arguments": "{}"},
    }
    for index in range(4)
]


@pytest.fixture
async def db_session():
    db_path = f"/tmp/test_agent_toolcalls_{uuid.uuid4().hex}.db"
    engine = create_async_engine(f"sqlite+aiosqlite:///{db_path}")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    Session = async_sessionmaker(bind=engine, expire_on_commit=False)
    async with Session() as session:
        yield session
    await engine.dispose()
    if os.path.exists(db_path):
        os.remove(db_path)


def _make_client(db_session) -> AsyncClient:
    app = FastAPI()

    @app.middleware("http")
    async def _authenticate(request: Request, call_next):
        request.state.user_id = "test"
        return await call_next(request)

    app.include_router(router)

    async def _override_db():
        yield db_session

    app.dependency_overrides[get_db] = _override_db
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


async def _seed(db_session, *messages: AgentMessage) -> None:
    db_session.add(Project(id=PROJECT_ID, user_id="test", title="project"))
    db_session.add(AgentConversation(
        id=CONVERSATION_ID, user_id="test", project_id=PROJECT_ID, title="conversation",
    ))
    for message in messages:
        message.conversation_id = CONVERSATION_ID
        db_session.add(message)
    await db_session.commit()


async def _get_messages(client: AsyncClient) -> list[dict]:
    response = await client.get(
        f"/projects/{PROJECT_ID}/agent/conversations/{CONVERSATION_ID}"
    )
    assert response.status_code == 200, response.text
    return response.json()["messages"]


@pytest.mark.anyio
async def test_conversation_get_returns_persisted_tool_calls(db_session):
    """工具轮（content 为空）必须带着 4 条工具调用原样返回。"""
    raw = json.dumps(TOOL_CALLS)
    await _seed(db_session, AgentMessage(id="m-tools", role="assistant", content="", tool_calls=raw))

    async with _make_client(db_session) as client:
        messages = await _get_messages(client)

    tool_message = next(message for message in messages if message["id"] == "m-tools")
    assert tool_message["tool_calls"] == raw  # 线形态 = 原始 JSON 字符串
    assert len(json.loads(tool_message["tool_calls"])) == 4


@pytest.mark.anyio
async def test_message_without_tool_calls_is_returned_unchanged(db_session):
    """无工具调用的消息保持原行为：content/role 不变，tool_calls 为 null。"""
    await _seed(db_session, AgentMessage(id="m-plain", role="assistant", content="neutral history reply"))

    async with _make_client(db_session) as client:
        messages = await _get_messages(client)

    plain = next(message for message in messages if message["id"] == "m-plain")
    assert plain["content"] == "neutral history reply"
    assert plain["role"] == "assistant"
    assert plain["tool_calls"] is None


@pytest.mark.anyio
async def test_invalid_empty_and_wrapped_tool_calls_never_raise(db_session):
    """非法/空/NULL JSON 必须原样透传（读取路径不得 500），包裹形态也不丢。"""
    await _seed(
        db_session,
        AgentMessage(id="m-invalid", role="assistant", content="", tool_calls="not-json{{"),
        AgentMessage(id="m-empty", role="assistant", content="", tool_calls=""),
        AgentMessage(id="m-null", role="assistant", content="", tool_calls=None),
        AgentMessage(
            id="m-wrapped", role="assistant", content="",
            tool_calls=json.dumps({"tool_calls": TOOL_CALLS}),
        ),
    )

    async with _make_client(db_session) as client:
        messages = await _get_messages(client)

    by_id = {message["id"]: message for message in messages}
    assert by_id["m-invalid"]["tool_calls"] == "not-json{{"
    assert by_id["m-empty"]["tool_calls"] == ""
    assert by_id["m-null"]["tool_calls"] is None
    assert len(json.loads(by_id["m-wrapped"]["tool_calls"])["tool_calls"]) == 4
