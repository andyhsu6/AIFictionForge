"""模型可用性探测端点测试（outline-model-400-fix plan todo 3）。

端点：POST /settings/models/probe（settings router 自带 prefix=/settings；
main.py 再挂 /api，本测试用裸 FastAPI app include_router，与 test_generation_language_e2e 同法）。
拦截点：monkeypatch app.api.settings 命名空间的 create_user_ai_service_with_mcp，
真实走 get_user_ai_service_from_db → resolve_runtime_ai_config 归一链（provider/base_url 语义与生成一致），
stub 的 generate_text_stream 不发任何真实网络请求、不含任何原文数据。

覆盖 plan 验收 (a)-(j)：成功/空正文 ok、http 分类+单行≤300+TTL300、network 分类+TTL60、
other 不缓存、缓存命中去重、并发同 key 仅一次上游调用、双用户隔离、
settings.updated_at 变更失效、commandcode 别名归一、auto_mcp 透传、422/401。
"""
import asyncio
import os
import time
import uuid
from datetime import datetime
from types import SimpleNamespace

import httpx
import pytest
from fastapi import FastAPI, Request
from httpx import ASGITransport, AsyncClient
from sqlalchemy import update
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import app.api.settings as settings_module
from app.api.settings import _MODEL_PROBE_CACHE, router as settings_router
from app.core.errors import register_exception_handlers
from app.database import Base, get_db
import app.models  # noqa: F401 - 注册全部表（含 MCPPlugin，get_user_ai_service_from_db 会查询）
from app.models.settings import Settings

PROBE_URL = "/settings/models/probe"
UPSTREAM_BASE = "https://gw.test/v1"
BODY_MARKER = "UPSTREAM_400_MARKER"


class _StubAIService:
    api_provider: str
    base_url: str

    def __init__(self, harness: "_Harness", user_id: str, created_kwargs: dict):
        self.harness = harness
        self.user_id = user_id
        self.api_provider = created_kwargs["api_provider"]
        self.base_url = created_kwargs["api_base_url"]

    async def generate_text_stream(self, **kwargs):
        self.harness.calls.append((self.user_id, kwargs.get("model")))
        self.harness.stream_kwargs.append(kwargs)
        behavior = self.harness.behaviors.get(self.user_id, "ok")
        if behavior == "ok":
            yield "pong"
        elif behavior == "empty":
            return
        elif behavior == "slow_ok":
            await asyncio.sleep(0.05)
            yield "pong"
        elif behavior == "http400":
            request = httpx.Request("POST", f"{self.base_url}/chat/completions")
            response = httpx.Response(
                400,
                request=request,
                json={"error": {"message": f"{BODY_MARKER} model 'ghost-model' does not exist\nline2\twith\x01ctrl"}},
            )
            raise httpx.HTTPStatusError(
                f"Client error '400 Bad Request' for url '{self.base_url}/chat/completions'",
                request=request,
                response=response,
            )
        elif behavior == "http400_huge":
            request = httpx.Request("POST", f"{self.base_url}/chat/completions")
            response = httpx.Response(
                400, request=request, json={"error": {"message": "E" * 1000}}
            )
            raise httpx.HTTPStatusError("Client error '400 Bad Request'", request=request, response=response)
        elif behavior == "network":
            raise httpx.ConnectError("All connection attempts failed\r\n[next line]")
        elif behavior == "timeout":
            raise httpx.ReadTimeout("timed out while connecting")
        elif behavior == "other":
            raise RuntimeError("provider not configured")
        else:  # pragma: no cover - 防止行为拼写错误静默通过
            raise AssertionError(f"unknown behavior {behavior!r}")


class _Harness:
    def __init__(self):
        self.behaviors: dict[str, str] = {}
        self.calls: list[tuple[str, str]] = []
        self.stream_kwargs: list[dict] = []
        self.created: list[dict] = []

    def fake_create(self, **kwargs):
        self.created.append(dict(kwargs))
        return _StubAIService(self, kwargs["user_id"], kwargs)


class _Env:
    def __init__(self, harness: _Harness, session_factory, app: FastAPI):
        self.harness = harness
        self.session_factory = session_factory
        self.app = app

    def client(self) -> AsyncClient:
        return AsyncClient(transport=ASGITransport(app=self.app), base_url="http://test")

    def calls_for(self, user_id: str) -> int:
        return sum(1 for uid, _ in self.harness.calls if uid == user_id)

    async def probe(self, user_id: str | None, body: dict):
        headers = {"x-test-user": user_id} if user_id else {}
        async with self.client() as client:
            return await client.post(PROBE_URL, json=body, headers=headers)

    async def seed(self, user_id: str, **overrides):
        row = Settings(
            user_id=user_id,
            api_provider="openai",
            api_key="sk-stub-not-a-real-key",
            api_base_url=UPSTREAM_BASE,
            llm_model="good-model",
            temperature=0.7,
            max_tokens=2000,
            updated_at=datetime(2026, 1, 1, 0, 0, 0),
        )
        for key, value in overrides.items():
            setattr(row, key, value)
        async with self.session_factory() as session:
            session.add(row)
            await session.commit()


