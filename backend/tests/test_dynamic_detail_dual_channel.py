"""task 14（todo14）：dynamic_detail 双通道契约回归测试。

- 封面上游失败（原 dynamic HTTP 站点）：envelope 为
  {"detail": <原文>, "code": "dynamic_detail", "params": {}, "raw": <原文>}，
  HTTP status 保留原上游状态码；detail 与旧文案 byte-identity。
- security.url_blocked_with_hint（registry 参数化码接线）：detail 保持
  "拼接原文" byte-identity，reason 参数可结构化翻译。
- polish.py 守卫：ApiError 不再被 except Exception 字符串化为 "[code] detail"。
"""
import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest

from app.config import settings as app_settings
from app.core.errors import ApiError, DYNAMIC_DETAIL_CODE


# ---------------------------------------------------------------------------
# (a) 封面上游失败 → dynamic_detail 双通道 envelope（status 保留上游码）
# ---------------------------------------------------------------------------

def _make_upstream_status_error(status_code: int, payload: dict) -> httpx.HTTPStatusError:
    request = httpx.Request("POST", "https://upstream.example.invalid/v1/images")
    response = httpx.Response(status_code=status_code, json=payload, request=request)
    return httpx.HTTPStatusError(
        f"Client error '{status_code}'", request=request, response=response
    )


def test_cover_upstream_failure_returns_dynamic_detail_envelope(monkeypatch):
    from app.services.cover_generation_service import cover_generation_service
    from app.services.prompt_service import PromptService

    project = SimpleNamespace(
        id="p1",
        cover_status="failed",  # 非 generating/ready，避免提前命中 conflict 守卫
        cover_error=None,
        cover_prompt="prompt",
        cover_image_url=None,
        cover_updated_at=None,
    )

    async def _fake_get_project(*, db, user_id, project_id):
        return project

    async def _fake_get_settings(*, db, user_id):
        return SimpleNamespace(cover_image_provider="gemini", cover_api_key="k", cover_image_model="m")

    def _fake_validate(self, settings):
        return None

    def _fake_build_provider(self, settings):
        class _Provider:
            async def generate_cover(self, **kwargs):
                raise _make_upstream_status_error(429, {"error": "quota exceeded"})

        return _Provider()

    async def _fake_prompt(*args, **kwargs):
        return "cover prompt"

    monkeypatch.setattr(cover_generation_service, "_get_project", _fake_get_project)
    monkeypatch.setattr(cover_generation_service, "_get_settings", _fake_get_settings)
    monkeypatch.setattr(type(cover_generation_service), "_validate_cover_settings", _fake_validate)
    monkeypatch.setattr(type(cover_generation_service), "_build_provider", _fake_build_provider)
    monkeypatch.setattr(PromptService, "build_novel_cover_prompt", _fake_prompt)

    with pytest.raises(ApiError) as exc_info:
        asyncio.run(
            cover_generation_service.generate_cover(
                db=AsyncMock(), user_id="u1", project_id="p1", overwrite=True
            )
        )

    err = exc_info.value
    assert err.code == DYNAMIC_DETAIL_CODE
    assert err.status == 429  # 原 status_code=exc.response.status_code 保留
    assert err.detail == "quota exceeded"  # _extract_upstream_error_detail 原文
    assert err.raw == "quota exceeded"  # 双通道：raw 与 detail 同原文
    # envelope 精确形状：detail 保旧契约，code/raw 为新增结构化字段
    assert err.to_envelope() == {
        "detail": "quota exceeded",
        "code": "dynamic_detail",
        "params": {},
        "raw": "quota exceeded",
    }
    # 失败态落库字段同步更新
    assert project.cover_status == "failed"
    assert project.cover_error == "quota exceeded"


# ---------------------------------------------------------------------------
# (b) security.url_blocked_with_hint 接线（registry 参数化码，detail byte-identity）
# ---------------------------------------------------------------------------

def test_url_blocked_with_hint_wired(monkeypatch):
    from app.security import validate_ai_http_url

    monkeypatch.setattr(app_settings, "allow_private_ai_endpoints", False, raising=False)
    monkeypatch.setattr(app_settings, "allowed_ai_hosts", "", raising=False)

    with pytest.raises(ApiError) as exc_info:
        validate_ai_http_url("http://localhost:11434/v1")

    err = exc_info.value
    reason = "URL不允许指向本机地址"  # security.url_loopback 的 registry 默认文案
    legacy_detail = (
        f"{reason}。如需连接本地或 Docker 内网 LLM，"
        "请设置 ALLOW_PRIVATE_AI_ENDPOINTS=true，"
        "或把主机名加入 ALLOWED_AI_HOSTS（例如 host.docker.internal,127.0.0.1）。"
    )
    assert err.code == "security.url_blocked_with_hint"
    # detail 与旧 f-string 拼接结果 byte-identity（旧客户端无感）
    assert err.detail == legacy_detail
    assert err.status == 400
    assert err.params == {"reason": reason}
    # 参数化注册码走 code/params 通道，无需 raw
    assert err.to_envelope() == {
        "detail": legacy_detail,
        "code": "security.url_blocked_with_hint",
        "params": {"reason": reason},
    }


def test_url_blocked_non_hint_keeps_registry_code(monkeypatch):
    """不命中提示语 token 的 400（如 URL不能为空）不走 hint 站点。"""
    from app.security import validate_ai_http_url

    monkeypatch.setattr(app_settings, "allow_private_ai_endpoints", False, raising=False)
    monkeypatch.setattr(app_settings, "allowed_ai_hosts", "", raising=False)

    with pytest.raises(ApiError) as exc_info:
        validate_ai_http_url("")
    assert exc_info.value.code == "security.url_empty"


# ---------------------------------------------------------------------------
# (c) polish.py 守卫：ApiError 原样穿透，不被 except Exception 字符串化
# ---------------------------------------------------------------------------

def test_polish_guard_propagates_api_error_unwrapped(monkeypatch):
    from app.api.polish import polish_text
    from app.schemas.polish import PolishRequest
    from app.services.prompt_service import PromptService

    async def _raise_template_error(*args, **kwargs):
        raise ApiError(code="not_found.prompt_template")

    monkeypatch.setattr(PromptService, "get_template", _raise_template_error)

    class _State:
        user_id = "u1"

    class _FakeRequest:
        state = _State()

    with pytest.raises(ApiError) as exc_info:
        asyncio.run(
            polish_text(
                request=PolishRequest(original_text="一段AI生成的文本"),
                http_request=_FakeRequest(),
                db=AsyncMock(),
                user_ai_service=None,
            )
        )

    err = exc_info.value
    assert err.code == "not_found.prompt_template"
    # 不再被包成 500 "AI去味失败: [not_found.prompt_template] ..." 字符串
    # （注意：str(err) 本身含 "[code] detail" 前缀，这里断言的是 detail 字段干净）
    assert "AI去味失败" not in str(err)
    assert err.detail == "模板 {{template_key}} 不存在"
    assert "[not_found." not in err.detail
