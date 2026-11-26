"""输出预算能力测试：预算按**实测能力**（窗口剩余 + 网关报过的输出上限）定，不按模型名。

背景（issue #276）：章内续写/局部重写原先走「模型名登记表 + 思考型名字判定」
（`detect_max_output_tokens` / `ensure_thinking_model_min_tokens`），名字错了或表过期
预算就悄悄腰斩。这两个函数与登记表已删除，统一入口是
`AIService.resolve_output_budget`（实测窗口 ⇒ `plan_output_budget`）。

本文件钉住该入口的可观测行为：
  1. 1M 窗口 + 小 prompt ⇒ 预算 ≈ 窗口剩余，且**换任意模型名结果相同**；
  2. 网关报过更小的输出上限（`learn_output_ceiling`）⇒ 预算被钳；
  3. 窗口不可得 ⇒ 入口**抛异常**（降级由调用方决定，不返回猜测值）；
  4. 递归到 `plan_output_budget` 的窗口/下限语义不变。

全部使用合成数值，不含任何真实书籍原文。
"""
import pytest

from app.core.errors import ApiError
from app.services.ai_clients.output_caps import (
    clear_output_ceiling_memo,
    learn_output_ceiling,
)
from app.services.ai_service import (
    AIService,
    THINKING_MODEL_DEFAULT_MAX_TOKENS,
    detect_context_window,
    is_thinking_model,
)

MEASURED_GW = "https://api.commandcode.ai/provider/v1"
BIG_WINDOW = 1_000_000
CONTEXT_MARGIN = 2048


def _service(base_url: str, model: str) -> AIService:
    """不发真实请求的 AIService（不绑定用户；窗口由用例 monkeypatch 提供）。"""
    return AIService(
        api_provider="openai",
        api_key="test",
        api_base_url=base_url,
        default_model=model,
    )


def _patch_window(monkeypatch, svc: AIService, window=None, error=None):
    """替掉窗口访问器：给一个实测窗口，或抛出门禁错误。"""

    async def _fake(model=None, provider=None):
        if error is not None:
            raise error
        return window

    monkeypatch.setattr(svc, "resolve_effective_window_tokens", _fake)


def _remember_output_ceiling_for_test(base_url, model, ceiling):
    """喂一条网关风格的拒绝给公开学习入口，等价于一次真实 400 学到的结果。"""
    learned = learn_output_ceiling(
        base_url=base_url,
        model=model,
        body=f"/max_tokens: 900000 is not less or equal to {ceiling}",
        requested_max_tokens=900000,
    )
    assert learned == ceiling, "夹具没学到说明拒绝文本不合判据，别让用例静默跑在空记忆上"


# ---------------------------------------------------------------------------
# 残留的相邻函数：当前可观测行为不变（预算判定已不用它们）
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
# 1. 预算 = 窗口剩余；模型名不影响结论
# ---------------------------------------------------------------------------


def test_budget_follows_measured_window_and_ignores_model_name(monkeypatch):
    prompt_chars = 5_000
    expected = BIG_WINDOW - prompt_chars - CONTEXT_MARGIN

    results = {}
    for model in ("deepseek-v4-flash", "some-brand-new-model-9000", ""):
        svc = _service(MEASURED_GW, model)
        _patch_window(monkeypatch, svc, window=BIG_WINDOW)
        max_tokens, breakdown = _run(
            svc.resolve_output_budget(
                target_chars=6000, prompt_chars=prompt_chars, model=model
            )
        )
        results[model] = max_tokens
        assert max_tokens == expected, f"{model}: 预算应等于窗口剩余"
        assert breakdown["window"] == BIG_WINDOW
        assert breakdown["input_tokens"] == prompt_chars  # CHARS_PER_TOKEN == 1.0
        assert breakdown["context_room"] == expected
        assert breakdown["measured_cap"] is None

    # 三个完全不同的模型名（含空名）得到同一个预算 ⇒ 判定里没有名字分支
    assert len(set(results.values())) == 1


