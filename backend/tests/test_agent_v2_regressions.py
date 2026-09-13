"""PR-0a 回归网：锁住「工具结果持久化为对话历史」这条唯一路径的可观察行为。

覆盖 7 项：waiting_confirmation 收口、finalize_interrupted_turn 三态、
MCP 工具批准、auto_approve 直通、page_context 透传与轮数上限、
超大工具结果不挤掉首条诉求（护栏 2，锁 _serialize_tool_response 的长度上限）、
历史窗口的有界性（HISTORY_LIMIT 只是有界尾部窗口，不是容量保证）。

每条都断言持久化路径独有的可观察量（工具轮落成 agent_messages 行、工具结果以
<tool> 段进入**下一轮 prompt 的历史区块**）——否则只要「本轮能答出来」就算过，
跨回合持久化坏掉时无人报警。这也是为什么断言全是正向结构断言，而不是
"某段旧文案不出现" 这类负向断言：后者在旧实现被删掉后再也没有失败的可能。

变异自证配方（v1 与灰度 flag 都已删除，不能再靠翻 flag 分派来证伪）：
1. 把 `_save_tool_response` 的 `self.db.add(tool_msg)` / `await self.db.flush()`
   换成 `return tool_msg`（结果只留在内存、不落库）⇒ 实测 3 条变红：
   test_auto_approve_executes_inside_turn、
   test_page_context_and_persisted_history_reach_prompt、
   test_round_budget_exhaustion_raises_and_finalize_marks_cancelled。
2. 把 `_save_assistant_with_tool_calls` 的 `self.db.add(assistant)` 同样短路 ⇒
   实测 4 条变红：test_risk2_tool_stops_at_waiting_confirmation、
   test_mcp_write_tool_requires_confirmation 以及上面带 tool_calls 行的后两条。
3. 关掉护栏 2 的截断分支（把 `_serialize_tool_response` 的 `if len(content) > TOOL_RESULT_MAX_CHARS`
   短路）⇒ 实测 1 条变红：test_huge_tool_result_does_not_evict_earliest_request 撞
   `first_request in prompt`（超大行原样进 prompt 后首条诉求被挤掉）。直接抬
   `TOOL_RESULT_MAX_CHARS`（实测 10 ** 9）同样变红，且更早撞行大小前提自证
   （50059 > 10**9 不成立 ⇒ 本用例是空场景）。该用例另有 eviction 前提自证段：把
   `_build_prompt` 的 60000 预算抬到 500000（PR-0c 的可能改写）时它在自证段变红，
   不会静默退化成"没有东西可舍"的空场景。反向不锁：上限调小（实测 200）仍为绿，可接受 ——
   本用例锁的是"超大行不得挤掉首条诉求"，任何更严的上限都满足它，"截断分支成死路"这个
   方向由 test_history_limit_keeps_bounded_recent_tail 的配对断言守。
改回后必须重新全绿。
"""
import json
import uuid
from collections import Counter
from types import SimpleNamespace

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.database import Base
from app.models.project import Project
from app.models.project_agent import (
    AgentConversation,
    AgentExecutionStep,
    AgentMessage,
    AgentToolCall,
)
from app.services import project_agent_service as pas
from app.services.project_agent_service import ProjectAgentService
from app.services.project_agent_tools import ProjectAgentTool


@pytest.fixture
async def db_session():
    db_path = f"/tmp/test_agent_v2_reg_{uuid.uuid4().hex}.db"
    engine = create_async_engine(f"sqlite+aiosqlite:///{db_path}")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    Session = async_sessionmaker(bind=engine, expire_on_commit=False)
    async with Session() as session:
        yield session
    await engine.dispose()
    import os
    if os.path.exists(db_path):
        os.remove(db_path)


def add_fake_tool(svc: ProjectAgentService, name: str, risk_level: int) -> None:
    """注册一个假工具到 registry 的私有表。

    收敛成一个 helper：若 registry 换注册入口，测试侧只改这一处。
    risk_level=2 走确认分支，0 走直接执行分支。
    """
    svc.registry._tools[name] = ProjectAgentTool(
        name, "回归用假工具", {"type": "object", "properties": {}}, risk_level=risk_level
    )


async def make_conversation(db) -> AgentConversation:
    db.add(Project(id="proj-1", user_id="test", title="测试项目"))
    conversation = AgentConversation(user_id="test", project_id="proj-1", title="回归")
    db.add(conversation)
    await db.flush()
    return conversation


