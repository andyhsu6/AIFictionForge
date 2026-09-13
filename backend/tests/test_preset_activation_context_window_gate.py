"""需求 #55 / C1：预设激活必须过同一道上下文窗口硬拦。

堵的后门：`activate_preset` 会把预设的模型写进 Settings 主字段（＝实发默认模型），
但它原先把整个读改写关在写锁里、且从不调用保存闸门 ⇒ 点一下「激活」就能静默换掉
实发模型。C1 让激活在**锁外**过闸、锁内只做 CAS 比对与写入。

钉住：
1. 预设模型低于下限 ⇒ 激活被拒，Settings 主字段 + api_presets 子树一字不动
   （verdict 键允许新增）；且判定在写锁**之外**完成（锁获取 0 次）。
2. 已有合格结论 ⇒ 激活成功，写锁恰好获取 1 次（零探测）。
3. 未见三元组 ⇒ 锁外同步补测一次后激活成功，写锁获取 2 次
   （探测结论缓存写入 + 激活写入）。
4. 探测期间预设被改 ⇒ `validation.preset_changed`（CAS），主字段与 is_active 不动。
5. 两步出口：先经保存路径带上预设**自己的** provider/key/base_url/model 声明窗口
   （这一步会把该模型设为默认、并关掉当前激活预设——`save_settings` 的既有行为），
   随后激活成功。
6. 空模型预设跳过闸门（未配置模型由使用点报 ai_model_not_configured）。

夹具只用中性占位（big-model / gw.test / "chapter one body text"），
不含任何导入原文、角色人名或书名（AGENTS.md 原文数据脱敏硬约束）。
"""
import asyncio
import json
import os
import uuid
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest
from fastapi import FastAPI, Request
from httpx import ASGITransport, AsyncClient
from sqlalchemy import event, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

import app.models  # noqa: F401 - 注册全部表（settings 建表需要）
import app.api.settings as settings_module
import app.services.model_capability_probe as probe_module
from app.core.db_write_lock import db_write_locks
from app.core.errors import register_exception_handlers
from app.database import Base, get_db
from app.models.settings import Settings
from app.services.model_capability_probe import (
    PREFERENCES_KEY,
    ProbeOutcome,
    SOURCE_PROBE,
    SOURCE_USER_DECLARED,
    VERDICT_INCONCLUSIVE,
    VERDICT_QUALIFIED,
    VERDICT_UNQUALIFIED,
    triple_key,
)

BELOW_MINIMUM = "validation.ai_model_below_minimum"
PRESET_CHANGED = "validation.preset_changed"
GATEWAY = "https://gw.test/v1"
OTHER_GATEWAY = "https://other-gw.test/v1"
API_KEY = "sk-stub-not-a-real-key"
QUALIFIED_MODEL = "big-model"
SMALL_MODEL = "gpt-4o-mini"  # 登记表内 128K 级模型
UNKNOWN_MODEL = "mystery-model"  # 未登记，探测判不出
DECLARED = 1_048_576

MAIN_FIELDS = ("openai", API_KEY, GATEWAY, "main-model", 0.7, 2000, "main prompt")


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


# ========== 夹具 ==========


@pytest.fixture
async def db_factory():
    db_path = f"/tmp/test_preset_gate_{uuid.uuid4().hex}.db"
    engine = create_async_engine(
        f"sqlite+aiosqlite:///{db_path}",
        connect_args={"check_same_thread": False, "timeout": 30.0},
    )

    @event.listens_for(engine.sync_engine, "connect")
    def _pragmas(dbapi_conn, _record):  # pragma: no cover - 对齐 app.database.get_engine
        cursor = dbapi_conn.cursor()
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.execute("PRAGMA synchronous=NORMAL")
        cursor.execute("PRAGMA busy_timeout=30000")
        cursor.close()

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(bind=engine, class_=AsyncSession, expire_on_commit=False)
    yield factory
    await engine.dispose()
    for suffix in ("", "-wal", "-shm"):
        path = db_path + suffix
        if os.path.exists(path):
            os.remove(path)