def test_small_window_clamps_budget_to_remaining_room(monkeypatch):
    # 窗口 8000、输入 6000 ⇒ 剩余 8000-6000-2048 = -48（已超窗）：预算被下限钳住，
    # context_room 保持非正数作为「这段 prompt 塞不进窗口」的信号，而不是当成无约束。
    svc = _service(MEASURED_GW, "gpt-4o")
    _patch_window(monkeypatch, svc, window=8_000)
    max_tokens, breakdown = _run(
        svc.resolve_output_budget(target_chars=6000, prompt_chars=6000, model="gpt-4o")
    )
    assert breakdown["context_room"] == -48
    assert max_tokens == 1024  # _OUTPUT_BUDGET_MIN_TOKENS


# ---------------------------------------------------------------------------
# 2. 网关报过的输出上限优先（issue #147 的记忆）
# ---------------------------------------------------------------------------


def test_gateway_reported_ceiling_clamps_the_budget(monkeypatch):
    clear_output_ceiling_memo()
    _remember_output_ceiling_for_test(MEASURED_GW, "gpt-4o", 16384)
    try:
        svc = _service(MEASURED_GW, "gpt-4o")
        _patch_window(monkeypatch, svc, window=BIG_WINDOW)
        max_tokens, breakdown = _run(
            svc.resolve_output_budget(target_chars=6000, prompt_chars=5_000, model="gpt-4o")
        )
    finally:
        clear_output_ceiling_memo()

    assert breakdown["measured_cap"] == 16384
    assert breakdown["cap"] == 16384
    assert max_tokens == 16384  # 即便窗口还剩近 1M，也按上游确认过的上限规划


def test_ceiling_memory_is_scoped_to_the_gateway_that_reported_it(monkeypatch):
    clear_output_ceiling_memo()
    _remember_output_ceiling_for_test(MEASURED_GW, "gpt-4o", 16384)
    try:
        other = _service("https://other.test/v1", "gpt-4o")
        _patch_window(monkeypatch, other, window=BIG_WINDOW)
        max_tokens, breakdown = _run(
            other.resolve_output_budget(target_chars=6000, prompt_chars=5_000, model="gpt-4o")
        )
    finally:
        clear_output_ceiling_memo()

    assert breakdown["measured_cap"] is None
    assert max_tokens == BIG_WINDOW - 5_000 - CONTEXT_MARGIN


# ---------------------------------------------------------------------------
# 3. 窗口不可得 ⇒ 抛异常（失败契约，降级归调用方）
# ---------------------------------------------------------------------------


def test_window_failure_propagates_to_the_caller(monkeypatch):
    svc = _service(MEASURED_GW, "gpt-4o")
    _patch_window(
        monkeypatch,
        svc,
        error=ApiError(
            code="validation.ai_model_below_minimum", params={"model": "gpt-4o"}
        ),
    )

    with pytest.raises(ApiError) as excinfo:
        _run(svc.resolve_output_budget(target_chars=6000, prompt_chars=500, model="gpt-4o"))
    assert excinfo.value.code == "validation.ai_model_below_minimum"


def test_unexpected_window_error_also_propagates(monkeypatch):
    # 非门禁异常（DB 抖动/解析 bug）不能被吞成某个具体预算
    svc = _service(MEASURED_GW, "gpt-4o")
    _patch_window(monkeypatch, svc, error=RuntimeError("db down"))

    with pytest.raises(RuntimeError):
        _run(svc.resolve_output_budget(target_chars=6000, prompt_chars=500, model="gpt-4o"))


# ---------------------------------------------------------------------------
# 4. 名字登记表机制已删除（issue #276）
# ---------------------------------------------------------------------------


def test_name_based_output_registry_is_gone():
    import app.services.ai_service as module

    for symbol in (
        "detect_max_output_tokens",
        "ensure_thinking_model_min_tokens",
        "_KNOWN_OUTPUT_LIMITS",
        "_DEFAULT_MAX_OUTPUT_TOKENS",
    ):
        assert not hasattr(module, symbol), (
            f"{symbol} 又回来了 —— 输出预算必须走实测能力，不能按模型名猜"
        )


def _run(awaitable):
    """在同步用例里驱动一次 async 调用（本文件不需要事件循环夹具）。"""
    import asyncio

    return asyncio.run(awaitable)
