"""finish_reason 从流内部传到 call_with_json_retry 的链路测试（#178）。

修复背景：`generate_text_stream` 是异步生成器，内部把 provider 的 dict 块
（usage / finish_reason）全部消费，只 yield 字符串。`call_with_json_retry`
却期待从 chunk 里读到 dict 形式的 finish_reason，该分支永远进不去 ⇒
`result["finish_reason"]` 恒为 None，`ai.content_filtered` /
`ai.output_truncated` 分流是死代码，被审核拦截时白白重试到泛化的
ValueError("模型输出为空，无法解析 JSON")，指标里的「结束原因」永远是「未知」。

本测试用假 provider 驱动**真实**的 generate_text_stream（不打真实请求），
覆盖：分流生效、调用次数、回归文案、新出参的写回契约。
"""
from typing import Any, List

import pytest

from app.core.errors import ApiError
from app.services.ai_service import AIService

EMPTY_MESSAGE = "模型输出为空，无法解析 JSON"


class _FinishProvider:
    """按块吐出文本/控制块，并记录被调用（= 一次上游请求）的次数。"""

    def __init__(self, chunks: List[Any], fail_after: bool = False):
        self.chunks = chunks
        self.fail_after = fail_after
        self.stream_calls = 0

    async def generate(self, **kwargs):
        raise AssertionError("不应走非流式路径")

    def generate_stream(self, **kwargs):
        self.stream_calls += 1

        async def _gen():
            for chunk in self.chunks:
                yield chunk
            if self.fail_after:
                raise RuntimeError("upstream exploded")

        return _gen()


def _make_service(provider: _FinishProvider) -> AIService:
    svc = AIService(
        api_provider="openai",
        api_key="test",
        api_base_url="http://localhost",
        default_model="test-model",
        default_temperature=0.7,
        default_max_tokens=1000,
    )
    svc._openai_provider = provider
    return svc


@pytest.mark.anyio
async def test_content_filter_fails_fast_through_real_stream():
    """content_filter + 正文 0 字符：一次调用即抛 ai.content_filtered。"""
    rec = _FinishProvider([{"finish_reason": "content_filter"}])
    svc = _make_service(rec)

    with pytest.raises(ApiError) as ei:
        await svc.call_with_json_retry(
            prompt="生成角色", expected_type="object", max_retries=3, auto_mcp=False
        )

    assert ei.value.code == "ai.content_filtered"
    assert ei.value.params["finish_reason"] == "content_filter"
    # 被审核拦截时同 prompt 重试必然同样被拦 ⇒ 只允许 1 次上游请求
    assert rec.stream_calls == 1


@pytest.mark.anyio
async def test_length_fails_fast_through_real_stream():
    """length + 正文 0 字符：一次调用即抛 ai.output_truncated。"""
    rec = _FinishProvider([{"finish_reason": "length"}])
    svc = _make_service(rec)

    with pytest.raises(ApiError) as ei:
        await svc.call_with_json_retry(
            prompt="生成角色", expected_type="object", max_retries=3, auto_mcp=False
        )

    assert ei.value.code == "ai.output_truncated"
    assert ei.value.params["finish_reason"] == "length"
    assert rec.stream_calls == 1


@pytest.mark.anyio
async def test_empty_content_with_stop_still_retries_with_unchanged_message():
    """回归：空正文 + finish_reason=stop 仍重试 max_retries 次，ValueError 文案逐字不变。"""
    max_retries = 3
    rec = _FinishProvider([{"finish_reason": "stop"}])
    svc = _make_service(rec)

    with pytest.raises(ValueError) as ei:
        await svc.call_with_json_retry(
            prompt="生成角色", expected_type="object", max_retries=max_retries, auto_mcp=False
        )

    assert not isinstance(ei.value, ApiError)
    assert str(ei.value) == EMPTY_MESSAGE
    assert rec.stream_calls == max_retries


@pytest.mark.anyio
async def test_generate_text_stream_writes_finish_reason_to_sink():
    """出参 sink 拿到真实结束原因；yield 契约仍是「只吐字符串」。"""
    rec = _FinishProvider(["你好", {"finish_reason": "length"}])
    svc = _make_service(rec)
    sink: dict = {}

    chunks = []
    async for chunk in svc.generate_text_stream(
        prompt="生成", auto_mcp=False, finish_sink=sink
    ):
        chunks.append(chunk)

    assert chunks == ["你好"]
    assert all(isinstance(c, str) for c in chunks)
    assert sink["finish_reason"] == "length"


@pytest.mark.anyio
async def test_generate_text_stream_without_sink_unchanged():
    """不传 sink 时行为与改动前一致：只 yield 字符串，不 yield dict。"""
    rec = _FinishProvider(["你好", {"usage": {"total_tokens": 5}}, {"finish_reason": "length"}])
    svc = _make_service(rec)

    chunks = []
    async for chunk in svc.generate_text_stream(prompt="生成", auto_mcp=False):
        chunks.append(chunk)

    assert chunks == ["你好"]
    assert all(isinstance(c, str) for c in chunks)


@pytest.mark.anyio
async def test_generate_text_stream_sink_written_on_error_path():
    """异常路径也写回结束原因（finally 保证），调用方不会丢失诊断信息。"""
    rec = _FinishProvider(["部分"], fail_after=True)
    svc = _make_service(rec)
    sink: dict = {}

    with pytest.raises(RuntimeError):
        async for _ in svc.generate_text_stream(prompt="生成", auto_mcp=False, finish_sink=sink):
            pass

    assert sink.get("finish_reason") == "stop"
