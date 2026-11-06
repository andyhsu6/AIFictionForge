"""manage_foreshadow 的 data 契约（issue #223）。

背景（issue #223 真实失败形状）：提案期对 create/update 的 data 键走
`STRICT_DATA_FIELDS` 白名单，但暴露给模型的 `manage_foreshadow` schema 里 data 是
空壳对象（`additionalProperties: true`、无 `properties`）。模型看不到合法字段名，
按语义写成 `data.plant_chapter` / `data.target_chapter`，整份计划被拒。

本文件钉死修复后的三条契约：
(a) schema 真的给出字段名（模型第一次就能看到 plant_chapter_number 等）；
(b) 提案期：语义别名放行、真正未知字段仍拒（消息含「不支持的字段」）；
(c) 执行期：别名在 handler 入口同样归一（否则计划批准后执行失败）。

夹具与断言文本全部为中性占位（AGENTS.md 脱敏硬约束）。
"""
from __future__ import annotations

import os
import uuid
from types import SimpleNamespace

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.database import Base
from app.models.chapter import Chapter
from app.models.foreshadow import Foreshadow
from app.models.project import Project
from app.services.agent_plan_schema import PlanValidationError, validate_plan
from app.services.project_agent_extended_tools import (
    ProjectAgentExtendedTools,
    normalize_data_aliases,
)
from app.services.project_agent_selectors import find_foreshadow
from app.services.project_agent_tools import ProjectAgentToolRegistry

PROJECT_ID = "proj-foreshadow-data-1"
FORESHADOW_ID = "b1c2d3e4-f5a6-4b7c-8d9e-000000000201"
CHAPTER_ID = "c1d2e3f4-a5b6-4c7d-8e9f-000000000301"
# plant/resolve 从 data 直接读取、但不写进 Foreshadow 模型的上下文键（#223 review）。
FORESHADOW_CONTEXT_KEYS = {"chapter_id", "chapter_number", "is_partial", "reason"}


@pytest.fixture
async def db_session():
    """临时文件 SQLite（与 test_plan_step_argument_contract.py 同模式）。"""
    db_path = f"/tmp/test_foreshadow_data_contract_{uuid.uuid4().hex}.db"
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
async def seeded(db_session):
    """一个项目 + 一条待更新的伏笔。"""
    project = Project(id=PROJECT_ID, user_id="user-1", title="placeholder project")
    row = Foreshadow(
        id=FORESHADOW_ID, project_id=PROJECT_ID,
        title="placeholder foreshadow", content="placeholder old content",
        status="pending",
    )
    db_session.add_all([project, row])
    await db_session.commit()
    return project, db_session


@pytest.fixture
async def seeded_with_chapter(seeded):
    """在 seeded 基础上补一条章节：plant/resolve 用 chapter_number 定位。"""
    project, db = seeded
    db.add(Chapter(
        id=CHAPTER_ID, project_id=PROJECT_ID, chapter_number=3,
        title="placeholder chapter", content="",
    ))
    await db.commit()
    return project, db


def _plan(steps: list[dict]) -> dict:
    return {"objective": "placeholder objective", "steps": steps}


def _registry_schemas() -> dict[str, dict]:
    registry = ProjectAgentToolRegistry(SimpleNamespace(id="p1"), None)  # 构造不查库
    return {
        item["function"]["name"]: item["function"]["parameters"]
        for item in registry.definitions()
    }


def _foreshadow_data_schema() -> dict:
    schema = _registry_schemas()["manage_foreshadow"]
    return schema["properties"]["data"]


# --- (a) schema 暴露合法字段名 ------------------------------------------------


def test_tool_schema_exposes_foreshadow_data_properties():
    """(a1) 模型可见的 data 不再是空壳：写库字段 + plant/resolve 上下文键全部可见。"""
    data_schema = _foreshadow_data_schema()
    properties = data_schema.get("properties")
    assert isinstance(properties, dict) and properties
    assert set(properties) == (
        ProjectAgentExtendedTools.FORESHADOW_FIELDS | FORESHADOW_CONTEXT_KEYS
    )
    assert properties["plant_chapter_number"]["type"] == "integer"
    assert properties["target_resolve_chapter_number"]["type"] == "integer"
    assert properties["auto_remind"]["type"] == "boolean"
    assert properties["tags"]["type"] == "array"
    assert "pending" in properties["status"]["enum"]
    # plant/resolve 的章节定位键必须可见：否则模型只会用 plant_chapter_number，
    # 提案期通过、执行期报「必须提供 chapter_id 或 chapter_number」（#223 review）。
    assert properties["chapter_number"]["type"] == "integer"
    assert properties["chapter_id"]["type"] == "string"
    assert properties["is_partial"]["type"] == "boolean"
    assert properties["reason"]["type"] == "string"


