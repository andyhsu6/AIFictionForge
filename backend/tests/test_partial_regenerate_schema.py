"""PartialRegenerateRequest/Response schema 特征化测试（W2/B1）。

基线部分固定改动前的现有行为（rewrite 调用方字节兼容）；
新增部分固定 continue 字段的可选契约。
"""
import pytest
from pydantic import ValidationError

from app.schemas.chapter import PartialRegenerateRequest


def _minimal_request(**overrides):
    data = {
        "selected_text": "x",
        "start_position": 0,
        "end_position": 1,
        "user_instructions": "y",
    }
    data.update(overrides)
    return PartialRegenerateRequest(**data)


# ---------- baseline: current behavior (must pass on unchanged schema) ----------

def test_baseline_minimal_defaults():
    req = _minimal_request()
    assert req.length_mode == "similar"
    assert req.context_chars == 500
    assert req.target_word_count is None


def test_baseline_target_word_count_below_10_rejected():
    with pytest.raises(ValidationError):
        _minimal_request(target_word_count=9)


def test_baseline_target_word_count_min_bound_kept():
    assert _minimal_request(target_word_count=10).target_word_count == 10


def test_baseline_existing_constraints_unchanged():
    with pytest.raises(ValidationError):
        _minimal_request(start_position=-1)
    with pytest.raises(ValidationError):
        _minimal_request(context_chars=99)
    with pytest.raises(ValidationError):
        _minimal_request(context_chars=2001)
    with pytest.raises(ValidationError):
        _minimal_request(user_instructions="")


# ---------- new contract: optional continue fields ----------

def test_continue_defaults_keep_rewrite_compatible():
    req = _minimal_request()
    assert req.mode == "rewrite"
    assert req.segment_index == 0
    assert req.already_generated_chars == 0
    assert req.rolling_context is None
    assert req.content_hash is None


def test_target_word_count_6000_now_accepted():
    assert _minimal_request(target_word_count=6000).target_word_count == 6000


def test_mode_continue_accepted():
    req = _minimal_request(
        mode="continue",
        segment_index=3,
        already_generated_chars=1200,
        rolling_context="上一段结尾……",
        content_hash="abc123",
    )
    assert req.mode == "continue"
    assert req.segment_index == 3
    assert req.already_generated_chars == 1200
    assert req.rolling_context == "上一段结尾……"
    assert req.content_hash == "abc123"


def test_mode_invalid_value_rejected():
    with pytest.raises(ValidationError):
        _minimal_request(mode="foo")


def test_segment_index_negative_rejected():
    with pytest.raises(ValidationError):
        _minimal_request(segment_index=-1)


def test_already_generated_chars_negative_rejected():
    with pytest.raises(ValidationError):
        _minimal_request(already_generated_chars=-1)
