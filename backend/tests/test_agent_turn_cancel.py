"""创作助手回合取消 / 卡死自愈回归（issue #257）。

锁的四条腿：
1. 显式取消会让在飞的模型调用立刻中断（不是等它自然返回），回合以
   `AgentTurnCancelled` 受控收口；
2. 单回合墙钟预算耗尽走 `AgentTurnTimeout`，不再出现 20+ 分钟；
3. `finalize_interrupted_turn` 在 session 已坏 / ORM 对象已过期时**绝不抛出**，
   且不再依赖读过期对象拿主键（旧实现的 PendingRollbackError 就是从这里来的）；
4. `reconcile_stale_turns` 把不属于任何运行中回合的 running 步骤收尾，并且
   不会误伤正在跑的回合。
"""
import asyncio
import uuid

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.database import Base
from app.models.project import Project
from app.models.project_agent import (
    AgentConversation,
    AgentExecutionStep,
    AgentMessage,
)
from app.services import agent_turn_registry
from app.services.project_agent_service import (
    STALE_TURN_MESSAGE,
    AgentTurnCancelled,
    AgentTurnTimeout,
    ProjectAgentService,
    reconcile_stale_turns,
)
from support.agent_stubs import AgentAIServiceStub


@pytest.fixture(autouse=True)
def clean_turn_registry():
    agent_turn_registry.reset_for_tests()
    yield
    agent_turn_registry.reset_for_tests()


@pytest.fixture
async def db_session():
    db_path = f"/tmp/test_agent_turn_cancel_{uuid.uuid4().hex}.db"
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


async def _make_conversation(db) -> AgentConversation:
    db.add(Project(id="proj-1", user_id="test", title="测试项目"))
    conversation = AgentConversation(user_id="test", project_id="proj-1", title="取消回归")
    db.add(conversation)
    await db.flush()
    return conversation


def _make_service(db) -> ProjectAgentService:
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


def _slow_model(svc: ProjectAgentService, seconds: float = 30.0) -> None:
    async def fake_generate_text(**kwargs):
        await asyncio.sleep(seconds)
        return {"content": "迟到的回答", "tool_calls": [], "usage": {}}

    async def fake_stream_full(**kwargs):
        await asyncio.sleep(seconds)
        return {"content": "迟到的回答", "tool_calls": [], "usage": {}}

    svc.ai_service.generate_text = fake_generate_text
    svc.ai_service.generate_text_stream_full = fake_stream_full


async def _drain(svc: ProjectAgentService, conversation_id: str) -> None:
    async for _event in svc.stream_chat(
        conversation_id=conversation_id,
        message="帮我查一下资料",
        page_context={"route": "/project/1"},
    ):
        pass


async def _running_steps(db, conversation_id: str) -> list[AgentExecutionStep]:
    return list((await db.execute(
        select(AgentExecutionStep).where(
            AgentExecutionStep.conversation_id == conversation_id,
            AgentExecutionStep.status == "running",
        )
    )).scalars().all())


async def _assistant_contents(db, conversation_id: str) -> list[str]:
    rows = list((await db.execute(
        select(AgentMessage).where(
            AgentMessage.conversation_id == conversation_id,
            AgentMessage.role == "assistant",
        )
    )).scalars().all())
    return [row.content for row in rows]


@pytest.mark.anyio
async def test_cancel_interrupts_inflight_model_call_and_finalizes(db_session):
    """取消必须中断在飞的模型 await，收尾后不留 running 步骤。"""
    conversation = await _make_conversation(db_session)
    svc = _make_service(db_session)
    _slow_model(svc, seconds=30)

    task = asyncio.create_task(_drain(svc, conversation.id))
    # 等回合登记并进入模型调用（thought 步骤已 commit，故 running 行可见）。
    for _ in range(100):
        await asyncio.sleep(0.02)
        if agent_turn_registry.is_active(conversation.id) and await _running_steps(
            db_session, conversation.id
        ):
            break
    assert agent_turn_registry.is_active(conversation.id), "回合未登记 ⇒ 取消接口找不到目标"
    assert await _running_steps(db_session, conversation.id), "前置数据未落出 running 步骤"

    handle = agent_turn_registry.request_cancel(conversation.id)
    assert handle is not None

    with pytest.raises(AgentTurnCancelled):
        await asyncio.wait_for(task, timeout=5)

    # 异常退出 ⇒ 会话进入「收尾中」：此时卡死自愈不得动手（否则会把正在收尾的
    # running 行误写成「服务重启/连接异常」）。
    assert agent_turn_registry.is_settling(conversation.id), "退出的回合未标记为收尾中"
    assert await reconcile_stale_turns(
        db_session, conversation_id=conversation.id
    ) == 0, "收尾进行中的会话被自愈误判为卡死"

    # 路由在 AgentTurnCancelled 分支里做的事：收尾成「已停止」。
    await svc.finalize_interrupted_turn("用户停止", cancelled=True)
    assert await _running_steps(db_session, conversation.id) == []
    assert "本次执行已由用户停止。" in await _assistant_contents(db_session, conversation.id)
    assert not agent_turn_registry.is_active(conversation.id), "取消后登记未注销"

    # 收尾落盘后才结算：取消接口等的就是这个事件。
    assert not handle.finished_event.is_set()
    svc.mark_turn_settled()
    assert handle.finished_event.is_set()
    assert not agent_turn_registry.is_settling(conversation.id)


