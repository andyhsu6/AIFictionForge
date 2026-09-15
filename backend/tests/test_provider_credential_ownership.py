"""#64：per-call provider 覆盖不得派发到运维/服务器全局的 API key 上。

`provider` 是请求体里的 per-call 覆盖（`api/polish.py`、`api/outlines.py`、
`api/wizard_stream.py`、`services/plot_expansion_service.py` 都原样透传，最终喂给
`AIService._get_provider`）。修复前，当它指向**用户没有配置的**那家 provider 时，
`AIService.__init__` 会用 `app_settings.<provider>_api_key`（服务器全局 env 凭据）
兜底建槽位 ⇒ 任意登录用户都能花运维的额度，把只应由该用户负责的内容发出去。

本文件钉住的事实：
- 负向（漏洞）：非 active provider 的 per-call 覆盖，`_get_provider` 与
  `_dispatch_endpoint` 两条路径都必须抛 `validation.provider_not_configured`；
  槽位不得用运维 key 建起来，一次请求都不许发出。
- 正向护栏（不得误伤）：
  (a) 用户自己那家 active provider 照常解析，优先用自己的 key；
  (b) 用户没有自己的 key 时，active provider 仍回落到该家自己的 env 默认 key
      （这是产品预期的「默认 key」行为，不是漏洞）；
  (c) gemini 既有行为不变（只有 active 时才有槽位，且从不拿非 active 的全局 key）。

测试值一律中性占位（user-key / operator-key / gw.test / big-model），不含任何真实
密钥或原文数据（AGENTS.md 脱敏硬约束）。
"""
import json
from pathlib import Path

import pytest

from app.core.errors import ERROR_REGISTRY, ApiError
from app.services.ai_service import AIService

PROVIDER_NOT_CONFIGURED = "validation.provider_not_configured"

REPO_BACKEND = Path(__file__).resolve().parents[1]
FRONTEND_LOCALES = REPO_BACKEND.parent / "frontend/src/locales"

OPERATOR_KEY = "operator-key"
USER_KEY = "user-key"
GATEWAY = "https://gw.test/v1"

# 两个方向都要拒：openai 用户覆盖到 anthropic，anthropic 用户覆盖到 openai。
REVERSE_DIRECTIONS = [("openai", "anthropic"), ("anthropic", "openai")]

GLOBAL_KEY_ATTRS = ("openai_api_key", "anthropic_api_key", "gemini_api_key")


@pytest.fixture
def env_keys(monkeypatch):
    """把三家 env key 都种成运维 key：证明「非 active provider 不再借用它」。"""
    for attr in GLOBAL_KEY_ATTRS:
        monkeypatch.setattr(
            "app.services.ai_service.app_settings." + attr, OPERATOR_KEY, raising=False
        )


def _service(api_provider: str, api_key: str = USER_KEY, base_url: str = GATEWAY) -> AIService:
    return AIService(
        api_provider=api_provider,
        api_key=api_key,
        api_base_url=base_url,
        default_model="big-model",
    )


# ========== 负向：漏洞本身 ==========


@pytest.mark.parametrize("user_provider,other", REVERSE_DIRECTIONS)
def test_override_to_provider_the_user_did_not_configure_is_refused_via_get_provider(
    env_keys, user_provider, other
):
    """`_get_provider(other)` 必须抛 typed refusal，绝不返回带运维 key 的槽位。"""
    svc = _service(user_provider)

    with pytest.raises(ApiError) as exc:
        svc._get_provider(other)

    assert exc.value.code == PROVIDER_NOT_CONFIGURED
    assert exc.value.params["provider"] == other

    # 槽位本身不许用运维 key 建起来（否则 return 的就是它）
    assert getattr(svc, f"_{other}_provider") is None, (
        f"非 active provider {other!r} 仍被全局 key 建了槽位"
    )
    assert svc._provider_endpoints[other][1] != OPERATOR_KEY


@pytest.mark.parametrize("user_provider,other", REVERSE_DIRECTIONS)
def test_override_to_provider_the_user_did_not_configure_is_refused_via_dispatch_endpoint(
    env_keys, user_provider, other
):
    """dispatch 三元组同样拒：否则门禁会先拿运维 key 去探测别家 host。"""
    svc = _service(user_provider)

    with pytest.raises(ApiError) as exc:
        svc._dispatch_endpoint(other)

    assert exc.value.code == PROVIDER_NOT_CONFIGURED
    assert exc.value.params["provider"] == other


