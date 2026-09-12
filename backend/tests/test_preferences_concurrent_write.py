"""preferences 读改写并发回归测试（[Bug] #56，伞形需求 #55 步骤 1 前置修复）。

背景：`Settings.preferences` 是 `Column(Text)` 存 JSON 字符串（不是 JSON 列），
所有写入方都是「整读 -> merge -> json.dumps -> 整写」。两个写入方并发时，
后写者会带着自己的陈旧快照提交，把前者刚写入的键整个抹掉——
后台探测写入与用户保存设置互相覆盖整个 blob（语言偏好与 api_presets 已在互抹）。

本文件断言：并发写入必须串行化，两侧键都要存活。

覆盖：
- 并发往返：request 风格写入（PUT /settings/preferences 写 language）与
  后台风格写入（POST /settings/presets 写 api_presets，独立会话）竞态同一用户，
  断言两侧键都存活（未加锁时为 lost update）
- 互斥证明：往锁注册表里预置一把可计数的 Lock，断言两个写入方都取了锁，
  且临界区持有者数始终 <= 1（不依赖概率）
- 单一注册表：preferences 写入方与章节后台写入方共用同一个 get_db_write_lock
  与同一个锁字典，否则两条写入链互不互斥
- 逐个挂锁：八个 preferences 写入路径各取一次 per-user 写锁，
  漏挂任何一个调用点即红（只修一处不叫修好）
- 陈旧读：写入方会话若在取锁之前读过该行，锁内的重读仍必须以其他会话
  已提交的 blob 为合并基线（本机 SQLite 下 populate_existing 与否都成立，
  这条钉的是跨驱动契约与 `refresh=True` 接线，不是机制敏感性）
- 全部端点走真实 ASGI 栈（get_db 每请求一个会话），贴近生产并发形态

测试值一律中性占位（theme_seed/preset-0/sk-stub-*），不含任何导入原文、
角色人名或书名（AGENTS.md 原文数据脱敏硬约束）。
"""
import asyncio
import json
import os
import uuid
from types import SimpleNamespace

import pytest
from fastapi import FastAPI, Request
from httpx import ASGITransport, AsyncClient
from sqlalchemy import event, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import app.models  # noqa: F401 - 注册全部表（settings 建表需要）
import app.api.settings as settings_module
from app.api.chapters import get_db_write_lock as get_chapters_db_write_lock
from app.api.settings import router as settings_router, get_user_settings, update_preferences
from app.api.settings import (
    activate_preset,
    create_preset,
    delete_preset,
    save_settings,
    set_chapter_analysis_preset_selection,
    update_preset,
    update_settings,
)
from app.core.db_write_lock import db_write_lock, db_write_locks, get_db_write_lock
from app.core.errors import register_exception_handlers
from app.database import Base, get_db
from app.models.settings import Settings
from app.schemas.settings import (
    APIKeyPresetConfig,
    ChapterAnalysisPresetSelectionRequest,
    PreferencesUpdate,
    PresetCreateRequest,
    PresetUpdateRequest,
    SettingsCreate,
    SettingsUpdate,
)

PREFERENCES_URL = "/settings/preferences"
PRESETS_URL = "/settings/presets"

# 竞态轮数：每轮一个独立用户 + 一次并发双写。取 25 是因为单轮撞窗概率不低
# 但非必然（变异自证：去掉取锁后第 1 轮即红），多轮把敏感性拉到接近必然。
ROUNDS = 25


class _Env:
    def __init__(self, session_factory, app: FastAPI):
        self.session_factory = session_factory
        self.app = app
        self.lock_keys: set[str] = set()

    def client(self) -> AsyncClient:
        return AsyncClient(transport=ASGITransport(app=self.app), base_url="http://test")

    async def seed(self, user_id: str, preferences: dict):
        """预置 Settings 行（含既有无关键），避免竞态双方走建行分支。"""
        self.lock_keys.add(user_id)
        async with self.session_factory() as session:
            session.add(Settings(
                user_id=user_id,
                api_provider="openai",
                api_key="sk-stub-not-a-real-key",
                api_base_url="https://gw.test/v1",
                llm_model="stub-model",
                temperature=0.7,
                max_tokens=2000,
                preferences=json.dumps(preferences, ensure_ascii=False),
            ))
            await session.commit()

    async def read_preferences(self, user_id: str) -> dict:
        """用独立会话回读数据库真值（不复用写入方会话的身份映射）。"""
        async with self.session_factory() as session:
            row = (await session.execute(
                select(Settings).where(Settings.user_id == user_id)
            )).scalar_one()
            return json.loads(row.preferences or "{}")

    async def save_language(self, user_id: str, language: str = "en"):
        """request 风格写入：用户在前端点「保存偏好」。"""
        async with self.client() as client:
            return await client.put(
                PREFERENCES_URL,
                json={"language": language},
                headers={"x-test-user": user_id},
            )

    async def write_preset(self, user_id: str, name: str):
        """后台风格写入：另一个写入方往同一个 blob 追加自己的键。"""
        async with self.client() as client:
            return await client.post(
                PRESETS_URL,
                json={
                    "name": name,
                    "description": "stub preset",
                    "config": {
                        "api_provider": "openai",
                        "api_key": "sk-stub-not-a-real-key",
                        "api_base_url": "https://gw.test/v1",
                        "llm_model": "stub-model",
                    },
                },
                headers={"x-test-user": user_id},
            )


