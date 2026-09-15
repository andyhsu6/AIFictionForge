"""Issue #120：background_tasks 的时间列必须全部落在同一基准（naive UTC）。

created_at/updated_at 的 server_default=func.now() 是 UTC（PG 由 database.py 钉定
session TimeZone，SQLite 的 CURRENT_TIMESTAMP 恒 UTC）。计划 runner 的收尾直接 UPDATE
曾用 datetime.now()（本地墙钟）写 started_at/completed_at，实测同一行 created_at(UTC)
与 completed_at(本地) 相差整 8 小时。这里沿用 test_agent_history_ordering 的判据：
本机 CST 下本地墙钟领先 UTC 28800s，远超 60s 容差。
"""
import os
import uuid
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.database import Base
from app.models.background_task import BackgroundTask
from app.models.project import Project
from app.services import background_task_service as bts_module
from app.services.agent_plan_runner import _write_plan_row
from app.services.background_task_service import BackgroundTaskService, TaskProgressTracker
from app.services.project_agent_operational_tools import ProjectAgentOperationalTools

BASIS_TOLERANCE_SECONDS = 60


def _naive_utc_now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


@pytest.fixture
async def factory():
    db_path = f"/tmp/test_bg_task_ts_{uuid.uuid4().hex}.db"
    engine = create_async_engine(f"sqlite+aiosqlite:///{db_path}")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield async_sessionmaker(bind=engine, class_=AsyncSession, expire_on_commit=False)
    await engine.dispose()
    if os.path.exists(db_path):
        os.remove(db_path)


async def _seed_task(factory, **overrides) -> str:
    kwargs = {
        "id": uuid.uuid4().hex,
        "user_id": "u-1",
        "project_id": "p-1",
        "task_type": "chapter_generate",
        "status": "pending",
        "progress": 0,
    }
    kwargs.update(overrides)
    async with factory() as session:
        session.add(BackgroundTask(**kwargs))
        await session.commit()
    return kwargs["id"]


async def _load(factory, task_id: str) -> BackgroundTask:
    async with factory() as session:
        return await session.get(BackgroundTask, task_id)


def _assert_naive_utc_near(stamp, reference, column: str) -> None:
    assert stamp is not None, f"{column} 未写入"
    assert stamp.tzinfo is None, f"{column} 不应带 tzinfo"
    drift = abs((stamp - reference).total_seconds())
    assert drift <= BASIS_TOLERANCE_SECONDS, (
        f"{column}={stamp} 与真实 UTC 相差 {drift:.0f}s（本机时区偏移应为 28800s 量级）"
        " ⇒ 落的是本地墙钟，未走 naive UTC"
    )


@pytest.mark.anyio
async def test_write_plan_row_stamps_share_naive_utc_basis(factory):
    """计划 runner 收尾直接 UPDATE 写出的三个戳必须都是 naive UTC。"""
    task_id = await _seed_task(factory, task_type="agent_plan", status="pending")

    await _write_plan_row(
        factory, task_id,
        status="running", progress=10,
        status_message="已开始", started=True, completed=True,
    )
    reference = _naive_utc_now()

    row = await _load(factory, task_id)
    for column in ("started_at", "completed_at", "updated_at"):
        _assert_naive_utc_near(getattr(row, column), reference, column)

    created_delta = abs((row.started_at - row.created_at).total_seconds())
    assert created_delta <= BASIS_TOLERANCE_SECONDS, (
        f"created_at(server_default UTC) 与 started_at 相差 {created_delta:.0f}s"
        " ⇒ 同一行混了两种基准"
    )