@pytest.fixture
async def env(monkeypatch):
    db_path = f"/tmp/test_models_probe_{uuid.uuid4().hex}.db"
    engine = create_async_engine(f"sqlite+aiosqlite:///{db_path}")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    session_factory = async_sessionmaker(bind=engine, expire_on_commit=False)

    harness = _Harness()
    monkeypatch.setattr(settings_module, "create_user_ai_service_with_mcp", harness.fake_create)
    _MODEL_PROBE_CACHE.clear()

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

    yield _Env(harness, session_factory, app)
    app.dependency_overrides.clear()
    _MODEL_PROBE_CACHE.clear()
    await engine.dispose()
    if os.path.exists(db_path):
        os.remove(db_path)


# ========== (b) 成功路径 ==========

@pytest.mark.anyio
async def test_success_returns_ok_true(env: _Env):
    await env.seed("u-1")
    resp = await env.probe("u-1", {"model": "good-model"})
    assert resp.status_code == 200, resp.text
    assert resp.json() == {"ok": True, "model": "good-model", "detail": None, "error_class": None}
    kwargs = env.harness.stream_kwargs[0]
    assert kwargs["prompt"] == "ping"
    assert kwargs["max_tokens"] == 1
    assert kwargs["auto_mcp"] is True  # 默认与 outline 生成请求 enable_mcp=True 对齐


@pytest.mark.anyio
async def test_success_with_empty_content_still_ok(env: _Env):
    env.harness.behaviors["u-1"] = "empty"
    await env.seed("u-1")
    resp = await env.probe("u-1", {"model": "m-1"})
    body = resp.json()
    assert body["ok"] is True and body["error_class"] is None and body["detail"] is None


# ========== (a) http 分类 + detail 归一化 ==========

@pytest.mark.anyio
async def test_upstream_400_classified_http_single_line(env: _Env):
    env.harness.behaviors["u-1"] = "http400"
    await env.seed("u-1")
    resp = await env.probe("u-1", {"model": "ghost-model"})
    body = resp.json()
    assert body["ok"] is False
    assert body["error_class"] == "http"
    detail = body["detail"]
    assert BODY_MARKER in detail and "上游响应: " in detail  # 优先携带增强后的上游 body 文本
    assert len(detail) <= 300
    assert "\n" not in detail and "\r" not in detail and "\x01" not in detail and "\t" not in detail
    assert "  " not in detail  # 空白折叠为单空格


@pytest.mark.anyio
async def test_long_http_detail_truncated_to_300(env: _Env):
    env.harness.behaviors["u-1"] = "http400_huge"
    await env.seed("u-1")
    resp = await env.probe("u-1", {"model": "m-1"})
    assert len(resp.json()["detail"]) <= 300


# ========== 缓存语义 ==========

@pytest.mark.anyio
async def test_http_result_cached_within_ttl(env: _Env):
    env.harness.behaviors["u-1"] = "http400"
    await env.seed("u-1")
    first = (await env.probe("u-1", {"model": "m-1"})).json()
    second = (await env.probe("u-1", {"model": "m-1"})).json()
    assert first == second
    assert env.calls_for("u-1") == 1  # 第二次命中缓存
    entry = next(e for e in _MODEL_PROBE_CACHE.values() if e["state"] == "done")
    assert 0 < entry["expires_at"] - time.time() <= 300


@pytest.mark.anyio
async def test_network_classified_and_cached_60s(env: _Env):
    env.harness.behaviors["u-1"] = "network"
    await env.seed("u-1")
    resp = await env.probe("u-1", {"model": "m-1"})
    body = resp.json()
    assert body["ok"] is False and body["error_class"] == "network"
    assert "\r" not in body["detail"] and "\n" not in body["detail"]
    assert len(body["detail"]) <= 200
    entry = next(e for e in _MODEL_PROBE_CACHE.values() if e["state"] == "done")
    assert 0 < entry["expires_at"] - time.time() <= 60
    second = (await env.probe("u-1", {"model": "m-1"})).json()
    assert second == body
    assert env.calls_for("u-1") == 1  # 60s 内不重探


