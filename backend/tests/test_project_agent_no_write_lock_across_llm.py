"""issue #222 回归护栏：跨 LLM 调用的长 IO 不得持有 SQLite 写事务（写锁）。

失效形态（实测）：`_create_step` / `_update_step` / `_save_assistant_with_tool_calls`
一律只 `flush()` 不 `commit()`，而 `stream_chat` 在轮首先 flush 一轮 step，紧接着
`await self._call_round(...)`（实测 30~190s）。这段时间 SQLite 的写锁被本进程独占，
`backend/app/database.py` 的 busy_timeout(30s) 到点后，任何并发写（后台任务进度、
计划行更新、取消操作）必然抛 "database is locked"。

本文件钉两条**可观察**契约（不读服务内部字段）：
1. 进入 `_call_round` 的那一刻没有未提交事务；
2. 更强的等价表述：此刻另一条连接能立刻写入（修复前它抛 OperationalError）。

两条都在修复前变红、修复后变绿；夹具全部为中性占位，不含任何导入原文
（AGENTS.md 脱敏硬约束）。
"""
import os
import uuid

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.database import Base
from app.models.project import Project
from app.models.project_agent import AgentConversation, AgentMessage
from app.services import agent_prompt_budget as apb
from app.services import project_agent_service as pas
from app.services.project_agent_service import ProjectAgentService
from app.services.project_agent_tools import ProjectAgentTool
from support.agent_stubs import AgentAIServiceStub


@pytest.fixture(autouse=True)
def stub_history_budget(monkeypatch):
    """预算换算依赖窗口探测结论（DB 缓存行 + 网关元数据），与写锁无关。

    与 test_agent_tool_persistence.py / test_agent_v2_regressions.py 同款钉法：
    固定成 PR-0c 之前的 60000，本文件只证「长 IO 之前有没有未提交事务」。
    """

    async def fake_resolve(**kwargs):
        return 60_000

    monkeypatch.setattr(apb, "resolve_history_budget_chars", fake_resolve)


@pytest.fixture
def db_path() -> str:
    """临时文件 SQLite 路径（第二条连接要能独立打开同一个库）。"""
    return f"/tmp/test_agent_writelock_{uuid.uuid4().hex}.db"


@pytest.fixture
async def db_session(db_path):
    engine = create_async_engine(f"sqlite+aiosqlite:///{db_path}")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    Session = async_sessionmaker(bind=engine, expire_on_commit=False)
    async with Session() as session:
        yield session
    await engine.dispose()
    if os.path.exists(db_path):
        os.remove(db_path)


async def make_conversation(db) -> AgentConversation:
    db.add(Project(id="proj-1", user_id="test", title="测试项目"))
    conversation = AgentConversation(user_id="test", project_id="proj-1", title="写锁回归")
    db.add(conversation)
    await db.flush()
    return conversation


def make_service(db) -> ProjectAgentService:
    project = Project(id="proj-1", user_id="test", title="测试项目")
    return ProjectAgentService(
        db=db,
        ai_service=AgentAIServiceStub(
            default_model="test-model",
            api_provider="openai",
            base_url="https://gw.example/v1",
        ),
        project=project,
        user_id="test",
    )


def answer(content: str) -> dict:
    return {
        "content": content,
        "tool_calls": [],
        "usage": {"prompt_tokens": 10, "completion_tokens": 5},
    }


def tool_call(name: str) -> dict:
    return {
        "content": "我先调用工具。",
        "tool_calls": [{"id": "call_lock_1", "function": {"name": name, "arguments": {}}}],
        "usage": {"prompt_tokens": 10, "completion_tokens": 5},
    }


def install_call_round_recorder(
    svc: ProjectAgentService, responses: list[dict], observed: list[bool]
) -> None:
    """替换 _call_round：记录「进入 LLM 调用这一刻」是否有未提交事务。"""

    async def fake_call_round(
        *, prompt, system_prompt, force_answer, available_tools, tool_choice="auto"
    ):
        observed.append(svc.db.in_transaction())
        return responses[min(len(observed) - 1, len(responses) - 1)]

    svc._call_round = fake_call_round


async def write_from_other_connection(db_path: str, conversation_id: str) -> None:
    """第二条连接（独立 engine）立刻写一行：修复前这里抛 database is locked。

    timeout 刻意压到 0.2s：本用例要证的是「写锁没被占用」，不是「30s 后重试成功」，
    真被占用时必须在毫秒级暴露，而不是拖到 busy_timeout 才失败。
    """
    engine = create_async_engine(
        f"sqlite+aiosqlite:///{db_path}", connect_args={"timeout": 0.2}
    )
    try:
        Session = async_sessionmaker(bind=engine, expire_on_commit=False)
        async with Session() as session:
            session.add(
                AgentMessage(
                    conversation_id=conversation_id,
                    role="user",
                    content="并发写入（后台任务进度等价物）",
                )
            )
            await session.commit()
    finally:
        await engine.dispose()


@pytest.mark.anyio
async def test_llm_call_starts_without_open_transaction(db_session):
    """轮首 flush 了 step，但进入 LLM 调用前必须先 commit 释放写锁。"""
    conversation = await make_conversation(db_session)
    svc = make_service(db_session)
    observed: list[bool] = []
    install_call_round_recorder(svc, [answer("回答")], observed)

    events = [
        event
        async for event in svc.stream_chat(
            conversation_id=conversation.id, message="你好", page_context={"route": "/"}
        )
    ]

    assert observed == [False], (
        "进入 _call_round 时仍持有未提交事务 ⇒ LLM 调用期间（30~190s）"
        "全库写锁被独占（issue #222）"
    )
    assert any(event["type"] == "result" for event in events)