@pytest.fixture
async def env():
    """临时文件 SQLite，会话配置对齐生产（expire_on_commit=False + WAL + busy_timeout）。"""
    db_path = f"/tmp/test_prefs_race_{uuid.uuid4().hex}.db"
    engine = create_async_engine(
        f"sqlite+aiosqlite:///{db_path}",
        connect_args={"check_same_thread": False, "timeout": 30.0},
    )

    @event.listens_for(engine.sync_engine, "connect")
    def _sqlite_pragmas(dbapi_conn, _record):  # pragma: no cover - 对齐 app.database.get_engine
        cursor = dbapi_conn.cursor()
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.execute("PRAGMA synchronous=NORMAL")
        cursor.execute("PRAGMA busy_timeout=30000")
        cursor.close()

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    session_factory = async_sessionmaker(bind=engine, expire_on_commit=False)

    app = FastAPI()
    register_exception_handlers(app)
    app.include_router(settings_router)

    async def override_get_db():
        async with session_factory() as session:
            yield session

    app.dependency_overrides[get_db] = override_get_db

    @app.middleware("http")
    async def fake_auth(request: Request, call_next):
        user_id = request.headers.get("x-test-user")
        if user_id:
            request.state.user = SimpleNamespace(user_id=user_id, is_admin=False)
        return await call_next(request)

    test_env = _Env(session_factory, app)
    yield test_env
    for user_id in test_env.lock_keys:
        db_write_locks.pop(user_id, None)
    app.dependency_overrides.clear()
    await engine.dispose()
    for suffix in ("", "-wal", "-shm"):
        path = db_path + suffix
        if os.path.exists(path):
            os.remove(path)


@pytest.mark.anyio
async def test_concurrent_preferences_writers_keep_both_keys(env: _Env):
    """并发双写同一用户：language 与 api_presets 两个键都必须存活（#56 lost update）。"""
    for round_index in range(ROUNDS):
        user_id = f"u-race-{round_index}"
        preset_name = f"preset-{round_index}"
        await env.seed(user_id, {"theme_seed": 7})

        language_resp, preset_resp = await asyncio.gather(
            env.save_language(user_id),
            env.write_preset(user_id, preset_name),
        )
        assert language_resp.status_code == 200, language_resp.text
        assert preset_resp.status_code == 200, preset_resp.text

        prefs = await env.read_preferences(user_id)
        assert prefs.get("language") == "en", (
            f"round {round_index}: 预设写入抹掉了 language 键（blob={prefs}）"
        )
        presets = prefs.get("api_presets", {}).get("presets", [])
        assert [p.get("name") for p in presets] == [preset_name], (
            f"round {round_index}: 偏好写入抹掉了 api_presets 键（blob={prefs}）"
        )
        assert prefs.get("theme_seed") == 7, (
            f"round {round_index}: 既有无关键丢失（blob={prefs}）"
        )


class _CountingLock(asyncio.Lock):
    """记录持有者峰值的 Lock：直接证明临界区串行，不依赖竞态概率。"""

    def __init__(self):
        super().__init__()
        self.acquisitions = 0
        self.holders = 0
        self.max_holders = 0

    async def acquire(self):  # type: ignore[override]
        acquired = await super().acquire()
        self.acquisitions += 1
        self.holders += 1
        self.max_holders = max(self.max_holders, self.holders)
        return acquired

    def release(self):  # type: ignore[override]
        self.holders -= 1
        return super().release()


@pytest.mark.anyio
async def test_both_preferences_writers_take_per_user_lock_exclusively(env: _Env):
    """两个写入方各取一次同一把 per-user 锁，且临界区不得重叠。"""
    user_id = "u-lock-exclusive"
    await env.seed(user_id, {"theme_seed": 7})
    spy_lock = _CountingLock()
    db_write_locks[user_id] = spy_lock  # 预置后由生产代码取用

    language_resp, preset_resp = await asyncio.gather(
        env.save_language(user_id),
        env.write_preset(user_id, "preset-spy"),
    )
    assert language_resp.status_code == 200, language_resp.text
    assert preset_resp.status_code == 200, preset_resp.text

    assert spy_lock.acquisitions == 2, "两个 preferences 写入方都必须获取 per-user 写锁"
    assert spy_lock.max_holders == 1, "临界区重叠：同一用户存在两个并发读改写"
    assert spy_lock.holders == 0, "临界区结束后锁必须已释放"