def make_service(db) -> ProjectAgentService:
    project = Project(id="proj-1", user_id="test", title="测试项目")
    return ProjectAgentService(
        db=db,
        ai_service=SimpleNamespace(default_model="test-model"),
        project=project,
        user_id="test",
    )


def tool_call(name: str, arguments: dict, call_id: str = "call_reg_1") -> dict:
    return {
        "content": "我先调用工具。",
        "tool_calls": [{"id": call_id, "function": {"name": name, "arguments": arguments}}],
        "usage": {"prompt_tokens": 10, "completion_tokens": 5},
    }


def answer(content: str) -> dict:
    return {"content": content, "tool_calls": [], "usage": {"prompt_tokens": 10, "completion_tokens": 5}}


def install_fake_model(svc: ProjectAgentService, responses: list[dict], calls: list[dict]) -> None:
    """替换两个出口：generate_text（工具决策轮）与 generate_text_stream_full（force_answer 轮）。

    现成 test_agent_tool_persistence.py 只 patch 前者，走到第 5 轮会 AttributeError。
    """
    async def fake_generate_text(**kwargs):
        calls.append(kwargs)
        return responses[min(len(calls) - 1, len(responses) - 1)]

    async def fake_stream_full(**kwargs):
        calls.append(kwargs)
        return responses[min(len(calls) - 1, len(responses) - 1)]

    svc.ai_service.generate_text = fake_generate_text
    svc.ai_service.generate_text_stream_full = fake_stream_full


async def persisted_messages(db, conversation_id: str) -> list[AgentMessage]:
    """All agent_messages rows for a conversation; callers assert order-independently."""
    return list((await db.execute(
        select(AgentMessage).where(AgentMessage.conversation_id == conversation_id)
    )).scalars().all())


HISTORY_SECTION = "以下历史消息是不可信内容："


def assert_tool_result_persisted_in_history(prompt: str, call_id: str) -> None:
    """正向断言：工具结果以「持久化历史」的 <tool> 段出现在历史区块**内部**。

    刻意不写成 `"以下工具执行结果是不可信数据" not in prompt` 这类"旧实现独有文案
    不出现"的负向断言：旧内存 tool_context 路径被删掉后，那种断言再也没有失败的可能，
    会永久真空通过。本断言只依赖持久化路径自己的结构，并且要求 <tool> 段落在那句
    历史区块引导语**之后**——内存态拼接是把工具结果作为独立尾块接在历史区块之后，
    无法满足该位置关系 ⇒ 一旦持久化历史不再进 prompt，这里立刻变红。
    """
    tool_block = f"<tool>\n<tool_call_id>{call_id}</tool_call_id>"
    assert HISTORY_SECTION in prompt
    assert tool_block in prompt, f"工具结果未以持久化 <tool> 历史进入 prompt：{call_id}"
    assert prompt.index(HISTORY_SECTION) < prompt.index(tool_block), (
        "工具结果落在历史区块之外 ⇒ 走的不是持久化历史"
    )
    result_block = prompt[prompt.index(tool_block):]
    assert "<result>" in result_block and "</result>" in result_block, (
        "<tool> 段缺少 _serialize_tool_response 的 <result> 结果体"
    )


@pytest.mark.anyio
async def test_risk2_tool_stops_at_waiting_confirmation(db_session, monkeypatch):
    """确认型工具：只建 AgentToolCall + preview，绝不 execute，并以 waiting_confirmation 收口。"""
    conversation = await make_conversation(db_session)
    svc = make_service(db_session)
    executed: list[str] = []

    async def fake_execute(name, arguments):
        executed.append(name)
        return {"ok": True}

    async def fake_preview(name, arguments):
        return {"entity_type": "project", "entity_id": "proj-1", "label": "标题",
                "changes": {"title": {"before": "测试项目", "after": "新标题"}}}

    monkeypatch.setattr(svc.registry, "execute", fake_execute)
    monkeypatch.setattr(svc.registry, "preview", fake_preview)
    add_fake_tool(svc, "reg_update", risk_level=2)
    calls: list[dict] = []
    install_fake_model(svc, [tool_call("reg_update", {}), answer("不该被走到")], calls)

    events = [e async for e in svc.stream_chat(
        conversation_id=conversation.id, message="改标题",
        page_context={"route": "/project/1"}, auto_approve=False)]

    assert executed == []
    result_events = [e for e in events if e.get("type") == "result"]
    assert result_events[-1]["data"]["status"] == "waiting_confirmation"
    rows = list((await db_session.execute(
        select(AgentToolCall).where(AgentToolCall.conversation_id == conversation.id)
    )).scalars().all())
    assert [r.status for r in rows] == ["waiting_confirmation"]
    assert rows[0].preview["changes"]["title"]["after"] == "新标题"
    assert len(calls) == 1

    # 持久化路径锁点：请求工具的 assistant 轮带 tool_calls 落库；未执行 ⇒ 不得有 role=tool 行。
    msgs = await persisted_messages(db_session, conversation.id)
    assert [m.role for m in msgs if m.role == "tool"] == []
    pending = [m for m in msgs if m.tool_calls]
    assert len(pending) == 1
    assert "reg_update" in pending[0].tool_calls