@pytest.mark.parametrize("bad", ["  ", "mystery-provider"])
def test_blank_or_unknown_provider_override_is_refused_with_the_typed_error(env_keys, bad):
    """空白/未知 provider 同样是「不是用户配置的那家」，拒绝而非 ValueError/500。"""
    svc = _service("openai")

    with pytest.raises(ApiError) as exc:
        svc._get_provider(bad)
    assert exc.value.code == PROVIDER_NOT_CONFIGURED

    with pytest.raises(ApiError) as exc2:
        svc._dispatch_endpoint(bad)
    assert exc2.value.code == PROVIDER_NOT_CONFIGURED


@pytest.mark.anyio
@pytest.mark.parametrize("user_provider,other", REVERSE_DIRECTIONS)
async def test_override_is_refused_before_any_request_is_dispatched(env_keys, user_provider, other):
    """真实入口（generate_text）在发请求之前就拒：没有任何 provider 被调用。"""
    svc = _service(user_provider)
    calls = []

    class _Recording:
        async def generate(self, **kwargs):
            calls.append(kwargs)
            return {"content": "stub", "finish_reason": "stop", "tool_calls": []}

    # 即便有人手动塞了槽位，覆盖别家也必须被拒（不许「塞了就放行」）
    setattr(svc, f"_{other}_provider", _Recording())

    with pytest.raises(ApiError) as exc:
        await svc.generate_text(prompt="chapter one body text", provider=other)

    assert exc.value.code == PROVIDER_NOT_CONFIGURED
    assert calls == [], "被拒的跨 provider 覆盖仍然发出了请求"


# ========== 正向(a)：active provider 用自己的 key ==========


@pytest.mark.parametrize("active", ["openai", "anthropic", "gemini"])
def test_active_provider_resolves_with_the_users_own_key(env_keys, active):
    """用户配置的 provider + 自己的 key：照常解析，且派发键就是用户 key。"""
    svc = _service(active)

    assert svc._get_provider(active) is getattr(svc, f"_{active}_provider")
    assert svc._get_provider() is getattr(svc, f"_{active}_provider")
    assert svc._provider_endpoints[active][1] == USER_KEY
    assert svc._dispatch_endpoint(active)[2] == USER_KEY
    assert svc._dispatch_endpoint()[2] == USER_KEY


# ========== 正向(b)：缺自己的 key 时，active provider 仍用该家 env 默认 key ==========


def test_active_provider_without_user_key_still_uses_env_default(monkeypatch):
    """用户没配 key ⇒ active provider 回落到该家自己的 env 默认 key（预期行为）。"""
    monkeypatch.setattr(
        "app.services.ai_service.app_settings.openai_api_key", "env-default-key", raising=False
    )
    monkeypatch.setattr(
        "app.services.ai_service.app_settings.anthropic_api_key", OPERATOR_KEY, raising=False
    )

    svc = AIService(api_provider="openai", api_key=None, api_base_url=GATEWAY)

    assert svc._openai_provider is not None, "active provider 缺用户 key 时被误伤"
    assert svc._provider_endpoints["openai"][1] == "env-default-key"
    assert svc._dispatch_endpoint("openai")[2] == "env-default-key"


# ========== 正向(c)：gemini 既有行为不变 ==========


def test_gemini_slot_only_exists_when_active_and_never_borrows_global_key(env_keys):
    """gemini 只在 active 时有槽位；非 active 时不得借全局 key。"""
    active = _service("gemini")
    assert active._get_provider("gemini") is active._gemini_provider
    assert active._provider_endpoints["gemini"][1] == USER_KEY

    other = _service("openai")
    assert other._gemini_provider is None
    assert other._provider_endpoints["gemini"][1] is None
    with pytest.raises(ApiError) as exc:
        other._get_provider("gemini")
    assert exc.value.code == PROVIDER_NOT_CONFIGURED


# ========== 错误码注册 + i18n ==========


def test_refusal_code_registered_and_localized():
    assert PROVIDER_NOT_CONFIGURED in ERROR_REGISTRY
    detail, status = ERROR_REGISTRY[PROVIDER_NOT_CONFIGURED]
    assert detail and status == 400, "用户可自助修正的配置问题应为 400"

    zh = json.loads((FRONTEND_LOCALES / "zh/errors.json").read_text(encoding="utf-8"))
    en = json.loads((FRONTEND_LOCALES / "en/errors.json").read_text(encoding="utf-8"))
    leaf = PROVIDER_NOT_CONFIGURED.split(".", 1)[1]
    assert zh["validation"].get(leaf), "zh errors.json 缺该码"
    assert en["validation"].get(leaf), "en errors.json 缺该码"
    assert zh["validation"][leaf] == detail, "zh 值与 registry 默认 detail 必须逐字节一致"
