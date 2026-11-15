"""思考强度档位（reasoning_effort）与字数下限的契约测试。

实测背景（issue #295 排查，2026-11-14，第 67 章 / glm-5.3-flash）：
- 同一提示词、同一章，不传档位时 completion 18,888–36,114 token、首字 252–477s；
- `reasoning_effort=low`：推理 100–303 token、整章 43–53s；
- `high`：推理 9,747、209s；`medium`：49–661。
即思考档位是章节生成延迟的主控变量，必须真的进到请求体、且被判据守住。

同时守两件容易退化的行为：
1. 档位不传时必须**不注入**该字段（否则等于把所有网关都当支持方）；
2. 重试递归（去 stream_options / 学到的输出上限）不能把档位丢掉。
"""
import json

import httpx
import pytest

from app.schemas.chapter import BatchGenerateRequest, ChapterGenerateRequest
from app.services.ai_clients.openai_client import OpenAIClient
from app.services.ai_config import AIClientConfig, RateLimitConfig, RetryConfig


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
        retry=RetryConfig(max_retries=1, base_delay=0.0),
        rate_limit=RateLimitConfig(request_delay=0.0),
    )


def _make_client(handler, base_url: str):
    client = OpenAIClient(api_key="reasoning-key", base_url=base_url, config=_fast_config())
    pooled = client.http_client
    client.http_client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return client, pooled


async def _close(client, pooled) -> None:
    from app.services.ai_clients import base_client

    await client.http_client.aclose()
    await pooled.aclose()
    base_client._http_client_pool.pop(client._get_client_key(), None)


def _ok_stream() -> httpx.Response:
    return httpx.Response(
        200,
        stream=_UnreadStream([_sse([{"choices": [{"delta": {"content": "ok"}}]}]) + b"data: [DONE]\n\n"]),
        headers={"content-type": "text/event-stream"},
    )


# ===== 请求模型 =====


def test_generate_request_defaults_to_medium():
    request = ChapterGenerateRequest(content_language="zh")
    assert request.reasoning_effort == "medium"


def test_generate_request_accepts_explicit_none_to_keep_gateway_default():
    request = ChapterGenerateRequest(content_language="zh", reasoning_effort=None)
    assert request.reasoning_effort is None


@pytest.mark.parametrize("level", ["low", "medium", "high"])
def test_generate_request_accepts_known_levels(level):
    assert ChapterGenerateRequest(
        content_language="zh", reasoning_effort=level
    ).reasoning_effort == level


def test_generate_request_rejects_unknown_level():
    with pytest.raises(Exception):
        ChapterGenerateRequest(content_language="zh", reasoning_effort="ultra")


def test_batch_request_defaults_to_medium():
    request = BatchGenerateRequest(
        start_chapter_number=1, count=1, content_language="zh"
    )
    assert request.reasoning_effort == "medium"


# ===== 请求体注入 =====


@pytest.mark.anyio
async def test_payload_carries_reasoning_effort(monkeypatch):
    from app.services.ai_clients import openai_client as openai_client_module

    monkeypatch.setattr(openai_client_module, "_STREAM_USAGE_UNSUPPORTED_BASE_URLS", set())
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(json.loads(request.content))
        return _ok_stream()

    client, pooled = _make_client(handler, "https://gateway.test/v1")
    try:
        chunks = [
            chunk
            async for chunk in client.chat_completion_stream(
                [{"role": "user", "content": "hi"}],
                "test-model",
                0.7,
                100,
                reasoning_effort="low",
            )
        ]
    finally:
        await _close(client, pooled)
    assert chunks
    assert seen and seen[0]["reasoning_effort"] == "low"


@pytest.mark.anyio
async def test_payload_omits_reasoning_effort_when_not_given(monkeypatch):
    from app.services.ai_clients import openai_client as openai_client_module

    monkeypatch.setattr(openai_client_module, "_STREAM_USAGE_UNSUPPORTED_BASE_URLS", set())
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(json.loads(request.content))
        return _ok_stream()

    client, pooled = _make_client(handler, "https://gateway.test/v1")
    try:
        [
            chunk
            async for chunk in client.chat_completion_stream(
                [{"role": "user", "content": "hi"}], "test-model", 0.7, 100
            )
        ]
    finally:
        await _close(client, pooled)
    assert "reasoning_effort" not in seen[0]


