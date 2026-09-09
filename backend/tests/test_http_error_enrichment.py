"""HTTPStatusError 上游 body 增强测试（todo 1）。

修复背景：大纲生成遇上游模型网关 400 时，异常消息只有
"Client error '400 Bad Request' for url ..."，上游返回的真实原因被吞掉。
base_client._enrich_http_status_error 把响应 body（error.message 或原文预览）
附加到 "上游响应: " 标记之后；call_with_json_retry 的 response_format 降级
判定只在标记之前匹配，避免上游 body 恰含 "response_format" 字样时误降级。
"""
import datetime
import json

import httpx
import pytest

from app.services.ai_clients.base_client import (
    UPSTREAM_BODY_MARKER,
    BaseAIClient,
    _enrich_http_status_error,
    _http_client_pool,
)
from app.services.ai_config import AIClientConfig, RateLimitConfig, RetryConfig
from app.services.ai_service import AIService

SHORT_URL = "https://u.test/v1/c"


def _make_status_error(body, status_code=400, as_json=False):
    """构造带 request/response 的 HTTPStatusError（非流式，body 已读）。"""
    request = httpx.Request("POST", SHORT_URL)
    if as_json:
        response = httpx.Response(status_code, json=body, request=request)
        text = response.text
    else:
        response = httpx.Response(status_code, text=body, request=request)
        text = body
    error = httpx.HTTPStatusError(
        f"Client error '{status_code} Bad Request' for url '{SHORT_URL}'",
        request=request,
        response=response,
    )
    return error, response, text


# (a) JSON body → error.message 附加在标记后，异常属性完整保留
@pytest.mark.anyio
async def test_json_error_message_appended_after_marker():
    payload = {
        "error": {
            "message": "model 'qwen-x' does not support json_object",
            "type": "invalid_request_error",
        }
    }
    error, response, _ = _make_status_error(payload, as_json=True)

    enriched = await _enrich_http_status_error(error)
    msg = str(enriched)

    assert isinstance(enriched, httpx.HTTPStatusError)
    assert enriched is not error
    assert UPSTREAM_BODY_MARKER in msg
    assert msg.split(UPSTREAM_BODY_MARKER, 1)[1] == payload["error"]["message"]
    assert enriched.response is response
    assert enriched.request is error.request
    assert enriched.response.status_code == 400


# (b) 非 JSON body → 标记后为 ≤200 字符原文预览
@pytest.mark.anyio
async def test_non_json_body_preview_capped_at_200():
    body = "<html><body>" + "e" * 300 + "</body></html>"
    error, _, _ = _make_status_error(body)

    enriched = await _enrich_http_status_error(error)
    msg = str(enriched)

    assert UPSTREAM_BODY_MARKER in msg
    preview = msg.split(UPSTREAM_BODY_MARKER, 1)[1]
    assert preview == body[:200]
    assert len(preview) <= 200


# (c) 超长 body → 总消息 ≤450 字符（background_tasks.status_message 列宽约束）
@pytest.mark.anyio
async def test_long_body_total_message_within_450():
    payload = {"error": {"message": "x" * 5000}}
    error, _, _ = _make_status_error(payload, as_json=True)

    enriched = await _enrich_http_status_error(error)
    msg = str(enriched)

    assert len(msg) <= 450
    assert UPSTREAM_BODY_MARKER in msg


# (d) 流式响应体未读 → aread 防护，无 ResponseNotRead 逃逸
class _UnreadStream(httpx.AsyncByteStream):
    def __init__(self, chunks):
        self._chunks = chunks

    async def __aiter__(self):
        for chunk in self._chunks:
            yield chunk


@pytest.mark.anyio
async def test_streamed_unread_body_aread_guard():
    body = json.dumps({"error": {"message": "late streamed detail"}}).encode()
    request = httpx.Request("POST", SHORT_URL)
    response = httpx.Response(400, stream=_UnreadStream([body]), request=request)
    with pytest.raises(httpx.ResponseNotRead):
        response.text

    error = httpx.HTTPStatusError(
        f"Client error '400 Bad Request' for url '{SHORT_URL}'",
        request=request,
        response=response,
    )
    enriched = await _enrich_http_status_error(error)

    assert "late streamed detail" in str(enriched)
    assert enriched.response is response


# _request_with_retry 集成：两处最终 raise 均走增强助手
class _StubClient(BaseAIClient):
    def _build_headers(self):
        return {}

    async def chat_completion(self, messages, model, temperature, max_tokens, tools=None, tool_choice=None):
        raise NotImplementedError

    async def chat_completion_stream(self, messages, model, temperature, max_tokens):
        yield ""
        raise NotImplementedError


