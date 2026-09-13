"""PR-2a：propose_plan 规划工具 + approve-plan 一次性批准端点。

只读工具/中性夹具：禁止出现任何真实书名、人名、正文片段（AGENTS.md 脱敏硬约束）。
"""
from __future__ import annotations
import json
import os
import uuid
from types import SimpleNamespace

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.database import Base
from app.models.project import Project
from app.models.project_agent import AgentConversation, AgentMessage, AgentToolCall
from app.services.agent_plan_schema import (
    EXCLUDED_PLAN_TOOLS,
    PROPOSE_PLAN_TOOL_NAME,
    PlanValidationError,
    plannable_tool_names,
    validate_plan,
)
from app.services.project_agent_service import ProjectAgentService


ALLOWED = {"get_project_overview", "list_outlines", "start_project_task"}


def _plan(steps):
    return {"objective": "build three chapters", "steps": steps}


def test_valid_three_step_plan_normalises():
    raw = _plan([
        {"id": "s1", "tool": "start_project_task", "action": "generate_outlines",
         "arguments": {}, "note": None},
        {"id": "s2", "tool": "start_project_task", "action": "expand_outline",
         "arguments": {"outline_id": "o1"}},
        {"id": "s3", "tool": "start_project_task", "action": "generate_chapter",
         "arguments": {"chapter_number": 3}},
    ])
    plan = validate_plan(raw, allowed_tools=ALLOWED)
    assert plan["objective"] == "build three chapters"
    assert [s["id"] for s in plan["steps"]] == ["s1", "s2", "s3"]
    assert plan["steps"][0]["note"] == ""
    assert set(plan["steps"][0]) == {"id", "tool", "action", "arguments", "note"}


@pytest.mark.parametrize("tool", sorted(EXCLUDED_PLAN_TOOLS))
def test_import_tools_are_excluded_from_plan_schema(tool):
    with pytest.raises(PlanValidationError, match="不允许出现在计划中"):
        validate_plan(_plan([{"id": "s1", "tool": tool, "arguments": {}}]),
                      allowed_tools=ALLOWED | {tool})


def test_unknown_tool_rejected_with_readable_message():
    with pytest.raises(PlanValidationError, match="未启用的工具"):
        validate_plan(_plan([{"id": "s1", "tool": "drop_database"}]), allowed_tools=ALLOWED)


def test_duplicate_step_ids_rejected():
    with pytest.raises(PlanValidationError, match="重复"):
        validate_plan(_plan([{"id": "s1", "tool": "list_outlines"},
                             {"id": "s1", "tool": "list_outlines"}]), allowed_tools=ALLOWED)


def test_start_project_task_requires_known_action():
    with pytest.raises(PlanValidationError, match="action"):
        validate_plan(_plan([{"id": "s1", "tool": "start_project_task", "arguments": {}}]),
                      allowed_tools=ALLOWED)
    with pytest.raises(PlanValidationError, match="未知 action"):
        validate_plan(_plan([{"id": "s1", "tool": "start_project_task",
                              "action": "drop_chapters"}]), allowed_tools=ALLOWED)


def test_empty_or_oversized_plan_rejected():
    with pytest.raises(PlanValidationError):
        validate_plan(_plan([]), allowed_tools=ALLOWED)
    with pytest.raises(PlanValidationError, match="上限"):
        validate_plan(_plan([{"id": f"s{i}", "tool": "list_outlines"} for i in range(13)]),
                      allowed_tools=ALLOWED)


def test_plannable_tool_names_drops_propose_plan_and_import_tools():
    definitions = [
        {"type": "function", "function": {"name": "list_outlines"}},
        {"type": "function", "function": {"name": PROPOSE_PLAN_TOOL_NAME}},
        {"type": "function", "function": {"name": "import_outlines_json"}},
    ]
    assert plannable_tool_names(definitions) == {"list_outlines"}


