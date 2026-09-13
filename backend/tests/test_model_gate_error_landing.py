"""步骤 3b（伞形需求 #55）：门禁码在「看不见表单」的路径上必须有落点。

评审在 `ffbf434` 上确认的三件缺陷，本文件逐个钉住（前两条是修复的证伪用例）：

1. **SSE 丢码**：`AIService.generate_text_stream` 是异步生成器，`_require_model()` 的
   守卫在**首次 `__anext__`** 才抛，也就是落在 SSE handler 的 `except Exception` 里；
   handler 硬编码 `internal.generation_failed` 后，用户只看到一句通用失败，
   拿不到通往设置页的码。⇒ 本文件用**真实 SSE 路由**读事件流，断言 `error_code` 是
   `validation.*` 且 params 带模型名/下限。`e4d3f6c` 修完列出的那批之后，评审又指出
   创作助手的 `/projects/{id}/agent/chat-stream` 仍是同一个形态的汇流口（硬编码
   `internal.agent_execution_failed`），而它是核心 AI 功能 ⇒ 见下面的 1b。
2. **后台任务丢码**：后台任务弹不出表单，只能靠既有结构化列
   `BackgroundTask.status_code`/`status_params` 传码（前端按码渲染本地化文案并链到设置页）。
3. **「从当前配置创建预设」500**：`llm_model` 列默认值移除后自动建的 Settings 行存 NULL，
   而 `APIKeyPresetConfig.llm_model` 是必填 str ⇒ pydantic ValidationError 冒成 500 信封。

外加结构性守卫：**从 FastAPI 应用的路由表里枚举**所有 SSE 端点，逐个用 AST 检查
「会接住 ApiError 的 except 分支是否把错误码写死」。旧守卫是对 4 个手挑文件 grep
3 根 needles——把清单扩到 18 个真正的汇流口它照样绿，等于没有守卫。

测试值一律中性占位（gw.test / stub-model / "chapter one body text"），不含任何导入原文、
角色人名或书名（AGENTS.md 原文数据脱敏硬约束）。
"""
import ast
import inspect
import json
import os
import uuid
from datetime import datetime, timezone
from types import SimpleNamespace
from typing import Any, Dict, List, Optional

import httpx
import pytest
from fastapi import FastAPI, Request
from httpx import ASGITransport, AsyncClient
from sqlalchemy import event, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

import app.models  # noqa: F401 - 注册全部表（settings/background_task 建表需要）
import app.api.project_agent as project_agent_module
import app.api.settings as settings_module
import app.api.skills as skills_module
import app.services.model_capability_probe as probe_module
from app.core.errors import ApiError, register_exception_handlers, sse_code_for_exception
from app.database import Base, get_db
from app.models.background_task import BackgroundTask
from app.models.project import Project
from app.models.settings import Settings
from app.services.background_task_service import TaskProgressTracker
from app.services.model_capability_probe import (
    MIN_CONTEXT_WINDOW_TOKENS,
    PREFERENCES_KEY,
    SOURCE_PROBE,
    VERDICT_UNQUALIFIED,
    is_due_for_daily_recheck,
    triple_key,
)
from app.services.skill_loader import get_all_skills_cached
from app.utils.sse_response import WizardProgressTracker

NOT_CONFIGURED = "validation.ai_model_not_configured"
BELOW_MINIMUM = "validation.ai_model_below_minimum"
GENERIC_FAILURE = "internal.generation_failed"

SMALL_MODEL = "gpt-4o-mini"   # 真实 128K 级模型（登记表内 128000）
GATEWAY = "https://gw.test/v1"
API_KEY = "sk-stub-not-a-real-key"
NEUTRAL_PROMPT = "chapter one body text"

SKILL_CHAT_URL = "/api/skills/chat"
PRESET_FROM_CURRENT_URL = "/settings/presets/from-current"
# `build_app` 直接 include router，不带 `app/main.py` 那层 `/api` 挂载前缀
AGENT_CHAT_STREAM_URL = "/projects/proj-gate/agent/chat-stream"

