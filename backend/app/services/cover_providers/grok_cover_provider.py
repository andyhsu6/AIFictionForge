"""Grok 封面图片 Provider"""
from __future__ import annotations

import base64
import struct
from typing import Any

import httpx

from app.logger import get_logger
from app.services.cover_providers.base_cover_provider import BaseCoverProvider, CoverGenerationResult

logger = get_logger(__name__)


class GrokCoverProvider(BaseCoverProvider):
    """基于 xAI Grok Images API 的封面生成实现"""

    def __init__(self, api_key: str, base_url: str):
        self.api_key = api_key
        self.base_url = (base_url or "https://api.x.ai/v1").rstrip("/")

    async def generate_cover(
        self,
        *,
        prompt: str,
        model: str,
        width: int,
        height: int,
    ) -> CoverGenerationResult:
        result = await self._request_cover(
            prompt=prompt,
            model=model,
            width=width,
            height=height,
        )
        return self._to_public_result(result)

    async def _request_cover(
        self,
        *,
        prompt: str,
        model: str,
        width: int,
        height: int,
    ) -> dict[str, Any]:
        url = f"{self.base_url}/images/generations"
        payload: dict[str, Any] = {
            "model": model,
            "prompt": self._adapt_prompt(prompt=prompt, width=width, height=height),
            "n": 1,
            "response_format": "b64_json",
            "aspect_ratio": self._get_aspect_ratio(width=width, height=height),
            "resolution": self._get_resolution(width=width, height=height),
        }

        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }

        logger.debug(
            "Grok 封面生成请求开始: url=%s model=%s width=%s height=%s prompt_len=%s prompt_preview=%s",
            url,
            model,
            width,
            height,
            len(prompt or ""),
            (prompt or "")[:300].replace("\n", " "),
        )

        try:
            async with httpx.AsyncClient(timeout=180.0) as client:
                response = await client.post(url, headers=headers, json=payload)

            logger.debug(
                "Grok 封面生成响应: status=%s content_type=%s headers=%s body_preview=%s",
                response.status_code,
                response.headers.get("content-type"),
                {
                    "x-request-id": response.headers.get("x-request-id"),
                    "cf-ray": response.headers.get("cf-ray"),
                    "openai-processing-ms": response.headers.get("openai-processing-ms"),
                },
                response.text[:1000],
            )

            response.raise_for_status()
            data = response.json()
        except httpx.HTTPStatusError as exc:
            logger.error(
                "Grok 封面生成 HTTP 错误: status=%s response=%s",
                exc.response.status_code if exc.response else None,
                exc.response.text[:2000] if exc.response is not None else None,
            )
            raise
        except Exception:
            logger.error("Grok 封面生成请求异常", exc_info=True)
            raise

        images = data.get("data") or []
        logger.debug(
            "Grok 封面生成解析结果: has_data=%s image_count=%s keys=%s",
            bool(data),
            len(images),
            list(data.keys()) if isinstance(data, dict) else type(data).__name__,
        )

        if not images:
            logger.error("Grok 未返回图片结果: data=%s", data)
            raise ValueError("Grok 未返回图片结果")

        image_item = images[0]
        revised_prompt = image_item.get("revised_prompt")
        logger.debug(
            "Grok 首张图片结果: keys=%s has_b64=%s has_url=%s revised_prompt_preview=%s",
            list(image_item.keys()),
            bool(image_item.get("b64_json")),
            bool(image_item.get("url")),
            (revised_prompt or "")[:300],
        )

        b64_json = image_item.get("b64_json")
        if b64_json:
            decoded_content = self._decode_base64_image(b64_json)
            image_width, image_height = self._detect_image_size(decoded_content)
            logger.debug(
                "Grok 返回 base64 图片: bytes=%s mime=image/jpeg size=%sx%s",
                len(decoded_content),
                image_width,
                image_height,
            )
            return {
                "content": decoded_content,
                "mime_type": "image/jpeg",
                "file_extension": "jpg",
                "revised_prompt": revised_prompt,
                "provider": "grok",
                "model": model,
                "image_width": image_width,
                "image_height": image_height,
            }

        image_url = image_item.get("url")
        if image_url:
            logger.debug("Grok 返回图片 URL，开始下载: %s", image_url)
            try:
                async with httpx.AsyncClient(timeout=180.0) as client:
                    image_response = await client.get(image_url)

                logger.debug(
                    "Grok 图片下载响应: status=%s content_type=%s content_length=%s",
                    image_response.status_code,
                    image_response.headers.get("content-type"),
                    image_response.headers.get("content-length"),
                )
                image_response.raise_for_status()
            except httpx.HTTPStatusError as exc:
                logger.error(
                    "Grok 图片下载 HTTP 错误: status=%s response=%s",
                    exc.response.status_code if exc.response else None,
                    exc.response.text[:2000] if exc.response is not None else None,
                )
                raise
            except Exception:
                logger.error("Grok 图片下载异常", exc_info=True)
                raise

            content_type = image_response.headers.get("content-type", "image/jpeg")
            file_extension = self._guess_extension(content_type=content_type, image_url=image_url)
            image_width, image_height = self._detect_image_size(image_response.content)
            logger.debug(
                "Grok 图片下载完成: bytes=%s extension=%s size=%sx%s",
                len(image_response.content),
                file_extension,
                image_width,
                image_height,
            )
            return {
                "content": image_response.content,
                "mime_type": content_type,
                "file_extension": file_extension,
                "revised_prompt": revised_prompt,
                "provider": "grok",
                "model": model,
                "image_width": image_width,
                "image_height": image_height,
            }

        logger.error("Grok 返回内容中既没有 b64_json，也没有 url: %s", data)
        raise ValueError("Grok 未返回可用的图片数据")

    @staticmethod
    def _to_public_result(result: dict[str, Any]) -> CoverGenerationResult:
        return {
            "content": result["content"],
            "mime_type": result["mime_type"],
            "file_extension": result["file_extension"],
            "revised_prompt": result.get("revised_prompt"),
            "provider": result["provider"],
            "model": result["model"],
        }

    @staticmethod
    def _detect_image_size(content: bytes) -> tuple[int, int]:
        if len(content) >= 24 and content[:8] == b"\x89PNG\r\n\x1a\n":
            width, height = struct.unpack(">II", content[16:24])
            return int(width), int(height)

        if len(content) >= 2 and content[:2] == b"\xff\xd8":
            index = 2
            content_length = len(content)
            while index < content_length - 1:
                if content[index] != 0xFF:
                    index += 1
                    continue
                marker = content[index + 1]
                index += 2
                if marker in (0xD8, 0xD9):
                    continue
                if index + 2 > content_length:
                    break
                segment_length = struct.unpack(">H", content[index:index + 2])[0]
                if segment_length < 2 or index + segment_length > content_length:
                    break
                if marker in {
                    0xC0, 0xC1, 0xC2, 0xC3,
                    0xC5, 0xC6, 0xC7,
                    0xC9, 0xCA, 0xCB,
                    0xCD, 0xCE, 0xCF,
                }:
                    if index + 7 <= content_length:
                        height, width = struct.unpack(">HH", content[index + 3:index + 7])
                        return int(width), int(height)
                    break
                index += segment_length

        return 0, 0

    @staticmethod
    def _decode_base64_image(value: str) -> bytes:
        if value.startswith("data:") and "," in value:
            value = value.split(",", 1)[1]
        return base64.b64decode(value)

    @staticmethod
    def _adapt_prompt(*, prompt: str, width: int, height: int) -> str:
        cleaned_prompt = " ".join((prompt or "").split())
        return (
            f"{cleaned_prompt} "
            f"Use a {width}x{height} vertical composition."
        ).strip()

    @staticmethod
    def _get_aspect_ratio(*, width: int, height: int) -> str:
        if width <= 0 or height <= 0:
            return "2:3"
        if width * 3 == height * 2:
            return "2:3"
        return f"{width}:{height}"

    @staticmethod
    def _get_resolution(*, width: int, height: int) -> str:
        longest_edge = max(width, height)
        if longest_edge >= 1536:
            return "2k"
        return "1k"

    @staticmethod
    def _guess_extension(*, content_type: str, image_url: str) -> str:
        lowered_content_type = (content_type or "").lower()
        lowered_url = (image_url or "").lower()
        if "png" in lowered_content_type or lowered_url.endswith(".png"):
            return "png"
        if "webp" in lowered_content_type or lowered_url.endswith(".webp"):
            return "webp"
        return "jpg"
