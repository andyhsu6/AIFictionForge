"""护栏 1：SQLite 的 created_at 是秒级（server_default=CURRENT_TIMESTAMP），
同一秒写入的 assistant(tool_calls) / tool 行会在 ORDER BY created_at DESC 上并列，
_load_history 的 reversed() 因此无法还原插入序 ⇒ provider 的 tool_call 配对被打乱。
Postgres 侧 now() 是微秒级，不会复现**并列**（但"基准"问题另见护栏 3）⇒ 并列用例必须建在 SQLite 上。

护栏 1b（基准）：SQLite 的 CURRENT_TIMESTAMP 是 **UTC**，本机 CST 下与 datetime.now()
实测相差整 8 小时。因此任何补上来的 Python 侧 default 都必须与 server_default 同基准
（naive UTC），否则同一列混两种基准：改前的行是 UTC、改后的行是本地时间 ⇒
排序按 UTC/本地混排直接颠倒（正时区下"后写的 ORM 行看起来更早"，负时区下反向），
且 API/前端把 naive 时间戳按本地解释时整体偏移。跨基准用例见
test_orm_default_and_server_default_share_one_time_basis。

护栏 3（PG 侧基准）：app/config.py:19 的 DATABASE_URL 默认值是 postgres，而 PG 迁移
（alembic/postgres/versions/20260817_1700_7c1a9e4b2d10:29,46）在
`timestamp without time zone` 列上写 now() ⇒ 落值随 **session TimeZone** 漂移
（now() 是 timestamptz，投给无时区列时按会话时区本地化）。Python 侧 default 加入之前
两条写路径都走 now() ⇒ 恒单基准；加入之后 ORM 写 = naive UTC、server_default 写 =
会话时区 ⇒ 非 UTC 会话下正好复现护栏 1b 描述的"颠倒排序"。因此 PG 引擎构造时必须把
session TimeZone 钉成 UTC（app/database.py 的 connect_args.server_settings），用例见
test_postgres_engine_pins_session_timezone_to_utc（不连真库，断言构造参数）。
"""
import asyncio
import uuid
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app import database as app_database
from app.database import Base
from app.models.project import Project
from app.models.project_agent import AgentConversation, AgentMessage, AgentToolCall
from app.services.project_agent_service import ProjectAgentService

# 同一瞬间的两种写入路径允许漂移的秒数上限；远超它即视为基准不一致（8h = 28800s）。
BASIS_TOLERANCE_SECONDS = 60


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


async def insert_via_server_default(db, table: str, columns: dict) -> None:
    """绕过 ORM 默认值写一行，让列的 server_default=func.now() 真正生效。

    这正是本次改动之前所有行的来源（alembic 建的表只有 CURRENT_TIMESTAMP），
    也是裸 SQL / 历史数据的代表。列清单里刻意不含 created_at。
    """
    cols = ", ".join(columns)
    binds = ", ".join(f":{name}" for name in columns)
    await db.execute(text(f"INSERT INTO {table} ({cols}) VALUES ({binds})"), columns)


async def make_conversation(db) -> AgentConversation:
    db.add(Project(id="proj-1", user_id="test", title="p"))
    conv = AgentConversation(user_id="test", project_id="proj-1", title="t")
    db.add(conv)
    await db.flush()
    return conv


@pytest.mark.anyio
async def test_orm_default_and_server_default_share_one_time_basis(db_session):
    """护栏 1b：ORM Python 侧默认值必须与 server_default(SQLite CURRENT_TIMESTAMP=UTC) 同基准。

    default=datetime.now（本地时间）时，本机实测与 server_default 相差整 8 小时：
    先写的 ORM 行拿到 +8h 的戳、后写的 server_default 行拿到真 UTC 戳 ⇒
    后写的行"看起来更早"，_load_history 排序颠倒；且 utcnow 的绝对校验也红。
    """
    conv = await make_conversation(db_session)

    db_session.add(AgentMessage(
        conversation_id=conv.id, role="user", content="orm-first"))
    await db_session.commit()

    # CURRENT_TIMESTAMP 只到秒，必须跨秒才能保证同基准下的先后无歧义。
    await asyncio.sleep(1.05)
    await insert_via_server_default(db_session, "agent_messages", {
        "id": str(uuid.uuid4()), "conversation_id": conv.id,
        "role": "assistant", "content": "server-default-second",
    })
    await db_session.commit()

    history = await ProjectAgentService(
        db=db_session, ai_service=SimpleNamespace(default_model="m"),
        project=Project(id="proj-1", user_id="test", title="p"), user_id="test",
    )._load_history(conv.id)
    by_content = {m.content: m.created_at for m in history}

    assert [m.content for m in history] == [
        "orm-first", "server-default-second"], (
        "同一列混了两种时间基准 ⇒ 后写入的 server_default 行排到了 ORM 行之前"
    )
    delta = abs((by_content["server-default-second"]
                 - by_content["orm-first"]).total_seconds())
    assert delta <= BASIS_TOLERANCE_SECONDS, (
        f"ORM 默认值与 server_default 相差 {delta}s（本机时区偏移应为 28800s 量级）"
        " ⇒ Python 侧默认值没有走 naive UTC"
    )
    utc_now = datetime.now(timezone.utc).replace(tzinfo=None)
    assert abs((by_content["orm-first"] - utc_now).total_seconds()) <= BASIS_TOLERANCE_SECONDS, (
        "ORM 写入的 created_at 与真实 UTC 时刻不吻合 ⇒ 落的是本地时间"
    )


