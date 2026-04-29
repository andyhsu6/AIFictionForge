"""封面图片 Provider 抽象基类"""
from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Optional, TypedDict


class CoverGenerationResult(TypedDict):
    """封面生成结果"""

    content: bytes
    mime_type: str
    file_extension: str
    revised_prompt: Optional[str]
    provider: str
    model: str


class BaseCoverProvider(ABC):
    """封面图片 Provider 抽象基类"""

    @abstractmethod
    async def generate_cover(
        self,
        *,
        prompt: str,
        model: str,
        width: int,
        height: int,
    ) -> CoverGenerationResult:
        """生成封面图片"""
        raise NotImplementedError
