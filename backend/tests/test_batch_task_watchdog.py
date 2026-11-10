"""批量生成任务 watchdog 测试。

背景：后端以 uvicorn --reload 运行，任何 .py 写入都会重启 worker；正在执行的
批量生成协程（BackgroundTasks）被直接杀掉，`batch_generation_tasks` 永远停在
`status='running'`，UI 既不失败也不成功。

覆盖：
1. 陈旧心跳的 running 任务被回收为 failed；
2. 新鲜心跳不动；
3. completed/failed/cancelled 一律不动；
4. heartbeat_at 为空时回退 started_at / created_at；
5. done-callback：协程异常 → 任务置 failed 且带异常类型名；
6. 迁移后 heartbeat_at 列存在。
"""
import asyncio
import sqlite3
from contextlib import suppress
from datetime import datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import update
from sqlalchemy.exc import OperationalError
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import app.database
from app.api import chapters as chapters_api
from app.database import Base
from app.models.batch_generation_task import BatchGenerationTask
from app.models.chapter import Chapter
from app.models.project import Project

NOW = datetime.now()


def _make_task(
    task_id: str,
    *,
    status: str,
    chapter_ids: list[str] | None = None,
    **kwargs,
) -> BatchGenerationTask:
    return BatchGenerationTask(
        id=task_id,
        project_id="p-watchdog",
        user_id="u-watchdog",
        start_chapter_number=1,
        chapter_count=1,
        chapter_ids=chapter_ids or ["c-watchdog"],
        total_chapters=1,
        status=status,
        **kwargs,
    )


async def _make_session_factory():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(bind=engine, expire_on_commit=False)
    return engine, factory


class _RecorderLogger:
    """logger 替身：项目自研 get_logger 关了 propagate，caplog 不可靠。"""

    def __init__(self) -> None:
        self.warnings: list[str] = []
        self.errors: list[str] = []

    def warning(self, msg, *args):
        self.warnings.append(msg % args if args else str(msg))

    def error(self, msg, *args):
        self.errors.append(msg % args if args else str(msg))

    def info(self, msg, *args):
        pass

    def debug(self, msg, *args):
        pass


class _FlakySession:
    """真实 session 代理：第一次 execute 抛 database is locked，其余透传。"""

    def __init__(self, real_factory, state):
        self._real = real_factory()
        self._state = state

    async def __aenter__(self):
        await self._real.__aenter__()
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return await self._real.__aexit__(exc_type, exc, tb)

    async def execute(self, stmt):
        if self._state["fail_first"]:
            self._state["fail_first"] = False
            raise OperationalError(
                "UPDATE batch_generation_tasks", {}, Exception("database is locked")
            )
        return await self._real.execute(stmt)

    async def commit(self):
        await self._real.commit()


class _FlakyFactory:
    def __init__(self, real_factory, state):
        self._real_factory = real_factory
        self._state = state

    def __call__(self):
        return _FlakySession(self._real_factory, self._state)


class _AlwaysFailSession:
    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    async def execute(self, stmt):
        raise OperationalError(
            "UPDATE batch_generation_tasks", {}, Exception("database is locked")
        )

    async def commit(self):
        pass


class _AlwaysFailFactory:
    def __call__(self):
        return _AlwaysFailSession()


@pytest.mark.anyio
async def test_stale_running_task_is_recovered():
    """心跳早于 11 分钟 → 恢复为 failed，error_message 含「心跳超时」。"""
    engine, factory = await _make_session_factory()
    try:
        async with factory() as session:
            session.add(
                _make_task(
                    "b-stale",
                    status="running",
                    heartbeat_at=NOW - timedelta(minutes=11),
                )
            )
            await session.commit()

        async with factory() as session:
            task = await session.get(BatchGenerationTask, "b-stale")
            recovered = await chapters_api._recover_stale_batch_tasks(session, [task])

            assert recovered == {"b-stale"}
            refreshed = await session.get(BatchGenerationTask, "b-stale")
            assert refreshed.status == "failed"
            assert refreshed.completed_at is not None
            assert "心跳超时" in refreshed.error_message
    finally:
        await engine.dispose()


