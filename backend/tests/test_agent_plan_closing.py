"""PR-2c §4 收尾聚合：provider_call_id 配对 / 单条 tool 消息 / 强制 headless 参数。"""
import json
import os
import uuid
from datetime import datetime
from typing import Any

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.database import Base
from app.models.project_agent import AgentConversation, AgentMessage, AgentToolCall
from app.services import agent_plan_runner as runner
from app.services.agent_plan_schema import validate_plan

SECRET_CHAPTER_TEXT = "PLACEHOLDER-NEUTRAL-BODY-TEXT-9f3c"


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


def _handle_with_steps(count: int = 3) -> runner._PlanHandle:
    """_PlanHandle 真实形状 + step_results 真实条目；条目刻意多带 result/detail 哨兵键，
    聚合白名单必须把它们挡在 payload 外（不查库）。"""
    handle = runner._PlanHandle(
        plan_task_id="plan-agg-1",
        user_id="u-1",
        project_id="p-1",
        conversation_id="conv-agg-1",
        steps=[
            {"id": f"s{i}", "tool": "list_background_tasks" if i < count else "start_project_task",
             "action": "" if i < count else "generate_chapter",
             "arguments": ({"chapter_number": i} if i < count
                           else {"chapter_number": i, "prompt": SECRET_CHAPTER_TEXT}),
             "note": ""}
            for i in range(1, count + 1)
        ],
        tool_call_id="tool-anchor-1",
        task_input={"tool_call_id": "tool-anchor-1", "objective": "aggregate the steps"},
    )
    handle.step_results = [
        {"index": i, "action": "list_background_tasks", "status": "completed",
         "inline": True, "entity_id": "", "detail": {"body": SECRET_CHAPTER_TEXT}}
        for i in range(1, count)
    ] + [
        {"index": count, "action": "generate_chapter", "status": "failed",
         "inline": False, "sub_task_id": f"task-{count}",
         "sub_task_type": "chapter_generate", "sub_task_status": "failed",
         "sub_task_progress": 40, "error": "step failed",
         "error_code": "internal.agent_plan_step_failed",
         "result": {"body": SECRET_CHAPTER_TEXT}},
    ]
    handle.steps_done = max(count - 1, 0)
    handle.failed_at_step = count
    return handle


@pytest.fixture
def handle_with_steps():
    return _handle_with_steps(3)