def test_foreshadow_schema_properties_are_write_fields_plus_context_keys():
    """(a2) 单一来源：properties = 写库字段 + 上下文键，运行期白名单只取写库那半。

    上下文键不得并入 FORESHADOW_FIELDS：create/update 会把 data 整体喂给
    Foreshadow(...)，多出的键会让 ORM 构造失败。
    """
    properties = set(_foreshadow_data_schema()["properties"])
    assert properties - FORESHADOW_CONTEXT_KEYS == ProjectAgentExtendedTools.FORESHADOW_FIELDS
    assert not (ProjectAgentExtendedTools.FORESHADOW_FIELDS & FORESHADOW_CONTEXT_KEYS)


# --- (b) 提案期：别名放行、真正未知字段仍拒 ------------------------------------


def test_validate_plan_accepts_plant_chapter_alias_on_create():
    """(b1) issue #223 真实形状：data.plant_chapter / data.target_chapter 必须放行。"""
    step = {
        "id": "s1", "tool": "manage_foreshadow",
        "arguments": {
            "action": "create",
            "data": {
                "title": "placeholder foreshadow", "content": "placeholder content",
                "plant_chapter": 3, "target_chapter": 20,
            },
        },
    }
    plan = validate_plan(_plan([step]), allowed_tools={"manage_foreshadow"},
                         tool_schemas=_registry_schemas())
    # raw arguments 原样透传（执行期再归一），提案期不得再拒绝。
    assert plan["steps"][0]["arguments"]["data"]["plant_chapter"] == 3


def test_validate_plan_accepts_plant_chapter_alias_on_update():
    """(b2) update 同样放行别名（严格动作，走 STRICT_DATA_FIELDS 归一后同判）。"""
    step = {
        "id": "s1", "tool": "manage_foreshadow",
        "arguments": {
            "action": "update", "foreshadow_id": FORESHADOW_ID,
            "data": {"plant_chapter": 7, "target_chapter": 33},
        },
    }
    plan = validate_plan(_plan([step]), allowed_tools={"manage_foreshadow"},
                         tool_schemas=_registry_schemas())
    assert plan["steps"][0]["arguments"]["data"]["target_chapter"] == 33


def test_validate_plan_rejects_unknown_foreshadow_data_key():
    """(b3) 真正未知的字段仍然拒绝，消息含「不支持的字段」与字段名。"""
    step = {
        "id": "s1", "tool": "manage_foreshadow",
        "arguments": {
            "action": "update", "foreshadow_id": FORESHADOW_ID,
            "data": {"plant_year": 2025},
        },
    }
    with pytest.raises(PlanValidationError) as exc_info:
        validate_plan(_plan([step]), allowed_tools={"manage_foreshadow"},
                      tool_schemas=_registry_schemas())
    message = str(exc_info.value)
    assert "不支持的字段" in message
    assert "data.plant_year" in message


def test_normalize_data_aliases_maps_foreshadow_chapter_aliases():
    """(b4) 归一结果：别名落到规范键，规范键优先，不修改调用方对象。"""
    original = {"plant_chapter": 3, "target_chapter": 20, "title": "placeholder"}
    normalized = normalize_data_aliases("manage_foreshadow", original)
    assert normalized == {
        "plant_chapter_number": 3,
        "target_resolve_chapter_number": 20,
        "title": "placeholder",
    }
    assert "plant_chapter" in original  # 副作用为零

    both = normalize_data_aliases(
        "manage_foreshadow", {"plant_chapter": 3, "plant_chapter_number": 9}
    )
    assert both == {"plant_chapter_number": 9}


# --- (c) 执行期：handler 入口同样归一 -----------------------------------------


