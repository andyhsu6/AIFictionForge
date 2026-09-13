"""PR-2c §4 收尾聚合：provider_call_id 配对 / 单条 tool 消息 / 强制 headless 参数。"""
import json
import os
import uuid

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.database import Base
from app.models.project_agent import AgentMessage, AgentToolCall
from app.services import agent_plan_runner as runner


@pytest.fixture
def env_factory():
    return None


@pytest.fixture
async def session_factory():
    """临时文件 SQLite（范本：test_agent_plan_runner.py 的 env），绝不碰 .env 开发库。"""
    db_path = f"/tmp/test_plan_closing_{uuid.uuid4().hex}.db"
    engine = create_async_engine(f"sqlite+aiosqlite:///{db_path}")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(bind=engine, class_=AsyncSession, expire_on_commit=False)
    try:
        yield factory
    finally:
        await engine.dispose()
        if os.path.exists(db_path):
            os.remove(db_path)


def _tool_call_row(*, message_id: str | None) -> AgentToolCall:
    return AgentToolCall(
        id=str(uuid.uuid4()),
        conversation_id="conv-1",
        message_id=message_id,
        user_id="u-1",
        project_id="p-1",
        tool_name="propose_plan",
        arguments={"objective": "run three steps", "steps": []},
        risk_level=0,
        requires_confirmation=False,
        status="executing",
    )


@pytest.mark.anyio
async def test_provider_id_taken_from_assistant_message(session_factory):
    """provider 回了 id ⇒ 聚合消息必须挂那个 id，而不是 AgentToolCall.id。"""
    async with session_factory() as db:
        assistant = AgentMessage(
            conversation_id="conv-1", role="assistant", content="",
            tool_calls=json.dumps([{"id": "call_provider_9",
                                    "function": {"name": "propose_plan", "arguments": "{}"}}]),
        )
        db.add(assistant)
        await db.flush()
        record = _tool_call_row(message_id=assistant.id)
        db.add(record)
        await db.commit()
        provider_call_id = await runner.resolve_provider_call_id(
            session_factory, tool_call_id=record.id, conversation_id="conv-1"
        )
    assert provider_call_id == "call_provider_9"
    assert provider_call_id != record.id


@pytest.mark.anyio
async def test_missing_provider_id_falls_back_to_tool_call_id(session_factory):
    """:906 回退分支 ⇒ 同值合法。任何逻辑不得假设两者必然不同。"""
    async with session_factory() as db:
        assistant = AgentMessage(
            conversation_id="conv-1", role="assistant", content="",
            tool_calls=json.dumps([{"id": "",
                                    "function": {"name": "propose_plan", "arguments": "{}"}}]),
        )
        db.add(assistant)
        await db.flush()
        record = _tool_call_row(message_id=assistant.id)
        db.add(record)
        await db.commit()
        provider_call_id = await runner.resolve_provider_call_id(
            session_factory, tool_call_id=record.id, conversation_id="conv-1"
        )
    assert provider_call_id == record.id


@pytest.mark.anyio
async def test_no_assistant_message_still_resolves(session_factory):
    """特判分支可能没写 message_id ⇒ 退化成 AgentToolCall.id，绝不返回 None 让收尾跳过。"""
    async with session_factory() as db:
        record = _tool_call_row(message_id=None)
        db.add(record)
        await db.commit()
        provider_call_id = await runner.resolve_provider_call_id(
            session_factory, tool_call_id=record.id, conversation_id="conv-1"
        )
    assert provider_call_id == record.id


@pytest.mark.anyio
async def test_task_input_provider_id_wins(session_factory):
    async with session_factory() as db:
        record = _tool_call_row(message_id=None)
        db.add(record)
        await db.commit()
        provider_call_id = await runner.resolve_provider_call_id(
            session_factory, tool_call_id=record.id, conversation_id="conv-1",
            plan_task_input={"provider_call_id": "call_from_task_input"},
        )
    assert provider_call_id == "call_from_task_input"
