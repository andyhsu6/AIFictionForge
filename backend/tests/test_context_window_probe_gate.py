"""步骤 3（伞形需求 #55）：上下文窗口行为探测 + 绑定「实发模型」的硬拦门禁。

要钉住的事实：
1. 探测三态各一例（mock 客户端，**不发真请求**）：qualified / unqualified / inconclusive。
2. 探测路径的重试实际生效 **1 次**（`_request_with_retry` 默认 max_retries=3 且只排除
   401/403/404 ⇒ ② 档预期拿到的 400 会被原样重发 3 次，3 倍计费）。
3. ③ needle 档**未接线**：调用即 inconclusive 且一次请求都不发。
4. `daily` 触发点选 needle 档必须抛断言错误（架构层禁止误挂，否则每天 ≈1M token）。
5. 真实 128K 级模型（gpt-4o-mini）保存 ⇒ **被拒**。
6. 未登记/探测不出 ⇒ 要求显式声明 context_window_tokens；声明 ≥1M 可保存、
   声明 <1M 仍被拒；实测 <1M 的模型即使声明 ≥1M 也**不放行**（无勾选放行通道）。
7. **绕过路径**：配好合格 1M 默认模型的用户，逐次传 `model="gpt-4o-mini"`
   （请求体 / 后台任务 task_input）必须在**发请求那一刻**被拦，而不是只在保存时。
8. `get_effective_context_window` 无合格结论即抛错，绝不返回 0/None（0 在本仓库＝禁用）。
9. **per-call `provider` 覆盖**：`provider` 同样取自请求体并决定 `_get_provider` 选哪个
   槽位 ⇒ 门禁必须判定**实发**的 (provider, base_url, model) 三元组，探测与缓存键同源
   （审核 b09a047 发现的 wrong-host admit：用用户自己的合格网关给别家 host 开合格证）。

测试值一律中性占位（big-model / gw.test / "chapter one body text"），
不含任何导入原文、角色人名或书名（AGENTS.md 原文数据脱敏硬约束）。
"""
import asyncio
import inspect
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
from app.core.db_write_lock import db_write_lock, db_write_locks
from app.core.errors import ERROR_REGISTRY, ApiError, register_exception_handlers
from app.database import Base, get_db
from app.models.settings import Settings
from app.services.ai_service import (
    AIService,
    _FULL_BOOK_BUDGET_RATIO,
    _KNOWN_CONTEXT_WINDOWS,
    detect_context_window,
)
from app.services.model_capability_probe import (
    MIN_CONTEXT_WINDOW_TOKENS,
    PREFERENCES_KEY,
    PROBE_MAX_ATTEMPTS,
    ProbeOutcome,
    SOURCE_PROBE,
    SOURCE_USER_DECLARED,
    TRIGGER_ALLOWED_TIERS,
    TRIGGER_DAILY,
    TRIGGER_MANUAL,
    TRIGGER_SAVE,
    TIER_MAX_TOKENS_BOUND,
    TIER_METADATA,
    TIER_NEEDLE,
    VERDICT_INCONCLUSIVE,
    VERDICT_QUALIFIED,
    VERDICT_UNQUALIFIED,
    assert_tier_allowed,
    ensure_model_allowed,
    get_effective_context_window,
    probe_model_context_window,
    probe_needle_tier,
    triple_key,
    verdict_may_overwrite,
)

BELOW_MINIMUM = "validation.ai_model_below_minimum"
QUALIFIED_MODEL = "big-model"
SMALL_MODEL = "gpt-4o-mini"          # 真实 128K 级模型（登记表内 128000）
UNKNOWN_MODEL = "mystery-model"      # 未登记，且 ①② 都判不出
GATEWAY = "https://gw.test/v1"
# 另一家的 host：per-call `provider` 覆盖实际会派发到那里（anthropic 的探测 URL
# 由 `_metadata_url` 拼成 `<base>/v1/models/<id>`，所以 base 不带 /v1）。
OTHER_GATEWAY = "https://other-gw.test"
API_KEY = "sk-stub-not-a-real-key"
NEUTRAL_PROMPT = "chapter one body text"