@pytest.mark.anyio
async def test_update_accepts_alias_keys_at_runtime(seeded):
    """(c1) 计划批准后执行：data.plant_chapter 不得触发「包含不支持的字段」。"""
    project, db = seeded
    tools = ProjectAgentExtendedTools(project, db)

    await tools.execute("manage_foreshadow", {
        "action": "update", "foreshadow_id": FORESHADOW_ID,
        "data": {"plant_chapter": 3, "target_chapter": 20},
    })

    row = await find_foreshadow(db, PROJECT_ID, {"foreshadow_id": FORESHADOW_ID})
    await db.refresh(row)
    assert row.plant_chapter_number == 3
    assert row.target_resolve_chapter_number == 20


@pytest.mark.anyio
async def test_create_accepts_alias_keys_at_runtime(seeded):
    """(c2) create 同样归一别名，别名键不会漏进 ORM 构造参数。"""
    project, db = seeded
    tools = ProjectAgentExtendedTools(project, db)

    await tools.execute("manage_foreshadow", {
        "action": "create",
        "data": {
            "title": "placeholder new foreshadow", "content": "placeholder new content",
            "plant_chapter": 5,
        },
    })

    row = (
        await db.execute(
            Foreshadow.__table__.select().where(
                Foreshadow.title == "placeholder new foreshadow"
            )
        )
    ).first()
    assert row is not None
    assert row.plant_chapter_number == 5


@pytest.mark.anyio
async def test_runtime_still_rejects_truly_unknown_key(seeded):
    """(c3) 反向守卫：运行期仍拒未知键（归一不等于放行）。"""
    project, db = seeded
    tools = ProjectAgentExtendedTools(project, db)

    with pytest.raises(ValueError, match="包含不支持的字段"):
        await tools.preview("manage_foreshadow", {
            "action": "update", "foreshadow_id": FORESHADOW_ID,
            "data": {"plant_year": 2025},
        })


# --- (d) plant/resolve 的上下文键必须可用（#223 异模型审核 MAJOR） --------------
#
# 修复前：schema 只暴露 21 个写库字段、描述又说“章节号只能用 plant_chapter_number”，
# 模型照做后 plant/resolve 会抛「必须提供 chapter_id 或 chapter_number」——提案期
# 通过、执行期必然失败。下面三条钉住上下文键真的能走通，且没有把计划章节键误当
# 成实际章节键。


@pytest.mark.anyio
async def test_plant_preview_accepts_chapter_number_context_key(seeded_with_chapter):
    """(d1) plant 用 data.chapter_number 能定位章节（模型按新 schema 书写即可执行）。"""
    project, db = seeded_with_chapter
    tools = ProjectAgentExtendedTools(project, db)

    result = await tools.preview("manage_foreshadow", {
        "action": "plant", "foreshadow_id": FORESHADOW_ID,
        "data": {"chapter_number": 3},
    })

    assert result["changes"]["plant_chapter_number"]["after"] == 3
    assert result["changes"]["status"]["after"] == "planted"


@pytest.mark.anyio
async def test_plant_preview_rejects_planned_chapter_key(seeded_with_chapter):
    """(d2) 反向：plant 不接受计划章节键，报错指明要用 chapter_id/chapter_number。

    这不是我们要掩盖的失败——plan 的 plant_chapter_number 是计划值，与实际埋入
    章节是两件事；测试固定这条边界，避免以后把两者混成一个字段。
    """
    project, db = seeded_with_chapter
    tools = ProjectAgentExtendedTools(project, db)

    with pytest.raises(ValueError, match="必须提供 chapter_id 或 chapter_number"):
        await tools.preview("manage_foreshadow", {
            "action": "plant", "foreshadow_id": FORESHADOW_ID,
            "data": {"plant_chapter_number": 3},
        })


@pytest.mark.anyio
async def test_resolve_preview_accepts_chapter_number_and_is_partial(seeded_with_chapter):
    """(d3) resolve 用 chapter_number + is_partial 能走通（部分回收状态正确）。"""
    project, db = seeded_with_chapter
    tools = ProjectAgentExtendedTools(project, db)

    result = await tools.preview("manage_foreshadow", {
        "action": "resolve", "foreshadow_id": FORESHADOW_ID,
        "data": {"chapter_number": 3, "is_partial": True},
    })

    assert result["changes"]["status"]["after"] == "partially_resolved"
    assert result["changes"]["actual_resolve_chapter_number"]["after"] == 3
