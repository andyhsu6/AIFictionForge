import asyncio
import json
from app.services.ai_clients.gemini_client import GeminiClient


class _FakeStreamResponse:
    def __init__(self, lines):
        self._lines = lines
        self.status_code = 200

    def raise_for_status(self):
        return None

    async def aiter_lines(self):
        for line in self._lines:
            yield line

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False


def test_gemini_stream_maps_prompt_feedback_block_to_content_filter():
    async def _run():
        client = GeminiClient(api_key="test-key", base_url="https://example.test/v1beta")
        lines = [
            'data:{"promptFeedback":{"blockReason":"PROHIBITED_CONTENT"},"usageMetadata":{"promptTokenCount":100,"totalTokenCount":100}}',
        ]
        client.client.stream = lambda *args, **kwargs: _FakeStreamResponse(lines)

        chunks = [
            item
            async for item in client.chat_completion_stream(
                messages=[{"role": "user", "content": "hi"}],
                model="gemini-4-hydrogen",
                temperature=0.7,
                max_tokens=1024,
            )
        ]

        assert any(c.get("finish_reason") == "content_filter" for c in chunks)
        assert any(c.get("done") is True for c in chunks)

    asyncio.run(_run())


def test_gemini_stream_maps_candidate_finish_reasons_and_skips_thought_parts():
    async def _run():
        client = GeminiClient(api_key="test-key", base_url="https://example.test/v1beta")
        lines = [
            "data: " + json.dumps({
                "candidates": [{
                    "content": {
                        "parts": [
                            {"text": "secret reasoning", "thought": True},
                            {"text": "正文一段"},
                        ]
                    },
                    "finishReason": "MAX_TOKENS",
                }],
                "usageMetadata": {"promptTokenCount": 50, "candidatesTokenCount": 20, "totalTokenCount": 70},
            }),
        ]
        client.client.stream = lambda *args, **kwargs: _FakeStreamResponse(lines)

        chunks = [
            item
            async for item in client.chat_completion_stream(
                messages=[{"role": "user", "content": "hi"}],
                model="gemini-4-hydrogen",
                temperature=0.7,
                max_tokens=1024,
            )
        ]

        contents = [c["content"] for c in chunks if "content" in c]
        assert contents == ["正文一段"]
        assert any(c.get("finish_reason") == "length" for c in chunks)
        assert any(c.get("done") is True for c in chunks)

    asyncio.run(_run())