@pytest.mark.anyio
async def test_fresh_heartbeat_is_not_recovered():
    """心跳 1 分钟前 → 不动。"""
    engine, factory = await _make_session_factory()
    try:
        async with factory() as session:
            session.add(
                _make_task(
                    "b-fresh",
                    status="running",
                    heartbeat_at=NOW - timedelta(minutes=1),
                )
            )
            await session.commit()

        async with factory() as session:
            task = await session.get(BatchGenerationTask, "b-fresh")
            recovered = await chapters_api._recover_stale_batch_tasks(session, [task])

            assert recovered == set()
            assert task.status == "running"
            assert task.error_message is None
    finally:
        await engine.dispose()


@pytest.mark.anyio
async def test_terminal_statuses_are_never_recovered():
    """completed / failed / cancelled + 陈旧心跳 → 一律不动。"""
    engine, factory = await _make_session_factory()
    try:
        async with factory() as session:
            for status in ("completed", "failed", "cancelled"):
                session.add(
                    _make_task(
                        f"b-{status}",
                        status=status,
                        heartbeat_at=NOW - timedelta(hours=2),
                        error_message="原有错误",
                        completed_at=NOW - timedelta(hours=1),
                    )
                )
            await session.commit()

        async with factory() as session:
            tasks = [
                await session.get(BatchGenerationTask, f"b-{status}")
                for status in ("completed", "failed", "cancelled")
            ]
            recovered = await chapters_api._recover_stale_batch_tasks(session, tasks)

            assert recovered == set()
            for task in tasks:
                assert task.status in ("completed", "failed", "cancelled")
                assert task.error_message == "原有错误"
    finally:
        await engine.dispose()


@pytest.mark.anyio
async def test_heartbeat_falls_back_to_started_at_only():
    """heartbeat_at 为空时只看 started_at；created_at（UTC）不得参与回退。"""
    engine, factory = await _make_session_factory()
    try:
        async with factory() as session:
            # 无心跳、started_at 陈旧 → 回收
            session.add(
                _make_task(
                    "b-started",
                    status="running",
                    heartbeat_at=None,
                    started_at=NOW - timedelta(minutes=30),
                    created_at=NOW - timedelta(minutes=40),
                )
            )
            # created_at 陈旧但 started_at 新鲜 → 以 started_at 为准，不回收
            session.add(
                _make_task(
                    "b-created",
                    status="running",
                    heartbeat_at=None,
                    started_at=NOW - timedelta(minutes=1),
                    created_at=NOW - timedelta(hours=3),
                )
            )
            # started_at 缺失 → 不判定（created_at 是 UTC，混用会早杀 8 小时）
            session.add(
                _make_task(
                    "b-no-reference",
                    status="running",
                    heartbeat_at=None,
                    started_at=None,
                    created_at=NOW - timedelta(hours=3),
                )
            )
            await session.commit()

        async with factory() as session:
            tasks = [
                await session.get(BatchGenerationTask, task_id)
                for task_id in ("b-started", "b-created", "b-no-reference")
            ]
            recovered = await chapters_api._recover_stale_batch_tasks(session, tasks)

            assert recovered == {"b-started"}
            assert (await session.get(BatchGenerationTask, "b-created")).status == "running"
            assert (await session.get(BatchGenerationTask, "b-no-reference")).status == "running"
    finally:
        await engine.dispose()


@pytest.mark.anyio
async def test_module_constant_overrides_stale_threshold(monkeypatch):
    """BATCH_STALE_MINUTES 可 monkeypatch：阈值放大后同一心跳不再被判陈旧。"""
    engine, factory = await _make_session_factory()
    try:
        async with factory() as session:
            session.add(
                _make_task(
                    "b-threshold",
                    status="running",
                    heartbeat_at=NOW - timedelta(minutes=11),
                )
            )
            await session.commit()

        monkeypatch.setattr(chapters_api, "BATCH_STALE_MINUTES", 60)
        async with factory() as session:
            task = await session.get(BatchGenerationTask, "b-threshold")
            assert await chapters_api._recover_stale_batch_tasks(session, [task]) == set()
            assert task.status == "running"
    finally:
        await engine.dispose()


