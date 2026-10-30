"""角色档案原子事实校验（issue #190 / P1 纯函数层）。

本模块只做三件事，全部是纯函数、无 IO、无日志、无模型调用：

1. ``parse_profile_payload``：判定一份返回是"原子事实格式"还是旧的长文本格式
   （旧格式回退路径，兼容旧提示词/旧项目/同步链路）。
2. ``validate_profile_facts``：校验模型给出的"原子事实 + 原文引用"，只判断
   **可判定真假**的条件——引文在批次正文里字面存在、字段合法、claim 不含
   叙述残留、谓词与字段匹配。
3. ``render_profile``：把通过校验的事实拼成三个自由文本字段。

第一版：字面存在性 + 字段谓词，谓词层是兜底、不是主保证。
真正的保证来自"引文必须在原文里字面存在"这一条；谓词（关系断言句式、
事件短语）只是挡住最明显的错位，**不判断纯语义错位**（门派归属写进外貌
这类），也不能替代模型按字段理解语义。
"""

from __future__ import annotations

import json
import os
import re
import unicodedata
from dataclasses import dataclass
from typing import Any

# issue #190：档案改走「原子事实 + 原文引用」的总开关，所有写入路径共用这一处。
# 线上回退旧提示词（三段长文本 + 启发式门禁）：PROFILE_FACTS_PROMPT=0。
PROFILE_FACTS_PROMPT_ENABLED = os.getenv("PROFILE_FACTS_PROMPT", "1").strip().lower() not in {
    "0",
    "false",
    "off",
    "no",
}

FACT_FIELDS = ("personality", "background", "appearance")  # current_state 由章节分析链路拥有，不在范围内
MIN_QUOTE_CHARS = 8
MAX_CLAIM_CHARS = 60
MAX_FACTS_PER_FIELD = 6
# 按字段的渲染条数上限：background 承载身份/师承/经历，信息密度最高；
# appearance 靠 2-4 条覆盖体态/容貌/衣着，再多就是同义堆叠。
FACT_FIELD_LIMITS = {"personality": 6, "background": 8, "appearance": 4}

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

# 嵌套判定时视为措辞差异的结构助词
_STRUCTURAL_PARTICLES = "的地得"

# 判定「包含关系是不是同一特质」时用的极性标记（见 _folded_contains）。
# 只做保守判定：长串里**通篇**出现任一标记就不折叠。中文的否定标记与中心语
# 之间距离是开放的（副词、能愿动词、后置补语都会插进来，「不」还能出现在中心语
# 之后），按「紧邻前后缀」枚举追不完，所以改成通篇扫描。
_NEGATION_CHARS = "不没无非未勿莫别弗"
# 反问/疑问同样会把「X」写成「X 过吗」「岂能 X」「焉能 X」
_RHETORICAL_CHARS = "岂焉何"
_QUESTION_CHARS = "吗呢"
_QUESTION_MARKS = ("？", "?")

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


def normalize_claim_form(text: str) -> str:
    """claim 的「措辞归一」串，只用于同一字段内判断两条 claim 是否互相嵌套。

    去空白、去标点、去结构助词（的/地/得）——这些只是同一特质的措辞差异：
    「甲庄园的管家」与「甲庄园管家」、「甲之妻、乙之母」与「乙之母」
    说的是同一件事。

    与 ``normalize_quote`` 刻意分开：后者服务于「引文必须在原文里字面存在」这条
    硬校验，归一得越多越容易放过伪造引用；本函数只回答「是不是同一特质的再次
    表述」，**不得**外泄到引文校验路径。
    """
    if not isinstance(text, str):
        return ""
    return "".join(
        char
        for char in text
        if not char.isspace()
        and not unicodedata.category(char).startswith("P")
        and char not in _STRUCTURAL_PARTICLES
    )


def _preferred_text(current: str, candidate: str) -> str:
    """归一串相同的两条里挑哪条做渲染文本：信息量优先，其次与顺序无关。"""
    def key(text: str) -> tuple[int, int, str]:
        return (len(text.replace("、", "")), -text.count("、"), text)

    return candidate if key(candidate) > key(current) else current


