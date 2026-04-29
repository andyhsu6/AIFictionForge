"""Gemini 封面图片 Provider"""
from __future__ import annotations

import base64
from typing import Any

import httpx

from app.logger import get_logger
from app.services.cover_providers.base_cover_provider import BaseCoverProvider, CoverGenerationResult

logger = get_logger(__name__)


class GeminiCoverProvider(BaseCoverProvider):
    """基于 Gemini API 的封面生成实现"""

    def __init__(self, api_key: str, base_url: str):
        self.api_key = api_key
        self.base_url = (base_url or "https://generativelanguage.googleapis.com/v1beta").rstrip("/")

    async def generate_cover(
        self,
        *,
        prompt: str,
        model: str,
        width: int,
        height: int,
    ) -> CoverGenerationResult:
        url = f"{self.base_url}/models/{model}:generateContent"
        payload: dict[str, Any] = {
            "contents": [{
                "role": "user",
                "parts": [{
                    "text": (
                        f"{prompt}\n\n"
                        f"Generate a final cover image at {width}x{height} pixels. "
                        "Return one final cover image."
                    )
                }]
            }],
            "generationConfig": {
                "temperature": 0.4,
            },
        }
        headers = {
            "x-goog-api-key": self.api_key,
            "Content-Type": "application/json",
        }

        try:
            async with httpx.AsyncClient(timeout=120.0) as client:
                response = await client.post(url, headers=headers, json=payload)

            response.raise_for_status()
            data = response.json()
        except httpx.HTTPStatusError as exc:
            logger.error(
                "Gemini 封面生成 HTTP 错误: status=%s response=%s",
                exc.response.status_code if exc.response else None,
                exc.response.text[:2000] if exc.response is not None else None,
            )
            raise
        except Exception:
            logger.error("Gemini 封面生成请求异常", exc_info=True)
            raise

        candidates = data.get("candidates") or []
        if not candidates:
            raise ValueError("Gemini 未返回候选结果")

        parts = candidates[0].get("content", {}).get("parts", [])
        for part in parts:
            inline_data = part.get("inlineData")
            if not inline_data:
                continue

            mime_type = inline_data.get("mimeType", "image/png")
            image_data = inline_data.get("data")
            if not image_data:
                continue

            file_extension = "png" if "png" in mime_type else "jpg"
            return {
                "content": base64.b64decode(image_data),
                "mime_type": mime_type,
                "file_extension": file_extension,
                "revised_prompt": None,
                "provider": "gemini",
                "model": model,
            }

        logger.error("Gemini 返回内容中未找到 inlineData 图像数据")
        raise ValueError("Gemini 未返回图片数据")
