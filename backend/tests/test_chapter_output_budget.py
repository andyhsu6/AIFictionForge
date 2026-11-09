"""章节输出预算单一来源测试（2026-11-08 批量第 58 章空正文回归）。

背景：调用点曾各自 `max(2000, min(target*3, 16000))` 硬算 max_tokens 并显式传入，
绕过了 `resolve_effective_max_tokens` 的思考型升级 / 网关实测上限 / 上下文剩余。
现在预算统一由 `plan_output_budget` 给出。
"""
import pytest

from app.api.chapters import (
    ChapterGenerationTruncatedError,
    NonRetryableChapterGenerationError,
    _is_budget_retry_exhausted,
    _is_non_retryable_truncation,
    _is_truncated_chapter_content,
    _next_budget_after_truncation,
    _reasoning_chars_from_usage,
    _retry_allowance,
    _truncation_error_message,
)
from app.services.ai_service import plan_output_budget
from app.services.ai_metrics import TokenUsage


def test_plain_model_has_no_reasoning_reserve():
    """① 普通模型：reserve=0，预算=content+slack（未触上限）。"""
    max_tokens, breakdown = plan_output_budget(target_chars=6000, model="gpt-4o")

    assert breakdown["content"] == 6000
    assert breakdown["reserve"] == 0
    assert breakdown["cap"] == 16384
    assert breakdown["context_room"] is None
    assert breakdown["requested"] == 6000 + 0 + 2048
    assert max_tokens == 8048


def test_thinking_model_reserves_room_for_reasoning():
    """② 思考型模型：reasoning 与正文共享预算，reserve=max(content, 8192)。"""
    max_tokens, breakdown = plan_output_budget(
        target_chars=6000, model="deepseek-v4-flash"
    )

    assert breakdown["reserve"] == 8192
    assert breakdown["cap"] == 64000
    assert breakdown["requested"] == 6000 + 8192 + 2048
    assert max_tokens == 16240


def test_cap_clamps_requested_budget():
    """③ cap 生效：期望值超过模型输出上限时按 cap 截断。"""
    max_tokens, breakdown = plan_output_budget(target_chars=100000, model="gpt-4o")

    assert breakdown["requested"] > breakdown["cap"]
    assert max_tokens == breakdown["cap"] == 16384


def test_context_room_clamps_budget():
    """④ context_room 生效：窗口 - 输入 - margin 小于期望值时按剩余空间截断。"""
    max_tokens, breakdown = plan_output_budget(
        target_chars=3000,
        model="gpt-4o",
        input_tokens=6000,
        context_window=10000,
    )

    assert breakdown["context_room"] == 10000 - 6000 - 2048
    assert max_tokens == 1952


def test_malformed_inputs_are_tolerated():
    """⑤ 畸形输入：任务/窗口/输入非法时降级为安全值，不抛错、不出负数。"""
    max_tokens, breakdown = plan_output_budget(
        target_chars=-5,
        model=None,
        input_tokens="not-a-number",
        context_window="abc",
    )

    assert breakdown["content"] == 1024
    assert breakdown["context_room"] is None
    assert max_tokens == max(1024, 1024 + 0 + 2048)

    # 窗口为正但剩余 <= 0 → 约束生效：预算压到下限，breakdown 里保留负数（超窗信号）
    zero_room_tokens, zero_room = plan_output_budget(
        target_chars=3000, model="gpt-4o", input_tokens=9000, context_window=10000
    )
    assert zero_room["context_room"] == 10000 - 9000 - 2048
    assert zero_room_tokens == 1024

    # 输入远超窗口（审核给的实测口径）：同样是「超窗」而不是「无约束」
    over_tokens, over = plan_output_budget(
        target_chars=6000, model="gpt-4o", input_tokens=200000, context_window=128000
    )
    assert over["context_room"] == 128000 - 200000 - 2048
    assert over_tokens == 1024

    # 负数/非正输入仍不参与约束
    negative_tokens, negative = plan_output_budget(
        target_chars=3000, model="gpt-4o", input_tokens=-100, context_window=10000
    )
    assert negative["context_room"] is None
    assert negative_tokens == 5048


