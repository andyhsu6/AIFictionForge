"""PR-0c：助手 prompt 预算按实测窗口分层。

单位链（务必读完整再改数字）：
    get_effective_context_window() -> token
      -> x CHARS_PER_TOKEN -> 字符
      -> x ratio -> clamp(min, max) -> history_budget_chars
任何一环都不允许出现第二套系数或第二处 clamp。
"""
import pytest

from app.services.agent_prompt_budget import (
    CHARS_PER_TOKEN,
    HISTORY_BUDGET_ANCHOR_CAP_CHARS,
    compute_history_budget_chars,
)


def test_chars_per_token_is_the_single_source_constant():
    # 取值 1.0 的依据见 agent_prompt_budget.py 内注释（中文≈1 字符/token 的保守近似，
    # 出处 ai_service.py 的"单位错配"历史）。改这个数字必须同时改 config 默认值。
    assert CHARS_PER_TOKEN == 1.0


def test_one_million_token_window_yields_300000_chars():
    # 架构计划 §5 验收原文：mock 返回 1_000_000 ⇒ 预算 300000
    assert compute_history_budget_chars(1_000_000) == 300_000


def test_upper_clamp_prevents_using_the_whole_window_as_history():
    assert compute_history_budget_chars(100_000_000) == 400_000


def test_lower_clamp_is_the_legacy_value_and_not_a_small_model_fallback():
    # 60000 恰等于现状硬编码值；它的用途是防异常配置算出过小/无界预算，
    # 不是为小模型兜底（小模型已被计划 B 拦在系统外）。
    assert compute_history_budget_chars(1_000) == 60_000


def test_zero_window_clamps_to_floor_instead_of_becoming_unbounded_or_zero():
    assert compute_history_budget_chars(0) == 60_000


def test_negative_window_is_rejected_not_silently_clamped():
    with pytest.raises(ValueError):
        compute_history_budget_chars(-1)


def test_anchor_cap_matches_per_message_truncation_cap():
    # 永不裁剪段的长度上限沿用历史里的单条 6000 字符口径，不新造一个魔法数
    assert HISTORY_BUDGET_ANCHOR_CAP_CHARS == 6_000
