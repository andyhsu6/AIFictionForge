"""自适应学习「上游报出的输出上限」（issue #147）。

为什么存在
----------
`max_tokens` 是**单次输出**上限，和上下文窗口是两件事。本模块前身是一行按网关写死的
钳值（`commandcode.ai => max_tokens <= 384000`，commit `bfaf807`），它的隐含假设是
「一个网关只有一个输出上限」。实测证明假设不成立：同一网关上 DeepSeek V4 Flash 报
384000、`meituan/LongCat-2.0:free` 报 131072，于是用户把模型换成后者之后**每个请求
都被 400 拒掉**（表现为「切换模型报错不给用」），而命令行/OpenCode 用同一把 key、
同一个 base URL、同一个模型却正常——差别只在它们发的 max_tokens 小。

所以这里不换新魔数，改成**读网关自己报出的数字**：请求被 400 拒时，若报错文本点名了
一个低于我们发出去的值、且归属明确是输出上限的数字，就按 (base_url, model) 记住它，
后续请求钳到该值。网关把上限改了（实测同一模型 6 分钟内从 131072 变成 262144）会随
下一次报错自动跟上。

为什么钳制方向是「只降不升 + 只认对比短语后面的数」
----------------------------------------------------
钳太狠 = 正文被静默截断（#13/#45 的失效形态：思考型模型推理吃掉预算后正文为空）；
钳不动 = 再吃一次 400（生成前的参数校验，不落计费）。代价不对称，所以：
- 归属含糊（同一分句里既讲输出又讲上下文）的数字一律不用；
- 只认紧跟在「less or equal to / at most / <= / limit of」这类短语之后的数字：网关会把
  **它拒绝的那个值**一起写进报错，早先「候选取最大」的规则学的就是这种回显值，得到一个
  仍然会被拒的上限（issue #147 自测时抓到）；多条限额同时出现时取最小的那条；
- 报不出数字就返回 None，绝不猜。

分句与数字归属的判据刻意与 `model_capability_probe._classify_bound_rejection`（#65）
同源，但那边在判「窗口」，这边在判「输出」，两者结论方向相反，故不共用代码。

已知未覆盖项（如实声明，勿当成已通用）
--------------------------------------
`_CEILING_PHRASES` 只有英文：那是本机唯一实测到的报错措辞（中文网关也用的是英文短语）。
若某网关用中文写对比短语（「最大输出131072」），这里学不到上限 —— 后果是**退回现状**
（不钳制、原样把上游报错抛给用户），不是钳出一个错数字。补措辞请以真实抓包为准，
不要凭想象扩表（想象出来的短语只会把无关数字吸进来）。
"""
from __future__ import annotations

import re
from typing import Dict, Optional, Tuple

# 「这个数字说的是输出/补全上限」
_OUTPUT_CAP_HINTS = (
    "max_tokens",
    "max tokens",
    "maximum tokens",
    "output tokens",
    "output length",
    "output cap",
    "max_completion",
    "completion tokens",
    "response tokens",
    "can generate",
)
# 「这个数字说的是上下文/输入上限」——与一次能生成多少无关，不得当成输出上限来钳。
_CONTEXT_BOUND_HINTS = (
    "maximum context",
    "max context",
    "context length",
    "context window",
    "context size",
    "context tokens",
    "model context",
    "n_ctx",
    "prompt tokens",
    "input tokens",
    "total tokens",
    "token limit",
    "too many tokens",
    "prompt is too long",
    "input is too long",
)
# 句读切分：数字只按它所在的分句判定含义。刻意**不含**逗号，因为逗号要留给
# 「131,072」这种千分位写法（正则已按整体匹配，孤立的 131 不会被当成候选）。
_CLAUSE_BREAKS = frozenset('.;!?\n\r{}[]()"')
# 边界只排除**数字/逗号/点号**相邻（挡住版本号、小数、千分位碎片）。刻意不用 `\w`：
# Python 的 `\w` 含中日韩字符，`最大输出131072` 这种中文措辞就会读不出上限，而本仓库
# 实测的那个网关正是中文报错。`\uXXXX` 转义里的伪数字由「对比短语」判据挡住。
_NUMBER_PATTERN = re.compile(r"(?<![\d,.])(?P<number>\d{1,3}(?:,\d{3})+|\d+)(?![\d,.])")
# 「这个数字是上限」的措辞。数字必须紧跟在对比/限额短语**之后**：网关的报错会同时
# 回显它收到的值（`/max_tokens: 900000 is not less or equal to 131072`），把回显值当成
# 上限就等于学到一个仍然会被拒的数字，白钳预算还修不好。
_CEILING_PHRASES = (
    "less or equal to",
    "less than or equal to",
    "must be <=",
    "should be <=",
    "<=",
    "at most",
    "no more than",
    "maximum allowed",
    "allowed is",
    "limit of",
    "limited to",
    "up to",
    "cannot exceed",
    "can not exceed",
    "exceeds",
)
_CLAUSE_BEFORE_CHARS = 80
_CLAUSE_AFTER_CHARS = 24
# 对比短语必须在数字**前面这么短一截**里出现，才算这个数字是上限
_CEILING_LOOKBEHIND_CHARS = 28