@pytest.mark.anyio
async def test_done_callback_marks_crashed_batch_failed(monkeypatch):
    """协程抛 RuntimeError → done-callback 用独立会话把 batch 置 failed。"""
    engine, factory = await _make_session_factory()
    try:
        async with factory() as session:
            session.add(_make_task("b-crash", status="running", heartbeat_at=NOW))
            await session.commit()

        async def _fake_get_engine(user_id: str):
            return engine

        monkeypatch.setattr(app.database, "get_engine", _fake_get_engine)
        # 断言 set 不残留（GC 保护引用需要能正常释放）
        chapters_api.batch_background_tasks.clear()

        async def _boom():
            raise RuntimeError("worker 被重载")

        task = chapters_api._spawn_batch_generation(
            _boom(), batch_id="b-crash", user_id="u-watchdog"
        )
        with pytest.raises(RuntimeError):
            await task

        # done-callback 经 call_soon 调度，兜底任务再落库：轮询至多 2 秒
        for _ in range(200):
            async with factory() as session:
                row = await session.get(BatchGenerationTask, "b-crash")
                if row.status == "failed":
                    break
            await asyncio.sleep(0.01)

        async with factory() as session:
            row = await session.get(BatchGenerationTask, "b-crash")
            assert row.status == "failed"
            assert "RuntimeError" in row.error_message
            assert "worker 被重载" in row.error_message
    finally:
        chapters_api.batch_background_tasks.clear()
        await engine.dispose()


@pytest.mark.anyio
async def test_ticker_keeps_heartbeat_fresh_during_long_chapter(monkeypatch):
    """700s 慢操作压缩模拟：旧心跳在协程运行期间被 ticker 持续刷新，回收不误杀。"""
    engine, factory = await _make_session_factory()
    try:
        stale = NOW - timedelta(minutes=11)
        async with factory() as session:
            session.add(_make_task("b-ticker", status="running", heartbeat_at=stale))
            await session.commit()

        async def _fake_get_engine(user_id: str):
            return engine

        monkeypatch.setattr(app.database, "get_engine", _fake_get_engine)
        chapters_api.batch_background_tasks.clear()

        started = asyncio.Event()

        async def _slow_chapter():
            started.set()
            await asyncio.sleep(0.3)  # 压缩模拟单章 700s（生成 + 600s 同步分析 + 退避）

        task = chapters_api._spawn_batch_generation(
            _slow_chapter(),
            batch_id="b-ticker",
            user_id="u-watchdog",
            heartbeat_interval=0.02,
        )
        await started.wait()
        await asyncio.sleep(0.1)
        assert not task.done()

        async with factory() as session:
            row = await session.get(BatchGenerationTask, "b-ticker")
            assert row.heartbeat_at > stale, "ticker 未刷新心跳，长任务会被误杀"
            # 心跳新鲜 ⇒ 读取接口的陈旧回收不得误杀仍在跑的任务
            assert await chapters_api._recover_stale_batch_tasks(session, [row]) == set()
            assert row.status == "running"

        await task
    finally:
        chapters_api.batch_background_tasks.clear()
        await engine.dispose()


@pytest.mark.anyio
async def test_ticker_survives_transient_db_error(monkeypatch):
    """一次瞬时 DB 异常不应让 ticker 永久退出：后续继续写心跳。"""
    engine, factory = await _make_session_factory()
    try:
        stale = NOW - timedelta(minutes=11)
        async with factory() as session:
            session.add(_make_task("b-flaky", status="running", heartbeat_at=stale))
            await session.commit()

        state = {"fail_first": True}

        async def _fake_make_heartbeat_session(user_id):
            return _FlakyFactory(factory, state), asyncio.Lock()

        monkeypatch.setattr(
            chapters_api, "_make_heartbeat_session", _fake_make_heartbeat_session
        )

        ticker = asyncio.create_task(
            chapters_api._batch_heartbeat_loop(
                "b-flaky", "u-watchdog", interval=0.01
            )
        )
        try:
            refreshed = None
            for _ in range(200):
                async with factory() as session:
                    row = await session.get(BatchGenerationTask, "b-flaky")
                    if row.heartbeat_at and row.heartbeat_at > stale:
                        refreshed = row.heartbeat_at
                        break
                await asyncio.sleep(0.01)
            assert refreshed is not None, "瞬时失败后 ticker 未继续写心跳"
            assert not ticker.done(), "一次瞬时 DB 异常不应让 ticker 退出"
        finally:
            ticker.cancel()
            with suppress(asyncio.CancelledError):
                await ticker
    finally:
        await engine.dispose()


