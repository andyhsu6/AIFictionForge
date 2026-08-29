"""拆书角色生成分批逻辑测试（#13 Step 3 之 D1）。

背景：单次 JSON 生成 8-20 个角色+组织导致输出过大。修复：
1) target_count 上限 20→10
2) 大批次按 batch_size 拆成多次小批次生成，降低单次输出规模
"""
import pytest

from app.services.book_import_service import BookImportService


def test_split_single_batch_when_under_batch_size():
    svc = BookImportService()
    batches = svc._split_character_batches(total=5, batch_size=6)
    assert batches == [5]


def test_split_two_batches():
    svc = BookImportService()
    batches = svc._split_character_batches(total=10, batch_size=6)
    assert batches == [6, 4]


def test_split_three_batches():
    svc = BookImportService()
    batches = svc._split_character_batches(total=17, batch_size=6)
    assert batches == [6, 6, 5]


def test_split_total_capped_at_ten():
    """D1: target_count 上限从 20 降到 10，超过 10 的一律按 10 处理。"""
    svc = BookImportService()
    assert svc._cap_character_target(15) == 10
    assert svc._cap_character_target(8) == 8
    assert svc._cap_character_target(25) == 10


def test_split_preserves_sum():
    """分批总数必须等于总目标数。"""
    svc = BookImportService()
    for total in (6, 7, 10, 12, 20):
        assert sum(svc._split_character_batches(total=total, batch_size=6)) == total