@pytest.mark.anyio
async def test_turn_budget_exhaustion_raises_timeout(db_session, monkeypatch):
    """单回合预算耗尽必须抛 AgentTurnTimeout，而不是继续发模型请求。"""
    conversation = await _make_conversation(db_session)
    svc = _make_service(db_session)
    _slow_model(svc, seconds=30)
    monkeypatch.setattr(
        ProjectAgentService, "_turn_timeout_seconds", lambda self: 0.001
    )

    with pytest.raises(AgentTurnTimeout):
        await asyncio.wait_for(_drain(svc, conversation.id), timeout=5)

    await svc.finalize_interrupted_turn(
        "单回合超时", cancelled=True, content="本次执行已超过 5 分钟上限，已自动停止。"
    )
    assert await _running_steps(db_session, conversation.id) == []
    assert any(
        "已超过" in content and "自动停止" in content
        for content in await _assistant_contents(db_session, conversation.id)
    )


@pytest.mark.anyio
async def test_finalize_does_not_read_expired_orm_object(db_session):
    """收尾只认回合开始时抄下的主键：过期 ORM 对象一律不碰（#257 根因）。"""

    class _ExpiredConversation:
        @property
        def id(self):  # pragma: no cover - 被访问即失败
            raise RuntimeError("expired attribute access must not happen in finalize")

    conversation = await _make_conversation(db_session)
    user_message = AgentMessage(
        conversation_id=conversation.id, role="user", content="问题"
    )
    db_session.add(user_message)
    await db_session.flush()
    await db_session.commit()

    svc = _make_service(db_session)
    svc._active_conversation = _ExpiredConversation()  # type: ignore[assignment]
    svc._active_conversation_id = conversation.id
    svc._active_user_message_id = user_message.id

    await svc.finalize_interrupted_turn("boom", cancelled=False)
    assert any(
        "本次执行因请求失败而中止" in content
        for content in await _assistant_contents(db_session, conversation.id)
    )


@pytest.mark.anyio
async def test_finalize_never_raises_on_broken_session(db_session):
    """session 已进入 PendingRollback 时，收尾也不能把错误处理再炸一次。"""
    conversation = await _make_conversation(db_session)
    user_message = AgentMessage(
        conversation_id=conversation.id, role="user", content="问题"
    )
    db_session.add(user_message)
    await db_session.commit()

    svc = _make_service(db_session)
    svc._active_conversation_id = conversation.id
    svc._active_user_message_id = user_message.id

    # 人为把 session 打成「必须 rollback 才能继续」的状态；不手动 rollback，
    # 让 finalize 自己的第一步回滚来恢复——这正是 #257 里它缺的那一步。
    from sqlalchemy.exc import IntegrityError

    with pytest.raises(IntegrityError):
        await db_session.execute(
            AgentMessage.__table__.insert().values(id=None, role=None)
        )
        await db_session.flush()

    await svc.finalize_interrupted_turn("database is locked", cancelled=False)
    # 不只是「不抛」：收尾还必须真的写出可见终止消息（旧实现这里是二次崩溃）。
    assert any(
        "本次执行因请求失败而中止" in content
        for content in await _assistant_contents(db_session, conversation.id)
    )


