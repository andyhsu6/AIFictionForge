"""确认端点（issue #68）新增测试用的数据/服务夹具。

`test_agent_confirm_tool_row.py` 只保留 Given/When/Then 断言；跨用例复用的常量、
假 registry、落库种子与「下一轮 prompt」读取集中在这里，避免测试模块越过 250
pure LOC 上限（见 programming skill 的 oversized-module 规则）。
"""
from __future__ import annotations

import json
from datetime import datetime
from types import SimpleNamespace

from sqlalchemy import select

from app.models.project import Project
from app.models.project_agent import AgentConversation, AgentMessage, AgentToolCall
from app.services.project_agent_service import ProjectAgentService
from support.agent_stubs import AgentAIServiceStub

PROJECT_ID = "proj-confirm"
USER_ID = "u-confirm"
PROVIDER_CALL_ID = "call_confirm_1"
OLDER_PROVIDER_CALL_ID = "call_proposal_older"
NEWER_PROVIDER_CALL_ID = "call_proposal_newer"
TOOL_NAME = "reg_update"
TOOL_ARGS = {"title": "新标题"}
# 端点会把「现算预览」与「入库预览」逐字比较（stale 守卫）⇒ 两侧必须同形。
PREVIEW = {
    "entity_type": "project",
    "entity_id": PROJECT_ID,
    "label": "标题",
    "changes": {"title": {"before": "旧标题", "after": "新标题"}},
}


class FakeRegistry:
    """确认端点内部自建 registry（`ProjectAgentToolRegistry(project, db)`）。

    用能 get/preview/execute 的假件替换，避免为一条 route 断言去注册真实工具。
    """

    def __init__(self, result: dict) -> None:
        self._result = result

    def get(self, name: str):
        return SimpleNamespace(name=name)

    async def preview(self, name: str, arguments: dict) -> dict:
        return PREVIEW

    async def execute(self, name: str, arguments: dict) -> dict:
        return self._result


def fake_request(user_id: str = USER_ID):
    """`_user_id(request)` 读 request.state.user_id（api/project_agent.py:67）。"""
    return SimpleNamespace(state=SimpleNamespace(user_id=user_id))


def _proposal_message(
    conversation_id: str, content: str, call_id: str, *, day: int | None = None
) -> AgentMessage:
    message = AgentMessage(
        conversation_id=conversation_id,
        role="assistant",
        content=content,
        tool_calls=json.dumps([{
            "id": call_id,
            "function": {"name": TOOL_NAME, "arguments": TOOL_ARGS},
        }], ensure_ascii=False),
    )
    if day is not None:
        message.created_at = datetime(2026, 1, day)
    return message


def _pending_record(conversation_id: str, *, message_id: str | None = None) -> AgentToolCall:
    return AgentToolCall(
        conversation_id=conversation_id,
        user_id=USER_ID,
        project_id=PROJECT_ID,
        tool_name=TOOL_NAME,
        arguments=TOOL_ARGS,
        risk_level=2,
        requires_confirmation=True,
        status="waiting_confirmation",
        preview=PREVIEW,
        message_id=message_id,
    )


async def _seed_conversation(db) -> AgentConversation:
    db.add(Project(id=PROJECT_ID, user_id=USER_ID, title="确认用例项目"))
    conversation = AgentConversation(user_id=USER_ID, project_id=PROJECT_ID, title="c")
    db.add(conversation)
    await db.flush()
    return conversation


async def seed_pending_call(db) -> tuple[str, str]:
    """落一条「已提案、待确认」的调用：assistant(tool_calls) 行 + AgentToolCall 行。

    直接落库而不是驱动 stream_chat：测试锁的是确认端点，不是提案回合。
    """
    conversation = await _seed_conversation(db)
    db.add(_proposal_message(conversation.id, "我先改标题。", PROVIDER_CALL_ID))
    record = _pending_record(conversation.id)
    db.add(record)
    await db.commit()
    return conversation.id, record.id


async def seed_pending_call_pointing_at_older_proposal(db) -> tuple[str, str]:
    """两次「同工具、同参数」的提案，pending 行的 `message_id` 指向**较旧**那条。

    用来钉住解析顺序：`message_id` 优先于「最近一条 assistant(tool_calls)」扫描。
    较旧/较新用显式 `created_at` 区分（默认值同毫秒会并列）。
    """
    conversation = await _seed_conversation(db)
    older = _proposal_message(conversation.id, "较早的提案。", OLDER_PROVIDER_CALL_ID, day=1)
    newer = _proposal_message(conversation.id, "较新的提案。", NEWER_PROVIDER_CALL_ID, day=2)
    db.add_all([older, newer])
    await db.flush()
    record = _pending_record(conversation.id, message_id=older.id)
    db.add(record)
    await db.commit()
    return conversation.id, record.id


async def messages(db, conversation_id: str) -> list[AgentMessage]:
    return list((await db.execute(
        select(AgentMessage)
        .where(AgentMessage.conversation_id == conversation_id)
        .order_by(AgentMessage.created_at)
    )).scalars().all())


async def tool_rows(db, conversation_id: str) -> list[AgentMessage]:
    return [m for m in await messages(db, conversation_id) if m.role == "tool"]


async def next_turn_prompt(db, conversation_id: str) -> str:
    """驱动一轮普通对话，返回发给模型的 prompt（历史区块在里头）。"""
    calls: list[dict] = []
    svc = ProjectAgentService(
        db=db,
        ai_service=AgentAIServiceStub(default_model="m"),
        project=Project(id=PROJECT_ID, user_id=USER_ID, title="确认用例项目"),
        user_id=USER_ID,
    )

    async def fake_generate(**kwargs):
        calls.append(kwargs)
        return {"content": "好的。", "tool_calls": [], "usage": {}}

    svc.ai_service.generate_text = fake_generate
    svc.ai_service.generate_text_stream_full = fake_generate
    async for _ in svc.stream_chat(
        conversation_id=conversation_id,
        message="刚才的修改结果是什么？",
        page_context={"route": "/p/1"},
    ):
        pass
    assert calls, "下一轮没有向模型发请求"
    return calls[0]["prompt"]
