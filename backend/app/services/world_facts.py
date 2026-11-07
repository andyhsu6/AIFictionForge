"""世界设定事实（issue #242 P1）：构建、校验、合并与渲染。

世界设定有四份文本列（提示词/UI 兼容）与一份事实列 world_facts（可溯源）。
事实形状：
    {"field": "world_rules", "text": "原子事实", "evidence": [{"chapter": 1, "quote": "逐字引文"}], "source": "original"}

本模块只处理数据结构，不触碰数据库会话；导入/同步服务负责读写。
"""
from __future__ import annotations

import json
import re
from typing import Any, Optional

# 世界设定四字段（与 projects 表列名一致）
WORLD_FACT_FIELDS = (
    "world_time_period",
    "world_location",
    "world_atmosphere",
    "world_rules",
)

# 抽取模板的 evidence 键与项目字段名不同
EVIDENCE_KEYS = {
    "world_time_period": "time_period",
    "world_location": "location",
    "world_atmosphere": "atmosphere",
    "world_rules": "rules",
}

QUOTE_MIN_LEN = 8
QUOTE_MAX_LEN = 60
FACT_TEXT_MAX_LEN = 400
RENDER_SEPARATOR = "；"
SENTENCE_ENDINGS = ("。", "！", "？", "；", "…", "!", "?", ";")
# 按句末标点拆分（保留标点），用于把整段抽取文归一化为原子 claim
_CLAIM_SPLIT_RE = re.compile(r"(?<=[。！？；!?;])\s*")


def _split_claim_text(text: str) -> list[str]:
    """按句末标点把整段文本拆成原子 claim；保留标点以便无损回渲染。"""
    parts = [part.strip() for part in _CLAIM_SPLIT_RE.split(text) if part.strip()]
    return parts or [text.strip()]


def build_world_facts(
    world_data: dict[str, Any], ai_written: dict[str, str]
) -> list[dict[str, Any]]:
    """把抽取结果整理为可溯源事实（只收 AI 真正写入的字段）。

    每个字段按句末标点拆成原子 claim（issue #251）：导入 facts 与同步 claim
    形状一致后，才能用 facts 反向渲染文本而不产生重复。标点保留在 claim 文本里，
    渲染时按原分隔符可无损还原；quote 落在句内时挂到该句，否则挂到整字段。
    """
    raw_evidence = world_data.get("evidence")
    evidence_map = raw_evidence if isinstance(raw_evidence, dict) else {}
    facts: list[dict[str, Any]] = []
    for field, text in ai_written.items():
        if field not in WORLD_FACT_FIELDS or not isinstance(text, str) or not text.strip():
            continue
        key = EVIDENCE_KEYS.get(field)
        items = evidence_map.get(key) if key else None
        evidence: list[dict[str, Any]] = []
        if isinstance(items, list):
            for item in items:
                if not isinstance(item, dict):
                    continue
                chapter = item.get("chapter")
                quote = item.get("quote")
                if isinstance(chapter, int) and isinstance(quote, str) and quote.strip():
                    evidence.append({"chapter": chapter, "quote": quote.strip()})
        for sentence in _split_claim_text(text.strip()):
            matched = [entry for entry in evidence if entry["quote"] in sentence]
            facts.append(
                {
                    "field": field,
                    "text": sentence,
                    "evidence": matched or evidence,
                    "source": "original",
                }
            )
    return facts


def load_world_facts(raw: Optional[str]) -> list[dict[str, Any]]:
    """读取已存的事实数组；脏数据/旧格式一律视为空。"""
    if not raw:
        return []
    try:
        data = json.loads(raw)
    except (TypeError, ValueError):
        return []
    if not isinstance(data, list):
        return []
    return [item for item in data if isinstance(item, dict)]


