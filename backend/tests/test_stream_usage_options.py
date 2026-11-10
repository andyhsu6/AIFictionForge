"""流式 usage（含 reasoning_tokens）采集测试。

背景：2026-11-09 复测发现流式请求从未带 `stream_options.include_usage`，
该网关的流式响应因此**完全不含 usage**（日志长期 `输出Token=未知`），
`reasoning_tokens` 永远拿不到 → 自适应预算阶梯退化成盲抬。

覆盖：
1. 流式 payload 带 `stream_options={"include_usage": True}`；
2. 网关 400 抱怨该字段时，去掉字段重试一次并记住该 base_url；
3. `choices: []` 的 usage-only chunk 不会被丢掉；
4. 只有 `completion_tokens`、没有 details 时估算推理用量并标记为估算。
"""
import json

import httpx
import pytest

from app.services.ai_clients import openai_client as openai_client_module
from app.services.ai_clients.openai_client import OpenAIClient
from app.services.ai_config import AIClientConfig, RateLimitConfig, RetryConfig
from app.services.ai_metrics import TokenUsage
from app.services.ai_service import AIService


class _UnreadStream(httpx.AsyncByteStream):
    def __init__(self, chunks):
        self._chunks = chunks

    async def __aiter__(self):
        for chunk in self._chunks:
            yield chunk


def _sse(events) -> bytes:
    return "".join(
        f"data: {json.dumps(event, ensure_ascii=False)}\n\n" for event in events
    ).encode()


def _fast_config() -> AIClientConfig:
    return AIClientConfig(
        retry=RetryConfig(max_retries=2, base_delay=0.0),
        rate_limit=RateLimitConfig(request_delay=0.0),
    )


def _make_client(handler, base_url: str):
    client = OpenAIClient(api_key="stream-usage-key", base_url=base_url, config=_fast_config())
    pooled = client.http_client
    client.http_client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return client, pooled


async def _close(client, pooled) -> None:
    active = client.http_client
    await active.aclose()
    await pooled.aclose()
    tool_pool = getattr(client, "_get_client_key", None)
    if callable(tool_pool):
        from app.services.ai_clients import base_client

        base_client._http_client_pool.pop(client._get_client_key(), None)


async def _drain(client) -> list:
    return [
        chunk
        async for chunk in client.chat_completion_stream(
            [{"role": "user", "content": "hi"}], "test-model", 0.7, 100
        )
    ]


def _ok_stream(events):
    return httpx.Response(
        200,
        stream=_UnreadStream([_sse(events) + b"data: [DONE]\n\n"]),
        headers={"content-type": "text/event-stream"},
    )


@pytest.mark.anyio
async def test_stream_payload_requests_usage(monkeypatch):
    """流式请求必须显式开启 include_usage，否则网关不回 usage。"""
    monkeypatch.setattr(openai_client_module, "_STREAM_USAGE_UNSUPPORTED_BASE_URLS", set())
    bodies = []

    def handler(request):
        bodies.append(json.loads(request.content))
        return _ok_stream([{"choices": [{"index": 0, "delta": {"content": "Hi"}}]}])

    client, pooled = _make_client(handler, "https://u-stream-payload.test/v1")
    try:
        await _drain(client)
    finally:
        await _close(client, pooled)

    assert bodies[0]["stream"] is True
    assert bodies[0]["stream_options"] == {"include_usage": True}


@pytest.mark.anyio
async def test_gateway_rejecting_stream_options_is_retried_without_it(monkeypatch):
    """400 抱怨 stream_options/include_usage → 去掉字段重试成功，并缓存到该 base_url。"""
    monkeypatch.setattr(openai_client_module, "_STREAM_USAGE_UNSUPPORTED_BASE_URLS", set())
    bodies = []

    def handler(request):
        bodies.append(json.loads(request.content))
        if len(bodies) == 1:
            return httpx.Response(
                400,
                stream=_UnreadStream(
                    [json.dumps({"error": {"message": "stream_options is not supported"}}).encode()]
                ),
            )
        return _ok_stream(
            [{"choices": [{"index": 0, "delta": {"content": "Hi"}, "finish_reason": "stop"}]}]
        )

    client, pooled = _make_client(handler, "https://u-stream-fallback.test/v1")
    try:
        chunks = await _drain(client)
        # 同一 base_url 的后续请求直接不再带该字段（不再先撞 400）
        await _drain(client)
    finally:
        await _close(client, pooled)

    assert bodies[0]["stream_options"] == {"include_usage": True}
    assert "stream_options" not in bodies[1]
    assert {"content": "Hi"} in chunks
    assert len(bodies) == 3, f"降级后不该继续先撞 400：实际请求 {len(bodies)} 次"
    assert "stream_options" not in bodies[2]


@pytest.mark.anyio
async def test_usage_only_chunk_without_choices_is_collected(monkeypatch):
    """开启 include_usage 后，携带 usage 的 chunk 是 choices: []，不能被丢掉。"""
    monkeypatch.setattr(openai_client_module, "_STREAM_USAGE_UNSUPPORTED_BASE_URLS", set())

    def handler(request):
        return _ok_stream(
            [
                {"choices": [{"index": 0, "delta": {"content": "字"}}]},
                {
                    "choices": [],
                    "usage": {
                        "prompt_tokens": 13,
                        "total_tokens": 338,
                        "completion_tokens": 325,
                        "completion_tokens_details": {"reasoning_tokens": 311},
                    },
                },
            ]
        )

    client, pooled = _make_client(handler, "https://u-stream-usageonly.test/v1")
    try:
        chunks = await _drain(client)
    finally:
        await _close(client, pooled)

    usage_chunks = [chunk for chunk in chunks if "usage" in chunk]
    assert len(usage_chunks) == 1
    parsed = TokenUsage.from_response({"usage": usage_chunks[0]["usage"]})
    assert parsed.reasoning_tokens == 311
    assert parsed.completion_tokens == 325


class _UsageWithoutDetailsProvider:
    """只回 completion_tokens、不回 completion_tokens_details 的假 provider。"""

    async def generate(self, **kwargs):
        raise AssertionError("不应走非流式路径")

    def generate_stream(self, **kwargs):
        async def _gen():
            yield {
                "usage": {
                    "prompt_tokens": 10,
                    "completion_tokens": 325,
                    "total_tokens": 335,
                }
            }
            yield "正文五个字"
            yield {"done": True, "finish_reason": "length"}

        return _gen()


@pytest.mark.anyio
async def test_reasoning_tokens_estimated_when_details_missing():
    """details 缺失时用 completion_tokens - 正文字符估算，并标记 reasoning_estimated。"""
    service = AIService(default_model="glm-5.3-flash", default_max_tokens=16000)
    service.api_provider = "openai"
    service._openai_provider = _UsageWithoutDetailsProvider()

    finish_sink = {}
    text = ""
    async for chunk in service.generate_text_stream(
        prompt="hi", max_tokens=2000, auto_mcp=False, finish_sink=finish_sink
    ):
        text += chunk

    usage = finish_sink["usage"]
    assert usage.reasoning_tokens == 325 - len(text)
    assert usage.reasoning_tokens > 0
    assert usage.reasoning_estimated is True