@pytest.mark.anyio
async def test_mcp_write_tool_requires_confirmation(db_session, monkeypatch):
    """MCP 非只读工具：进 proposed 分支且不执行；预览由 build_mcp_tool_preview 生成。"""
    conversation = await make_conversation(db_session)
    svc = make_service(db_session)
    svc.mcp_tools = [{"type": "function", "function": {"name": "mcp_write", "parameters": {}}}]
    mcp_calls: list[str] = []

    async def fake_mcp(user_id, tool_name, arguments, tool_call_id):
        mcp_calls.append(tool_name)
        return {"success": True, "content": "ok"}

    monkeypatch.setattr(pas, "execute_mcp_tool_call", fake_mcp)
    monkeypatch.setattr(pas, "mcp_tool_is_read_only", lambda metadata: False)
    calls: list[dict] = []
    install_fake_model(svc, [tool_call("mcp_write", {"a": 1}), answer("不该被走到")], calls)

    events = [e async for e in svc.stream_chat(
        conversation_id=conversation.id, message="调用外部工具",
        page_context={"route": "/project/1"}, auto_approve=False)]

    assert mcp_calls == []
    assert [e for e in events if e.get("type") == "result"][-1]["data"]["status"] == "waiting_confirmation"
    record = (await db_session.execute(
        select(AgentToolCall).where(AgentToolCall.conversation_id == conversation.id)
    )).scalars().one()
    assert record.preview["entity_type"] == "mcp_tool"
    assert record.preview["changes"]["execution"]["after"] == "批准后调用 MCP 服务"

    # 持久化路径锁点：MCP 轮同样以 tool_calls 持久化 assistant 行，且不落 tool 行。
    msgs = await persisted_messages(db_session, conversation.id)
    assert [m.role for m in msgs if m.role == "tool"] == []
    pending = [m for m in msgs if m.tool_calls]
    assert len(pending) == 1
    assert "mcp_write" in pending[0].tool_calls


@pytest.mark.anyio
async def test_auto_approve_executes_inside_turn(db_session, monkeypatch):
    """auto_approve=True：同一回合内直接执行并持久化 role=tool 结果。"""
    conversation = await make_conversation(db_session)
    svc = make_service(db_session)

    async def fake_execute(name, arguments):
        return {"data": {"ok": True}, "resources": ["characters"], "message": "已执行"}

    async def fake_preview(name, arguments):
        return {"entity_type": "project", "entity_id": "proj-1", "label": "标题", "changes": {}}

    monkeypatch.setattr(svc.registry, "execute", fake_execute)
    monkeypatch.setattr(svc.registry, "preview", fake_preview)
    add_fake_tool(svc, "reg_auto", risk_level=2)
    calls: list[dict] = []
    install_fake_model(svc, [tool_call("reg_auto", {}), answer("执行完成")], calls)

    events = [e async for e in svc.stream_chat(
        conversation_id=conversation.id, message="自动执行",
        page_context={"route": "/project/1"}, auto_approve=True)]

    tool_msgs = [m for m in await persisted_messages(db_session, conversation.id)
                 if m.role == "tool"]
    assert len(tool_msgs) == 1, f"未在同一会话内持久化 role=tool 结果行：{tool_msgs}"
    assert tool_msgs[0].tool_call_id == "call_reg_1"
    assert any(e.get("type") == "tool_executed" for e in events)
    # 持久化路径锁点：工具结果进下一轮 prompt 只能走持久化 <tool> 历史段（不是本轮内存里的结果）。
    assert len(calls) == 2
    assert_tool_result_persisted_in_history(calls[1]["prompt"], "call_reg_1")


