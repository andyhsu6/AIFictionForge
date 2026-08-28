"""Agent 工具调用持久化与 prompt 序列化单元测试（Todo #3/#4）。

覆盖：
- _build_prompt 对 assistant(tool_calls) 与 role="tool" 消息的序列化格式
- 含工具历史时 prompt 不丢失有效内容
- _save_assistant_with_tool_calls / _save_tool_response 保存后 _load_history 可还原
"""
import json
import os
import uuid
from types import SimpleNamespace

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.config import settings
from app.database import Base
from app.models.character import Character
from app.models.project import Project
from app.models.project_agent import AgentConversation, AgentMessage, AgentToolCall
from app.services.project_agent_service import ProjectAgentService


@pytest.fixture
async def db_session():
    """临时文件 SQLite（避免 in-memory 多连接问题），测试后清理。"""
    db_path = f"/tmp/test_agent_tool_{uuid.uuid4().hex}.db"
    engine = create_async_engine(f"sqlite+aiosqlite:///{db_path}")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    Session = async_sessionmaker(bind=engine, expire_on_commit=False)
    async with Session() as session:
        yield session
    await engine.dispose()
    if os.path.exists(db_path):
        os.remove(db_path)


def make_service(db, *, project_id="proj-1", user_id="test") -> ProjectAgentService:
    """构造最小 ProjectAgentService：registry 构造只存引用，不做 DB 查询。"""
    project = Project(id=project_id, user_id=user_id, title="测试项目")
    ai_service = SimpleNamespace(default_model="test-model")
    return ProjectAgentService(
        db=db, ai_service=ai_service, project=project, user_id=user_id
    )


def make_tool_history() -> list[AgentMessage]:
    """构造含 assistant(tool_calls) + tool 响应的历史消息列表（时间正序）。"""
    user = AgentMessage(conversation_id="conv-1", role="user", content="查询角色列表")
    assistant = AgentMessage(
        conversation_id="conv-1",
        role="assistant",
        content="我来查询角色信息。",
        tool_calls=json.dumps(
            [{
                "id": "call_abc123",
                "function": {"name": "get_characters", "arguments": {"project_id": "p1"}},
            }],
            ensure_ascii=False,
        ),
    )
    tool = AgentMessage(
        conversation_id="conv-1",
        role="tool",
        content=json.dumps(
            {"tool": "get_characters", "error": None, "result": [{"id": "c1", "name": "张三"}]},
            ensure_ascii=False,
        ),
        tool_call_id="call_abc123",
    )
    return [user, assistant, tool]


@pytest.mark.anyio
async def test_agent_message_tool_serialization(db_session):
    """assistant(tool_calls) 与 tool 消息按约定格式序列化进 prompt。"""
    svc = make_service(db_session)
    history = make_tool_history()

    prompt = svc._build_prompt(history, {"route": "/project/1"}, None, False)

    # assistant(tool_calls) 格式：<assistant>\n{content}\n<tool_calls>\n{json}\n</tool_calls>\n</assistant>
    assert "<assistant>\n我来查询角色信息。\n<tool_calls>\n" in prompt
    assert "</tool_calls>\n</assistant>" in prompt
    assert "get_characters" in prompt
    # tool 响应格式：<tool>\n<tool_call_id>{id}</tool_call_id>\n<result>{content}</result>\n</tool>
    assert "<tool>\n<tool_call_id>call_abc123</tool_call_id>\n<result>" in prompt
    assert "</result>\n</tool>" in prompt
    assert "张三" in prompt  # 工具结果内容进入 prompt


@pytest.mark.anyio
async def test_build_prompt_with_tool_history(db_session):
    """含工具历史时，工具结果与用户消息都保留（有效内容不被截断丢弃）。"""
    svc = make_service(db_session)
    history = make_tool_history()

    prompt = svc._build_prompt(history, {"route": "/project/1"}, None, False)

    assert "查询角色列表" in prompt  # 用户消息保留
    assert "我来查询角色信息。" in prompt  # assistant 内容保留
    assert "张三" in prompt  # 工具结果保留