def test_glm_output_cap_matches_measured_value():
    """实测登记：commandcode + glm 接受 max_tokens=200000，登记表不得再写 8192。"""
    max_tokens, breakdown = plan_output_budget(
        target_chars=6000,
        model="z-ai/glm-5.3-flash",
        base_url="https://api.commandcode.ai/provider/v1",
    )

    assert breakdown["cap"] == 200000
    assert breakdown["reserve"] == 8192  # commandcode 通道按思考型处理
    assert max_tokens == 16240


def test_truncation_detection_rejects_empty_and_length():
    """空正文一律失败；finish_reason=length 一律算截断（与目标字数占比无关）。"""
    assert _is_truncated_chapter_content("", 6000, "stop")
    assert _is_truncated_chapter_content("   \n", 6000, "length")
    assert _is_truncated_chapter_content("字" * 100, 6000, "length")
    # 已达目标 80% 仍是残稿：length 的定义就是「被输出上限切断」
    assert _is_truncated_chapter_content("字" * 4800, 6000, "length")
    # 英文按字符够长也不能放行截断
    english = "word " * 1200  # 6000 字符
    assert len(english) >= 6000
    assert _is_truncated_chapter_content(english, 6000, "length")


def test_truncation_detection_stop_short_is_success_with_warning(caplog):
    """stop + 正文偏短不判截断（短而完整是质量问题），只打 WARNING 便于统计。"""
    with caplog.at_level("WARNING"):
        assert not _is_truncated_chapter_content("字" * 2500, 6000, "stop", "第58章")

    warning_text = "\n".join(record.message for record in caplog.records)
    assert "第58章" in warning_text
    assert "2500" in warning_text and "6000" in warning_text

    # 英文长文在 stop 下同样正常
    assert not _is_truncated_chapter_content("word " * 1200, 6000, "stop")
    # length 与目标字数无关：目标未知时只要非空就仍算截断
    assert _is_truncated_chapter_content("字" * 10, 0, "length")
    assert _is_truncated_chapter_content("字" * 10, "bad", "length")
    # finish_reason 缺失时只有空正文才判失败
    assert not _is_truncated_chapter_content("字" * 10, 6000, None)


def test_reasoning_chars_estimate_from_usage():
    """推理字符只能由 usage 估算：completion_tokens - 正文字符。"""
    assert _reasoning_chars_from_usage(TokenUsage(completion_tokens=5000), 1200) == 3800
    assert _reasoning_chars_from_usage(TokenUsage(completion_tokens=100), 1200) is None
    assert _reasoning_chars_from_usage(TokenUsage(), 1200) is None
    assert _reasoning_chars_from_usage(None, 1200) is None


def test_token_usage_parses_reasoning_tokens():
    """reasoning_tokens 从 completion_tokens_details 解析；缺失/脏值 → None。"""
    usage = TokenUsage.from_response(
        {
            "usage": {
                "prompt_tokens": 181068,
                "completion_tokens": 38000,
                "completion_tokens_details": {"reasoning_tokens": 37678},
            }
        }
    )
    assert usage.reasoning_tokens == 37678

    # 缺 completion_tokens_details / 子字段缺失 / 脏值
    assert TokenUsage.from_response({"usage": {"completion_tokens": 10}}).reasoning_tokens is None
    assert TokenUsage.from_response(
        {"usage": {"completion_tokens_details": {}}}
    ).reasoning_tokens is None
    assert TokenUsage.from_response(
        {"usage": {"completion_tokens_details": {"reasoning_tokens": "bad"}}}
    ).reasoning_tokens is None
    # details 不是 dict（网关脏数据）时不炸
    assert TokenUsage.from_response(
        {"usage": {"completion_tokens_details": "n/a"}}
    ).reasoning_tokens is None


