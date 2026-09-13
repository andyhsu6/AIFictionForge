"""PR-0c：助手 prompt 预算按实测窗口分层。

单位链（务必读完整再改数字）：
    get_effective_context_window() -> token
      -> x CHARS_PER_TOKEN -> 字符
      -> x ratio -> clamp(min, max) -> history_budget_chars
任何一环都不允许出现第二套系数或第二处 clamp。
"""
import os
import types
import uuid

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.core.errors import ApiError
from app.database import Base
from app.models.project import Project
from app.models.project_agent import AgentConversation, AgentMessage
from app.services import agent_prompt_budget as apb
from app.services.agent_prompt_budget import (
    CHARS_PER_TOKEN,
    HISTORY_BUDGET_ANCHOR_CAP_CHARS,
    compute_history_budget_chars,
    resolve_history_budget_chars,
)
from app.services.project_agent_service import ProjectAgentService
from app.services.project_agent_tools import ProjectAgentTool


def test_chars_per_token_is_the_single_source_constant():
    # 取值 1.0 的依据见 agent_prompt_budget.py 内注释（中文≈1 字符/token 的保守近似，
    # 出处 ai_service.py 的"单位错配"历史）。改这个数字必须同时改 config 默认值。
    assert CHARS_PER_TOKEN == 1.0


def test_one_million_token_window_yields_300000_chars():
    # 架构计划 §5 验收原文：mock 返回 1_000_000 ⇒ 预算 300000
    assert compute_history_budget_chars(1_000_000) == 300_000


def test_upper_clamp_prevents_using_the_whole_window_as_history():
    assert compute_history_budget_chars(100_000_000) == 400_000


def test_lower_clamp_is_the_legacy_value_and_not_a_small_model_fallback():
    # 60000 恰等于现状硬编码值；它的用途是防异常配置算出过小/无界预算，
    # 不是为小模型兜底（小模型已被计划 B 拦在系统外）。
    assert compute_history_budget_chars(1_000) == 60_000


def test_zero_window_clamps_to_floor_instead_of_becoming_unbounded_or_zero():
    assert compute_history_budget_chars(0) == 60_000


def test_negative_window_is_rejected_not_silently_clamped():
    with pytest.raises(ValueError):
        compute_history_budget_chars(-1)


def test_anchor_cap_matches_per_message_truncation_cap():
    # 永不裁剪段的长度上限沿用历史里的单条 6000 字符口径，不新造一个魔法数
    assert HISTORY_BUDGET_ANCHOR_CAP_CHARS == 6_000


@pytest.mark.anyio
async def test_resolver_passes_the_probed_window_through_the_single_formula(monkeypatch):
    seen = {}

    async def fake_probe(user_id, model, db, *, provider=None, base_url=None):
        seen.update(
            user_id=user_id, model=model, db=db, provider=provider, base_url=base_url
        )
        return 1_000_000

    monkeypatch.setattr(apb, "get_effective_context_window", fake_probe)
    budget = await resolve_history_budget_chars(
        user_id="u1", model="m1", db=object(), provider="openai",
        base_url="https://gw.example/v1",
    )
    assert budget == 300_000
    # 三元组必须原样送达：省略 provider/base_url 会让"同名模型挂两个网关"的用户
    # 每次发 prompt 都吃一个 validation.ai_model_below_minimum（契约见锚点复核）
    assert seen == {
        "user_id": "u1",
        "model": "m1",
        "db": seen["db"],
        "provider": "openai",
        "base_url": "https://gw.example/v1",
    }


def test_provider_and_base_url_are_mandatory_keywords():
    # 不给默认值 ⇒ 漏传就是 TypeError，而不是悄悄走"按模型名查缓存"的歧义路径
    with pytest.raises(TypeError):
        resolve_history_budget_chars(user_id="u", model="m", db=None)  # type: ignore[call-arg]


@pytest.mark.anyio
async def test_probe_failure_bubbles_up_untouched(monkeypatch):
    """预算算不出来时必须**明确报错**，禁止静默退回 60000。

    也不要 except Exception 再包一层 —— 那会把门禁的错误码文案吃掉。
    """

    async def boom(user_id, model, db, *, provider=None, base_url=None):
        raise ApiError(
            code="validation.ai_model_below_minimum",
            params={"model": model, "min_window": 1_000_000},
        )

    monkeypatch.setattr(apb, "get_effective_context_window", boom)
    with pytest.raises(ApiError) as exc:
        await resolve_history_budget_chars(
            user_id="u1", model="gpt-4", db=object(), provider="openai", base_url=""
        )
    assert exc.value.code == "validation.ai_model_below_minimum"
    assert exc.value.code != "internal.error"  # 没被泛化成通用失败