def _folded_contains(outer: str, inner: str) -> bool:
    """``inner`` 是否可作为 ``outer`` 的重复表述被吸收。

    字面包含不足以判定重复：

    1. **否定与反问**：「克制」被「不克制」「克制不了」「并不十分克制」包含，
       「耐心」被「耐心全无」「不太耐心」包含，说的都是相反或另一件事；按字面
       吸收会静默删掉较短的一条，让档案断言反转。中文否定标记与中心语的距离是
       开放的（`不克制` / `克制不了` / `并不十分克制` / `未必克制`），按紧邻
       前后缀枚举追不完，所以改成**通篇扫描**：长串里出现任一否定语素或反问
       标记就不折叠。代价是少数含这些字的正常短语不再合并（宁可少合并，不要写反）。
    2. **纯 ASCII 不折叠**：英文没有可依赖的词边界（去标点去空格后更没有了），
       「care」在「careless」里、「friend」在「boyfriend」里，都属于不同词而非
       措辞差异。被吸收的一方是 ASCII 即豁免——混排 claim 里同样没有词边界。
    3. **单字不折叠**：一个字的表述几乎总是恰好在更长的表述里出现，吸收它只会丢信息。
    """
    if inner == outer or len(inner) < 2:
        return False
    if inner.isascii():
        return False
    if any(char in outer for char in _NEGATION_CHARS + _RHETORICAL_CHARS + _QUESTION_CHARS):
        return False
    if any(mark in outer for mark in _QUESTION_MARKS):
        return False
    return inner in outer


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

    同一字段内做两级去重（都是确定性纯函数，无 IO、无模型调用）：

    1. **精确去重**：按 ``normalize_quote(claim)``，先到先留；
    2. **嵌套去重**（issue #201 第 2 项）：按 ``normalize_claim_form``（去空白、
       标点、结构助词）比较。归一串完全相同（只差标点或「的」）合并为一条、保留
       原文较长者；若甲的归一串是乙的归一串的**真子串**，丢弃甲、保留乙——同存
       只是同义堆叠。否定语境（「甲」与「不甲」）、纯 ASCII 形式、单字表述一律
       不折叠，理由见 ``_folded_contains``。三条以上互相嵌套时保留最长者，
       长度相等但内容不同的串不会互为真子串，因此不会互相吞并。

    这是确定性的字面比较，不做语义判断，所以它只回答「这一条是不是另一条的
    子串」。刻意不用语义相似度（embedding）：前序实测本地 ONNX 模型对真库 claim
    全配对时，人工确认过的真实重复相似度低于 0.89 进不了候选档，而 0.90 档里
    过半是误合并、0.95 档一条都抓不到——误合并率远高于字面规则。两种口径都能用
    ``backend/scripts/measure_fact_dedupe.py`` 重跑复核。

    两级去重**之后**才按 ``FACT_FIELD_LIMITS`` 截断（被合并掉的重复不占配额），
    每字段用 `、` 连接；渲染前剥一次句末标点（`。！？；`），仍为空则跳过该字段；
    没有事实返回 ``{}``。

    排序取存活项所在簇的最早位置；多父簇按「最短的包含者」定归属，因此同一批
    事实的**存活集合**与输入顺序无关，但多父簇场景下的相对次序不保证等于源顺序。
    复杂度 O(n²)：落库路径已按字段封顶，单批 payload 未封顶时仍可正常运行。

    ``kind`` 目前不影响结果：组织同样有 appearance 字段、字段集合一致，
    组织专属的谓词差异在 validate 阶段按 kind 留位处理。
    """
    buckets: dict[str, list[tuple[int, str, str]]] = {field: [] for field in FACT_FIELDS}
    seen: dict[str, set[str]] = {field: set() for field in FACT_FIELDS}

    # 阶段 1：按字段收集 + 精确去重
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
        seen[field].add(key)

        text = claim.strip().rstrip(_TRAILING_PUNCT).strip()
        form = normalize_claim_form(text)
        if not form:
            continue
        buckets[field].append((len(buckets[field]), text, form))

    rendered: dict[str, str] = {}
    for field, entries in buckets.items():
        if not entries:
            continue

        # 阶段 2：嵌套去重。entries 条数个位数，两两比较足够。
        # 2a. 归一串相同 → 同一特质的措辞差异（只差标点或「的」）：保留信息量最大的一条，
        #     也就是原文最长者（长度相同取先到的），位置取该组里最早的。
        groups: list[dict] = []
        by_form: dict[str, dict] = {}
        for pos, text, form in entries:
            group = by_form.get(form)
            if group is None:
                group = {"form": form, "pos": pos, "text": text}
                by_form[form] = group
                groups.append(group)
                continue
            # 择优键不数「、」：连接符本身不是信息，含它的原文反而会与渲染
            # 结果的分隔符混淆（渲染时就是用「、」连接各条）。键里带上原文本身，
            # 保证长度打平时结果也与输入顺序无关。
            if _preferred_text(group["text"], text) == text:
                group["text"] = text
            group["pos"] = min(group["pos"], pos)

        # 2b. 归一串是另一条的**真子串**才折叠；否定上下文与纯 ASCII 形式不折（见
        #     _folded_contains）。三条以上互相嵌套时只有最长者存活。
        forms = [group["form"] for group in groups]
        survivors = [
            i
            for i, form in enumerate(forms)
            if not any(j != i and _folded_contains(forms[j], form) for j in range(len(forms)))
        ]

        # 2c. 位置继承：每条被吸收项只归属**最短的**包含它的存活项（同长取更早的），
        #     不能像「凡包含我的都算我的簇」那样合并——一条短串同时被多条长串包含时，
        #     会让互不相关的簇全部塌到同一个位置，把排在后面的独立事实挤出配额。
        sort_pos = {i: groups[i]["pos"] for i in survivors}
        for i, form in enumerate(forms):
            if i in survivors:
                continue
            owners = [j for j in survivors if _folded_contains(forms[j], form)]
            if not owners:
                # 不可能发生：折叠要求严格更短，链长严格递增必有存活项。
                # 若真出现，说明规则被改坏了——宁可少继承一个位置，也不要报错。
                continue
            owner = min(owners, key=lambda j: (len(forms[j]), groups[j]["pos"]))
            sort_pos[owner] = min(sort_pos[owner], groups[i]["pos"])

        survivors.sort(key=lambda i: (sort_pos[i], i))

        limit = FACT_FIELD_LIMITS.get(field, MAX_FACTS_PER_FIELD)
        items = [groups[i]["text"] for i in survivors[:limit]]
        if items:
            rendered[field] = "、".join(items)

    return rendered


# 旧格式（模型直接给三段长文本）与本模块的字段集合一致，直接复用
LEGACY_PROFILE_FIELDS = FACT_FIELDS


@dataclass
class ParsedProfile:
    """一次 payload 格式判定的结果。

    ``entity``：payload 的 ``name``（非 str 或缺失则为 ``""``）。
    ``mode``：``"facts"`` / ``"legacy"`` / ``"empty"``。
    ``facts``：``mode == "facts"`` 时的原始事实条目，不做校验、不预过滤。
    ``legacy``：``mode == "legacy"`` 时的字段文本，只含非空字段（已 strip）。
    """

    entity: str
    mode: str
    facts: list[dict]
    legacy: dict[str, str]


def parse_profile_payload(payload: Any) -> ParsedProfile:
    """判定一份角色档案返回是"原子事实格式"还是"旧长文本格式"（issue #190 P2）。

    这是旧格式的回退路径：提示词改为输出 ``profile_facts`` 原子事实后，旧项目、
    用户自定义提示词与同步链路仍会给出 ``personality``/``background``/
    ``appearance`` 三段长文本，需要先分清格式再交给下游。

    判定顺序：

    1. 非 dict → ``mode="empty"``，不抛异常；
    2. ``profile_facts``（优先）或 ``facts`` 是非空 list → ``mode="facts"``，
       原始条目原样返回，此时忽略旧文本字段；
    3. 否则收集旧文本字段中的非空字符串（strip 后），非空即 ``mode="legacy"``；
    4. 都没有 → ``mode="empty"``。

    事实条目本身不校验（字段、引文、章号交给 ``validate_profile_facts``），
    空 list 与非 list 都只是"不是事实格式"，继续走旧文本分支，不报错。
    """
    if not isinstance(payload, dict):
        return ParsedProfile(entity="", mode="empty", facts=[], legacy={})

    raw_name = payload.get("name")
    entity = raw_name if isinstance(raw_name, str) else ""

    raw_facts = payload.get("profile_facts")
    if not isinstance(raw_facts, list) or not raw_facts:
        raw_facts = payload.get("facts")
    if isinstance(raw_facts, list) and raw_facts:
        return ParsedProfile(entity=entity, mode="facts", facts=raw_facts, legacy={})

    legacy: dict[str, str] = {}
    for field in LEGACY_PROFILE_FIELDS:
        value = payload.get(field)
        if isinstance(value, str) and value.strip():
            legacy[field] = value.strip()

    return ParsedProfile(
        entity=entity,
        mode="legacy" if legacy else "empty",
        facts=[],
        legacy=legacy,
    )


def resolve_profile_fields(
    payload: Any,
    chapters_by_number: dict[int, str],
    *,
    kind: str = "character",
) -> tuple[dict[str, str], dict[str, int], list[dict]]:
    """把一份实体返回解析成可直接落库的档案文本，并给出统计。

    这是 P4 接线的唯一入口，保证三条写入路径用同一套判定：

    - ``facts`` 格式 → 校验 + 渲染，返回只含非空字段的 ``{field: text}``；
    - ``legacy`` / ``empty`` 格式 → 返回空 dict，**调用方保持原行为**
      （旧门禁 ``looks_like_profile_narration`` 仍在旧路径上生效，
      这里不重复判断，避免两套口径打架）。

    统计在 ``validate_profile_facts`` 的 stats 基础上追加：

    - ``payloads_facts`` / ``payloads_legacy`` / ``payloads_empty``：格式计数；
    - ``facts_rendered``：通过校验的事实条数；
    - ``facts_field_personality`` / ``facts_field_background`` /
      ``facts_field_appearance``：各字段通过校验的条数（用于观察覆盖率）。

    第三个返回值是**通过校验的事实本身**（只含 accepted 条目），供落库做溯源
    （``characters.profile_facts``）；legacy/empty 格式返回空列表。
    """
    parsed = parse_profile_payload(payload)
    stats: dict[str, int] = {f"payloads_{parsed.mode}": 1}
    if parsed.mode != "facts":
        return {}, stats, []

    result = validate_profile_facts(
        {"name": parsed.entity, "facts": parsed.facts},
        chapters_by_number,
        kind=kind,
    )
    for key, value in result.stats.items():
        stats[key] = stats.get(key, 0) + value
    for fact in result.facts:
        key = f"facts_field_{fact.get('field')}"
        stats[key] = stats.get(key, 0) + 1
    stats["facts_rendered"] = len(result.facts)

    return render_profile(result.facts, kind=kind), stats, result.facts


def merge_profile_facts(existing: Any, new_facts: list[dict]) -> str | None:
    """把新事实并入已有溯源 JSON，返回新的 JSON 串（无事实时返回 None）。

    合并规则：按 ``(field, normalize(claim))`` 去重，新事实覆盖旧值；
    每个字段只保留**最近** ``FACT_FIELD_LIMITS[field]`` 条——档案是演进中的
    快照，旧属性被新章节推翻时应当让位，而不是无限堆积。
    """
    merged: dict[str, list[dict]] = {field: [] for field in FACT_FIELDS}
    seen: dict[str, dict[str, dict]] = {field: {} for field in FACT_FIELDS}

    def _add(items: Any) -> None:
        if not isinstance(items, list):
            return
        for fact in items:
            if not isinstance(fact, dict):
                continue
            field = fact.get("field")
            claim = fact.get("claim")
            if field not in FACT_FIELDS or not isinstance(claim, str):
                continue
            key = normalize_quote(claim)
            if not key:
                continue
            seen[field][key] = fact

    if isinstance(existing, str) and existing.strip():
        try:
            _add(json.loads(existing))
        except (ValueError, TypeError):
            # 溯源是附属信息，损坏时按"没有历史事实"处理，不影响本次写入
            pass
    _add(new_facts)

    for field in FACT_FIELDS:
        limit = FACT_FIELD_LIMITS.get(field, MAX_FACTS_PER_FIELD)
        # dict 保持插入顺序：旧事实在前，新事实在前者被覆盖时原位更新 → 取尾部 limit 条即"最近"
        merged[field] = list(seen[field].values())[-limit:]

    facts = [fact for field in FACT_FIELDS for fact in merged[field]]
    if not facts:
        return None
    return json.dumps(facts, ensure_ascii=False)