@pytest.mark.anyio
async def test_retry_after_stream_options_rejection_keeps_reasoning_effort(monkeypatch):
    from app.services.ai_clients import openai_client as openai_client_module

    monkeypatch.setattr(openai_client_module, "_STREAM_USAGE_UNSUPPORTED_BASE_URLS", set())
    """去 stream_options 的重试递归必须把档位带下去，否则重试会退回高推理。"""
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        seen.append(body)
        if "stream_options" in body:
            return httpx.Response(
                400,
                json={"error": {"message": "unknown field stream_options"}},
            )
        return _ok_stream()

    client, pooled = _make_client(handler, "https://gateway-retry.test/v1")
    try:
        [
            chunk
            async for chunk in client.chat_completion_stream(
                [{"role": "user", "content": "hi"}],
                "test-model",
                0.7,
                100,
                reasoning_effort="medium",
            )
        ]
    finally:
        await _close(client, pooled)

    assert len(seen) == 2
    assert seen[0]["reasoning_effort"] == "medium"
    assert seen[1]["reasoning_effort"] == "medium"


# ===== 字数下限 =====


def test_length_floor_note_zh():
    from app.api.chapters import _length_floor_note

    note = _length_floor_note(3000, "zh")
    assert "不少于 2790 字" in note
    assert "不得收尾" in note


def test_length_floor_note_en():
    from app.api.chapters import _length_floor_note

    assert "at least 2790 characters" in _length_floor_note(3000, "en")


def test_length_floor_note_empty_without_target():
    from app.api.chapters import _length_floor_note

    assert _length_floor_note(None, "zh") == ""
    assert _length_floor_note(0, "zh") == ""


def test_append_style_tail_applies_floor_even_without_profile():
    from app.api.chapters import _append_style_tail

    prompt = _append_style_tail("BODY", "", 3000)
    assert prompt.startswith("BODY")
    assert "不少于 2790 字" in prompt


def test_append_style_tail_has_no_floor_when_target_missing():
    from app.api.chapters import _append_style_tail

    assert _append_style_tail("BODY", "") == "BODY"


@pytest.mark.anyio
async def test_gateway_rejecting_reasoning_effort_is_retried_without_it(monkeypatch):
    """网关不认识该字段 → 去字段重试一次并成功，不再让整章生成直接失败。"""
    from app.services.ai_clients import openai_client as openai_client_module

    monkeypatch.setattr(openai_client_module, "_STREAM_USAGE_UNSUPPORTED_BASE_URLS", set())
    monkeypatch.setattr(
        openai_client_module, "_REASONING_EFFORT_UNSUPPORTED_BASE_URLS", set()
    )
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        seen.append(body)
        if "reasoning_effort" in body:
            return httpx.Response(
                400,
                json={"error": {"message": "unknown field 'reasoning_effort'"}},
            )
        return _ok_stream()

    client, pooled = _make_client(handler, "https://gateway-strict.test/v1")
    try:
        chunks = [
            chunk
            async for chunk in client.chat_completion_stream(
                [{"role": "user", "content": "hi"}],
                "test-model",
                0.7,
                100,
                reasoning_effort="medium",
            )
        ]
    finally:
        await _close(client, pooled)

    assert chunks
    assert len(seen) == 2
    assert "reasoning_effort" in seen[0]
    assert "reasoning_effort" not in seen[1]


@pytest.mark.anyio
async def test_rejected_reasoning_effort_is_remembered_per_base_url(monkeypatch):
    """学到之后，同一网关的后续请求不再先吃一次 400。"""
    from app.services.ai_clients import openai_client as openai_client_module

    monkeypatch.setattr(openai_client_module, "_STREAM_USAGE_UNSUPPORTED_BASE_URLS", set())
    monkeypatch.setattr(
        openai_client_module, "_REASONING_EFFORT_UNSUPPORTED_BASE_URLS", set()
    )
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        seen.append(body)
        if "reasoning_effort" in body:
            return httpx.Response(
                400, json={"error": {"message": "unrecognized field reasoning_effort"}}
            )
        return _ok_stream()

    client, pooled = _make_client(handler, "https://gateway-memo.test/v1")
    try:
        for _ in range(2):
            [
                chunk
                async for chunk in client.chat_completion_stream(
                    [{"role": "user", "content": "hi"}],
                    "test-model",
                    0.7,
                    100,
                    reasoning_effort="low",
                )
            ]
    finally:
        await _close(client, pooled)

    # 第一次：带字段撞 400 → 去字段重试；第二次：直接不带字段。
    assert [("reasoning_effort" in body) for body in seen] == [True, False, False]


