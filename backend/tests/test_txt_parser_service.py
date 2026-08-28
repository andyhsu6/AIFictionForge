"""TXT 解析服务回归测试。

覆盖缺陷：弱标题启发式在强标题（第X章）存在时仍被启用，导致正文对话/叙述短行
被误判为章节标题，产生空正文章节，进而触发 _build_summary('') 返回 None 后
(None or None).strip() 崩溃。
"""
from app.services.txt_parser_service import txt_parser_service


def test_split_chapters_ignores_weak_headings_when_strong_exist():
    """强标题存在时，正文中的短对话行（「妈妈……」等）不得被识别为章节标题。"""
    text = """第一章 相遇

这是第一章的正文内容，描写两个人在街上相遇的场景。

「妈妈……」

但是想了想还是算了…

第二章 离别

这是第二章的正文内容，描写离别时的场景。
"""
    chapters = txt_parser_service.split_chapters(text)

    assert len(chapters) == 2
    assert chapters[0]["title"] == "第一章 相遇"
    assert chapters[1]["title"] == "第二章 离别"
    # 每个章节必须有正文，不允许出现空正文章节
    assert all(c["content"].strip() for c in chapters)


def test_split_chapters_uses_strong_headings_only():
    """31 个强标题的真实文件形态：只按第X章切分，正文短行不参与切分。"""
    text = """第一章 开局

正文内容第一段。

他听后直接不耐烦的朝我挥手

正文内容第二段。

第二章 发展

正文内容第三段。
"""
    chapters = txt_parser_service.split_chapters(text)

    assert [c["title"] for c in chapters] == ["第一章 开局", "第二章 发展"]
    assert all(c["content"].strip() for c in chapters)


def test_split_chapters_weak_headings_fallback_when_no_strong():
    """全文没有强标题时，弱标题兜底仍然生效（保持原有兜底能力）。"""
    text = """引言

这是没有章节标题的小说开头段落，内容足够长。

过渡

这是第二个段落的正文内容。
"""
    chapters = txt_parser_service.split_chapters(text)

    assert len(chapters) >= 2
    assert all(c["content"].strip() for c in chapters)


def test_split_chapters_trailing_heading_without_body_keeps_chapter():
    """最后一个章节标题后无正文时，章节保留但正文为空（由上层兜底，不崩溃）。"""
    text = """第一章 有正文

这是第一章的正文内容。

第二章 空章节
"""
    chapters = txt_parser_service.split_chapters(text)

    assert [c["title"] for c in chapters] == ["第一章 有正文", "第二章 空章节"]
    # 末章正文为空是允许的输入形态，解析器不抛异常
    assert chapters[1]["content"] == ""