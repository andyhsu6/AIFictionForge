"""PR-2c §4 收尾聚合：provider_call_id 配对 / 单条 tool 消息 / 强制 headless 参数。"""
import json
import os
import uuid

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.database import Base
from app.models.project_agent import AgentMessage, AgentToolCall
from app.services import agent_plan_runner as runner
from app.services.agent_plan_schema import validate_plan


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


def _tool_call_row(*, message_id: str | None,
                   arguments: dict | None = None) -> AgentToolCall:
    return AgentToolCall(
        id=str(uuid.uuid4()),
        conversation_id="conv-1",
        message_id=message_id,
        user_id="u-1",
        project_id="p-1",
        tool_name="propose_plan",
        arguments=arguments if arguments is not None else {"objective": "run three steps", "steps": []},
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


@pytest.mark.anyio
async def test_conversation_scan_recovers_stale_provider_link(session_factory):
    """生产形状：record.message_id 指向 tool_calls=NULL 的计划卡，且
    record.arguments 是 validate_plan 归一化后的计划（≠ 原始 provider arguments）
    ⇒ 全量 dict 相等恒不成立，必须靠 tolerant key 扫会话把 id 找回来。"""
    raw_args = {"objective": "run three steps",
                "steps": [{"id": "s1", "tool": "list_background_tasks", "arguments": {}}]}
    validated = validate_plan(raw_args, allowed_tools={"list_background_tasks"})
    assert validated != raw_args
    async with session_factory() as db:
        early = AgentMessage(
            conversation_id="conv-real", role="assistant", content="",
            tool_calls=json.dumps([{
                "id": "call-real-1",
                "function": {"name": "propose_plan", "arguments": json.dumps(raw_args)},
            }]),
        )
        db.add(early)
        await db.flush()
        card = AgentMessage(
            conversation_id="conv-real", role="assistant", content="plan card",
        )
        db.add(card)
        await db.flush()
        record = _tool_call_row(message_id=card.id, arguments=validated)
        db.add(record)
        await db.commit()
        provider_call_id = await runner.resolve_provider_call_id(
            session_factory, tool_call_id=record.id, conversation_id="conv-real",
        )
    assert provider_call_id == "call-real-1"


@pytest.mark.anyio
async def test_two_proposals_older_record_resolves_its_own_id(session_factory):
    """同会话两份计划：旧计划获批时，扫会话必须按 objective/id/tool 命中旧 entry，
    不得拿最新一条 propose_plan 的 id（生产链路的 record.message_id 指向计划卡）。"""
    raw_old = {"objective": "older plan",
               "steps": [{"id": "s1", "tool": "list_background_tasks", "arguments": {}}]}
    raw_new = {"objective": "newer plan",
               "steps": [{"id": "s1", "tool": "get_project_stats", "arguments": {}}]}
    validated_old = validate_plan(raw_old, allowed_tools={"list_background_tasks"})
    async with session_factory() as db:
        db.add(AgentMessage(
            conversation_id="conv-two", role="assistant", content="",
            tool_calls=json.dumps([{
                "id": "call-old",
                "function": {"name": "propose_plan", "arguments": json.dumps(raw_old)},
            }]),
        ))
        await db.flush()
        db.add(AgentMessage(
            conversation_id="conv-two", role="assistant", content="",
            tool_calls=json.dumps([{
                "id": "call-new",
                "function": {"name": "propose_plan", "arguments": json.dumps(raw_new)},
            }]),
        ))
        await db.flush()
        card = AgentMessage(
            conversation_id="conv-two", role="assistant", content="plan card",
        )
        db.add(card)
        await db.flush()
        record = _tool_call_row(message_id=card.id, arguments=validated_old)
        db.add(record)
        await db.commit()
        provider_call_id = await runner.resolve_provider_call_id(
            session_factory, tool_call_id=record.id, conversation_id="conv-two",
        )
    assert provider_call_id == "call-old"


@pytest.mark.anyio
async def test_conversation_scan_without_propose_plan_entry_falls_back(session_factory):
    async with session_factory() as db:
        other = AgentMessage(
            conversation_id="conv-plain", role="assistant", content="",
            tool_calls=json.dumps([{"id": "call-other",
                                    "function": {"name": "get_project_stats", "arguments": "{}"}}]),
        )
        db.add(other)
        await db.flush()
        record = _tool_call_row(message_id=None)
        db.add(record)
        await db.commit()
        provider_call_id = await runner.resolve_provider_call_id(
            session_factory, tool_call_id=record.id, conversation_id="conv-plain",
        )
    assert provider_call_id == record.id


@pytest.mark.anyio
async def test_newest_entry_fallback_when_record_args_unmatchable(session_factory):
    """record.arguments 拿不到配对键（空 dict）时，回退到最新一条 propose_plan entry。"""
    raw_args = {"objective": "fallback plan",
                "steps": [{"id": "s1", "tool": "list_background_tasks", "arguments": {}}]}
    async with session_factory() as db:
        db.add(AgentMessage(
            conversation_id="conv-fallback", role="assistant", content="",
            tool_calls=json.dumps([{
                "id": "call-fallback",
                "function": {"name": "propose_plan", "arguments": json.dumps(raw_args)},
            }]),
        ))
        record = _tool_call_row(message_id=None, arguments={})
        db.add(record)
        await db.commit()
        provider_call_id = await runner.resolve_provider_call_id(
            session_factory, tool_call_id=record.id, conversation_id="conv-fallback",
        )
    assert provider_call_id == "call-fallback"


@pytest.mark.anyio
async def test_malformed_tool_calls_never_raise(session_factory):
    """N1：畸形 entry 只跳过，绝不让收尾崩掉。"""
    payloads = [
        json.dumps(["not-a-dict"]),
        json.dumps([{"id": "x", "function": "propose_plan"}]),
        json.dumps([{"id": "", "function": {"name": "propose_plan", "arguments": "{}"}}]),
        "{not-json",
    ]
    async with session_factory() as db:
        for idx, payload in enumerate(payloads):
            db.add(AgentMessage(
                conversation_id="conv-bad", role="assistant", content=str(idx),
                tool_calls=payload,
            ))
        record = _tool_call_row(message_id=None)
        db.add(record)
        await db.commit()
        provider_call_id = await runner.resolve_provider_call_id(
            session_factory, tool_call_id=record.id, conversation_id="conv-bad",
        )
    assert provider_call_id == record.id


@pytest.mark.anyio
async def test_whitespace_provider_id_is_ignored(session_factory):
    """N6：纯空白 id 视为缺失，不得原样上送。"""
    async with session_factory() as db:
        record = _tool_call_row(message_id=None)
        db.add(record)
        await db.commit()
        provider_call_id = await runner.resolve_provider_call_id(
            session_factory, tool_call_id=record.id, conversation_id="conv-none",
            plan_task_input={"provider_call_id": "   "},
        )
    assert provider_call_id == record.id
