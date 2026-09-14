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

from app.api import project_agent as agent_api
from app.core.errors import ApiError
from app.database import Base
import app.services.agent_plan_dispatch as dispatch
from app.models.background_task import BackgroundTask
from app.models.project import Project
from app.models.project_agent import AgentConversation, AgentMessage, AgentToolCall
from app.services.agent_plan_schema import (
    EXCLUDED_PLAN_TOOLS,
    PROPOSE_PLAN_TOOL_NAME,
    PlanValidationError,
    plannable_tool_names,
    validate_plan,
)
from app.services.ai_service import is_thinking_model
from app.services.project_agent_service import (
    PLAN_MODE_INSTRUCTION,
    ProjectAgentService,
)
from support.agent_stubs import AgentAIServiceStub


@pytest.fixture(autouse=True)
def stub_history_budget(monkeypatch):
    """PR-0c 合并后：本文件锁的是规划回合，不是预算换算（与 main 侧同习惯）。

    换算要走 B 的探测结论（DB 缓存行 + 网关元数据）⇒ 与本文件要证的事无关，
    统一钉成 PR-0c 之前的硬编码 60000，规划断言一字不改。
    """
    import app.services.agent_prompt_budget as apb

    async def fake_resolve(**kwargs):
        return 60_000

    monkeypatch.setattr(apb, "resolve_history_budget_chars", fake_resolve)


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
    svc.ai_service = AgentAIServiceStub(generate_text=fake_generate_text)
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


class _FakeAgentAIService(AgentAIServiceStub):
    """假 AI 出口：忠实复刻服务层用到的公开出口（provider / 思考型判断）。

    预算窗口面继承自 `AgentAIServiceStub`（本文件另用 autouse fixture 钉住 60000，
    见 `stub_history_budget`，那层隔离保留）。思考型判断必须真的读
    `default_model` / `base_url`：本文件 issue #77 的用例
    会逐次改这两个字段，恒 False 的桩会让那些断言真空通过。
    """

    def __init__(self, *, default_model: str = "mock-model", base_url: str = "") -> None:
        self.default_model = default_model
        self.base_url = base_url

    def resolve_dispatch_provider(self, provider=None) -> str:
        return "openai"

    def is_thinking_model_active(self) -> bool:
        return is_thinking_model(self.default_model, self.base_url)


