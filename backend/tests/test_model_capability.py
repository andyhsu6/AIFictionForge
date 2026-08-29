"""模型能力分级测试（D4：推荐 + 自动分级，不硬限制）。

根据模型上下文窗口自动选择注入策略：
- 1M 上下文 → 全书全量注入（大预算）
- 128K 上下文 → 摘要 + 检索（中预算）
- 小窗口 → 现状（低预算/不注入）
"""
from app.services.ai_service import (
    resolve_context_budget_chars,
    detect_context_window,
)


def test_detect_1m_context_window():
    assert detect_context_window("deepseek-v4-flash") >= 1000000
    assert detect_context_window("deepseek-v3") >= 1000000


def test_detect_128k_context_window():
    assert detect_context_window("claude-3-5-sonnet") >= 128000
    assert detect_context_window("gpt-4o") >= 128000


def test_detect_small_context_window():
    assert detect_context_window("gpt-3.5-turbo") < 128000


def test_1m_model_gets_full_book_budget():
    """1M 模型：全书注入预算 = 窗口的 80%（≈800K 字符）。"""
    budget = resolve_context_budget_chars("deepseek-v4-flash")
    assert budget >= 700000  # 全书量级，显著高于 128K 档位


def test_128k_model_gets_reduced_budget():
    """128K 模型：降级为摘要+检索预算（窗口的 30%，≈38K）。"""
    budget = resolve_context_budget_chars("claude-3-5-sonnet")
    assert budget < 700000  # 不触发全书全量
    assert budget > 10000  # 仍保留一定的上下文注入能力


def test_unknown_model_defaults_conservative():
    """未知模型：保守预算，避免超窗口。"""
    budget = resolve_context_budget_chars("unknown-model-xyz")
    assert budget < 200000
