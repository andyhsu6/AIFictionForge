"""Helpers for reasoning-model responses (DeepSeek / MiniMax / think tags)."""
from __future__ import annotations

import re
from typing import Any, Dict, Optional, Tuple

_THINK_BLOCK_RE = re.compile(
    r"<(think|thinking|reasoning|reason)>\s*.*?\s*</\1>",
    flags=re.IGNORECASE | re.DOTALL,
)


def strip_think_tags(text: Optional[str]) -> str:
    """Remove model thought blocks so JSON parsers and chapter text stay clean."""
    if not text:
        return text or ""
    cleaned = _THINK_BLOCK_RE.sub("", text)
    return cleaned.strip() if cleaned != text else cleaned


def extract_reasoning_text(payload: Dict[str, Any]) -> str:
    """Pick reasoning text from OpenAI-compatible delta/message objects."""
    if not isinstance(payload, dict):
        return ""
    for key in ("reasoning_content", "reasoning", "reasoning_details"):
        value = payload.get(key)
        if isinstance(value, str) and value:
            return value
        if isinstance(value, list):
            parts = []
            for item in value:
                if isinstance(item, str) and item:
                    parts.append(item)
                elif isinstance(item, dict):
                    text = item.get("text") or item.get("content") or ""
                    if text:
                        parts.append(str(text))
            if parts:
                return "".join(parts)
    return ""


def split_content_and_reasoning(payload: Dict[str, Any]) -> Tuple[str, str]:
    """Return (visible content, reasoning) without mixing the two."""
    if not isinstance(payload, dict):
        return "", ""
    content = payload.get("content") or ""
    if not isinstance(content, str):
        content = str(content)
    return content, extract_reasoning_text(payload)


def uses_minimax_api(model: Optional[str], base_url: Optional[str]) -> bool:
    blob = f"{model or ''} {base_url or ''}".lower()
    return "minimax" in blob


def sse_data_payload(line: str) -> Optional[str]:
    """Extract JSON payload from `data: {...}` or `data:{...}` SSE lines."""
    if not line:
        return None
    stripped = line.strip()
    if not stripped.startswith("data:"):
        return None
    return stripped[5:].strip()