@pytest.mark.anyio
async def test_timeout_classified_network(env: _Env):
    env.harness.behaviors["u-1"] = "timeout"
    await env.seed("u-1")
    body = (await env.probe("u-1", {"model": "m-1"})).json()
    assert body["ok"] is False and body["error_class"] == "network"


@pytest.mark.anyio
async def test_other_error_not_cached(env: _Env):
    env.harness.behaviors["u-1"] = "other"
    await env.seed("u-1")
    first = (await env.probe("u-1", {"model": "m-1"})).json()
    assert first["ok"] is False and first["error_class"] == "other"
    assert len(first["detail"]) <= 200
    second = (await env.probe("u-1", {"model": "m-1"})).json()
    assert second == first
    assert env.calls_for("u-1") == 2  # other 不进缓存，重探


@pytest.mark.anyio
async def test_settings_updated_at_change_invalidates_cache(env: _Env):
    await env.seed("u-1")
    assert (await env.probe("u-1", {"model": "m-1"})).json()["ok"] is True
    assert env.calls_for("u-1") == 1
    async with env.session_factory() as session:
        await session.execute(
            update(Settings)
            .where(Settings.user_id == "u-1")
            .values(updated_at=datetime(2026, 1, 2, 0, 0, 0))
        )
        await session.commit()
    assert (await env.probe("u-1", {"model": "m-1"})).json()["ok"] is True
    assert env.calls_for("u-1") == 2  # updated_at 变 → key 变 → 缓存 miss


# ========== (g) 并发同 key 去重 ==========

@pytest.mark.anyio
async def test_concurrent_identical_probes_single_upstream_call(env: _Env):
    env.harness.behaviors["u-1"] = "slow_ok"
    await env.seed("u-1")
    first, second = await asyncio.gather(
        env.probe("u-1", {"model": "m-1"}),
        env.probe("u-1", {"model": "m-1"}),
    )
    assert first.json()["ok"] is True and second.json()["ok"] is True
    assert env.calls_for("u-1") == 1  # inflight Future 去重：仅 leader 打到上游


# ========== (d) 双用户隔离 ==========

@pytest.mark.anyio
async def test_two_users_same_model_isolated(env: _Env):
    env.harness.behaviors["u-a"] = "ok"
    env.harness.behaviors["u-b"] = "http400"
    await env.seed("u-a")
    await env.seed("u-b")  # 同 base_url、探测同 model，仅 user_id/updated_at 不同
    body_a = (await env.probe("u-a", {"model": "ghost-model"})).json()
    body_b = (await env.probe("u-b", {"model": "ghost-model"})).json()
    assert body_a["ok"] is True and body_a["error_class"] is None
    assert body_b["ok"] is False and body_b["error_class"] == "http"
    assert env.calls_for("u-a") == 1 and env.calls_for("u-b") == 1


# ========== (h) provider 别名归一与生成一致 ==========

@pytest.mark.anyio
async def test_commandcode_alias_normalized_like_generation(env: _Env):
    await env.seed("u-1", api_provider="commandcode")
    resp = await env.probe("u-1", {"model": "m-1", "provider": "commandcode"})
    assert resp.json()["ok"] is True
    created = env.harness.created[-1]
    assert created["api_provider"] == "openai"  # 服务端配置经 resolve 链归一（别名→openai）
    assert created["api_base_url"] == UPSTREAM_BASE
    kwargs = env.harness.stream_kwargs[-1]
    assert kwargs["provider"] == "commandcode"  # 请求 provider 原样透传给生成路径（内部再归一）
    key = next(iter(_MODEL_PROBE_CACHE))
    assert key[0] == "u-1" and key[1] == "openai" and key[2] == UPSTREAM_BASE  # 缓存 key 用归一后 provider


# ========== (i) auto_mcp 透传 ==========

@pytest.mark.anyio
async def test_enable_mcp_false_passed_through(env: _Env):
    await env.seed("u-1")
    await env.probe("u-1", {"model": "m-1", "enable_mcp": False})
    assert env.harness.stream_kwargs[-1]["auto_mcp"] is False
    assert env.harness.stream_kwargs[-1]["temperature"] == 0


# ========== (j) 校验与鉴权 ==========

@pytest.mark.anyio
@pytest.mark.parametrize("bad_model", ["", "x" * 201])
async def test_invalid_model_422(env: _Env, bad_model: str):
    await env.seed("u-1")
    resp = await env.probe("u-1", {"model": bad_model})
    assert resp.status_code == 422
    assert env.harness.calls == []


@pytest.mark.anyio
async def test_unauthenticated_401(env: _Env):
    resp = await env.probe(None, {"model": "m-1"})
    assert resp.status_code == 401
    assert env.harness.calls == []