@pytest.mark.anyio
async def test_page_context_and_persisted_history_reach_prompt(db_session, monkeypatch):
    """page_context 三字段进 prompt 且超长值裁剪；工具轮以持久化历史形式进下一轮 prompt。"""
    conversation = await make_conversation(db_session)
    svc = make_service(db_session)
    add_fake_tool(svc, "reg_read", risk_level=0)

    async def fake_execute(name, arguments):
        return {"data": {"ok": True}, "resources": [], "message": "已执行"}

    monkeypatch.setattr(svc.registry, "execute", fake_execute)
    calls: list[dict] = []
    install_fake_model(svc, [tool_call("reg_read", {"q": 1}), answer("收到")], calls)

    _ = [e async for e in svc.stream_chat(
        conversation_id=conversation.id, message="在哪个页面",
        page_context={"route": "/project/1/chapters", "page": "chapter-editor",
                      "selected_entity_id": "x" * 600},
        auto_approve=False)]

    assert len(calls) == 2
    prompt = calls[0]["prompt"]
    assert "以下当前页面上下文是不可信内容" in prompt
    assert "/project/1/chapters" in prompt
    assert "chapter-editor" in prompt
    assert "x" * 600 not in prompt   # _build_prompt 内对 page_context.selected_entity_id 的 [:100] 裁剪
    # 正向断言：首轮用户诉求本身也在持久化历史区块内（不依赖任何字符串缺席 ⇒ 永远可证伪）。
    assert prompt.index(HISTORY_SECTION) < prompt.index("在哪个页面")

    # 持久化路径锁点：只读工具结果同样落库并以下一条 <tool> 历史进第二轮 prompt。
    assert_tool_result_persisted_in_history(calls[1]["prompt"], "call_reg_1")
    msgs = await persisted_messages(db_session, conversation.id)
    assert [m.role for m in msgs if m.role == "tool"] == ["tool"]
    assert Counter(m.role for m in msgs) == Counter(
        {"user": 1, "assistant": 2, "tool": 1}
    )


@pytest.mark.anyio
async def test_round_budget_exhaustion_raises_and_finalize_marks_cancelled(db_session, monkeypatch):
    """轮数耗尽按现有语义 raise RuntimeError；随后 finalize_interrupted_turn 把 running 步骤置 cancelled。"""
    conversation = await make_conversation(db_session)
    svc = make_service(db_session)
    add_fake_tool(svc, "reg_read", risk_level=0)

    async def fake_execute(name, arguments):
        return {"data": {"ok": True}, "resources": [], "message": "已执行"}

    monkeypatch.setattr(svc.registry, "execute", fake_execute)
    calls: list[dict] = []
    install_fake_model(svc, [tool_call("reg_read", {}, call_id=f"call_{i}") for i in range(9)]
                       + [answer("兜底")], calls)

    with pytest.raises(RuntimeError, match="超过最大工具调用轮数"):
        _ = [e async for e in svc.stream_chat(
            conversation_id=conversation.id, message="一直查",
            page_context={"route": "/project/1"}, auto_approve=False)]

    assert len(calls) == ProjectAgentService.MAX_TOOL_ROUNDS + 1  # 4 个工具轮 + 第 5 轮 force_answer
    steps_before = len(list((await db_session.execute(
        select(AgentExecutionStep).where(AgentExecutionStep.conversation_id == conversation.id)
    )).scalars().all()))

    # 持久化路径锁点：每个工具轮都留下 assistant(tool_calls) + role=tool 两行，且末轮 prompt 看得到前几轮结果。
    msgs = await persisted_messages(db_session, conversation.id)
    assert sum(1 for m in msgs if m.role == "tool") == ProjectAgentService.MAX_TOOL_ROUNDS + 1
    assert sum(1 for m in msgs if m.tool_calls) == ProjectAgentService.MAX_TOOL_ROUNDS + 1
    assert_tool_result_persisted_in_history(calls[-1]["prompt"], "call_3")

    # finalize 的 running→cancelled 这条腿必须有真实的 running 行才测得到：
    # 轮数耗尽时所有步骤都已 completed ⇒ cancelled_steps 为空 ⇒ 旧的 all([]) 恒真（空覆盖）。
    # 这里用生产同一个 _create_step 落一条**已提交**的 running 步骤，对应生产形态
    # 「工具执行中途客户端断开：_save_tool_response 已 commit、_update_step 尚未跑」。
    await svc._create_step(
        svc._active_conversation, svc._active_user_message, 999,
        step_type="tool", category="tool", title="reg_read",
        content="正在调用工具。", status="running", steps=[],
    )
    await db_session.commit()
    running_before = list((await db_session.execute(
        select(AgentExecutionStep).where(
            AgentExecutionStep.conversation_id == conversation.id,
            AgentExecutionStep.status == "running",
        )
    )).scalars().all())
    assert len(running_before) >= 1, "前置数据未落出 running 步骤 ⇒ 本用例仍是空断言"
    running_ids = {step.id for step in running_before}

    await svc.finalize_interrupted_turn("客户端断开", cancelled=True)
    assistant_msgs = [m for m in await persisted_messages(db_session, conversation.id)
                      if m.role == "assistant"]
    assert any("本次执行已由用户停止。" == m.content for m in assistant_msgs)
    assert steps_before >= 1
    cancelled_steps = list((await db_session.execute(
        select(AgentExecutionStep).where(
            AgentExecutionStep.conversation_id == conversation.id,
            AgentExecutionStep.status == "cancelled",
        )
    )).scalars().all())
    assert len(cancelled_steps) > 0, (
        f"finalize_interrupted_turn 没有改写任何 running 步骤（running={running_ids}）"
        " ⇒ running→cancelled 这条腿未被执行"
    )
    assert running_ids <= {step.id for step in cancelled_steps}, (
        "仍处于 running 的步骤没被 finalize 收口"
    )
    assert all(s.content == "本次执行已由用户停止。" for s in cancelled_steps)


