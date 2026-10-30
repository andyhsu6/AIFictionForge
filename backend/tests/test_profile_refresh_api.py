"""档案重算 API 接线测试（M2）：estimate / preview / apply。

钉死契约：
(a) estimate 只算不调模型，候选数与水位语义与 service 层一致；
(b) preview 建后台任务并产出 `task_result.plan`，列表态只回 summary、详情态给全量；
(c) apply 正常回放计划且不删任务；
(d) 计划过期 / 章节信号变化 → conflict.profile_plan_stale；
(e) 预览进行中再 preview / apply → conflict.profile_refresh_running；
(f) 非项目所有者 → 404。

夹具与断言文本全部为中性占位（AGENTS.md 脱敏硬约束）。
"""
from __future__ import annotations

import asyncio
import os
import uuid
from datetime import datetime, timedelta, timezone

import pytest
from fastapi import FastAPI, Request
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import app.database as database_module
from app.api.projects import router as projects_router
from app.api.tasks import router as tasks_router
from app.core.errors import register_exception_handlers
from app.database import Base, get_db
from app.models.background_task import BackgroundTask
from app.models.chapter import Chapter
from app.models.character import Character
from app.models.project import Project
from app.services.background_task_service import background_task_service
from app.services.character_profile_sync_service import (
    CharacterProfileSyncService,
    character_profile_sync_service,
)

USER_ID = "user-pr-api"
OTHER_USER_ID = "user-pr-other"
PROJECT_ID = "proj-pr-api"
QUOTE = "占位正文甲。占位正文乙"

pytestmark = pytest.mark.anyio


class FakeAIService:
    """记录调用次数的最小 AI 桩。"""

    def __init__(self, response=None):
        self.response = response
        self.calls: list[dict] = []

    async def call_with_json_retry(self, *, prompt, **kwargs):
        self.calls.append({"prompt": prompt, **kwargs})
        return self.response


class Harness:
    def __init__(self, app: FastAPI, session_factory, ai: FakeAIService):
        self.app = app
        self.session_factory = session_factory
        self.ai = ai

    def client(self) -> AsyncClient:
        return AsyncClient(transport=ASGITransport(app=self.app), base_url="http://test")


@pytest.fixture(autouse=True)
def _clear_reentry_guard():
    CharacterProfileSyncService._in_progress.clear()
    CharacterProfileSyncService._world_in_progress.clear()
    yield
    CharacterProfileSyncService._in_progress.clear()
    CharacterProfileSyncService._world_in_progress.clear()


@pytest.fixture(autouse=True)
def _isolate_task_queues():
    """后台队列是进程级单例：每测试换新队列，避免上个测试的 worker 抢走本测试的任务。"""
    background_task_service._user_queues.clear()
    background_task_service._user_workers.clear()
    yield
    background_task_service._user_queues.clear()
    background_task_service._user_workers.clear()


@pytest.fixture
async def harness(monkeypatch):
    db_path = f"/tmp/test_profile_refresh_api_{uuid.uuid4().hex}.db"
    engine = create_async_engine(f"sqlite+aiosqlite:///{db_path}")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    session_factory = async_sessionmaker(bind=engine, expire_on_commit=False)

    # 后台任务用 get_engine(user_id) 开独立会话：把共享引擎缓存指向本测试库
    monkeypatch.setitem(database_module._engine_cache, "shared_sqlite", engine)

    ai = FakeAIService()

    async def _fake_ai_service(user_id, db):
        return ai

    monkeypatch.setattr(
        CharacterProfileSyncService, "_resolve_ai_service", staticmethod(_fake_ai_service)
    )

    async def override_get_db():
        async with session_factory() as session:
            yield session

    app = FastAPI()
    register_exception_handlers(app)
    app.include_router(projects_router)
    app.include_router(tasks_router)
    app.dependency_overrides[get_db] = override_get_db

    @app.middleware("http")
    async def fake_auth(request: Request, call_next):
        request.state.user_id = request.headers.get("x-test-user", USER_ID)
        return await call_next(request)

    try:
        yield Harness(app, session_factory, ai)
    finally:
        app.dependency_overrides.clear()
        await engine.dispose()
        if os.path.exists(db_path):
            os.remove(db_path)


async def _seed(session_factory, *, chapters, characters) -> None:
    async with session_factory() as db:
        db.add(Project(id=PROJECT_ID, user_id=USER_ID, title="占位项目", genre="通用"))
        for spec in characters:
            db.add(Character(project_id=PROJECT_ID, **spec))
        for number, content in chapters:
            db.add(
                Chapter(
                    project_id=PROJECT_ID,
                    chapter_number=number,
                    title=f"占位章节标题{number}",
                    content=content,
                    word_count=len(content),
                )
            )
        await db.commit()


