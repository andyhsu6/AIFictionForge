"""issue #68：确认端点为被批准的调用补一条配对的 role=tool 结果行。

#66 起「持久化历史」是工具结果进入下一轮的唯一通道，而它只认 `role=tool` 行：
`_serialize_tool_response` 按 `agent_messages.tool_call_id` 与提案那条
`assistant(tool_calls)` 里的 provider id 配对。确认端点此前只落一条 assistant
散文行 ⇒ 下一轮 history 里那条 assistant(tool_calls) 永远配不到结果行，用户批准
的写入结果对模型不可见。

夹具（种子/假 registry/下一轮 prompt）在 `support/confirm_tool_row.py`；本模块只留
Given/When/Then。钉住的行为：
- 确认前：提案 assistant 行在库、绝无 role=tool 行（与既有回归同款前置）；
- 确认后：恰好一条 role=tool 行，`tool_call_id` == provider id，内容即工具结果；
- 下一轮 prompt 的历史区块里 assistant(tool_calls) 与 <tool> 结果段按同一 id 配对出现；
- 重复确认被 409 拒绝，不产生第二条结果行；
- 拒绝路径**不**落结果行（无结果可暴露，散文行已完整表达结局），显式钉住该决定。
"""
from __future__ import annotations

import json
import os
import uuid

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.api import project_agent as agent_api
from app.core.errors import ApiError
from app.database import Base
from support.confirm_tool_row import (
    OLDER_PROVIDER_CALL_ID,
    PROJECT_ID,
    PROVIDER_CALL_ID,
    FakeRegistry,
    fake_request,
    messages,
    next_turn_prompt,
    seed_pending_call,
    seed_pending_call_pointing_at_older_proposal,
    tool_rows,
)


@pytest.fixture(autouse=True)
def stub_history_budget(monkeypatch):
    """本文件锁的是确认路径的落库/配对，不是预算换算（与既有 agent 测试同习惯）。"""
    import app.services.agent_prompt_budget as apb

    async def fake_resolve(**kwargs):
        return 60_000

    monkeypatch.setattr(apb, "resolve_history_budget_chars", fake_resolve)


@pytest.fixture
async def db_engine():
    db_path = f"/tmp/test_confirm_tool_row_{uuid.uuid4().hex}.db"
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


@pytest.mark.anyio
async def test_confirm_persists_paired_tool_row_and_next_turn_sees_it(
    db_session, monkeypatch
):
    conversation_id, tool_call_id = await seed_pending_call(db_session)
    tool_result = {"message": "标题已更新", "data": {"title": "新标题"}}
    monkeypatch.setattr(
        agent_api, "ProjectAgentToolRegistry", lambda *a, **k: FakeRegistry(tool_result)
    )

    # (d) 确认前：提案行在库，但绝无 role=tool 行。
    assert [m.role for m in await messages(db_session, conversation_id) if m.role == "tool"] == []

    await agent_api.confirm_tool_call(
        project_id=PROJECT_ID,
        tool_call_id=tool_call_id,
        request=fake_request(),
        db=db_session,
    )

    # (a) 恰好一条配对行，id 必须等于提案 assistant 里的 provider id。
    rows = await tool_rows(db_session, conversation_id)
    assert len(rows) == 1, f"确认后必须恰好一条 role=tool 行，实得 {len(rows)}"
    assert rows[0].tool_call_id == PROVIDER_CALL_ID
    # (b) 内容就是 handler 产出的工具结果。
    assert json.loads(rows[0].content)["result"] == tool_result

    # (c) 下一轮 prompt：assistant(tool_calls) 与 <tool> 结果段按同一 id 配对出现。
    prompt = await next_turn_prompt(db_session, conversation_id)
    tool_block = f"<tool>\n<tool_call_id>{PROVIDER_CALL_ID}</tool_call_id>"
    assert tool_block in prompt, "工具结果未以持久化 <tool> 历史进入下一轮 prompt"
    assert f'"id": "{PROVIDER_CALL_ID}"' in prompt, "assistant(tool_calls) 段丢失"
    assert prompt.index("<tool_calls>") < prompt.index(tool_block), (
        "工具结果出现在提案之前 ⇒ 不是同一条调用的配对历史"
    )


@pytest.mark.anyio
async def test_second_confirm_is_refused_and_does_not_duplicate_the_row(
    db_session, monkeypatch
):
    conversation_id, tool_call_id = await seed_pending_call(db_session)
    monkeypatch.setattr(
        agent_api, "ProjectAgentToolRegistry", lambda *a, **k: FakeRegistry({"message": "ok"})
    )

    await agent_api.confirm_tool_call(
        project_id=PROJECT_ID, tool_call_id=tool_call_id,
        request=fake_request(), db=db_session,
    )

    with pytest.raises(ApiError) as again:
        await agent_api.confirm_tool_call(
            project_id=PROJECT_ID, tool_call_id=tool_call_id,
            request=fake_request(), db=db_session,
        )
    assert again.value.code == "conflict.agent_modification_state"
    assert len(await tool_rows(db_session, conversation_id)) == 1, "第二次确认不得再写结果行"


@pytest.mark.anyio
async def test_reject_does_not_persist_a_tool_row(db_session):
    """决定：拒绝路径不落结果行。

    拒绝没有工具结果可暴露（散文行已完整表达"已取消"），而 role=tool 语义是
    "工具返回了这个结果"；写一条 result=null 的行会把一次从未发生的执行渲染成
    工具调用。故此处显式钉住"不存在"，而不是让它保持未测试的偶然状态。
    """
    conversation_id, tool_call_id = await seed_pending_call(db_session)

    await agent_api.reject_tool_call(
        project_id=PROJECT_ID, tool_call_id=tool_call_id,
        request=fake_request(), db=db_session,
    )

    assert await tool_rows(db_session, conversation_id) == []


@pytest.mark.anyio
async def test_resolve_prefers_the_message_id_proposal_over_the_most_recent(
    db_session, monkeypatch
):
    """`message_id` 指向哪条提案，就用哪条的 provider id —— 不是「时间最近」那条。

    两次同工具同参数的提案在库里无法靠 (name, args) 区分，只有 `message_id` 能
    指向确切的提案消息；丢掉这一优先级就会取到最近一条，配错对。
    """
    conversation_id, tool_call_id = await seed_pending_call_pointing_at_older_proposal(
        db_session
    )
    monkeypatch.setattr(
        agent_api, "ProjectAgentToolRegistry", lambda *a, **k: FakeRegistry({"message": "ok"})
    )

    await agent_api.confirm_tool_call(
        project_id=PROJECT_ID, tool_call_id=tool_call_id,
        request=fake_request(), db=db_session,
    )

    rows = await tool_rows(db_session, conversation_id)
    assert len(rows) == 1
    assert rows[0].tool_call_id == OLDER_PROVIDER_CALL_ID, (
        "应优先采用 message_id 指向的提案（较旧），而不是时间最近的提案"
    )
