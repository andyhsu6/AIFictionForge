"""章节输出预算契约测试：预算由**实测能力**决定，不按模型名/登记表/目标字数决定。

背景：调用点曾各自 `max(2000, min(target*3, 16000))` 硬算 max_tokens 并显式传入，
绕过了网关实测上限与上下文剩余（2026-11-08 批量第 58 章：6000 字目标、181068 字符
输入、思考型模型，预算被钉在 16000，推理吃光后正文 0 字）。

新契约：`max_tokens = max(1024, min(cap, context_room))`，其中
`cap = 实测输出上限 > 窗口剩余 > THINKING_MODEL_DEFAULT_MAX_TOKENS`。
`target_chars` 只作为信息与日志归因，不再参与计算。
"""
import inspect
import pathlib
import re

import pytest

from app.services.ai_clients.output_caps import (
    clear_output_ceiling_memo,
    learn_output_ceiling,
)
from app.services.ai_service import (
    THINKING_MODEL_DEFAULT_MAX_TOKENS,
    _OUTPUT_BUDGET_CONTEXT_MARGIN,
    _OUTPUT_BUDGET_MIN_TOKENS,
    plan_output_budget,
)

WINDOW_1M = 1_048_576
INPUT_TOKENS = 181_068
ROOM_1M = WINDOW_1M - INPUT_TOKENS - _OUTPUT_BUDGET_CONTEXT_MARGIN


@pytest.fixture(autouse=True)
def _clean_memo():
    clear_output_ceiling_memo()
    yield
    clear_output_ceiling_memo()


def test_budget_is_identical_across_model_names():
    """核心守卫：预算判定与模型名无关（无 "glm"/"gpt-4o" 之类的名字分支）。"""
    kwargs = dict(
        target_chars=6000,
        input_tokens=INPUT_TOKENS,
        context_window=WINDOW_1M,
    )
    results = [
        plan_output_budget(model="z-ai/glm-5.3-flash", **kwargs),
        plan_output_budget(model="some-brand-new-model-9000", **kwargs),
        plan_output_budget(model=None, **kwargs),
    ]

    budgets = {max_tokens for max_tokens, _ in results}
    assert budgets == {ROOM_1M}, budgets
    for _, breakdown in results:
        assert breakdown["context_room"] == ROOM_1M
        assert breakdown["measured_cap"] is None


def test_one_million_window_gives_exact_window_room():
    """1M 窗口 + 181068 输入 → 预算 = 窗口 - 输入 - 2048（精确值）。"""
    max_tokens, breakdown = plan_output_budget(
        target_chars=6000,
        model="z-ai/glm-5.3-flash",
        base_url="https://api.commandcode.ai/provider/v1",
        input_tokens=INPUT_TOKENS,
        context_window=WINDOW_1M,
    )

    assert ROOM_1M == 865460
    assert max_tokens == 1_048_576 - 181_068 - 2048
    assert breakdown["context_room"] == max_tokens
    assert breakdown["cap"] == max_tokens
    assert breakdown["window"] == WINDOW_1M
    assert breakdown["input_tokens"] == INPUT_TOKENS


def test_measured_output_ceiling_clamps_budget():
    """网关报过的输出上限优先：窗口剩余再大也被钳到实测值。"""
    base_url = "https://api.commandcode.ai/provider/v1"
    model = "meituan/LongCat-2.0:free"
    learned = learn_output_ceiling(
        base_url=base_url,
        model=model,
        body="/max_tokens: 865460 is not less or equal to 400000",
        requested_max_tokens=ROOM_1M,
    )
    assert learned == 400000

    max_tokens, breakdown = plan_output_budget(
        target_chars=6000,
        model=model,
        base_url=base_url,
        input_tokens=INPUT_TOKENS,
        context_window=WINDOW_1M,
    )

    assert breakdown["measured_cap"] == 400000
    assert breakdown["cap"] == 400000
    assert max_tokens == 400000


def test_without_probed_window_falls_back_to_thinking_default():
    """未探测窗口/输入 → cap 回退 THINKING_MODEL_DEFAULT_MAX_TOKENS。"""
    max_tokens, breakdown = plan_output_budget(target_chars=6000, model="gpt-4o")

    assert breakdown["context_room"] is None
    assert breakdown["window"] is None
    assert breakdown["input_tokens"] is None
    assert breakdown["cap"] == THINKING_MODEL_DEFAULT_MAX_TOKENS
    assert max_tokens == THINKING_MODEL_DEFAULT_MAX_TOKENS