@pytest.mark.anyio
async def test_registry_rejects_preview_and_execute_for_propose_plan():
    """定案的安全网：propose_plan 既不进 preview() 也不进 execute()。"""
    from app.services.project_agent_tools import ProjectAgentToolRegistry

    registry = ProjectAgentToolRegistry(SimpleNamespace(id="p1"), None)  # 构造不查库
    tool = registry.get(PROPOSE_PLAN_TOOL_NAME)
    assert tool.requires_confirmation is False
    assert tool.risk_level == 0
    assert PROPOSE_PLAN_TOOL_NAME in {
        item["function"]["name"] for item in registry.definitions()
    }
    with pytest.raises(ValueError, match="终止型规划工具"):
        await registry.preview(PROPOSE_PLAN_TOOL_NAME, {})
    with pytest.raises(ValueError, match="终止型规划工具"):
        await registry.execute(PROPOSE_PLAN_TOOL_NAME, {})


# --------------------------------------------------------------------------- #
# Task 2：规划收口轮的工具收窄与 tool_choice 能力门
# --------------------------------------------------------------------------- #


def test_provider_capability_gate_is_fail_closed():
    from app.services import agent_plan_schema as schema
    from app.services.agent_plan_schema import provider_supports_required_tool_choice

    assert provider_supports_required_tool_choice("gemini") is False
    assert provider_supports_required_tool_choice(None) is False
    assert provider_supports_required_tool_choice("totally-unknown-provider") is False
    # 白名单里至少有一个 provider 判 True，否则这道门是死的
    assert schema.REQUIRED_TOOL_CHOICE_PROVIDERS
    assert any(
        provider_supports_required_tool_choice(name)
        for name in sorted(schema.REQUIRED_TOOL_CHOICE_PROVIDERS)
    )


def test_required_tool_choice_whitelist_matches_clients_that_send_it():
    """白名单必须逐个对钉到**真的把 tool_choice 写进 payload** 的客户端。

    凭印象填的白名单是最危险的失效模式：Gemini 客户端收下 tool_choice 形参却从不
    进 payload（gemini_client 只把它写进函数签名），置 required 会静默空转，
    模型照样可以不调工具 ⇒ 规划回合失去 ② 这道手段而测试全绿。
    """
    from app.services import agent_plan_schema as schema
    from app.services.ai_service import normalize_provider

    assert schema.REQUIRED_TOOL_CHOICE_PROVIDERS == frozenset({"openai", "anthropic"})
    # 别名必须归一化到白名单里的字符串，否则真实部署（commandcode）会判 False。
    assert normalize_provider("commandcode") == "openai"
    assert normalize_provider("CommandCode") == "openai"


def _bare_service():
    from app.services.project_agent_service import ProjectAgentService

    return object.__new__(ProjectAgentService)  # helper 不使用实例状态，避开 __init__ 的 DB/AI 依赖


def test_tools_for_round_hides_propose_plan_when_not_planning():
    from app.services.project_agent_service import ProjectAgentService

    svc = _bare_service()
    base = [
        {"type": "function", "function": {"name": "list_outlines"}},
        {"type": "function", "function": {"name": PROPOSE_PLAN_TOOL_NAME}},
    ]
    assert ProjectAgentService._tools_for_round(
        svc, base, plan_mode=False, closing=False
    ) == [{"type": "function", "function": {"name": "list_outlines"}}]


def test_tools_for_round_narrows_to_propose_plan_when_closing():
    from app.services.project_agent_service import ProjectAgentService

    svc = _bare_service()
    base = [
        {"type": "function", "function": {"name": "list_outlines"}},
        {"type": "function", "function": {"name": PROPOSE_PLAN_TOOL_NAME}},
    ]
    got = ProjectAgentService._tools_for_round(svc, base, plan_mode=True, closing=True)
    assert got == [{"type": "function", "function": {"name": PROPOSE_PLAN_TOOL_NAME}}]
    both = ProjectAgentService._tools_for_round(svc, base, plan_mode=True, closing=False)
    assert [t["function"]["name"] for t in both] == ["list_outlines", PROPOSE_PLAN_TOOL_NAME]


