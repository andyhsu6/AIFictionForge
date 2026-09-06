"""空内容快速失败测试。

修复背景：思考型模型把 token 预算耗在推理上时正文为 0 字符，
解析必然失败。快速失败：跳过无意义的 JSON 解析直接重试。
"""
import pytest

from app.services.ai_service import AIService


class _EmptyThenOkProvider:
    """首次输出为空（模拟思考耗光预算），第二次输出合法 JSON。"""

    def __init__(self):
        self.calls = []
        self.n = 0

    async def generate(self, **kwargs):
        raise AssertionError("不应走非流式路径")

    def generate_stream(self, **kwargs):
        self.calls.append(kwargs)
        self.n += 1

        async def _gen():
            if self.n == 1:
                yield ""
            else:
                yield '{"name": "张三"}'

        return _gen()


def _make_service(provider, *, model="gpt-4o", max_tokens=32000):
    svc = AIService(default_model=model, default_max_tokens=max_tokens)
    svc._openai_provider = provider
    svc.api_provider = "openai"
    return svc


@pytest.mark.anyio
async def test_empty_content_immediately_retries():
    """空输出立即重试（不消耗多余解析），第二次成功后返回。"""
    rec = _EmptyThenOkProvider()
    svc = _make_service(rec)

    result = await svc.call_with_json_retry(prompt="生成角色", expected_type="object")

    assert result == {"name": "张三"}
    assert rec.n == 2


@pytest.mark.anyio
async def test_response_format_injected_by_default():
    """默认自动注入 response_format=json_object（无 MCP tools、OpenAI 提供商时）。"""
    rec = _EmptyThenOkProvider()
    svc = _make_service(rec)

    await svc.call_with_json_retry(prompt="生成角色", expected_type="object")

    assert rec.calls[0]["response_format"] == {"type": "json_object"}
    assert rec.calls[1]["response_format"] == {"type": "json_object"}
