"""PR-0c：助手 prompt 预算按实测窗口分层。

单位链（务必读完整再改数字）：
    get_effective_context_window() -> token
      -> x CHARS_PER_TOKEN -> 字符
      -> x ratio -> clamp(min, max) -> history_budget_chars
任何一环都不允许出现第二套系数或第二处 clamp。
"""
import pytest

from app.core.errors import ApiError
from app.services import agent_prompt_budget as apb
from app.services.agent_prompt_budget import (
    CHARS_PER_TOKEN,
    HISTORY_BUDGET_ANCHOR_CAP_CHARS,
    compute_history_budget_chars,
    resolve_history_budget_chars,
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


@pytest.mark.anyio
async def test_resolver_passes_the_probed_window_through_the_single_formula(monkeypatch):
    seen = {}

    async def fake_probe(user_id, model, db, *, provider=None, base_url=None):
        seen.update(
            user_id=user_id, model=model, db=db, provider=provider, base_url=base_url
        )
        return 1_000_000

    monkeypatch.setattr(apb, "get_effective_context_window", fake_probe)
    budget = await resolve_history_budget_chars(
        user_id="u1", model="m1", db=object(), provider="openai",
        base_url="https://gw.example/v1",
    )
    assert budget == 300_000
    # 三元组必须原样送达：省略 provider/base_url 会让"同名模型挂两个网关"的用户
    # 每次发 prompt 都吃一个 validation.ai_model_below_minimum（契约见锚点复核）
    assert seen == {
        "user_id": "u1",
        "model": "m1",
        "db": seen["db"],
        "provider": "openai",
        "base_url": "https://gw.example/v1",
    }


def test_provider_and_base_url_are_mandatory_keywords():
    # 不给默认值 ⇒ 漏传就是 TypeError，而不是悄悄走"按模型名查缓存"的歧义路径
    with pytest.raises(TypeError):
        resolve_history_budget_chars(user_id="u", model="m", db=None)  # type: ignore[call-arg]


@pytest.mark.anyio
async def test_probe_failure_bubbles_up_untouched(monkeypatch):
    """预算算不出来时必须**明确报错**，禁止静默退回 60000。

    也不要 except Exception 再包一层 —— 那会把门禁的错误码文案吃掉。
    """

    async def boom(user_id, model, db, *, provider=None, base_url=None):
        raise ApiError(
            code="validation.ai_model_below_minimum",
            params={"model": model, "min_window": 1_000_000},
        )

    monkeypatch.setattr(apb, "get_effective_context_window", boom)
    with pytest.raises(ApiError) as exc:
        await resolve_history_budget_chars(
            user_id="u1", model="gpt-4", db=object(), provider="openai", base_url=""
        )
    assert exc.value.code == "validation.ai_model_below_minimum"
    assert exc.value.code != "internal.error"  # 没被泛化成通用失败


@pytest.mark.anyio
async def test_trace_is_filled_with_tokens_and_budget(monkeypatch):
    async def fake_probe(user_id, model, db, *, provider=None, base_url=None):
        return 2_000_000

    monkeypatch.setattr(apb, "get_effective_context_window", fake_probe)
    trace = apb.PromptBudgetTrace(budget_chars=0)
    budget = await resolve_history_budget_chars(
        user_id="u", model="m", db=object(), provider="openai", base_url="b",
        trace=trace,
    )
    assert budget == 400_000  # 2M x 0.3 = 600000 -> 上限 400000
    assert (trace.effective_tokens, trace.budget_chars) == (2_000_000, 400_000)


def test_config_defaults_match_the_module_constants():
    """四个键的默认值必须与模块常量逐一对齐 —— 这条断言就是防漂移的门。"""
    from app.config import settings

    assert settings.agent_chars_per_token == CHARS_PER_TOKEN
    assert settings.agent_history_budget_ratio == apb.HISTORY_BUDGET_RATIO
    assert settings.agent_history_budget_min_chars == apb.HISTORY_BUDGET_MIN_CHARS
    assert settings.agent_history_budget_max_chars == apb.HISTORY_BUDGET_MAX_CHARS


def test_settings_can_tune_the_ratio(monkeypatch):
    monkeypatch.setattr(apb, "HISTORY_BUDGET_RATIO", 0.5, raising=False)
    assert compute_history_budget_chars(1_000_000, ratio=0.5) == 400_000
