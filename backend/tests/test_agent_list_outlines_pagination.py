"""`list_outlines` 分页 / compact 契约（issue #170）。

背景：`list_chapters` 与 `list_foreshadows` 都支持 offset / compact，`list_outlines`
却把这两个参数判为「不支持的字段」，计划步骤因此被提案期拒绝一次（模型照着另两个
列表工具的形状写，行为本身没有错）。同时它返回的 total 是本页条数而非过滤后全量，
分页调用方无法判断「还有没有下一页」。

本文件钉死：
(a) offset / limit 的窗口与 total（过滤后全量）；
(b) compact 的 items 形状（[id, order_index, title]，不含 content）；
(c) 非 compact 仍返回 content；
(d) 契约在提案期同样成立（validate_plan 不再拒绝 offset/compact）。

夹具全部为中性占位文本。
"""
from __future__ import annotations

import os
import uuid
from types import SimpleNamespace

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.database import Base
from app.models.outline import Outline
from app.models.project import Project
from app.services.agent_plan_schema import (
    PlanValidationError,
    plannable_tool_names,
    validate_plan,
)
from app.services.project_agent_tools import ProjectAgentToolRegistry

PROJECT_ID = "proj-list-outlines-1"


@pytest.fixture
async def seeded():
    """一个项目 + 5 条大纲（order_index 1..5），其中第 3 条带筛选词。"""
    db_path = f"/tmp/test_list_outlines_{uuid.uuid4().hex}.db"
    engine = create_async_engine(f"sqlite+aiosqlite:///{db_path}")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    Session = async_sessionmaker(bind=engine, expire_on_commit=False)
    async with Session() as session:
        project = Project(id=PROJECT_ID, user_id="user-1", title="placeholder project")
        rows = [
            Outline(
                id=f"outline-{index}",
                project_id=PROJECT_ID,
                order_index=index,
                title=f"placeholder outline {index}",
                content=("placeholder keyword body" if index == 3 else "placeholder body text"),
            )
            for index in range(1, 6)
        ]
        session.add_all([project, *rows])
        await session.commit()
        yield project, session
    await engine.dispose()
    if os.path.exists(db_path):
        os.remove(db_path)


async def _list(project, db, **arguments):
    registry = ProjectAgentToolRegistry(project, db)
    return await registry.execute("list_outlines", arguments)


@pytest.mark.anyio
async def test_offset_window_and_total_are_post_filter(seeded):
    """(a) offset 窗口正确；total 是过滤后的全量，不是本页条数。"""
    project, db = seeded
    first = await _list(project, db, limit=2)
    assert [row["order_index"] for row in first["items"]] == [1, 2]
    assert (first["total"], first["returned"], first["offset"]) == (5, 2, 0)

    second = await _list(project, db, limit=2, offset=2)
    assert [row["order_index"] for row in second["items"]] == [3, 4]
    assert (second["total"], second["returned"], second["offset"]) == (5, 2, 2)

    tail = await _list(project, db, limit=2, offset=4)
    assert [row["order_index"] for row in tail["items"]] == [5]
    assert (tail["total"], tail["returned"]) == (5, 1)

    # 关键词过滤后 total 随过滤收缩 —— 分页调用方据此判断还有没有下一页
    filtered = await _list(project, db, query="keyword", limit=10)
    assert filtered["total"] == 1
    assert [row["order_index"] for row in filtered["items"]] == [3]


@pytest.mark.anyio
async def test_compact_items_are_positional_and_omit_content(seeded):
    """(b) compact 返回 [id, order_index, title]，不含正文。"""
    project, db = seeded
    compact = await _list(project, db, limit=1, compact=True)

    assert compact["items"] == [["outline-1", 1, "placeholder outline 1"]]
    assert (compact["total"], compact["returned"], compact["offset"]) == (5, 1, 0)


@pytest.mark.anyio
async def test_non_compact_keeps_content(seeded):
    """(c) 非 compact 仍返回完整条目（回归：默认形状不变）。"""
    project, db = seeded
    full = await _list(project, db, limit=1, offset=2)

    assert full["items"] == [{
        "id": "outline-3",
        "order_index": 3,
        "title": "placeholder outline 3",
        "content": "placeholder keyword body",
    }]


def test_validate_plan_accepts_offset_and_compact():
    """(d) 提案期不再把 offset/compact 判成「不支持的字段」。"""
    registry = ProjectAgentToolRegistry(SimpleNamespace(id=PROJECT_ID), None)  # 构造不查库
    definitions = registry.definitions()
    schemas = {item["function"]["name"]: item["function"]["parameters"] for item in definitions}

    step = {
        "id": "s1", "tool": "list_outlines",
        "arguments": {"limit": 20, "offset": 40, "compact": True},
    }
    plan = validate_plan(
        {"objective": "placeholder objective", "steps": [step]},
        allowed_tools=plannable_tool_names(definitions),
        tool_schemas=schemas,
    )
    assert plan["steps"][0]["arguments"]["compact"] is True


def test_validate_plan_still_rejects_unknown_outline_fields():
    """反向守卫：不知道的键仍要在提案期拒掉（别把 schema 放宽成任意键）。"""
    registry = ProjectAgentToolRegistry(SimpleNamespace(id=PROJECT_ID), None)
    definitions = registry.definitions()
    schemas = {item["function"]["name"]: item["function"]["parameters"] for item in definitions}

    step = {
        "id": "s1", "tool": "list_outlines",
        "arguments": {"cursor": "abc"},
    }
    with pytest.raises(PlanValidationError) as exc_info:
        validate_plan(
            {"objective": "placeholder objective", "steps": [step]},
            allowed_tools=plannable_tool_names(definitions),
            tool_schemas=schemas,
        )
    assert "cursor" in str(exc_info.value)