@pytest.fixture
async def env(db_engine, db_session):
    """种子数据 + 可直接驱动 stream_chat 的 service（provider 出口必须是真方法）。"""
    db_session.add(Project(id=PROJECT_ID, user_id=USER_ID, title="neutral project"))
    conversation = AgentConversation(
        user_id=USER_ID, project_id=PROJECT_ID, title="planning turn"
    )
    db_session.add(conversation)
    await db_session.commit()

    service = ProjectAgentService(
        db=db_session,
        ai_service=_FakeAgentAIService(),
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


async def run_turn(
    env,
    responses,
    *,
    plan_mode: bool,
    calls: list,
    auto_approve: bool = False,
    forbidden_final: bool = False,
) -> list[dict]:
    """两个出口都要 patch：generate_text（工具决策轮）与 generate_text_stream_full（末轮）。

    `forbidden_final=True` 用于 auto_approve 回合：计划被直接放行后必须在
    propose_plan 分支里 return，走到最终回答轮本身就是缺陷。
    """

    async def fake_generate_text(**kwargs):
        calls.append(kwargs)
        return responses[min(len(calls) - 1, len(responses) - 1)]

    async def fake_stream_full(**kwargs):
        calls.append(kwargs)
        if forbidden_final:
            raise AssertionError("auto-approved plan must not reach the answer round")
        return responses[min(len(calls) - 1, len(responses) - 1)]

    env.service.ai_service.generate_text = fake_generate_text
    env.service.ai_service.generate_text_stream_full = fake_stream_full
    return [
        e async for e in env.service.stream_chat(
            conversation_id=env.conversation_id,
            message="plan my book",
            page_context={"route": "/project/1"},
            auto_approve=auto_approve,
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


def test_plan_mode_instruction_is_added_only_when_planning():
    """规划回合必须在 prompt 里明说"先 propose_plan"；非规划回合逐字节回 PR-1。

    只把 propose_plan 挂进工具集不构成指令：具体多步请求（多章分析后总结）下模型
    照样直接调 start_project_task 并停在逐工具确认，计划卡永不出现（5/5 实测）。
    """
    svc = _bare_service()
    svc.project = SimpleNamespace(id="p-plan", title="neutral project")
    history = [
        AgentMessage(conversation_id="c1", role="user", content="plan a multi-step request")
    ]
    page_context = {"route": "/project/p-plan"}

    default_prompt = svc._build_prompt(history, page_context, budget_chars=60_000)
    off_prompt = svc._build_prompt(history, page_context, plan_mode=False, budget_chars=60_000)
    on_prompt = svc._build_prompt(history, page_context, plan_mode=True, budget_chars=60_000)

    assert off_prompt == default_prompt, "plan_mode=False 必须与 PR-1 逐字节一致"
    assert PLAN_MODE_INSTRUCTION not in off_prompt
    assert PLAN_MODE_INSTRUCTION in on_prompt
    assert on_prompt.startswith(off_prompt), "规划指令只能追加，不得改写既有段落"


@pytest.mark.anyio
async def test_planning_turn_threads_plan_mode_into_prompt(env):
    """调用点必须把 plan_mode 穿到 _build_prompt：只测 _build_prompt 挡不住忘传。"""
    plan_calls: list[dict] = []
    await run_turn(env, [answer("收到。")], plan_mode=True, calls=plan_calls)
    assert plan_calls and PLAN_MODE_INSTRUCTION in plan_calls[0]["prompt"]

    off_calls: list[dict] = []
    await run_turn(env, [answer("收到。")], plan_mode=False, calls=off_calls)
    assert off_calls and PLAN_MODE_INSTRUCTION not in off_calls[0]["prompt"]


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


# --------------------------------------------------------------------------- #
# 思考型模型不得收到强制 tool_choice（issue #77：真实运行被网关拒
# "Thinking mode does not support this tool_choice" ⇒ 计划永远产不出来）
# --------------------------------------------------------------------------- #


async def _closing_turn_calls(
    env, *, model: str, base_url: str, plan_mode: bool = True
) -> list[dict]:
    """驱动到第 3 次模型调用（规划模式的收口轮）并返回全部捕获的 kwargs。

    收口轮不产出计划 ⇒ 走 (a) 形态有界重试；与
    test_closing_round_offers_only_propose_plan_and_requires_it 用同一组响应，
    因此第 3 次调用形状可直接比较。
    """
    env.service.ai_service.default_model = model
    env.service.ai_service.base_url = base_url
    calls: list[dict] = []
    await run_turn(
        env,
        [
            tool_call("list_outlines", {}, call_id="c1"),
            tool_call("list_outlines", {}, call_id="c2"),
            answer("收口轮直接回答了（缺陷形态）。"),
        ],
        plan_mode=plan_mode,
        calls=calls,
    )
    return calls


@pytest.mark.anyio
async def test_thinking_model_closing_round_uses_auto_tool_choice(env):
    """思考型模型（deepseek 名 / commandcode 网关）拒收 required ⇒ 收口轮必须回退 auto。

    收口轮工具集只留 propose_plan，auto 不削弱"模型仍能提交计划"的能力，
    只解除网关对强制 tool_choice 的硬拒绝。
    """
    calls = await _closing_turn_calls(
        env, model="deepseek-v4-flash", base_url="https://api.commandcode.ai/v1"
    )

    closing = calls[2]
    assert [item["function"]["name"] for item in closing["tools"]] == [
        PROPOSE_PLAN_TOOL_NAME
    ]
    assert closing["tool_choice"] == "auto"


@pytest.mark.anyio
async def test_thinking_model_closing_round_still_produces_plan(env):
    """配对反向：回退 auto 之后，收口轮提交的计划仍必须被接受（不是死胡同）。"""
    env.service.ai_service.default_model = "deepseek-v4-flash"
    env.service.ai_service.base_url = "https://api.commandcode.ai/v1"
    events = await run_turn(
        env,
        [
            tool_call("list_outlines", {}, call_id="c1"),
            tool_call("list_outlines", {}, call_id="c2"),
            _plan_response(call_id="call-thinking-plan"),
        ],
        plan_mode=True,
        calls=[],
    )

    final = [e for e in events if e["type"] == "result"][-1]
    assert final["data"]["status"] == "waiting_confirmation"
    plan_rows = [
        row for row in await read_tool_calls(env)
        if row.tool_name == PROPOSE_PLAN_TOOL_NAME
    ]
    assert len(plan_rows) == 1 and plan_rows[0].status == "waiting_confirmation"


@pytest.mark.anyio
async def test_non_thinking_model_closing_round_still_requires_tool_choice(env):
    """非思考型 openai 模型维持 required：PR-2a 的强制产出手段不得被本修复回退。"""
    calls = await _closing_turn_calls(
        env, model="gpt-4o", base_url="https://api.openai.com/v1"
    )
    assert calls[2]["tool_choice"] == "required"


@pytest.mark.anyio
async def test_plan_mode_off_keeps_tool_choice_auto(env):
    """plan_mode=False 与 PR-1 逐字一致：看不到计划工具，且所有轮 tool_choice=auto。"""
    calls = await _closing_turn_calls(
        env,
        model="deepseek-v4-flash",
        base_url="https://api.commandcode.ai/v1",
        plan_mode=False,
    )
    assert calls, "没有捕获到任何模型调用"
    for call in calls:
        names = {item["function"]["name"] for item in (call["tools"] or [])}
        assert PROPOSE_PLAN_TOOL_NAME not in names
        assert call["tool_choice"] == "auto"


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


@pytest.mark.anyio
async def test_propose_plan_outside_planning_round_never_opens_plan_card(env):
    """N2：特判必须 gate 在 `plan_mode` 上，不然幻觉调用会凭空产出一张计划卡。

    `propose_plan` 只在规划回合被提供给模型；模型在非规划回合凭幻觉调用它时，若特判
    不看 `plan_mode`，就会落一张 `waiting_confirmation` 的"等待批准的计划"卡——一步都
    不会执行，但用户看见一次错报（而且这张卡真能被批准）。正确行为是走**既有的**
    失败收口：registry 的终止型工具安全网抛 ValueError ⇒ record/step 都 failed。

    反向配对：同样这份计划、同一个回合形状，`plan_mode=True` 时仍必须产出
    waiting_confirmation 卡（否则这条 gate 会被"干脆整支删掉"糊过去）。
    """
    events = await run_turn(
        env,
        [
            tool_call(
                PROPOSE_PLAN_TOOL_NAME,
                json.loads(json.dumps(VALID_PLAN)),
                call_id="call-hallucinated",
            ),
            answer("普通回合，我直接回答。"),
        ],
        plan_mode=False,
        calls=[],
    )

    rows = await read_tool_calls(env)
    plan_rows = [row for row in rows if row.tool_name == PROPOSE_PLAN_TOOL_NAME]
    assert len(plan_rows) == 1
    assert plan_rows[0].status == "failed", (
        "非规划回合的 propose_plan 必须显式失败，不得停在等待批准"
    )
    assert "waiting_confirmation" not in {row.status for row in rows}
    assert plan_rows[0].result is None and plan_rows[0].executed_at is None
    assert await read_plan_tasks(env) == [], "幻觉调用绝不得建 agent_plan 任务行"
    assert [e for e in events if e["type"] == "result"][-1]["data"]["status"] == "completed"
    tool_steps = [
        e["data"] for e in events
        if e["type"] == "step_update" and e["data"]["step_type"] == "tool"
    ]
    assert tool_steps and all(step["status"] == "failed" for step in tool_steps)
    assert not [
        step for step in tool_steps
        if (step.get("detail") or {}).get("plan")
    ], "非规划回合不得下发计划卡 payload"

    plan_events = await run_turn(
        env, [_plan_response(call_id="call-real-plan")], plan_mode=True, calls=[]
    )
    real = [
        row for row in await read_tool_calls(env)
        if row.tool_name == PROPOSE_PLAN_TOOL_NAME and row.id != plan_rows[0].id
    ]
    assert len(real) == 1
    assert real[0].status == "waiting_confirmation"
    assert [e for e in plan_events if e["type"] == "result"][-1]["data"]["status"] == (
        "waiting_confirmation"
    )


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


# --------------------------------------------------------------------------- #
# Task 5：auto_approve 的同事务直路由
# --------------------------------------------------------------------------- #

@pytest.fixture(autouse=True)
def _no_plan_runner(monkeypatch):
    """每个用例都从「执行器未注册」出发，并自动复位（setattr 要求符号存在）。"""
    monkeypatch.setattr(dispatch, "_PLAN_RUNNER", None)


async def read_plan_tasks(env) -> list[BackgroundTask]:
    ReaderSession = async_sessionmaker(bind=env.engine, expire_on_commit=False)
    async with ReaderSession() as session:
        return list((await session.execute(
            select(BackgroundTask).where(BackgroundTask.project_id == env.project_id)
        )).scalars().all())


async def _noop_runner(**kwargs):
    """占位执行器：PR-2a 只验证接缝的入参与时序，不验证执行。"""
    return object()


async def _forbid_registry(env) -> None:
    async def boom(*a, **k):
        raise AssertionError("propose_plan must never reach registry.execute/preview")

    env.service.registry.execute = boom
    env.service.registry.preview = boom


@pytest.mark.anyio
async def test_auto_approve_routes_plan_to_task_without_registry(env):
    """同一事务内置 executing + 建 agent_plan 任务行；卡片根本不出现。"""
    calls: list[dict] = []
    dispatch.register_plan_runner(_noop_runner)
    await _forbid_registry(env)
    events = await run_turn(
        env, [_plan_response()], plan_mode=True, calls=calls,
        auto_approve=True, forbidden_final=True,
    )

    assert calls, "没有捕获到任何模型调用"
    assert events[-1]["type"] == "result" and events[-1]["data"]["status"] == "completed"
    rows = await read_tool_calls(env)
    assert len(rows) == 1
    record = rows[0]
    assert record.tool_name == PROPOSE_PLAN_TOOL_NAME
    assert record.status == "executing"
    assert record.confirmed_at is not None
    assert record.executed_at is None, "auto_approve 只放行计划，不得执行任何步骤"
    assert record.message_id == events[-1]["data"]["message_id"]

    tasks = await read_plan_tasks(env)
    assert len(tasks) == 1
    task = tasks[0]
    assert task.task_type == "agent_plan"
    assert task.status == "pending", "pending 只能由执行器改成 running"
    assert task.task_input["tool_call_id"] == record.id
    assert task.task_input["conversation_id"] == env.conversation_id
    assert task.task_input["objective"] == "build three chapters"
    assert [s["id"] for s in task.task_input["steps"]] == ["s1", "s2", "s3"]
    # PR-3 刷新后靠 result 反查计划行（entity_id 单独不唯一，必须配 task_type）
    assert record.result == {"entity_id": task.id, "task_type": "agent_plan"}
    assert events[-1]["data"]["plan_task_id"] == task.id

    tool_steps = [
        e["data"] for e in events
        if e["type"] == "step_update" and e["data"]["step_type"] == "tool"
    ]
    assert len(tool_steps) == 1
    assert tool_steps[0]["status"] == "completed"
    assert tool_steps[0]["detail"]["approval_mode"] == "automatic"
    assert [s["id"] for s in tool_steps[0]["detail"]["plan"]["steps"]] == ["s1", "s2", "s3"]


@pytest.mark.anyio
async def test_auto_approve_without_runner_leaves_no_orphan_task(env):
    """执行器未注册 ⇒ 判在**建任务行之前**：不留一条永远 pending 的孤儿计划行。"""
    await _forbid_registry(env)
    events = await run_turn(
        env, [_plan_response()], plan_mode=True, calls=[],
        auto_approve=True, forbidden_final=True,
    )

    assert await read_plan_tasks(env) == []
    chunks = [e for e in events if e["type"] == "final_chunk"]
    assert chunks and "执行器" in chunks[-1]["content"]
    rows = await read_tool_calls(env)
    assert [row.status for row in rows] == ["failed"]
    assert rows[0].result is None
    tool_steps = [
        e["data"] for e in events
        if e["type"] == "step_update" and e["data"]["step_type"] == "tool"
    ]
    assert len(tool_steps) == 1 and tool_steps[0]["status"] == "failed"
    assert tool_steps[0]["detail"]["error_code"] == dispatch.PLAN_RUNNER_UNAVAILABLE_CODE


@pytest.mark.anyio
async def test_registered_runner_receives_anchors_after_commit(env):
    """runner 用**独立 session** 也必须读得到任务行 ⇒ 调度必须发生在提交之后。"""
    seen: dict = {}

    async def fake_runner(**kwargs):
        seen.update(kwargs)
        ReaderSession = async_sessionmaker(bind=env.engine, expire_on_commit=False)
        async with ReaderSession() as session:
            seen["visible_task"] = await session.get(BackgroundTask, kwargs["plan_task_id"])

    dispatch.register_plan_runner(fake_runner)
    await _forbid_registry(env)
    events = await run_turn(
        env, [_plan_response()], plan_mode=True, calls=[],
        auto_approve=True, forbidden_final=True,
    )

    assert seen, "runner 没有被调用"
    assert seen["conversation_id"] == env.conversation_id
    assert seen["user_id"] == env.user_id
    assert seen["project_id"] == env.project_id
    assert [s["id"] for s in seen["steps"]] == ["s1", "s2", "s3"]
    assert seen["visible_task"] is not None, "调度早于提交：runner 读不到自己的任务行"
    assert seen["visible_task"].status == "pending"
    assert events[-1]["data"]["plan_task_id"] == seen["plan_task_id"]


# --------------------------------------------------------------------------- #
# Task 6：approve-plan 一次性批准端点
# --------------------------------------------------------------------------- #

APPROVABLE_STEPS = [
    {"id": "s1", "tool": "list_outlines", "arguments": {}},
    {"id": "s2", "tool": "start_project_task", "action": "expand_outline",
     "arguments": {"outline_id": "o1"}},
    {"id": "s3", "tool": "list_outlines", "arguments": {}},
]


async def seed_waiting_plan_call(env, *, steps=None, tool_name=PROPOSE_PLAN_TOOL_NAME,
                                 status="waiting_confirmation") -> str:
    """直接落一条待批准行（不依赖流式路径），返回 tool_call_id。"""
    arguments = {
        "objective": "build three chapters",
        "steps": APPROVABLE_STEPS if steps is None else steps,
    }
    if tool_name == PROPOSE_PLAN_TOOL_NAME:
        arguments = validate_plan(
            arguments, allowed_tools={"list_outlines", "start_project_task"},
        )
    ReaderSession = async_sessionmaker(bind=env.engine, expire_on_commit=False)
    async with ReaderSession() as session:
        row = AgentToolCall(
            conversation_id=env.conversation_id, user_id=env.user_id,
            project_id=env.project_id, tool_name=tool_name, arguments=arguments,
            risk_level=0, requires_confirmation=False, status=status,
        )
        session.add(row)
        await session.commit()
        return row.id


def _fake_request(env):
    """`_user_id(request)` 读 request.state.user_id（api/project_agent.py:48）。"""
    return SimpleNamespace(state=SimpleNamespace(user_id=env.user_id))


async def approve(env, tool_call_id, **payload_fields):
    ReaderSession = async_sessionmaker(bind=env.engine, expire_on_commit=False)
    async with ReaderSession() as session:
        return await agent_api.approve_plan(
            project_id=env.project_id,
            tool_call_id=tool_call_id,
            payload=agent_api.AgentPlanApprovalRequest(**payload_fields),
            request=_fake_request(env),
            db=session,
        )


async def load_tool_call(env, tool_call_id) -> AgentToolCall:
    ReaderSession = async_sessionmaker(bind=env.engine, expire_on_commit=False)
    async with ReaderSession() as session:
        return await session.get(AgentToolCall, tool_call_id)


@pytest.mark.anyio
async def test_approve_plan_returns_501_before_creating_any_task_row(env):
    """执行器未注册 ⇒ 501 必须判在**建任务行之前**，且抢占回滚、卡片没被吃掉。"""
    tool_call_id = await seed_waiting_plan_call(env)

    with pytest.raises(ApiError) as caught:
        await approve(env, tool_call_id)

    assert caught.value.code == dispatch.PLAN_RUNNER_UNAVAILABLE_CODE
    assert caught.value.status == 501
    assert (await load_tool_call(env, tool_call_id)).status == "waiting_confirmation"
    assert await read_plan_tasks(env) == []


@pytest.mark.anyio
async def test_approve_plan_claims_once_and_second_is_409(env):
    """勾选只保留被选步骤、顺序按计划；第二次批准必须 409（不新增第二套抢占）。"""
    started: list[dict] = []

    async def fake_runner(**kwargs):
        started.append(kwargs)
        return object()

    dispatch.register_plan_runner(fake_runner)
    tool_call_id = await seed_waiting_plan_call(env)

    first = await approve(env, tool_call_id, selected_step_ids=["s3", "s1"])
    assert first.steps_total == 2 and first.status == "executing"
    assert first.tool_call_id == tool_call_id
    assert [s["id"] for s in started[0]["steps"]] == ["s1", "s3"], "顺序必须按计划，不按勾选"
    assert started[0]["conversation_id"] == env.conversation_id

    record = await load_tool_call(env, tool_call_id)
    assert record.status == "executing"
    assert record.confirmed_at is not None
    tasks = await read_plan_tasks(env)
    assert len(tasks) == 1
    task = tasks[0]
    assert task.id == first.plan_task_id
    assert task.task_type == "agent_plan"
    assert task.status == "pending", "端点只建 pending 行，running 归 runner"
    assert task.task_input["tool_call_id"] == tool_call_id
    assert task.task_input["conversation_id"] == env.conversation_id
    assert [s["id"] for s in task.task_input["steps"]] == ["s1", "s3"]
    # PR-3 刷新后靠 result 的 {entity_id, task_type} 找计划行
    assert record.result == {"entity_id": task.id, "task_type": "agent_plan"}
    # 原始计划完整留在 arguments（批准的是子集，不改写提案本身）
    assert [s["id"] for s in record.arguments["steps"]] == ["s1", "s2", "s3"]

    with pytest.raises(ApiError) as again:
        await approve(env, tool_call_id)
    assert again.value.code == "conflict.agent_modification_state"
    assert again.value.status == 409
    assert len(await read_plan_tasks(env)) == 1, "第二次批准不得再建任务行"


@pytest.mark.anyio
async def test_approve_plan_rejects_unknown_or_empty_selection(env):
    """勾选非法 ⇒ 400 + 卡片回到 waiting_confirmation（用户还能重试）。"""
    dispatch.register_plan_runner(_noop_runner)
    tool_call_id = await seed_waiting_plan_call(env)

    with pytest.raises(ApiError) as unknown:
        await approve(env, tool_call_id, selected_step_ids=["nope"])
    assert unknown.value.code == "validation.agent_plan_step_selection"
    assert unknown.value.status == 400
    assert (await load_tool_call(env, tool_call_id)).status == "waiting_confirmation"
    assert await read_plan_tasks(env) == []

    with pytest.raises(ApiError) as empty:
        await approve(env, tool_call_id, selected_step_ids=[])
    assert empty.value.code == "validation.agent_plan_step_selection"
    assert await read_plan_tasks(env) == []


@pytest.mark.anyio
async def test_approve_plan_omitting_selection_approves_every_step(env):
    """N1 语义钉子：省略 `selected_step_ids` ⇒ **批准全部步骤**，顺序按计划。

    架构计划 §2 定的就是"客户端不传即整份批准"，而计划卡的「全选」走的正是这条
    路径（PR-3）。把默认改成拒绝不会有任何既有用例变红——那是一次静默的功能删除，
    所以这里显式钉住它。真正的危险（字段名写错被静默丢弃当成省略）已由
    `ConfigDict(extra="forbid")` 挡在 422，见
    `test_plan_approval_request_field_name_is_exact`。

    反向配对：传子集 ⇒ 只落子集。两种语义必须在同一个用例里互相制衡，
    否则"把默认改成全部/改成拒绝"都能只靠改一边通过。
    """
    started: list[dict] = []

    async def fake_runner(**kwargs):
        started.append(kwargs)
        return object()

    dispatch.register_plan_runner(fake_runner)

    all_call = await seed_waiting_plan_call(env)
    all_result = await approve(env, all_call)          # 不传 selected_step_ids
    assert all_result.steps_total == 3
    assert [s["id"] for s in started[0]["steps"]] == ["s1", "s2", "s3"], \
        "省略字段必须等价于按计划的完整步骤序列"

    # §7②：同一会话已有未定稿计划 ⇒ 第二次批准会被并发护栏拒绝；
    # 本用例只钉选择语义，因此第二次批准换到干净会话（夹具修正，非放宽护栏）。
    second_conversation = AgentConversation(
        user_id=env.user_id, project_id=env.project_id, title="second planning turn"
    )
    env.service.db.add(second_conversation)
    await env.service.db.commit()
    env.conversation_id = second_conversation.id
    subset_call = await seed_waiting_plan_call(env)
    subset_result = await approve(env, subset_call, selected_step_ids=["s2"])
    assert subset_result.steps_total == 1
    assert [s["id"] for s in started[1]["steps"]] == ["s2"]

    by_call = {t.task_input["tool_call_id"]: t for t in await read_plan_tasks(env)}
    assert [s["id"] for s in by_call[all_call].task_input["steps"]] == ["s1", "s2", "s3"]
    assert [s["id"] for s in by_call[subset_call].task_input["steps"]] == ["s2"]
    # 落库的任务行同样保留 objective：省略字段不得连带丢掉计划本体
    assert by_call[all_call].task_input["objective"] == "build three chapters"
    assert by_call[subset_call].task_input["objective"] == "build three chapters"
    assert len(by_call) == 2, "两次批准各自只建一行任务"
    assert (await load_tool_call(env, all_call)).status == "executing"
    assert (await load_tool_call(env, subset_call)).status == "executing"


@pytest.mark.anyio
async def test_approve_plan_refuses_a_tool_call_that_is_not_a_plan(env):
    """端点只吃 propose_plan：拿一张差异确认卡来批准必须被拒并回滚抢占。"""
    dispatch.register_plan_runner(_noop_runner)
    tool_call_id = await seed_waiting_plan_call(env, tool_name="update_project")

    with pytest.raises(ApiError) as caught:
        await approve(env, tool_call_id)
    assert caught.value.code == "conflict.agent_modification_state"
    assert (await load_tool_call(env, tool_call_id)).status == "waiting_confirmation"
    assert await read_plan_tasks(env) == []


def test_plan_approval_request_field_name_is_exact():
    """字段名写错 = 静默丢弃 = 等价于批准全部步骤，所以形状必须钉死。"""
    from pydantic import ValidationError

    assert set(agent_api.AgentPlanApprovalRequest.model_fields) == {"selected_step_ids"}
    assert agent_api.AgentPlanApprovalRequest().selected_step_ids is None
    with pytest.raises(ValidationError):
        agent_api.AgentPlanApprovalRequest(selected_ids=["s1"])


def test_plan_error_codes_are_registered_with_right_status():
    from app.core.errors import ERROR_REGISTRY

    assert ERROR_REGISTRY["internal.agent_plan_not_available"][1] == 501
    assert ERROR_REGISTRY["validation.agent_plan_step_selection"][1] == 400
    assert ERROR_REGISTRY["validation.agent_plan_invalid"][1] == 400
    # 只注册不使用：护栏在 PR-2c 落地，先把码占住避免同区域冲突
    assert ERROR_REGISTRY["conflict.agent_plan_running"][1] == 409


def test_agent_plan_task_type_maps_every_reachable_resource():
    """`agent_plan` 必须有非空资源映射，且每个名字都是前端真的在监听的那种。

    漏掉这个键的失败是**静默**的：`affected_resources_for_task()` 走
    `get(task_type, ())` 回 `[]` ⇒ SETTLED 事件 resources 为空 ⇒ 没有任何页面
    监听器命中 ⇒ 用户批准一份计划、任务跑完、数据落库，界面上却什么都没变；
    同一份漏项还会让 `FloatingTaskPanel` 的 `default: return taskType` 把
    "agent_plan" 这个原始字符串画给用户（前端侧由
    `frontend/src/i18n-integrity/task-type-label.test.ts` 钉住标签那一半）。

    `careers` / `organizations` 是最容易漏的两项：计划步骤可以写职业与组织，
    而它们只有 `career_generate` / `organization_generate` 两个窄映射，
    单看邻近条目很容易以为不必列。

    下面的"前端认识的资源名"是前端侧字面量（I6 约定）：后端 pytest 不读
    `../frontend`（纯后端环境/Docker 镜像只装 `backend/`，评审 F3 已裁定），
    清单在两侧各存一份，消费者见 `frontend/src/pages/*.tsx` 里
    `AGENT_DATA_CHANGED` 与任务 SETTLED 监听中的 `resources?.includes(...)`。
    """
    from app.services.task_resources import (
        TASK_TYPE_RESOURCES,
        affected_resources_for_task,
    )

    frontend_known_resources = {
        "tasks",  # ProjectAgentPanel 特殊处理，只用于刷新任务列表本身
        "projects",
        "outlines",
        "chapters",
        "characters",
        "careers",
        "organizations",
        "analysis",
        "foreshadows",
    }

    assert "agent_plan" in TASK_TYPE_RESOURCES, "缺条目 ⇒ affected_resources_for_task 静默回 []"
    resources = affected_resources_for_task("agent_plan")
    assert resources == [
        "chapters", "outlines", "characters", "careers",
        "organizations", "analysis", "projects", "foreshadows",
    ]
    assert {"careers", "organizations"} <= set(resources)
    assert [name for name in resources if name not in frontend_known_resources] == []