def test_input_over_window_is_clamped_to_floor_and_keeps_negative_signal():
    """room <= 0（输入已超窗）→ 预算压到下限，breakdown 保留非正数作为超窗信号。"""
    max_tokens, breakdown = plan_output_budget(
        target_chars=6000,
        model="gpt-4o",
        input_tokens=200_000,
        context_window=128_000,
    )

    assert breakdown["context_room"] == 128_000 - 200_000 - 2048
    assert breakdown["context_room"] <= 0
    assert max_tokens == _OUTPUT_BUDGET_MIN_TOKENS


def test_small_target_chars_still_gets_full_window_room():
    """目标字数很小也不按它预测预算：预算仍是窗口剩余，不是 500。"""
    max_tokens, breakdown = plan_output_budget(
        target_chars=500,
        model="some-brand-new-model-9000",
        input_tokens=INPUT_TOKENS,
        context_window=WINDOW_1M,
    )

    assert breakdown["content"] == _OUTPUT_BUDGET_MIN_TOKENS
    assert max_tokens == ROOM_1M
    assert max_tokens != 500


def test_malformed_inputs_are_tolerated():
    """畸形输入：目标/窗口/输入非法时降级为安全值，不抛错、不出负数。"""
    max_tokens, breakdown = plan_output_budget(
        target_chars=-5,
        model=None,
        input_tokens="not-a-number",
        context_window="abc",
    )

    assert breakdown["content"] == _OUTPUT_BUDGET_MIN_TOKENS
    assert breakdown["context_room"] is None
    assert max_tokens == THINKING_MODEL_DEFAULT_MAX_TOKENS

    # 负数/非正输入不构成窗口约束
    negative_tokens, negative = plan_output_budget(
        target_chars=3000,
        model="gpt-4o",
        input_tokens=-100,
        context_window=10000,
    )
    assert negative["context_room"] is None
    assert negative_tokens == THINKING_MODEL_DEFAULT_MAX_TOKENS

    # 非正窗口同样不构成约束
    zero_window_tokens, zero_window = plan_output_budget(
        target_chars=3000,
        model="gpt-4o",
        input_tokens=100,
        context_window=0,
    )
    assert zero_window["context_room"] is None
    assert zero_window_tokens == THINKING_MODEL_DEFAULT_MAX_TOKENS


# --------------------------------------------------------------------------
# 接线守卫：预算模块本身对，但调用点漏传 window/input 就等于退回"按目标字数猜"。
# 只断言源码级形状（参考 test_agent_prompt_budget.py 的 inspect/读文件风格）。
# --------------------------------------------------------------------------

CHAPTERS_PY = pathlib.Path(__file__).resolve().parents[1] / "app" / "api" / "chapters.py"

#: 「每处调用点必须在调用行及其后 8 行内同时出现这两个关键字参数」的窗口。
_CALL_ARG_WINDOW = 8