@pytest.mark.anyio
async def test_preferences_and_chapter_writers_share_one_lock_registry():
    """settings 与 chapters 必须共用同一把锁注册表，否则两条写入链互不互斥。"""
    assert settings_module.db_write_lock is db_write_lock, (
        "settings 写入方不得自带第二套写锁实现"
    )
    assert get_chapters_db_write_lock is get_db_write_lock, (
        "章节写锁与 preferences 写锁必须是同一个函数/同一个注册表"
    )

    user_id = "u-shared-registry"
    try:
        core_lock = await get_db_write_lock(user_id)
        chapters_lock = await get_chapters_db_write_lock(user_id)
        assert core_lock is chapters_lock is db_write_locks[user_id]
    finally:
        db_write_locks.pop(user_id, None)


@pytest.mark.anyio
async def test_locked_writer_merges_committed_blob_not_stale_session_read(env: _Env):
    """锁外读过一次的会话，锁内重读必须看到其他会话已提交的 blob（#56 合并基线）。"""
    user_id = "u-stale-read"
    await env.seed(user_id, {"theme_seed": 7})
    request_session = env.session_factory()
    env.lock_keys.add(user_id)

    try:
        # 取锁之前先读一次：身份映射与 WAL 读快照都停在「还没有 language」的版本
        await get_user_settings(user_id, request_session)

        # 另一个会话（后台写入方形态）提交新键
        async with env.session_factory() as background_session:
            row = (await background_session.execute(
                select(Settings).where(Settings.user_id == user_id)
            )).scalar_one()
            row.preferences = json.dumps({"theme_seed": 7, "language": "zh"})
            await background_session.commit()

        # 直接调用 endpoint 协程（与仓库既有测试同法）：合并基线必须是刚提交的 blob
        await update_preferences(
            PreferencesUpdate(content_language="en"),
            SimpleNamespace(user_id=user_id, is_admin=False),
            request_session,
        )

        prefs = await env.read_preferences(user_id)
        assert prefs == {"theme_seed": 7, "language": "zh", "content_language": "en"}, (
            f"锁内重读拿到陈旧 blob，后台写入的键被抹掉（blob={prefs}）"
        )
    finally:
        await request_session.close()


# ========== 每个写入方都必须挂锁（防漏挂）==========

PRESET_ID = "preset-x"


def _preset_config() -> APIKeyPresetConfig:
    return APIKeyPresetConfig(
        api_provider="openai",
        api_key="sk-stub-not-a-real-key",
        api_base_url="https://gw.test/v1",
        llm_model="stub-model",
    )


def _seed_blob(case: str) -> dict:
    """预设类用例需要一个已存在的未激活预设。"""
    blob: dict = {"theme_seed": 7}
    if case in {"update_preset", "delete_preset", "activate_preset", "chapter_analysis_preset"}:
        blob["api_presets"] = {
            "version": "1.0",
            "presets": [{
                "id": PRESET_ID,
                "name": "preset-x",
                "description": "stub preset",
                "is_active": False,
                "created_at": "2026-01-01T00:00:00",
                "config": _preset_config().model_dump(),
            }],
        }
    return blob


WRITE_CASES = [
    "save_settings", "update_settings", "update_preferences", "create_preset",
    "update_preset", "delete_preset", "activate_preset", "chapter_analysis_preset",
]


async def _invoke(case: str, user_id: str, session) -> None:
    user = SimpleNamespace(user_id=user_id, is_admin=False)
    if case == "save_settings":
        await save_settings(SettingsCreate(llm_model="stub-model"), user, session)
    elif case == "update_settings":
        await update_settings(SettingsUpdate(temperature=0.5), user, session)
    elif case == "update_preferences":
        await update_preferences(PreferencesUpdate(language="en"), user, session)
    elif case == "create_preset":
        await create_preset(
            PresetCreateRequest(name="preset-new", description="stub preset", config=_preset_config()),
            user, session,
        )
    elif case == "update_preset":
        await update_preset(PRESET_ID, PresetUpdateRequest(name="preset-renamed"), user, session)
    elif case == "delete_preset":
        await delete_preset(PRESET_ID, user, session)
    elif case == "activate_preset":
        await activate_preset(PRESET_ID, user, session)
    elif case == "chapter_analysis_preset":
        await set_chapter_analysis_preset_selection(
            ChapterAnalysisPresetSelectionRequest(preset_id=PRESET_ID), user, session
        )
    else:  # pragma: no cover - 防止用例名拼写错误静默通过
        raise AssertionError(f"unknown case {case!r}")


@pytest.mark.anyio
@pytest.mark.parametrize("case", WRITE_CASES)
async def test_every_preferences_writer_acquires_the_per_user_lock(env: _Env, case: str):
    """八个 preferences 写入路径每个都必须取一次 per-user 写锁（漏挂即红）。"""
    user_id = f"u-writer-{case}"
    await env.seed(user_id, _seed_blob(case))
    spy_lock = _CountingLock()
    db_write_locks[user_id] = spy_lock

    async with env.session_factory() as session:
        await _invoke(case, user_id, session)

    assert spy_lock.acquisitions == 1, f"{case}: 未取 per-user 写锁，读改写仍会抹键（#56）"
    assert spy_lock.max_holders == 1, f"{case}: 临界区状态异常"
    assert spy_lock.holders == 0, f"{case}: 锁未释放"
