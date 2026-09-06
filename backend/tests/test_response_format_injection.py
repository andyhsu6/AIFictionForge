"""response_format 注入测试。

修复背景：思考型模型（deepseek-v4-flash 等）间歇性返回空正文或含中文引号的
非法 JSON，导致解析失败。通过在 OpenAI 兼容 API 请求中注入
response_format={"type": "json_object"}，让服务端强制输出合法 JSON。
"""
from app.services.ai_clients.openai_client import OpenAIClient


def _make_client():
    return OpenAIClient(api_key="test-key", base_url="https://api.openai.com/v1")


def test_build_payload_injects_response_format_when_passed():
    """显式传入 response_format 时注入 payload。"""
    client = _make_client()
    payload = client._build_payload(
        messages=[{"role": "user", "content": "hi"}],
        model="gpt-4o",
        temperature=0.7,
        max_tokens=100,
        response_format={"type": "json_object"},
    )
    assert payload["response_format"] == {"type": "json_object"}


def test_build_payload_omits_response_format_by_default():
    """未传 response_format 时不注入（非 JSON 调用不受影响）。"""
    client = _make_client()
    payload = client._build_payload(
        messages=[{"role": "user", "content": "hi"}],
        model="gpt-4o",
        temperature=0.7,
        max_tokens=100,
    )
    assert "response_format" not in payload
