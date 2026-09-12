"""需求 #55 步骤 4：兜底与降级已删除的回归测试（含 §4b「0 = 禁用」语义消灭）。

钉住四件事：
1. **系统兜底模型常量消失**：`config.py` 不再有 `default_model` 字段。
2. **预算单一来源**：全书注入/拆书注入的字符预算只来自实发模型**实测/显式声明**
   的上下文窗口（`get_effective_context_window`），不再按模型名查静态登记表分三档；
   拿不到合格结论即抛 validation.* 错误码，且没有任何请求外发。
3. **`0 = 禁用` 语义消失**：两个上下文构建器与两个全书构建函数的预算参数改为
   **必填无默认值**，非正数当场抛错，`if > 0` 的开关式判断取消。
4. **红线仍在**：`book_import_service` 的「书太大 ⇒ head 全文 + 尾部加权全文 +
   中间摘要链」拆分必须保留（AGENTS.md 拆分优先原则）。

测试值一律中性占位（needle-model / gw.test / "body text"），不含任何导入原文、
角色人名或书名（AGENTS.md 原文数据脱敏硬约束）。
"""
import inspect
import json
import os
import re
import uuid
from datetime import datetime, timezone

import httpx
import pytest
from sqlalchemy import event, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

import app.models  # noqa: F401 - 注册全部表（settings 建表需要）
import app.services.model_capability_probe as probe_module
from app.api import chapters as chapters_module
from app.config import Settings as AppSettings
from app.core.errors import ApiError
from app.database import Base
from app.models.settings import Settings
from app.services.ai_service import AIService, _FULL_BOOK_BUDGET_RATIO
from app.services.book_import_service import BookImportService
from app.services.chapter_context_service import (
    OneToManyContextBuilder,
    OneToOneContextBuilder,
    _build_full_book_context,
)
from app.services.model_capability_probe import (
    PREFERENCES_KEY,
    SOURCE_PROBE,
    TIER_METADATA,
    VERDICT_QUALIFIED,
    VERDICT_UNQUALIFIED,
    memo_clear,
    triple_key,
)

NOT_CONFIGURED = "validation.ai_model_not_configured"
BELOW_MINIMUM = "validation.ai_model_below_minimum"
QUALIFIED_MODEL = "needle-model"        # **未登记**在 _KNOWN_CONTEXT_WINDOWS 里：
                                        # 预算若还查静态表就不可能得出 1M 结论
SMALL_MODEL = "gpt-4o-mini"
GATEWAY = "https://gw.test/v1"
API_KEY = "sk-stub-not-a-real-key"
PROBED_WINDOW_TOKENS = 1_048_576

REPO_BACKEND = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _verdict_entry(verdict, tokens):
    return {
        "result": verdict,
        "source": SOURCE_PROBE,
        "context_window_tokens": tokens,
        "tier": TIER_METADATA,
        "detail": "seeded",
        "checked_at": _now_iso(),
    }


class _Row:
    """中性章节桩：只有 chapter_number / title / content / summary / expansion_plan。"""

    def __init__(self, num, title, content, summary=None, expansion_plan=None):
        self.chapter_number = num
        self.title = title
        self.content = content
        self.summary = summary
        self.expansion_plan = expansion_plan


def _neutral_book(chapters=20, per_chapter=150):
    return [
        _Row(i, f"ch{i}", f"body text {i} " * per_chapter)
        for i in range(1, chapters + 1)
    ]


class _RecordingProvider:
    def __init__(self):
        self.calls = []

    async def generate(self, **kwargs):
        self.calls.append(kwargs)
        return {"content": "stub", "finish_reason": "stop", "tool_calls": []}

    async def generate_stream(self, **kwargs):
        self.calls.append(kwargs)
        yield "stub"


@pytest.fixture(autouse=True)
def _no_memo_leakage():
    memo_clear()
    yield
    memo_clear()


@pytest.fixture
def probe_gateway(monkeypatch):
    """探测客户端换成 MockTransport：本文件任何测试都不得真出网。"""
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url))
        return httpx.Response(404, json={"error": {"message": "no such endpoint"}})

    def _factory(transport=None):
        return httpx.AsyncClient(transport=httpx.MockTransport(handler))

    monkeypatch.setattr(probe_module, "create_probe_client", _factory)
    yield calls


@pytest.fixture
async def db_factory():
    db_path = f"/tmp/test_no_fallback_{uuid.uuid4().hex}.db"
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


async def seed_verdict(factory, user_id, model, *, verdict=VERDICT_QUALIFIED, tokens=None):
    blob = {PREFERENCES_KEY: {triple_key("openai", GATEWAY, model): _verdict_entry(verdict, tokens)}}
    async with factory() as session:
        session.add(Settings(
            user_id=user_id,
            api_provider="openai",
            api_key=API_KEY,
            api_base_url=GATEWAY,
            llm_model=model,
            temperature=0.7,
            max_tokens=2000,
            preferences=json.dumps(blob, ensure_ascii=False),
        ))
        await session.commit()


