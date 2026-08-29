"""思考型模型 token 预算提升逻辑测试（issue #13 修复 Step 1）。

背景：commandcode/deepseek-v4-flash 是思考型模型，长 JSON 输出时推理过程
耗尽默认 max_tokens(32000) 预算，导致正文为空 / 网关 524 超时。
修复：检测思考型模型，对大 JSON 任务自动提升预算。
"""
from app.services.ai_service import is_thinking_model, resolve_effective_max_tokens


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