@pytest.mark.anyio
async def test_trace_is_filled_with_tokens_and_budget(monkeypatch):
    async def fake_probe(user_id, model, db, *, provider=None, base_url=None):
        return 2_000_000

    monkeypatch.setattr(apb, "get_effective_context_window", fake_probe)
    trace = apb.PromptBudgetTrace(budget_chars=0)
    budget = await resolve_history_budget_chars(
        user_id="u", model="m", db=object(), provider="openai", base_url="b",
        trace=trace,
    )
    assert budget == 400_000  # 2M x 0.3 = 600000 -> 上限 400000
    assert (trace.effective_tokens, trace.budget_chars) == (2_000_000, 400_000)


def test_config_defaults_match_the_module_constants():
    """四个键的默认值必须与模块常量逐一对齐 —— 这条断言就是防漂移的门。"""
    from app.config import settings

    assert settings.agent_chars_per_token == CHARS_PER_TOKEN
    assert settings.agent_history_budget_ratio == apb.HISTORY_BUDGET_RATIO
    assert settings.agent_history_budget_min_chars == apb.HISTORY_BUDGET_MIN_CHARS
    assert settings.agent_history_budget_max_chars == apb.HISTORY_BUDGET_MAX_CHARS


def test_settings_can_tune_the_ratio(monkeypatch):
    monkeypatch.setattr(apb, "HISTORY_BUDGET_RATIO", 0.5, raising=False)
    assert compute_history_budget_chars(1_000_000, ratio=0.5) == 400_000


# --------------------------------------------------------------------------
# Task 3：_build_prompt 改用注入预算 + 裁剪留痕
# --------------------------------------------------------------------------


def make_msg(role, content, tool_calls=None, tool_call_id=None):
    """`_build_prompt` 只读 role / content / tool_calls / tool_call_id 四个属性。"""
    return types.SimpleNamespace(
        role=role, content=content, tool_calls=tool_calls, tool_call_id=tool_call_id
    )


def bare_service():
    """绕开 DB：`_build_prompt` 与 `_history_budget_chars` 都不需要真实会话。"""
    svc = ProjectAgentService.__new__(ProjectAgentService)
    svc.project = types.SimpleNamespace(id="p1", title="project one")
    svc.user_id = "u1"
    svc.db = None
    svc.ai_service = types.SimpleNamespace(
        default_model="m1", api_provider="openai", base_url="https://gw.example/v1"
    )
    return svc


def build(svc, history, budget_chars, trace=None):
    return svc._build_prompt(
        history, {}, False, budget_chars=budget_chars, trace=trace
    )


def test_build_prompt_requires_budget_chars_keyword():
    svc = bare_service()
    with pytest.raises(TypeError) as exc:
        svc._build_prompt([make_msg("user", "hi")], {})  # type: ignore[call-arg]
    # 反向钉子必须**只**在"参数缺失"时成立：若实现退回硬编码、或把 budget_chars 做成
    # 可选参数，报错文案会是"unexpected keyword"/根本没有报错，这两种都要判失败。
    message = str(exc.value)
    assert "budget_chars" in message
    assert "missing" in message, f"不是缺少关键字参数导致的 TypeError：{message}"


def test_hardcoded_60000_is_gone_and_budget_is_honoured():
    svc = bare_service()
    body = "x" * 5_000
    history = [make_msg("user", f"oldest {body}"), make_msg("user", f"mid {body}"),
               make_msg("user", f"newest {body}")]
    trace = apb.PromptBudgetTrace(budget_chars=6_000)
    prompt = build(svc, history, 6_000, trace)
    # 每条 part ≈ 5.0k 字符 ⇒ 装下 newest 后 mid 就超预算，break 时 newest 已收，
    # 剩余 2 条（mid + oldest）即 dropped（reversed 序 index=1，total=3）。
    assert "newest" in prompt
    assert "mid" not in prompt
    assert "oldest" not in prompt          # 最旧的被丢弃 —— PR-0c 之前这件事不可见
    assert trace.dropped_messages == 2
    assert trace.dropped_chars > 9_000     # 只统计 content 长度，见 _count_remaining_chars
    assert trace.budget_chars == 6_000
    assert trace.used_chars > 0