class _Env:
    def __init__(self, session_factory, app):
        self.session_factory = session_factory
        self.app = app

    def client(self):
        return AsyncClient(transport=ASGITransport(app=self.app), base_url="http://test")


@pytest.fixture
async def env(db_factory):
    app = FastAPI()
    register_exception_handlers(app)
    app.include_router(settings_module.router)

    async def override_get_db():
        async with db_factory() as session:
            yield session

    app.dependency_overrides[get_db] = override_get_db

    @app.middleware("http")
    async def fake_auth(request: Request, call_next):
        user_id = request.headers.get("x-test-user")
        if user_id:
            request.state.user = SimpleNamespace(user_id=user_id, is_admin=False)
        return await call_next(request)

    test_env = _Env(db_factory, app)
    yield test_env
    app.dependency_overrides.clear()


def preset(pid: str, config: dict, *, active: bool = False) -> dict:
    return {"id": pid, "name": pid, "is_active": active, "config": config}


def preset_config(
    *,
    model: str = QUALIFIED_MODEL,
    base_url: str = GATEWAY,
    provider: str = "openai",
    key: str = API_KEY,
) -> dict:
    return {
        "api_provider": provider,
        "api_key": key,
        "api_base_url": base_url,
        "llm_model": model,
        "temperature": 0.5,
        "max_tokens": 1234,
        "system_prompt": "preset prompt",
    }


def _entry(verdict: str, *, tokens=None, source=SOURCE_PROBE) -> dict:
    return {
        "result": verdict,
        "source": source,
        "context_window_tokens": tokens,
        "checked_at": _now_iso(),
    }


async def seed_settings(factory, user_id: str, *, preferences: dict) -> None:
    async with factory() as session:
        session.add(
            Settings(
                user_id=user_id,
                api_provider="openai",
                api_key=API_KEY,
                api_base_url=GATEWAY,
                llm_model="main-model",
                temperature=0.7,
                max_tokens=2000,
                system_prompt="main prompt",
                preferences=json.dumps(preferences, ensure_ascii=False),
            )
        )
        await session.commit()


async def read_row(factory, user_id: str) -> Settings:
    async with factory() as session:
        return (
            await session.execute(select(Settings).where(Settings.user_id == user_id))
        ).scalar_one()


def _main_fields(row: Settings):
    return (
        row.api_provider,
        row.api_key,
        row.api_base_url,
        row.llm_model,
        row.temperature,
        row.max_tokens,
        row.system_prompt,
    )


def _install_counting_lock(user_id: str):
    class _CountingLock(asyncio.Lock):
        def __init__(self):
            super().__init__()
            self.acquisitions = 0

        async def acquire(self):  # type: ignore[override]
            acquired = await super().acquire()
            self.acquisitions += 1
            return acquired

    spy = _CountingLock()
    db_write_locks[user_id] = spy
    return spy


class _FakeProbe:
    """可控探测桩：记录调用参数，可阻塞到测试放行（CAS 用例用）。"""

    def __init__(self, outcome: ProbeOutcome):
        self.outcome = outcome
        self.calls = []
        self.started = asyncio.Event()
        self.release = asyncio.Event()
        self.block = False

    async def __call__(self, **kwargs):
        self.calls.append(kwargs)
        self.started.set()
        if self.block:
            await self.release.wait()
        return self.outcome


def _install_probe(monkeypatch, outcome: ProbeOutcome) -> _FakeProbe:
    probe_module.memo_clear()
    fake = _FakeProbe(outcome)
    monkeypatch.setattr(probe_module, "probe_model_context_window", fake)
    return fake


async def post_activate(env, user_id: str, preset_id: str):
    async with env.client() as client:
        return await client.post(
            f"/settings/presets/{preset_id}/activate",
            headers={"x-test-user": user_id},
        )