def tool_payload(size: int) -> str:
    """_save_tool_response 的落库形态：JSON 字符串（上限 TOOL_RESULT_PERSIST_MAX_CHARS）.

    size 只算 result.text 的字符数，整行再加约 40 字符的 JSON 骨架。
    """
    return json.dumps(
        {"tool": "reg_read", "error": None, "result": {"text": "结" * size}},
        ensure_ascii=False,
    )


@pytest.mark.anyio
async def test_huge_tool_result_does_not_evict_earliest_request(db_session, monkeypatch):
    """护栏 2：_serialize_tool_response 无截断 ⇒ 一条超大工具结果吃掉绝大部分
    _build_prompt 的 60000 总预算，触发 break 把首条用户诉求整条挤掉。

    行大小全部取生产可达值（落库上限 TOOL_RESULT_PERSIST_MAX_CHARS，另两条是章节详情
    级别的 7000），一条超大 + 两条中等即越过 break 阈值 —— 这正是 v2 工具多回合的正常
    形态。护栏 2（TOOL_RESULT_MAX_CHARS=8000 + 截断标记）后三条 tool 段必须全部保留。

    本用例自带前提自证：先算受护栏保护的 prompt，再把 TOOL_RESULT_MAX_CHARS 抬到
    10**9（等价于关掉护栏 2）重算同一份历史，**必须**看到首条诉求被挤掉。这样一旦
    PR-0c 改写 60000 预算、让这份历史再也舍不掉任何东西，本用例立刻在自证段变红，
    而不是静默退化成"没有东西可舍"的空场景。
    """
    conversation = await make_conversation(db_session)
    svc = make_service(db_session)
    first_request = "第一章的伏笔还没收，请先分析第 1 章"
    huge = tool_payload(ProjectAgentService.TOOL_RESULT_PERSIST_MAX_CHARS)
    db_session.add(AgentMessage(
        conversation_id=conversation.id, role="user", content=first_request))
    db_session.add(AgentMessage(
        conversation_id=conversation.id, role="assistant", content="我查一下。"))
    db_session.add(AgentMessage(
        conversation_id=conversation.id, role="tool",
        content=tool_payload(7000), tool_call_id="call_old"))
    db_session.add(AgentMessage(
        conversation_id=conversation.id, role="tool",
        content=tool_payload(7000), tool_call_id="call_mid"))
    db_session.add(AgentMessage(
        conversation_id=conversation.id, role="tool",
        content=huge, tool_call_id="call_big"))
    db_session.add(AgentMessage(
        conversation_id=conversation.id, role="user", content="继续"))
    await db_session.commit()

    history = await svc._load_history(conversation.id)
    assert [m.role for m in history] == [
        "user", "assistant", "tool", "tool", "tool", "user"], (
        f"前置历史形态不对：{[(m.role, len(m.content or '')) for m in history]}"
    )
    # 前提自证（行大小侧）：这条 tool 行必须真的越过护栏 2 的截断阈值，
    # 否则本用例测的只是"没有超长行"，护栏 2 的分支根本没被走到。
    assert len(huge) > ProjectAgentService.TOOL_RESULT_MAX_CHARS, (
        f"超大工具结果行只有 {len(huge)} 字符，未越过 TOOL_RESULT_MAX_CHARS"
        " ⇒ 截断分支未被触发，本用例是空场景"
    )

    guarded = svc._build_prompt(history, {"route": "/project/1"})
    # 前提自证：关掉护栏 2，同一份历史必须真的发生 eviction，否则本用例没在测预算
    monkeypatch.setattr(ProjectAgentService, "TOOL_RESULT_MAX_CHARS", 10 ** 9)
    unguarded = svc._build_prompt(history, {"route": "/project/1"})
    assert first_request not in unguarded, (
        "关掉护栏 2 后首条诉求仍在 ⇒ 行大小已越过 eviction 区间，需重新放大或改测预算本身")
    prompt = guarded

    assert first_request in prompt, (
        "首条用户诉求被超大工具结果挤掉 ⇒ _serialize_tool_response 未限长"
    )
    assert "已截断" in prompt, "超大工具结果未带截断标记"
    assert "结" * ProjectAgentService.TOOL_RESULT_PERSIST_MAX_CHARS not in prompt, (
        "落库上限的工具结果原样进 prompt"
    )
    assert prompt.count("<tool>") == 3, (
        f"三条 tool 结果段未全部进入 prompt：{prompt.count('<tool>')} ⇒ 仍有历史被舍"
    )