@pytest.mark.anyio
async def test_tool_call_burst_has_no_created_at_tie(db_session):
    """护栏 2（AgentToolCall）：api/project_agent.py:208 以 ORDER BY created_at 读工具调用，
    前端 ProjectAgentPanel 依该数组顺序批量批准 waiting_confirmation 工具 ⇒ 顺序有副作用。
    纯 server_default 在同秒插入时全部并列 ⇒ 排序退化为未定义行为。
    """
    conv = await make_conversation(db_session)
    for index in range(3):
        db_session.add(AgentToolCall(
            conversation_id=conv.id, user_id="test", project_id="proj-1",
            tool_name=f"tool_{index}", arguments={}, status="proposed",
        ))
    await db_session.commit()

    rows = list((await db_session.execute(
        select(AgentToolCall)
        .where(AgentToolCall.conversation_id == conv.id)
        .order_by(AgentToolCall.created_at)
    )).scalars().all())
    # 必须有这一条：rows 为空时下面两个断言（集合无重复 / 已排序）**双双恒真** ⇒ 空覆盖。
    assert len(rows) == 3, f"只读回 {len(rows)} 行 ⇒ 未落到被测数据，本用例退化为空断言"

    stamps = [r.created_at for r in rows]
    assert len(set(stamps)) == len(stamps), (
        f"agent_tool_calls.created_at 同秒并列 {stamps}"
        " ⇒ ORDER BY created_at 的顺序未定义"
    )
    assert stamps == sorted(stamps)


@pytest.mark.anyio
async def test_tool_call_orm_default_matches_server_default_basis(db_session):
    """护栏 2b：AgentToolCall 的 Python 侧默认值同样必须是 naive UTC 基准。

    与护栏 1b 同型：若这里被复制成 default=datetime.now，后写入的 server_default 行
    会在 ORDER BY created_at 上插到 ORM 行之前。
    """
    conv = await make_conversation(db_session)

    db_session.add(AgentToolCall(
        conversation_id=conv.id, user_id="test", project_id="proj-1",
        tool_name="orm_first", arguments={}, status="proposed",
    ))
    await db_session.commit()

    await asyncio.sleep(1.05)
    await insert_via_server_default(db_session, "agent_tool_calls", {
        "id": str(uuid.uuid4()), "conversation_id": conv.id, "user_id": "test",
        "project_id": "proj-1", "tool_name": "sql_second", "arguments": "{}",
        "risk_level": 0, "requires_confirmation": 0, "status": "proposed",
    })
    await db_session.commit()

    rows = list((await db_session.execute(
        select(AgentToolCall)
        .where(AgentToolCall.conversation_id == conv.id)
        .order_by(AgentToolCall.created_at)
    )).scalars().all())
    assert [r.tool_name for r in rows] == ["orm_first", "sql_second"], (
        "agent_tool_calls 同列混基准 ⇒ ORDER BY created_at 排序颠倒"
    )
    delta = abs((rows[1].created_at - rows[0].created_at).total_seconds())
    assert delta <= BASIS_TOLERANCE_SECONDS, (
        f"ORM 默认值与 server_default 相差 {delta}s ⇒ 未走 naive UTC"
    )


# app/database.py 的 PG 分支是仓库里**唯一**带 server_settings 的引擎构造点；
# scripts/cleanup_book_import_data.py:267 也构造引擎，但走的是另一份参数（无
# server_settings，且该脚本只做 UPDATE/DELETE、不写入任何 created_at 列）。
PG_DATABASE_URL = "postgresql+asyncpg://user:pass@localhost:5432/aistoryforge"


@pytest.mark.anyio
async def test_postgres_engine_pins_session_timezone_to_utc(monkeypatch):
    """护栏 3：PG 引擎构造路径必须把 session TimeZone 钉成 UTC。

    不连真库：直接捕获 create_async_engine 的入参。改前 server_settings 只有
    application_name/jit/search_path ⇒ 本用例红；钉上 TimeZone=UTC 后绿。
    去掉该键 ⇒ 本用例必须重新变红（这是它的可证伪性）。
    """
    captured: dict = {}

    def fake_create_async_engine(url, **kwargs):
        captured["url"] = url
        captured.update(kwargs)
        return SimpleNamespace(url=url)

    monkeypatch.setattr(app_database, "create_async_engine", fake_create_async_engine)
    monkeypatch.setattr(app_database.settings, "database_url", PG_DATABASE_URL)
    # 引擎缓存置空，确保真的走到构造分支（monkeypatch 结束后自动还原）。
    monkeypatch.setattr(app_database, "_engine_cache", {})

    await app_database.get_engine("tz-guard-user")

    assert captured.get("url") == PG_DATABASE_URL, (
        "PG 分支根本没有构造引擎 ⇒ 本用例为空断言"
    )
    server_settings = captured["connect_args"]["server_settings"]
    assert server_settings.get("TimeZone") == "UTC", (
        f"PG session TimeZone 未钉定（server_settings={sorted(server_settings)}）"
        " ⇒ now() 按会话时区落值，与 ORM 侧 naive UTC 默认值构成双基准，"
        "ORDER BY created_at 在非 UTC 会话下颠倒排序（护栏 3 失效）"
    )
