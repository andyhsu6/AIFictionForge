"""步骤 2（伞形需求 #55）：用户未配置模型即明确报错，系统不再替他猜一个。

背景：`config.py` 的 `default_model: str = "gpt-4"`（窗口表内仅 8192）曾被
`ai_service.py` 以 `default_model or app_settings.default_model` 回填，也被
`api/settings.py` 与 ORM 列默认值静默写进用户配置，导致「未配置」的用户拿到一个
8K 窗口的模型且毫无提示。计划要求：未配置 → 显式错误
`validation.ai_model_not_configured`，**不得**让 None 传到 provider 变成
400/422 或 provider 端随机报错。

必须区分的同名不同物：`<...>_ai_service.default_model` 装的是**用户自己配置的默认
模型**（语义「本次请求未指定则用用户配置的」），那不是兜底，一律保留；本文件末尾
的计数守卫就是钉这一点的。

测试值一律中性占位（needle-model / stub-model / gw.test），不含任何导入原文、
角色人名或书名（AGENTS.md 原文数据脱敏硬约束）。
"""
import json
import re
from pathlib import Path

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import app.models  # noqa: F401 - 注册全部表（settings 建表需要）
from app.core.errors import ERROR_REGISTRY, ApiError
from app.database import Base
from app.models.settings import Settings
from app.services.ai_service import AIService

NOT_CONFIGURED = "validation.ai_model_not_configured"
BELOW_MINIMUM = "validation.ai_model_below_minimum"

REPO_BACKEND = Path(__file__).resolve().parents[1]
FRONTEND_LOCALES = REPO_BACKEND.parent / "frontend/src/locales"

# 「用户自己配置的默认模型」读取点：逐个保留（计划 §1 点名 14 处 = chapters 11 +
# characters/organizations/outlines 各 1；实测另有 5 处同语义站点，见下方注释）。
USER_DEFAULT_MODEL_SITES = {
    "app/api/chapters.py": 11,
    "app/api/characters.py": 1,
    "app/api/organizations.py": 1,
    "app/api/outlines.py": 1,
    # 计划原文未列出、但与之完全同语义（本次请求未指定则用用户配置的默认模型）
    "app/services/book_import_service.py": 3,
    "app/services/project_agent_service.py": 2,
}

_USER_DEFAULT_PATTERNS = [
    re.compile(r"getattr\((?:user_|self\.)?ai_service\s*,\s*[\"']default_model[\"']"),
    re.compile(r"(?:user_)?ai_service\.default_model"),
]

# registry 允许的前缀（既有集合，禁止 warning.）
ALLOWED_PREFIXES = (
    "auth.", "conflict.", "email.", "forbidden.", "import.", "internal.",
    "not_found.", "progress.", "rate_limit.", "security.", "task.", "validation.",
    "dynamic_detail",
)


# ========== 错误码注册 ==========

def test_both_error_codes_registered():
    """两个码都必须注册：只配 `ai_model_not_configured` 会让步骤 3 抛未注册码。"""
    assert NOT_CONFIGURED in ERROR_REGISTRY, "未配置码必须注册"
    assert BELOW_MINIMUM in ERROR_REGISTRY, "窗口不合格码必须在本步一并注册（实发门禁在步骤 3）"
    for code in (NOT_CONFIGURED, BELOW_MINIMUM):
        detail, status = ERROR_REGISTRY[code]
        assert detail, f"{code} 必须有默认可读文案"
        assert status == 400, f"{code} 应为 400（用户可自助修正的配置问题）"


def test_registry_uses_only_existing_prefixes():
    """registry 前缀只允许既有集合，禁止 warning.（警告性质信息走响应体 warnings）。"""
    for code in ERROR_REGISTRY:
        assert code.startswith(ALLOWED_PREFIXES), f"非法错误码前缀: {code}"
    assert not any(code.startswith("warning.") for code in ERROR_REGISTRY)


# ========== 未配置即明确报错，且请求绝不外发 ==========

