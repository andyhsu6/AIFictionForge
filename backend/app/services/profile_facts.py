"""角色档案原子事实校验（issue #190 / P1 纯函数层）。

本模块只做两件事，全部是纯函数、无 IO、无日志、无模型调用：

1. ``validate_profile_facts``：校验模型给出的"原子事实 + 原文引用"，只判断
   **可判定真假**的条件——引文在批次正文里字面存在、字段合法、claim 不含
   叙述残留、谓词与字段匹配。
2. ``render_profile``：把通过校验的事实拼成三个自由文本字段。

第一版：字面存在性 + 字段谓词，谓词层是兜底、不是主保证。
真正的保证来自"引文必须在原文里字面存在"这一条；谓词（关系断言句式、
事件短语）只是挡住最明显的错位，**不判断纯语义错位**（门派归属写进外貌
这类），也不能替代模型按字段理解语义。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

FACT_FIELDS = ("personality", "background", "appearance")  # current_state 由章节分析链路拥有，不在范围内
MIN_QUOTE_CHARS = 8
MAX_CLAIM_CHARS = 60
MAX_FACTS_PER_FIELD = 6

# claim 里出现这些字符说明模型还在写句子/多句叙述，不是原子事实
_BANNED_CLAIM_CHARS = "。！？；!?;\n\r"

# 关系断言句式：说明把"关系"当成"性格"（如「狄宏伟之妻」「与丈夫并称双刀」）。
# 只匹配断言结构，不匹配称谓词的任意出现——「视下属如兄弟」是性格，不是关系。
_RELATION_ASSERTION_RE = re.compile(
    r"(之[妻夫子女父母兄姐弟妹]"
    r"|的(丈夫|妻子|夫君|娘子|父亲|母亲|儿子|女儿|师父|师傅|师兄|师姐|师弟|师妹)"
    r"|(?:与|和|跟)(?:丈夫|妻子|夫君|娘子|父亲|母亲|兄弟|姐妹|师父|师傅|师兄|师姐|师弟|师妹))"
)

# 事件短语：出现在 personality 里说明把"发生过的事"当成了"性格"。
# 刻意用短语而非单字：单字「杀」会误杀「杀伐果断」「冷面杀手」，
# 单字「婚」会误杀「婚姻观」，单字「生」会误杀「生性」。
_PERSONALITY_EVENT_PHRASES = (
    "死于",
    "而死",
    "被杀",
    "杀死",
    "杀害",
    "战死",
    "战败",
    "身亡",
    "去世",
    "自尽",
    "获救",
    "被俘",
    "即位",
    "登基",
    "怀孕",
    "出生",
    "嫁人",
    "娶妻",
    "成婚",
    "结婚",
    "复仇",
    "报仇",
)

# 事件短语：出现在 appearance 里说明把"行为/结局"当成了"外貌"。
# 同样不用单字（「战袍」「杀气」都是合法外貌/气质描述）。
_APPEARANCE_EVENT_PHRASES = (
    "死于",
    "被杀",
    "战死",
    "自尽",
    "被俘",
    "杀敌",
    "挥刀",
    "救下",
    "嫁人",
    "娶妻",
)

# 与 str 的 \s 保持一致，额外显式列出全角空格，避免不同实现差异
_WHITESPACE_RE = re.compile(r"[\s\u3000]+")

# 渲染时剥掉的句末标点
_TRAILING_PUNCT = "。！？；"

_STAT_KEYS = (
    "facts_in",
    "facts_accepted",
    "field_invalid",
    "claim_invalid",
    "quote_invalid",
    "quote_not_found",
    "chapter_invalid",
    "inferred_not_allowed",
    "chapter_corrected",
)


@dataclass
class ProfileFactResult:
    """一次校验的结果。"""

    entity: str
    facts: list[dict]
    stats: dict[str, int]


def _new_stats() -> dict[str, int]:
    stats = {key: 0 for key in _STAT_KEYS}
    stats["payload_invalid"] = 0
    return stats


def normalize_quote(text: str) -> str:
    """去掉全部空白字符（含换行、制表符、全角空格），得到用于字面比对的串。

    这里刻意**不归一标点**：本模块唯一的硬校验是"引文在原文中字面存在"，
    一旦把 `，`/`。` 之类统一，模型编造的近似句子就会通过，硬校验退化成
    启发式猜测，反而更难发现伪造引用。空白属于排版差异，才允许折叠。
    """
    if not isinstance(text, str):
        return ""
    return _WHITESPACE_RE.sub("", text)


def normalize_chapters(chapters_by_number: dict[int, str]) -> dict[int, str]:
    """把批次正文一次性归一化，供多处引文定位复用（避免逐条事实重复跑正则）。"""
    return {
        number: normalize_quote(content or "")
        for number, content in (chapters_by_number or {}).items()
    }


def _locate(needle: str, normalized_chapters: dict[int, str], claimed: int) -> int | None:
    """在已归一化的批次正文里定位引文；先查 claimed，再按章距升序扫其余章。"""
    if not needle:
        return None
    if claimed in normalized_chapters and needle in normalized_chapters[claimed]:
        return claimed
    others = sorted(
        (number for number in normalized_chapters if number != claimed),
        key=lambda number: (abs(number - claimed), number),
    )
    for number in others:
        if needle in normalized_chapters[number]:
            return number
    return None


def locate_quote(quote: str, chapters_by_number: dict[int, str], claimed: int) -> int | None:
    """在批次正文中定位引文，返回真实章号；找不到返回 None。

    先查 ``claimed`` 章；未命中则按"与 claimed 的距离"升序查其余章——这样
    模型记错章号时能纠正而不是把真实引用丢掉。
    """
    return _locate(normalize_quote(quote), normalize_chapters(chapters_by_number), claimed)


def _is_positive_int(value: Any) -> bool:
    # bool 是 int 的子类，但 True/False 不是合法章号
    return isinstance(value, int) and not isinstance(value, bool) and value > 0


def _claim_has_banned_text(claim: str) -> bool:
    if any(char in claim for char in _BANNED_CLAIM_CHARS):
        return True
    # 省略号意味着叙述残留（"他……"），不是原子事实
    return "…" in claim


def _claim_matches_field(field: str, claim: str) -> bool:
    """字段谓词兜底：claim 是否与声明字段相容。

    只挡"形状上明显属于另一类"的内容（关系断言、事件短语），
    不判断"门派归属写进外貌"这类纯语义错位——那类问题确定性规则挡不住，
    由"引文必须字面存在"与提示词约束负责，不在这一层假装能解决。
    """
    if field == "personality":
        if _RELATION_ASSERTION_RE.search(claim):
            return False
        if any(phrase in claim for phrase in _PERSONALITY_EVENT_PHRASES):
            return False
    elif field == "appearance":
        if any(phrase in claim for phrase in _APPEARANCE_EVENT_PHRASES):
            return False
    return True


def validate_profile_facts(
    payload: Any,
    chapters_by_number: dict[int, str],
    *,
    kind: str = "character",
) -> ProfileFactResult:
    """逐条校验实体事实，丢弃不合格的条目并计数，不抛异常。

    ``kind`` 用于区分角色与组织，本阶段的字段集合与谓词规则相同，参数先留位。
    每条事实任一条件不满足即丢弃并计入对应统计；只有全部通过才进入 facts。
    """
    stats = _new_stats()

    if not isinstance(payload, dict):
        stats["payload_invalid"] = 1
        return ProfileFactResult(entity="", facts=[], stats=stats)

    raw_name = payload.get("name")
    entity = raw_name if isinstance(raw_name, str) else ""

    raw_facts = payload.get("facts")
    if not isinstance(raw_facts, list):
        stats["payload_invalid"] = 1
        return ProfileFactResult(entity=entity, facts=[], stats=stats)

    stats["facts_in"] = len(raw_facts)
    normalized_chapters = normalize_chapters(chapters_by_number)
    accepted: list[dict] = []

    for raw in raw_facts:
        # 非对象条目没有 field 可判，归入 field_invalid
        if not isinstance(raw, dict):
            stats["field_invalid"] += 1
            continue

        field = raw.get("field")
        if field not in FACT_FIELDS:
            stats["field_invalid"] += 1
            continue

        claim = raw.get("claim")
        if (
            not isinstance(claim, str)
            or not claim.strip()
            or _claim_has_banned_text(claim)
            or len(claim) > MAX_CLAIM_CHARS
        ):
            stats["claim_invalid"] += 1
            continue

        quote = raw.get("quote")
        if not isinstance(quote, str) or len(normalize_quote(quote)) < MIN_QUOTE_CHARS:
            stats["quote_invalid"] += 1
            continue

        claimed = raw.get("chapter_number")
        chapter_ok = _is_positive_int(claimed)
        # 章号非法时仍允许定位（定位失败优先计 quote_not_found），用 0 作为参考点
        located = _locate(normalize_quote(quote), normalized_chapters, claimed if chapter_ok else 0)
        if located is None:
            stats["quote_not_found"] += 1
            continue
        if not chapter_ok:
            stats["chapter_invalid"] += 1
            continue

        if not _claim_matches_field(field, claim):
            stats["claim_invalid"] += 1
            continue

        inferred = raw.get("inferred") is True
        if inferred and field != "personality":
            # 背景/外貌必须是原文直述，不接受推断
            stats["inferred_not_allowed"] += 1
            continue

        if located != claimed:
            stats["chapter_corrected"] += 1

        stats["facts_accepted"] += 1
        accepted.append(
            {
                "field": field,
                "claim": claim,
                "quote": quote,
                "chapter_number": located,
                "inferred": inferred,
            }
        )

    return ProfileFactResult(entity=entity, facts=accepted, stats=stats)


def render_profile(facts: list[dict], *, kind: str = "character") -> dict[str, str]:
    """把通过校验的事实渲染成 ``personality``/``background``/``appearance`` 文本。

    - 同一字段内按 ``normalize_quote(claim)`` 去重，先到先留；
    - 每字段最多 ``MAX_FACTS_PER_FIELD`` 条，用 `、` 连接；
    - 渲染前再剥一次句末标点（`。！？；`），仍为空则跳过该字段；
    - 没有事实返回 ``{}``。

    ``kind`` 目前不影响结果：组织同样有 appearance 字段、字段集合一致，
    组织专属的谓词差异在 validate 阶段按 kind 留位处理。
    """
    buckets: dict[str, list[str]] = {field: [] for field in FACT_FIELDS}
    seen: dict[str, set[str]] = {field: set() for field in FACT_FIELDS}

    for fact in facts or []:
        if not isinstance(fact, dict):
            continue
        field = fact.get("field")
        if field not in FACT_FIELDS:
            continue
        claim = fact.get("claim")
        if not isinstance(claim, str):
            continue

        key = normalize_quote(claim)
        if not key or key in seen[field]:
            continue
        if len(buckets[field]) >= MAX_FACTS_PER_FIELD:
            continue
        seen[field].add(key)

        text = claim.strip().rstrip(_TRAILING_PUNCT).strip()
        if not text:
            continue
        buckets[field].append(text)

    return {field: "、".join(items) for field, items in buckets.items() if items}
