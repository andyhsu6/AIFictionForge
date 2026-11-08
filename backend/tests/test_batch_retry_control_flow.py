"""批量生成外层重试控制流测试（BLOCKER 回归）。

背景：`execute_batch_generation_in_order` 的单章 `while` 循环里，allowance 用尽的
`else` 分支只记账 + 打日志、**没有 break**。控制流回到
`while retry_count <= task.max_retries and not chapter_success` 时两个条件仍成立，
于是同一章被反复重试、`failed_chapters` 重复记账，
`NonRetryableChapterGenerationError`（allowance=0）承诺的「0 次外层重试」形同虚设。

这里驱动**真实的** `execute_batch_generation_in_order`：DB 用内存 SQLite、引擎与单章
生成函数都是假体，但循环/记账/汇总全部走生产代码，因此断言的是「实际调用次数」。
"""
import asyncio

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import app.database
from app.api import chapters as chapters_api
from app.database import Base
from app.models.batch_generation_task import BatchGenerationTask
from app.models.chapter import Chapter
from app.models.project import Project

CHAPTER_NUMBER = 58


class _CountingGenerator:
    """恒定抛指定异常的假单章生成函数，记录被调用次数。"""

    def __init__(self, exc: BaseException):
        self.exc = exc
        self.calls = 0

    async def __call__(self, **kwargs):
        self.calls += 1
        raise self.exc


async def _run_batch(monkeypatch, exc: BaseException):
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    session_factory = async_sessionmaker(bind=engine, expire_on_commit=False)

    async with session_factory() as session:
        session.add(Project(id="p-flow", user_id="u-flow", title="stub-project"))
        session.add(
            Chapter(
                id="c-flow",
                project_id="p-flow",
                chapter_number=CHAPTER_NUMBER,
                title="stub-chapter",
            )
        )
        session.add(
            BatchGenerationTask(
                id="b-flow",
                project_id="p-flow",
                user_id="u-flow",
                start_chapter_number=CHAPTER_NUMBER,
                chapter_count=1,
                chapter_ids=["c-flow"],
                total_chapters=1,
                target_word_count=6000,
                max_retries=3,
                enable_analysis=False,
                status="pending",
            )
        )
        await session.commit()

    generator = _CountingGenerator(exc)

    async def _fake_get_engine(user_id: str):
        return engine

    async def _prerequisites_ok(db, chapter):
        return True, "", []

    async def _analysis_ready(db, chapter):
        return True, ""

    async def _no_sleep(*args, **kwargs):
        return None

    monkeypatch.setattr(app.database, "get_engine", _fake_get_engine)
    monkeypatch.setattr(
        chapters_api, "generate_single_chapter_for_batch", generator
    )
    monkeypatch.setattr(chapters_api, "check_prerequisites", _prerequisites_ok)
    monkeypatch.setattr(
        chapters_api, "check_previous_analysis_ready", _analysis_ready
    )
    # 退避等待对断言无意义，直接跳过（否则截断类用例要真睡 2 秒）
    monkeypatch.setattr(asyncio, "sleep", _no_sleep)

    try:
        await chapters_api.execute_batch_generation_in_order(
            batch_id="b-flow",
            user_id="u-flow",
            ai_service=object(),
            enable_mcp=False,
        )
        async with session_factory() as session:
            task = await session.get(BatchGenerationTask, "b-flow")
            return generator, task
    finally:
        await engine.dispose()


@pytest.mark.anyio
async def test_non_retryable_error_is_not_replayed(monkeypatch):
    """NonRetryable（allowance=0）：外层只调用 1 次，失败明细只有 1 条。"""
    generator, task = await _run_batch(
        monkeypatch, chapters_api.NonRetryableChapterGenerationError("空正文")
    )

    assert generator.calls == 1, f"不可重试错误仍被外层重放：{generator.calls} 次"
    assert [f["chapter_number"] for f in (task.failed_chapters or [])] == [CHAPTER_NUMBER]
    assert task.status == "failed"
    assert task.current_retry_count == 0


@pytest.mark.anyio
async def test_truncation_error_retries_at_most_once(monkeypatch):
    """截断类（allowance=1）：首发生成 + 1 次外层重试，共 2 次；明细仍只有 1 条。"""
    generator, task = await _run_batch(
        monkeypatch, chapters_api.ChapterGenerationTruncatedError("半截正文")
    )

    assert generator.calls == 2, f"截断类外层重试次数不符：{generator.calls} 次"
    assert [f["chapter_number"] for f in (task.failed_chapters or [])] == [CHAPTER_NUMBER]
    assert task.status == "failed"
