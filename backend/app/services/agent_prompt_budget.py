"""助手 prompt 预算的唯一换算来源（PR-0c，架构计划 §5）。

来历备忘（写给后来想"调大数字"的人，请勿删）
------------------------------------------------
`60000` 这个数字由 2026-08-26 `a46e3b3` 在 **v1 语义**下引入；v2 PR（`64e6fe6` /
`631b6a47`）在计划文档 `.omo/plans/agent-tool-refactor.md:330` 明确选择维持现状、
`learnings.md:30` 记为 untouched —— **两道阈值（总预算 60000 与轮数上限 4）的交互
从未被复盘**。它同时是现存**唯一**成本刹车：直接删掉它，历史长度就变成无界。
所以本 PR 的形态是"按实测窗口分层换算"，而不是"放宽"或"删掉"。

ratio=0.3 的依据：一个决策轮的固定重发成本 = 系统提示词 ≈1.5k 字符 + 全量工具
schema ≈20k 字符（operational 8267B + 基础 6618B + extended 5339B = 20224B，未含
MCP 工具与 `as_model_tool()` 包裹层）+ 每轮输出余量。剩下的才给历史。
上限 400000：防"把整张窗口当历史"导致成本失控（历史是每轮重发的，不是发一次）。
下限 60000：防异常配置（ratio 调到 0、窗口被误写很小）算出过小甚至无界的预算；
  **不是**为小模型兜底 —— 窗口不足的模型已被计划 B 以
  `validation.ai_model_below_minimum` 拦在系统外。
"""
from __future__ import annotations

from dataclasses import dataclass, field

#: 字符↔token 换算的**全仓库唯一定义处**。取值 1.0 是"中文≈1 字符/token"的保守
#: 近似，依据是 `ai_service.py` 里既有同类注释及其记录的"单位错配"历史（B 需求 #55
#: 步骤 2/3 就是为根除单位错配而生）。禁止在第二个文件里再出现这个系数。
CHARS_PER_TOKEN: float = 1.0

#: 与现状硬编码值一致的保守下界（见模块 docstring 的来历备忘）。
HISTORY_BUDGET_MIN_CHARS: int = 60_000
HISTORY_BUDGET_MAX_CHARS: int = 400_000
HISTORY_BUDGET_RATIO: float = 0.3

#: 永不裁剪段（本轮原始诉求 / PR-2c 后的计划 objective）的长度上限。
#: 刻意复用历史消息既有的 `[:6000]` 口径，不新造魔法数：保证"锚点"本身
#: 不会反过来把整张预算吃掉，也不会无界增长。
HISTORY_BUDGET_ANCHOR_CAP_CHARS: int = 6_000


@dataclass
class PromptBudgetTrace:
    """一次 prompt 组装的裁剪留痕（§5 ④）。由 async 侧创建、`_build_prompt` 填充。

    `dropped_messages > 0` 即意味着"发生了静默丢弃"——PR-0c 之前这件事完全不可见。
    """

    budget_chars: int
    used_chars: int = 0
    dropped_messages: int = 0
    dropped_chars: int = 0
    anchor_chars: int = 0
    anchor_truncated: bool = False
    effective_tokens: int | None = None
    dropped_summaries: list[str] = field(default_factory=list)

    def as_log(self) -> str:
        return (
            "[agent-prompt-budget] budget={budget} used={used} "
            "dropped={dropped} dropped_chars={dropped_chars} anchor={anchor} "
            "anchor_truncated={anchor_truncated} tokens={tokens}".format(
                budget=self.budget_chars,
                used=self.used_chars,
                dropped=self.dropped_messages,
                dropped_chars=self.dropped_chars,
                anchor=self.anchor_chars,
                anchor_truncated=self.anchor_truncated,
                tokens=self.effective_tokens,
            )
        )


def compute_history_budget_chars(
    effective_tokens: int,
    *,
    ratio: float = HISTORY_BUDGET_RATIO,
    min_chars: int = HISTORY_BUDGET_MIN_CHARS,
    max_chars: int = HISTORY_BUDGET_MAX_CHARS,
    chars_per_token: float = CHARS_PER_TOKEN,
) -> int:
    """唯一换算式：clamp(tokens x CHARS_PER_TOKEN x ratio, min, max)。

    输入单位是 **token**（`get_effective_context_window()` 的返回值），输出单位是
    **字符**（`_build_prompt` 的裁剪口径）。除此函数外，任何地方都不得做
    token→字符 换算或 clamp。

    **禁止**复用 `model_capability_probe.MIN_CONTEXT_WINDOW_TOKENS`：那是"准入门禁
    阈值"，与"预算换算"无关，耦合起来会让改门禁顺带改掉助手的成本结构。
    """
    if effective_tokens < 0:
        raise ValueError(f"effective_tokens must be >= 0, got {effective_tokens}")
    budget = int(effective_tokens * chars_per_token * ratio)
    if budget < min_chars:
        return int(min_chars)
    if budget > max_chars:
        return int(max_chars)
    return budget
