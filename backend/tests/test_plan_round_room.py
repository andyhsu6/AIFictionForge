"""规划回合的收口重试轮：在 MAX_TOOL_ROUNDS 之后补 PLAN_MAX_RETRIES 个带工具的轮（issue #98）。

背景（真实验收复现）：收口轮里被产出校验拒收的计划，下一轮必须还能重试；只按
`MAX_TOOL_ROUNDS` 收束时收口轮就是最后一个带工具的轮，重试无处可发 ⇒ 整回合没有计划。
本文件钉：被拒后可重试成功、三次尝试后仍有可读收口、非规划回合逐字不变、
规划回合的 force_answer 推到 index 6、收口轮强制执行在补出的轮里原样生效。

只读工具/中性夹具：禁止出现任何真实书名、人名、正文片段（AGENTS.md 脱敏硬约束）。
"""
from __future__ import annotations

import os
import uuid
from types import SimpleNamespace

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.database import Base
from app.models.project import Project
from app.models.project_agent import (
    AgentConversation,
    AgentMessage,
    AgentToolCall,
)
from app.services.agent_plan_schema import PROPOSE_PLAN_TOOL_NAME
from app.services.project_agent_service import ProjectAgentService
from support.agent_stubs import AgentAIServiceStub

PROJECT_ID = "proj-plan-room"
USER_ID = "u-plan-room"
READ_TOOL = "list_outlines"
LEDGER_TOOL = "list_foreshadows"


@pytest.fixture
async def db_engine():
    db_path = f"/tmp/test_plan_round_room_{uuid.uuid4().hex}.db"
    engine = create_async_engine(f"sqlite+aiosqlite:///{db_path}")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield engine
    await engine.dispose()
    if os.path.exists(db_path):
        os.remove(db_path)


@pytest.fixture
async def db_session(db_engine):
    Session = async_sessionmaker(bind=db_engine, expire_on_commit=False)
    async with Session() as session:
        yield session


class RoundScriptedAIService(AgentAIServiceStub):
    """按轮次脚本回放模型出口。

    `generate_text`（工具决策轮）按调用序号取脚本；`generate_text_stream_full`
    （force_answer 轮：无工具、流式）恒返回纯文本 —— 真实模型手里没有工具时
    不可能提交 propose_plan，替身必须同样做不到，否则 (a) 会假绿。
    """

    def __init__(self) -> None:
        super().__init__(default_model="mock-model", base_url="")
        self.scripted: list[dict] = []
        self.final_text = "我需要更多信息。"
        self.tool_round_calls: list[dict] = []
        self.stream_calls: list[dict] = []

    def resolve_dispatch_provider(self, provider=None) -> str:
        return "openai"

    def is_thinking_model_active(self) -> bool:
        return False

    async def generate_text(self, **kwargs):
        self.tool_round_calls.append(kwargs)
        index = len(self.tool_round_calls) - 1
        if index < len(self.scripted):
            return self.scripted[index]
        return {"content": self.final_text, "tool_calls": [], "usage": {}}

    async def generate_text_stream_full(self, **kwargs):
        self.stream_calls.append(kwargs)
        return {"content": self.final_text, "tool_calls": [], "usage": {}}


@pytest.fixture
async def env(db_engine, db_session):
    db_session.add(Project(id=PROJECT_ID, user_id=USER_ID, title="neutral project"))
    conversation = AgentConversation(
        user_id=USER_ID, project_id=PROJECT_ID, title="planning turn"
    )
    db_session.add(conversation)
    await db_session.commit()

    ai = RoundScriptedAIService()
    service = ProjectAgentService(
        db=db_session,
        ai_service=ai,
        project=Project(id=PROJECT_ID, user_id=USER_ID, title="neutral project"),
        user_id=USER_ID,
    )
    yield SimpleNamespace(
        service=service,
        ai=ai,
        engine=db_engine,
        conversation_id=conversation.id,
    )


async def run_turn(env, *, plan_mode: bool) -> list[dict]:
    return [
        event async for event in env.service.stream_chat(
            conversation_id=env.conversation_id,
            message="按台账提交一份执行计划",
            page_context={"route": f"/project/{PROJECT_ID}"},
            plan_mode=plan_mode,
        )
    ]


def tool_call(name: str, arguments: dict, call_id: str = "call-1") -> dict:
    return {
        "content": "我先调用工具。",
        "tool_calls": [{"id": call_id, "function": {"name": name, "arguments": arguments}}],
        "usage": {"prompt_tokens": 10, "completion_tokens": 5},
    }


def _read_round(call_id: str) -> dict:
    return tool_call(READ_TOOL, {}, call_id=call_id)


