"""世界设定事实（issue #242 P1）：构建、校验、合并与渲染。

世界设定有四份文本列（提示词/UI 兼容）与一份事实列 world_facts（可溯源）。
事实形状：
    {"field": "world_rules", "text": "原子事实", "evidence": [{"chapter": 1, "quote": "逐字引文"}], "source": "original"}

本模块只处理数据结构，不触碰数据库会话；导入/同步服务负责读写。
"""
from __future__ import annotations

import json
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

NOT_MENTIONED = "原文未提及"
QUOTE_MIN_LEN = 8
QUOTE_MAX_LEN = 60
FACT_TEXT_MAX_LEN = 400
RENDER_SEPARATOR = "；"


def build_world_facts(
    world_data: dict[str, Any], ai_written: dict[str, str]
) -> list[dict[str, Any]]:
    """把抽取结果整理为可溯源事实（只收 AI 真正写入的字段）。"""
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
        facts.append(
            {"field": field, "text": text.strip(), "evidence": evidence, "source": "original"}
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
        rendered[field] = RENDER_SEPARATOR.join(unique)
    return rendered