class _RecordingProvider:
    """记录每一次 provider 调用：断言「一次都没被调用」即证明无 model:null 出网。"""

    def __init__(self):
        self.calls = []

    async def generate(self, **kwargs):
        self.calls.append(kwargs)
        return {"content": "stub", "finish_reason": "stop", "tool_calls": []}

    async def generate_stream(self, **kwargs):
        self.calls.append(kwargs)
        yield "stub"


@pytest.fixture
def stub_provider_service(monkeypatch):
    """构造只挂着假 provider 的 AIService：不发真请求，且与本机 .env 完全解耦。"""
    # 屏蔽 .env 真实 key：否则 AIService 会自行构造真实 OpenAIClient
    for attr in ("openai_api_key", "anthropic_api_key", "gemini_api_key"):
        monkeypatch.setattr("app.services.ai_service.app_settings." + attr, None, raising=False)

    def _make(**overrides):
        overrides.setdefault("api_provider", "openai")
        overrides.setdefault("api_key", "sk-stub-not-a-real-key")
        overrides.setdefault("api_base_url", "https://gw.test/v1")
        svc = AIService(**overrides)
        provider = _RecordingProvider()
        svc._openai_provider = provider
        svc._anthropic_provider = None
        svc._gemini_provider = None
        return svc, provider

    return _make


async def _invoke(svc, entry):
    if entry == "generate_text":
        return await svc.generate_text(prompt="chapter one body text")
    if entry == "generate_text_stream":
        return [chunk async for chunk in svc.generate_text_stream(prompt="chapter one body text")]
    if entry == "generate_text_stream_full":
        return await svc.generate_text_stream_full(prompt="chapter one body text")
    if entry == "call_with_json_retry":
        return await svc.call_with_json_retry(prompt="chapter one body text", max_retries=3)
    raise AssertionError(f"unknown entry {entry!r}")  # pragma: no cover - 防止入口名拼写静默通过


AI_ENTRIES = [
    "generate_text",
    "generate_text_stream",
    "generate_text_stream_full",
    "call_with_json_retry",
]


@pytest.mark.anyio
@pytest.mark.parametrize("entry", AI_ENTRIES)
async def test_unconfigured_model_raises_and_no_request_goes_out(stub_provider_service, entry):
    """清空用户模型后调用任一 AI 入口：报 not_configured，且 mock provider 零调用。"""
    svc, provider = stub_provider_service(default_model=None)
    assert svc.default_model is None

    with pytest.raises(ApiError) as exc_info:
        await _invoke(svc, entry)

    assert exc_info.value.code == NOT_CONFIGURED
    assert provider.calls == [], f"{entry}: 有请求带着空模型打到了 provider"


@pytest.mark.parametrize("empty", [None, "", "   ", "\t"])
def test_empty_user_model_normalizes_to_none(empty):
    """None/空串/纯空白都算未配置：不能留一个空白字符串躲过判空。"""
    assert AIService(default_model=empty).default_model is None


@pytest.mark.anyio
@pytest.mark.parametrize("entry", AI_ENTRIES)
async def test_whitespace_only_user_model_also_blocks_request(stub_provider_service, entry):
    """空白串同样不得穿透到 provider（否则等于把 "   " 当模型名发出去）。"""
    svc, provider = stub_provider_service(default_model="   ")
    with pytest.raises(ApiError) as exc_info:
        await _invoke(svc, entry)
    assert exc_info.value.code == NOT_CONFIGURED
    assert provider.calls == []


@pytest.mark.anyio
async def test_explicit_model_still_works_without_user_default(stub_provider_service):
    """未被误伤：本次请求显式指定模型时，用户没配默认模型也必须照常发请求。"""
    svc, provider = stub_provider_service(default_model=None)
    result = await svc.generate_text(prompt="chapter one body text", model="needle-model")
    assert result["content"] == "stub"
    assert [c["model"] for c in provider.calls] == ["needle-model"]


@pytest.mark.anyio
async def test_user_configured_default_still_used_when_request_omits_model(stub_provider_service):
    """14 处同名语义的根：请求未指定模型时用**用户配置的**默认模型，不是系统常量。"""
    svc, provider = stub_provider_service(default_model="stub-model")
    await svc.generate_text(prompt="chapter one body text")
    assert [c["model"] for c in provider.calls] == ["stub-model"]