def merge_world_facts(
    existing: list[dict[str, Any]], new: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    """按 (field, text) 合并事实：同一条事实追加新证据，不整条丢弃。"""
    merged: list[dict[str, Any]] = []
    index: dict[tuple[Any, Any], int] = {}
    for item in existing:
        fact = dict(item)
        fact["evidence"] = [
            dict(entry) for entry in item.get("evidence") or [] if isinstance(entry, dict)
        ]
        index[(fact.get("field"), fact.get("text"))] = len(merged)
        merged.append(fact)
    for item in new:
        fact = dict(item)
        fact["evidence"] = [
            dict(entry) for entry in item.get("evidence") or [] if isinstance(entry, dict)
        ]
        key = (fact.get("field"), fact.get("text"))
        if key in index:
            target = merged[index[key]]
            seen = {(entry.get("chapter"), entry.get("quote")) for entry in target["evidence"]}
            for entry in fact["evidence"]:
                entry_key = (entry.get("chapter"), entry.get("quote"))
                if entry_key in seen:
                    continue
                target["evidence"].append(entry)
                seen.add(entry_key)
            continue
        index[key] = len(merged)
        merged.append(fact)
    return merged


def validate_world_facts_claims(
    claims: Any, chapter_texts: dict[int, str]
) -> tuple[list[dict[str, Any]], int]:
    """校验同步返回的 claims，返回 (合法事实, 被拒条数)。

    事实必须带至少一条可回查证据：章节号在给定范围内，引文逐字出现在该章
    正文里。非法 claim 直接丢弃（不阻塞其余批次写入），由调用方统计。

    引文长度 8-60 与同步提示词规则 5 联动：模型返回超长逐字引文会被判非法，
    这是预期行为（防止整段复制），不是 bug。
    """
    if not isinstance(claims, list):
        return [], 0
    valid: list[dict[str, Any]] = []
    rejected = 0
    for claim in claims:
        if not isinstance(claim, dict):
            rejected += 1
            continue
        field = claim.get("field")
        text = claim.get("text")
        if (
            field not in WORLD_FACT_FIELDS
            or not isinstance(text, str)
            or not text.strip()
            or len(text.strip()) > FACT_TEXT_MAX_LEN
        ):
            rejected += 1
            continue
        raw_evidence = claim.get("evidence")
        evidence: list[dict[str, Any]] = []
        if isinstance(raw_evidence, list):
            for item in raw_evidence:
                if not isinstance(item, dict):
                    continue
                chapter = item.get("chapter")
                quote = item.get("quote")
                if not isinstance(chapter, int) or not isinstance(quote, str):
                    continue
                quote_text = quote.strip()
                if not (QUOTE_MIN_LEN <= len(quote_text) <= QUOTE_MAX_LEN):
                    continue
                content = chapter_texts.get(chapter)
                if not content or quote_text not in content:
                    continue
                if {"chapter": chapter, "quote": quote_text} not in evidence:
                    evidence.append({"chapter": chapter, "quote": quote_text})
        if not evidence:
            rejected += 1
            continue
        valid.append(
            {
                "field": field,
                "text": text.strip(),
                "evidence": evidence,
                "source": "original",
            }
        )
    return valid, rejected


def render_world_facts(
    facts: list[dict[str, Any]], fields: tuple[str, ...] = WORLD_FACT_FIELDS
) -> dict[str, str]:
    """把事实按字段渲染成文本（用分隔符连接，保持事实顺序）。"""
    grouped: dict[str, list[str]] = {}
    for fact in facts:
        if not isinstance(fact, dict):
            continue
        field = fact.get("field")
        text = fact.get("text")
        if field in fields and isinstance(text, str) and text.strip():
            grouped.setdefault(field, []).append(text.strip())
    rendered: dict[str, str] = {}
    for field, texts in grouped.items():
        seen: set[str] = set()
        unique = [text for text in texts if not (text in seen or seen.add(text))]
        parts: list[str] = []
        for text in unique:
            # 上一句自带落句标点时视为完整句，直接拼接（导入文可无损还原）；
            # 否则用分隔符连接，避免原子 claim 粘连成病句。
            if parts and not parts[-1].endswith(SENTENCE_ENDINGS):
                parts.append(RENDER_SEPARATOR)
            parts.append(text)
        rendered[field] = "".join(parts)
    return rendered
