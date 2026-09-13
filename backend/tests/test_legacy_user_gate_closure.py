"""步骤 5（伞形需求 #55）：存量用户收口 —— 启动/首请求按缓存结论拒绝 + 弹出同一表单。

要钉住的事实（计划 §5 与 §3 的「两种缺结论」区分）：

1. **缓存结论已是 `<1M`** ⇒ 重启后首个 AI 请求**直接按缓存拒绝**：零网络、零 token、
   provider 一次都不被调用，并带上表单能渲染的三段数 params。
2. **从未有过结论**（老用户升级后首次遇到）⇒ **同步补测 ①② 再定论**。
   若端点其实 >=1M，本次请求必须**成功**，绝不能吃到一个「AI 坏了，重试就好」的
   非确定性失败。这条是本文件存在的核心理由 —— 它是
   「实现成『没有结论就拒』的偷懒版本」的照妖镜。
3. **已有定论（qualified/unqualified）** ⇒ 当场按现有结论执行，**零网络、永不自动
   复测**（Rule R）。合格结论那侧由 `test_context_window_probe_gate.py::
   test_settled_qualified_verdict_is_never_reprobed` 钉住；本文件补**不合格**结论
   那侧：拒绝必须当场发生，不取决于网关此刻是否可达。
   与之相对，**缓存结论是 `inconclusive`** 时不是结论 ⇒ 派发时同步复探（R1）。
4. **「拒绝 + 弹出同一表单」的可寻址性**：设置页读取（`GET /settings`）必须把
   **已缓存**的结论以门禁表单能直接渲染的形态交出去，且**读取路径零探测**——
   页面渲染不是 AI 功能，§3 的同步补测只适用于真要跑 AI 功能那条路。
5. **缓存结论是持久状态，一次「没测出来」抹不掉它**（issue #59）：网关不可达时
   探测回 `inconclusive` 是**非测量**，不是新证据。它既不能把实测 `<1M` 降级成
   「去声明一个窗口」——那正好是实测结论本该挡住的出口——也不能把实测 `>=1M`
   或用户声明抹成未知。强度序见 `evidence_strength` / `verdict_may_overwrite`。

测试值一律中性占位（big-model / gw.test / "chapter one body text"），不含任何
导入原文、角色人名或书名（AGENTS.md 原文数据脱敏硬约束）。
"""
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
    SOURCE_USER_DECLARED,
    VERDICT_INCONCLUSIVE,
    VERDICT_QUALIFIED,
    VERDICT_UNQUALIFIED,
    evidence_strength,
    triple_key,
    verdict_may_overwrite,
)

BELOW_MINIMUM = "validation.ai_model_below_minimum"
QUALIFIED_MODEL = "big-model"
SMALL_MODEL = "gpt-4o-mini"          # 真实 128K 级模型（登记表内 128000）
UNKNOWN_MODEL = "mystery-model"      # 未登记，只能靠用户显式声明
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
    """① 档 `GET /models/<id>` 老实报窗口的假网关；记录每一次出网请求。

    `unreachable = True` 时**抛连接错误**，与真实场景「网关进程已被杀掉」同形态：
    探测层把任何传输异常判成 `inconclusive`，HTTP 端点仍回 200。
    """

    def __init__(self, window_tokens=METADATA_WINDOW):
        self.calls = []
        self.window_tokens = window_tokens
        self.unreachable = False

    @property
    def total_calls(self):
        return len(self.calls)

    def handler(self, request: httpx.Request) -> httpx.Response:
        if self.unreachable:
            raise httpx.ConnectError("connection refused", request=request)
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
async def test_settled_below_minimum_verdict_refuses_without_reprobe(db_factory, gateway):
    """Rule R：`unqualified` 是定论 ⇒ 重启后首个请求直接按缓存拒：不重探、不发请求。

    「重启」在单测里 = 清空进程内 memo（`_memo` 与重测冷却都是进程局部的）+ 新会话 +
    新 AIService，于是结论只能从数据库的 preferences blob 里读到，与升级后的真实进程
    一模一样。结论即使很久以前测的也**永不自动复测**（原日频复测已删除）。
    """
    user_id = "u-legacy-cached-small"
    await seed_settings(
        db_factory, user_id, llm_model=SMALL_MODEL,
        preferences=small_verdicts(checked_at=_days_ago_iso(30)),
    )

    probe_module.memo_clear()  # ← 「重启」：进程内 memo 与冷却归零
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
        "已定论永不复测 —— 拒绝必须确定，不能取决于网关此刻是否可达"
    )


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


