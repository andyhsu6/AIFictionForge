"""模型输出能力注册表测试（章内续写 B0：输出 token 上限 → 字符预算换算的数据基础）。

背景：章内续写需要把模型 max output tokens 换算为安全字符预算
（effective_segment_chars = min(8000, floor(output_limit / 2.0))），
因此 ai_service 需要一个与 _KNOWN_CONTEXT_WINDOWS 对称的输出上限注册表。

本文件先钉住相邻既有函数的当前可观测行为（characterization），
再对新函数 detect_max_output_tokens 做行为测试。
"""
from app.services.ai_service import (
    _DEFAULT_MAX_OUTPUT_TOKENS,
    _KNOWN_OUTPUT_LIMITS,
    THINKING_MODEL_DEFAULT_MAX_TOKENS,
    detect_context_window,
    detect_max_output_tokens,
    is_thinking_model,
)


# ---------------------------------------------------------------------------
# Characterization：钉住未改动的既有行为（对应 test_model_capability /
# test_ai_token_budget 所依赖的契约，防止新注册表引入时被误改）
# ---------------------------------------------------------------------------


def test_characterization_context_window_unknown_is_32768():
    assert detect_context_window("totally-unknown-model-xyz") == 32768


def test_characterization_context_window_prefers_longer_key():
    # gpt-4o / gpt-4.1 必须先于 gpt-4 命中（按键长度降序解析）
    assert detect_context_window("gpt-4o") == 128000
    assert detect_context_window("gpt-4.1") == 1047576


def test_characterization_is_thinking_model_by_name_and_gateway():
    assert is_thinking_model("deepseek-v4-flash", "https://api.commandcode.ai/v1") is True
    assert is_thinking_model("deepseek-r1", "https://api.openai.com/v1") is True
    assert is_thinking_model("gpt-4o", "https://api.openai.com/v1") is False


def test_characterization_thinking_default_budget_constant():
    assert THINKING_MODEL_DEFAULT_MAX_TOKENS == 64000


# ---------------------------------------------------------------------------
# detect_max_output_tokens 行为测试
# ---------------------------------------------------------------------------


def test_default_output_limit_is_positive():
    assert _DEFAULT_MAX_OUTPUT_TOKENS > 0


def test_registry_values_are_all_positive():
    assert all(limit > 0 for limit in _KNOWN_OUTPUT_LIMITS.values())


def test_exact_model_match():
    assert detect_max_output_tokens("deepseek-v4") == 64000
    assert detect_max_output_tokens("gpt-4.1") == 32768
    assert detect_max_output_tokens("gemini-2.5-pro") == 65536
    assert detect_max_output_tokens("gpt-4o") == 16384


def test_longest_key_wins_over_shorter_prefix():
    # gpt-4.1 键必须先于 gpt-4 命中；gpt-4o 同理
    assert detect_max_output_tokens("gpt-4.1-mini") == 32768
    assert detect_max_output_tokens("gpt-4o-2024-08-06") == 16384
    # 裸 gpt-4 仍命中短键
    assert detect_max_output_tokens("gpt-4-0613") == 8192


def test_thinking_models_never_below_thinking_default():
    # 思考/推理模型条目不得低于 THINKING_MODEL_DEFAULT_MAX_TOKENS（修复 #13 语义）
    for model in ("deepseek-v3", "deepseek-r1", "deepseek-r1-distill-qwen"):
        assert detect_max_output_tokens(model) >= THINKING_MODEL_DEFAULT_MAX_TOKENS


def test_case_insensitive_match():
    assert detect_max_output_tokens("DeepSeek-V4") == 64000


def test_unknown_model_falls_back_to_conservative_default():
    assert detect_max_output_tokens("some-brand-new-model-9000") == _DEFAULT_MAX_OUTPUT_TOKENS


def test_none_and_empty_model_are_robust():
    assert detect_max_output_tokens(None) == _DEFAULT_MAX_OUTPUT_TOKENS
    assert detect_max_output_tokens("") == _DEFAULT_MAX_OUTPUT_TOKENS
    assert detect_max_output_tokens("   ") == _DEFAULT_MAX_OUTPUT_TOKENS


def test_base_url_none_is_robust_and_gateway_url_accepted():
    assert detect_max_output_tokens("gpt-4o", None) == 16384
    assert detect_max_output_tokens("gpt-4o", "https://api.commandcode.ai/v1") == 16384
    assert detect_max_output_tokens(None, "https://api.commandcode.ai/v1") == _DEFAULT_MAX_OUTPUT_TOKENS
