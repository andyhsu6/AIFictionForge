"""Tier3 全书注入上下文构建测试（双模式：预算内全量 / 超预算尾部加权）。

章节生成从"最近10章摘要+10条记忆"升级为"全书注入"：
- 全书字符数 ≤ 预算 → 全量注入（1M 模型直接可用）
- 超预算 → 尾部加权（保留最后 N 章全文）+ 头部保留（伏笔/世界观在早期章节）
"""
import pytest

from app.services.chapter_context_service import OneToManyContextBuilder


class _Row:
    def __init__(self, num, title, content):
        self.chapter_number = num
        self.title = title
        self.content = content


def _make_builder():
    return OneToManyContextBuilder()


def test_full_book_context_all_chapters_within_budget():
    builder = _make_builder()
    chapters = [_Row(1, "第一章", "甲" * 100), _Row(2, "第二章", "乙" * 100)]

    text = builder._build_full_book_context(chapters, budget_chars=1000)

    assert "第一章" in text and "第二章" in text
    assert "甲" * 100 in text and "乙" * 100 in text  # 全量


def test_full_book_context_over_budget_keeps_head_and_tail():
    """超预算时保留头部（早期设定）与尾部（近期剧情），丢弃中间。"""
    builder = _make_builder()
    chapters = [_Row(i, f"第{i}章", f"内容{i}" * 100) for i in range(1, 21)]  # 20章

    text = builder._build_full_book_context(chapters, budget_chars=3000)

    # 头部保留（第一章世界观/伏笔）
    assert "第1章" in text
    # 尾部保留（最近剧情）
    assert "第20章" in text
    # 总长受预算约束（加安全余量）
    assert len(text) <= 3000 * 1.2


def test_full_book_context_empty_input():
    builder = _make_builder()
    assert builder._build_full_book_context([], budget_chars=1000) == ""
