"""大纲结构化元数据契约回归测试（feat/outline-metadata-contract）。

契约：`manage_outline.create` 与 `update_outline` 通过**顶层扁平字段**
scenes/characters/key_points/key_events/emotion/goal 提供结构化元数据；
它们不是必填，且不能包进 data。

覆盖：
(a) validate_plan 接受 create 带顶层元数据；
(b) 带 data 包裹的元数据仍被拒（保持既有扁平契约）；
(c) create 预览的 after 含规范化后的字段；
(d) execute 落库 structure 含全部字段，非法 characters.type 归为 character；
(e) update_outline 合并元数据且不丢已有键；
(f) 未提供元数据时行为不变（只改 title/content，旧元数据保留）。

夹具与断言文本全部为中性占位（AGENTS.md 脱敏硬约束）。
"""
from __future__ import annotations

import json
import os
import uuid
from types import SimpleNamespace

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.database import Base
from app.models.chapter import Chapter
from app.models.outline import Outline
from app.models.project import Project
from app.services.agent_plan_schema import PlanValidationError, validate_plan
from app.services.project_agent_extended_tools import ProjectAgentExtendedTools
from app.services.project_agent_tools import ProjectAgentToolRegistry

PROJECT_ID = "proj-outline-metadata-1"

METADATA = {
    "scenes": ["placeholder scene one", "placeholder scene two"],
    "characters": [
        {"name": "placeholder ally", "type": "organization"},
        {"name": "placeholder lead", "type": "character"},
    ],
    "key_points": ["placeholder point one"],
    "key_events": ["placeholder event one"],
    "emotion": "placeholder emotion",
    "goal": "placeholder goal",
}


@pytest.fixture
async def db_session():
    db_path = f"/tmp/test_outline_metadata_{uuid.uuid4().hex}.db"
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
async def project(db_session):
    row = Project(id=PROJECT_ID, user_id="user-1", title="placeholder project")
    db_session.add(row)
    await db_session.commit()
    return row


def _plan(steps: list[dict]) -> dict:
    return {"objective": "placeholder objective", "steps": steps}


def _registry_schemas() -> dict[str, dict]:
    """registry.definitions() 是模型唯一能看到的面，验收也用它。"""
    registry = ProjectAgentToolRegistry(SimpleNamespace(id="p1"), None)
    return {
        item["function"]["name"]: item["function"]["parameters"]
        for item in registry.definitions()
    }


def _create_step(**overrides) -> dict:
    arguments = {
        "action": "create",
        "title": "placeholder outline",
        "content": "placeholder outline content",
        "order_index": 1,
        **overrides,
    }
    return {"id": "s1", "tool": "manage_outline", "arguments": arguments}


async def _outline(db, outline_id: str) -> Outline:
    row = (await db.execute(select(Outline).where(Outline.id == outline_id))).scalar_one()
    await db.refresh(row)
    return row


# --- (a)/(b) 提案期契约 --------------------------------------------------------


def test_validate_plan_accepts_outline_create_with_top_level_metadata():
    plan = validate_plan(
        _plan([_create_step(**METADATA)]),
        allowed_tools={"manage_outline"},
        tool_schemas=_registry_schemas(),
    )
    arguments = plan["steps"][0]["arguments"]
    assert arguments["scenes"] == METADATA["scenes"]
    assert arguments["characters"] == METADATA["characters"]


def test_validate_plan_rejects_metadata_wrapped_in_data():
    step = _create_step(data={"scenes": ["placeholder scene"]})
    with pytest.raises(PlanValidationError) as exc_info:
        validate_plan(
            _plan([step]),
            allowed_tools={"manage_outline"},
            tool_schemas=_registry_schemas(),
        )
    message = str(exc_info.value)
    assert "s1" in message
    assert "不支持的字段 data" in message


# --- (c) 预览 ----------------------------------------------------------------


@pytest.mark.anyio
async def test_preview_create_reports_normalized_metadata(project, db_session):
    tools = ProjectAgentExtendedTools(project, db_session)
    arguments = {
        "action": "create",
        "title": "placeholder outline",
        "content": "placeholder outline content",
        "order_index": 1,
        **METADATA,
        "characters": [
            {"name": "placeholder ally", "type": "organization"},
            {"name": "  ", "type": "character"},
            {"name": "placeholder lead", "type": "sect"},
            {"name": "placeholder lead", "type": "character"},
        ],
    }

    preview = await tools.preview("manage_outline", arguments)

    changes = preview["changes"]
    assert changes["scenes"]["after"] == METADATA["scenes"]
    assert changes["key_points"]["after"] == METADATA["key_points"]
    assert changes["key_events"]["after"] == METADATA["key_events"]
    assert changes["emotion"]["after"] == METADATA["emotion"]
    assert changes["goal"]["after"] == METADATA["goal"]
    # 空 name 被丢弃；非法 type 归为 character；同名同类型去重。
    assert changes["characters"]["after"] == METADATA["characters"]


@pytest.mark.anyio
async def test_create_rejects_invalid_metadata_type(project, db_session):
    tools = ProjectAgentExtendedTools(project, db_session)
    arguments = {
        "action": "create",
        "title": "placeholder outline",
        "content": "placeholder outline content",
        "order_index": 1,
        "scenes": "placeholder scene",
    }
    with pytest.raises(ValueError) as exc_info:
        await tools.preview("manage_outline", arguments)
    assert "scenes" in str(exc_info.value)