def _fields_payload(name: str, **fields) -> dict:
    return {
        "characters": [
            {
                "name": name,
                "fields": {
                    "personality": fields.get("personality"),
                    "background": fields.get("background"),
                    "appearance": fields.get("appearance"),
                    "current_state": fields.get("current_state"),
                },
                "organizations": [],
            }
        ]
    }


async def _wait_for_task(client: AsyncClient, task_id: str, *, timeout: float = 10.0) -> dict:
    """轮询任务直到终态（后台队列在同一个事件循环里跑）。"""
    deadline = asyncio.get_event_loop().time() + timeout
    body: dict = {}
    while asyncio.get_event_loop().time() < deadline:
        body = (await client.get(f"/tasks/{task_id}")).json()
        if body.get("status") in ("completed", "failed", "cancelled"):
            return body
        await asyncio.sleep(0.05)
    raise AssertionError(f"任务未在 {timeout}s 内结束: {body}")


async def _preview(harness: Harness, client: AsyncClient, **body) -> dict:
    resp = await client.post(f"/projects/{PROJECT_ID}/characters/refresh-preview", json=body)
    assert resp.status_code == 200, resp.text
    created = resp.json()
    assert created["task_type"] == "profile_preview"
    task = await _wait_for_task(client, created["task_id"])
    assert task["status"] == "completed", task
    return task


# --- ① estimate 不调模型 ----------------------------------------------------


