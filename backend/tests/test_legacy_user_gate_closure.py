"""步骤 5（伞形需求 #55）：存量用户收口 —— 启动/首请求按缓存结论拒绝 + 弹出同一表单。

要钉住的事实（计划 §5 与 §3 的「两种缺结论」区分）：

1. **缓存结论已是 `<1M`** ⇒ 重启后首个 AI 请求**直接按缓存拒绝**：零网络、零 token、
   provider 一次都不被调用，并带上表单能渲染的三段数 params。
2. **从未有过结论**（老用户升级后首次遇到）⇒ **同步补测 ①② 再定论**。
   若端点其实 >=1M，本次请求必须**成功**，绝不能吃到一个「AI 坏了，重试就好」的
   非确定性失败。这条是本文件存在的核心理由 —— 它是
   「实现成『没有结论就拒』的偷懒版本」的照妖镜。
3. **已有结论、只是今天没复测** ⇒ 当场按现有结论执行，复测 fire-and-forget。
   合格结论那侧的同一形态由 `test_context_window_probe_gate.py::
   test_stale_verdict_uses_existing_conclusion_and_rechecks_in_background` 钉住；
   本文件补**不合格**结论那侧：拒绝必须当场发生，不能被后台复测拖住。
4. **「拒绝 + 弹出同一表单」的可寻址性**：设置页读取（`GET /settings`）必须把
   **已缓存**的结论以门禁表单能直接渲染的形态交出去，且**读取路径零探测**——
   页面渲染不是 AI 功能，§3 的同步补测只适用于真要跑 AI 功能那条路。

测试值一律中性占位（big-model / gw.test / "chapter one body text"），不含任何
导入原文、角色人名或书名（AGENTS.md 原文数据脱敏硬约束）。
"""
import asyncio
import json
import os
import uuid
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import httpx
import pytest
from fastapi import FastAPI, Request
from httpx import ASGITransport, AsyncClient
from sqlalchemy import event, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

import app.models  # noqa: F401 - 注册全部表（settings 建表需要）
import app.api.settings as settings_module
import app.services.model_capability_probe as probe_module
from app.core.errors import ApiError, register_exception_handlers
from app.database import Base, get_db
from app.models.settings import Settings
from app.services.ai_service import AIService
from app.services.model_capability_probe import (
    MIN_CONTEXT_WINDOW_TOKENS,
    PREFERENCES_KEY,
    ProbeOutcome,
    SOURCE_PROBE,
    VERDICT_QUALIFIED,
    VERDICT_UNQUALIFIED,
    is_due_for_daily_recheck,
    pending_recheck_tasks,
    triple_key,
)

BELOW_MINIMUM = "validation.ai_model_below_minimum"
QUALIFIED_MODEL = "big-model"
SMALL_MODEL = "gpt-4o-mini"          # 真实 128K 级模型（登记表内 128000）
GATEWAY = "https://gw.test/v1"
API_KEY = "sk-stub-not-a-real-key"
NEUTRAL_PROMPT = "chapter one body text"
METADATA_WINDOW = 1_048_576
SMALL_WINDOW = 128_000