@pytest.fixture
def service_factory(monkeypatch):
    """真实 AIService（绑定 user_id + db_session ⇒ 门禁与预算走同一条路）。"""
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


# ========== 1. 系统兜底模型常量消失 ==========

def test_system_default_model_constant_is_gone():
    assert "default_model" not in AppSettings.model_fields, "config.py 仍在声明系统兜底模型"
    config_source = open(os.path.join(REPO_BACKEND, "app/config.py"), encoding="utf-8").read()
    assert 'default_model: str = "gpt-4"' not in config_source


# ========== 2. 分档降级与 32K 回退消失 ==========

def test_tiered_helpers_are_gone_from_modules():
    """`resolve_context_budget_chars` 与 book_import 的 detect_context_window 依赖必须消失。"""
    import app.services.ai_service as ai_module

    assert not hasattr(ai_module, "resolve_context_budget_chars")
    source = open(
        os.path.join(REPO_BACKEND, "app/services/book_import_service.py"), encoding="utf-8"
    ).read()
    assert "detect_context_window" not in source, "拆书路径仍在按模型名猜窗口"
    assert "32768" not in source, "拆书路径仍残留 32K 回退判据"


def test_chapters_budget_helper_is_async_single_source():
    """chapters 的预算助手必须是 async：同步版本只能来自静态登记表分档。"""
    assert inspect.iscoroutinefunction(chapters_module._resolve_full_book_budget)
    source = inspect.getsource(chapters_module._resolve_full_book_budget)
    assert "resolve_context_budget_chars(" not in source, "仍在调用已删除的分档预算函数"


# ========== 3. §4b：0 = 禁用 语义消灭 ==========

@pytest.mark.parametrize("builder", [OneToManyContextBuilder, OneToOneContextBuilder])
def test_builder_budget_parameter_has_no_default(builder):
    param = inspect.signature(builder.__init__).parameters["full_book_budget_chars"]
    assert param.default is inspect.Parameter.empty, f"{builder.__name__} 又出现了预算默认值"
    assert param.kind is inspect.Parameter.KEYWORD_ONLY


@pytest.mark.parametrize("builder", [OneToManyContextBuilder, OneToOneContextBuilder])
def test_builder_without_budget_raises_at_construction(builder):
    with pytest.raises(TypeError):
        builder()                       # 忘记传预算 ⇒ 构造即失败，而不是悄悄不注入
    with pytest.raises(TypeError):
        builder(None, None)


@pytest.mark.parametrize("bad", [0, -1, None, "1000", True, 1.0])
def test_builder_rejects_non_positive_or_non_integer_budget(bad):
    with pytest.raises((TypeError, ValueError)):
        OneToManyContextBuilder(full_book_budget_chars=bad)


def test_builder_keeps_a_positive_budget():
    assert OneToManyContextBuilder(full_book_budget_chars=600000).full_book_budget_chars == 600000
    assert OneToOneContextBuilder(full_book_budget_chars=600000).full_book_budget_chars == 600000


def test_full_book_context_builders_require_positive_budget():
    with pytest.raises(TypeError):
        _build_full_book_context(_neutral_book(chapters=2))          # 隐藏默认值已删除
    with pytest.raises(ValueError):
        _build_full_book_context(_neutral_book(chapters=2), budget_chars=0)

    svc = BookImportService()
    with pytest.raises(TypeError):
        svc._build_import_fulltext(_neutral_book(chapters=2))
    with pytest.raises(ValueError):
        svc._build_import_fulltext(_neutral_book(chapters=2), budget_chars=0)
    assert "model_name" not in inspect.signature(svc._build_import_fulltext).parameters


# ========== 4. 预算单一来源 = 实发模型实测/声明窗口 ==========

@pytest.mark.anyio
async def test_budget_comes_from_verdict_not_from_model_name(
    db_factory, service_factory, probe_gateway
):
    """未登记于静态表的模型，只要结论是 1M ⇒ 预算按结论算（旧分档只会得到猜测值）。"""
    user_id = f"u-budget-{uuid.uuid4().hex[:8]}"
    await seed_verdict(db_factory, user_id, QUALIFIED_MODEL, tokens=PROBED_WINDOW_TOKENS)
    async with db_factory() as session:
        svc, provider = service_factory(user_id, session)
        budget = await svc.resolve_full_book_budget_chars(QUALIFIED_MODEL)

    assert budget == int(PROBED_WINDOW_TOKENS * _FULL_BOOK_BUDGET_RATIO)
    assert budget > 0
    assert provider.calls == [], "预算换算不该发出 AI 请求"
    assert probe_gateway == [], "已有合格结论时不该再探测"


