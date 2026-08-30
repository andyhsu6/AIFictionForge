"""call_with_json_retry 流式改造测试（#13 Step 2）。

修复背景：call_with_json_retry 内部使用非流式 generate_text，思考型模型
长 JSON 输出时推理阶段即超时（Cloudflare 524）。改为流式累积后：
- 推理增量不占用网关单次响应时长（边生成边送达）
- 正文通过流式块累积，最终仍返回完整解析后的 JSON
"""
import pytest

from app.services.ai_service import AIService


class _StreamProvider:
    """流式 provider：按块吐出 JSON，验证累积逻辑。"""

    def __init__(self, chunks=None, fail_first=False):
        self.calls = []
        self.chunks = chunks or ['{"name": "张三"}']
        self.fail_first = fail_first

    async def generate(self, **kwargs):
        self.calls.append(("non-stream", kwargs))
        raise AssertionError("不应走非流式路径")

    def generate_stream(self, **kwargs):
        self.calls.append(("stream", kwargs))
        async def _gen():
            for c in self.chunks:
                yield c
        return _gen()


def _make_service(provider, *, model="gpt-4o", max_tokens=32000):
    svc = AIService(default_model=model, default_max_tokens=max_tokens)
    svc._openai_provider = provider
    svc.api_provider = "openai"
    return svc


@pytest.mark.anyio
async def test_call_with_json_retry_uses_stream_path():
    """拆书路径必须走流式，不再走非流式 generate。"""
    rec = _StreamProvider()
    svc = _make_service(rec)

    result = await svc.call_with_json_retry(prompt="生成角色", expected_type="object")

    assert result == {"name": "张三"}
    assert rec.calls[0][0] == "stream"


@pytest.mark.anyio
async def test_call_with_json_retry_accumulates_multiple_chunks():
    """多块流式输出正确累积成完整 JSON。"""
    rec = _StreamProvider(chunks=['{"name":', ' "张三",', ' "age": 18}'])
    svc = _make_service(rec)

    result = await svc.call_with_json_retry(prompt="生成角色", expected_type="object")

    assert result == {"name": "张三", "age": 18}


@pytest.mark.anyio
async def test_call_with_json_retry_returns_array():
    """expected_type=array 时返回列表。"""
    rec = _StreamProvider(chunks=['[{"name": "A"}, {"name": "B"}]'])
    svc = _make_service(rec)

    result = await svc.call_with_json_retry(prompt="生成列表", expected_type="array")

    assert isinstance(result, list)
    assert len(result) == 2


@pytest.mark.anyio
async def test_call_with_json_retry_retries_on_parse_failure():
    """首次返回非 JSON → 重试（流式路径），成功后返回。"""
    calls = {"n": 0}

    class _FlakyProvider(_StreamProvider):
        def generate_stream(self, **kwargs):
            calls["n"] += 1
            async def _gen():
                if calls["n"] == 1:
                    yield "不是 JSON"
                else:
                    yield '{"ok": true}'
            return _gen()

    rec = _FlakyProvider()
    svc = _make_service(rec)

    result = await svc.call_with_json_retry(prompt="生成", expected_type="object")

    assert result == {"ok": True}
    assert calls["n"] == 2  # 首次失败 + 一次重试