def _fast_config():
    return AIClientConfig(
        retry=RetryConfig(max_retries=2, base_delay=0.0),
        rate_limit=RateLimitConfig(request_delay=0.0),
    )


async def _stub_client(handler):
    client = _StubClient(api_key="enrich-test-key", base_url="https://u.test", config=_fast_config())
    pooled = client.http_client
    client.http_client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return client, pooled


# 非重试码分支（raise site 1）：403 首次即增强抛出、不重试
@pytest.mark.anyio
async def test_request_with_retry_non_retryable_raises_enriched():
    calls = []

    def handler(request):
        calls.append(request)
        response = httpx.Response(403, json={"error": {"message": "quota exceeded for model"}})
        response.elapsed = datetime.timedelta(0)
        return response

    client, pooled = await _stub_client(handler)
    try:
        with pytest.raises(httpx.HTTPStatusError) as exc_info:
            await client._request_with_retry("POST", "/v1/c", {"model": "m"})
    finally:
        await client.http_client.aclose()
        await pooled.aclose()
        _http_client_pool.pop(client._get_client_key(), None)

    msg = str(exc_info.value)
    assert UPSTREAM_BODY_MARKER in msg
    assert "quota exceeded for model" in msg
    assert exc_info.value.response.status_code == 403
    assert len(calls) == 1


# 末轮分支（raise site 2）：400 重试耗尽后增强抛出
@pytest.mark.anyio
async def test_request_with_retry_final_round_raises_enriched():
    calls = []

    def handler(request):
        calls.append(request)
        response = httpx.Response(400, json={"error": {"message": "unsupported parameter in body"}})
        response.elapsed = datetime.timedelta(0)
        return response

    client, pooled = await _stub_client(handler)
    try:
        with pytest.raises(httpx.HTTPStatusError) as exc_info:
            await client._request_with_retry("POST", "/v1/c", {"model": "m"})
    finally:
        await client.http_client.aclose()
        await pooled.aclose()
        _http_client_pool.pop(client._get_client_key(), None)

    msg = str(exc_info.value)
    assert UPSTREAM_BODY_MARKER in msg
    assert "unsupported parameter in body" in msg
    assert exc_info.value.response.status_code == 400
    assert len(calls) == 2


# (e) 降级回归：上游 400 body 含 "response_format"（标记之后）→ 不触发降级，原样 raise
class _RaisingProvider:
    def __init__(self, errors):
        self.errors = errors
        self.calls = []

    async def generate(self, **kwargs):
        raise AssertionError("不应走非流式路径")

    def generate_stream(self, **kwargs):
        self.calls.append(kwargs)
        index = len(self.calls) - 1
        error = self.errors[index] if index < len(self.errors) else None

        async def _gen():
            if error is not None:
                raise error
            yield '{"ok": true}'

        return _gen()


def _make_service(provider):
    svc = AIService(default_model="gpt-4o", default_max_tokens=32000)
    svc._openai_provider = provider
    svc.api_provider = "openai"
    return svc


@pytest.mark.anyio
async def test_no_false_degrade_when_upstream_body_mentions_response_format():
    payload = {"error": {"message": "Unsupported parameter: 'response_format' is not supported by this model"}}
    error, _, _ = _make_status_error(payload, as_json=True)
    enriched = await _enrich_http_status_error(error)
    assert "response_format" in str(enriched)
    assert "response_format" not in str(enriched).split(UPSTREAM_BODY_MARKER, 1)[0]

    provider = _RaisingProvider([enriched])
    svc = _make_service(provider)

    with pytest.raises(httpx.HTTPStatusError) as exc_info:
        await svc.call_with_json_retry(prompt="测试提示词", expected_type="object")

    assert exc_info.value is enriched
    assert provider.calls[0]["response_format"] == {"type": "json_object"}
    assert len(provider.calls) == 1


# 对照组：原始异常文本（标记之前/无标记）含 "response_format" 的真实不支持错误仍正常降级重试
@pytest.mark.anyio
async def test_genuine_response_format_error_still_degrades():
    request = httpx.Request("POST", SHORT_URL)
    response = httpx.Response(400, text="bad parameter", request=request)
    genuine = httpx.HTTPStatusError(
        f"Client error '400 Bad Request' for url '{SHORT_URL}'\n"
        "Error code: 400 - Unsupported parameter: response_format",
        request=request,
        response=response,
    )

    provider = _RaisingProvider([genuine])
    svc = _make_service(provider)

    result = await svc.call_with_json_retry(prompt="测试提示词", expected_type="object")

    assert result == {"ok": True}
    assert len(provider.calls) == 2
    assert provider.calls[0]["response_format"] == {"type": "json_object"}
    assert provider.calls[1]["response_format"] is None