@pytest.mark.anyio
async def test_call_round_forwards_tool_choice_and_keeps_agent_side_routing():
    from app.services.project_agent_service import ProjectAgentService

    recorded: dict = {}

    async def fake_generate_text(**kwargs):
        recorded.update(kwargs)
        return {"content": "", "tool_calls": [], "usage": {}}

    svc = _bare_service()
    svc.ai_service = SimpleNamespace(generate_text=fake_generate_text)
    await ProjectAgentService._call_round(
        svc,
        prompt="p",
        system_prompt="s",
        force_answer=False,
        available_tools=[{"type": "function", "function": {"name": "propose_plan"}}],
        tool_choice="required",
    )
    assert recorded["tool_choice"] == "required"
    assert recorded["auto_mcp"] is False          # shared-terms 硬约束
    assert recorded["handle_tool_calls"] is False  # 工具只能由 agent 自己的 registry 路由


# --------------------------------------------------------------------------- #
# Task 3：propose_plan 前置特判分支
# --------------------------------------------------------------------------- #

PROJECT_ID = "proj-plan-a"
USER_ID = "u-plan-a"


@pytest.fixture
async def db_engine():
    """临时文件 SQLite（与 tests/test_project_agent_inline_task.py 同习惯）。

    engine 单独成一个 fixture：核对「回合真的提交了」必须**另开一条连接**读库，
    沿用 service 的 session 会走 identity map 看到未提交的行。
    """
    db_path = f"/tmp/test_plan_pr2a_{uuid.uuid4().hex}.db"
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


@pytest.fixture
async def env(db_engine, db_session):
    """种子数据 + 可直接驱动 stream_chat 的 service（api_provider 必须是真字符串）。"""
    db_session.add(Project(id=PROJECT_ID, user_id=USER_ID, title="neutral project"))
    conversation = AgentConversation(
        user_id=USER_ID, project_id=PROJECT_ID, title="planning turn"
    )
    db_session.add(conversation)
    await db_session.commit()

    service = ProjectAgentService(
        db=db_session,
        ai_service=SimpleNamespace(default_model="mock-model", api_provider="openai"),
        project=Project(id=PROJECT_ID, user_id=USER_ID, title="neutral project"),
        user_id=USER_ID,
    )
    yield SimpleNamespace(
        service=service,
        project_id=PROJECT_ID,
        user_id=USER_ID,
        conversation_id=conversation.id,
        engine=db_engine,
    )


async def read_tool_calls(env) -> list[AgentToolCall]:
    """另开一条连接读库：只 flush 未提交的行在这里读不到。"""
    ReaderSession = async_sessionmaker(bind=env.engine, expire_on_commit=False)
    async with ReaderSession() as session:
        return list((await session.execute(
            select(AgentToolCall).where(AgentToolCall.project_id == env.project_id)
        )).scalars().all())


async def run_turn(env, responses, *, plan_mode: bool, calls: list) -> list[dict]:
    """两个出口都要 patch：generate_text（工具决策轮）与 generate_text_stream_full（末轮）。"""

    async def fake_generate_text(**kwargs):
        calls.append(kwargs)
        return responses[min(len(calls) - 1, len(responses) - 1)]

    async def fake_stream_full(**kwargs):
        calls.append(kwargs)
        return responses[min(len(calls) - 1, len(responses) - 1)]

    env.service.ai_service.generate_text = fake_generate_text
    env.service.ai_service.generate_text_stream_full = fake_stream_full
    return [
        e async for e in env.service.stream_chat(
            conversation_id=env.conversation_id,
            message="plan my book",
            page_context={"route": "/project/1"},
            auto_approve=False,
            plan_mode=plan_mode,
        )
    ]


def tool_call(name: str, arguments: dict, call_id: str = "call-pr2a-1") -> dict:
    return {
        "content": "我先调用工具。",
        "tool_calls": [{"id": call_id, "function": {"name": name, "arguments": arguments}}],
        "usage": {"prompt_tokens": 10, "completion_tokens": 5},
    }


def answer(content: str) -> dict:
    return {"content": content, "tool_calls": [], "usage": {}}


VALID_PLAN = {
    "objective": "build three chapters",
    "steps": [
        {"id": "s1", "tool": "list_outlines", "arguments": {}},
        {"id": "s2", "tool": "list_outlines", "arguments": {"limit": 3}},
        {"id": "s3", "tool": "list_outlines", "arguments": {}},
    ],
}


def _plan_response(**extra):
    return tool_call(
        PROPOSE_PLAN_TOOL_NAME, json.loads(json.dumps(VALID_PLAN)), **extra
    )