def test_reasoning_tokens_survive_client_usage_passthrough():
    """链路：openai_client 透传的 3+1 键 usage → from_response 得到真实推理用量。"""
    client_usage = {
        "usage": {
            "prompt_tokens": 181068,
            "completion_tokens": 38000,
            "total_tokens": 219068,
            "completion_tokens_details": {"reasoning_tokens": 37678},
        }
    }

    usage = TokenUsage.from_response(client_usage)

    assert usage.reasoning_tokens == 37678
    assert usage.completion_tokens == 38000


def test_usage_add_merges_reasoning_tokens():
    """add() 对称累加 reasoning_tokens；任一侧 None 时结果为另一侧，两侧都 None 仍为 None。"""
    merged = TokenUsage(reasoning_tokens=30000)
    merged.add(TokenUsage(reasoning_tokens=7678))
    assert merged.reasoning_tokens == 37678

    only_left = TokenUsage(reasoning_tokens=37678)
    only_left.add(TokenUsage())
    assert only_left.reasoning_tokens == 37678

    none_both = TokenUsage()
    none_both.add(TokenUsage())
    assert none_both.reasoning_tokens is None


def test_retry_budget_uses_reported_reasoning_tokens():
    """① 有正文 + 真实 reasoning_tokens：按 content + ceil(reasoning*1.5) + 2048 升级。"""
    upgraded = _next_budget_after_truncation(
        content_budget=6000,
        current_max_tokens=16240,
        cap=200000,
        reasoning_tokens=37678,
        content_chars=1200,
    )

    assert upgraded == 6000 + 56517 + 2048  # ceil(37678 * 1.5) == 56517
    assert upgraded <= 200000


def test_retry_budget_doubles_when_content_is_empty():
    """content=0 时首跳 = content + max(1.5×当前预算, ceil(reasoning*1.5)+2048)。"""
    # 实测那章：max(24360, 56517+2048=58565) = 58565 → 6000+58565 = 64565
    assert (
        _next_budget_after_truncation(
            content_budget=6000,
            current_max_tokens=16240,
            cap=200000,
            reasoning_tokens=37678,
            content_chars=0,
        )
        == 64565
    )
    # 只有 1.5× 信号时（无 reasoning_tokens → 总预算盲抬 4×：16240 → 64960）
    assert (
        _next_budget_after_truncation(
            content_budget=6000,
            current_max_tokens=16240,
            cap=200000,
            reasoning_tokens=None,
            content_chars=0,
        )
        == 4 * 16240
    )
    # reasoning 信号比 1.5× 小时取 1.5×（max 语义）
    assert (
        _next_budget_after_truncation(
            content_budget=6000,
            current_max_tokens=16240,
            cap=200000,
            reasoning_tokens=1000,
            content_chars=0,
        )
        == 6000 + 24360
    )
    # 假设真的走到第二跳：1.5× 而不是 2×（不再一步跳到 135k）
    assert (
        _next_budget_after_truncation(
            content_budget=6000,
            current_max_tokens=64565,
            cap=200000,
            reasoning_tokens=37678,
            content_chars=0,
        )
        == 6000 + 96848  # ceil(64565 * 1.5) == 96848
    )


def test_empty_content_ladder_stops_after_one_retry():
    """空正文阶梯：16240 → 64565 →（仍空）不可重试，不再升预算。"""
    first_hop = _next_budget_after_truncation(
        content_budget=6000,
        current_max_tokens=16240,
        cap=200000,
        reasoning_tokens=37678,
        content_chars=0,
    )
    assert first_hop == 64565
    assert not _is_budget_retry_exhausted(
        upgraded_budget=first_hop, budget_retries=0, content_is_empty=True
    )

    second_hop = _next_budget_after_truncation(
        content_budget=6000,
        current_max_tokens=first_hop,
        cap=200000,
        reasoning_tokens=37678,
        content_chars=0,
    )
    # 第二次仍空：判定停（预算公式本身不一步跳 135k）
    assert second_hop == 102848
    assert _is_budget_retry_exhausted(
        upgraded_budget=second_hop, budget_retries=1, content_is_empty=True
    )
    assert _is_non_retryable_truncation(
        upgraded_budget=second_hop, content_is_empty=True
    )