@pytest.mark.anyio
async def test_ticker_gives_up_after_consecutive_failures(monkeypatch):
    """连续失败 BATCH_HEARTBEAT_MAX_FAILURES 次才退出，且以正常返回结束。"""
    recorder = _RecorderLogger()
    monkeypatch.setattr(chapters_api, "logger", recorder)

    async def _fake_make_heartbeat_session(user_id):
        return _AlwaysFailFactory(), asyncio.Lock()

    monkeypatch.setattr(
        chapters_api, "_make_heartbeat_session", _fake_make_heartbeat_session
    )

    ticker = asyncio.create_task(
        chapters_api._batch_heartbeat_loop("b-dead", "u-watchdog", interval=0.01)
    )
    await asyncio.wait_for(ticker, timeout=2)

    assert ticker.done()
    assert ticker.exception() is None
    failures = [w for w in recorder.warnings if "批量任务心跳失败" in w]
    assert len(failures) == chapters_api.BATCH_HEARTBEAT_MAX_FAILURES
    assert any("ticker 退出" in e for e in recorder.errors)


@pytest.mark.anyio
async def test_ticker_is_cancelled_when_main_task_finishes(monkeypatch):
    """主任务正常结束或抛异常时 ticker 都被 cancel，不留后台任务。"""
    engine, factory = await _make_session_factory()
    try:
        async with factory() as session:
            session.add(_make_task("b-noleak", status="running", heartbeat_at=NOW))
            await session.commit()

        async def _fake_get_engine(user_id: str):
            return engine

        monkeypatch.setattr(app.database, "get_engine", _fake_get_engine)
        chapters_api.batch_background_tasks.clear()
        baseline = set(asyncio.all_tasks())

        async def _quick():
            await asyncio.sleep(0.05)

        normal = chapters_api._spawn_batch_generation(
            _quick(),
            batch_id="b-noleak",
            user_id="u-watchdog",
            heartbeat_interval=0.01,
        )
        await normal

        async def _boom():
            raise RuntimeError("boom")

        crashed = chapters_api._spawn_batch_generation(
            _boom(),
            batch_id="b-noleak",
            user_id="u-watchdog",
            heartbeat_interval=0.01,
        )
        with pytest.raises(RuntimeError):
            await crashed

        # 等 done-callback 的兜底任务也结束，再断言无遗留 task
        for _ in range(200):
            if not chapters_api.batch_background_tasks:
                break
            await asyncio.sleep(0.01)
        await asyncio.sleep(0)

        leftover = set(asyncio.all_tasks()) - baseline
        assert not leftover, f"ticker 泄漏: {leftover}"
    finally:
        chapters_api.batch_background_tasks.clear()
        await engine.dispose()