def ledger_plan(limit: int, *, step_id: str = "s4") -> dict:
    """忠实形状的台账计划：步骤参数与交接文档同构（只有 limit 不同）。"""
    return {
        "objective": "补齐伏笔台账",
        "steps": [
            {"id": step_id, "tool": LEDGER_TOOL, "arguments": {"limit": limit}},
        ],
    }


def _plan_response(plan: dict, call_id: str) -> dict:
    return tool_call(PROPOSE_PLAN_TOOL_NAME, plan, call_id=call_id)


async def read_tool_calls(env) -> list[AgentToolCall]:
    Session = async_sessionmaker(bind=env.engine, expire_on_commit=False)
    async with Session() as session:
        return list((await session.execute(
            select(AgentToolCall).where(AgentToolCall.project_id == PROJECT_ID)
        )).scalars().all())


async def read_messages(env, role: str) -> list[AgentMessage]:
    Session = async_sessionmaker(bind=env.engine, expire_on_commit=False)
    async with Session() as session:
        return list((await session.execute(
            select(AgentMessage).where(
                AgentMessage.conversation_id == env.conversation_id,
                AgentMessage.role == role,
            )
        )).scalars().all())


def round_tools(call: dict) -> list[str]:
    return [item["function"]["name"] for item in (call["tools"] or [])]


def final_content(events: list[dict]) -> str:
    chunks = [event for event in events if event["type"] == "final_chunk"]
    assert chunks, "回合必须以可读文案收口"
    return chunks[-1]["content"]


def result_status(events: list[dict]) -> str:
    final = events[-1]
    assert final["type"] == "result"
    return final["data"]["status"]


@pytest.mark.anyio
async def test_rejected_plan_in_closing_round_retries_with_tools_next_round(env):
    """(a) 验收形状：收口轮因 limit 越界被拒，下一轮必须还能提交计划。

    修复前：index 3 是最后一个带工具的轮，index 4 是 force_answer（无工具、流式），
    第二次提交无处可发 ⇒ 整回合没有计划。修复后 index 4 仍是收口轮（只给
    propose_plan），第二次尝试用 limit=120 的忠实计划成功。
    """
    env.ai.scripted = [
        _read_round("c1"),
        _read_round("c2"),
        _read_round("c3"),
        _plan_response(ledger_plan(501), "c-over-cap"),
        _plan_response(ledger_plan(120), "c-faithful"),
    ]
    events = await run_turn(env, plan_mode=True)

    assert len(env.ai.tool_round_calls) == 5, "被拒后必须发生第二轮工具轮尝试"
    assert env.ai.stream_calls == [], "修复后不应走到无工具的 force_answer 轮"
    assert round_tools(env.ai.tool_round_calls[3]) == [PROPOSE_PLAN_TOOL_NAME]
    assert round_tools(env.ai.tool_round_calls[4]) == [PROPOSE_PLAN_TOOL_NAME]

    rows = await read_tool_calls(env)
    rejected = [row for row in rows if row.status == "failed"]
    assert len(rejected) == 1
    assert "limit 不能大于 500" in (rejected[0].error_message or "")
    assert "s4" in (rejected[0].error_message or "")
    produced = [row for row in rows if row.status == "waiting_confirmation"]
    assert len(produced) == 1
    assert produced[0].arguments["steps"][0]["arguments"]["limit"] == 120

    assert result_status(events) == "waiting_confirmation"
    # 拒收原因经 role=tool 响应回喂（校验失败路径没有额外的 system 纠正行）
    rejections = [
        message for message in await read_messages(env, "tool")
        if "limit 不能大于 500" in (message.content or "")
    ]
    assert len(rejections) == 1


@pytest.mark.anyio
async def test_three_failed_attempts_close_coherently_without_plan(env):
    """(b) 收口轮 3/4/5 各消费一次失败尝试；第 3 次耗尽后走 _finish_without_plan。

    轮数写死 6（3 只读 + 3 次规划尝试）：没有补轮时这里只有 5 轮（index 4 是
    force_answer），删掉耗尽收口则会把第 4 次尝试推到 index 6 ⇒ 7 轮。
    """
    env.ai.scripted = [
        _read_round("c1"),
        _read_round("c2"),
        _read_round("c3"),
        _plan_response(ledger_plan(501), "bad-1"),
        _plan_response(ledger_plan(501), "bad-2"),
        _plan_response(ledger_plan(501), "bad-3"),
    ]
    events = await run_turn(env, plan_mode=True)

    assert len(env.ai.tool_round_calls) == 6, "三次尝试必须在带工具的轮里用完"
    assert env.ai.stream_calls == []
    for index in (3, 4, 5):
        assert round_tools(env.ai.tool_round_calls[index]) == [PROPOSE_PLAN_TOOL_NAME]
    rejections = [
        message for message in await read_messages(env, "tool")
        if "limit 不能大于 500" in (message.content or "")
    ]
    assert len(rejections) == 3, "三次失败原因都必须回喂给模型"

    content = final_content(events)
    assert "计划" in content
    assert "limit 不能大于 500" in content, "收口文案必须带出最后一次失败原因"
    assert env.ai.final_text not in content, "模型原文不得冒充最终回答"
    assert result_status(events) == "completed"

    rows = await read_tool_calls(env)
    assert [row.tool_name for row in rows] == [READ_TOOL] * 3 + [
        PROPOSE_PLAN_TOOL_NAME
    ] * 3
    assert all(row.status == "failed" for row in rows[3:])


