"""拆书导入服务回归测试。

覆盖缺陷：_build_fallback_outline_structure 对空正文章节（content='' 且 summary=None）
执行 (None or None).strip() 抛 AttributeError: 'NoneType' object has no attribute 'strip'。
"""
from app.schemas.book_import import BookImportChapter
from app.services.book_import_service import BookImportService


def test_fallback_outline_structure_handles_empty_chapter():
    """空正文章节（content='' 且 summary=None）必须返回默认 summary，不抛异常。"""
    svc = BookImportService()
    chapter = BookImportChapter(title="末章", content="", chapter_number=1, summary=None)

    result = svc._build_fallback_outline_structure(chapter)

    assert isinstance(result, dict)
    assert result["summary"]  # 非空，应为默认文案
    assert result["summary"] == "本章围绕主要人物与核心冲突推进剧情。"


def test_fallback_outline_structure_handles_none_summary_with_content():
    """summary=None 但有正文时，summary 取正文前 120 字，不抛异常。"""
    svc = BookImportService()
    content = "这是有正文的章节内容，用于验证 summary 从正文生成。"
    chapter = BookImportChapter(title="有正文", content=content, chapter_number=1, summary=None)

    result = svc._build_fallback_outline_structure(chapter)

    assert isinstance(result, dict)
    assert result["summary"] == content