@pytest.mark.anyio
async def test_propose_plan_creates_waiting_confirmation_without_executing(env):
    calls: list[dict] = []

    async def boom_execute(*a, **k):  # registry.execute 被调用即失败
        raise AssertionError("propose_plan must never reach registry.execute")

    async def boom_preview(*a, **k):
        raise AssertionError("propose_plan must never reach registry.preview")

    env.service.registry.execute = boom_execute
    env.service.registry.preview = boom_preview

    events = await run_turn(env, [_plan_response()], plan_mode=True, calls=calls)

    assert calls[0]["tool_choice"] in ("auto", "required")
    final = events[-1]
    assert final["type"] == "result" and final["data"]["status"] == "waiting_confirmation"

    rows = await read_tool_calls(env)
    assert len(rows) == 1
    assert rows[0].tool_name == PROPOSE_PLAN_TOOL_NAME
    assert rows[0].status == "waiting_confirmation"
    assert rows[0].requires_confirmation is False   # 特判接管，不是靠 risk_level
    assert [s["id"] for s in rows[0].arguments["steps"]] == ["s1", "s2", "s3"]
    assert rows[0].result is None and rows[0].executed_at is None
    # 复用既有 `if proposed:` 收口块：它负责把 message_id 挂到 assistant 并提交。
    assert rows[0].message_id == final["data"]["message_id"]
    # 计划必须原样进 arguments（PR-2b 的执行器只读这一列）
    assert rows[0].arguments["objective"] == "build three chapters"


@pytest.mark.anyio
async def test_propose_plan_step_card_is_the_confirmation_surface(env):
    """确认卡的可见面：step 状态 + detail.plan（PR-3 的 PlanApprovalCard 只读这里）。"""
    events = await run_turn(env, [_plan_response()], plan_mode=True, calls=[])

    updates = [e for e in events if e["type"] == "step_update"]
    plan_steps = [
        e["data"] for e in updates
        if e["data"]["step_type"] == "tool" and e["data"]["status"] == "waiting_confirmation"
    ]
    assert len(plan_steps) == 1
    detail = plan_steps[0]["detail"]
    assert [s["id"] for s in detail["plan"]["steps"]] == ["s1", "s2", "s3"]
    assert detail["tool_call"]["status"] == "waiting_confirmation"
    assert detail["tool_call"]["tool_name"] == PROPOSE_PLAN_TOOL_NAME


@pytest.mark.anyio
async def test_non_planning_round_never_offers_propose_plan(env):
    """第二道保险落在**回合级**：默认 plan_mode=False 时模型不得看到 propose_plan。

    只有 helper 的单元测试挡不住"循环里忘了调用 helper"——那时 propose_plan
    会出现在每一轮工具集里，模型可以在任意一轮结束规划回合。
    """
    calls: list[dict] = []
    await run_turn(
        env,
        [answer("普通回合。")],
        plan_mode=False,
        calls=calls,
    )

    assert calls, "没有捕获到任何模型调用"
    for call in calls:
        names = {item["function"]["name"] for item in (call["tools"] or [])}
        assert names, "非规划回合也必须带项目工具"
        assert PROPOSE_PLAN_TOOL_NAME not in names


@pytest.mark.anyio
async def test_closing_round_offers_only_propose_plan_and_requires_it(env):
    """手段 ①+② 的回合级落地：预算耗尽那一轮只给 propose_plan 且 tool_choice=required。"""
    calls: list[dict] = []
    await run_turn(
        env,
        [
            tool_call("list_outlines", {}, call_id="c1"),
            tool_call("list_outlines", {}, call_id="c2"),
            answer("收口轮直接回答了（缺陷形态）。"),
        ],
        plan_mode=True,
        calls=calls,
    )

    # Task 4 之后收口轮没产出计划会继续重问，所以总轮数不再恒为 3；
    # 本用例钉的是**前三轮的工具集与 tool_choice 形状**，不是总轮数。
    assert len(calls) >= 3
    ordered = [
        [item["function"]["name"] for item in (call["tools"] or [])] for call in calls[:2]
    ]
    assert all(names and names[-1] == PROPOSE_PLAN_TOOL_NAME for names in ordered)
    assert all(len(names) > 1 for names in ordered)   # 普通规划轮仍然保留只读工具
    assert [call["tool_choice"] for call in calls[:2]] == ["auto", "auto"]
    assert [item["function"]["name"] for item in calls[2]["tools"]] == [
        PROPOSE_PLAN_TOOL_NAME
    ]
    assert calls[2]["tool_choice"] == "required"