@pytest.mark.anyio
async def test_non_plan_turn_still_force_answers_at_max_tool_rounds(env):
    """(e1) 非规划回合的工具轮仍恰好 MAX_TOOL_ROUNDS 轮，随后就是无工具强制回答。"""
    env.ai.scripted = [
        _read_round("c1"),
        _read_round("c2"),
        _read_round("c3"),
        _read_round("c4"),
    ]
    env.ai.final_text = "普通回答。"
    events = await run_turn(env, plan_mode=False)

    assert len(env.ai.tool_round_calls) == ProjectAgentService.MAX_TOOL_ROUNDS
    assert len(env.ai.stream_calls) == 1
    assert "tools" not in env.ai.stream_calls[0]
    assert all(
        PROPOSE_PLAN_TOOL_NAME not in round_tools(call)
        for call in env.ai.tool_round_calls
    )
    assert all(call["tool_choice"] == "auto" for call in env.ai.tool_round_calls)
    assert final_content(events) == "普通回答。"
    assert result_status(events) == "completed"


@pytest.mark.anyio
async def test_plan_turn_has_retry_rounds_before_the_forced_answer(env, monkeypatch):
    """(e2) 规划回合的轮上限 = MAX_TOOL_ROUNDS + PLAN_MAX_RETRIES，force_answer 在 index 6。

    把收口判定整个停掉，让模型按脚本把每一轮都当普通工具轮用：这样观察到的就是
    循环边界本身，而不是收口策略。修复前工具轮只有 4 轮（index 4 已是 force_answer）。
    """
    monkeypatch.setattr(ProjectAgentService, "_plan_closing_round", lambda self, **kwargs: False)
    env.ai.scripted = [_read_round(f"c{i}") for i in range(6)]
    env.ai.final_text = "收口轮兜底文案。"
    events = await run_turn(env, plan_mode=True)

    assert len(env.ai.tool_round_calls) == (
        ProjectAgentService.MAX_TOOL_ROUNDS + ProjectAgentService.PLAN_MAX_RETRIES
    ) == 6
    assert len(env.ai.stream_calls) == 1, "第 7 次调用（index 6）才是 force_answer 轮"
    assert "tools" not in env.ai.stream_calls[0]
    for call in env.ai.tool_round_calls:
        assert PROPOSE_PLAN_TOOL_NAME in round_tools(call), "非收口轮仍带完整工具集"
    content = final_content(events)
    assert "计划" in content
    assert env.ai.final_text not in content, "force_answer 轮原文不得冒充答案"
    assert result_status(events) == "completed"


@pytest.mark.anyio
async def test_closing_enforcement_still_blocks_read_tools_in_retry_rounds(env):
    """(f) 收口轮的只读调用一律不执行、计数并纠正；补出的重试轮行为不变。"""
    executed: list[str] = []
    real_execute = env.service.registry.execute

    async def spy_execute(name, arguments=None):
        executed.append(name)
        return await real_execute(name, arguments)

    env.service.registry.execute = spy_execute

    env.ai.scripted = [
        _read_round("c1"),
        _read_round("c2"),
        _read_round("c3"),
        _read_round("c-violation"),
        _plan_response(ledger_plan(120), "c-faithful"),
    ]
    events = await run_turn(env, plan_mode=True)

    assert executed.count(READ_TOOL) == 3, "收口轮的只读调用不得执行"
    assert len(env.ai.tool_round_calls) == 5
    assert round_tools(env.ai.tool_round_calls[3]) == [PROPOSE_PLAN_TOOL_NAME]
    assert round_tools(env.ai.tool_round_calls[4]) == [PROPOSE_PLAN_TOOL_NAME]

    blocked = [row for row in await read_tool_calls(env) if row.status == "failed"]
    assert len(blocked) == 1
    assert blocked[0].executed_at is None
    assert "收口轮不允许调用工具" in (blocked[0].error_message or "")
    assert PROPOSE_PLAN_TOOL_NAME in (blocked[0].error_message or "")
    assert len(await read_messages(env, "system")) == 1

    assert result_status(events) == "waiting_confirmation"