def test_every_plan_output_budget_call_point_wires_context_window_and_input_tokens():
    """chapters.py 的章节生成调用点必须喂实测窗口与输入量；唯一例外是重写的降级分支。

    漏传 `context_window` ⇒ 预算退回 `THINKING_MODEL_DEFAULT_MAX_TOKENS`（正是
    2026-11-08 批量第 58 章空正文的形态）；漏传 `input_tokens` ⇒ 剩余空间算不出来。

    issue #276 后章节生成仍有 3 处主调用点走 `resolve_effective_window_tokens` +
    `plan_output_budget`；另有一处**故意的**降级调用 —— 局部重写在
    `resolve_output_budget` 抛错（能力不可得）时退回保守上限，它本来就没有窗口可用，
    所以只断言它确实位于 except 分支内、且不是「悄悄换成按目标字数硬算」。
    """
    lines = CHAPTERS_PY.read_text(encoding="utf-8").splitlines()
    call_rows = [
        idx
        for idx, line in enumerate(lines)
        if "plan_output_budget(" in line and not line.lstrip().startswith("#")
    ]
    assert len(call_rows) == 4, (
        f"预期 4 处调用点（流式/后台/批量 + 重写降级），实际 {len(call_rows)} 处: "
        f"{[row + 1 for row in call_rows]}"
    )

    primary_rows, fallback_rows = [], []
    for row in call_rows:
        window = "\n".join(lines[row : row + _CALL_ARG_WINDOW + 1])
        if "context_window=window_tokens" in window:
            primary_rows.append(row)
            # 必须字面绑定到变量，不接受 `context_window=None` 这类「传了但没值」的形态：
            # 后者能骗过子串检查，却让预算退回保守上限（变异实测存活，故收窄）。
            assert "input_tokens=input_tokens_est" in window, (
                f"chapters.py:{row + 1} 的 plan_output_budget 调用没有喂估算的输入 token"
            )
        else:
            fallback_rows.append(row)

    assert len(primary_rows) == 3, (
        f"预期 3 处主调用点喂了实测窗口，实际 {len(primary_rows)} 处: "
        f"{[row + 1 for row in primary_rows]}"
    )
    assert len(fallback_rows) == 1, (
        f"预期只有 1 处无窗口的降级调用，实际 {[row + 1 for row in fallback_rows]}"
    )
    fallback_row = fallback_rows[0]
    fallback_context = "\n".join(lines[max(0, fallback_row - _CALL_ARG_WINDOW) : fallback_row + 1])
    assert "except Exception" in fallback_context, (
        f"chapters.py:{fallback_row + 1} 的无窗口调用必须位于能力不可得的 except 分支内"
    )
    assert "max(" not in "\n".join(
        lines[fallback_row + 1 : fallback_row + _CALL_ARG_WINDOW + 1]
    ), f"chapters.py:{fallback_row + 1} 又按目标字数硬算 max_tokens 了"


def test_chapters_has_no_local_max_tokens_hardcoding_left():
    """旧的本地硬算 `max(2000, min(...))` 必须彻底消失（它绕过实测上限与窗口剩余）。"""
    source = CHAPTERS_PY.read_text(encoding="utf-8")
    assert not re.search(r"max\(\s*2000\s*,\s*min\(", source), (
        "chapters.py 里又出现了 max(2000, min(...)) 本地硬算 max_tokens"
    )


def test_plan_output_budget_does_not_reintroduce_name_or_table_branches():
    """预算函数体不得回读登记表/思考型判定：那会把"能力无关"重新变成名字分支。"""
    body = inspect.getsource(plan_output_budget)
    for symbol in (
        "_KNOWN_OUTPUT_LIMITS",
        "detect_max_output_tokens",
        "is_thinking_model",
    ):
        assert symbol not in body, (
            f"plan_output_budget 函数体引用了 {symbol} —— 预算又按模型名/登记表猜了"
        )


def test_every_generation_path_appends_the_style_tail_block():
    """三条生成路径都必须把实测「写作规格」贴到提示词末尾（issue #279 后续）。

    变异实测：把 ``_append_style_tail`` 改成直接返回 prompt（不追加）时，行为测试
    全绿 —— 因为「模型是否听话」无法在单测里断言。这里用源码守卫钉住接线本身：
    少了任何一处，模型就会退回被 12 万 token 上下文稀释的短句腔。

    只匹配到参数列表的第一个实参：全章生成路径在 `style_content` 之后还传
    `target_word_count`（字数下限尾注），续写/局部重写不传。
    """
    source = CHAPTERS_PY.read_text(encoding="utf-8")
    wiring = "prompt = _append_style_tail(prompt, style_content"
    assert source.count(wiring) == 5, (
        "预期 5 处生成路径（SSE/后台/批量/续写/局部重写）都接线，"
        f"实际 {source.count(wiring)} 处"
    )


def test_every_chapter_path_injects_the_established_names_block():
    """三条章节生成路径都必须注入「已确立专名」块。

    变异实测（同类守卫）：删掉注入不会让任何行为测试失败，只会让模型重新开始
    自造地名并沿分析链路进入正史——所以用电到源码守卫钉住。
    """
    source = CHAPTERS_PY.read_text(encoding="utf-8")
    assert source.count("established_names = await build_established_names_block(") == 3, (
        "预期 3 处章节生成路径注入专名块，实际 "
        f"{source.count('established_names = await build_established_names_block(')} 处"
    )