class ClosingAIService:
    """收尾 LLM 出口：捕获 kwargs；流式/JSON 重试出口一律炸（runner 不得使用）。"""

    default_model = "closing-test-model"

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    async def generate_text(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
        self.calls.append({"args": args, "kwargs": kwargs})
        return {
            "content": "计划已执行完成，共 2/3 步。",
            "model": self.default_model,
            "usage": {"prompt_tokens": 11, "completion_tokens": 7},
        }

    async def generate_text_stream(self, *args: Any, **kwargs: Any):
        raise AssertionError("收尾不得使用流式出口")
        yield ""  # pragma: no cover —— 保持 async generator 形状

    async def generate_text_stream_full(self, *args: Any, **kwargs: Any):
        raise AssertionError("收尾不得使用流式出口")
        yield ""  # pragma: no cover

    async def call_with_json_retry(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
        raise AssertionError("收尾不得使用 JSON 重试出口")


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
async def test_two_proposals_same_steps_different_arguments_resolve_own_id(session_factory):
    """同 objective、同 (id, tool) 序列、仅步 arguments 不同的两份计划：
    旧计划获批时必须命中旧 entry，不得被新 entry 的 arguments 冒领。"""
    raw_old = {"objective": "shared objective",
               "steps": [{"id": "s1", "tool": "list_background_tasks", "arguments": {"limit": 3}}]}
    raw_new = {"objective": "shared objective",
               "steps": [{"id": "s1", "tool": "list_background_tasks", "arguments": {"limit": 5}}]}
    validated_old = validate_plan(raw_old, allowed_tools={"list_background_tasks"})
    async with session_factory() as db:
        db.add(AgentMessage(
            conversation_id="conv-args", role="assistant", content="",
            tool_calls=json.dumps([{
                "id": "call-a",
                "function": {"name": "propose_plan", "arguments": json.dumps(raw_old)},
            }]),
        ))
        await db.flush()
        db.add(AgentMessage(
            conversation_id="conv-args", role="assistant", content="",
            tool_calls=json.dumps([{
                "id": "call-b",
                "function": {"name": "propose_plan", "arguments": json.dumps(raw_new)},
            }]),
        ))
        await db.flush()
        card = AgentMessage(
            conversation_id="conv-args", role="assistant", content="plan card",
        )
        db.add(card)
        await db.flush()
        record = _tool_call_row(message_id=card.id, arguments=validated_old)
        db.add(record)
        await db.commit()
        provider_call_id = await runner.resolve_provider_call_id(
            session_factory, tool_call_id=record.id, conversation_id="conv-args",
        )
    assert provider_call_id == "call-a"


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


@pytest.mark.anyio
async def test_one_plan_writes_exactly_one_tool_message(session_factory, handle_with_steps):
    async with session_factory() as db:
        db.add(AgentConversation(
            id=handle_with_steps.conversation_id, user_id="u-1", project_id="p-1",
            title="plan conv", last_message_at=datetime(2020, 1, 1, 0, 0, 0),
        ))
        await db.commit()
    message_id = await runner._insert_plan_summary_message(
        session_factory, handle=handle_with_steps, provider_call_id="call_agg",
        outcome="completed", summary="plan finished",
    )
    async with session_factory() as db:
        rows = (await db.execute(select(AgentMessage).where(
            AgentMessage.conversation_id == handle_with_steps.conversation_id
        ))).scalars().all()
        conversation = await db.get(AgentConversation, handle_with_steps.conversation_id)
    assert message_id
    assert [row.role for row in rows] == ["tool"]
    assert all(row.role != "system" for row in rows)
    tool_row = rows[0]
    assert tool_row.id == message_id
    assert tool_row.tool_call_id == "call_agg"
    stored = json.loads(tool_row.content)
    assert stored["tool"] == "plan_run_summary"
    assert stored["result"]["outcome"] == "completed"
    assert stored["result"]["steps_total"] == 3
    assert stored["result"]["steps_done"] == 2
    assert conversation.last_message_at > datetime(2020, 1, 1, 0, 0, 0)


@pytest.mark.anyio
async def test_summary_payload_carries_no_step_result_blobs(session_factory, handle_with_steps):
    payload = runner.build_plan_summary_payload(handle_with_steps, "completed", "plan finished")
    dumped = json.dumps(payload, ensure_ascii=False, default=str)
    assert SECRET_CHAPTER_TEXT not in dumped
    assert all(
        SECRET_CHAPTER_TEXT not in json.dumps(step, ensure_ascii=False, default=str)
        for step in payload["steps"]
    )
    assert set().union(*(set(step) for step in payload["steps"])) <= {
        "id", "tool", "status", "action", "sub_task_id", "task_type", "error_code",
    }
    await runner._insert_plan_summary_message(
        session_factory, handle=handle_with_steps, provider_call_id="call_agg",
        outcome="completed", summary="plan finished",
    )
    async with session_factory() as db:
        content = (await db.execute(select(AgentMessage.content).where(
            AgentMessage.conversation_id == handle_with_steps.conversation_id
        ))).scalar_one()
    assert SECRET_CHAPTER_TEXT not in content


@pytest.mark.anyio
async def test_summary_is_idempotent_per_plan(session_factory, handle_with_steps):
    first = await runner._insert_plan_summary_message(
        session_factory, handle=handle_with_steps, provider_call_id="call_agg",
        outcome="completed", summary="plan finished",
    )
    second = await runner._insert_plan_summary_message(
        session_factory, handle=handle_with_steps, provider_call_id="call_agg",
        outcome="failed", summary="plan failed",
    )
    assert first == second
    async with session_factory() as db:
        rows = (await db.execute(select(AgentMessage).where(
            AgentMessage.conversation_id == handle_with_steps.conversation_id
        ))).scalars().all()
    assert [row.role for row in rows] == ["tool"]


@pytest.mark.anyio
async def test_closing_call_carries_mandatory_headless_params(session_factory, handle_with_steps):
    ai = ClosingAIService()
    handle_with_steps.ai_service = ai
    await runner._closing_stage(handle_with_steps, session_factory, "completed", "plan finished")
    assert len(ai.calls) == 1
    call = ai.calls[0]
    assert call["args"] == ()
    kwargs = call["kwargs"]
    assert kwargs["tools"] is None
    assert kwargs["auto_mcp"] is False
    assert kwargs["handle_tool_calls"] is False
    assert "plan_run_summary" in kwargs["prompt"] or "steps_total" in kwargs["prompt"]
    assert "灵创创作助手" in kwargs["system_prompt"]


@pytest.mark.anyio
async def test_closing_writes_one_assistant_and_one_tool_row(session_factory, handle_with_steps):
    ai = ClosingAIService()
    handle_with_steps.ai_service = ai
    await runner._closing_stage(handle_with_steps, session_factory, "completed", "plan finished")
    async with session_factory() as db:
        rows = (await db.execute(select(AgentMessage).where(
            AgentMessage.conversation_id == handle_with_steps.conversation_id
        ).order_by(AgentMessage.created_at.asc()))).scalars().all()
    assert [row.role for row in rows] == ["tool", "assistant"]
    assistant = rows[1]
    assert assistant.content == "计划已执行完成，共 2/3 步。"
    assert assistant.model == "closing-test-model"
    assert assistant.prompt_tokens == 11
    assert assistant.completion_tokens == 7
    assert assistant.tool_call_id is None


@pytest.mark.anyio
async def test_cancelled_plan_skips_llm_but_still_persists_facts(session_factory, handle_with_steps):
    ai = ClosingAIService()
    handle_with_steps.ai_service = ai
    handle_with_steps.cancel_requested = True
    await runner._closing_stage(handle_with_steps, session_factory, "cancelled", "计划已取消")
    assert ai.calls == []
    async with session_factory() as db:
        rows = (await db.execute(select(AgentMessage).where(
            AgentMessage.conversation_id == handle_with_steps.conversation_id
        ))).scalars().all()
    assert [row.role for row in rows] == ["tool"]
    stored = json.loads(rows[0].content)
    assert stored["result"]["outcome"] == "cancelled"


@pytest.mark.anyio
async def test_missing_ai_service_never_breaks_the_plan(session_factory, handle_with_steps):
    handle_with_steps.ai_service = None
    await runner._closing_stage(handle_with_steps, session_factory, "completed", "plan finished")
    async with session_factory() as db:
        rows = (await db.execute(select(AgentMessage).where(
            AgentMessage.conversation_id == handle_with_steps.conversation_id
        ))).scalars().all()
    assert [row.role for row in rows] == ["tool"]


@pytest.mark.anyio
async def test_llm_calls_stay_one_regardless_of_step_count(session_factory):
    async def close_with(count: int) -> ClosingAIService:
        handle = _handle_with_steps(count)
        ai = ClosingAIService()
        handle.ai_service = ai
        await runner._closing_stage(handle, session_factory, "completed", "plan finished")
        return ai

    three = await close_with(3)
    eight = await close_with(8)
    assert len(three.calls) == 1
    assert len(eight.calls) == 1