def test_retry_exhaustion_rules_for_non_empty_content():
    """有正文的截断不受「连续两次空正文」影响，可用满 2 次预算升级。"""
    assert not _is_budget_retry_exhausted(
        upgraded_budget=32480, budget_retries=1, content_is_empty=False
    )
    assert _is_budget_retry_exhausted(
        upgraded_budget=32480, budget_retries=2, content_is_empty=False
    )
    assert not _is_non_retryable_truncation(
        upgraded_budget=32480, content_is_empty=False
    )
    # 到 cap / 算不出更大预算 → 一律不可重试
    assert _is_budget_retry_exhausted(
        upgraded_budget=None, budget_retries=0, content_is_empty=False
    )
    assert _is_non_retryable_truncation(
        upgraded_budget=None, content_is_empty=False
    )


def test_retry_budget_falls_back_to_doubling_without_usage():
    """② 有正文但拿不到 reasoning_tokens：退化为 ×2。"""
    assert (
        _next_budget_after_truncation(
            content_budget=6000,
            current_max_tokens=16240,
            cap=200000,
            reasoning_tokens=None,
            content_chars=1200,
        )
        == 32480
    )
    # reasoning_tokens=0 与非正同样视为「无用量」
    assert (
        _next_budget_after_truncation(
            content_budget=6000,
            current_max_tokens=16240,
            cap=200000,
            reasoning_tokens=0,
            content_chars=1200,
        )
        == 32480
    )


def test_retry_budget_stops_at_cap():
    """③ 已达 cap 不再重试；公式值超过 cap 时钳到 cap。"""
    assert (
        _next_budget_after_truncation(
            content_budget=6000,
            current_max_tokens=200000,
            cap=200000,
            reasoning_tokens=37678,
            content_chars=1200,
        )
        is None
    )
    # 公式值超过 cap → 钳到 cap（仍大于当前预算，可重试）
    assert (
        _next_budget_after_truncation(
            content_budget=180000,
            current_max_tokens=16240,
            cap=200000,
            reasoning_tokens=37678,
            content_chars=1200,
        )
        == 200000
    )


def test_truncation_message_marks_estimated_reasoning():
    """估算的推理用量必须标「≈（估算）」，不得冒充实测值。"""
    message = _truncation_error_message(
        chapter_label="第60章",
        content_chars=0,
        target_word_count=6000,
        finish_reason="length",
        reasoning_tokens=311,
        reasoning_estimated=True,
        reasoning_chars=311,
        breakdown={
            "content": 6000,
            "reserve": 8192,
            "cap": 200000,
            "context_room": None,
        },
        budget_attempts=[16240, 64960],
    )

    assert "第60章" in message
    assert "reasoning_tokens≈311（估算）" in message
    assert "尝试预算=[16240, 64960]" in message


def test_retry_allowance_by_error_type():
    """③ 不可重试错误外层 0 次；截断类 1 次；其它沿用 task.max_retries。"""
    assert (
        _retry_allowance(NonRetryableChapterGenerationError("空正文"), 3) == 0
    )
    assert _retry_allowance(ChapterGenerationTruncatedError("半截"), 3) == 1
    assert _retry_allowance(ChapterGenerationTruncatedError("半截"), 0) == 0
    assert _retry_allowance(RuntimeError("其它"), 3) == 3
    # 不可重试错误是截断类的子类，判定顺序不能反
    assert issubclass(NonRetryableChapterGenerationError, ChapterGenerationTruncatedError)