async def post_settings(env, user_id: str, payload: dict):
    async with env.client() as client:
        return await client.post("/settings", json=payload, headers={"x-test-user": user_id})


# ========== 1. 低于下限被拒：状态一字不动，且判定在锁外短路 ==========


@pytest.mark.anyio
async def test_below_floor_preset_activation_is_rejected_and_leaves_state_untouched(env, monkeypatch):
    user_id = "u-preset-below"
    cached_key = triple_key("openai", GATEWAY, SMALL_MODEL)
    presets = [preset("p-small", preset_config(model=SMALL_MODEL))]
    prefs = {
        "api_presets": {"presets": presets, "version": "1.0"},
        "theme_seed": 7,
        PREFERENCES_KEY: {cached_key: _entry(VERDICT_UNQUALIFIED, tokens=128_000)},
    }
    await seed_settings(env.session_factory, user_id, preferences=prefs)
    fake = _install_probe(monkeypatch, ProbeOutcome(verdict=VERDICT_QUALIFIED, context_window_tokens=DECLARED))
    spy = _install_counting_lock(user_id)
    try:
        resp = await post_activate(env, user_id, "p-small")
    finally:
        db_write_locks.pop(user_id, None)

    assert resp.status_code == 400, resp.text
    assert resp.json()["code"] == BELOW_MINIMUM
    assert fake.calls == [], "已有缓存结论时不得发探测"
    assert spy.acquisitions == 0, "低于下限必须在取写锁之前短路（闸门在锁外）"

    row = await read_row(env.session_factory, user_id)
    assert _main_fields(row) == MAIN_FIELDS, "被拒的激活改动了 Settings 主字段"
    blob = json.loads(row.preferences)
    assert blob["api_presets"] == prefs["api_presets"], "被拒的激活改动了 api_presets 子树"
    assert blob["theme_seed"] == 7, "被拒的激活抹掉了无关偏好键"
    assert blob[PREFERENCES_KEY][cached_key]["result"] == VERDICT_UNQUALIFIED


# ========== 2. 已有合格结论：激活成功，写锁恰好 1 次 ==========


@pytest.mark.anyio
async def test_cached_qualified_preset_activation_succeeds_with_one_lock(env, monkeypatch):
    user_id = "u-preset-cached"
    cached_key = triple_key("openai", GATEWAY, QUALIFIED_MODEL)
    presets = [preset("p-big", preset_config(model=QUALIFIED_MODEL))]
    prefs = {
        "api_presets": {"presets": presets, "version": "1.0"},
        PREFERENCES_KEY: {cached_key: _entry(VERDICT_QUALIFIED, tokens=DECLARED)},
    }
    await seed_settings(env.session_factory, user_id, preferences=prefs)
    fake = _install_probe(monkeypatch, ProbeOutcome(verdict=VERDICT_INCONCLUSIVE))
    spy = _install_counting_lock(user_id)
    try:
        resp = await post_activate(env, user_id, "p-big")
    finally:
        db_write_locks.pop(user_id, None)

    assert resp.status_code == 200, resp.text
    assert fake.calls == [], "已有合格结论不得重探（热路径零网络）"
    assert spy.acquisitions == 1, "缓存命中激活只应取一次写锁（激活写入本身）"

    row = await read_row(env.session_factory, user_id)
    assert row.llm_model == QUALIFIED_MODEL
    assert _main_fields(row) == (
        "openai", API_KEY, GATEWAY, QUALIFIED_MODEL, 0.5, 1234, "preset prompt",
    )
    blob = json.loads(row.preferences)
    assert blob["api_presets"]["presets"][0]["is_active"] is True


# ========== 3. 未见三元组：锁外补测一次后激活，写锁 2 次 ==========


