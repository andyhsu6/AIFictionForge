"""拆书章节原文 excerpt 构建测试（Tier2 喂全文）。

角色/组织生成从"只喂世界观摘要"升级为"追加注入已入库章节原文 excerpt"，
让 AI 基于真实剧情生成更准确的角色。excerpt 构建与关系抽取对齐：
每章截取前 N 字符，按章节号正序拼接。
"""
from app.services.book_import_service import BookImportService


def _chapter(n, title, content):
    return type("C", (), {"chapter_number": n, "title": title, "content": content})()


def test_build_excerpt_truncates_per_chapter():
    svc = BookImportService()
    chs = [_chapter(1, "第一章", "甲" * 5000), _chapter(2, "第二章", "乙" * 5000)]

    excerpt = svc._build_chapter_excerpt(chs, per_chapter_chars=100)

    assert "甲" * 100 in excerpt
    assert "乙" * 100 in excerpt
    assert "甲" * 101 not in excerpt  # 每章被截断


def test_build_excerpt_orders_by_chapter_number():
    svc = BookImportService()
    chs = [_chapter(3, "第三章", "丙"), _chapter(1, "第一章", "甲"), _chapter(2, "第二章", "乙")]

    excerpt = svc._build_chapter_excerpt(chs, per_chapter_chars=10)

    assert excerpt.index("甲") < excerpt.index("乙") < excerpt.index("丙")


def test_build_excerpt_skips_empty_content():
    svc = BookImportService()
    chs = [_chapter(1, "第一章", ""), _chapter(2, "第二章", "有内容")]

    excerpt = svc._build_chapter_excerpt(chs, per_chapter_chars=10)

    assert "有内容" in excerpt
    assert "第一章" not in excerpt  # 空章节被跳过


def test_build_excerpt_returns_empty_for_no_chapters():
    svc = BookImportService()
    assert svc._build_chapter_excerpt([], per_chapter_chars=100) == ""