async def test_estimate_counts_candidates_without_calling_model(harness):
    await _seed(
        harness.session_factory,
        chapters=[(1, f"角色甲登场。{QUOTE}。")],
        characters=[
            {"name": "角色甲"},
            {"name": "角色乙", "profile_synced_through_chapter": 5},
        ],
    )

    async with harness.client() as client:
        resp = await client.get(
            f"/projects/{PROJECT_ID}/characters/refresh-estimate",
            params={"mode": "full", "from_chapter": 1},
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()

    assert body["mode"] == "full"
    assert body["entities_total"] == 2
    assert body["candidates"] == 1, "全量模式忽略水位，但仍要求正文里出现过名称"
    assert body["no_mention"] == 1
    assert body["chunks"] == 1
    assert body["calls_min"] >= 1
    assert body["calls_max"] == body["calls_min"] * 3
    assert harness.ai.calls == [], "estimate 不得调用模型"


async def test_estimate_respects_watermark_in_incremental_mode(harness):
    await _seed(
        harness.session_factory,
        chapters=[(1, f"角色甲登场。{QUOTE}。")],
        characters=[{"name": "角色甲", "profile_synced_through_chapter": 5}],
    )

    async with harness.client() as client:
        resp = await client.get(
            f"/projects/{PROJECT_ID}/characters/refresh-estimate",
            params={"mode": "incremental"},
        )
        assert resp.status_code == 200, resp.text
        assert resp.json()["candidates"] == 0

    assert harness.ai.calls == []


# --- ② preview 建任务并产出计划 ---------------------------------------------


async def test_preview_creates_task_with_plan_and_list_returns_summary_only(harness):
    await _seed(
        harness.session_factory,
        chapters=[(1, f"角色甲登场。{QUOTE}。")],
        characters=[{"name": "角色甲"}],
    )
    harness.ai.response = _fields_payload("角色甲", personality="占位新性格")

    async with harness.client() as client:
        task = await _preview(harness, client, mode="full", from_chapter=1)

        plan = task["task_result"]["plan"]
        assert plan["plan_version"] == 1
        assert plan["mode"] == "full"
        assert plan["from_chapter"] == 1
        assert plan["through_chapter"] == 1
        assert plan["created_at"]
        assert plan["project_signal"]["chapters_with_content"] == 1
        assert [entry["name"] for entry in plan["entities"]] == ["角色甲"]
        assert plan["summary"]["add"] == 1
        assert plan["summary"]["entities"] == 1
        assert task["progress_details"]["stage"] == "complete"
        assert task["status_code"] == "progress.profile_preview_ready"

        listed = await client.get("/tasks", params={"project_id": PROJECT_ID})
        assert listed.status_code == 200, listed.text
        item = next(i for i in listed.json()["items"] if i["id"] == task["id"])
        assert item["task_result"] == {"summary": plan["summary"]}, "列表态不得回传完整 plan"

    assert len(harness.ai.calls) >= 1
    # dry-run 不写库
    async with harness.session_factory() as db:
        character = (await db.get(Character, plan["entities"][0]["id"]))
        assert character.personality is None


# --- ③ apply 正常回放 -------------------------------------------------------


async def test_apply_replays_plan_and_keeps_task(harness):
    await _seed(
        harness.session_factory,
        chapters=[(1, f"角色甲登场。{QUOTE}。")],
        characters=[{"name": "角色甲"}],
    )
    harness.ai.response = _fields_payload("角色甲", personality="占位新性格")

    async with harness.client() as client:
        task = await _preview(harness, client, mode="full")
        calls_after_preview = len(harness.ai.calls)

        resp = await client.post(
            f"/projects/{PROJECT_ID}/characters/refresh-apply",
            json={"preview_task_id": task["id"]},
        )
        assert resp.status_code == 200, resp.text
        assert resp.json() == {"applied": 1, "fields_written": 1, "stale": []}

        still_there = await client.get(f"/tasks/{task['id']}")
        assert still_there.status_code == 200, "apply 后不得删除预览任务"
        assert still_there.json()["task_result"]["apply_summary"]["fields_written"] == 1

    assert len(harness.ai.calls) == calls_after_preview, "apply 不得调用模型"
    async with harness.session_factory() as db:
        character = await db.get(Character, task["task_result"]["plan"]["entities"][0]["id"])
        assert character.personality == "占位新性格"


# --- ④ 计划过期 -------------------------------------------------------------


async def test_apply_rejects_plan_after_chapter_signal_changed(harness):
    await _seed(
        harness.session_factory,
        chapters=[(1, f"角色甲登场。{QUOTE}。")],
        characters=[{"name": "角色甲"}],
    )
    harness.ai.response = _fields_payload("角色甲", personality="占位新性格")

    async with harness.client() as client:
        task = await _preview(harness, client, mode="full")

        async with harness.session_factory() as db:
            db.add(
                Chapter(
                    project_id=PROJECT_ID,
                    chapter_number=2,
                    title="占位章节标题2",
                    content="角色甲再次登场。",
                    word_count=10,
                )
            )
            await db.commit()

        resp = await client.post(
            f"/projects/{PROJECT_ID}/characters/refresh-apply",
            json={"preview_task_id": task["id"]},
        )
        assert resp.status_code == 409, resp.text
        assert resp.json()["code"] == "conflict.profile_plan_stale"

    async with harness.session_factory() as db:
        character = (await db.get(Character, task["task_result"]["plan"]["entities"][0]["id"]))
        await db.refresh(character)
        assert character.personality is None, "整单拒绝：不得部分写入"


async def test_apply_rejects_plan_older_than_ttl(harness):
    await _seed(
        harness.session_factory,
        chapters=[(1, f"角色甲登场。{QUOTE}。")],
        characters=[{"name": "角色甲"}],
    )
    harness.ai.response = _fields_payload("角色甲", personality="占位新性格")

    async with harness.client() as client:
        task = await _preview(harness, client, mode="full")

        expired = (
            datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(minutes=61)
        ).isoformat()
        async with harness.session_factory() as db:
            row = await db.get(BackgroundTask, task["id"])
            result = dict(row.task_result)
            result["plan"] = {**result["plan"], "created_at": expired}
            row.task_result = result
            await db.commit()

        resp = await client.post(
            f"/projects/{PROJECT_ID}/characters/refresh-apply",
            json={"preview_task_id": task["id"]},
        )
        assert resp.status_code == 409, resp.text
        assert resp.json()["code"] == "conflict.profile_plan_stale"


async def test_apply_with_unknown_preview_task_returns_not_found(harness):
    await _seed(
        harness.session_factory,
        chapters=[(1, "角色甲登场。")],
        characters=[{"name": "角色甲"}],
    )

    async with harness.client() as client:
        resp = await client.post(
            f"/projects/{PROJECT_ID}/characters/refresh-apply",
            json={"preview_task_id": "missing-task"},
        )
        assert resp.status_code == 404, resp.text
        assert resp.json()["code"] == "not_found.profile_plan"


# --- ⑤ 预览进行中 ----------------------------------------------------------


async def test_preview_and_apply_conflict_while_refresh_running(harness):
    await _seed(
        harness.session_factory,
        chapters=[(1, f"角色甲登场。{QUOTE}。")],
        characters=[{"name": "角色甲"}],
    )
    harness.ai.response = _fields_payload("角色甲", personality="占位新性格")

    async with harness.client() as client:
        task = await _preview(harness, client, mode="full")

        CharacterProfileSyncService._in_progress.add(PROJECT_ID)
        try:
            resp = await client.post(
                f"/projects/{PROJECT_ID}/characters/refresh-preview", json={"mode": "full"}
            )
            assert resp.status_code == 409, resp.text
            assert resp.json()["code"] == "conflict.profile_refresh_running"

            applied = await client.post(
                f"/projects/{PROJECT_ID}/characters/refresh-apply",
                json={"preview_task_id": task["id"]},
            )
            assert applied.status_code == 409, applied.text
            assert applied.json()["code"] == "conflict.profile_refresh_running"
        finally:
            CharacterProfileSyncService._in_progress.discard(PROJECT_ID)

        # 忙闲解除后可正常应用
        ok = await client.post(
            f"/projects/{PROJECT_ID}/characters/refresh-apply",
            json={"preview_task_id": task["id"]},
        )
        assert ok.status_code == 200, ok.text
        assert ok.json()["fields_written"] == 1


# --- ⑦ 清理任务时保留新鲜的计划 --------------------------------------------


async def test_clear_project_tasks_keeps_fresh_profile_plans(harness):
    await _seed(
        harness.session_factory,
        chapters=[(1, f"角色甲登场。{QUOTE}。")],
        characters=[{"name": "角色甲"}],
    )
    harness.ai.response = _fields_payload("角色甲", personality="占位新性格")

    async with harness.client() as client:
        fresh = await _preview(harness, client, mode="full")

        async with harness.session_factory() as db:
            stale = BackgroundTask(
                user_id=USER_ID,
                project_id=PROJECT_ID,
                task_type="profile_preview",
                status="completed",
                progress=100,
                task_result={"summary": {}, "plan_available": True, "plan": {"x": 1}},
                created_at=datetime.now(timezone.utc).replace(tzinfo=None)
                - timedelta(minutes=61),
            )
            plain = BackgroundTask(
                user_id=USER_ID,
                project_id=PROJECT_ID,
                task_type="character_generate",
                status="completed",
                progress=100,
            )
            db.add_all([stale, plain])
            await db.commit()
            stale_id, plain_id = stale.id, plain.id

        resp = await client.delete(f"/tasks/project/{PROJECT_ID}/clear")
        assert resp.status_code == 200, resp.text

        assert (await client.get(f"/tasks/{fresh['id']}")).status_code == 200, (
            "60 分钟内的预览计划必须保留"
        )
        assert (await client.get(f"/tasks/{stale_id}")).status_code == 404
        assert (await client.get(f"/tasks/{plain_id}")).status_code == 404


# --- ⑥ 非项目所有者 --------------------------------------------------------


async def test_endpoints_reject_other_user(harness):
    await _seed(
        harness.session_factory,
        chapters=[(1, "角色甲登场。")],
        characters=[{"name": "角色甲"}],
    )
    headers = {"x-test-user": OTHER_USER_ID}

    async with harness.client() as client:
        estimate = await client.get(
            f"/projects/{PROJECT_ID}/characters/refresh-estimate", headers=headers
        )
        preview = await client.post(
            f"/projects/{PROJECT_ID}/characters/refresh-preview", json={}, headers=headers
        )
        applied = await client.post(
            f"/projects/{PROJECT_ID}/characters/refresh-apply",
            json={"preview_task_id": "task-x"},
            headers=headers,
        )

    for resp in (estimate, preview, applied):
        assert resp.status_code == 404, resp.text
        assert resp.json()["code"] == "not_found.project_or_forbidden"


# --- ⑧ 批次进度映射 -------------------------------------------------------


async def test_preview_maps_batch_progress_into_task_progress(harness, monkeypatch):
    """on_progress(done,total) → 20+70*done/total，且 batches_done/total 逐批递增。"""
    names = [f"角色{index}" for index in range(1, 7)]  # 6 实体 = 2 个 chunk
    await _seed(
        harness.session_factory,
        chapters=[(1, "、".join(names) + f"登场。{QUOTE}。")],
        characters=[{"name": name} for name in names],
    )
    harness.ai.response = {
        "characters": [
            {"name": name, "fields": {"personality": f"{name}占位性格"}, "organizations": []}
            for name in names
        ]
    }

    from app.services.background_task_service import TaskProgressTracker

    original_update = TaskProgressTracker._update_task
    updates: list[dict] = []

    async def spy_update(self, **kwargs):
        updates.append(dict(kwargs))
        return await original_update(self, **kwargs)

    monkeypatch.setattr(TaskProgressTracker, "_update_task", spy_update)

    async with harness.client() as client:
        task = await _preview(harness, client, mode="full")
    assert len(task["task_result"]["plan"]["entities"]) == 6

    details = [
        update["progress_details"]
        for update in updates
        if isinstance(update.get("progress_details"), dict)
    ]
    batch_steps = [d for d in details if d["stage"] == "generating"]
    assert [(d["batches_done"], d["batches_total"]) for d in batch_steps] == [(1, 2), (2, 2)], (
        "批次进度应从 (1,total) 开始逐批递增"
    )
    for update in updates:
        detail = update.get("progress_details")
        if not isinstance(detail, dict) or detail["stage"] != "generating":
            continue
        done, total = detail["batches_done"], detail["batches_total"]
        assert update["progress"] == 20 + int(70 * done / max(total, 1))
        assert update["status_code"] == "progress.profile_previewing"

    # 三档仍在：preparing 20 → 批间递增 → saving 90 → complete 100
    stages = [d["stage"] for d in details]
    assert stages[0] == "init"
    assert "preparing" in stages and "saving" in stages and "complete" in stages
    saving = next(u for u in updates if (u.get("progress_details") or {}).get("stage") == "saving")
    assert saving["progress"] == 90
    assert saving["progress_details"]["batches_done"] == saving["progress_details"]["batches_total"] == 2


# --- ⑨ 预览中途取消 -------------------------------------------------------


async def test_preview_cancel_midway_marks_task_cancelled_without_plan(harness):
    names = [f"角色{index}" for index in range(1, 7)]  # 2 个 chunk，够在中途取消
    await _seed(
        harness.session_factory,
        chapters=[(1, "、".join(names) + f"登场。{QUOTE}。")],
        characters=[{"name": name} for name in names],
    )
    harness.ai.response = {
        "characters": [
            {"name": name, "fields": {"personality": f"{name}占位性格"}, "organizations": []}
            for name in names
        ]
    }

    original_call = harness.ai.call_with_json_retry

    async def cancel_on_first_batch(*, prompt, **kwargs):
        """第一批模型调用后置 cancel_requested：下一批开始前服务层应中断。"""
        async with harness.session_factory() as db:
            await db.execute(
                update(BackgroundTask)
                .where(
                    BackgroundTask.project_id == PROJECT_ID,
                    BackgroundTask.task_type == "profile_preview",
                )
                .values(cancel_requested=True)
            )
            await db.commit()
        return await original_call(prompt=prompt, **kwargs)

    harness.ai.call_with_json_retry = cancel_on_first_batch

    async with harness.client() as client:
        created = await client.post(
            f"/projects/{PROJECT_ID}/characters/refresh-preview", json={"mode": "full"}
        )
        assert created.status_code == 200, created.text
        task = await _wait_for_task(client, created.json()["task_id"])

    assert task["status"] == "cancelled", task
    assert task["status_code"] == "task.cancelled"
    assert "plan" not in (task["task_result"] or {}), "取消不得产出计划"

    async with harness.session_factory() as db:
        characters = list((await db.execute(select(Character))).scalars())
        assert [c.name for c in characters]  # 夹具自证：确实存在实体
        for character in characters:
            assert character.personality is None
            assert character.ai_profile_snapshot is None
            assert character.profile_synced_through_chapter == 0


# --- ⑩ apply_plan 内部忙 → 409 ---------------------------------------------


async def test_apply_returns_conflict_when_apply_plan_reports_busy(harness, monkeypatch):
    await _seed(
        harness.session_factory,
        chapters=[(1, f"角色甲登场。{QUOTE}。")],
        characters=[{"name": "角色甲"}],
    )
    harness.ai.response = _fields_payload("角色甲", personality="占位新性格")

    async with harness.client() as client:
        task = await _preview(harness, client, mode="full")

        async def _busy_apply(*, db, project_id, plan):
            return {"applied": 0, "fields_written": 0, "stale": [], "busy": True}

        monkeypatch.setattr(character_profile_sync_service, "apply_plan", _busy_apply)

        resp = await client.post(
            f"/projects/{PROJECT_ID}/characters/refresh-apply",
            json={"preview_task_id": task["id"]},
        )
        assert resp.status_code == 409, resp.text
        assert resp.json()["code"] == "conflict.profile_refresh_running"

        detail = await client.get(f"/tasks/{task['id']}")
        assert "applied_at" not in detail.json()["task_result"], "忙时不得写应用痕迹"
        assert "plan" in detail.json()["task_result"], "计划本身保留，可重试"

    async with harness.session_factory() as db:
        character = await db.get(Character, task["task_result"]["plan"]["entities"][0]["id"])
        assert character.personality is None