_BOUND_REJECTION_BODY = (
    b'{"error":{"message":"This model\'s maximum context length is 128000 tokens"}}'
)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _days_ago_iso(days: int) -> str:
    return (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()


# ========== 离线 Mock 网关（探测唯一出网口；真出网即失败） ==========


class _Gateway:
    """① 档 `GET /models/<id>` 老实报窗口的假网关；记录每一次出网请求。"""

    def __init__(self, window_tokens=METADATA_WINDOW):
        self.calls = []
        self.window_tokens = window_tokens

    @property
    def total_calls(self):
        return len(self.calls)

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.calls.append({"path": request.url.path, "method": request.method, "body": request.content})

        if request.url.path.startswith("/v1/models/"):
            return httpx.Response(200, json={"context_length": self.window_tokens})

        if request.url.path.endswith("/chat/completions"):
            return httpx.Response(400, content=_BOUND_REJECTION_BODY)

        return httpx.Response(404, json={"error": {"message": "unhandled"}})


@pytest.fixture
def gateway(monkeypatch):
    """把探测专用客户端换成 MockTransport —— 探测路径永不真出网。"""
    gw = _Gateway()

    def _factory(transport=None):
        return httpx.AsyncClient(transport=httpx.MockTransport(gw.handler))

    probe_module.memo_clear()
    monkeypatch.setattr(probe_module, "create_probe_client", _factory)
    yield gw
    probe_module.memo_clear()


# ========== 离线 DB + 用户绑定服务 ==========


class _RecordingProvider:
    """记录每一次 provider 调用：断言「零调用」即证明不合格模型从未发出去。"""

    def __init__(self):
        self.calls = []

    async def generate(self, **kwargs):
        self.calls.append(kwargs)
        return {"content": "generated stub text", "finish_reason": "stop", "tool_calls": []}


@pytest.fixture
async def db_factory():
    db_path = f"/tmp/test_legacy_gate_{uuid.uuid4().hex}.db"
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


async def seed_settings(factory, user_id, *, preferences=None, llm_model=SMALL_MODEL):
    async with factory() as session:
        session.add(Settings(
            user_id=user_id,
            api_provider="openai",
            api_key=API_KEY,
            api_base_url=GATEWAY,
            llm_model=llm_model,
            temperature=0.7,
            max_tokens=2000,
            preferences=json.dumps(preferences or {}, ensure_ascii=False),
        ))
        await session.commit()


def _entry(verdict, tokens, *, checked_at=None):
    return {
        "result": verdict,
        "source": SOURCE_PROBE,
        "context_window_tokens": tokens,
        "tier": "metadata",
        "detail": "seeded before restart",
        "checked_at": checked_at or _now_iso(),
    }


def small_verdicts(**kwargs):
    """一条持久化的「实测 128K」结论，三元组与保存/派发路径同一口径。"""
    return {PREFERENCES_KEY: {
        triple_key("openai", GATEWAY, SMALL_MODEL): _entry(VERDICT_UNQUALIFIED, SMALL_WINDOW, **kwargs)
    }}


def _service(user_id, session, *, default_model=SMALL_MODEL):
    svc = AIService(
        api_provider="openai",
        api_key=API_KEY,
        api_base_url=GATEWAY,
        default_model=default_model,
        user_id=user_id,
        db_session=session,
        enable_mcp=False,
    )
    provider = _RecordingProvider()
    svc._openai_provider = provider
    svc._anthropic_provider = None
    svc._gemini_provider = None
    return svc, provider


async def read_blob(factory, user_id):
    async with factory() as session:
        row = (await session.execute(select(Settings).where(Settings.user_id == user_id))).scalar_one()
        return json.loads(row.preferences)


# ========== 1. 缓存结论 <1M ⇒ 重启后首请求当场拒绝（零网络） ==========


@pytest.mark.anyio
async def test_cached_small_model_refuses_first_request_after_restart_without_any_network(
    db_factory, gateway
):
    """存量用户已有 `<1M` 结论 ⇒ 重启后首个 AI 请求直接按缓存拒：不重新探测、不发请求。

    「重启」在单测里 = 清空进程内 memo（`_memo` 是进程局部的）+ 新会话 + 新 AIService，
    于是结论只能从数据库的 preferences blob 里读到，与升级后的真实进程一模一样。
    """
    user_id = "u-legacy-cached-small"
    await seed_settings(db_factory, user_id, llm_model=SMALL_MODEL, preferences=small_verdicts())

    probe_module.memo_clear()  # ← 「重启」：进程内 memo 归零
    rechecks_before = set(pending_recheck_tasks())
    async with db_factory() as session:
        svc, provider = _service(user_id, session)
        with pytest.raises(ApiError) as exc_info:
            await svc.generate_text(prompt=NEUTRAL_PROMPT)

    assert exc_info.value.code == BELOW_MINIMUM
    assert exc_info.value.params["model"] == SMALL_MODEL
    assert exc_info.value.params["verdict"] == VERDICT_UNQUALIFIED
    # 三段数原料：探测值 128000 / 采用值为空 / 声明不是放行通道
    assert exc_info.value.params["measured_context_window_tokens"] == SMALL_WINDOW
    assert exc_info.value.params["min_window"] == MIN_CONTEXT_WINDOW_TOKENS
    assert exc_info.value.params["requires_explicit_declaration"] is False
    assert provider.calls == [], "不合格模型居然真的被发出去了"
    assert gateway.total_calls == 0, (
        "已有当场结论时不该重新探测 —— 拒绝必须确定，不能取决于网关此刻是否可达"
    )
    assert set(pending_recheck_tasks()) == rechecks_before, "今天刚测过的结论不触发复测"


@pytest.mark.anyio
async def test_stale_cached_small_model_refuses_now_and_queues_background_recheck(
    db_factory, gateway, monkeypatch
):
    """已有结论只是今天没复测 ⇒ 本次照旧拒绝，复测扔后台（绝不 await）。

    证伪方式：若实现把复测 await 在本次请求上，`gateway.total_calls` 在抛出前就会 > 0；
    断言「当场拒 + 后台有任务」才钉住「日频非阻塞只适用于复测」。
    """
    user_id = "u-legacy-stale-small"
    stale_checked_at = _days_ago_iso(2)
    await seed_settings(
        db_factory, user_id, llm_model=SMALL_MODEL,
        preferences=small_verdicts(checked_at=stale_checked_at),
    )
    # 前提自证：这条结论确实已经过期（否则本用例什么都没测）
    assert is_due_for_daily_recheck(ProbeOutcome(
        verdict=VERDICT_UNQUALIFIED, context_window_tokens=SMALL_WINDOW, checked_at=stale_checked_at
    ))

    opened = []

    async def fake_session(user):
        session = db_factory()
        opened.append(session)
        return session

    monkeypatch.setattr(probe_module, "_open_user_session", fake_session)
    gateway.window_tokens = SMALL_WINDOW  # 后台复测拿到同一个现实：依旧不合格

    probe_module.memo_clear()
    rechecks_before = set(pending_recheck_tasks())
    async with db_factory() as session:
        svc, provider = _service(user_id, session)
        with pytest.raises(ApiError) as exc_info:
            await svc.generate_text(prompt=NEUTRAL_PROMPT)
        assert gateway.total_calls == 0, "拒绝发生在派发当场，不能被复测的网络请求挡在前面"

    queued = set(pending_recheck_tasks()) - rechecks_before
    assert queued, "过期结论必须排队后台复测（抓网关侧漂移）"
    await asyncio.gather(*queued)
    assert not (set(pending_recheck_tasks()) - rechecks_before)
    for session in opened:
        await session.close()

    # 复测真的落了缓存：checked_at 刷成今天（而不是原地不动的两天前）
    stored = (await read_blob(db_factory, user_id))[PREFERENCES_KEY][
        triple_key("openai", GATEWAY, SMALL_MODEL)]
    assert stored["result"] == VERDICT_UNQUALIFIED
    assert stored["checked_at"] != stale_checked_at
    assert not is_due_for_daily_recheck(ProbeOutcome(
        verdict=stored["result"], context_window_tokens=stored["context_window_tokens"],
        checked_at=stored["checked_at"],
    ))


# ========== 2. 从未有结论 + 端点真的 >=1M ⇒ 同步补测后必须成功 ==========


@pytest.mark.anyio
async def test_legacy_user_without_verdict_on_a_real_1m_endpoint_succeeds(db_factory, gateway):
    """反向验收：老用户升级后**没有**结论，而他的端点确实 >=1M ⇒ 首个请求必须成功。

    没有这条，「缺结论就拒」的误实现会一路绿灯通过 —— 而那等于给每个升级上来的用户
    在升级后的第一次 AI 调用上砸一个非确定性失败。
    """
    user_id = "u-legacy-never-probed"
    await seed_settings(
        db_factory, user_id, llm_model=QUALIFIED_MODEL, preferences={"theme_seed": 11}
    )
    key = triple_key("openai", GATEWAY, QUALIFIED_MODEL)

    probe_module.memo_clear()
    async with db_factory() as session:
        svc, provider = _service(user_id, session, default_model=QUALIFIED_MODEL)
        result = await svc.generate_text(prompt=NEUTRAL_PROMPT)

    assert result["content"] == "generated stub text", "合格端点被拦下了：缺结论被当成了拒绝理由"
    assert provider.calls and provider.calls[0]["model"] == QUALIFIED_MODEL
    assert gateway.total_calls == 1, "首次定论要同步补测；① 档已给判据时不该再打 ② 档"

    blob = await read_blob(db_factory, user_id)
    assert blob["theme_seed"] == 11, "结论缓存写入抹掉了无关的偏好键"
    stored = blob[PREFERENCES_KEY][key]
    assert stored["result"] == VERDICT_QUALIFIED
    assert stored["context_window_tokens"] == METADATA_WINDOW
    assert stored["checked_at"]

    # 第二次派发读缓存即可：热路径零网络
    probe_module.memo_clear()
    async with db_factory() as session:
        svc, provider = _service(user_id, session, default_model=QUALIFIED_MODEL)
        await svc.generate_text(prompt=NEUTRAL_PROMPT)
    assert gateway.total_calls == 1, "已有合格结论后仍去探测 = 每次派发都多一次往返"
    assert provider.calls[-1]["model"] == QUALIFIED_MODEL


# ========== 3. 设置页读取 = 表单的落点（零探测） ==========


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

    yield _Env(db_factory, app)
    app.dependency_overrides.clear()


@pytest.mark.anyio
async def test_settings_read_surfaces_the_cached_below_minimum_conclusion(env, gateway):
    """「拒绝 + 弹出同一表单」要有落点：已缓存的 `<1M` 结论必须能从设置页读出来，
    并且**一次网络都不发**（页面渲染不是 AI 功能）。"""
    user_id = "u-legacy-read-surface"
    await seed_settings(env.session_factory, user_id, preferences=small_verdicts())
    probe_module.memo_clear()

    async with env.client() as client:
        resp = await client.get("/settings", headers={"x-test-user": user_id})

    assert resp.status_code == 200, resp.text
    gate = resp.json()["context_window_gate"]
    assert gate is not None, "存量不合格配置在设置页不可见 ⇒ 用户只知道「AI 坏了」"
    assert gate["model"] == SMALL_MODEL
    assert gate["verdict"] == VERDICT_UNQUALIFIED
    assert gate["min_window"] == MIN_CONTEXT_WINDOW_TOKENS
    assert gate["measured_context_window_tokens"] == SMALL_WINDOW
    assert gate["requires_explicit_declaration"] is False
    assert gate["due_for_recheck"] is False
    assert gateway.total_calls == 0, "设置页读取绝不发探测请求"


@pytest.mark.anyio
async def test_settings_read_reports_no_conclusion_for_a_never_probed_legacy_user(env, gateway):
    """从未有结论 ⇒ 读取端点是 `None`（而不是「不合格」）：补测发生在派发路径。"""
    user_id = "u-legacy-read-absent"
    await seed_settings(env.session_factory, user_id, preferences={"theme_seed": 1})
    probe_module.memo_clear()

    async with env.client() as client:
        resp = await client.get("/settings", headers={"x-test-user": user_id})

    assert resp.status_code == 200, resp.text
    assert resp.json()["context_window_gate"] is None
    assert gateway.total_calls == 0


@pytest.mark.anyio
async def test_settings_read_gate_state_shares_the_dispatch_paths_triple_key(env, gateway):
    """读取端点与派发路径必须算出**同一个**三元组键，否则缓存永远命不中，
    存量用户在设置页永远看到「未检测」。"""
    user_id = "u-legacy-key-parity"
    await seed_settings(env.session_factory, user_id, llm_model=QUALIFIED_MODEL, preferences={})
    probe_module.memo_clear()

    # 先经派发路径首次定论（真实用户的顺序就是：升级 -> 用一次 AI）
    async with env.session_factory() as session:
        svc, _ = _service(user_id, session, default_model=QUALIFIED_MODEL)
        await svc.generate_text(prompt=NEUTRAL_PROMPT)
    assert gateway.total_calls == 1

    probe_module.memo_clear()
    async with env.client() as client:
        resp = await client.get("/settings", headers={"x-test-user": user_id})

    gate = resp.json()["context_window_gate"]
    assert gate["verdict"] == VERDICT_QUALIFIED, "派发写入的结论设置页读不到 ⇒ 三元组口径漂移"
    assert gate["measured_context_window_tokens"] == METADATA_WINDOW
    assert gateway.total_calls == 1, "读取路径只读缓存，不该再探一次"