@pytest.mark.anyio
async def test_unseen_triple_is_probed_once_then_activation_succeeds(env, monkeypatch):
    user_id = "u-preset-cold"
    presets = [preset("p-new", preset_config(model=QUALIFIED_MODEL))]
    prefs = {"api_presets": {"presets": presets, "version": "1.0"}}
    await seed_settings(env.session_factory, user_id, preferences=prefs)
    fake = _install_probe(monkeypatch, ProbeOutcome(verdict=VERDICT_QUALIFIED, context_window_tokens=DECLARED))
    spy = _install_counting_lock(user_id)
    try:
        resp = await post_activate(env, user_id, "p-new")
    finally:
        db_write_locks.pop(user_id, None)

    assert resp.status_code == 200, resp.text
    assert len(fake.calls) == 1, "未见三元组必须同步补测且只补测一次"
    assert fake.calls[0]["provider"] == "openai"
    assert fake.calls[0]["base_url"] == GATEWAY
    assert fake.calls[0]["model"] == QUALIFIED_MODEL
    assert spy.acquisitions == 2, "冷探测 = 结论缓存写入 1 次 + 激活写入 1 次"

    row = await read_row(env.session_factory, user_id)
    assert row.llm_model == QUALIFIED_MODEL
    cached = json.loads(row.preferences)[PREFERENCES_KEY][triple_key("openai", GATEWAY, QUALIFIED_MODEL)]
    assert cached["result"] == VERDICT_QUALIFIED


# ========== 4. 探测期间预设被改：CAS 拒绝 ==========


@pytest.mark.anyio
async def test_preset_changed_during_probe_rejects_activation(env, monkeypatch):
    user_id = "u-preset-drift"
    presets = [preset("p-drift", preset_config(model=QUALIFIED_MODEL))]
    prefs = {"api_presets": {"presets": presets, "version": "1.0"}}
    await seed_settings(env.session_factory, user_id, preferences=prefs)
    fake = _install_probe(monkeypatch, ProbeOutcome(verdict=VERDICT_QUALIFIED, context_window_tokens=DECLARED))
    fake.block = True
    spy = _install_counting_lock(user_id)
    try:
        async with env.client() as client:
            task = asyncio.create_task(
                client.post(
                    "/settings/presets/p-drift/activate",
                    headers={"x-test-user": user_id},
                )
            )
            await asyncio.wait_for(fake.started.wait(), timeout=5)
            # 探测进行中，另一个请求改写预设的模型与 host
            async with env.session_factory() as session:
                row = (
                    await session.execute(select(Settings).where(Settings.user_id == user_id))
                ).scalar_one()
                blob = json.loads(row.preferences)
                blob["api_presets"]["presets"][0]["config"]["llm_model"] = UNKNOWN_MODEL
                blob["api_presets"]["presets"][0]["config"]["api_base_url"] = OTHER_GATEWAY
                row.preferences = json.dumps(blob, ensure_ascii=False)
                await session.commit()
            fake.release.set()
            resp = await asyncio.wait_for(task, timeout=10)
    finally:
        db_write_locks.pop(user_id, None)

    assert resp.status_code == 409, resp.text
    assert resp.json()["code"] == PRESET_CHANGED
    assert spy.acquisitions == 2, "冷探测缓存写入 + 激活锁；CAS 在锁内失败"

    row = await read_row(env.session_factory, user_id)
    assert _main_fields(row) == MAIN_FIELDS, "CAS 失败后主字段不得被写入"
    blob = json.loads(row.preferences)
    assert blob["api_presets"]["presets"][0]["is_active"] is False


# ========== 5. 两步出口：先声明（预设自己的三元组），再激活 ==========