# ========== SSE 汇流口枚举判据（结构守卫用） ==========
# 「这个 handler 自己构造了流式事件响应」的两个合法写法。
SSE_RESPONSE_MARKERS = ("create_sse_response", "text/event-stream")
# ApiError 直接继承 Exception ⇒ 只有这些分支类型会把它一起接住。
API_ERROR_CATCHING_NAMES = frozenset({"Exception", "BaseException", "ApiError"})
# 事件流里所有「发一帧错误」的具名调用。
SSE_ERROR_EMITTERS = frozenset({"send_error", "error", "error_from_exception"})
# 本身就按异常选码的发射器，写死码的风险不适用。
CODE_PRESERVING_EMITTERS = frozenset({"error_from_exception"})
# 评审时实有 18 个 SSE 端点。下限只为「枚举判据被人改掉 ⇒ 守卫静默空转」这一种失效兜底。
SSE_ENDPOINT_FLOOR = 18


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _unqualified_entry(tokens: int = 128_000) -> Dict[str, Any]:
    """一条**今天刚测过**的「实测不合格」缓存结论：派发时零网络即可拒，也不触发复测。"""
    return {
        "result": VERDICT_UNQUALIFIED,
        "source": SOURCE_PROBE,
        "context_window_tokens": tokens,
        "tier": "metadata",
        "detail": "seeded unqualified",
        "checked_at": _now_iso(),
    }


# ========== 夹具：离线 DB + 离线探测网关 + 无真实凭据 ==========


@pytest.fixture
async def db_factory():
    db_path = f"/tmp/test_gate_landing_{uuid.uuid4().hex}.db"
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
    factory.test_engine = engine  # 供需要复用同一 engine 的用例取用
    yield factory
    await engine.dispose()
    for suffix in ("", "-wal", "-shm"):
        path = db_path + suffix
        if os.path.exists(path):
            os.remove(path)


@pytest.fixture
def offline_probe_gateway(monkeypatch):
    """探测客户端换成 MockTransport：缓存命中时它一次都不该被调用（真出网即失败）。"""
    calls: List[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path)
        return httpx.Response(404, json={"error": {"message": "no such endpoint"}})

    probe_module.memo_clear()
    monkeypatch.setattr(
        probe_module, "create_probe_client",
        lambda transport=None: httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )
    yield calls
    probe_module.memo_clear()


@pytest.fixture
def no_real_provider_keys(monkeypatch):
    """屏蔽 .env 真实 key：测试实例绝不能拿真凭据出网。"""
    for attr in ("openai_api_key", "anthropic_api_key", "gemini_api_key"):
        monkeypatch.setattr("app.services.ai_service.app_settings." + attr, None, raising=False)


async def seed_settings(factory, user_id, *, llm_model=None, preferences=None, api_key=API_KEY):
    async with factory() as session:
        session.add(Settings(
            user_id=user_id,
            api_provider="openai",
            api_key=api_key,
            api_base_url=GATEWAY,
            llm_model=llm_model,
            temperature=0.7,
            max_tokens=2000,
            preferences=json.dumps(preferences or {}, ensure_ascii=False),
        ))
        await session.commit()


def build_app(factory, *routers) -> FastAPI:
    """装配与生产同一套异常 handler 的测试 app；auth 走 request.state.user（require_login 读它）。"""
    app = FastAPI()
    register_exception_handlers(app)
    for router in routers:
        app.include_router(router)

    async def override_get_db():
        async with factory() as session:
            yield session

    app.dependency_overrides[get_db] = override_get_db

    @app.middleware("http")
    async def fake_auth(request: Request, call_next):
        user_id = request.headers.get("x-test-user")
        if user_id:
            request.state.user = SimpleNamespace(user_id=user_id, is_admin=False)
            # `require_login` 读 state.user；`api/project_agent.py::_user_id` 读 state.user_id
            # （生产由 auth_middleware 同时注入）。两边都要给，否则 agent 路由吃 auth.unauthorized。
            request.state.user_id = user_id
        return await call_next(request)

    return app


def client_for(app: FastAPI) -> AsyncClient:
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


async def first_skill_key() -> str:
    """取真实 Skill 目录里的第一个键名（不硬编码，避免目录改名后测试假绿）。"""
    skills = get_all_skills_cached()
    assert skills, "Skill 目录为空，无法构造真实流式路径"
    return skills[0]["template_key"]