# ========== 5. 结论强度序：非测量抹不掉持久状态（issue #59） ==========
#
# 真机浏览器验收抓到的回归：设置页加载时那一枪静默探测打到不可达的网关，
# 后端**照实回 200 + inconclusive**（不是抛异常），而 `write_verdict` 把它落了库，
# 于是「实测 128,000」这条唯一能解释「为什么 AI 被拒」的证据被一次「没测出来」抹掉，
# 界面上一个真实测量过的 128K 用户被永久降级成「你去声明一个窗口」——
# 而显式声明恰恰是实测 `<1M` 本该挡住的出口。
# 这里刻意走 `POST /settings/check-context-window` 全路径：jsdom 那侧的旧用例把
# 不可达 mock 成了传输层抛异常，正好绕开了后端真正的状态丢失形态。


def _verdict_blob(entry_key, entry, **extra):
    """把一条结论按缓存真实形状包成 preferences blob。"""
    return {PREFERENCES_KEY: {entry_key: entry}, **extra}


async def run_silent_page_probe(env, client_user_id, *, model=SMALL_MODEL):
    """复刻设置页的**手动**「重新检测」（POST /settings/check-context-window）。"""
    async with env.client() as client:
        return await client.post(
            "/settings/check-context-window",
            headers={"x-test-user": client_user_id},
            json={
                "provider": "openai",
                "api_key": API_KEY,
                "api_base_url": GATEWAY,
                "llm_model": model,
            },
        )


@pytest.mark.anyio
async def test_unreachable_page_probe_does_not_erase_a_measured_below_minimum_verdict(
    env, gateway
):
    """核心回归：网关不可达 ⇒ 已缓存的「实测 128K」必须原样留在库里。"""
    user_id = "u-state-loss-primary"
    key = triple_key("openai", GATEWAY, SMALL_MODEL)
    seeded = small_verdicts()
    original = dict(seeded[PREFERENCES_KEY][key])
    await seed_settings(env.session_factory, user_id, preferences=seeded)
    probe_module.memo_clear()

    gateway.unreachable = True
    resp = await run_silent_page_probe(env, user_id)
    assert resp.status_code == 200, resp.text
    # 前提自证：这一枪确实「判不出」，而且不是靠抛异常表达的（HTTP 200 形态）
    body = resp.json()
    assert body["success"] is True
    assert body["details"]["verdict"] == VERDICT_INCONCLUSIVE
    assert gateway.total_calls == 0, "不可达即一次请求都没成行，缓存基线只能来自数据库"

    probe_module.memo_clear()
    stored = (await read_blob(env.session_factory, user_id))[PREFERENCES_KEY][key]
    assert stored["result"] == VERDICT_UNQUALIFIED, "非测量抹掉了实测结论：持久状态丢失"
    assert stored["context_window_tokens"] == original["context_window_tokens"]
    assert stored["source"] == SOURCE_PROBE
    assert stored["checked_at"] == original["checked_at"], "结论的测量时间被改写"

    # 拒绝形态仍是「实测低于下限」，而不是「请去声明一个窗口」
    async with env.client() as client:
        gate = (
            await client.get("/settings", headers={"x-test-user": user_id})
        ).json()["context_window_gate"]
    assert gate["verdict"] == VERDICT_UNQUALIFIED
    assert gate["measured_context_window_tokens"] == SMALL_WINDOW
    assert gate["requires_explicit_declaration"] is False, "被实测挡住的出口又开了"


@pytest.mark.anyio
async def test_save_gate_after_an_unreachable_probe_still_refuses_with_measured_evidence(
    env, gateway
):
    """同一状态丢失误动的下游后果：保存必须回「实测不足」，不是「要求显式声明」。"""
    user_id = "u-state-loss-save-path"
    await seed_settings(env.session_factory, user_id, preferences=small_verdicts())
    probe_module.memo_clear()

    gateway.unreachable = True
    page_probe = await run_silent_page_probe(env, user_id)
    assert page_probe.json()["details"]["verdict"] == VERDICT_INCONCLUSIVE

    async with env.client() as client:
        resp = await client.post(
            "/settings",
            headers={"x-test-user": user_id},
            json={
                "api_provider": "openai",
                "api_key": API_KEY,
                "api_base_url": GATEWAY,
                "llm_model": SMALL_MODEL,
                "context_window_tokens": 2_000_000,
            },
        )

    assert resp.status_code == 400, resp.text
    params = resp.json()["params"]
    assert resp.json()["code"] == BELOW_MINIMUM
    assert params["verdict"] == VERDICT_UNQUALIFIED
    assert params["measured_context_window_tokens"] == SMALL_WINDOW
    assert params["requires_explicit_declaration"] is False, "声明通道对实测小模型重新打开"
    assert gateway.total_calls == 0, "已有当场结论时拒绝必须确定，不取决于网关此刻可达与否"