@pytest.mark.anyio
async def test_coroutine_exits_when_externally_failed(monkeypatch):
    """协程发现 status='failed'（被回收）→ 立即退出，且不把终态改回 completed。"""
    engine, factory = await _make_session_factory()
    try:
        async with factory() as session:
            session.add(Project(id="p-stopped", user_id="u-watchdog", title="stub"))
            session.add(
                Chapter(
                    id="c-stopped",
                    project_id="p-stopped",
                    chapter_number=1,
                    title="stub",
                )
            )
            session.add(
                _make_task(
                    "b-stopped",
                    status="pending",
                    chapter_ids=["c-stopped"],
                )
            )
            await session.commit()

        async def _fake_get_engine(user_id: str):
            return engine

        calls = 0

        async def _fake_generate(**kwargs):
            nonlocal calls
            calls += 1
            # 模拟陈旧回收：另一条会话在协程运行期间把任务标 failed
            async with factory() as other:
                await other.execute(
                    update(BatchGenerationTask)
                    .where(BatchGenerationTask.id == "b-stopped")
                    .values(
                        status="failed",
                        completed_at=datetime.now(),
                        error_message="外部回收：心跳超时",
                    )
                )
                await other.commit()
            return {"summary": "已生成"}

        async def _prerequisites_ok(db, chapter):
            return True, "", []

        async def _analysis_ready(db, chapter):
            return True, ""

        async def _no_sleep(*args, **kwargs):
            return None

        monkeypatch.setattr(app.database, "get_engine", _fake_get_engine)
        monkeypatch.setattr(chapters_api, "generate_single_chapter_for_batch", _fake_generate)
        monkeypatch.setattr(chapters_api, "check_prerequisites", _prerequisites_ok)
        monkeypatch.setattr(chapters_api, "check_previous_analysis_ready", _analysis_ready)
        monkeypatch.setattr(asyncio, "sleep", _no_sleep)

        await chapters_api.execute_batch_generation_in_order(
            batch_id="b-stopped",
            user_id="u-watchdog",
            ai_service=object(),
            enable_mcp=False,
        )

        assert calls == 1
        async with factory() as session:
            row = await session.get(BatchGenerationTask, "b-stopped")
            assert row.status == "failed", "回收后的任务被协程改回了别的终态"
            assert row.error_message == "外部回收：心跳超时"
            assert row.completed_chapters == 0
    finally:
        await engine.dispose()


@pytest.mark.anyio
async def test_terminal_update_is_conditional():
    """终态条件更新：running 才写；已被回收的 failed 任务 rowcount=0 且不被覆盖。"""
    engine, factory = await _make_session_factory()
    try:
        async with factory() as session:
            session.add(
                _make_task(
                    "b-final",
                    status="failed",
                    error_message="外部回收：心跳超时",
                    current_chapter_id="c-x",
                )
            )
            session.add(
                _make_task(
                    "b-running",
                    status="running",
                    heartbeat_at=NOW,
                    current_chapter_id="c-y",
                )
            )
            await session.commit()

        async with factory() as session:
            skipped = await chapters_api._update_batch_status_if_running(
                session, "b-final", status="completed", clear_current=True
            )
            assert skipped == 0

            applied = await chapters_api._update_batch_status_if_running(
                session, "b-running", status="completed", clear_current=True
            )
            assert applied == 1

        async with factory() as session:
            finalized = await session.get(BatchGenerationTask, "b-final")
            assert finalized.status == "failed"
            assert finalized.error_message == "外部回收：心跳超时"

            done = await session.get(BatchGenerationTask, "b-running")
            assert done.status == "completed"
            assert done.current_chapter_id is None
    finally:
        await engine.dispose()


def test_migration_adds_heartbeat_column(tmp_path, monkeypatch):
    """迁移往返：upgrade head → downgrade -1 → upgrade head，列随之增删。"""
    from alembic import command
    from alembic.config import Config
    from app.config import settings

    backend_dir = Path(__file__).resolve().parents[1]
    db_path = tmp_path / "migration-heartbeat.db"
    monkeypatch.setattr(
        settings, "database_url", f"sqlite+aiosqlite:///{db_path}"
    )
    monkeypatch.chdir(backend_dir)
    config = Config("alembic-sqlite.ini")

    def _columns() -> list[str]:
        assert db_path.exists()
        with sqlite3.connect(db_path) as conn:
            return [row[1] for row in conn.execute("PRAGMA table_info(batch_generation_tasks)")]

    command.upgrade(config, "head")
    assert "heartbeat_at" in _columns()

    command.downgrade(config, "-1")
    assert "heartbeat_at" not in _columns()

    command.upgrade(config, "head")
    assert "heartbeat_at" in _columns()