@pytest.mark.anyio
async def test_save_and_load_tool_roundtrip(db_session):
    """保存 assistant(tool_calls) + tool 响应后，_load_history 可还原 role/tool_call_id。"""
    svc = make_service(db_session)
    conversation = AgentConversation(user_id="test", project_id="proj-1", title="测试对话")
    db_session.add(conversation)
    await db_session.flush()

    tool_calls = [{
        "id": "call_abc123",
        "function": {"name": "get_characters", "arguments": {"project_id": "p1"}},
    }]
    assistant = await svc._save_assistant_with_tool_calls(
        conversation, "我来查询角色信息。", tool_calls, 10, 20
    )
    tool_msg = await svc._save_tool_response(
        conversation, "call_abc123", "get_characters", [{"id": "c1", "name": "张三"}]
    )
    await db_session.commit()

    assert assistant.role == "assistant"
    assert assistant.tool_calls is not None
    assert "get_characters" in assistant.tool_calls
    assert tool_msg.role == "tool"
    assert tool_msg.tool_call_id == "call_abc123"

    history = await svc._load_history(conversation.id)
    roles = {m.role for m in history}
    assert "tool" in roles
    assert "assistant" in roles
    tool_msgs = [m for m in history if m.role == "tool"]
    assert len(tool_msgs) == 1
    assert tool_msgs[0].tool_call_id == "call_abc123"
    assert "张三" in tool_msgs[0].content


@pytest.mark.anyio
async def test_agent_reuses_tool_result(db_session, monkeypatch):
    """两轮对话（两条用户消息）：第一轮 LLM 调用 list_characters 查询角色，
    结果持久化为 role=tool 消息；第二轮 prompt 包含第一轮的工具结果，
    LLM 直接回答，不再重复查询（Todo #6 集成测试）。"""
    monkeypatch.setattr(settings, "agent_tool_persistence_enabled", True)

    db_session.add(Project(id="proj-1", user_id="test", title="测试项目"))
    db_session.add(Character(project_id="proj-1", name="张三"))
    await db_session.flush()

    svc = make_service(db_session)
    conversation = AgentConversation(user_id="test", project_id="proj-1", title="测试对话")
    db_session.add(conversation)
    await db_session.flush()

    prompts: list[str] = []
    responses: list[dict] = []

    async def fake_generate_text(**kwargs):
        prompts.append(kwargs["prompt"])
        if len(prompts) == 1:
            response = {
                "content": "我来查询角色信息。",
                "tool_calls": [{
                    "id": "call_abc123",
                    "function": {"name": "list_characters", "arguments": {}},
                }],
                "usage": {"prompt_tokens": 10, "completion_tokens": 5},
            }
        elif len(prompts) == 2:
            response = {
                "content": "查询完成。",
                "tool_calls": [],
                "usage": {"prompt_tokens": 20, "completion_tokens": 10},
            }
        else:
            response = {
                "content": "第一个角色是张三。",
                "tool_calls": [],
                "usage": {"prompt_tokens": 20, "completion_tokens": 10},
            }
        responses.append(response)
        return response

    svc.ai_service.generate_text = fake_generate_text

    # 第一轮：LLM 调用 list_characters 查询角色
    events1 = [e async for e in svc.stream_chat(
        conversation_id=conversation.id,
        message="查询角色列表",
        page_context={"route": "/project/1"},
        auto_approve=False,
    )]
    # 第二轮：新用户消息，LLM 应直接复用第一轮查询结果
    events2 = [e async for e in svc.stream_chat(
        conversation_id=conversation.id,
        message="第一个角色是谁？",
        page_context={"route": "/project/1"},
        auto_approve=False,
    )]

    # 共 3 次 LLM 调用：第一轮工具调用 + 第一轮收尾 + 第二轮直接回答
    assert len(prompts) == 3
    # 第二轮 prompt 以 <tool> 消息格式包含第一轮的工具结果（张三）→ 证明复用
    assert "<tool>\n<tool_call_id>call_abc123</tool_call_id>" in prompts[2]
    assert "张三" in prompts[2]
    # 第二轮 LLM 未再调用工具（直接回答）
    assert responses[2]["tool_calls"] == []

    # 工具结果已持久化为 role=tool 消息
    history = await svc._load_history(conversation.id)
    tool_msgs = [m for m in history if m.role == "tool"]
    assert len(tool_msgs) == 1
    assert "张三" in tool_msgs[0].content

    # AgentToolCall 记录仍写入（execution steps 展示依赖）
    records = (await db_session.execute(
        select(AgentToolCall).where(AgentToolCall.conversation_id == conversation.id)
    )).scalars().all()
    assert len(records) == 1
    assert records[0].tool_name == "list_characters"
    assert records[0].status == "executed"

    # 第二轮事件流无工具步骤（LLM 直接回答），以 result/completed 收尾
    tool_steps2 = [
        e for e in events2
        if e["type"] == "step_start" and e["data"]["step_type"] == "tool"
    ]
    assert tool_steps2 == []
    assert events2[-1]["type"] == "result"
    assert events2[-1]["data"]["status"] == "completed"