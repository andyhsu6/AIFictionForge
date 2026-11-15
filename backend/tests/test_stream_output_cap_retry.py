"""流式请求撞上「输出上限 400」时的原地重试测试（issue #147）。

背景：预算按窗口给足后，第一次撞上网关更低的输出上限不该让整次生成白失败。
`chat_completion_stream` 曾在 400 时只 `learn_output_ceiling` 后 `raise`
（注释称「流已打开，无法原地重发」）；现在改成与 `stream_options` 降级同一形态的
递归重试：学到更低的上限 → 用 `max_tokens=learned` 重新发起并转发 chunk。
"""
import json

import httpx
import pytest

from app.services.ai_clients import openai_client as openai_client_module
from app.services.ai_clients.base_client import _http_client_pool
from app.services.ai_clients.openai_client import OpenAIClient
from app.services.ai_clients.output_caps import (
    clear_output_ceiling_memo,
    resolve_output_ceiling,
)
from app.services.ai_config import AIClientConfig, RateLimitConfig, RetryConfig

BASE = "https://cap-retry.test/v1"
MODEL = "meituan/LongCat-2.0:free"
CEILING = 400_000
REQUESTED = 865_460


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


def _rejection_body(echoed: int, ceiling: int) -> str:
    return f"参数校验失败: \n/max_tokens: {echoed} is not less or equal to {ceiling}\n"


@pytest.fixture(autouse=True)
def _clean_memo(monkeypatch):
    monkeypatch.setattr(openai_client_module, "_STREAM_USAGE_UNSUPPORTED_BASE_URLS", set())
    clear_output_ceiling_memo()
    yield
    clear_output_ceiling_memo()


@pytest.mark.anyio
async def test_stream_output_cap_400_is_retried_in_place_with_learned_ceiling():
    bodies = []

    def handler(request):
        bodies.append(json.loads(request.content.decode()))
        if len(bodies) == 1:
            return httpx.Response(400, text=_rejection_body(REQUESTED, CEILING))
        return httpx.Response(
            200,
            stream=_UnreadStream(
                [
                    _sse(
                        [
                            {
                                "choices": [
                                    {
                                        "index": 0,
                                        "delta": {"content": "正文"},
                                        "finish_reason": "stop",
                                    }
                                ]
                            }
                        ]
                    )
                    + b"data: [DONE]\n\n"
                ]
            ),
            headers={"content-type": "text/event-stream"},
        )

    client = OpenAIClient(api_key="k", base_url=BASE, config=_fast_config())
    pooled = client.http_client
    client.http_client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    try:
        chunks = [
            chunk
            async for chunk in client.chat_completion_stream(
                messages=[{"role": "user", "content": "hi"}],
                model=MODEL,
                temperature=0.7,
                max_tokens=REQUESTED,
            )
        ]
    finally:
        await client.http_client.aclose()
        await pooled.aclose()
        _http_client_pool.pop(client._get_client_key(), None)

    assert [body["max_tokens"] for body in bodies] == [REQUESTED, CEILING]
    assert {"content": "正文"} in chunks
    assert any(chunk.get("done") for chunk in chunks)
    # 学到的上限被记住，后续请求不再先撞 400
    assert resolve_output_ceiling(BASE, MODEL) == CEILING


async def _collect_with_handler(
    handler, *, max_tokens: int = REQUESTED, **stream_kwargs
) -> list:
    """用 MockTransport 跑一次流式请求；异常原样抛给调用方断言。"""
    client = OpenAIClient(api_key="k", base_url=BASE, config=_fast_config())
    pooled = client.http_client
    client.http_client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    try:
        return [
            chunk
            async for chunk in client.chat_completion_stream(
                messages=[{"role": "user", "content": "hi"}],
                model=MODEL,
                temperature=0.7,
                max_tokens=max_tokens,
                **stream_kwargs,
            )
        ]
    finally:
        await client.http_client.aclose()
        await pooled.aclose()
        _http_client_pool.pop(client._get_client_key(), None)


@pytest.mark.anyio
async def test_stream_output_cap_400_not_retried_again_when_second_attempt_hits_the_same_cap():
    """第二次仍 400 且报同一上限 ⇒ 递归必须有界：直接抛出，只发 2 次请求。

    界在 `parse_reported_output_ceiling`：只接受严格低于本次请求值的数字，所以第二次
    报同值时「学不到」⇒ 不重试。调用点那次 `learned < payload["max_tokens"]` 是第二道
    冗余防护（单独去掉它行为不变，已实测），本用例钉的是**可观测结果**：请求次数恰好
    2 次且抛出，任一处退化成"无条件重试"都会当场变红。
    """
    bodies = []

    def handler(request):
        sent = json.loads(request.content.decode())["max_tokens"]
        bodies.append(sent)
        return httpx.Response(400, text=_rejection_body(sent, CEILING))

    with pytest.raises(httpx.HTTPStatusError):
        await _collect_with_handler(handler)

    assert bodies == [REQUESTED, CEILING]
    assert len(bodies) == 2
    # 第一次学到的上限仍然保留（学到的值本身是对的，只是不能再降）
    assert resolve_output_ceiling(BASE, MODEL) == CEILING