# ========== 系统常量回填已断开 ==========

def test_ai_service_no_longer_reads_system_default_model():
    """ai_service 不得再回填系统默认模型（那正是「系统代猜」）。"""
    source = (REPO_BACKEND / "app/services/ai_service.py").read_text(encoding="utf-8")
    assert "app_settings.default_model" not in source, "ai_service 仍在回填系统默认模型"
    assert AIService(default_model=None).default_model is None


def test_settings_api_no_longer_seeds_system_default_model():
    """api/settings.py 两处回填必须消失：否则全新安装被塞进 gpt-4，步骤 3 后首次保存即被硬拦。"""
    from app.api.settings import read_env_defaults

    source = (REPO_BACKEND / "app/api/settings.py").read_text(encoding="utf-8")
    assert "app_settings.default_model" not in source, "settings API 仍在用系统常量填充用户配置"
    defaults = read_env_defaults()
    assert not defaults["llm_model"], f"read_env_defaults 仍在猜模型: {defaults['llm_model']!r}"


@pytest.mark.anyio
async def test_fresh_settings_row_is_not_silently_given_a_model():
    """全新安装：建行未填 llm_model 时，ORM 列默认值也不得静默写入 gpt-4。"""
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    session_factory = async_sessionmaker(bind=engine, expire_on_commit=False)
    try:
        async with session_factory() as session:
            session.add(Settings(user_id="u-fresh", api_provider="openai", api_key="sk-stub"))
            await session.commit()
        async with session_factory() as session:
            row = (await session.execute(
                select(Settings).where(Settings.user_id == "u-fresh")
            )).scalar_one()
            assert not row.llm_model, f"未配置的新建行被静默塞入模型: {row.llm_model!r}"
    finally:
        await engine.dispose()


def test_no_backfill_of_system_constant_anywhere_but_config():
    """系统默认模型常量只允许留在 config.py 声明处（其值在步骤 4 删除）。"""
    offenders = []
    for path in sorted((REPO_BACKEND / "app").rglob("*.py")):
        if path.name == "config.py":
            continue
        text = path.read_text(encoding="utf-8")
        if re.search(r"\b\w*settings\.default_model\b", text):
            offenders.append(str(path.relative_to(REPO_BACKEND)))
    assert offenders == [], f"仍在读取系统默认模型常量: {offenders}"


# ========== 同名不同物：用户默认模型读取点必须原样保留 ==========

def test_user_default_model_call_sites_are_preserved():
    """`custom_model or ai_service.default_model` 不是兜底，删掉即功能回归。"""
    total = 0
    for rel, expected in USER_DEFAULT_MODEL_SITES.items():
        text = (REPO_BACKEND / rel).read_text(encoding="utf-8")
        found = sum(len(p.findall(text)) for p in _USER_DEFAULT_PATTERNS)
        assert found == expected, (
            f"{rel}: 用户配置的默认模型读取点应为 {expected} 处，实际 {found} 处"
            "——这些不是兜底，不得删除"
        )
        total += expected
    assert total == 19, "读取点总数与实测不符，说明有站点被增删"


# ========== i18n：错误码必须有 zh/en 文案 ==========

def test_new_codes_have_zh_and_en_locale_entries():
    zh = json.loads((FRONTEND_LOCALES / "zh/errors.json").read_text(encoding="utf-8"))
    en = json.loads((FRONTEND_LOCALES / "en/errors.json").read_text(encoding="utf-8"))
    for code in (NOT_CONFIGURED, BELOW_MINIMUM):
        leaf = code.split(".", 1)[1]
        assert zh["validation"].get(leaf), f"zh errors.json 缺 {code}"
        assert en["validation"].get(leaf), f"en errors.json 缺 {code}"
        # parity 工具要求 zh 值与 registry 默认 detail 逐字节一致
        assert zh["validation"][leaf] == ERROR_REGISTRY[code][0], f"{code} zh 与 registry 不一致"