def test_larger_budget_keeps_everything_and_reports_no_drop():
    svc = bare_service()
    body = "y" * 5_000
    history = [make_msg("user", f"a {body}"), make_msg("user", f"b {body}"),
               make_msg("user", f"c {body}")]
    trace = apb.PromptBudgetTrace(budget_chars=300_000)
    prompt = build(svc, history, 300_000, trace)
    assert all(t in prompt for t in ("a ", "b ", "c "))
    assert trace.dropped_messages == 0
    assert trace.dropped_chars == 0


def test_no_trace_still_works():
    svc = bare_service()
    prompt = build(svc, [make_msg("user", "hello")], 60_000)
    assert "hello" in prompt


def test_budget_binds_characters_not_row_count():
    """PR-0a 实测：绑死裁剪的是字符预算，不是行数。

    历史形态取 PR-0a 记下的真实形状：1 条首问 + 8 条打满 `TOOL_RESULT_MAX_CHARS`
    的 tool 行 = 9 行，**远小于** HISTORY_LIMIT=40 ⇒ 行窗口一条都没舍。
    两次跑同一份历史，只改字符预算：60000 时只能带进 7 条 tool 段（丢弃 2 条），
    400000 时 8 条全进、零丢弃。行数、HISTORY_LIMIT、序列化分支全都固定不变，
    唯一变量是字符预算 ⇒ 本用例证伪"预算其实由行数决定"这种读法。
    """
    svc = bare_service()
    fill = "结" * ProjectAgentService.TOOL_RESULT_MAX_CHARS
    history = [make_msg("user", "first request")] + [
        make_msg("tool", fill, tool_call_id=f"call_{i}") for i in range(8)
    ]
    assert len(history) < ProjectAgentService.HISTORY_LIMIT, (
        "前置失效：行数已越过 HISTORY_LIMIT ⇒ 无法证明约束来自字符预算"
    )
    assert all(len(m.content) >= ProjectAgentService.TOOL_RESULT_MAX_CHARS
               for m in history if m.role == "tool")

    tight_trace = apb.PromptBudgetTrace(budget_chars=60_000)
    tight = build(svc, history, 60_000, tight_trace)
    assert tight.count("<tool>") == 7, (
        f"8 条打满单条上限的 tool 行只应带进 7 条，实际 {tight.count('<tool>')} 条"
        " ⇒ 裁剪不是按字符预算发生的"
    )
    assert tight_trace.dropped_messages == 2
    # 留痕只统计 content 长度（不做二次序列化）：首问 + 一条打满上限的 tool 行
    assert tight_trace.dropped_chars == len("first request") + ProjectAgentService.TOOL_RESULT_MAX_CHARS

    loose_trace = apb.PromptBudgetTrace(budget_chars=400_000)
    loose = build(svc, history, 400_000, loose_trace)
    assert loose.count("<tool>") == 8
    assert loose_trace.dropped_messages == 0
    assert loose_trace.dropped_chars == 0


@pytest.mark.anyio
async def test_history_budget_helper_uses_the_real_provider_triple(monkeypatch):
    seen = {}

    async def fake_resolve(**kwargs):
        seen.update(kwargs)
        return 300_000

    monkeypatch.setattr(apb, "resolve_history_budget_chars", fake_resolve)
    svc = bare_service()
    trace = apb.PromptBudgetTrace(budget_chars=0)
    budget = await svc._history_budget_chars(trace)
    assert budget == 300_000
    assert seen["provider"] == "openai"
    assert seen["base_url"] == "https://gw.example/v1"
    assert seen["model"] == "m1"
    assert seen["user_id"] == "u1"
    assert seen["db"] is None


@pytest.mark.anyio
async def test_history_budget_helper_surfaces_probe_error(monkeypatch):
    async def fake_resolve(**kwargs):
        raise ApiError(
            code="validation.ai_model_below_minimum",
            params={"model": "m1", "min_window": 1_000_000},
        )

    monkeypatch.setattr(apb, "resolve_history_budget_chars", fake_resolve)
    svc = bare_service()
    with pytest.raises(ApiError):
        await svc._history_budget_chars(apb.PromptBudgetTrace(budget_chars=0))