@pytest.mark.anyio
async def test_write_plan_row_still_writes_after_cancel(factory):
    """#120 不能回退取消竞态修复：行已被置 cancelled + cancel_requested 后，
    收尾直接 UPDATE 仍须落终态与步数（TaskProgressTracker 会因取消而冻结）。"""
    task_id = await _seed_task(
        factory, task_type="agent_plan", status="cancelled",
        cancel_requested=True, progress=10,
    )

    await _write_plan_row(
        factory, task_id,
        status="cancelled", progress=100,
        status_message="已取消（完成 2/3 步）",
        progress_details={"stage": "cancelled", "steps_done": 2, "steps_total": 3},
    )
    reference = _naive_utc_now()

    row = await _load(factory, task_id)
    assert row.status == "cancelled"
    assert row.progress == 100
    assert row.progress_details["steps_done"] == 2
    assert row.progress_details["steps_total"] == 3
    _assert_naive_utc_near(row.updated_at, reference, "updated_at")


@pytest.mark.anyio
async def test_cancel_task_stamps_naive_utc(factory):
    task_id = await _seed_task(factory, status="running", progress=20)

    async with factory() as session:
        assert await BackgroundTaskService.cancel_task(task_id, "u-1", session) is True
    reference = _naive_utc_now()

    row = await _load(factory, task_id)
    assert row.status == "cancelled"
    assert row.cancel_requested is True
    _assert_naive_utc_near(row.completed_at, reference, "completed_at")


@pytest.mark.anyio
async def test_progress_tracker_stamps_naive_utc(factory, monkeypatch):
    task_id = await _seed_task(factory, status="pending", progress=0)
    engine = factory.kw["bind"]

    async def _fake_engine(_user_id: str):
        return engine

    monkeypatch.setattr(bts_module, "get_engine", _fake_engine)
    tracker = TaskProgressTracker(task_id=task_id, user_id="u-1", task_name="章节")

    await tracker.start()
    started_reference = _naive_utc_now()
    row = await _load(factory, task_id)
    assert row.status == "running", "tracker.start 未落库 ⇒ 测试未命中被测路径"
    _assert_naive_utc_near(row.started_at, started_reference, "started_at")
    _assert_naive_utc_near(row.updated_at, started_reference, "updated_at")

    await tracker.complete()
    completed_reference = _naive_utc_now()
    row = await _load(factory, task_id)
    assert row.status == "completed"
    _assert_naive_utc_near(row.completed_at, completed_reference, "completed_at")


@pytest.mark.anyio
async def test_manage_background_task_cancel_stamps_naive_utc(factory):
    async with factory() as session:
        session.add(Project(id="p-1", user_id="u-1", title="t"))
        await session.commit()
    task_id = await _seed_task(
        factory, status="running", progress=30, project_id="p-1", user_id="u-1"
    )

    async with factory() as session:
        project = await session.get(Project, "p-1")
        tools = ProjectAgentOperationalTools(project, session)
        cancelled_id, _, _ = await tools._manage_background_task_cancel({"task_id": task_id})
        assert cancelled_id == task_id
        await session.commit()
    reference = _naive_utc_now()

    row = await _load(factory, task_id)
    assert row.status == "cancelled"
    _assert_naive_utc_near(row.completed_at, reference, "completed_at")


@pytest.mark.anyio
async def test_cleanup_cutoff_uses_naive_utc_basis(factory):
    """cleanup 的比较基准必须与 completed_at 同基准，否则会早删/漏删。

    该行 age 为 6d23h（UTC）；UTC cutoff=now-7d 时不该删，本地 cutoff=now+8h-7d
    时会被误删 —— 只有 cutoff 与 completed_at 同为 naive UTC 才能保住它。
    """
    recent = await _seed_task(
        factory, status="completed",
        completed_at=_naive_utc_now() - timedelta(days=6, hours=23),
    )
    stale = await _seed_task(
        factory, status="completed",
        completed_at=_naive_utc_now() - timedelta(days=8),
    )

    async with factory() as session:
        await BackgroundTaskService.cleanup_old_tasks("u-1", session, days=7)

    assert await _load(factory, recent) is not None, "UTC 基准下不应删除 6d23h 的行"
    assert await _load(factory, stale) is None, "超过 7d 的行应被清理"
