"""只读工具分页/筛选参数契约（issue #160）。

真实模型在计划里写了本仓库没声明的两个参数：

- ``list_chapters {"offset": 29, "limit": 21}`` —— 章节列表不分页，模型拿不到 30 章以后；
- ``list_foreshadows {"overdue": true}`` —— 台账无法只筛超期条目。

提案期拒收本身是对的，但用户侧表现为报错。本文件钉住补上参数后的行为：

1. ``list_chapters`` 的 ``offset`` 真的分页，``total`` 是**过滤后全量**而不是本页条数；
2. ``list_foreshadows`` 的 ``overdue`` 复用 ``Foreshadow.get_urgency_level`` 的「已超期」
   判据（与 ``get_foreshadow_stats`` 同口径：当前章节 = ``max(Chapter.chapter_number)``），
   过滤后再套 ``offset/limit``，``total`` 是过滤后的总数；
3. ``compact`` 输出格式与无 ``overdue`` 时的默认行为都不回归。

夹具与断言文本全部为中性占位（AGENTS.md 脱敏硬约束）。
"""
from __future__ import annotations

import os
from types import SimpleNamespace
import uuid

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.database import Base
from app.models.chapter import Chapter
from app.models.foreshadow import Foreshadow
from app.models.project import Project
from app.services.project_agent_tools import ProjectAgentToolRegistry

PROJECT_ID = "proj-read-paging"
USER_ID = "u-read-paging"
CHAPTER_COUNT = 5
CURRENT_CHAPTER = 10


@pytest.fixture
async def db_session():
    """临时文件 SQLite（与 test_plan_step_argument_contract.py 同模式）。"""
    db_path = f"/tmp/test_read_tool_paging_{uuid.uuid4().hex}.db"
    engine = create_async_engine(f"sqlite+aiosqlite:///{db_path}")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    Session = async_sessionmaker(bind=engine, expire_on_commit=False)
    async with Session() as session:
        yield session
    await engine.dispose()
    if os.path.exists(db_path):
        os.remove(db_path)


@pytest.fixture
async def chapter_env(db_session):
    """一个项目 + 5 章（章节分页用，章号 1..5）。"""
    project = Project(id=PROJECT_ID, user_id=USER_ID, title="placeholder project")
    chapters = [
        Chapter(
            project_id=PROJECT_ID, chapter_number=number,
            title=f"placeholder chapter {number}", summary="placeholder summary",
            content="", word_count=0, status="draft",
        )
        for number in range(1, CHAPTER_COUNT + 1)
    ]
    db_session.add_all([project, *chapters])
    await db_session.commit()
    return SimpleNamespace(
        project=project,
        db=db_session,
        registry=ProjectAgentToolRegistry(project, db_session),
    )


@pytest.fixture
async def overdue_env(db_session):
    """一个项目：3 条 planted 伏笔（目标回收章 5/20/30）+ 第 10 章（当前章节）。"""
    project = Project(id=PROJECT_ID, user_id=USER_ID, title="placeholder project")
    chapters = [
        Chapter(
            project_id=PROJECT_ID, chapter_number=number,
            title=f"placeholder chapter {number}", summary="placeholder summary",
            content="", word_count=0, status="draft",
        )
        for number in (1, 2, 3, 4, 5, CURRENT_CHAPTER)
    ]
    foreshadows = [
        Foreshadow(
            project_id=PROJECT_ID, title=f"placeholder foreshadow {target}",
            content="placeholder content", status="planted",
            plant_chapter_number=1, target_resolve_chapter_number=target,
        )
        for target in (5, 20, 30)
    ]
    db_session.add_all([project, *chapters, *foreshadows])
    await db_session.commit()
    return SimpleNamespace(
        project=project,
        db=db_session,
        registry=ProjectAgentToolRegistry(project, db_session),
    )


@pytest.mark.anyio
async def test_list_chapters_offset_pages_and_reports_full_total(chapter_env):
    """(1) offset=2/limit=2 → 第 3、4 章；total 是项目全量 5，不是本页 2。"""
    result = await chapter_env.registry.execute("list_chapters", {"offset": 2, "limit": 2})

    assert [item["chapter_number"] for item in result["items"]] == [3, 4]
    assert result["total"] == 5
    assert result["returned"] == 2
    assert result["offset"] == 2


@pytest.mark.anyio
async def test_list_chapters_defaults_to_first_page(chapter_env):
    """(2) 不传 offset → 从 0 开始；total/returned 都反映完整数据集。"""
    result = await chapter_env.registry.execute("list_chapters", {})

    assert result["offset"] == 0
    assert result["total"] == 5
    assert result["returned"] == 5
    assert [item["chapter_number"] for item in result["items"]] == [1, 2, 3, 4, 5]


@pytest.mark.anyio
async def test_list_foreshadows_overdue_filters_by_urgency(overdue_env):
    """(3) 当前章节 10：只有目标回收章 5 的条目算超期（与 stats 同口径）。"""
    result = await overdue_env.registry.execute("list_foreshadows", {"overdue": True})

    assert result["total"] == 1
    assert [item["target_resolve_chapter_number"] for item in result["items"]] == [5]


@pytest.mark.anyio
async def test_overdue_combines_with_compact_and_limit(overdue_env):
    """(4) overdue + limit + compact 可同时使用，compact 数组格式不回归。"""
    result = await overdue_env.registry.execute(
        "list_foreshadows", {"overdue": True, "limit": 1, "compact": True}
    )

    assert result["total"] == 1
    assert result["returned"] == 1
    assert isinstance(result["items"], list)
    assert isinstance(result["items"][0], list)
    assert result["items"][0][0]  # id 仍在首位


@pytest.mark.anyio
async def test_list_foreshadows_without_overdue_is_unchanged(overdue_env):
    """(5) 不传 overdue → 3 条全返回（默认行为不变）。"""
    result = await overdue_env.registry.execute("list_foreshadows", {})

    assert result["total"] == 3
    assert len(result["items"]) == 3
    assert all(item["status"] == "planted" for item in result["items"])