@pytest.mark.anyio
async def test_dispatch_inconclusive_is_not_persisted(db_factory, gateway):
    """R2：派发期判不出的结论**不落库** —— preferences 列逐字节不变。

    落库会让宕机期间每个 AI 请求都去抢不可重入写锁、整读整写 preferences 并 commit；
    而 R1 已经保证「没有结论」的下一枪会重新探测，粘不住。
    """
    user_id = "u-dispatch-inconclusive"
    seeded = {"theme_seed": 7}
    await seed_settings(db_factory, user_id, preferences=seeded)
    gateway.unreachable = True

    probe_module.memo_clear()
    async with db_factory() as session:
        svc, _ = _service(user_id, session)
        with pytest.raises(ApiError) as exc_info:
            await svc.generate_text(prompt=NEUTRAL_PROMPT)

    assert exc_info.value.code == BELOW_MINIMUM
    assert exc_info.value.params["verdict"] == VERDICT_INCONCLUSIVE
    assert gateway.total_calls == 0, "不可达网关一次请求都没成行"

    async with db_factory() as session:
        row = (await session.execute(
            select(Settings).where(Settings.user_id == user_id)
        )).scalar_one()
    assert json.loads(row.preferences) == seeded, "派发期 inconclusive 改写了 preferences 列"


@pytest.mark.anyio
async def test_dispatch_reprobe_is_throttled_in_process(db_factory, monkeypatch):
    """R3：冷却窗口内第二次派发不再打网关（先占坑再 await）。

    串行两次只能证冷却确实存在；同一 tick 的**并发**去重由
    `test_context_window_probe_gate.py::test_concurrent_dispatch_reprobes_are_deduplicated` 证。
    """
    user_id = "u-dispatch-cooldown"
    await seed_settings(db_factory, user_id, preferences={"theme_seed": 7})
    probes = []

    async def fake_probe(**kwargs):
        probes.append(kwargs)
        return ProbeOutcome(
            verdict=VERDICT_INCONCLUSIVE, source=SOURCE_PROBE,
            tier="metadata", detail="gateway down",
        )

    monkeypatch.setattr(probe_module, "probe_model_context_window", fake_probe)

    async def dispatch_once():
        async with db_factory() as session:
            svc, _ = _service(user_id, session)
            with pytest.raises(ApiError) as exc_info:
                await svc.generate_text(prompt=NEUTRAL_PROMPT)
            return exc_info.value

    probe_module.memo_clear()
    first = await dispatch_once()
    assert first.params["verdict"] == VERDICT_INCONCLUSIVE
    assert len(probes) == 1, "首次派发必须探测一次"

    second = await dispatch_once()
    assert second.params["verdict"] == VERDICT_INCONCLUSIVE
    assert len(probes) == 1, "冷却窗口内第二次派发不得再探（占坑必须先于 await）"


@pytest.mark.anyio
async def test_fresh_measured_verdict_replaces_an_older_measured_one(env, db_factory, gateway):
    """实测覆盖实测（同强度，新的赢）：网关侧真把窗口升到 >=1M，用户手动重测后必须被重新接纳。

    自动复测已删除 ⇒ 这条改由**用户显式动作**（手动「重新检测」）驱动；它仍是
    「强度序不会把用户永久锁死」的唯一证明（不许删）。派发侧只认已定论：旧 unqualified
    会一直拒绝，直到这次手动重测把新实测写回去。
    """
    user_id = "u-state-loss-upgrade"
    await seed_settings(
        db_factory, user_id,
        preferences=small_verdicts(checked_at=_days_ago_iso(2)),
    )
    gateway.window_tokens = METADATA_WINDOW  # 网关真的升配了

    resp = await run_silent_page_probe(env, user_id)
    assert resp.status_code == 200, resp.text
    assert resp.json()["details"]["verdict"] == VERDICT_QUALIFIED

    probe_module.memo_clear()
    async with db_factory() as session:
        svc, provider = _service(user_id, session)
        result = await svc.generate_text(prompt=NEUTRAL_PROMPT)
    assert result["content"] == "generated stub text", "升配后的模型没能被重新接纳"
    assert provider.calls, "实测覆盖实测是手动重测的正常形态"

    stored = (await read_blob(db_factory, user_id))[PREFERENCES_KEY][
        triple_key("openai", GATEWAY, SMALL_MODEL)]
    assert stored["result"] == VERDICT_QUALIFIED
    assert stored["context_window_tokens"] == METADATA_WINDOW


