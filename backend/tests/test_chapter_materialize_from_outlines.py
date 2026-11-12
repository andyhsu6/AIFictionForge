"""issue #290：按大纲补齐缺失章节 + 统计里报告缺口。

只测服务层函数（不跑 HTTP），用内存 SQLite 与最小种子数据。
"""

from __future__ import annotations

import asyncio
import json

from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from starlette.requests import Request

from app.api.chapters import (
    get_project_chapter_stats,
    materialize_chapters_from_outlines,
)
from app.database import Base
from app.models.chapter import Chapter
from app.models.outline import Outline
from app.models.project import Project

USER_ID = "u-290"
PROJECT_ID = "p-290"


def make_request(user_id: str = USER_ID) -> Request:
    request = Request({"type": "http", "method": "GET", "path": "/", "headers": []})
    request.state.user_id = user_id
    return request


async def _seed(session, *, outlines: int = 3, chapters: int = 2) -> None:
    session.add(Project(id=PROJECT_ID, user_id=USER_ID, title="t", outline_mode="one-to-many"))
    for number in range(1, outlines + 1):
        session.add(
            Outline(
                id=f"o-{number}",
                project_id=PROJECT_ID,
                title=f"第{number}章",
                structure=json.dumps({"title": f"第{number}章", "chapter_number": number}),
                order_index=number,
            )
        )
    for number in range(1, chapters + 1):
        session.add(
            Chapter(
                id=f"c-{number}",
                project_id=PROJECT_ID,
                chapter_number=number,
                title=f"第{number}章",
                content="line",
                word_count=4,
            )
        )
    await session.commit()


def _run(scenario) -> None:
    async def wrapper() -> None:
        engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        session_factory = async_sessionmaker(bind=engine, expire_on_commit=False)
        async with session_factory() as session:
            await scenario(session)
        await engine.dispose()

    asyncio.run(wrapper())


def test_stats_report_outlines_without_chapters():
    async def scenario(session):
        await _seed(session)
        stats = await get_project_chapter_stats(PROJECT_ID, make_request(), session)
        assert stats["total"] == 2
        assert stats["max_chapter_number"] == 2
        assert stats["outline_total"] == 3
        assert stats["max_outline_number"] == 3
        assert stats["missing_from_outlines"] == 1

    _run(scenario)


def test_materialize_creates_only_missing_chapters_and_is_idempotent():
    async def scenario(session):
        await _seed(session)

        first = await materialize_chapters_from_outlines(PROJECT_ID, make_request(), session)
        assert first["created"] == [3]
        assert first["count"] == 1

        created = (
            await session.execute(select(Chapter).where(Chapter.chapter_number == 3))
        ).scalar_one()
        assert created.outline_id == "o-3"
        assert created.title == "第3章"
        assert (created.content or "") == ""
        assert created.status == "draft"

        # 已存在的章节不被覆盖
        existing = (
            await session.execute(select(Chapter).where(Chapter.chapter_number == 1))
        ).scalar_one()
        assert existing.content == "line"

        second = await materialize_chapters_from_outlines(PROJECT_ID, make_request(), session)
        assert second == {"created": [], "count": 0}

        stats = await get_project_chapter_stats(PROJECT_ID, make_request(), session)
        assert stats["missing_from_outlines"] == 0

    _run(scenario)


def test_materialize_skips_outlines_without_order_index():
    async def scenario(session):
        session.add(Project(id=PROJECT_ID, user_id=USER_ID, title="t", outline_mode="one-to-many"))
        session.add(
            Outline(id="o-1", project_id=PROJECT_ID, title="orphan", structure="{}", order_index=None)
        )
        await session.commit()

        result = await materialize_chapters_from_outlines(PROJECT_ID, make_request(), session)
        assert result["count"] == 0
        assert (await session.execute(select(Chapter))).scalars().all() == []

    _run(scenario)
