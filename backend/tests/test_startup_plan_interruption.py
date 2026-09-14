"""PR-4：服务重启后 agent_plan 行的可读中断说明。"""
import json
import os
import uuid
from pathlib import Path

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.core.errors import ERROR_REGISTRY as ERROR_MESSAGES
from app.database import Base
from app.main import _sweep_interrupted_tasks
from app.models.background_task import BackgroundTask

CODE = "progress.agent_plan_interrupted"


@pytest.fixture
async def session_factory():
    db_path = f"/tmp/test_plan_interruption_{uuid.uuid4().hex}.db"
    engine = create_async_engine(f"sqlite+aiosqlite:///{db_path}")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(bind=engine, class_=AsyncSession, expire_on_commit=False)
    yield factory
    await engine.dispose()
    if os.path.exists(db_path):
        os.remove(db_path)


def _plan_row(**overrides):
    kwargs = {
        "id": uuid.uuid4().hex,
        "user_id": "u-test",
        "project_id": "p-test",
        "task_type": "agent_plan",
        "status": "running",
        "progress": 40,
        "status_message": "已完成 2/5 步",
        "task_input": {"steps": [{"id": f"s{i}"} for i in range(5)]},
        "progress_details": {
            "stage": "running",
            "message": "已完成 2/5 步",
            "outcome": "running",
            "steps_total": 5,
            "steps_done": 2,
            "failed_at_step": None,
            "cancel": {"requested": False, "reason": None,
                       "cancelled_sub_tasks": [], "uncancellable_sub_tasks": []},
            "step_results": [],
        },
    }
    kwargs.update(overrides)
    return BackgroundTask(**kwargs)


async def _seed(session_factory, row):
    async with session_factory() as session:
        session.add(row)
        await session.commit()


async def _fetch(session_factory, row_id):
    async with session_factory() as session:
        return await session.get(BackgroundTask, row_id)


@pytest.mark.anyio
async def test_interrupted_plan_row_carries_readable_note(session_factory):
    row = _plan_row()
    await _seed(session_factory, row)

    plan_rows = await _sweep_interrupted_tasks(engine=session_factory.kw["bind"])

    assert plan_rows == 1
    saved = await _fetch(session_factory, row.id)
    assert saved.status == "failed"
    assert saved.status_code == CODE
    assert saved.status_params == {"steps_done": 2, "steps_total": 5}
    assert saved.error_message == "服务重启，计划执行已中断"
    assert len(saved.status_message) <= 120                 # PG 侧 String(500) 超长直接报错
    assert "2/5" in saved.status_message and "请重新发起" in saved.status_message
    details = saved.progress_details
    assert details["interrupted"]["auto_retry"] is False    # 语义诚实：不自动重发
    assert details["interrupted"]["reason"] == "服务重启"
    assert details["steps_done"] == 2                       # 写方（PR-2b）的键必须原样保留
    assert details["steps_total"] == 5
    assert details["stage"] == "running"                    # 不给 PR-3 的枚举渲染造新值


@pytest.mark.anyio
async def test_non_plan_background_row_keeps_legacy_note(session_factory):
    row = _plan_row(task_type="chapter_write", status="pending")
    await _seed(session_factory, row)

    assert await _sweep_interrupted_tasks(engine=session_factory.kw["bind"]) == 0

    saved = await _fetch(session_factory, row.id)
    assert saved.status == "failed"
    assert saved.status_message == "服务重启，任务已中断，请重新发起"
    assert saved.status_code is None


@pytest.mark.anyio
async def test_terminal_plan_rows_are_untouched(session_factory):
    done = _plan_row(status="completed", progress=100)
    cancelled = _plan_row(status="cancelled")
    factory = session_factory
    engine = factory.kw["bind"]
    await _seed(factory, done)
    await _seed(factory, cancelled)

    assert await _sweep_interrupted_tasks(engine=engine) == 0

    assert (await _fetch(factory, done.id)).status == "completed"
    assert (await _fetch(factory, cancelled.id)).status == "cancelled"


@pytest.mark.anyio
async def test_plan_row_without_progress_details_still_gets_note(session_factory):
    row = _plan_row(progress_details=None)
    await _seed(session_factory, row)

    assert await _sweep_interrupted_tasks(engine=session_factory.kw["bind"]) == 1

    saved = await _fetch(session_factory, row.id)
    assert saved.status_params == {"steps_done": 0, "steps_total": 0}
    assert saved.progress_details["interrupted"]["auto_retry"] is False


def test_error_code_registered_bilingually():
    assert CODE in ERROR_MESSAGES
    text, http_status = ERROR_MESSAGES[CODE]
    assert http_status == 200
    assert "{{steps_done}}" in text and "{{steps_total}}" in text
    for locale_ns in ("zh", "en"):
        path = Path(__file__).resolve().parents[2] / "frontend/src/locales" / locale_ns / "errors.json"
        payload = json.loads(path.read_text(encoding="utf-8"))
        assert "agent_plan_interrupted" in payload.get("progress", {}), f"{locale_ns} 缺 {CODE}"