@pytest.mark.anyio
async def test_unrelated_400_still_raises(monkeypatch):
    """降级只在错误体提到该字段时触发，其它 400 不能被吞成「去掉字段再试」。"""
    from app.services.ai_clients import openai_client as openai_client_module

    monkeypatch.setattr(openai_client_module, "_STREAM_USAGE_UNSUPPORTED_BASE_URLS", set())
    monkeypatch.setattr(
        openai_client_module, "_REASONING_EFFORT_UNSUPPORTED_BASE_URLS", set()
    )
    attempts = []

    def handler(request: httpx.Request) -> httpx.Response:
        attempts.append(json.loads(request.content))
        return httpx.Response(400, json={"error": {"message": "invalid api key"}})

    client, pooled = _make_client(handler, "https://gateway-badkey.test/v1")
    try:
        with pytest.raises(Exception):
            [
                chunk
                async for chunk in client.chat_completion_stream(
                    [{"role": "user", "content": "hi"}],
                    "test-model",
                    0.7,
                    100,
                    reasoning_effort="low",
                )
            ]
    finally:
        await _close(client, pooled)

    assert len(attempts) == 1


# ===== 原文语感锚点：必须贴在生成点，且排在字数下限之前 =====


def _anchor_content() -> str:
    """用真实渲染函数生成风格正文，避免在测试里再抄一份标题字面量。"""
    from app.services.style_anchors import append_project_style_anchors  # noqa: F401
    from app.services.style_anchors import build_style_anchor_block

    return "普通风格描述\n" + build_style_anchor_block(["甲句。乙句。"])


def test_style_tail_copies_the_anchor_block_to_the_end():
    from app.api.chapters import _append_style_tail

    prompt = _append_style_tail("BODY", _anchor_content(), 3000)
    assert "甲句。乙句。" in prompt
    assert prompt.index("甲句") > prompt.index("BODY")


def test_length_floor_stays_after_the_anchor_sample():
    """硬约束要在样本之后（最贴近生成点），否则模型会照着样本随手收尾。"""
    from app.api.chapters import _append_style_tail

    prompt = _append_style_tail("BODY", _anchor_content(), 3000)
    assert prompt.index("不少于 2790 字") > prompt.index("甲句")


def test_style_tail_without_anchor_does_not_invent_one():
    from app.api.chapters import _append_style_tail

    assert "本书原文语感锚点" not in _append_style_tail("BODY", "普通风格描述", 3000)


def test_extract_anchor_block_returns_empty_when_absent():
    from app.api.chapters import _extract_anchor_block

    assert _extract_anchor_block("普通风格描述") == ""
    assert _extract_anchor_block(_anchor_content()).startswith("【本书原文语感锚点")


def test_extract_anchor_block_matches_the_real_renderer():
    """定位前缀与渲染头必须同源：改了渲染头而没改前缀时，这里会先红。"""
    from app.api.chapters import _extract_anchor_block
    from app.services.style_anchors import ANCHOR_HEADER, build_style_anchor_block

    rendered = build_style_anchor_block(["样本一。"])
    assert ANCHOR_HEADER in rendered
    assert _extract_anchor_block("规格\n" + rendered).startswith(ANCHOR_HEADER)


def test_extract_anchor_block_warns_when_the_header_drifted(caplog):
    """渲染头被改掉、前缀失配时必须留痕，不能静默不注入。"""
    import logging

    from app.api.chapters import _extract_anchor_block

    with caplog.at_level(logging.WARNING):
        assert _extract_anchor_block("【本书原文语感锚点（措辞改过了）】\n样本") == ""
    assert any("无法按渲染头定位" in record.message for record in caplog.records)