@pytest.mark.anyio
async def test_reconcile_marks_orphan_running_steps_failed(db_session):
    """没有运行中回合时，打开会话必须把残留 running 步骤收尾。"""
    conversation = await _make_conversation(db_session)
    user_message = AgentMessage(
        conversation_id=conversation.id, role="user", content="一直查资料"
    )
    db_session.add(user_message)
    await db_session.flush()
    step = AgentExecutionStep(
        conversation_id=conversation.id,
        user_message_id=user_message.id,
        sequence=0,
        step_type="thought",
        category="analysis",
        title="分析第 3 步",
        content="正在判断是否需要读取项目数据……",
        status="running",
    )
    db_session.add(step)
    await db_session.commit()

    repaired = await reconcile_stale_turns(db_session, conversation_id=conversation.id)
    assert repaired == 1
    await db_session.refresh(step)
    assert step.status == "failed"
    assert step.content == STALE_TURN_MESSAGE
    assert step.assistant_message_id, "孤儿步骤必须绑到一条可见的终止消息"
    assert STALE_TURN_MESSAGE in await _assistant_contents(db_session, conversation.id)

    # 幂等：第二次没有 running 行可修。
    assert await reconcile_stale_turns(db_session, conversation_id=conversation.id) == 0


@pytest.mark.anyio
async def test_reconcile_skips_conversation_with_active_turn(db_session):
    """正在跑的回合不能被自愈误判成卡死。"""
    conversation = await _make_conversation(db_session)
    user_message = AgentMessage(
        conversation_id=conversation.id, role="user", content="问题"
    )
    db_session.add(user_message)
    await db_session.flush()
    step = AgentExecutionStep(
        conversation_id=conversation.id,
        user_message_id=user_message.id,
        sequence=0,
        step_type="thought",
        category="analysis",
        title="分析第 1 步",
        content="进行中",
        status="running",
    )
    db_session.add(step)
    await db_session.commit()

    handle = agent_turn_registry.begin_turn(conversation.id)
    try:
        assert await reconcile_stale_turns(db_session, conversation_id=conversation.id) == 0
        await db_session.refresh(step)
        assert step.status == "running"
    finally:
        agent_turn_registry.finish_turn(handle)