@pytest.mark.anyio
async def test_closing_round_plan_validated_against_turn_whitelist_not_round_tools(env):
    """收口轮交出的合法计划必须被接受。

    收口轮只给模型 propose_plan ⇒ 若白名单取"该轮工具集"，它恒为空，**每一份**计划
    都会被判「未启用的工具」。白名单必须取本回合注册表+MCP 的完整工具集。
    """
    events = await run_turn(
        env,
        [
            tool_call("list_outlines", {}, call_id="c1"),
            tool_call("list_outlines", {}, call_id="c2"),
            _plan_response(call_id="call-plan-1"),
        ],
        plan_mode=True,
        calls=[],
    )

    final = [e for e in events if e["type"] == "result"][-1]
    assert final["data"]["status"] == "waiting_confirmation"
    # 前两轮只读调用也各有一行 ⇒ 只挑 propose_plan 那一行
    plan_rows = [row for row in await read_tool_calls(env) if row.tool_name == PROPOSE_PLAN_TOOL_NAME]
    assert len(plan_rows) == 1
    assert plan_rows[0].status == "waiting_confirmation"
    assert [s["id"] for s in plan_rows[0].arguments["steps"]] == ["s1", "s2", "s3"]


@pytest.mark.anyio
async def test_propose_plan_rejects_a_tool_the_round_never_offered(env):
    """白名单来自**本回合的工具集**：计划里引用没注册的工具必须被拒。"""
    bad_plan = {
        "objective": "sneak a write in",
        "steps": [{"id": "s1", "tool": "drop_database", "arguments": {"title": "x"}}],
    }
    # 第二份响应是普通回答：Task 4 之前的有界重试还不存在，
    # 若让假模型反复交同一份坏计划，回合会撞到轮数上限而抛错（那是 Task 4 的收口职责）。
    events = await run_turn(
        env,
        [
            tool_call(PROPOSE_PLAN_TOOL_NAME, bad_plan, call_id="call-bad"),
            answer("那我先只说明思路。"),
        ],
        plan_mode=True,
        calls=[],
    )

    rows = await read_tool_calls(env)
    assert len(rows) == 1
    assert rows[0].status == "failed"
    assert "未启用的工具" in (rows[0].error_message or "")
    assert [e for e in events if e["type"] == "result"][-1]["data"]["status"] == "completed"
    failed = [
        e["data"] for e in events
        if e["type"] == "step_update" and e["data"]["status"] == "failed"
    ]
    assert len(failed) == 1
    assert "计划格式需要修正" in failed[0]["content"]


# --------------------------------------------------------------------------- #
# Task 4：产出校验的有界重试（≤2 次）与可读收口
# --------------------------------------------------------------------------- #

BAD_PLAN = {"objective": "x", "steps": []}
BAD_PLAN_REASON = "steps 必须是非空数组"


def _bad_plan_response(call_id: str = "call-bad") -> dict:
    return tool_call(PROPOSE_PLAN_TOOL_NAME, BAD_PLAN, call_id=call_id)


def _plan_response_without_text() -> dict:
    response = _plan_response()
    response["content"] = ""      # 模型只调工具、不说话 ⇒ 走服务端兜底文案
    return response


async def read_messages(env, role: str) -> list:
    ReaderSession = async_sessionmaker(bind=env.engine, expire_on_commit=False)
    async with ReaderSession() as session:
        return list((await session.execute(
            select(AgentMessage).where(
                AgentMessage.conversation_id == env.conversation_id,
                AgentMessage.role == role,
            )
        )).scalars().all())


