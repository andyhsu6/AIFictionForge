"""护栏 1：SQLite 的 created_at 是秒级（server_default=CURRENT_TIMESTAMP），
同一秒写入的 assistant(tool_calls) / tool 行会在 ORDER BY created_at DESC 上并列，
_load_history 的 reversed() 因此无法还原插入序 ⇒ provider 的 tool_call 配对被打乱。
Postgres 侧 now() 是微秒级，不会复现 ⇒ 本测试必须建在 SQLite 上。
"""
import uuid
from types import SimpleNamespace

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.database import Base
from app.models.project import Project
from app.models.project_agent import AgentConversation, AgentMessage
from app.services.project_agent_service import ProjectAgentService


@pytest.fixture
async def db_session():
    db_path = f"/tmp/test_agent_order_{uuid.uuid4().hex}.db"
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


@pytest.mark.anyio
async def test_same_second_burst_keeps_insertion_order(db_session):
    db_session.add(Project(id="proj-1", user_id="test", title="p"))
    conv = AgentConversation(user_id="test", project_id="proj-1", title="t")
    db_session.add(conv)
    await db_session.flush()

    svc = ProjectAgentService(
        db=db_session, ai_service=SimpleNamespace(default_model="m"),
        project=Project(id="proj-1", user_id="test", title="p"), user_id="test",
    )
    burst = ["user", "assistant", "tool", "assistant", "tool", "user", "assistant", "tool"]
    for index, role in enumerate(burst):
        db_session.add(AgentMessage(
            conversation_id=conv.id, role=role, content=f"m{index}",
            tool_call_id=None if role != "tool" else f"call_{index}",
        ))
    await db_session.commit()

    history = await svc._load_history(conv.id)

    assert [m.content for m in history] == [f"m{i}" for i in range(len(burst))]
    stamps = [m.created_at for m in history]
    assert len(set(stamps)) == len(stamps), (
        "created_at 出现并列 ⇒ 排序依赖未定义行为；护栏 1 未生效"
    )