async def sse_error_event(client: AsyncClient, url: str, user_id: str, payload: Dict[str, Any]) -> Dict[str, Any]:
    """真发一次 SSE 请求，返回事件流里第一个 `type=="error"` 事件（按 SSE 帧解析 data: 行）。"""
    events: List[Dict[str, Any]] = []
    pending = ""
    async with client.stream("POST", url, json=payload, headers={"x-test-user": user_id}) as response:
        assert response.status_code == 200, await response.aread()
        async for chunk in response.aiter_text():
            pending += chunk
            while "\n\n" in pending:
                frame, pending = pending.split("\n\n", 1)
                for line in frame.split("\n"):
                    if not line.startswith("data: "):
                        continue
                    try:
                        events.append(json.loads(line[len("data: "):]))
                    except json.JSONDecodeError:
                        continue
    errors = [e for e in events if e.get("type") == "error"]
    assert errors, f"SSE 事件流里没有 error 事件：{events}"
    return errors[0]


# ========== 1. 真实 SSE 路由：门禁码必须送到前端 ==========


@pytest.mark.anyio
async def test_sse_stream_surfaces_not_configured_code(db_factory, no_real_provider_keys):
    """缺陷 1 的靶心：未配置模型时流式功能必须回 `validation.ai_model_not_configured`。

    修复前：守卫在首次 `__anext__` 抛，被 handler 的 `except Exception` 改写成
    `internal.generation_failed` ⇒ 本用例在旧代码上必红。
    """
    user_id = "u-sse-none"
    await seed_settings(db_factory, user_id, llm_model=None)
    app = build_app(db_factory, skills_module.router, settings_module.router)

    async with client_for(app) as client:
        event = await sse_error_event(client, SKILL_CHAT_URL, user_id, {
            "skill_key": await first_skill_key(), "message": NEUTRAL_PROMPT,
        })

    assert event["error_code"] == NOT_CONFIGURED, f"码被吞掉了：{event}"
    assert event["code"] == 400, "注册码的 status 应随码下发，而不是旧信封的 500"


@pytest.mark.anyio
async def test_sse_stream_surfaces_below_minimum_code_with_model_name(
    db_factory, no_real_provider_keys, offline_probe_gateway
):
    """同一处也必须覆盖 `validation.ai_model_below_minimum`，且 params 带模型名与下限。"""
    user_id = "u-sse-small"
    await seed_settings(db_factory, user_id, llm_model=SMALL_MODEL, preferences={
        PREFERENCES_KEY: {triple_key("openai", GATEWAY, SMALL_MODEL): _unqualified_entry()}
    })
    app = build_app(db_factory, skills_module.router, settings_module.router)

    async with client_for(app) as client:
        event = await sse_error_event(client, SKILL_CHAT_URL, user_id, {
            "skill_key": await first_skill_key(), "message": NEUTRAL_PROMPT,
        })

    assert event["error_code"] == BELOW_MINIMUM, f"码被吞掉了：{event}"
    assert event["error_params"]["model"] == SMALL_MODEL
    assert event["error_params"]["min_window"] == MIN_CONTEXT_WINDOW_TOKENS
    assert event["error_params"]["measured_context_window_tokens"] == 128_000
    assert event["code"] == 400
    assert offline_probe_gateway == [], "缓存已有结论却仍去探测：热路径应当零网络"


@pytest.mark.anyio
async def test_seeded_verdict_is_not_due_for_recheck():
    """夹具自检：上面那条结论必须「今天已测」，否则用例会悄悄冒出后台复测任务。"""
    from app.services.model_capability_probe import ProbeOutcome

    fresh = ProbeOutcome(
        verdict=VERDICT_UNQUALIFIED, context_window_tokens=128_000, source=SOURCE_PROBE,
        tier="metadata", detail="x", checked_at=_unqualified_entry()["checked_at"],
    )
    assert not is_due_for_daily_recheck(fresh)


# ========== 1b. 创作助手 /chat-stream：评审第 2 项遗留的吞码汇流口 ==========


