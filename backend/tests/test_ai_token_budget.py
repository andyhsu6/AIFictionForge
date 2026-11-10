"""思考型模型 token 预算提升逻辑测试（issue #13 修复 Step 1）。

背景：commandcode/deepseek-v4-flash 是思考型模型，长 JSON 输出时推理过程
耗尽默认 max_tokens(32000) 预算，导致正文为空 / 网关 524 超时。
修复：检测思考型模型，对大 JSON 任务自动提升预算。

issue #276 起：章节生成/章内续写/局部重写不再用名字判定预算（改走
`AIService.resolve_output_budget` 的实测能力），`ensure_thinking_model_min_tokens`
与名字登记表已删除。本文件只钉住**保留下来**的 `resolve_effective_max_tokens`
契约（通用路径的默认值兜底），语义与改动前一致。
"""
from app.services.ai_service import (
    is_thinking_model,
    resolve_effective_max_tokens,
)


def test_thinking_model_detected_by_model_name():
    assert is_thinking_model("deepseek-v4-flash", "https://api.commandcode.ai/v1")
    assert is_thinking_model("deepseek-r1", "https://api.openai.com/v1")
    assert is_thinking_model("gpt-4o", "https://api.commandcode.ai/v1")


def test_non_thinking_model_not_detected():
    assert not is_thinking_model("gpt-4o", "https://api.openai.com/v1")
    assert not is_thinking_model("claude-3-5-sonnet", "https://api.anthropic.com/v1")
    assert not is_thinking_model("qwen-max", "https://api.moonshot.cn/v1")


def test_explicit_max_tokens_always_wins():
    # 调用方显式传了 max_tokens，无论模型是否思考型都不改动
    assert resolve_effective_max_tokens(
        requested=20000, default=32000, model="deepseek-v4-flash", base_url="https://api.commandcode.ai/v1"
    ) == 20000


def test_thinking_model_low_default_gets_boosted():
    # 思考型模型 + 未显式传 + 默认预算过低 → 提升到 64000
    assert resolve_effective_max_tokens(
        requested=None, default=32000, model="deepseek-v4-flash", base_url="https://api.commandcode.ai/v1"
    ) == 64000


def test_thinking_model_high_default_untouched():
    # 用户已配置大预算（如 1000000）→ 不降级不提升
    assert resolve_effective_max_tokens(
        requested=None, default=1000000, model="deepseek-v4-flash", base_url="https://api.commandcode.ai/v1"
    ) == 1000000


def test_non_thinking_model_low_default_untouched():
    # 普通模型 + 低预算 → 不提升（只有思考型才需要）
    assert resolve_effective_max_tokens(
        requested=None, default=32000, model="gpt-4o", base_url="https://api.openai.com/v1"
    ) == 32000


# ---- issue #276：显式小预算不再被名字抬底（局部重写/续写改走实测能力） ----


def test_explicit_low_budget_is_no_longer_boosted_by_model_name():
    # 旧 `ensure_thinking_model_min_tokens` 会把 500 抬到 64000（修复 #45 的名字路径）。
    # 该机制随预算改造删除：调用方若要安全预算，必须走 resolve_output_budget 的实测能力。
    # resolve_effective_max_tokens 对显式值原样放行 —— 这条契约不变。
    assert resolve_effective_max_tokens(
        requested=500, default=32000, model="deepseek-v4-flash", base_url="https://api.commandcode.ai/v1"
    ) == 500


def test_explicit_budget_above_floor_untouched():
    assert resolve_effective_max_tokens(
        requested=200000, default=32000, model="deepseek-v4-flash", base_url="https://api.commandcode.ai/v1"
    ) == 200000


def test_explicit_low_budget_without_model_untouched():
    # 未传模型时同样原样放行（不再有按名字的兜底分支）
    assert resolve_effective_max_tokens(
        requested=500, default=32000, model=None
    ) == 500


def test_name_based_min_tokens_helper_is_gone():
    import app.services.ai_service as module

    assert not hasattr(module, "ensure_thinking_model_min_tokens")
