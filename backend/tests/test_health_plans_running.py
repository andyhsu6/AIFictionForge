"""PR-4：/health 的 plans_running 与实际运行中计划一致。"""
import uuid

import pytest

from app.main import _count_running_plans, health_check
from app.models.background_task import BackgroundTask
from test_startup_plan_interruption import session_factory  # noqa: F401


def _row(task_type="agent_plan", status="running"):
    return BackgroundTask(
        id=uuid.uuid4().hex,
        user_id="u-test",
        project_id="p-test",
        task_type=task_type,
        status=status,
        progress=10,
    )


async def _seed(session_factory, *rows):
    async with session_factory() as session:
        for row in rows:
            session.add(row)
        await session.commit()


@pytest.mark.anyio
async def test_plans_running_matches_actual_rows(session_factory):  # noqa: F811
    engine = session_factory.kw["bind"]
    await _seed(
        session_factory,
        _row(), _row(),                                   # 2 个在跑的计划
        _row(status="pending"),                           # 已批准未开跑：不算
        _row(status="completed"),                         # 终态：不算
        _row(task_type="chapter_write"),                  # 别的后台任务：不算
    )

    assert await _count_running_plans(engine=engine) == 2


@pytest.mark.anyio
async def test_health_reports_plans_running_and_keeps_legacy_keys(session_factory, monkeypatch):  # noqa: F811
    engine = session_factory.kw["bind"]
    await _seed(session_factory, _row())
    monkeypatch.setattr("app.main._health_engine", lambda: engine)

    payload = await health_check()

    assert payload["status"] == "ok"
    assert "branch" in payload and "commit" in payload     # aistoryforge.sh 的 sed 依赖它
    assert payload["plans_running"] == 1


@pytest.mark.anyio
async def test_health_never_fails_because_of_the_count(monkeypatch):
    async def boom(engine=None):
        raise RuntimeError("db down")

    monkeypatch.setattr("app.main._count_running_plans", boom)
    payload = await health_check()

    assert payload["status"] == "ok"
    assert payload["plans_running"] is None