# 进程内记忆：键 = (base_url, model)，值 = 网关自己报出的输出上限。
# 不落库——它是「这台网关此刻对这个回答过多少」，跨进程重启后重新学即可。
_output_ceilings: Dict[Tuple[str, str], int] = {}


def _memo_key(base_url: Optional[str], model: Optional[str]) -> Tuple[str, str]:
    return ((base_url or "").strip().rstrip("/").lower(), (model or "").strip())


def _clause_bounds(text: str, start: int, end: int) -> Tuple[int, int]:
    """数字所在分句的 [begin, finish)：向前到上一个句读（最多 _CLAUSE_BEFORE_CHARS）。"""
    begin = max(0, start - _CLAUSE_BEFORE_CHARS)
    for index in range(start - 1, begin - 1, -1):
        if text[index] in _CLAUSE_BREAKS:
            begin = index + 1
            break
    finish = min(len(text), end + _CLAUSE_AFTER_CHARS)
    for index in range(end, finish):
        if text[index] in _CLAUSE_BREAKS:
            finish = index
            break
    return begin, finish


def parse_reported_output_ceiling(body: str, requested_max_tokens: int) -> Optional[int]:
    """从一次拒绝的报错里取出**网关自己报出的**输出上限；取不到就返回 None。

    一个数字要同时满足三条才算上限：
    1. 严格低于 `requested_max_tokens`（等于或高于都说明这次报错没要求我们降下来）；
    2. 所在分句讲的是输出（点名 `max_tokens` 等），且**不**混讲上下文/输入（归属含糊
       就不猜，宁可再吃一次 400）；
    3. 紧跟在对比/限额短语之后（把网关回显的被拒值排除在外）。

    多条限额同时出现时取**最小**的那个：那是唯一同时满足所有约束的数字；钳过头的风险
    由「下一次报错再学到更准的值」兜底，而钳不动只是维持现状。
    """
    text = (body or "").lower()
    candidates = []
    for match in _NUMBER_PATTERN.finditer(text):
        digits = match.group("number").replace(",", "")
        value = int(digits)
        if value <= 0 or value >= requested_max_tokens:
            continue
        number_start = match.start("number")
        begin, _finish = _clause_bounds(text, number_start, match.end())
        clause = text[begin:number_start]
        if any(hint in clause for hint in _CONTEXT_BOUND_HINTS):
            continue
        if not any(hint in clause for hint in _OUTPUT_CAP_HINTS):
            continue
        # 只看数字**前面**那一小截：整句里出现过对比短语不算数（同句可能还有区域编号、
        # 重试次数之类的小数字，而候选取最小，误认一个小数字 = 把预算钳到不可用）。
        antecedent = text[max(begin, number_start - _CEILING_LOOKBEHIND_CHARS) : number_start]
        if not any(phrase in antecedent for phrase in _CEILING_PHRASES):
            continue
        candidates.append(value)
    return min(candidates) if candidates else None


def learn_output_ceiling(
    *,
    base_url: Optional[str],
    model: Optional[str],
    body: str,
    requested_max_tokens: int,
) -> Optional[int]:
    """解析并记住一条报错里的输出上限，返回本次学到的数字（没学到返回 None）。"""
    ceiling = parse_reported_output_ceiling(body, requested_max_tokens)
    if ceiling is None:
        return None
    _output_ceilings[_memo_key(base_url, model)] = ceiling
    return ceiling


def resolve_output_ceiling(base_url: Optional[str], model: Optional[str]) -> Optional[int]:
    """该 (网关, 模型) 已学到的输出上限；没学过返回 None（此时不钳制）。"""
    return _output_ceilings.get(_memo_key(base_url, model))


def clamp_to_known_ceiling(
    base_url: Optional[str], model: Optional[str], max_tokens: int
) -> int:
    """把请求预算钳到已学到的输出上限；**只降不升**（用户配的小预算绝不抬高）。"""
    ceiling = resolve_output_ceiling(base_url, model)
    if ceiling is not None and ceiling < max_tokens:
        return ceiling
    return max_tokens


def clear_output_ceiling_memo() -> None:
    """测试/运维用：清空已学到的输出上限。"""
    _output_ceilings.clear()
