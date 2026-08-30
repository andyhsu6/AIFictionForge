"""思考型模型 token 预算提升的集成测试（#13 Step 1）。

验证 generate_text / generate_text_stream 在思考型模型 + 低默认预算时，
实际传给 provider 的 max_tokens 被提升到 64000。
"""
import pytest
from unittest.mock import AsyncMock, MagicMock

from app.services.ai_service import AIService


class _RecordingProvider:
    """记录传给 generate 的 max_tokens 的假 provider。"""

    def __init__(self):
        self.calls = []

    async def generate(self, **kwargs):
        self.calls.append(kwargs)
        return {"content": "{}", "finish_reason": "stop", "usage": {}}

    def generate_stream(self, **kwargs):
        self.calls.append(kwargs)
        async def _gen():
            yield "{}"
        return _gen()


def _make_service(provider, *, max_tokens=32000, model="deepseek-v4-flash"):
    svc = AIService(default_model=model, default_max_tokens=max_tokens)
    # 绕过 __init__ 的 provider 初始化（需要 key），直接注入假 provider
    svc._openai_provider = provider
    svc.api_provider = "openai"
    return svc


@pytest.mark.anyio
async def test_generate_text_boosts_thinking_model_budget():
    rec = _RecordingProvider()
    svc = _make_service(rec, max_tokens=32000, model="deepseek-v4-flash")

    await svc.generate_text(prompt="生成 5 个角色 JSON", auto_mcp=False)

    assert rec.calls[0]["max_tokens"] == 64000


@pytest.mark.anyio
async def test_generate_text_keeps_non_thinking_default():
    rec = _RecordingProvider()
    svc = _make_service(rec, max_tokens=32000, model="gpt-4o")

    await svc.generate_text(prompt="生成 5 个角色 JSON", auto_mcp=False)

    assert rec.calls[0]["max_tokens"] == 32000


@pytest.mark.anyio
async def test_generate_text_respects_explicit_max_tokens():
    rec = _RecordingProvider()
    svc = _make_service(rec, max_tokens=32000, model="deepseek-v4-flash")

    await svc.generate_text(prompt="生成", max_tokens=20000, auto_mcp=False)

    assert rec.calls[0]["max_tokens"] == 20000


@pytest.mark.anyio
async def test_generate_text_stream_boosts_thinking_model_budget():
    rec = _RecordingProvider()
    svc = _make_service(rec, max_tokens=32000, model="deepseek-v4-flash")

    parts = []
    async for chunk in svc.generate_text_stream(prompt="生成 JSON", auto_mcp=False):
        parts.append(chunk)

    assert rec.calls[0]["max_tokens"] == 64000
    assert "".join(parts) == "{}"
