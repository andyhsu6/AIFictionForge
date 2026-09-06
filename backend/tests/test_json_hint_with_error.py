"""重试提示注入具体 JSON 错误测试。

修复背景：重试提示只含截断的失败输出，模型不知道错在哪。
现在把具体解析错误（如非法字符位置）注入提示词，提高重试成功率。
"""
import pytest

from app.services.ai_service import AIService


class _FlakyProvider:
    """首次输出非法 JSON，第二次成功；记录每次收到的 prompt。"""

    def __init__(self):
        self.prompts = []
        self.n = 0

    async def generate(self, **kwargs):
        raise AssertionError("不应走非流式路径")

    def generate_stream(self, **kwargs):
        self.prompts.append(kwargs.get("prompt", ""))
        self.n += 1

        async def _gen():
            if self.n == 1:
                yield "不是 JSON"
            else:
                yield '{"ok": true}'

        return _gen()


def _make_service(provider, *, model="gpt-4o", max_tokens=32000):
    svc = AIService(default_model=model, default_max_tokens=max_tokens)
    svc._openai_provider = provider
    svc.api_provider = "openai"
    return svc


@pytest.mark.anyio
async def test_retry_prompt_contains_json_error():
    """重试提示注入具体 JSON 解析错误。"""
    rec = _FlakyProvider()
    svc = _make_service(rec)

    result = await svc.call_with_json_retry(prompt="生成", expected_type="object")

    assert result == {"ok": True}
    assert rec.n == 2
    first_prompt = rec.prompts[0]
    retry_prompt = rec.prompts[1]
    assert retry_prompt.startswith(first_prompt)
    assert "JSON 错误详情" in retry_prompt


@pytest.mark.anyio
async def test_add_json_hint_formats_errors():
    """_add_json_hint 分别拼接 JSON 错误与校验提示。"""
    hint = AIService._add_json_hint(
        prompt="生成",
        attempt=2,
        json_error="Expecting value: line 1 column 5",
        extra_error="字段 name 缺失",
    )
    assert "JSON 错误详情: Expecting value: line 1 column 5" in hint
    assert "校验提示: 字段 name 缺失" in hint
    # 不截断超过 300 字符的错误
    long_error = "x" * 500
    hint2 = AIService._add_json_hint(prompt="生成", attempt=2, json_error=long_error)
    assert "x" * 300 in hint2
    assert "x" * 500 not in hint2