_SSE_FIRST_DELTA = b'data: {"choices":[{"delta":{"content":"ok"}}]}\n\n'
_BOUND_REJECTION_BODY = (
    b'{"error":{"message":"This model\'s maximum context length is 128000 tokens"}}'
)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _days_ago_iso(days: int) -> str:
    return (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()


# ========== Mock 网关 ==========


class _Gateway:
    """MockTransport 假网关：记录每一次出网请求，按档位可编程返回。

    默认行为贴近现实网关：不支持 `GET /models/<id>`（404），
    对 `max_tokens >= 1M` 的请求回上界类 400。
    """

    def __init__(
        self,
        *,
        metadata_status=404,
        metadata_body=None,
        bound_status=400,
        bound_body=_BOUND_REJECTION_BODY,
    ):
        self.calls = []
        self.metadata_status = metadata_status
        self.metadata_body = metadata_body
        self.bound_status = bound_status
        self.bound_body = bound_body

    @property
    def metadata_calls(self):
        return sum(1 for c in self.calls if "/models/" in c["path"])

    @property
    def bound_calls(self):
        return sum(1 for c in self.calls if "/chat/completions" in c["path"])

    @property
    def total_calls(self):
        return len(self.calls)

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.calls.append({
            "url": str(request.url),
            "path": request.url.path,
            "method": request.method,
            "body": request.content,
        })

        if request.url.path.startswith("/v1/models/"):
            if self.metadata_status >= 400:
                return httpx.Response(self.metadata_status, json={"error": {"message": "no such endpoint"}})
            return httpx.Response(200, json=self.metadata_body or {})

        if request.url.path.endswith("/chat/completions"):
            if self.bound_status >= 400:
                return httpx.Response(
                    self.bound_status, content=self.bound_body, headers={"content-type": "application/json"}
                )
            return httpx.Response(200, content=_SSE_FIRST_DELTA, headers={"content-type": "text/event-stream"})

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


# ========== 1. 探测三态 ==========


@pytest.mark.anyio
async def test_probe_qualified_from_metadata_tier(gateway):
    """① 档报 >=1M ⇒ qualified，且不必再走 ② 档（0 token 判定优先）。"""
    gateway.metadata_status = 200
    gateway.metadata_body = {"id": QUALIFIED_MODEL, "context_length": 1_048_576}

    outcome = await probe_model_context_window(
        provider="openai", base_url=GATEWAY, api_key=API_KEY, model=QUALIFIED_MODEL
    )

    assert outcome.verdict == VERDICT_QUALIFIED
    assert outcome.context_window_tokens == 1_048_576
    assert outcome.tier == TIER_METADATA
    assert outcome.source == SOURCE_PROBE
    assert gateway.bound_calls == 0, "① 档已有判据时不该再打 ② 档"


@pytest.mark.anyio
async def test_probe_unqualified_from_max_tokens_bound_tier(gateway):
    """① 判不出 + ② 被上界校验拒绝 ⇒ unqualified（128K 级模型的真实形态）。"""
    outcome = await probe_model_context_window(
        provider="openai", base_url=GATEWAY, api_key=API_KEY, model=SMALL_MODEL
    )

    assert outcome.verdict == VERDICT_UNQUALIFIED
    assert outcome.tier == TIER_MAX_TOKENS_BOUND
    assert "128000" in (outcome.detail or "")


@pytest.mark.anyio
async def test_probe_inconclusive_when_gateway_gives_no_useful_information(gateway):
    """鉴权失败（401）推不出窗口大小 ⇒ inconclusive：未知是「不合格」，不是「放行」。"""
    gateway.bound_status = 401
    gateway.bound_body = b'{"error":{"message":"invalid api key"}}'

    outcome = await probe_model_context_window(
        provider="openai", base_url=GATEWAY, api_key=API_KEY, model=UNKNOWN_MODEL
    )

    assert outcome.verdict == VERDICT_INCONCLUSIVE
    assert outcome.context_window_tokens is None


@pytest.mark.anyio
async def test_probe_qualified_from_streaming_acceptance(gateway):
    """② 档接受 max_tokens=1M 并吐出首个 delta ⇒ qualified。"""
    gateway.bound_status = 200

    outcome = await probe_model_context_window(
        provider="openai", base_url=GATEWAY, api_key=API_KEY, model=QUALIFIED_MODEL
    )

    assert outcome.verdict == VERDICT_QUALIFIED
    assert outcome.context_window_tokens == MIN_CONTEXT_WINDOW_TOKENS
    assert outcome.tier == TIER_MAX_TOKENS_BOUND


@pytest.mark.anyio
async def test_probe_tier_two_uses_stream_and_stops_at_first_delta(gateway):
    """② 档必须 stream=true：不校验上界而直接生成的实现，不断开会真的烧掉整个输出预算。"""
    gateway.bound_status = 200

    await probe_model_context_window(
        provider="openai", base_url=GATEWAY, api_key=API_KEY, model=QUALIFIED_MODEL
    )

    sent = json.loads(gateway.calls[-1]["body"])
    assert sent["stream"] is True
    assert sent["max_tokens"] >= MIN_CONTEXT_WINDOW_TOKENS
    assert gateway.bound_calls == 1, "拿到首个 delta 后必须立即断开，不该继续读流"


# ========== 1b. ② 档拒绝路径：不许记录没测到的东西（#65） ==========
#
# `unqualified` 是**实测**结论，#59 之后用户自己的声明抹不掉它。于是一次**假**的
# unqualified 会把一台合规模型**永久锁死**：界面上连个数字都没有，声明出口又被刻意关闭。
# 所以拒绝路径的判据必须是「这条报错真的排除了 >=1M 吗」：
#   A 报错讲的是**输出**上限（`max_tokens must be <= 16384`）——一次能生成多少
#     跟窗口有多大是两件事 ⇒ inconclusive，出口（声明）保持开着
#   B 刻度恰为 1M、prompt 非空 ⇒ 被拒只证明 `window < 1M + prompt`，排除不了
#     `window == 1M`（登记表里 deepseek-v3 / gemini-2 恰好就是 1000000）⇒ inconclusive
#   C 网关自己报出一个**低于下限的上下文数字** ⇒ 仍是实测 unqualified、仍不可声明翻盘
#     ——这才是门禁存在的意义，本节的修法绝不能把它一起放掉。

_OUTPUT_CAP_BODY = b'{"error":{"message":"max_tokens must be <= 16384"}}'
_ZERO_MARGIN_BODY = (
    b'{"error":{"message":"This model\'s context window is 1000000 tokens, '
    b'but your prompt (10 tokens) plus max_tokens (1000000) exceeds it. '
    b'Reduce max_tokens."}}'
)
_HONEST_128K_BODY = (
    b'{"error":{"message":"This model\'s maximum context length is 128000 tokens. '
    b'However, you requested 1000000 tokens (10 in messages + 1000000 in completion). '
    b'Please reduce the length of the messages or completion."}}'
)


@pytest.mark.anyio
async def test_probe_output_cap_rejection_is_inconclusive(gateway):
    """A：输出上限的 400 推不出任何窗口结论 ⇒ inconclusive，不是 unqualified。"""
    gateway.bound_body = _OUTPUT_CAP_BODY

    outcome = await probe_model_context_window(
        provider="openai", base_url=GATEWAY, api_key=API_KEY, model=QUALIFIED_MODEL
    )

    assert outcome.verdict == VERDICT_INCONCLUSIVE, (
        "把输出上限当成上下文窗口证据 = 永久锁死一台合规模型（#65）"
    )
    assert outcome.context_window_tokens is None
    assert "output cap" in (outcome.detail or "")


@pytest.mark.anyio
async def test_probe_boundary_rejection_at_exact_minimum_is_inconclusive(gateway):
    """B：恰好 1M 刻度 + 非空 prompt 被拒，只证明 `window < 1M+prompt` ⇒ 判不出。"""
    gateway.bound_body = _ZERO_MARGIN_BODY

    outcome = await probe_model_context_window(
        provider="openai", base_url=GATEWAY, api_key=API_KEY, model=QUALIFIED_MODEL
    )

    assert outcome.verdict == VERDICT_INCONCLUSIVE, (
        "零边际的边界拒绝不能当成 `<1M` 的实证（#65）"
    )
    assert "margin" in (outcome.detail or "")


@pytest.mark.anyio
async def test_probe_honest_context_number_stays_measured_unqualified(gateway):
    """C：网关报得出 128000 ⇒ 依旧是实测 unqualified，并带上那个数字（门禁的靶心）。"""
    gateway.bound_body = _HONEST_128K_BODY

    outcome = await probe_model_context_window(
        provider="openai", base_url=GATEWAY, api_key=API_KEY, model=SMALL_MODEL
    )

    assert outcome.verdict == VERDICT_UNQUALIFIED
    assert outcome.tier == TIER_MAX_TOKENS_BOUND
    assert outcome.context_window_tokens == 128_000, "报得出的数字必须落进结论，界面才有数可显示"
    assert "128000" in (outcome.detail or "")


@pytest.mark.anyio
async def test_probe_metadata_exact_minimum_is_qualified(gateway):
    """边界接受：① 档报 exactly 1,000,000 ⇒ 合规（判据是 `>=`，不得写成 `>`）。"""
    gateway.metadata_status = 200
    gateway.metadata_body = {"id": QUALIFIED_MODEL, "context_length": MIN_CONTEXT_WINDOW_TOKENS}

    outcome = await probe_model_context_window(
        provider="openai", base_url=GATEWAY, api_key=API_KEY, model=QUALIFIED_MODEL
    )

    assert outcome.verdict == VERDICT_QUALIFIED
    assert outcome.context_window_tokens == MIN_CONTEXT_WINDOW_TOKENS
    assert gateway.bound_calls == 0, "① 档已定论时不该再打 ② 档"


def test_bound_rejection_needs_a_number_beside_a_context_phrase():
    """判据来自网关自己报的数，而不是 `exceeds`/`reduce` 这类裸子串。"""
    classify = probe_module._classify_bound_rejection

    honest = classify(_HONEST_128K_BODY.decode())
    assert honest.proves_below_minimum is True
    assert honest.reported_context_tokens == 128_000

    for body in (
        _OUTPUT_CAP_BODY.decode(),
        _ZERO_MARGIN_BODY.decode(),
        "you asked for 1000000 tokens but this endpoint emits at most 16384 completion tokens",
        "reduce your prompt: it exceeds the model context window",   # 讲清了是上下文，但没报数
        # 裸子串不算判据：`exceeds` + 一个小于下限的数，读不出报的是**哪个**上限
        "your request exceeds 128000 tokens",
        # 同一分句里既有输出上限措辞又有上下文措辞 ⇒ 归属不确定 ⇒ 不许记录
        "input tokens plus max_tokens must fit in 16384 context tokens",
        # 多个候选时取**最大**那个（最保守）：这里 10 是回显的请求大小、2000000 才是窗口
        "you sent 10 prompt tokens, the maximum context length is 2000000 tokens",
        # 网关自己报的窗口 >= 下限：即便同一句里还有个很小的输出上限，也不许判成不合格
        "this model's context window is 2000000 tokens, but max_tokens must be at most 16384",
    ):
        evidence = classify(body)
        assert evidence.proves_below_minimum is False, f"不该记成实测不合格：{body}"

    # 数字必须**贴着**上下文措辞才算窗口证据
    lonely = classify("error 500: bad request, upstream returned an unexpected payload")
    assert lonely.proves_below_minimum is False
    assert lonely.is_bound_related is False


def test_fix_is_not_a_lower_threshold_or_a_declaration_override():
    """#65 的修法不能是「把刻度调小」或「让声明盖过实测」——那等于重开 #59 的洞。"""
    assert probe_module.MAX_TOKENS_PROBE_VALUE == MIN_CONTEXT_WINDOW_TOKENS

    measured_small = ProbeOutcome(
        verdict=VERDICT_UNQUALIFIED, context_window_tokens=128_000, tier=TIER_METADATA
    )
    declared_big = ProbeOutcome(
        verdict=VERDICT_QUALIFIED,
        context_window_tokens=2_000_000,
        source=SOURCE_USER_DECLARED,
    )
    no_verdict = ProbeOutcome(verdict=VERDICT_INCONCLUSIVE, tier=TIER_MAX_TOKENS_BOUND)

    assert verdict_may_overwrite(measured_small, declared_big) is False
    assert verdict_may_overwrite(measured_small, no_verdict) is False
    assert verdict_may_overwrite(measured_small, ProbeOutcome(verdict=VERDICT_UNQUALIFIED)) is True


# ========== 2. 绕重试 / 绕信号量 ==========


@pytest.mark.anyio
async def test_probe_issues_exactly_one_attempt_per_tier(gateway):
    """探测路径 max_retries 实际生效 1 次：预期内的 400 不得被重发（否则 3 倍计费）。"""
    assert PROBE_MAX_ATTEMPTS == 1
    assert probe_module.PROBE_TRANSPORT_RETRIES == 0

    await probe_model_context_window(
        provider="openai", base_url=GATEWAY, api_key=API_KEY, model=SMALL_MODEL
    )

    assert gateway.metadata_calls == 1
    assert gateway.bound_calls == 1, f"② 档被重发了 {gateway.bound_calls} 次，重试没绕开"
    assert gateway.total_calls == 2, "探测总请求数超出 ①+② 各一次"


def test_probe_client_bypasses_shared_retry_and_semaphore_paths():
    """探测客户端直连 httpx：不复用 BaseAIClient._request_with_retry，也不取全局信号量。"""
    source = inspect.getsource(probe_module)
    assert "_request_with_retry" in source, "探测必须点名它绕开了共享重试链（可审计）"
    assert "self._request_with_retry" not in source
    assert "await _request_with_retry" not in source
    assert "_get_semaphore" not in source, "探测不得排队在日常生成的并发闸后面"
    assert "_http_client_pool" not in source, "探测不得共用 AI 客户端池"


# ========== 3. ③ 未接线 ==========


@pytest.mark.anyio
async def test_needle_tier_is_not_wired(gateway):
    """③ 调用即返回未接线/inconclusive，且一次请求都不发（未接线却报合格＝给小模型开合格证）。"""
    outcome = await probe_needle_tier()

    assert outcome.verdict == VERDICT_INCONCLUSIVE
    assert outcome.tier == TIER_NEEDLE
    assert "not wired" in (outcome.detail or "")
    assert gateway.total_calls == 0


@pytest.mark.anyio
async def test_needle_tier_requested_explicitly_still_sends_nothing(gateway):
    """save 触发点允许 needle 档，但未接线的实现仍必须返回 inconclusive 且不发请求。"""
    outcome = await probe_model_context_window(
        provider="openai",
        base_url=GATEWAY,
        api_key=API_KEY,
        model=QUALIFIED_MODEL,
        trigger=TRIGGER_SAVE,
        tiers=(TIER_NEEDLE,),
    )

    assert outcome.verdict == VERDICT_INCONCLUSIVE
    assert gateway.metadata_calls == 0 and gateway.bound_calls == 0


# ========== 4. 触发点白名单（架构层断言） ==========


def test_trigger_allowed_tiers_shape():
    assert TRIGGER_ALLOWED_TIERS[TRIGGER_DAILY] == (TIER_METADATA, TIER_MAX_TOKENS_BOUND)
    for trigger in (TRIGGER_SAVE, TRIGGER_MANUAL):
        assert TIER_NEEDLE in TRIGGER_ALLOWED_TIERS[trigger]


def test_daily_trigger_with_needle_tier_raises_assertion_error():
    """日频挂 needle = 每个用户每天 ≈1M token，必须抛断言错误而不是静默降级。"""
    with pytest.raises(AssertionError) as exc_info:
        assert_tier_allowed(TRIGGER_DAILY, TIER_NEEDLE)
    assert "needle" in str(exc_info.value)


@pytest.mark.anyio
async def test_daily_probe_with_needle_tier_raises_before_any_request(gateway):
    """真入口同样拦：抛断言错误时一个请求都没发出。"""
    with pytest.raises(AssertionError):
        await probe_model_context_window(
            provider="openai",
            base_url=GATEWAY,
            api_key=API_KEY,
            model=QUALIFIED_MODEL,
            trigger=TRIGGER_DAILY,
            tiers=(TIER_NEEDLE,),
        )
    assert gateway.total_calls == 0


def test_unknown_trigger_is_refused():
    """新增触发点忘记登记到白名单 ⇒ 直接抛，不许默认允许一切档。"""
    with pytest.raises(AssertionError):
        assert_tier_allowed("nightly", TIER_METADATA)


# ========== 门禁 DB / 服务夹具 ==========


class _RecordingProvider:
    """记录每一次 provider 调用：断言「零调用」即证明不合格模型从未发出去。"""

    def __init__(self):
        self.calls = []

    async def generate(self, **kwargs):
        self.calls.append(kwargs)
        return {"content": '{"a": 1}', "finish_reason": "stop", "tool_calls": []}

    async def generate_stream(self, **kwargs):
        self.calls.append(kwargs)
        yield '{"a": 1}'


def _qualified_entry(tokens=1_048_576, *, source=SOURCE_PROBE, checked_at=None):
    return {
        "result": VERDICT_QUALIFIED,
        "source": source,
        "context_window_tokens": tokens,
        "tier": TIER_METADATA,
        "detail": "seeded",
        "checked_at": checked_at or _now_iso(),
    }


def _unqualified_entry(tokens=128_000):
    return {
        "result": VERDICT_UNQUALIFIED,
        "source": SOURCE_PROBE,
        "context_window_tokens": tokens,
        "tier": TIER_MAX_TOKENS_BOUND,
        "detail": "seeded",
        "checked_at": _now_iso(),
    }


@pytest.fixture
async def db_factory():
    db_path = f"/tmp/test_ctxwin_{uuid.uuid4().hex}.db"
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


async def seed_settings(factory, user_id, *, preferences=None, llm_model=QUALIFIED_MODEL):
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


@pytest.fixture
def service_factory(monkeypatch):
    """用户绑定的 AIService（带 user_id + db_session ⇒ 门禁生效），provider 换成录音桩。"""
    for attr in ("openai_api_key", "anthropic_api_key", "gemini_api_key"):
        monkeypatch.setattr("app.services.ai_service.app_settings." + attr, None, raising=False)

    def _make(user_id, session, *, default_model=QUALIFIED_MODEL):
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

    return _make


# ========== 5/7. 门禁绑定实发模型 ==========

AI_ENTRIES = ["generate_text", "generate_text_stream", "generate_text_stream_full", "call_with_json_retry"]


async def _dispatch(svc, entry, **kwargs):
    if entry == "generate_text":
        return await svc.generate_text(prompt=NEUTRAL_PROMPT, **kwargs)
    if entry == "generate_text_stream":
        return [chunk async for chunk in svc.generate_text_stream(prompt=NEUTRAL_PROMPT, **kwargs)]
    if entry == "generate_text_stream_full":
        return await svc.generate_text_stream_full(prompt=NEUTRAL_PROMPT, **kwargs)
    if entry == "call_with_json_retry":
        return await svc.call_with_json_retry(prompt=NEUTRAL_PROMPT, max_retries=1, **kwargs)
    raise AssertionError(f"unknown entry {entry!r}")  # pragma: no cover - 防止入口名拼错静默通过


@pytest.mark.anyio
@pytest.mark.parametrize("entry", AI_ENTRIES)
async def test_qualified_default_dispatches_without_any_probe(db_factory, service_factory, entry, gateway):
    """合格结论已在缓存里 ⇒ 热路径零网络（读一次行/dict，0 token，也不再探测）。"""
    user_id = f"u-hot-{entry}"
    await seed_settings(db_factory, user_id, preferences={
        PREFERENCES_KEY: {triple_key("openai", GATEWAY, QUALIFIED_MODEL): _qualified_entry()}
    })
    async with db_factory() as session:
        svc, provider = service_factory(user_id, session)
        await _dispatch(svc, entry)

    assert provider.calls, f"{entry}: 请求根本没发出去"
    assert provider.calls[0]["model"] == QUALIFIED_MODEL
    assert gateway.total_calls == 0, "缓存已有合格结论时不该再探测"


@pytest.mark.anyio
@pytest.mark.parametrize("entry", AI_ENTRIES)
async def test_per_request_small_model_is_rejected_at_dispatch(db_factory, service_factory, entry, gateway):
    """本需求的靶心：默认模型合格，逐次传 gpt-4o-mini ⇒ 在实发那一刻被拦，provider 零调用。

    `custom_model` 取自请求体 generate_request.model 与后台任务 task_input["model"]，
    最终写进 generate_kwargs["model"] ⇒ 只在保存时判定的门禁会被这条路径完全绕过。
    """
    user_id = f"u-bypass-{entry}"
    await seed_settings(db_factory, user_id, preferences={
        PREFERENCES_KEY: {triple_key("openai", GATEWAY, QUALIFIED_MODEL): _qualified_entry()}
    })
    task_input = {"model": SMALL_MODEL}  # 后台任务输入形态（chapters 的 task_input.get("model")）
    async with db_factory() as session:
        svc, provider = service_factory(user_id, session)
        with pytest.raises(ApiError) as exc_info:
            await _dispatch(svc, entry, model=task_input["model"])

    assert exc_info.value.code == BELOW_MINIMUM
    assert exc_info.value.params["model"] == SMALL_MODEL
    assert provider.calls == [], f"{entry}: 带着 128K 模型的请求真的发出去了"
    assert gateway.bound_calls == 1, "未见过的三元组必须同步补测 ①② 后再定论"


@pytest.mark.anyio
@pytest.mark.parametrize("entry", AI_ENTRIES)
async def test_per_call_provider_override_is_judged_on_the_dispatched_triple(
    db_factory, service_factory, entry, gateway, monkeypatch
):
    """门禁判定的是**本次实发**的三元组，不是用户保存的那家网关（#55 审核项）。

    `provider` 与 `model` 一样取自请求体（outlines/wizard_stream/polish 都传
    `data.get("provider")` / `request.provider`），最终喂给 `_get_provider(provider)`。
    修复前 `_require_model` 只看实例自己的 (api_provider, base_url, api_key) ⇒ 拿用户的
    合格网关给**另一个 host** 开合格证（wrong-host admit）。窗口是 (provider, base_url,
    model) 三元组的属性，不是模型名的属性 —— 所以两半都种成**同一个模型名**：
    只有键不同，才测得出「门禁查的是哪一把键」。
    """
    monkeypatch.setattr(
        "app.services.ai_service.app_settings.anthropic_base_url", OTHER_GATEWAY, raising=False
    )
    own = triple_key("openai", GATEWAY, QUALIFIED_MODEL)          # 用户保存的网关
    dispatched = triple_key("anthropic", OTHER_GATEWAY, QUALIFIED_MODEL)  # 本次真正会发的 host

    # 实发三元组不合格 ⇒ 必须被拦（哪怕保存的那条合格）
    user_id = f"u-override-bad-{entry}"
    await seed_settings(db_factory, user_id, preferences={
        PREFERENCES_KEY: {own: _qualified_entry(), dispatched: _unqualified_entry()}
    })
    async with db_factory() as session:
        svc, own_stub = service_factory(user_id, session)
        other_stub = _RecordingProvider()
        svc._anthropic_provider = other_stub
        with pytest.raises(ApiError) as exc_info:
            await _dispatch(svc, entry, provider="anthropic")

    assert exc_info.value.code == BELOW_MINIMUM
    assert exc_info.value.params["model"] == QUALIFIED_MODEL
    assert other_stub.calls == [] and own_stub.calls == [], f"{entry}: 别家的不合格模型真的发出去了"
    assert gateway.total_calls == 0, "两个三元组都已有结论 ⇒ 派发路径不该打任何网络"

    # 反向对照：只有实发三元组换成合格才放行，且请求确实落在别家槽位
    # （门禁若仍按保存的三元组判定，这一步会被误拒 ⇒ 两半合起来才钉住「同一把键」）
    user_id = f"u-override-ok-{entry}"
    await seed_settings(db_factory, user_id, preferences={
        PREFERENCES_KEY: {own: _unqualified_entry(), dispatched: _qualified_entry()}
    })
    async with db_factory() as session:
        svc, own_stub = service_factory(user_id, session)
        other_stub = _RecordingProvider()
        svc._anthropic_provider = other_stub
        await _dispatch(svc, entry, provider="anthropic")

    assert other_stub.calls and other_stub.calls[0]["model"] == QUALIFIED_MODEL, f"{entry}: 没派发到别家槽位"
    assert own_stub.calls == [], f"{entry}: 请求发给了保存的 provider"
    assert gateway.total_calls == 0, "命中缓存还去探测 ⇒ 门禁算的不是实发三元组"


@pytest.mark.anyio
async def test_per_call_provider_override_probes_and_caches_the_dispatched_triple(
    db_factory, service_factory, gateway, monkeypatch
):
    """别家三元组**从未有过结论** ⇒ 同步补测必须打在别家 host 上，结论也按别家的键落缓存。

    钉住「键算错」的另一半：修复前探测与写键都用用户自己的 (openai, gw.test) ⇒
    一个从没被量过的 host 白拿合格证。这里让别家在 ① 档自报 128K（同一个模型名在
    用户自己的网关上是合格的），于是「探了谁家」直接由 URL 与缓存键可观察。
    """
    monkeypatch.setattr(
        "app.services.ai_service.app_settings.anthropic_base_url", OTHER_GATEWAY, raising=False
    )
    gateway.metadata_status = 200
    gateway.metadata_body = {"context_length": 128_000}
    user_id = "u-override-probe"
    await seed_settings(db_factory, user_id, preferences={"theme_seed": 7})
    async with db_factory() as session:
        svc, own_stub = service_factory(user_id, session)
        other_stub = _RecordingProvider()
        svc._anthropic_provider = other_stub
        with pytest.raises(ApiError) as exc_info:
            await svc.generate_text(prompt=NEUTRAL_PROMPT, provider="anthropic")

    assert exc_info.value.code == BELOW_MINIMUM
    assert own_stub.calls == [] and other_stub.calls == []
    assert gateway.metadata_calls == 1 and gateway.bound_calls == 0, "未见过的三元组必须同步补测 ①②"
    assert [c["url"] for c in gateway.calls] == [f"{OTHER_GATEWAY}/v1/models/{QUALIFIED_MODEL}"], (
        "补测打到了用户自己的网关 ⇒ 门禁量的不是本次实发的 host"
    )
    async with db_factory() as check:
        row = (await check.execute(select(Settings).where(Settings.user_id == user_id))).scalar_one()
        blob = json.loads(row.preferences)
        stored = blob[PREFERENCES_KEY]
        assert stored[triple_key("anthropic", OTHER_GATEWAY, QUALIFIED_MODEL)]["result"] == VERDICT_UNQUALIFIED
        assert triple_key("openai", GATEWAY, QUALIFIED_MODEL) not in stored, "结论被写到了保存的三元组上"
        assert blob["theme_seed"] == 7, "缓存写入抹掉了无关的偏好键"


@pytest.mark.anyio
async def test_full_book_budget_follows_the_dispatched_provider(db_factory, service_factory, monkeypatch):
    """预算换算同样按**实发三元组**读窗口：两家都合格但窗口不同 ⇒ 数字必须跟着覆盖走。"""
    monkeypatch.setattr(
        "app.services.ai_service.app_settings.anthropic_base_url", OTHER_GATEWAY, raising=False
    )
    user_id = "u-override-budget"
    await seed_settings(db_factory, user_id, preferences={PREFERENCES_KEY: {
        triple_key("openai", GATEWAY, QUALIFIED_MODEL): _qualified_entry(tokens=1_048_576),
        triple_key("anthropic", OTHER_GATEWAY, QUALIFIED_MODEL): _qualified_entry(tokens=2_000_000),
    }})
    async with db_factory() as session:
        svc, _ = service_factory(user_id, session)
        own_budget = await svc.resolve_full_book_budget_chars(QUALIFIED_MODEL)
        override_budget = await svc.resolve_full_book_budget_chars(QUALIFIED_MODEL, "anthropic")

    assert own_budget == int(1_048_576 * _FULL_BOOK_BUDGET_RATIO)
    assert override_budget == int(2_000_000 * _FULL_BOOK_BUDGET_RATIO), (
        "预算按保存的三元组读 ⇒ 窗口量错了 host"
    )


@pytest.mark.anyio
async def test_legacy_user_without_any_verdict_is_probed_then_rejected(db_factory, service_factory, gateway):
    """存量用户（升级后从未有结论）：先同步补测 ①② 再定论，既不是直接拒也不是放行。"""
    user_id = "u-legacy"
    await seed_settings(db_factory, user_id, llm_model=SMALL_MODEL, preferences={"theme_seed": 7})
    async with db_factory() as session:
        svc, provider = service_factory(user_id, session, default_model=SMALL_MODEL)
        with pytest.raises(ApiError) as exc_info:
            await svc.generate_text(prompt=NEUTRAL_PROMPT)

    assert exc_info.value.code == BELOW_MINIMUM
    assert gateway.total_calls == 2, "首次定论必须同步跑完 ①②"
    assert provider.calls == []
    async with db_factory() as check:
        row = (await check.execute(select(Settings).where(Settings.user_id == user_id))).scalar_one()
        blob = json.loads(row.preferences)
        stored = blob[PREFERENCES_KEY][triple_key("openai", GATEWAY, SMALL_MODEL)]
        assert stored["result"] == VERDICT_UNQUALIFIED
        assert stored["source"] == SOURCE_PROBE
        assert stored["checked_at"]
        assert blob["theme_seed"] == 7, "缓存写入抹掉了无关的偏好键"


@pytest.mark.anyio
async def test_stale_verdict_uses_existing_conclusion_and_rechecks_in_background(
    db_factory, service_factory, monkeypatch
):
    """已有结论只是今天没复测 ⇒ 本次直接按现有结论执行，复测 fire-and-forget（绝不 await）。

    证伪方式：让后台复测返回「不合格」。若派发路径 await 了复测，本次请求就会被拦；
    断言它照常成功，才真正钉住「不 await」。复测落地后下一次派发才按新结论拒。
    """
    user_id = "u-stale"
    key = triple_key("openai", GATEWAY, QUALIFIED_MODEL)
    await seed_settings(db_factory, user_id, preferences={PREFERENCES_KEY: {
        key: _qualified_entry(checked_at=_days_ago_iso(3))
    }})
    probes = []
    opened_sessions = []

    async def fake_probe(**kwargs):
        probes.append(kwargs)
        return ProbeOutcome(
            verdict=VERDICT_UNQUALIFIED,
            source=SOURCE_PROBE,
            tier=TIER_MAX_TOKENS_BOUND,
            detail="gateway downgraded to a small window",
        )

    async def fake_session(user):
        session = db_factory()
        opened_sessions.append(session)
        return session

    probe_module.memo_clear()
    monkeypatch.setattr(probe_module, "probe_model_context_window", fake_probe)
    monkeypatch.setattr(probe_module, "_open_user_session", fake_session)

    async with db_factory() as session:
        svc, provider = service_factory(user_id, session)
        await svc.generate_text(prompt=NEUTRAL_PROMPT)

    assert provider.calls and provider.calls[0]["model"] == QUALIFIED_MODEL, "过期结论不该拦本次请求"
    assert len(probes) <= 1, "派发路径不得同步等复测（只有首次定论才 await）"
    pending = probe_module.pending_recheck_tasks()
    assert pending, "过期结论必须排队 fire-and-forget 后台复测"
    await asyncio.gather(*pending)
    assert probes[0]["trigger"] == TRIGGER_DAILY, "后台复测只能以 daily 触发点跑（只允许 ①②）"

    # 复测把新结论落了缓存 ⇒ 下一次派发按新结论拒绝（抓网关侧降配）
    probe_module.memo_clear()
    async with db_factory() as session:
        svc, provider = service_factory(user_id, session)
        with pytest.raises(ApiError) as exc_info:
            await svc.generate_text(prompt=NEUTRAL_PROMPT)
    assert exc_info.value.code == BELOW_MINIMUM

    async with db_factory() as check:
        row = (await check.execute(select(Settings).where(Settings.user_id == user_id))).scalar_one()
        assert json.loads(row.preferences)[PREFERENCES_KEY][key]["result"] == VERDICT_UNQUALIFIED
    for session in opened_sessions:
        await session.close()


@pytest.mark.anyio
async def test_gate_writes_verdict_inside_per_user_write_lock(db_factory, gateway):
    """结论缓存写入必须取 per-user 写锁（#56）：否则与设置保存互抹整个 blob。"""
    user_id = "u-lock"

    class _CountingLock(asyncio.Lock):
        def __init__(self):
            super().__init__()
            self.acquisitions = 0

        async def acquire(self):  # type: ignore[override]
            acquired = await super().acquire()
            self.acquisitions += 1
            return acquired

    await seed_settings(db_factory, user_id, llm_model=SMALL_MODEL, preferences={"theme_seed": 7})
    spy = _CountingLock()
    db_write_locks[user_id] = spy
    try:
        async with db_factory() as session:
            with pytest.raises(ApiError) as exc_info:
                await ensure_model_allowed(
                    user_id=user_id, db=session, provider="openai", base_url=GATEWAY,
                    api_key=API_KEY, model=SMALL_MODEL, trigger=TRIGGER_SAVE,
                )
    finally:
        db_write_locks.pop(user_id, None)

    assert exc_info.value.code == BELOW_MINIMUM
    assert spy.acquisitions == 1, "探测结论写入未取 per-user 写锁（会与其他 preferences 写入互抹）"


@pytest.mark.anyio
async def test_probe_inside_a_held_write_lock_times_out_instead_of_deadlocking(
    db_factory, gateway, monkeypatch
):
    """写锁**不可重入**：若将来有人在临界区里调了探测，必须超时跳过缓存，而不是挂死生产。"""
    from app.services import model_capability_probe as module

    user_id = "u-nest"
    await seed_settings(db_factory, user_id, llm_model=SMALL_MODEL, preferences={})
    monkeypatch.setattr(module, "CACHE_LOCK_ACQUIRE_TIMEOUT_SECONDS", 0.2)
    async with db_factory() as session:
        async with db_write_lock(user_id):  # 模拟既有临界区
            with pytest.raises(ApiError) as exc_info:
                await ensure_model_allowed(
                    user_id=user_id, db=session, provider="openai", base_url=GATEWAY,
                    api_key=API_KEY, model=SMALL_MODEL, trigger=TRIGGER_SAVE,
                )
    assert exc_info.value.code == BELOW_MINIMUM, "判定本身仍要成立（只是没缓存）"


def test_dispatch_path_can_only_use_the_daily_trigger():
    """派发路径的触发点必须是 daily：结构上禁止把 needle 档挂到每次实发上。"""
    source = inspect.getsource(AIService._require_model)
    assert "TRIGGER_DAILY" in source
    assert "TRIGGER_SAVE" not in source
    assert "TRIGGER_MANUAL" not in source


# ========== 6. get_effective_context_window 失败契约 ==========


@pytest.mark.anyio
async def test_effective_window_raises_instead_of_returning_zero(db_factory, gateway):
    """无合格结论必须抛错：返回 0 在本仓库已有真实语义（＝禁用），等于静默关功能。"""
    user_id = "u-effective"
    await seed_settings(db_factory, user_id, preferences={"theme_seed": 7})
    async with db_factory() as session:
        with pytest.raises(ApiError) as exc_info:
            await get_effective_context_window(user_id, QUALIFIED_MODEL, session)

    assert exc_info.value.code == BELOW_MINIMUM
    assert gateway.total_calls == 0, "访问器只读缓存，不该自己发探测请求"


@pytest.mark.anyio
async def test_effective_window_rejects_inconclusive_and_unqualified(db_factory):
    """三态里只有 qualified 能给出数字；unqualified/inconclusive 一律抛。"""
    user_id = "u-effective-two"
    small_key = triple_key("openai", GATEWAY, SMALL_MODEL)
    unknown_key = triple_key("openai", GATEWAY, UNKNOWN_MODEL)
    await seed_settings(db_factory, user_id, preferences={PREFERENCES_KEY: {
        small_key: {"result": VERDICT_UNQUALIFIED, "source": SOURCE_PROBE, "checked_at": _now_iso()},
        unknown_key: {"result": VERDICT_INCONCLUSIVE, "source": SOURCE_PROBE, "checked_at": _now_iso()},
    }})
    async with db_factory() as session:
        for model in (SMALL_MODEL, UNKNOWN_MODEL):
            with pytest.raises(ApiError):
                await get_effective_context_window(user_id, model, session, provider="openai", base_url=GATEWAY)


@pytest.mark.anyio
async def test_effective_window_returns_cached_qualified_number(db_factory):
    user_id = "u-effective-ok"
    await seed_settings(db_factory, user_id, preferences={
        PREFERENCES_KEY: {triple_key("openai", GATEWAY, QUALIFIED_MODEL): _qualified_entry(1_048_576)}
    })
    async with db_factory() as session:
        assert await get_effective_context_window(user_id, QUALIFIED_MODEL, session) == 1_048_576


# ========== 保存路径：硬拦与显式声明 ==========


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


async def post_settings(env, user_id, payload):
    async with env.client() as client:
        return await client.post("/settings", json=payload, headers={"x-test-user": user_id})


async def read_row(env, user_id):
    async with env.session_factory() as session:
        return (await session.execute(select(Settings).where(Settings.user_id == user_id))).scalar_one()


@pytest.mark.anyio
async def test_saving_a_real_128k_model_is_rejected(env, gateway):
    """验收：真实 128K 级模型保存 ⇒ 被拒，且没被写进用户配置。"""
    user_id = "u-save-small"
    await seed_settings(env.session_factory, user_id, llm_model="")

    resp = await post_settings(env, user_id, {
        "api_provider": "openai", "api_key": API_KEY, "api_base_url": GATEWAY, "llm_model": SMALL_MODEL,
    })

    assert resp.status_code == 400, resp.text
    body = resp.json()
    assert body["code"] == BELOW_MINIMUM
    assert body["params"]["verdict"] == VERDICT_UNQUALIFIED
    assert body["params"]["min_window"] == MIN_CONTEXT_WINDOW_TOKENS
    assert (await read_row(env, user_id)).llm_model != SMALL_MODEL, "被拒的模型居然存下来了"


@pytest.mark.anyio
async def test_unknown_model_requires_explicit_declaration_then_accepts_it(env, gateway):
    """未登记模型 ⇒ 先拒（要求显式声明）；声明 ≥1M 后可保存并落 user_declared 结论。"""
    user_id = "u-save-unknown"
    await seed_settings(env.session_factory, user_id, llm_model="")
    gateway.bound_status = 401
    gateway.bound_body = b'{"error":{"message":"invalid api key"}}'

    first = await post_settings(env, user_id, {
        "api_provider": "openai", "api_key": API_KEY, "api_base_url": GATEWAY, "llm_model": UNKNOWN_MODEL,
    })
    assert first.status_code == 400, first.text
    assert first.json()["params"]["requires_explicit_declaration"] is True
    assert first.json()["params"]["verdict"] == VERDICT_INCONCLUSIVE

    ok = await post_settings(env, user_id, {
        "api_provider": "openai", "api_key": API_KEY, "api_base_url": GATEWAY,
        "llm_model": UNKNOWN_MODEL, "context_window_tokens": 1_048_576,
    })
    assert ok.status_code == 200, ok.text
    row = await read_row(env, user_id)
    assert row.llm_model == UNKNOWN_MODEL
    declared = json.loads(row.preferences)[PREFERENCES_KEY][triple_key("openai", GATEWAY, UNKNOWN_MODEL)]
    assert declared["result"] == VERDICT_QUALIFIED
    assert declared["source"] == SOURCE_USER_DECLARED
    assert declared["context_window_tokens"] == 1_048_576


@pytest.mark.anyio
async def test_declaring_below_minimum_still_rejects(env, gateway):
    """声明 <1M 依然被拒——声明不是勾选放行通道。"""
    user_id = "u-save-bad-declaration"
    await seed_settings(env.session_factory, user_id, llm_model="")
    gateway.bound_status = 401
    gateway.bound_body = b'{"error":{"message":"invalid api key"}}'

    resp = await post_settings(env, user_id, {
        "api_provider": "openai", "api_key": API_KEY, "api_base_url": GATEWAY,
        "llm_model": UNKNOWN_MODEL, "context_window_tokens": 128_000,
    })

    assert resp.status_code == 400, resp.text
    assert resp.json()["code"] == BELOW_MINIMUM
    assert resp.json()["params"]["declared_context_window_tokens"] == 128_000
    assert (await read_row(env, user_id)).llm_model != UNKNOWN_MODEL


@pytest.mark.anyio
async def test_declaration_cannot_overwrite_a_measured_small_model(env, gateway):
    """实测 <1M 的模型即使声明 ≥1M 也不放行：硬拦没有勾选通道（计划 §2 表第 2 行）。"""
    user_id = "u-save-lie"
    await seed_settings(env.session_factory, user_id, llm_model="")

    resp = await post_settings(env, user_id, {
        "api_provider": "openai", "api_key": API_KEY, "api_base_url": GATEWAY,
        "llm_model": SMALL_MODEL, "context_window_tokens": 2_000_000,
    })

    assert resp.status_code == 400, resp.text
    assert resp.json()["params"]["verdict"] == VERDICT_UNQUALIFIED
    assert (await read_row(env, user_id)).llm_model != SMALL_MODEL


@pytest.mark.anyio
async def test_false_reject_from_output_cap_is_rescued_by_declaration(env, gateway):
    """#65 的出口：输出上限造成的判不出，用户声明 >=1M 就该救得回来。

    旧行为是把它当成 `<1M` 的实测 ⇒ 声明通道被 #59 刻意关闭 ⇒ 一台合规的大窗口模型
    永久锁死，而且界面上一个数字都没有（无从解释，也无从自救）。
    """
    user_id = "u-65-output-cap"
    await seed_settings(env.session_factory, user_id, llm_model="")
    gateway.bound_body = _OUTPUT_CAP_BODY

    first = await post_settings(env, user_id, {
        "api_provider": "openai", "api_key": API_KEY, "api_base_url": GATEWAY, "llm_model": QUALIFIED_MODEL,
    })
    assert first.status_code == 400, first.text
    params = first.json()["params"]
    assert params["verdict"] == VERDICT_INCONCLUSIVE, "输出上限不是窗口证据，不该报成实测不合格"
    assert params["requires_explicit_declaration"] is True
    assert params["measured_context_window_tokens"] is None

    ok = await post_settings(env, user_id, {
        "api_provider": "openai", "api_key": API_KEY, "api_base_url": GATEWAY,
        "llm_model": QUALIFIED_MODEL, "context_window_tokens": MIN_CONTEXT_WINDOW_TOKENS,
    })
    assert ok.status_code == 200, ok.text
    entry = json.loads((await read_row(env, user_id)).preferences)[PREFERENCES_KEY][
        triple_key("openai", GATEWAY, QUALIFIED_MODEL)
    ]
    assert entry["result"] == VERDICT_QUALIFIED
    assert entry["source"] == SOURCE_USER_DECLARED


@pytest.mark.anyio
async def test_measured_128k_rejection_body_is_still_not_rescuable(env, gateway):
    """#59 的护栏在 #65 修法之后必须照样成立：报得出 128000 的网关，声明翻不了盘。"""
    user_id = "u-65-honest-small"
    await seed_settings(env.session_factory, user_id, llm_model="")
    gateway.bound_body = _HONEST_128K_BODY

    first = await post_settings(env, user_id, {
        "api_provider": "openai", "api_key": API_KEY, "api_base_url": GATEWAY, "llm_model": SMALL_MODEL,
    })
    assert first.status_code == 400, first.text
    params = first.json()["params"]
    assert params["verdict"] == VERDICT_UNQUALIFIED
    assert params["measured_context_window_tokens"] == 128_000
    assert params["requires_explicit_declaration"] is False, (
        "实测低于下限还提示「去声明」= 给一条走不通的路画饼"
    )

    for attempt in (1, 2):
        lied = await post_settings(env, user_id, {
            "api_provider": "openai", "api_key": API_KEY, "api_base_url": GATEWAY,
            "llm_model": SMALL_MODEL, "context_window_tokens": 2_000_000,
        })
        assert lied.status_code == 400, f"第 {attempt} 次声明居然放行了实测 128K 的模型"
        assert lied.json()["params"]["verdict"] == VERDICT_UNQUALIFIED
    assert (await read_row(env, user_id)).llm_model != SMALL_MODEL


@pytest.mark.anyio
async def test_saving_a_probed_qualified_model_passes(env, gateway):
    """未被误伤：实测 >=1M 的模型照常保存。"""
    gateway.metadata_status = 200
    gateway.metadata_body = {"id": QUALIFIED_MODEL, "context_length": 1_048_576}
    user_id = "u-save-ok"
    await seed_settings(env.session_factory, user_id, llm_model="")

    resp = await post_settings(env, user_id, {
        "api_provider": "openai", "api_key": API_KEY, "api_base_url": GATEWAY, "llm_model": QUALIFIED_MODEL,
    })

    assert resp.status_code == 200, resp.text
    assert (await read_row(env, user_id)).llm_model == QUALIFIED_MODEL


@pytest.mark.anyio
async def test_saving_without_a_model_is_not_blocked_by_the_gate(env, gateway):
    """步骤 2 的语义不回归：清空模型仍可保存（未配置由使用点报 not_configured）。"""
    user_id = "u-save-empty"
    await seed_settings(env.session_factory, user_id, llm_model=QUALIFIED_MODEL)

    resp = await post_settings(env, user_id, {
        "api_provider": "openai", "api_key": API_KEY, "api_base_url": GATEWAY, "llm_model": "",
    })

    assert resp.status_code == 200, resp.text
    assert (await read_row(env, user_id)).llm_model == ""


# ========== check-context-window 端点（与 check-function-calling 同形态） ==========


@pytest.mark.anyio
async def test_check_context_window_endpoint_shape_and_three_numbers(env, gateway):
    """端点复用 check-function-calling 的结果结构，并给出表单要的三段数。"""
    user_id = "u-check-endpoint"
    await seed_settings(env.session_factory, user_id, llm_model=SMALL_MODEL)
    gateway.metadata_status = 200
    gateway.metadata_body = {"id": SMALL_MODEL, "context_length": 128_000}

    async with env.client() as client:
        resp = await client.post(
            "/settings/check-context-window",
            json={
                "provider": "openai", "api_key": API_KEY, "api_base_url": GATEWAY,
                "llm_model": SMALL_MODEL, "context_window_tokens": 900_000,
            },
            headers={"x-test-user": user_id},
        )

    assert resp.status_code == 200, resp.text
    body = resp.json()
    for key in ("success", "supported", "message", "response_time_ms", "provider", "model", "details"):
        assert key in body, f"结果结构缺少与 check-function-calling 同形的字段 {key}"
    assert body["success"] is True
    assert body["supported"] is False
    assert body["model"] == SMALL_MODEL
    assert body["details"]["verdict"] == VERDICT_UNQUALIFIED
    assert body["details"]["min_window"] == MIN_CONTEXT_WINDOW_TOKENS
    assert body["details"]["tiers_run"] == [TIER_METADATA]
    display = body["details"]["window_display"]
    assert display["probed_context_window_tokens"] == 128_000
    assert display["declared_context_window_tokens"] == 900_000
    assert display["minimum_required_context_window_tokens"] == MIN_CONTEXT_WINDOW_TOKENS


@pytest.mark.anyio
async def test_check_context_window_adopted_number_follows_the_gate_not_the_declaration(env, gateway):
    """第三个数必须等于门禁真正会采纳的那笔预算（评审第 5 项：客户端不再自己算）。

    旧写法是 `supported ? measured : (declared if declared >= MIN else None)`——它在
    「实测 128K + 声明 2M」上回 2,000,000，而 `ensure_model_allowed` 对实测不合格的模型
    **根本不看声明**、直接拒保存。端点于是替一笔必被拒的保存报了个预算，
    表单显示「会用 2M」而系统什么都不采纳：正是本分支要根除的静默失败形态。
    """
    user_id = "u-check-adopted"
    await seed_settings(env.session_factory, user_id, llm_model=SMALL_MODEL)

    async def display_for(*, metadata_body, bound_status, bound_body, declared):
        gateway.metadata_status = 200 if metadata_body is not None else 404
        gateway.metadata_body = metadata_body
        gateway.bound_status = bound_status
        gateway.bound_body = bound_body
        async with env.client() as client:
            resp = await client.post(
                "/settings/check-context-window",
                json={
                    "provider": "openai", "api_key": API_KEY, "api_base_url": GATEWAY,
                    "llm_model": SMALL_MODEL, "context_window_tokens": declared,
                },
                headers={"x-test-user": user_id},
            )
        assert resp.status_code == 200, resp.text
        body = resp.json()["details"]
        # 键必须恒在：前端要能区分「服务器说了没有预算」与「服务器根本没回答」
        assert "adopted_context_window_tokens" in body["window_display"]
        return body["verdict"], body["window_display"]

    probe_module.memo_clear()

    # 1) 实测合格 ⇒ 采纳实测值
    verdict, shown = await display_for(
        metadata_body={"id": SMALL_MODEL, "context_length": 2_000_000},
        bound_status=400, bound_body=_BOUND_REJECTION_BODY, declared=None,
    )
    assert verdict == VERDICT_QUALIFIED
    assert shown["adopted_context_window_tokens"] == 2_000_000

    # 2) 实测不合格 + 声明 2M ⇒ **不采纳任何预算**（声明不是勾选放行通道）
    probe_module.memo_clear()
    verdict, shown = await display_for(
        metadata_body={"id": SMALL_MODEL, "context_length": 128_000},
        bound_status=400, bound_body=_BOUND_REJECTION_BODY, declared=2_000_000,
    )
    assert verdict == VERDICT_UNQUALIFIED
    assert shown["declared_context_window_tokens"] == 2_000_000
    assert shown["adopted_context_window_tokens"] is None, (
        "实测低于下限时替必拒的保存报了个预算：表单会显示一个永远不会用的数"
    )

    # 3) 判不出 + 声明达到下限 ⇒ 采纳声明（这正是声明该生效的那一态）
    probe_module.memo_clear()
    verdict, shown = await display_for(
        metadata_body=None, bound_status=401,
        bound_body=b'{"error":{"message":"invalid api key"}}', declared=2_000_000,
    )
    assert verdict == VERDICT_INCONCLUSIVE
    assert shown["adopted_context_window_tokens"] == 2_000_000

    # 4) 判不出 + 声明低于下限 ⇒ 什么都不采纳
    probe_module.memo_clear()
    verdict, shown = await display_for(
        metadata_body=None, bound_status=401,
        bound_body=b'{"error":{"message":"invalid api key"}}', declared=900_000,
    )
    assert verdict == VERDICT_INCONCLUSIVE
    assert shown["adopted_context_window_tokens"] is None


@pytest.mark.anyio
async def test_check_context_window_endpoint_caches_verdict(env, gateway):
    """手动「重新检测」把结论落进 preferences：三元组 + result + source + checked_at。"""
    user_id = "u-check-cache"
    await seed_settings(env.session_factory, user_id, preferences={"theme_seed": 3})
    gateway.metadata_status = 200
    gateway.metadata_body = {"id": QUALIFIED_MODEL, "context_length": 2_000_000}

    async with env.client() as client:
        resp = await client.post(
            "/settings/check-context-window",
            json={"provider": "openai", "api_key": API_KEY, "api_base_url": GATEWAY, "llm_model": QUALIFIED_MODEL},
            headers={"x-test-user": user_id},
        )

    assert resp.status_code == 200, resp.text
    assert resp.json()["supported"] is True
    blob = json.loads((await read_row(env, user_id)).preferences)
    assert blob["theme_seed"] == 3, "缓存写入抹掉了无关键"
    entry = blob[PREFERENCES_KEY][triple_key("openai", GATEWAY, QUALIFIED_MODEL)]
    assert entry["result"] == VERDICT_QUALIFIED
    assert entry["context_window_tokens"] == 2_000_000
    assert entry["source"] == SOURCE_PROBE


@pytest.mark.anyio
async def test_check_context_window_endpoint_reports_blind_spot(env, gateway):
    """诚实：② 档判出的合格必须带上「静默截断型网关仍可能误判合格」的提示。"""
    gateway.bound_status = 200

    user_id = "u-check-honest"
    await seed_settings(env.session_factory, user_id)
    async with env.client() as client:
        resp = await client.post(
            "/settings/check-context-window",
            json={"provider": "openai", "api_key": API_KEY, "api_base_url": GATEWAY, "llm_model": QUALIFIED_MODEL},
            headers={"x-test-user": user_id},
        )
    body = resp.json()
    assert body["details"]["tier"] == TIER_MAX_TOKENS_BOUND
    assert body["details"]["blind_spot"] and "静默截断" in body["details"]["blind_spot"]


# ========== 登记表降级 / 常量边界 ==========


@pytest.mark.anyio
async def test_known_context_windows_only_seeds_the_probe_scale(db_factory, gateway, monkeypatch):
    """登记表降级为「从哪个刻度开始探」的提示：它不进接受/判定。"""
    captured = {}
    original = probe_module.probe_max_tokens_bound_tier

    async def spy(**kwargs):
        captured.update(kwargs)
        return ProbeOutcome(verdict=VERDICT_QUALIFIED, context_window_tokens=1_048_576, tier=TIER_MAX_TOKENS_BOUND)

    monkeypatch.setattr(probe_module, "probe_max_tokens_bound_tier", spy)
    assert detect_context_window(SMALL_MODEL) == _KNOWN_CONTEXT_WINDOWS["gpt-4o"] < MIN_CONTEXT_WINDOW_TOKENS

    await probe_model_context_window(
        provider="openai", base_url=GATEWAY, api_key=API_KEY, model=SMALL_MODEL,
        hint_window_tokens=detect_context_window(SMALL_MODEL),
    )
    # 提示 128K 远低于下限 ⇒ 刻度仍取产品下限（否则等于让常量表决定判定）
    assert captured["probe_value"] == MIN_CONTEXT_WINDOW_TOKENS
    assert original is not None


@pytest.mark.anyio
async def test_hint_above_minimum_probes_higher_then_falls_back_once(gateway):
    """提示 2M：先在 2M 刻度探（更强证据），被拒后退回下限刻度，最多两次。"""
    gateway.bound_status = 200

    async def handler(request: httpx.Request) -> httpx.Response:
        gateway.calls.append({"path": request.url.path, "method": request.method, "body": request.content})
        payload = json.loads(request.content.decode() or "{}")
        if payload.get("max_tokens", 0) > MIN_CONTEXT_WINDOW_TOKENS:
            return httpx.Response(400, content=_BOUND_REJECTION_BODY)
        return httpx.Response(200, content=_SSE_FIRST_DELTA, headers={"content-type": "text/event-stream"})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    outcome = await probe_model_context_window(
        provider="openai", base_url=GATEWAY, api_key=API_KEY, model=QUALIFIED_MODEL,
        hint_window_tokens=2_000_000, client=client, tiers=(TIER_MAX_TOKENS_BOUND,),
    )
    await client.aclose()

    assert outcome.verdict == VERDICT_QUALIFIED
    assert outcome.context_window_tokens == MIN_CONTEXT_WINDOW_TOKENS
    assert gateway.bound_calls == 2, "上界被拒后只应退回下限刻度再探一次"


def test_min_window_constant_is_not_the_book_injection_line():
    """MIN_CONTEXT_WINDOW_TOKENS 是新常量；_1M_THRESHOLD 是「全书注入启用线」，语义不同。"""
    from app.services import ai_service

    assert MIN_CONTEXT_WINDOW_TOKENS == 1_000_000
    assert ai_service._1M_THRESHOLD != MIN_CONTEXT_WINDOW_TOKENS


def test_blind_spots_are_documented_in_source():
    """两个已知盲区必须写进实现，注释与测试都不得暗示系统不可被绕过。"""
    source = inspect.getsource(probe_module)
    assert "静默截断" in source
    assert "任何时刻都不会被绕过" in source
    assert "换模型" in source or "降配" in source


def test_gate_reuses_the_registered_error_code_only():
    """本步不新增错误码（文案归 3b）；前缀也禁止 warning.。"""
    assert BELOW_MINIMUM in ERROR_REGISTRY
    assert not any(code.startswith("warning.") for code in ERROR_REGISTRY)
    source = inspect.getsource(probe_module)
    assert "ApiError(code=" in source