@pytest.mark.anyio
async def test_stream_output_cap_400_without_a_cap_number_is_not_memoized():
    """400 的 body 与输出上限无关 ⇒ 不写 memo、不重试，原样抛出。"""
    bodies = []

    def handler(request):
        bodies.append(json.loads(request.content.decode())["max_tokens"])
        return httpx.Response(400, json={"error": {"message": "bad request"}})

    with pytest.raises(httpx.HTTPStatusError):
        await _collect_with_handler(handler)

    assert bodies == [REQUESTED]
    assert resolve_output_ceiling(BASE, MODEL) is None


@pytest.mark.anyio
async def test_stream_max_tokens_rejection_without_a_number_falls_back_once():
    """网关拒绝 max_tokens 但不报上限（实测 gpt-6-luna）：退到保守预算重试一次。"""
    bodies = []

    def handler(request):
        bodies.append(json.loads(request.content.decode()))
        if bodies[-1]["max_tokens"] > 64_000:
            return httpx.Response(
                400,
                text="{\"error\":{\"message\":\"The value of 'max_tokens' is above this model's limit. Lower it and retry.\"}}",
            )
        return httpx.Response(
            200,
            stream=_UnreadStream(
                [
                    _sse([{"choices": [{"index": 0, "delta": {"content": "正文"}, "finish_reason": "stop"}]}])
                    + b"data: [DONE]\n\n"
                ]
            ),
            headers={"content-type": "text/event-stream"},
        )

    chunks = await _collect_with_handler(handler)
    assert [body["max_tokens"] for body in bodies] == [REQUESTED, 64_000]
    assert {"content": "正文"} in chunks
    # 报错没给数字，但 64,000 已被证明可用：记下来，下次不再重复吃一次 400
    assert resolve_output_ceiling(BASE, MODEL) == 64_000

    # 第二次请求直接从 64,000 起，不再有注定失败的往返
    bodies.clear()
    await _collect_with_handler(handler)
    assert [body["max_tokens"] for body in bodies] == [64_000]


@pytest.mark.anyio
async def test_stream_max_tokens_rejection_fallback_is_bounded():
    """退到 64000 后仍被拒：不再递归，直接抛错。"""
    bodies = []

    def handler(request):
        bodies.append(json.loads(request.content.decode()))
        return httpx.Response(
            400,
            text="{\"error\":{\"message\":\"The value of 'max_tokens' is above this model's limit.\"}}",
        )

    with pytest.raises(httpx.HTTPStatusError):
        await _collect_with_handler(handler)
    assert [body["max_tokens"] for body in bodies] == [REQUESTED, 64_000]


@pytest.mark.anyio
async def test_reported_ceiling_ending_with_a_period_is_learned_not_fallen_back():
    """网关最常见的措辞是 "…limit of 131072. Lower it and retry."。

    句号收尾的形态若解析不出数字，常量的 64,000 会抢先触发，把网关明说的
   上限压掉一半多 —— 这正是本次修掉的 MAJOR。
    """
    bodies = []

    def handler(request):
        body = json.loads(request.content.decode())
        bodies.append(body["max_tokens"])
        if body["max_tokens"] > 131_072:
            return httpx.Response(
                400,
                text="{\"error\":{\"message\":\"The value of 'max_tokens' is above this model's limit of 131072. Lower it and retry.\"}}",
            )
        return httpx.Response(
            200,
            stream=_UnreadStream(
                [
                    _sse([{"choices": [{"index": 0, "delta": {"content": "正文"}, "finish_reason": "stop"}]}])
                    + b"data: [DONE]\n\n"
                ]
            ),
            headers={"content-type": "text/event-stream"},
        )

    await _collect_with_handler(handler)
    assert bodies == [REQUESTED, 131_072]
    assert resolve_output_ceiling(BASE, MODEL) == 131_072


@pytest.mark.anyio
async def test_fallback_retry_keeps_request_parameters():
    """常量兜底的重试不能把 reasoning_effort / response_format 丢掉。

    丢了 reasoning_effort 会静默退回网关默认思考档，单章又变 5–8 分钟；
    丢了 response_format 会退化成自由文本。
    """
    bodies = []

    def handler(request):
        body = json.loads(request.content.decode())
        bodies.append(body)
        if body["max_tokens"] > 64_000:
            return httpx.Response(
                400,
                text="{\"error\":{\"message\":\"The value of 'max_tokens' is above this model's limit. Lower it and retry.\"}}",
            )
        return httpx.Response(
            200,
            stream=_UnreadStream(
                [
                    _sse([{"choices": [{"index": 0, "delta": {"content": "正文"}, "finish_reason": "stop"}]}])
                    + b"data: [DONE]\n\n"
                ]
            ),
            headers={"content-type": "text/event-stream"},
        )

    await _collect_with_handler(
        handler,
        reasoning_effort="low",
        response_format={"type": "json_object"},
    )

    assert len(bodies) == 2
    retry = bodies[1]
    assert retry["max_tokens"] == 64_000
    assert retry["reasoning_effort"] == "low"
    assert retry["response_format"] == {"type": "json_object"}
