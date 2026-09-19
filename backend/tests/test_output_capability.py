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


from app.services.ai_clients.output_caps import (  # noqa: E402
    clear_output_ceiling_memo,
    learn_output_ceiling,
)


def remember_output_ceiling_for_test(base_url, model, ceiling):
    """喂一条网关风格的拒绝给公开学习入口，等价于一次真实 400 学到的结果。"""
    learned = learn_output_ceiling(
        base_url=base_url,
        model=model,
        body=f"/max_tokens: 900000 is not less or equal to {ceiling}",
        requested_max_tokens=900000,
    )
    assert learned == ceiling, "夹具没学到说明拒绝文本不合判据，别让用例静默跑在空记忆上"


# ---------------------------------------------------------------------------
# Characterization：钉住未改动的既有行为（对应 test_model_capability /
# test_ai_token_budget 所依赖的契约，防止新注册表引入时被误改）
# ---------------------------------------------------------------------------


def test_characterization_context_window_unknown_has_no_hint():
    # 需求 #55 步骤 4：未登记模型不再回退成一个保守窗口值（那是伪装成实测结论的
    # 猜测），而是 None —— 「没有提示」，窗口判定一律交给实测/显式声明的结论。
    assert detect_context_window("totally-unknown-model-xyz") is None


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


# ========== 实测输出上限优先于登记表（issue #152）==========
# #147 起，网关自己报过的输出上限已经存在 output_caps 的进程内记忆里；本段钉住
# 「实测优先」这条口径（与 #55 把窗口登记表降级成提示是同一条路子）。

MEASURED_GW = "https://api.commandcode.ai/provider/v1"
LONGCAT = "meituan/LongCat-2.0:free"


def _with_measured_ceiling(base_url, model, ceiling):
    """在「网关报过一个上限」的状态下求值，求完立刻清干净，别污染同文件其他用例。"""
    clear_output_ceiling_memo()
    remember_output_ceiling_for_test(base_url, model, ceiling)
    try:
        return detect_max_output_tokens(model, base_url)
    finally:
        clear_output_ceiling_memo()


def test_a_measured_ceiling_below_the_registered_hint_wins():
    # 实测比登记表小：按登记表规划分段会被上游拒，所以小的那个才算数
    assert _with_measured_ceiling(MEASURED_GW, "deepseek-v4", 16384) == 16384


def test_a_measured_ceiling_lifts_an_unregistered_model_off_the_default():
    # 未登记模型本落到保守 8192；网关报过 131072 之后不该再按 8192 规划
    assert _with_measured_ceiling(MEASURED_GW, LONGCAT, 131072) == 131072


def test_measurement_is_scoped_to_the_gateway_that_reported_it():
    clear_output_ceiling_memo()
    remember_output_ceiling_for_test(MEASURED_GW, LONGCAT, 131072)
    try:
        assert detect_max_output_tokens(LONGCAT, "https://other.test/v1") == _DEFAULT_MAX_OUTPUT_TOKENS
        assert detect_max_output_tokens(LONGCAT, MEASURED_GW) == 131072
    finally:
        clear_output_ceiling_memo()


def test_registry_and_default_still_answer_when_nothing_has_been_measured():
    clear_output_ceiling_memo()
    assert detect_max_output_tokens("gpt-4o", MEASURED_GW) == _KNOWN_OUTPUT_LIMITS["gpt-4o"]
    assert detect_max_output_tokens("brand-new-model", MEASURED_GW) == _DEFAULT_MAX_OUTPUT_TOKENS
    assert detect_max_output_tokens("gpt-4o") == 16384  # base_url 省略时不去查记忆
