"""call_with_json_retry 空输出按 finish_reason 分流测试。

修复背景：上游内容审核会把请求拦成 HTTP 200 + finish_reason="content_filter"
+ 正文 0 字符。原实现只看「正文为空」就按同一 prompt 重试，必然被同样拦截，
最后抛泛化的 ValueError("模型输出为空，无法解析 JSON")，真实原因只活在某条
WARNING 里。现改为：
- content_filter → 不重试，直接 ai.content_filtered；
- length → 不重试，直接 ai.output_truncated；
- 其它（stop/空/None）→ 保留原有重试与最终 ValueError 文案。
"""
import pytest

from app.core.errors import ApiError
from app.services.ai_service import AIService

EMPTY_MESSAGE = "模型输出为空，无法解析 JSON"


def _make_service() -> AIService:
    """构造不发真实请求的 AIService（未绑定用户 ⇒ 模型门禁直通）。"""
    return AIService(
        api_provider="openai",
        api_key="test",
        api_base_url="http://localhost",
        default_model="test-model",
        default_temperature=0.7,
        default_max_tokens=1000,
    )


def _patch_stream(monkeypatch, chunks):
    """替换 generate_text_stream，记录调用次数，只吐出给定的块。"""
    calls = []

    async def _fake_stream(self, **kwargs):
        calls.append(kwargs)
        for chunk in chunks:
            yield chunk

    monkeypatch.setattr(AIService, "generate_text_stream", _fake_stream)
    return calls


@pytest.mark.anyio
async def test_content_filter_fails_fast_without_retry(monkeypatch):
    """content_filter：正文为空也不重试，一次调用即抛 ai.content_filtered。"""
    calls = _patch_stream(monkeypatch, [{"finish_reason": "content_filter"}])
    svc = _make_service()

    with pytest.raises(ApiError) as ei:
        await svc.call_with_json_retry(
            prompt="生成角色", expected_type="object", max_retries=3, auto_mcp=False
        )

    assert ei.value.code == "ai.content_filtered"
    assert ei.value.params["finish_reason"] == "content_filter"
    # 被审核拦截时同 prompt 重试必然同样被拦 ⇒ 只允许调用 1 次
    assert len(calls) == 1


@pytest.mark.anyio
async def test_length_fails_fast_without_retry(monkeypatch):
    """length：预算耗尽截断，同预算重试无效，一次调用即抛 ai.output_truncated。"""
    calls = _patch_stream(monkeypatch, [{"finish_reason": "length"}])
    svc = _make_service()

    with pytest.raises(ApiError) as ei:
        await svc.call_with_json_retry(
            prompt="生成角色", expected_type="object", max_retries=3, auto_mcp=False
        )

    assert ei.value.code == "ai.output_truncated"
    assert ei.value.params["finish_reason"] == "length"
    assert len(calls) == 1


@pytest.mark.anyio
async def test_empty_content_with_stop_still_retries(monkeypatch):
    """回归：空正文 + finish_reason=stop 仍按 max_retries 重试，最终 ValueError 文案不变。"""
    max_retries = 3
    calls = _patch_stream(monkeypatch, [{"finish_reason": "stop"}])
    svc = _make_service()

    with pytest.raises(ValueError) as ei:
        await svc.call_with_json_retry(
            prompt="生成角色",
            expected_type="object",
            max_retries=max_retries,
            auto_mcp=False,
        )

    assert not isinstance(ei.value, ApiError)
    assert str(ei.value) == EMPTY_MESSAGE
    assert len(calls) == max_retries
