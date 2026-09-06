"""拆书导入服务回归测试。

覆盖缺陷：
1. _build_fallback_outline_structure 对空正文章节（content='' 且 summary=None）
   执行 (None or None).strip() 抛 AttributeError: 'NoneType' object has no attribute 'strip'。
2. I1 回归：apply_import / apply_import_stream 的 try 守卫必须放行 ApiError
   （verify_project_access 抛 not_found.project_or_forbidden，404），
   不得落入 except Exception 包装成 HTTPException(500, "导入失败: ...")。
"""
import asyncio
from unittest.mock import AsyncMock

import pytest

from app.core.errors import ApiError
from app.schemas.book_import import BookImportApplyRequest, BookImportChapter, ProjectSuggestion
from app.services.book_import_service import BookImportService, _BookImportTask


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


def _completed_task(task_id: str) -> _BookImportTask:
    """构造一个已完成解析、可进入 apply 阶段的内存任务。"""
    return _BookImportTask(
        task_id=task_id,
        user_id="user-1",
        filename="book.txt",
        project_id=None,
        create_new_project=True,
        import_mode="append",
        status="completed",
    )


def _apply_payload() -> BookImportApplyRequest:
    return BookImportApplyRequest(
        project_suggestion=ProjectSuggestion(title="守卫回归测试"),
        chapters=[],
    )


@pytest.mark.parametrize("method_name", ["apply_import", "apply_import_stream"])
def test_apply_guards_propagate_api_error_unwrapped(monkeypatch, method_name):
    """I1 回归：守卫不得把 ApiError 包装成 500 "导入失败"。

    try 块内 verify_project_access 现抛 ApiError("not_found.project_or_forbidden")（404）；
    守卫必须先 rollback 再原样放行（镜像 retry_failed_steps_stream 的形状），
    而不是落入 except Exception → HTTPException(500, "导入失败: [not_found. ...]")。
    """
    svc = BookImportService()
    task = _completed_task("t-guard-1")
    svc._tasks[task.task_id] = task

    async def _raise_access_api_error(*args, **kwargs):
        raise ApiError(code="not_found.project_or_forbidden")

    # 模拟 try 块内 _prepare_project → verify_project_access 抛 ApiError
    monkeypatch.setattr(svc, "_prepare_project", _raise_access_api_error)
    db = AsyncMock()

    kwargs = dict(task_id=task.task_id, user_id=task.user_id, payload=_apply_payload(), db=db)
    if method_name == "apply_import_stream":
        kwargs["progress_callback"] = None

    with pytest.raises(ApiError) as exc_info:
        asyncio.run(getattr(svc, method_name)(**kwargs))

    assert exc_info.value.code == "not_found.project_or_forbidden"
    assert exc_info.value.status == 404
    assert "导入失败" not in exc_info.value.detail
    assert "[not_found." not in exc_info.value.detail
    db.rollback.assert_awaited()  # 守卫先 rollback 再 raise