# --- (d) create 落库 ---------------------------------------------------------


@pytest.mark.anyio
async def test_create_writes_metadata_into_structure(project, db_session):
    tools = ProjectAgentExtendedTools(project, db_session)

    result = await tools.execute(
        "manage_outline",
        {
            "action": "create",
            "title": "placeholder outline",
            "content": "placeholder outline content",
            "order_index": 1,
            **METADATA,
            "characters": [{"name": "placeholder lead", "type": "sect"}],
        },
    )

    row = await _outline(db_session, result["entity_id"])
    structure = json.loads(row.structure)
    assert structure["title"] == "placeholder outline"
    assert structure["summary"] == "placeholder outline content"
    assert structure["content"] == "placeholder outline content"
    assert structure["scenes"] == METADATA["scenes"]
    assert structure["key_points"] == METADATA["key_points"]
    assert structure["key_events"] == METADATA["key_events"]
    assert structure["emotion"] == METADATA["emotion"]
    assert structure["goal"] == METADATA["goal"]
    assert structure["characters"] == [{"name": "placeholder lead", "type": "character"}]


# --- (e) update_outline 合并 -------------------------------------------------


@pytest.fixture
async def seeded_outline(project, db_session):
    row = Outline(
        id=str(uuid.uuid4()),
        project_id=PROJECT_ID,
        title="placeholder outline",
        content="placeholder outline content",
        order_index=1,
        structure=json.dumps({
            "title": "placeholder outline",
            "summary": "placeholder outline content",
            "content": "placeholder outline content",
            "key_events": ["placeholder existing event"],
            "goal": "placeholder existing goal",
        }, ensure_ascii=False),
    )
    db_session.add(row)
    await db_session.commit()
    return row


@pytest.mark.anyio
async def test_update_outline_merges_metadata_and_keeps_existing_keys(
    project, db_session, seeded_outline,
):
    registry = ProjectAgentToolRegistry(project, db_session)

    await registry.execute("update_outline", {
        "outline_id": seeded_outline.id,
        "key_points": ["placeholder new point"],
        "emotion": "placeholder new emotion",
        "characters": [{"name": "placeholder lead", "type": "character"}],
    })

    structure = json.loads((await _outline(db_session, seeded_outline.id)).structure)
    assert structure["key_points"] == ["placeholder new point"]
    assert structure["emotion"] == "placeholder new emotion"
    assert structure["characters"] == [{"name": "placeholder lead", "type": "character"}]
    # 未提供的已有键必须保留。
    assert structure["key_events"] == ["placeholder existing event"]
    assert structure["goal"] == "placeholder existing goal"


@pytest.mark.anyio
async def test_update_outline_without_metadata_keeps_structure_metadata(
    project, db_session, seeded_outline,
):
    registry = ProjectAgentToolRegistry(project, db_session)

    await registry.execute("update_outline", {
        "outline_id": seeded_outline.id,
        "title": "placeholder renamed outline",
        "content": "placeholder new content",
    })

    row = await _outline(db_session, seeded_outline.id)
    structure = json.loads(row.structure)
    assert row.title == "placeholder renamed outline"
    assert structure["title"] == "placeholder renamed outline"
    assert structure["summary"] == "placeholder new content"
    assert structure["key_events"] == ["placeholder existing event"]
    assert structure["goal"] == "placeholder existing goal"


# --- (f) manage_chapter update_plan 的 plot_summary 落摘要列 ---------------------


@pytest.fixture
async def seeded_chapter(project, db_session):
    row = Chapter(
        id=str(uuid.uuid4()),
        project_id=PROJECT_ID,
        chapter_number=1,
        title="placeholder chapter",
        summary="placeholder old summary",
        expansion_plan=json.dumps(
            {"key_events": ["placeholder existing event"]}, ensure_ascii=False
        ),
    )
    db_session.add(row)
    await db_session.commit()
    return row


@pytest.mark.anyio
async def test_update_plan_writes_plot_summary_into_chapter_summary(
    project, db_session, seeded_chapter,
):
    tools = ProjectAgentExtendedTools(project, db_session)

    result = await tools.execute("manage_chapter", {
        "action": "update_plan",
        "chapter_id": seeded_chapter.id,
        "data": {
            "summary": "placeholder data summary",
            "expansion_plan": {
                "plot_summary": "placeholder plan summary",
                "key_events": ["placeholder new event"],
            },
        },
    })

    await db_session.refresh(seeded_chapter)
    assert seeded_chapter.summary == "placeholder plan summary"  # 规划内摘要优先
    stored = json.loads(seeded_chapter.expansion_plan)
    assert "plot_summary" not in stored  # 摘要只存列，不落第二份副本
    assert stored["key_events"] == ["placeholder new event"]
    # 回显与落库一致
    assert result["after"]["summary"] == "placeholder plan summary"
    assert result["after"]["expansion_plan"] == stored


@pytest.mark.anyio
async def test_update_plan_maps_top_level_summary_to_chapter_summary(
    project, db_session, seeded_chapter,
):
    tools = ProjectAgentExtendedTools(project, db_session)

    await tools.execute("manage_chapter", {
        "action": "update_plan",
        "chapter_id": seeded_chapter.id,
        "data": {
            "summary": "placeholder data summary",
            "key_events": ["placeholder new event"],
        },
    })

    await db_session.refresh(seeded_chapter)
    assert seeded_chapter.summary == "placeholder data summary"