@pytest.mark.anyio
async def test_agent_chat_stream_surfaces_below_minimum_code(
    db_factory, no_real_provider_keys, offline_probe_gateway
):
    """核心 AI 功能也不例外：不合格模型在创作助手里必须拿到通往设置页的码。

    修复前：`/chat-stream` 的 `except Exception` 把一切改写成
    `internal.agent_execution_failed` ⇒ 本用例在旧代码上必红。
    """
    user_id = "u-agent-small"
    await seed_settings(db_factory, user_id, llm_model=SMALL_MODEL, preferences={
        PREFERENCES_KEY: {triple_key("openai", GATEWAY, SMALL_MODEL): _unqualified_entry()}
    })
    async with db_factory() as session:
        session.add(Project(id="proj-gate", user_id=user_id, title="Neutral project"))
        await session.commit()

    app = build_app(db_factory, project_agent_module.router)

    async with client_for(app) as client:
        event = await sse_error_event(client, AGENT_CHAT_STREAM_URL, user_id, {
            "message": NEUTRAL_PROMPT, "page_context": {},
        })

    assert event["error_code"] == BELOW_MINIMUM, f"码被吞掉了：{event}"
    assert event["error_params"]["model"] == SMALL_MODEL
    assert event["error_params"]["measured_context_window_tokens"] == 128_000
    assert event["error_params"]["min_window"] == MIN_CONTEXT_WINDOW_TOKENS
    assert event["code"] == 400
    assert offline_probe_gateway == [], "缓存已有结论却仍去探测：热路径应当零网络"


@pytest.mark.anyio
async def test_agent_chat_stream_plain_failure_keeps_its_own_generic_code(
    db_factory, no_real_provider_keys, monkeypatch
):
    """反向验收：普通异常仍归站点的 `internal.agent_execution_failed`。

    没有这一条，「改用 `sse_code_for_exception`」就可能是把通用失败也一起改成了
    无码/造码——站点前缀与兜底码都得原样保留。
    """
    from app.services.project_agent_service import ProjectAgentService

    user_id = "u-agent-plain"
    await seed_settings(db_factory, user_id, llm_model="stub-model")
    async with db_factory() as session:
        session.add(Project(id="proj-gate", user_id=user_id, title="Neutral project"))
        await session.commit()

    async def exploding_stream(self, **_kwargs):
        raise RuntimeError("agent backend blew up")
        yield  # pragma: no cover - 让它成为异步生成器

    monkeypatch.setattr(ProjectAgentService, "stream_chat", exploding_stream)
    app = build_app(db_factory, project_agent_module.router)

    async with client_for(app) as client:
        event = await sse_error_event(client, AGENT_CHAT_STREAM_URL, user_id, {
            "message": NEUTRAL_PROMPT, "page_context": {},
        })

    assert event["error_code"] == "internal.agent_execution_failed", event
    assert event["error_params"] == {"error": "agent backend blew up"}
    assert "agent backend blew up" in event["error_raw"]


# ========== 2. 泛型收尾器的码保真 ==========


@pytest.mark.anyio
async def test_wizard_tracker_falls_back_only_for_plain_exceptions():
    """非 ApiError 仍归 `internal.generation_failed`；ApiError 保留自己的码。"""
    tracker = WizardProgressTracker("章节")

    def payload(frame: str) -> Dict[str, Any]:
        return json.loads(frame.split("data: ", 1)[1])

    plain = payload(await tracker.error_from_exception(ValueError("boom")))
    assert plain["error_code"] == GENERIC_FAILURE
    assert plain["error_params"] == {"error": "boom"}

    gate = payload(await tracker.error_from_exception(
        ApiError(code=NOT_CONFIGURED), f"生成失败: {NOT_CONFIGURED}"
    ))
    assert gate["error_code"] == NOT_CONFIGURED, gate
    assert gate["code"] == 400
    # 站点自己的诊断前缀留在 error/raw，不参与选文案（task 14a 双通道契约）
    assert gate["error_raw"] == f"生成失败: {NOT_CONFIGURED}"


def test_generic_mapper_still_returns_none_for_plain_exception():
    """`sse_code_for_exception` 是既有映射器：普通异常没有码，本任务只是开始使用它。"""
    code, params = sse_code_for_exception(ValueError("boom"))
    assert code is None
    assert params == {}