@pytest.mark.anyio
async def test_other_connection_can_write_while_llm_call_is_running(
    db_session, db_path
):
    """LLM 调用进行中，另一条连接必须能立刻写入（写锁已释放）。"""
    conversation = await make_conversation(db_session)
    svc = make_service(db_session)
    observed: list[bool] = []

    async def fake_call_round(
        *, prompt, system_prompt, force_answer, available_tools, tool_choice="auto"
    ):
        observed.append(svc.db.in_transaction())
        # 模拟「LLM 调用进行中」此刻的并发写：后台任务进度 / 计划行更新 / 取消操作
        await write_from_other_connection(db_path, conversation.id)
        return answer("回答")

    svc._call_round = fake_call_round

    events = [
        event
        async for event in svc.stream_chat(
            conversation_id=conversation.id, message="你好", page_context={"route": "/"}
        )
    ]

    assert any(event["type"] == "result" for event in events)


@pytest.mark.anyio
async def test_mcp_execution_starts_without_open_transaction(
    db_session, monkeypatch
):
    """MCP 工具执行是外部网络 IO：进入前同样必须先落盘释放写锁（issue #222 第二处）。"""
    conversation = await make_conversation(db_session)
    svc = make_service(db_session)
    svc.mcp_tools = [
        {
            "type": "function",
            "function": {
                "name": "lock_probe_mcp_read",
                "description": "回归用只读 MCP 假工具",
                "parameters": {"type": "object", "properties": {}},
            },
        }
    ]
    # 只读 MCP 工具免确认 ⇒ 走回合内直接执行分支（另一分支只落 preview）
    monkeypatch.setattr(pas, "mcp_tool_is_read_only", lambda metadata: True)
    observed: list[bool] = []

    async def fake_execute_mcp(user_id, name, arguments, record_id):
        observed.append(svc.db.in_transaction())
        return {"success": True, "message": "ok"}

    monkeypatch.setattr(pas, "execute_mcp_tool_call", fake_execute_mcp)
    round_observed: list[bool] = []
    install_call_round_recorder(
        svc, [tool_call("lock_probe_mcp_read"), answer("回答")], round_observed
    )

    events = [
        event
        async for event in svc.stream_chat(
            conversation_id=conversation.id,
            message="调用外部工具",
            page_context={"route": "/"},
        )
    ]

    assert observed == [False], (
        "进入 execute_mcp_tool_call 时仍持有未提交事务 ⇒ 外部网络 IO 期间写锁被独占"
    )
    assert any(event["type"] == "result" for event in events)


@pytest.mark.anyio
async def test_skill_branch_releases_write_lock_before_network_io(db_session, monkeypatch):
    """命中 skill 时：MCP 工具加载与窗口补测之前也必须先落盘（issue #222 第三处）。

    skill 分支在进 LLM 之前就 flush 了两个 step（thought + skill），随后
    `_prepare_mcp_tools`（缓存未命中 ⇒ 网络加载）与 `_history_budget_chars`
    （窗口无结论 ⇒ 同步补测）都是网络 IO，持写事务等它们即 #222 的失效形态。
    """
    conversation = await make_conversation(db_session)
    svc = make_service(db_session)
    observed: dict[str, bool] = {}

    from app.services import skill_loader

    monkeypatch.setattr(
        skill_loader,
        "get_skill_by_trigger",
        lambda message: {
            "template_key": "lock_probe_skill",
            "template_name": "回归用 Skill",
            "content": "回归用公开规则",
        },
    )

    async def fake_prepare_mcp(auto_mcp=True):
        observed["prepare_mcp_tools"] = svc.db.in_transaction()
        return []

    async def fake_history_budget(trace=None):
        observed["history_budget_chars"] = svc.db.in_transaction()
        return 60_000

    svc.ai_service._prepare_mcp_tools = fake_prepare_mcp
    svc._history_budget_chars = fake_history_budget
    round_observed: list[bool] = []
    install_call_round_recorder(svc, [answer("回答")], round_observed)

    events = [
        event
        async for event in svc.stream_chat(
            conversation_id=conversation.id,
            message="按 Skill 工作流处理",
            page_context={"route": "/"},
        )
    ]

    # 两个网络 IO 都被走到了，且都在写锁已释放之后
    assert observed == {
        "prepare_mcp_tools": False,
        "history_budget_chars": False,
    }, f"skill 分支在进入网络 IO 时仍持有未提交事务（issue #222）：{observed}"
    assert any(event["type"] == "result" for event in events)
    assert any(event.get("type") == "step_start" for event in events)


@pytest.mark.anyio
async def test_every_round_starts_without_open_transaction(db_session, monkeypatch):
    """多轮（工具轮 + 回答轮）同样：每一轮进入 LLM 调用都不得持有写事务。"""
    conversation = await make_conversation(db_session)
    svc = make_service(db_session)
    monkeypatch.setitem(
        svc.registry._tools,
        "lock_probe_read",
        ProjectAgentTool(
            "lock_probe_read", "回归用只读假工具", {"type": "object", "properties": {}}
        ),
    )

    async def fake_execute(name, arguments):
        return {"ok": True}

    monkeypatch.setattr(svc.registry, "execute", fake_execute)
    observed: list[bool] = []
    install_call_round_recorder(
        svc, [tool_call("lock_probe_read"), answer("回答")], observed
    )

    events = [
        event
        async for event in svc.stream_chat(
            conversation_id=conversation.id,
            message="查点东西",
            page_context={"route": "/"},
        )
    ]

    assert len(observed) == 2, "本用例要求先走一轮工具、再走一轮回答"
    assert observed == [False, False], (
        f"第 {observed.index(True) + 1} 轮进入 _call_round 时持有未提交事务（issue #222）"
    )
    assert any(event["type"] == "result" for event in events)