@pytest.mark.anyio
async def test_history_limit_keeps_bounded_recent_tail(db_session):
    """HISTORY_LIMIT 语义：只锁「窗口大小 + 最新的有序尾部 + 有界」，**不是**容量证明。

    本用例插的是 45 条同质 user 行（每行 2 字符），因此它证明不了"装得下一个多轮工具
    回合"：一回合落库的行数没有固定上界（实测单工具调用/轮 10 行 = 1 user + 5 assistant
    + 4 tool；一轮多并行调用按调用数线性增长，实测 4 轮 × 3 并行 = 18 行），行大小也
    远不止 2 字符。真正的容量约束在 _build_prompt 的 60000 字符预算——实测 8 条打满
    TOOL_RESULT_MAX_CHARS 的 tool 行只能带进 7 条，首条用户诉求仍会被挤掉；字节层面的
    取舍归 PR-0c 的预算分层。这里只锁行数窗口本身：45 行里最早的 5 行不得被读出来。
    """
    conversation = await make_conversation(db_session)
    svc = make_service(db_session)
    for i in range(45):
        db_session.add(AgentMessage(
            conversation_id=conversation.id, role="user", content=f"h{i}"))
    await db_session.commit()

    assert ProjectAgentService.HISTORY_LIMIT == 40, (
        "HISTORY_LIMIT 被改小 ⇒ 更早的工具回合会被整回合按行数舍掉"
    )
    history = await svc._load_history(conversation.id)
    assert len(history) == 40, f"历史窗口大小 {len(history)} ≠ HISTORY_LIMIT"
    assert [m.content for m in history] == [f"h{i}" for i in range(5, 45)], (
        "取到的不是最新的 40 条有序行 ⇒ 窗口或排序基准失效（护栏 1）"
    )
    # 配对断言：进 prompt 的上限必须严格小于落库上限，否则 _serialize_tool_response
    # 的截断分支永不触发 ⇒ 护栏 2 形同不存在（本文件的超大结果用例也会静默空转）。
    assert ProjectAgentService.TOOL_RESULT_MAX_CHARS < ProjectAgentService.TOOL_RESULT_PERSIST_MAX_CHARS, (
        "TOOL_RESULT_MAX_CHARS 不再小于落库侧 TOOL_RESULT_PERSIST_MAX_CHARS"
        " ⇒ 截断分支成为死路，单条工具结果仍可吃光 60000 历史预算"
    )