@pytest.mark.anyio
async def test_unqualified_verdict_raises_instead_of_a_small_budget(
    db_factory, service_factory, probe_gateway
):
    """结论不合格 ⇒ 抛 validation.ai_model_below_minimum，而不是给一个保守小预算。"""
    user_id = f"u-small-{uuid.uuid4().hex[:8]}"
    await seed_verdict(
        db_factory, user_id, SMALL_MODEL, verdict=VERDICT_UNQUALIFIED, tokens=128000
    )
    async with db_factory() as session:
        svc, provider = service_factory(user_id, session, default_model=SMALL_MODEL)
        with pytest.raises(ApiError) as exc_info:
            await svc.resolve_full_book_budget_chars(SMALL_MODEL)

    assert exc_info.value.code == BELOW_MINIMUM
    assert provider.calls == []


@pytest.mark.anyio
async def test_missing_model_raises_not_configured(db_factory, service_factory, probe_gateway):
    """未配置模型时预算换算报 not_configured（不得回退到任何猜测值）。"""
    user_id = f"u-none-{uuid.uuid4().hex[:8]}"
    await seed_verdict(db_factory, user_id, QUALIFIED_MODEL, tokens=PROBED_WINDOW_TOKENS)
    async with db_factory() as session:
        svc, provider = service_factory(user_id, session, default_model=None)
        with pytest.raises(ApiError) as exc_info:
            await svc.resolve_full_book_budget_chars(None)

    assert exc_info.value.code == NOT_CONFIGURED
    assert provider.calls == []


@pytest.mark.anyio
async def test_unbound_service_cannot_invent_a_budget(service_factory, probe_gateway):
    """未绑定用户/会话的服务拿不到结论 ⇒ 抛错，绝不返回 0 或猜测值。"""
    svc, provider = service_factory(None, None)
    with pytest.raises(ApiError) as exc_info:
        await svc.resolve_full_book_budget_chars(QUALIFIED_MODEL)
    assert exc_info.value.code == BELOW_MINIMUM
    assert provider.calls == []


# ========== 5. 验收回归：全量路径 / 超预算摘要链 ==========

@pytest.mark.anyio
async def test_whole_book_injection_takes_full_path_under_1m(
    db_factory, service_factory, probe_gateway
):
    """≥1M 配置下全书注入仍是全量：每章正文逐字在场，没有摘要链标记。"""
    user_id = f"u-full-{uuid.uuid4().hex[:8]}"
    await seed_verdict(db_factory, user_id, QUALIFIED_MODEL, tokens=PROBED_WINDOW_TOKENS)
    chapters = _neutral_book(chapters=20, per_chapter=150)  # ≈45K 字符，远小于预算
    async with db_factory() as session:
        svc, _ = service_factory(user_id, session)
        budget = await svc.resolve_full_book_budget_chars(QUALIFIED_MODEL)

    assert budget == int(PROBED_WINDOW_TOKENS * _FULL_BOOK_BUDGET_RATIO), (
        "预算不是来自实发模型的窗口结论（换了来源就该红）"
    )

    text = _build_full_book_context(chapters, budget_chars=budget)

    assert len(text) > 30000
    assert "【中间章节摘要链】" not in text
    for row in chapters:
        assert row.content in text, f"第{row.chapter_number}章正文未全量注入"
    assert OneToManyContextBuilder(full_book_budget_chars=budget).full_book_budget_chars == budget


def test_over_budget_book_still_produces_summary_chain():
    """红线：书体量超预算时的 head 全文 + 尾部加权全文 + 中间摘要链必须常在。

    每章 ≈131 字符、预算 600 ⇒ head(ch1) 与最近的 ch20/ch19/ch18 进全文，
    其余中间章节进摘要链。
    """
    chapters = _neutral_book(chapters=20, per_chapter=10)
    svc = BookImportService()

    text = svc._build_import_fulltext(chapters, budget_chars=600)

    assert "【中间章节摘要链】" in text, "超预算拆分链被删除（回归）"
    assert re.search(r"第\d+章《ch\d+》：", text), "摘要链里没有逐章一行的摘要条目"
    assert chapters[0].content in text, "head 全文被截断"
    assert chapters[-1].content in text, "尾部加权全文缺失"
    assert len(text) <= 600 * 1.6, "超预算拆分没有起到约束作用"


def test_over_budget_book_no_longer_retreats_to_truncated_excerpt():
    """被删掉的那条降级：窗口未知 ⇒ 整本书退回每章 1800 字符摘录。

    用 ≈2500 字符的头章做靶子：excerpt 形态必然把它截断到 1800，
    head 全文形态不会。
    """
    long_chapter = _Row(1, "ch1", "body text one " * 179)  # ≈2506 字符 > 1800
    chapters = [long_chapter] + _neutral_book(chapters=12, per_chapter=10)
    svc = BookImportService()

    text = svc._build_import_fulltext(chapters, budget_chars=3000)

    assert long_chapter.content in text, "头章被截断成 excerpt（退回分支复活）"
    assert "【中间章节摘要链】" in text, "预算没起到约束作用（该测试形态失效）"
