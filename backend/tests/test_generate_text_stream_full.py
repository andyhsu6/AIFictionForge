"""generate_text_stream_full 流式累积方法测试（#13 流式化原则）。

创作助手最终回答轮需要流式（避免长回复触发网关 524），但需要拿到
与 generate_text 相同的完整响应结构（content/finish_reason/usage）。
"""
import pytest

from app.services.ai_service import AIService


class _StreamProvider:
    def __init__(self, chunks=None):
        self.calls = []
        self.chunks = chunks or ["你好，", "我是创作助手。"]

    def generate(self, **kwargs):
        raise AssertionError("不应走非流式 generate")

    def generate_stream(self, **kwargs):
        self.calls.append(kwargs)
        async def _gen():
            for c in self.chunks:
                yield c
            yield {"usage": {"prompt_tokens": 10, "completion_tokens": 7, "total_tokens": 17}}
            yield {"finish_reason": "stop", "done": True}
        return _gen()


def _make_service(provider):
    svc = AIService(default_model="gpt-4o", default_max_tokens=32000)
    svc._openai_provider = provider
    svc.api_provider = "openai"
    return svc


@pytest.mark.anyio
async def test_stream_full_returns_content_dict():
    """流式累积结果应与 generate_text 返回结构一致。"""
    rec = _StreamProvider()
    svc = _make_service(rec)

    result = await svc.generate_text_stream_full(
        prompt="介绍项目",
        auto_mcp=False,
    )

    assert result["content"] == "你好，我是创作助手。"
    assert rec.calls[0]["max_tokens"] == 32000  # 非思考型模型不提升


@pytest.mark.anyio
async def test_stream_full_captures_usage_and_finish_reason():
    """usage 与 finish_reason 应被捕获。"""
    rec = _StreamProvider()
    svc = _make_service(rec)

    result = await svc.generate_text_stream_full(prompt="你好", auto_mcp=False)

    assert result["finish_reason"] == "stop"
    assert result["usage"]["completion_tokens"] == 7


@pytest.mark.anyio
async def test_stream_full_respects_explicit_max_tokens():
    """显式 max_tokens 应被尊重。"""
    rec = _StreamProvider()
    svc = _make_service(rec)

    result = await svc.generate_text_stream_full(prompt="你好", max_tokens=50000, auto_mcp=False)

    assert result["content"] == "你好，我是创作助手。"
    assert rec.calls[0]["max_tokens"] == 50000
