"""agent 对话路径的 AI 服务替身（PR-0c 预算门禁的共享桩）。

背景（#86 / #87 的代价）：`ProjectAgentService.stream_chat` 组装 prompt 前会经
`agent_prompt_budget.resolve_history_budget_chars()` 向注入的 AI 服务要「本次实发
的窗口」（`AIService.resolve_effective_window_tokens`）。用
`SimpleNamespace(default_model=...)` 手搓的裸替身只要还没走到那条路径就照旧全绿，
一旦走到就 `AttributeError` —— 这类"跨 PR 才爆"的桩已经让
`test_project_agent_inline_task.py` 的 11 个用例集体变红过一次。

约定：任何注入 `ProjectAgentService` 的 AI 替身都从这里构造（或继承），
预算窗口面由构造保证完整，不再靠每个测试文件自觉补齐。模型出口
（`generate_text` / `generate_text_stream_full` 等）仍由各用例自己注入：
补一个会静默返回假文本的默认实现反而会掩盖真实的调用面。
"""
from __future__ import annotations

#: 1M token 窗口：预算 = clamp(1M x 0.3, 60k, 400k) = 400k 字符（上限）。
#: 对"与预算无关"的用例等价于"历史永不被裁剪"，不会改变既有断言。
WINDOW_TOKENS: int = 1_000_000


class AgentAIServiceStub:
    """agent 路径的最小 AI 服务替身：`default_model` / `base_url` + 预算窗口面。

    - 直接当替身用：`AgentAIServiceStub(default_model="m", base_url="https://gw.example/v1")`；
    - 需要自定义模型出口的类假件可继承：
      `class Fake(AgentAIServiceStub): ...`（继承后窗口面自动补齐）。
    """

    default_model: str = "test-model"
    base_url: str = ""

    def __init__(self, **overrides) -> None:
        for name, value in overrides.items():
            setattr(self, name, value)

    async def resolve_effective_window_tokens(self, model=None, provider=None) -> int:
        """`AIService.resolve_effective_window_tokens` 的替身：固定 1M 窗口。

        签名与生产方法逐字同形（`model` / `provider` 默认 None），预算路径按
        关键字调用；返回真值而非哨兵，换算后的预算落在上限，不触发裁剪。
        """
        return WINDOW_TOKENS