@pytest.mark.anyio
async def test_finalize_retries_once_after_database_locked(db_session, monkeypatch):
    """收尾流程允许因 SQLite 写锁冲突整段重试一次（#257 现场命中点）。"""
    from sqlalchemy.exc import OperationalError

    conversation = await _make_conversation(db_session)
    user_message = AgentMessage(
        conversation_id=conversation.id, role="user", content="问题"
    )
    db_session.add(user_message)
    await db_session.commit()

    svc = _make_service(db_session)
    svc._active_conversation_id = conversation.id
    svc._active_user_message_id = user_message.id

    original = svc._apply_interrupted_turn
    calls = {"n": 0}

    async def flaky(**kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            raise OperationalError(
                "UPDATE agent_execution_steps", {}, Exception("database is locked")
            )
        await original(**kwargs)

    monkeypatch.setattr(svc, "_apply_interrupted_turn", flaky)
    await svc.finalize_interrupted_turn("database is locked", cancelled=False)
    # 不只是「不抛」：收尾还必须真的写出可见终止消息（旧实现这里是二次崩溃）。
    assert any(
        "本次执行因请求失败而中止" in content
        for content in await _assistant_contents(db_session, conversation.id)
    )

    assert calls["n"] == 2, "写锁冲突没有触发一次整段重试"
    assert any(
        "本次执行因请求失败而中止" in content
        for content in await _assistant_contents(db_session, conversation.id)
    )


@pytest.mark.anyio
async def test_finalize_swallows_persistent_database_locked(db_session, monkeypatch):
    """重试仍失败只能放弃：收尾绝不能把异常再抛回路由。"""
    from sqlalchemy.exc import OperationalError

    conversation = await _make_conversation(db_session)
    user_message = AgentMessage(
        conversation_id=conversation.id, role="user", content="问题"
    )
    db_session.add(user_message)
    await db_session.commit()

    svc = _make_service(db_session)
    svc._active_conversation_id = conversation.id
    svc._active_user_message_id = user_message.id

    calls = {"n": 0}

    async def always_locked(**kwargs):
        calls["n"] += 1
        raise OperationalError(
            "UPDATE agent_execution_steps", {}, Exception("database is locked")
        )

    monkeypatch.setattr(svc, "_apply_interrupted_turn", always_locked)
    await svc.finalize_interrupted_turn("database is locked", cancelled=False)
    assert calls["n"] == 2, "重试次数应被限制为一次"


@pytest.mark.anyio
async def test_detached_finalize_uses_independent_session(monkeypatch):
    """连接断开路径的收尾不能依赖请求会话：独立会话也要写成终态。"""
    import app.database as database_module
    from app.models.project_agent import AgentConversation, AgentMessage
    from app.services.project_agent_service import finalize_interrupted_turn_detached
    from sqlalchemy.ext.asyncio import async_sessionmaker

    db_path = f"/tmp/test_agent_turn_detached_{uuid.uuid4().hex}.db"
    engine = create_async_engine(f"sqlite+aiosqlite:///{db_path}")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(bind=engine, expire_on_commit=False)

    conversation_id = "conv-detached"
    user_message_id = "um-detached"
    async with factory() as session:
        session.add(Project(id="proj-1", user_id="test", title="测试项目"))
        session.add(AgentConversation(
            id=conversation_id, user_id="test", project_id="proj-1", title="断线"
        ))
        session.add(AgentMessage(
            id=user_message_id, conversation_id=conversation_id, role="user", content="查资料"
        ))
        session.add(AgentExecutionStep(
            conversation_id=conversation_id,
            user_message_id=user_message_id,
            sequence=0,
            step_type="thought",
            category="analysis",
            title="分析第 1 步",
            content="进行中",
            status="running",
        ))
        await session.commit()

    async def fake_get_engine(user_id: str):
        return engine

    monkeypatch.setattr(database_module, "get_engine", fake_get_engine)

    await finalize_interrupted_turn_detached(
        user_id="test",
        conversation_id=conversation_id,
        user_message_id=user_message_id,
        reason="连接已中断",
        cancelled=True,
    )

    async with factory() as session:
        step = (await session.execute(
            select(AgentExecutionStep).where(
                AgentExecutionStep.conversation_id == conversation_id
            )
        )).scalars().one()
        assistants = list((await session.execute(
            select(AgentMessage).where(
                AgentMessage.conversation_id == conversation_id,
                AgentMessage.role == "assistant",
            )
        )).scalars().all())
    assert step.status == "cancelled"
    assert step.assistant_message_id
    assert assistants and assistants[0].content == "本次执行已由用户停止。"

    await engine.dispose()
    import os

    if os.path.exists(db_path):
        os.remove(db_path)

@pytest.mark.anyio
async def test_settling_gate_blocks_reconcile_until_finalize_lands(db_session):
    """「已停止、收尾未落盘」的窗口内，卡死自愈不能把 running 行判成脏数据。"""
    conversation = await _make_conversation(db_session)
    user_message = AgentMessage(
        conversation_id=conversation.id, role="user", content="问题"
    )
    db_session.add(user_message)
    await db_session.flush()
    step = AgentExecutionStep(
        conversation_id=conversation.id,
        user_message_id=user_message.id,
        sequence=0,
        step_type="thought",
        category="analysis",
        title="分析第 1 步",
        content="进行中",
        status="running",
    )
    db_session.add(step)
    await db_session.commit()

    handle = agent_turn_registry.begin_turn(conversation.id)
    agent_turn_registry.finish_turn(handle, settling=True)
    try:
        assert await reconcile_stale_turns(
            db_session, conversation_id=conversation.id
        ) == 0, "收尾中的会话被自愈误判为卡死"
        await db_session.refresh(step)
        assert step.status == "running"
    finally:
        agent_turn_registry.mark_settled(handle)

    # 收尾落盘后：残留 running 行才允许被自愈收口。
    assert await reconcile_stale_turns(
        db_session, conversation_id=conversation.id
    ) == 1


@pytest.mark.anyio
async def test_await_turn_cancellable_interrupts_pending_await(db_session):
    """取消必须中断在飞的 await 本身，而不是等它自然返回。"""
    svc = _make_service(db_session)
    handle = agent_turn_registry.begin_turn("conv-await")
    svc._turn_handle = handle
    try:
        task = asyncio.create_task(
            svc._await_turn_cancellable(asyncio.sleep(30))
        )
        await asyncio.sleep(0.05)
        agent_turn_registry.request_cancel("conv-await")
        with pytest.raises(AgentTurnCancelled):
            await asyncio.wait_for(task, timeout=3)
    finally:
        agent_turn_registry.finish_turn(handle)


@pytest.mark.anyio
async def test_turn_budget_expires_mid_flight(db_session, monkeypatch):
    """预算在模型调用进行中到期：走 asyncio.timeout 改判为回合超时。"""
    conversation = await _make_conversation(db_session)
    svc = _make_service(db_session)
    _slow_model(svc, seconds=10)
    monkeypatch.setattr(ProjectAgentService, "MIN_ROUND_SECONDS", 0.0)
    monkeypatch.setattr(
        ProjectAgentService, "_turn_timeout_seconds", lambda self: 2.0
    )

    with pytest.raises(AgentTurnTimeout):
        await asyncio.wait_for(_drain(svc, conversation.id), timeout=8)

