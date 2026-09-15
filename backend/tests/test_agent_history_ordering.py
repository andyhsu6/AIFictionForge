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
import time
import uuid
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app import database as app_database
from app.api import project_agent as project_agent_api
from app.database import Base
from app.models.chapter import Chapter
from app.models.project import Project
from app.models.project_agent import (
    AgentConversation,
    AgentExecutionStep,
    AgentMessage,
    AgentToolCall,
    _naive_utc_now,
)
from app.services import agent_plan_runner as plan_runner
from app.services import project_agent_service as agent_service_module
from app.services.project_agent_service import ProjectAgentService
from support.agent_stubs import AgentAIServiceStub

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
        db=db_session, ai_service=AgentAIServiceStub(default_model="m"),
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


async def wait_past_second_tick(db, stamp: datetime) -> None:
    """等 SQLite 秒级 CURRENT_TIMESTAMP 跨过 stamp 所在秒（替代定长 sleep）。

    调用方必须传**刚 ORM 提交那一行的 created_at**（Python 侧 naive UTC，含微秒），
    不是提交前抓的 DB 时刻：只有这样"跨过了 stamp 所在秒"才等价于"下一条
    server_default 行的戳严格晚于 stamp"——定长 sleep(1.05) 只是在赌这个条件成立。
    CURRENT_TIMESTAMP 返回定长的 'YYYY-MM-DD HH:MM:SS' 字符串（实测 aiosqlite 原样
    返回字符串，秒精度），字典序 == 时间序，故直接比较字符串。
    """
    target = stamp.strftime("%Y-%m-%d %H:%M:%S")
    deadline = time.monotonic() + 3.0
    while True:
        now = str((await db.execute(text("SELECT CURRENT_TIMESTAMP"))).scalar_one())
        if now > target:   # 定长格式，字典序==时间序
            return
        if time.monotonic() > deadline:
            raise AssertionError(
                f"CURRENT_TIMESTAMP 3s 未跨过 {target} ⇒ 时钟异常；"
                "或者 stamp 根本不在 UTC 基准上（ORM 默认值被改成本地时间时会领先秒针整"
                " 8 小时，永远跨不过去）⇒ 先查 _naive_utc_now（护栏 1b/3）"
            )
        await asyncio.sleep(0.02)


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

    orm_row = AgentMessage(
        conversation_id=conv.id, role="user", content="orm-first")
    db_session.add(orm_row)
    await db_session.commit()

    # CURRENT_TIMESTAMP 只到秒，必须跨秒才能保证同基准下的先后无歧义。
    await wait_past_second_tick(db_session, orm_row.created_at)
    await insert_via_server_default(db_session, "agent_messages", {
        "id": str(uuid.uuid4()), "conversation_id": conv.id,
        "role": "assistant", "content": "server-default-second",
    })
    await db_session.commit()

    history = await ProjectAgentService(
        db=db_session, ai_service=AgentAIServiceStub(default_model="m"),
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

    orm_row = AgentToolCall(
        conversation_id=conv.id, user_id="test", project_id="proj-1",
        tool_name="orm_first", arguments={}, status="proposed",
    )
    db_session.add(orm_row)
    await db_session.commit()

    await wait_past_second_tick(db_session, orm_row.created_at)
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


# ---------------------------------------------------------------------------
# issue #67：AgentConversation.last_message_at / AgentToolCall.executed_at /
# confirmed_at / AgentExecutionStep.updated_at 也各有唯一的 DB 基准
# （server_default=func.now() ⇒ UTC），Python 侧写路径必须同基准。
#
# last_message_at 是会话列表的 ORDER BY 键（api/project_agent.py:180）；其余三列
# 是展示/审计字段。三种写入形态都要覆盖：ORM 属性赋值（service）、Core `.values()`
# 直写（api/runner）。#66 只锁了 created_at，本组用例锁剩下四列。
# ---------------------------------------------------------------------------

LOCAL_BASIS_SHIFT = timedelta(hours=8)


class _LocalBasisDatetime(datetime):
    """模拟"本机时区非 UTC"：无 tz 的 now()（本地墙钟）恒比真 UTC 快 LOCAL_BASIS_SHIFT。

    被测模块的 `datetime` 被 patch 成它之后，任何仍写 `datetime.now()` 的站点都会
    落本地基准（UTC+8，正是 issue 复现里实测的 +8h 偏差）；而 `_naive_utc_now()`
    定义在 models 模块、用的是未被 patch 的 datetime，仍落真 UTC ⇒ 两种基准的差值
    恒为 8h，与运行机器真实时区无关（CI 为 UTC 时也能稳定复现，不靠本机 CST）。
    """

    @classmethod
    def now(cls, tz=None):
        if tz is None:
            return datetime.now(timezone.utc).replace(tzinfo=None) + LOCAL_BASIS_SHIFT
        return datetime.now(tz)


def assert_naive_utc(stamped, column: str) -> None:
    """落值必须贴近 `_naive_utc_now()`；本地墙钟基准会差 8h ⇒ 红。"""
    assert stamped is not None, f"{column} 未写入 ⇒ 用例退化为空断言"
    delta = abs((stamped - _naive_utc_now()).total_seconds())
    assert delta <= BASIS_TOLERANCE_SECONDS, (
        f"{column} 与 naive UTC 相差 {delta}s（本机时区偏移应为 8h 量级）"
        " ⇒ 该列仍写本地墙钟，与自己的 server_default 混了两种基准"
    )


def install_answer_model(svc: ProjectAgentService) -> None:
    """把两个模型出口都换成直接给终答，驱动一条最简 stream_chat 回合。"""

    async def fake(**kwargs):
        return {
            "content": "收到。",
            "tool_calls": [],
            "usage": {"prompt_tokens": 1, "completion_tokens": 1},
        }

    svc.ai_service.generate_text = fake
    svc.ai_service.generate_text_stream_full = fake


def install_tool_then_answer_model(
    svc: ProjectAgentService, name: str, arguments: dict
) -> None:
    """第一个模型轮发一个工具调用，第二轮给终答；用于打 in-loop executed_at。"""
    responses = [
        {
            "content": "先调用工具。",
            "tool_calls": [
                {"id": "call_67", "function": {"name": name, "arguments": arguments}}
            ],
            "usage": {"prompt_tokens": 1, "completion_tokens": 1},
        },
        {
            "content": "看到了。",
            "tool_calls": [],
            "usage": {"prompt_tokens": 1, "completion_tokens": 1},
        },
    ]
    calls: list[dict] = []

    async def fake(**kwargs):
        calls.append(kwargs)
        return responses[min(len(calls) - 1, len(responses) - 1)]

    svc.ai_service.generate_text = fake
    svc.ai_service.generate_text_stream_full = fake


def make_service(db) -> ProjectAgentService:
    return ProjectAgentService(
        db=db, ai_service=AgentAIServiceStub(default_model="m"),
        project=Project(id="proj-1", user_id="test", title="p"), user_id="test",
    )


@pytest.fixture
async def db_factory():
    """runner 的 `_finalize_tool_call` 收的是 session_factory，不是 session。"""
    db_path = f"/tmp/test_agent_order_factory_{uuid.uuid4().hex}.db"
    engine = create_async_engine(f"sqlite+aiosqlite:///{db_path}")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield async_sessionmaker(bind=engine, expire_on_commit=False)
    await engine.dispose()
    import os
    if os.path.exists(db_path):
        os.remove(db_path)


@pytest.mark.anyio
async def test_stream_chat_writes_last_message_at_on_naive_utc_basis(
    db_session, monkeypatch
):
    """护栏 4：会话回合入口写 last_message_at 必须走 naive UTC。

    未打补丁时本机 CST 下 `datetime.now()` 与 `_naive_utc_now()` 差 8h；补丁把差
    距钉死为 8h，故即使在 UTC 机器上也稳定复现（issue #67 实测 03:15 vs 11:15）。
    """
    monkeypatch.setattr(
        agent_service_module, "datetime", _LocalBasisDatetime, raising=False
    )
    conv = await make_conversation(db_session)
    svc = make_service(db_session)
    install_answer_model(svc)

    events = [e async for e in svc.stream_chat(
        conversation_id=conv.id, message="你好",
        page_context={"route": "/project/proj-1"},
    )]
    assert any(e["type"] == "conversation" for e in events), "回合未跑起来 ⇒ 空断言"

    stamped = (await db_session.execute(
        select(AgentConversation.last_message_at).where(AgentConversation.id == conv.id)
    )).scalar_one()
    assert_naive_utc(stamped, "agent_conversations.last_message_at")


@pytest.mark.anyio
async def test_conversation_order_is_monotonic_across_write_paths(
    db_session, monkeypatch
):
    """护栏 4b：ORDER BY last_message_at DESC 不能因写入路径不同而颠倒。

    conv_python 由代码路径先写（真时刻 T）；等 SQLite 秒级 CURRENT_TIMESTAMP 跨过 T
    所在秒后，用 server_default 写更晚的 conv_db（真时刻 > T）。修复后 DESK 顺序是
    [conv_db, conv_python]；仍写本地基准时 conv_python 落 T+8h，排到更晚写入的
    conv_db 之前 ⇒ 本用例红。
    """
    monkeypatch.setattr(
        agent_service_module, "datetime", _LocalBasisDatetime, raising=False
    )
    conv_python = await make_conversation(db_session)
    svc = make_service(db_session)
    real_before = _naive_utc_now()
    await svc._save_assistant(conv_python, "reply", 0, 0)

    # 用**真 UTC** 时刻等待跨秒：即使被测站点写的是本地基准（T+8h），真墙钟照常前进。
    await wait_past_second_tick(db_session, real_before)
    db_default_id = str(uuid.uuid4())
    await insert_via_server_default(db_session, "agent_conversations", {
        "id": db_default_id, "user_id": "test", "project_id": "proj-1",
        "title": "db-default", "status": "active",
    })
    await db_session.commit()

    rows = list((await db_session.execute(
        select(AgentConversation)
        .where(
            AgentConversation.project_id == "proj-1",
            AgentConversation.user_id == "test",
        )
        .order_by(AgentConversation.last_message_at.desc())
    )).scalars().all())
    assert len(rows) == 2, f"只读回 {len(rows)} 行 ⇒ 未落到被测数据"
    assert rows[0].id == db_default_id, (
        "Python 写入路径的 last_message_at 仍是本地基准 ⇒ 它比后写、更晚的"
        " server_default 行还“新”，会话列表顺序颠倒"
    )
    stamps = [r.last_message_at for r in rows]
    assert stamps == sorted(stamps, reverse=True)


@pytest.mark.anyio
async def test_claim_tool_call_confirmed_at_written_on_naive_utc_basis(
    db_session, monkeypatch
):
    """护栏 5：`_claim_tool_call`（api Core .values() 直写）的 confirmed_at 必须 UTC。"""
    monkeypatch.setattr(
        project_agent_api, "datetime", _LocalBasisDatetime, raising=False
    )
    conv = await make_conversation(db_session)
    row = AgentToolCall(
        conversation_id=conv.id, user_id="test", project_id="proj-1",
        tool_name="update_chapter", arguments={}, status="waiting_confirmation",
        requires_confirmation=True,
    )
    db_session.add(row)
    await db_session.commit()

    claimed = await project_agent_api._claim_tool_call(
        db_session, tool_call_id=row.id, project_id="proj-1",
        user_id="test", claimed_status="executing",
    )
    assert claimed.status == "executing"
    assert_naive_utc(claimed.confirmed_at, "agent_tool_calls.confirmed_at")


@pytest.mark.anyio
async def test_finalize_tool_call_executed_at_written_on_naive_utc_basis(
    db_factory, monkeypatch
):
    """护栏 6：runner `_finalize_tool_call`（计划收尾 Core 直写）的 executed_at 必须 UTC。"""
    monkeypatch.setattr(
        plan_runner, "datetime", _LocalBasisDatetime, raising=False
    )
    async with db_factory() as db:
        db.add(Project(id="proj-1", user_id="test", title="p"))
        conv = AgentConversation(user_id="test", project_id="proj-1", title="t")
        db.add(conv)
        await db.flush()
        row = AgentToolCall(
            conversation_id=conv.id, user_id="test", project_id="proj-1",
            tool_name="propose_plan", arguments={}, status="executing",
        )
        db.add(row)
        await db.commit()
        row_id = row.id

    finalized = await plan_runner._finalize_tool_call(
        db_factory, row_id, status="executed", result={}, error_message=None,
    )
    assert finalized is True, "收尾未命中 executing 行 ⇒ 空断言"
    async with db_factory() as db:
        stamped = (await db.execute(
            select(AgentToolCall.executed_at).where(AgentToolCall.id == row_id)
        )).scalar_one()
    assert_naive_utc(stamped, "agent_tool_calls.executed_at")


@pytest.mark.anyio
async def test_in_loop_executed_at_written_on_naive_utc_basis(db_session, monkeypatch):
    """护栏 6b：工具循环内执行工具（service ORM 赋值）的 executed_at 必须 UTC。"""
    monkeypatch.setattr(
        agent_service_module, "datetime", _LocalBasisDatetime, raising=False
    )
    conv = await make_conversation(db_session)
    db_session.add(Chapter(
        project_id="proj-1", chapter_number=1, title="c1", content="body",
    ))
    await db_session.flush()
    svc = make_service(db_session)
    install_tool_then_answer_model(
        svc, "get_chapter_detail", {"chapter_number": 1, "include_content": True}
    )

    _ = [e async for e in svc.stream_chat(
        conversation_id=conv.id, message="看下第 1 章",
        page_context={"route": "/project/proj-1"},
    )]
    row = (await db_session.execute(select(AgentToolCall))).scalars().one()
    assert row.status == "executed", f"工具未执行（status={row.status}）⇒ 空断言"
    assert_naive_utc(row.executed_at, "agent_tool_calls.executed_at")


@pytest.mark.anyio
async def test_execution_step_updated_at_written_on_naive_utc_basis(
    db_session, monkeypatch
):
    """护栏 7：`_update_step`（service ORM 赋值）的 updated_at 必须 UTC。

    该列还有 server_default/onupdate=func.now()；这里保留显式写入（见 _update_step
    注释：避免 onupdate 让属性过期、SSE 序列化触发 AsyncSession 隐式 IO），只换基准。
    """
    monkeypatch.setattr(
        agent_service_module, "datetime", _LocalBasisDatetime, raising=False
    )
    conv = await make_conversation(db_session)
    step = AgentExecutionStep(
        conversation_id=conv.id, step_type="tool", category="project",
        title="t", status="running",
    )
    db_session.add(step)
    await db_session.commit()

    svc = make_service(db_session)
    await svc._update_step(step, status="completed", content="done")
    await db_session.commit()
    assert_naive_utc(step.updated_at, "agent_execution_steps.updated_at")