# --------------------------------------------------------------------------
# Task 3：回合级接线 —— 预算每回合算一次、注入每一轮
# --------------------------------------------------------------------------


@pytest.fixture
async def db_session():
    """短生命周期 SQLite 会话：本用例要跑真实的 stream_chat 持久化链路。"""
    db_path = f"/tmp/test_agent_budget_{uuid.uuid4().hex}.db"
    engine = create_async_engine(f"sqlite+aiosqlite:///{db_path}")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    Session = async_sessionmaker(bind=engine, expire_on_commit=False)
    async with Session() as session:
        yield session
    await engine.dispose()
    if os.path.exists(db_path):
        os.remove(db_path)


@pytest.mark.anyio
async def test_stream_chat_resolves_the_budget_once_per_turn(db_session, monkeypatch):
    """接线形状：async 侧每**回合**算一次，轮循环里每一轮都注入同一个值。

    这条用例是唯一能证伪"调用点其实还写着硬编码 60000"的：种子历史约 64k 字符，
    桩预算 6000 ⇒ 只要注入生效，两轮 prompt 都必须远小于历史本身。
    同时用两轮（工具轮 + 回答轮）证明换算没有被挪进轮循环重复 await。
    """
    project = Project(id="p1", user_id="u1", title="project one")
    conversation = AgentConversation(user_id="u1", project_id="p1", title="t")
    db_session.add(project)
    db_session.add(conversation)
    await db_session.flush()
    fill = "结" * ProjectAgentService.TOOL_RESULT_MAX_CHARS
    db_session.add(AgentMessage(
        conversation_id=conversation.id, role="user", content="first request"))
    for i in range(8):
        db_session.add(AgentMessage(
            conversation_id=conversation.id, role="tool",
            content=fill, tool_call_id=f"call_seed_{i}"))
    await db_session.commit()
    seeded_chars = sum(len(fill) for _ in range(8))
    assert seeded_chars > 60_000, "前置失效：种子历史必须大于旧硬编码预算"

    prompts: list[str] = []
    ai_service = types.SimpleNamespace(
        default_model="m1",
        api_provider="openai",
        base_url="https://gw.example/v1",
    )

    async def fake_generate_text(**kwargs):
        prompts.append(kwargs["prompt"])
        if len(prompts) > 1:
            return {"content": "收到", "tool_calls": [], "usage": {}}
        return {
            "content": "我查一下。",
            "tool_calls": [{
                "id": "call_live_1",
                "function": {"name": "budget_read", "arguments": {}},
            }],
            "usage": {"prompt_tokens": 1, "completion_tokens": 1},
        }

    async def fake_stream_full(**kwargs):
        prompts.append(kwargs["prompt"])
        return {"content": "收到", "tool_calls": [], "usage": {}}

    ai_service.generate_text = fake_generate_text
    ai_service.generate_text_stream_full = fake_stream_full

    svc = ProjectAgentService(
        db=db_session, ai_service=ai_service, project=project, user_id="u1"
    )
    svc.registry._tools["budget_read"] = ProjectAgentTool(
        "budget_read", "预算接线用例用", {"type": "object", "properties": {}},
        risk_level=0,
    )

    async def fake_execute(name, arguments):
        return {"data": {"ok": True}, "resources": [], "message": "已执行"}

    monkeypatch.setattr(svc.registry, "execute", fake_execute)

    resolver_calls: list[dict] = []

    async def fake_resolve(**kwargs):
        resolver_calls.append(kwargs)
        return 6_000

    monkeypatch.setattr(apb, "resolve_history_budget_chars", fake_resolve)

    _ = [
        e async for e in svc.stream_chat(
            conversation_id=conversation.id,
            message="继续",
            page_context={"route": "/project/1"},
            auto_approve=False,
        )
    ]

    assert len(prompts) == 2, f"本用例需要两轮（工具轮 + 回答轮），实际 {len(prompts)}"
    assert len(resolver_calls) == 1, (
        f"预算换算应每回合一次，实际 {len(resolver_calls)} 次 ⇒ 被挪进了轮循环"
    )
    assert all(len(prompt) < 20_000 for prompt in prompts), (
        "注入的 6000 字符预算没生效 ⇒ 调用点仍在用别处的预算值"
        f"（prompt 长度={[len(p) for p in prompts]}，种子历史={seeded_chars} 字符）"
    )