@pytest.mark.anyio
async def test_closing_round_without_tool_call_retries_exactly_twice(env):
    """形态 (a)：收口轮没有 tool_calls ⇒ 计数重问，最多 2 次后以可读文案收口。

    总轮数 5 = 2 轮只读 + 2 次重问 + 第 3 次失败即收口；把 PLAN_MAX_RETRIES 调大
    会撞到 MAX_TOOL_ROUNDS 而抛裸 RuntimeError（本用例变红），调小则轮数与纠正
    消息数同时变红——两个方向都钉住「≤2」。
    """
    calls: list[dict] = []
    events = await run_turn(
        env,
        [
            tool_call("list_outlines", {}, call_id="c1"),
            tool_call("list_outlines", {}, call_id="c2"),
            answer("我再想想。"),
            answer("还是先讲道理。"),
            answer("最后仍然不讲道理。"),
        ],
        plan_mode=True,
        calls=calls,
    )

    assert len(calls) == 5, f"实际发生 {len(calls)} 次模型调用，重试上界失控"
    # 定案的数字，不是从被测常量推出来的：改 PLAN_MAX_RETRIES 就必须同时改这里。
    assert ProjectAgentService.PLAN_MAX_RETRIES == 2
    assert len(await read_messages(env, "system")) == 2
    finals = [e for e in events if e["type"] == "final_chunk"]
    assert finals, "必须以可读文案收口"
    assert "计划" in finals[-1]["content"]
    final = events[-1]
    assert final["type"] == "result" and final["data"]["status"] == "completed"
    # 计划一步都没产出 ⇒ 不得留下 waiting_confirmation 的假卡片
    rows = await read_tool_calls(env)
    assert [row.tool_name for row in rows] == ["list_outlines", "list_outlines"]
    assert all(row.status == "executed" for row in rows)


@pytest.mark.anyio
async def test_invalid_plan_budget_is_consumed_and_reason_reaches_user(env):
    """形态 (b)：Task 3 写下的 plan_attempts / plan_correction **必须被消费**。

    未消费 ⇒ 这里既不会有第 3 次尝试（无界），收口文案也不会带出 schema 的原始
    失败原因，卡片更不会被置 failed。三条断言各自钉住一个消费点。
    """
    calls: list[dict] = []
    events = await run_turn(env, [_bad_plan_response()], plan_mode=True, calls=calls)

    rows = await read_tool_calls(env)
    assert len(rows) == 3           # 1 次初始 + 2 次重问；写死，不随常量漂移
    assert all(row.tool_name == PROPOSE_PLAN_TOOL_NAME for row in rows)
    assert all(row.status == "failed" for row in rows)
    assert all(BAD_PLAN_REASON in (row.error_message or "") for row in rows)

    finals = [e for e in events if e["type"] == "final_chunk"]
    assert finals and BAD_PLAN_REASON in finals[-1]["content"], "plan_correction 无人消费"
    assert events[-1]["type"] == "result" and events[-1]["data"]["status"] == "completed"
    assert len(calls) == 3          # 第 4 次请求就是成本失控


@pytest.mark.anyio
async def test_closing_round_wrong_tool_counts_against_retry_budget(env):
    """形态 (c)：收口轮调了不是 propose_plan 的工具，同样计数并最终以可读文案收口。"""
    events = await run_turn(
        env,
        [
            _bad_plan_response(),
            tool_call("list_outlines", {}, call_id="c2"),
            tool_call("list_outlines", {}, call_id="c3"),
        ],
        plan_mode=True,
        calls=[],
    )

    rows = await read_tool_calls(env)
    assert [row.tool_name for row in rows] == [
        PROPOSE_PLAN_TOOL_NAME, "list_outlines", "list_outlines",
    ]
    finals = [e for e in events if e["type"] == "final_chunk"]
    assert finals and "计划" in finals[-1]["content"]
    assert events[-1]["type"] == "result" and events[-1]["data"]["status"] == "completed"


@pytest.mark.anyio
async def test_plan_close_text_never_promises_a_diff_row(env):
    """计划卡的 `preview is None` ⇒ 兜底文案不得说「请核对下方差异」（空标签错报）。"""
    events = await run_turn(env, [_plan_response_without_text()], plan_mode=True, calls=[])

    finals = [e for e in events if e["type"] == "final_chunk"]
    assert finals
    assert "计划" in finals[0]["content"]
    assert "请核对下方差异" not in finals[0]["content"]
