"""Tier3 全书注入上下文构建测试（拆分优先：预算内全量 / 超预算三级摘要链）。

章节生成从"最近10章摘要+10条记忆"升级为"全书注入"：
- 全书字符数 ≤ 预算 → 全量注入（1M 模型直接可用），单章超长不截断
- 超预算 → head 全文 + 尾部加权全文 + 中间被丢弃章节走存量摘要链
- 单章本身超预算 → 该章改用摘要链条目，不做硬截断
"""
import pytest

from app.services.chapter_context_service import OneToManyContextBuilder


class _Row:
    def __init__(self, num, title, content, summary=None, expansion_plan=None):
        self.chapter_number = num
        self.title = title
        self.content = content
        self.summary = summary
        self.expansion_plan = expansion_plan


def _make_builder():
    return OneToManyContextBuilder()


def test_full_book_context_all_chapters_within_budget():
    builder = _make_builder()
    chapters = [_Row(1, "第一章", "甲" * 100), _Row(2, "第二章", "乙" * 100)]

    text = builder._build_full_book_context(chapters, budget_chars=1000)

    assert "第一章" in text and "第二章" in text
    assert "甲" * 100 in text and "乙" * 100 in text  # 全量


def test_full_book_context_over_budget_keeps_head_and_tail():
    """超预算时保留头部（早期设定）与尾部（近期剧情），中间进摘要链。"""
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


def test_full_book_context_single_huge_chapter_not_truncated():
    """单章超长且全书在预算内 → 全文注入，不截断。"""
    builder = _make_builder()
    chapters = [_Row(1, "第一章", "甲" * 50000)]

    text = builder._build_full_book_context(chapters, budget_chars=60000)

    assert len(text) >= 50000  # 全文保留，未截断
    assert "第一章" in text


def test_full_book_context_single_huge_chapter_over_budget_uses_summary():
    """单章本身超预算 → 该章改用摘要链条目，不做硬截断。"""
    builder = _make_builder()
    chapters = [_Row(1, "第一章", "甲" * 50000, summary="开篇设定摘要")]

    text = builder._build_full_book_context(chapters, budget_chars=1000)

    assert "开篇设定摘要" in text  # 摘要条目代替全文
    assert "甲" * 50000 not in text  # 没有超预算的截断全文
    assert len(text) <= 1000 * 1.2


def test_full_book_context_middle_chapters_become_summary_chain():
    """超预算时中间被丢弃章节以摘要链呈现（每章一行，纯存量字段）。"""
    builder = _make_builder()
    # 首尾小章 + 中间 3 章较大，超预算 → 中间走摘要链
    chapters = [
        _Row(1, "第一章", "甲" * 200, summary="开篇"),
        _Row(2, "第二章", "乙" * 2000, summary="第二章摘要", expansion_plan='{"plot_summary":"第二章情节点","key_events":["事件B1","事件B2"]}'),
        _Row(3, "第三章", "丙" * 2000, summary="第三章摘要"),
        _Row(4, "第四章", "丁" * 2000, summary="第四章摘要"),
        _Row(5, "第五章", "戊" * 200, summary="结尾"),
    ]

    text = builder._build_full_book_context(chapters, budget_chars=1000)

    # head 全文 + 尾部全文保留
    assert "甲" * 200 in text and "戊" * 200 in text
    # 中间章节以摘要链呈现（不再丢信息）
    assert "【中间章节摘要链】" in text
    assert "第二章情节点" in text  # expansion_plan 优先于 summary
    assert "关键事件：事件B1；事件B2" in text
    assert "第三章摘要" in text and "第四章摘要" in text
    assert "乙" * 2000 not in text  # 中间章全文不注入
    assert len(text) <= 1000 * 1.2


def test_full_book_context_summary_chain_tail_preference():
    """摘要链放不下全部中间章时，保留最近的中间章摘要（tail 优先延续）。"""
    builder = _make_builder()
    chapters = [
        _Row(1, "第一章", "甲" * 300),
        _Row(2, "第二章", "乙" * 500, summary="第二章摘要"),
        _Row(3, "第三章", "丙" * 500, summary="第三章摘要"),
        _Row(4, "第四章", "丁" * 500, summary="第四章摘要"),
        _Row(5, "第五章", "戊" * 300),
    ]

    text = builder._build_full_book_context(chapters, budget_chars=625)

    # 预算极紧：head + 尾部全文（约620字符）后剩余不足，摘要链仅保留最近的中间章
    assert "第四章" in text and "第四章摘要" in text
    assert "第二章" not in text  # 最远的中间章摘要被裁剪（tail 优先）
    assert len(text) <= 625 * 1.2


def test_full_book_context_tail_skip_instead_of_break():
    """超预算章节跳过（continue），继续尝试更早的小章节，跳过章走摘要链。"""
    builder = _make_builder()
    # 第5章超大，但第4/3章小——continue 保留 3/4 章，第5章走摘要链
    chapters = [
        _Row(1, "第一章", "甲" * 100),
        _Row(2, "第二章", "乙" * 100),
        _Row(3, "第三章", "丙" * 100),
        _Row(4, "第四章", "丁" * 100),
        _Row(5, "第五章", "戊" * 10000, summary="第五章摘要"),  # 超长
    ]

    text = builder._build_full_book_context(chapters, budget_chars=1000, tail_chapters=5)

    # 第4/3章全文仍保留
    assert "第四章" in text
    assert "第三章" in text
    # 第5章超预算，全文不注入，改走摘要链
    assert "戊" * 10000 not in text
    assert "第五章摘要" in text
    assert len(text) <= 1000 * 1.2