@pytest.mark.anyio
async def test_first_ever_inconclusive_probe_is_recorded_and_later_probe_can_replace_it(
    env, gateway
):
    """从未定论 + 网关不可达 ⇒ **手动**重测判不出时它仍须落库（save/manual 要渲染表单），
    且它绝不能把用户永久钉在未知上：网关恢复后重测可覆盖它。"""
    user_id = "u-state-loss-cold-start"
    key = triple_key("openai", GATEWAY, SMALL_MODEL)
    await seed_settings(env.session_factory, user_id, preferences={})
    probe_module.memo_clear()

    gateway.unreachable = True
    resp = await run_silent_page_probe(env, user_id)
    assert resp.json()["details"]["verdict"] == VERDICT_INCONCLUSIVE
    stored = (await read_blob(env.session_factory, user_id))[PREFERENCES_KEY][key]
    assert stored["result"] == VERDICT_INCONCLUSIVE, "手动重测的首个判不出必须落库（表单要渲染）"

    gateway.unreachable = False
    gateway.window_tokens = SMALL_WINDOW  # 网关恢复后如实报告 128K ⇒ 这次是实测
    resp2 = await run_silent_page_probe(env, user_id)
    assert resp2.json()["details"]["verdict"] == VERDICT_UNQUALIFIED, "弱证据落库后强证据进不来"
    stored = (await read_blob(env.session_factory, user_id))[PREFERENCES_KEY][key]
    assert stored["result"] == VERDICT_UNQUALIFIED
    assert stored["context_window_tokens"] == SMALL_WINDOW


@pytest.mark.anyio
async def test_unreachable_manual_recheck_does_not_erase_an_explicit_declaration(
    env, db_factory, gateway
):
    """用户显式声明的结论同样是持久状态：一次判不出的**手动重测**不能把它抹回未知。"""
    user_id = "u-state-loss-declared"
    key = triple_key("openai", GATEWAY, UNKNOWN_MODEL)
    await seed_settings(
        db_factory,
        user_id,
        llm_model=UNKNOWN_MODEL,
        preferences=_verdict_blob(key, {
            "result": VERDICT_QUALIFIED,
            "source": SOURCE_USER_DECLARED,
            "context_window_tokens": METADATA_WINDOW,
            "tier": None,
            "detail": f"user declared {METADATA_WINDOW} tokens",
            "checked_at": _days_ago_iso(4),
        }, theme_seed=7),
    )
    gateway.unreachable = True

    probe_module.memo_clear()
    resp = await run_silent_page_probe(env, user_id, model=UNKNOWN_MODEL)
    assert resp.status_code == 200, resp.text
    assert resp.json()["details"]["verdict"] == VERDICT_INCONCLUSIVE

    blob = await read_blob(db_factory, user_id)
    assert blob["theme_seed"] == 7, "拒绝弱证据时的补写抹掉了无关偏好键"
    stored = blob[PREFERENCES_KEY][key]
    assert stored["source"] == SOURCE_USER_DECLARED
    assert stored["result"] == VERDICT_QUALIFIED

    # 声明仍是持久状态：下一次派发照常成功（不会被一次判不出抹成永久拒绝）
    probe_module.memo_clear()
    async with db_factory() as session:
        svc, provider = _service(user_id, session, default_model=UNKNOWN_MODEL)
        result = await svc.generate_text(prompt=NEUTRAL_PROMPT)
    assert result["content"] == "generated stub text", "声明被抹掉后，正常派发吃到了非确定性失败"
    assert provider.calls


def test_evidence_strength_orders_non_measurement_below_declaration_below_measurement():
    """强度序本身要可寻址：非测量 < 声明 < 实测，实测之间只看新旧。"""
    measured_small = ProbeOutcome(verdict=VERDICT_UNQUALIFIED, context_window_tokens=SMALL_WINDOW)
    measured_big = ProbeOutcome(verdict=VERDICT_QUALIFIED, context_window_tokens=METADATA_WINDOW)
    declared = ProbeOutcome(
        verdict=VERDICT_QUALIFIED,
        context_window_tokens=METADATA_WINDOW,
        source=SOURCE_USER_DECLARED,
    )
    cannot_decide = ProbeOutcome(verdict=VERDICT_INCONCLUSIVE)

    assert evidence_strength(cannot_decide) < evidence_strength(declared)
    assert evidence_strength(declared) < evidence_strength(measured_small)
    # 「升配要被重新接纳 / 降配要被重新拒绝」都依赖实测之间不分强弱
    assert evidence_strength(measured_small) == evidence_strength(measured_big)

    # 同强度即「新的赢」：这条就是日常复测
    assert verdict_may_overwrite(measured_big, measured_small)
    assert verdict_may_overwrite(measured_small, measured_big)
    assert verdict_may_overwrite(declared, measured_small)
    assert not verdict_may_overwrite(measured_small, declared)
    for stronger in (measured_small, measured_big, declared):
        assert not verdict_may_overwrite(stronger, cannot_decide)
        assert verdict_may_overwrite(cannot_decide, stronger)