@pytest.mark.anyio
async def test_task_tracker_error_from_exception_persists_structured_columns(db_factory, monkeypatch):
    """后台任务：门禁码必须写进既有结构化列，FloatingTaskPanel 才能按码渲染并链到设置页。"""
    from app.services import background_task_service as bts

    async with db_factory() as session:
        session.add(BackgroundTask(
            id="bg-gate", user_id="u", project_id="p", task_type="chapter_generate",
            status="running",
        ))
        await session.commit()

    engine = db_factory.test_engine

    async def fake_get_engine(user_id):
        return engine

    monkeypatch.setattr(bts, "get_engine", fake_get_engine)

    tracker = TaskProgressTracker("bg-gate", "u", "章节")
    await tracker.error_from_exception(ApiError(
        code=BELOW_MINIMUM,
        params={"model": SMALL_MODEL, "min_window": MIN_CONTEXT_WINDOW_TOKENS},
    ))

    async with db_factory() as session:
        row = (await session.execute(
            select(BackgroundTask).where(BackgroundTask.id == "bg-gate")
        )).scalar_one()

    assert row.status == "failed"
    assert row.status_code == BELOW_MINIMUM, f"码没进结构化列：{row.status_code}"
    assert row.status_params["model"] == SMALL_MODEL
    assert row.status_params["min_window"] == MIN_CONTEXT_WINDOW_TOKENS


@pytest.mark.anyio
async def test_task_tracker_plain_exception_keeps_task_failed(db_factory, monkeypatch):
    """未被误伤：普通异常的后台失败行仍归既有 `task.failed`，通用失败语义不变。"""
    from app.services import background_task_service as bts

    async with db_factory() as session:
        session.add(BackgroundTask(
            id="bg-plain", user_id="u", project_id="p", task_type="chapter_generate",
            status="running",
        ))
        await session.commit()

    engine = db_factory.test_engine

    async def fake_get_engine(user_id):
        return engine

    monkeypatch.setattr(bts, "get_engine", fake_get_engine)

    await TaskProgressTracker("bg-plain", "u", "章节").error_from_exception(ValueError("boom"))

    async with db_factory() as session:
        row = (await session.execute(
            select(BackgroundTask).where(BackgroundTask.id == "bg-plain")
        )).scalar_one()
    assert row.status_code == "task.failed"
    assert row.error_message == "boom"


# ========== 3. 「从当前配置创建预设」不得冒 500 ==========


@pytest.mark.anyio
async def test_preset_from_current_without_model_returns_clean_code(db_factory, no_real_provider_keys):
    """缺陷 3：NULL llm_model（列默认值移除后的新建行）⇒ 干净错误码，不是 500 信封。"""
    user_id = "u-preset-null"
    await seed_settings(db_factory, user_id, llm_model=None, api_key=None)
    app = build_app(db_factory, settings_module.router)

    async with client_for(app) as client:
        resp = await client.post(
            PRESET_FROM_CURRENT_URL, params={"name": "p1"}, headers={"x-test-user": user_id}
        )

    assert resp.status_code == 400, resp.text
    body = resp.json()
    assert body["code"] == NOT_CONFIGURED, body
    assert "ValidationError" not in json.dumps(body, ensure_ascii=False)


@pytest.mark.anyio
async def test_preset_from_current_with_model_still_creates_preset(db_factory, no_real_provider_keys):
    """未被误伤：配了模型的用户照常建预设。"""
    user_id = "u-preset-ok"
    await seed_settings(db_factory, user_id, llm_model="stub-model")
    app = build_app(db_factory, settings_module.router)

    async with client_for(app) as client:
        resp = await client.post(
            PRESET_FROM_CURRENT_URL, params={"name": "p-ok"}, headers={"x-test-user": user_id}
        )

    assert resp.status_code == 200, resp.text
    assert resp.json()["config"]["llm_model"] == "stub-model"


# ========== 4. 结构守卫：泛型流式收尾不再写死码 ==========
#
# 旧守卫是「4 个手挑文件 + 3 根 needles」的 grep：把清单扩到 `api/project_agent.py`
# 与 `api/book_import.py` 后它照样绿，也就是说它对真正的汇流口毫无覆盖。
# 现在改成**从 FastAPI 应用本身枚举** SSE 端点，逐个用 AST 检查它的异常收尾。


