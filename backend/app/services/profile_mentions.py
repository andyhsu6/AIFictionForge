"""实体提及判定：``name + aliases`` 的 token 层（issue #190 后续）。

生产同步链路（``character_profile_sync_service._character_names`` / ``_mentioned_in``）
判断「实体是否出现在正文里」时用的是 ``name + aliases``；两个离线脚本（
``sweep_profiles_readonly`` / ``backfill_profiles``）此前只按 ``name`` 精确子串匹配，
于是正文只用简称/称谓的实体被误判成 ``not_in_text``。

本模块只做三件事，全部是纯函数、无 IO、无 DB、无模型调用：

1. ``parse_aliases`` / ``character_tokens``：把 ``characters.aliases``（JSON 数组串）
   与 name 合成候选 token；长度 < ``MIN_TOKEN_LEN`` 的 token 丢弃——单字 token
   在中文正文里几乎必然误命中（如「沈」之于「沈浪」）。
2. ``unique_tokens_by_entity``：按**项目内唯一性**过滤 token。别名救回的代价是
   误判风险：某个 token 若被多个实体共用、或与别的实体的 token 互为子串，
   就不能用来判定归属。唯一性拿不到时宁可退回只用 name（现状），
   保证改造不会比现状更差。
3. ``count_mentions`` / ``mention_tokens``：按 token 统计正文命中，供两个离线脚本
   复用同一套口径。
"""

from __future__ import annotations

import json
from collections import defaultdict
from typing import Any

# 单字 token 在中文正文里误命中率过高（「李」会命中所有姓李的人），不参与匹配
MIN_TOKEN_LEN = 2

# 半截 JSON（如 "[张三,李四"）不能当逗号分隔串解析，否则会把 "[张三" 当成别名
_JSON_WRAPPER_CHARS = ("[", "{")


def _clean_tokens(items: Any) -> list[str]:
    """只保留非空字符串元素（strip 后）；非字符串元素无法当名称用，丢弃。"""
    if not isinstance(items, (list, tuple)):
        return []
    tokens: list[str] = []
    for item in items:
        if not isinstance(item, str):
            continue
        text = item.strip()
        if text:
            tokens.append(text)
    return tokens


def parse_aliases(raw: Any) -> list[str]:
    """把 ``characters.aliases`` 解析成别名列表，坏值一律当"没有别名"。

    列里存的是 JSON 数组串，但历史数据/手工写库可能是逗号分隔串、已经是 list，
    或干脆是坏 JSON（列宽 500 曾经截断过）。解析失败时返回 ``[]`` 而不是抛异常：
    别名是救回手段，不是必需数据，坏值不应让离线脚本整体失败。
    """
    if raw is None:
        return []
    if isinstance(raw, (list, tuple)):
        return _clean_tokens(raw)
    if not isinstance(raw, str):
        return []

    text = raw.strip()
    if not text:
        return []

    try:
        value = json.loads(text)
    except (TypeError, ValueError):
        # 不是合法 JSON：可能是逗号分隔串；像半截 JSON 的直接当坏值
        if text.startswith(_JSON_WRAPPER_CHARS):
            return []
        return _clean_tokens(text.split(","))

    if isinstance(value, str):
        # JSON 字符串（"张三,李四"）按逗号分隔串处理
        return _clean_tokens(value.split(","))
    return _clean_tokens(value)


def character_tokens(entity: Any) -> list[str]:
    """实体的候选 token：name 在最前，其后是 aliases；去重保序，丢弃过短 token。

    ``entity`` 只要有 ``name`` / ``aliases`` 属性即可（SQLAlchemy Row、ORM 对象、
    dataclass 都行），本模块不依赖具体类型。
    """
    name = getattr(entity, "name", None)
    raw_tokens: list[str] = [name if isinstance(name, str) else ""]
    raw_tokens.extend(parse_aliases(getattr(entity, "aliases", None)))

    tokens: list[str] = []
    seen: set[str] = set()
    for raw in raw_tokens:
        text = raw.strip()
        if len(text) < MIN_TOKEN_LEN or text in seen:
            continue
        seen.add(text)
        tokens.append(text)
    return tokens


def unique_tokens_by_entity(entities: Any) -> dict[str, list[str]]:
    """按项目内唯一性过滤 token，返回 ``{entity.id: [token, ...]}``。

    三条排除规则（任一条命中即排除该 token）：

    1. token 不属于该实体（``character_tokens`` 里没有）；
    2. 别的实体也声明了同名 token（共用 token 无法判定归属）；
    3. 别的实体的 token 与它互为子串（``u in t or t in u``）——「沈」与「沈浪」
       同时存在时，``沈`` 的命中可能来自 ``沈浪``。

    同实体内部的 name 与 alias 互相包含**不算**冲突：``大乾朝廷`` 与别名
    ``朝廷`` 都描述同一个实体，两个都该保留。

    某实体所有 token 都被排除时值为 ``[]``，调用方用 ``mention_tokens`` 退回只用 name。
    """

    per_entity: dict[str, list[str]] = {}
    for entity in entities or []:
        per_entity[getattr(entity, "id", None)] = character_tokens(entity)

    declared: dict[str, set[Any]] = defaultdict(set)
    for entity_id, tokens in per_entity.items():
        for token in tokens:
            declared[token].add(entity_id)

    result: dict[str, list[str]] = {}
    for entity_id, tokens in per_entity.items():
        kept: list[str] = []
        for token in tokens:
            if len(declared[token]) > 1:
                continue
            conflicted = False
            for other_id, other_tokens in per_entity.items():
                if other_id == entity_id:
                    continue
                for other in other_tokens:
                    if other != token and (other in token or token in other):
                        conflicted = True
                        break
                if conflicted:
                    break
            if not conflicted:
                kept.append(token)
        result[entity_id] = kept
    return result


def count_mentions(text: str, tokens: list[str]) -> int:
    """各 token 在 ``text`` 里出现次数之和（出现次数口径，不是出现与否）。"""
    if not text or not tokens:
        return 0
    return sum(text.count(token) for token in tokens if token)


def mention_tokens(entity: Any, unique_map: dict[str, list[str]]) -> list[str]:
    """取该实体可用的 token；唯一性表拿不到（或全部被排除）时退回只用 name。

    回退是刻意设计的：现状就是只用 name 匹配，改造不能让它更差。
    """
    tokens = unique_map.get(getattr(entity, "id", None)) or [
        (getattr(entity, "name", None) or "").strip()
    ]
    return [token for token in tokens if token]