@pytest.mark.anyio
async def test_declaration_exit_then_activation_succeeds(env, monkeypatch):
    user_id = "u-preset-exit"
    presets = [
        preset("p-other", preset_config(model=QUALIFIED_MODEL), active=True),
        preset("p-exit", preset_config(model=UNKNOWN_MODEL)),
    ]
    prefs = {"api_presets": {"presets": presets, "version": "1.0"}}
    await seed_settings(env.session_factory, user_id, preferences=prefs)
    fake = _install_probe(monkeypatch, ProbeOutcome(verdict=VERDICT_INCONCLUSIVE, detail="gateway unreachable"))

    first = await post_activate(env, user_id, "p-exit")
    assert first.status_code == 400, first.text
    assert first.json()["code"] == BELOW_MINIMUM
    assert first.json()["params"]["requires_explicit_declaration"] is True

    # 声明必须带**预设自己的** provider/key/base_url/model，否则缓存的是别的三元组
    declared_payload = {
        "api_provider": "openai",
        "api_key": API_KEY,
        "api_base_url": GATEWAY,
        "llm_model": UNKNOWN_MODEL,
        "temperature": 0.5,
        "max_tokens": 1234,
        "system_prompt": "preset prompt",
        "context_window_tokens": DECLARED,
    }
    second = await post_settings(env, user_id, declared_payload)
    assert second.status_code == 200, second.text

    mid = await read_row(env.session_factory, user_id)
    assert mid.llm_model == UNKNOWN_MODEL, "声明保存这一步会把该模型设为默认"
    mid_blob = json.loads(mid.preferences)
    mid_presets = {p["id"]: p for p in mid_blob["api_presets"]["presets"]}
    assert mid_presets["p-other"]["is_active"] is False, "手动保存会关掉当前激活预设"
    assert mid_presets["p-exit"]["is_active"] is False
    declared_key = triple_key("openai", GATEWAY, UNKNOWN_MODEL)
    declared = mid_blob[PREFERENCES_KEY][declared_key]
    assert declared["result"] == VERDICT_QUALIFIED
    assert declared["source"] == SOURCE_USER_DECLARED, "声明必须落在预设自己的三元组上"

    third = await post_activate(env, user_id, "p-exit")
    assert third.status_code == 200, third.text
    final = await read_row(env.session_factory, user_id)
    final_presets = {p["id"]: p for p in json.loads(final.preferences)["api_presets"]["presets"]}
    assert final.llm_model == UNKNOWN_MODEL
    assert final_presets["p-exit"]["is_active"] is True
    assert final_presets["p-other"]["is_active"] is False


# ========== 6. 空模型预设跳过闸门 ==========


@pytest.mark.anyio
async def test_empty_model_preset_skips_the_gate(env, monkeypatch):
    user_id = "u-preset-empty"
    presets = [preset("p-empty", preset_config(model=""))]
    prefs = {"api_presets": {"presets": presets, "version": "1.0"}}
    await seed_settings(env.session_factory, user_id, preferences=prefs)
    fake = _install_probe(monkeypatch, ProbeOutcome(verdict=VERDICT_UNQUALIFIED))

    resp = await post_activate(env, user_id, "p-empty")

    assert resp.status_code == 200, resp.text
    assert fake.calls == [], "空模型没有可判定的三元组，不应探测"
    row = await read_row(env.session_factory, user_id)
    assert row.llm_model == ""


# ========== 7. 探测总时限：挂起网关必须在常数内返回 inconclusive ==========


@pytest.mark.anyio
async def test_probe_returns_inconclusive_within_the_total_deadline(monkeypatch):
    import httpx

    async def _hang(_request):
        await asyncio.sleep(5)
        return httpx.Response(200, json={})

    monkeypatch.setattr(probe_module, "PROBE_TOTAL_DEADLINE_SECONDS", 0.2)
    client = httpx.AsyncClient(transport=httpx.MockTransport(_hang))
    try:
        outcome = await probe_module.probe_model_context_window(
            provider="openai",
            base_url=GATEWAY,
            api_key=API_KEY,
            model=UNKNOWN_MODEL,
            trigger="save",
            client=client,
        )
    finally:
        await client.aclose()

    assert outcome.verdict == VERDICT_INCONCLUSIVE
    assert "deadline" in (outcome.detail or ""), outcome.detail