def _sse_endpoint_sources() -> Dict[str, str]:
    """真实应用里所有「返回 SSE 事件流」的端点：`路由路径 -> handler 源码`。

    判据不是文件名清单、也不是目录扫描，而是「这个 handler 自己构造了流式事件响应」。
    路由注册表是权威的：新增一个汇流口（哪怕在新模块里）会自动进入本集合，守卫随之生效。
    """
    from app.main import app as production_app

    found: Dict[str, str] = {}
    for route in production_app.routes:
        endpoint = getattr(route, "endpoint", None)
        if not callable(endpoint):
            continue
        try:
            source = inspect.getsource(endpoint)
        except (OSError, TypeError):  # pragma: no cover - 动态构造的 handler
            continue
        if any(marker in source for marker in SSE_RESPONSE_MARKERS):
            found[f"{getattr(route, 'path', '?')}::{endpoint.__name__}"] = source
    return found


def _catches_api_error(handler: ast.ExceptHandler) -> bool:
    """这个 except 分支是否会接住 ApiError（ApiError 直接继承 Exception）。"""
    if handler.type is None:  # 裸 except
        return True
    elements = handler.type.elts if isinstance(handler.type, ast.Tuple) else [handler.type]
    for element in elements:
        name = element.id if isinstance(element, ast.Name) else ast.unparse(element)
        if name in API_ERROR_CATCHING_NAMES:
            return True
    return False


def _hardcoded_error_code(call: ast.Call) -> Optional[str]:
    """这次 error 帧发射调用是否把码写死成字符串字面量（⇒ ApiError 的码被覆盖）。

    取的是 `send_error(error, code, ...)` 的 code 位与 `tracker.error(..., error_code=...)`
    的 error_code 位。非字面量（`code or "internal.x"`、`sse_code_for_exception` 的结果）
    说明码来自异常本身，属合规写法。
    """
    for keyword in call.keywords:
        if keyword.arg in ("code", "error_code"):
            value = keyword.value
            if isinstance(value, ast.Constant) and isinstance(value.value, str):
                return value.value
            return None
    if len(call.args) >= 2:
        second = call.args[1]
        if isinstance(second, ast.Constant) and isinstance(second.value, str):
            return second.value
    return None


def _swallowing_sinks(source: str, endpoint_name: str) -> List[str]:
    """handler 源码里所有「能吞掉 ApiError 且写死错误码」的收尾分支。"""
    offenders: List[str] = []
    for node in ast.walk(ast.parse(source)):
        if not isinstance(node, ast.Try):
            continue
        for index, handler in enumerate(node.handlers):
            if not _catches_api_error(handler):
                continue
            # 前面已有分支专门接住 ApiError ⇒ 泛型分支根本收不到它，不算吞码
            if any(_catches_api_error(earlier) for earlier in node.handlers[:index]):
                continue
            for call in (n for n in ast.walk(handler) if isinstance(n, ast.Call)):
                func_name = (
                    call.func.attr
                    if isinstance(call.func, ast.Attribute)
                    else getattr(call.func, "id", "")
                )
                if func_name not in SSE_ERROR_EMITTERS or func_name in CODE_PRESERVING_EMITTERS:
                    continue
                hardcoded = _hardcoded_error_code(call)
                if hardcoded is not None:
                    offenders.append(
                        f"{endpoint_name} 第 {handler.lineno} 行的 except 会接住 ApiError，"
                        f"却用 {func_name}(code={hardcoded!r}) 写死了码"
                    )
    return offenders


def test_sse_endpoint_enumeration_is_not_silently_empty():
    """守卫的地基：枚举必须真的找到全部汇流口，否则「零违规」是假的。

    期望值取当前代码里 SSE 端点的实际数量（评审时是 18）。**只许变多不许变少**——
    数量掉了说明枚举判据被改掉，守卫会从此静默漏人。
    """
    endpoints = _sse_endpoint_sources()
    assert len(endpoints) >= SSE_ENDPOINT_FLOOR, (
        f"只枚举到 {len(endpoints)} 个 SSE 端点（下限 {SSE_ENDPOINT_FLOOR}）"
        "⇒ 枚举判据失效，本守卫已是空转"
    )
    assert any("chat-stream" in key for key in endpoints), "评审第 2 项那条路由没被枚举到"


def test_no_generic_sse_sink_swallows_api_errors():
    """每一个 SSE 端点都必须把 ApiError 的注册码原样送出去（不是 grep 手挑文件）。"""
    offenders: List[str] = []
    for key, source in _sse_endpoint_sources().items():
        offenders.extend(_swallowing_sinks(source, key))
    assert offenders == [], offenders
