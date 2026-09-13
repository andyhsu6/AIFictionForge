"""PR-1：action 级 risk 表与运行期条件免确认（含 fail-closed）。

本文件只测"策略层"（risk 表、纯函数解析、条件判定），
回合内的事件与持久化在 test_project_agent_inline_task.py 测。
"""
import dataclasses
import os
import uuid

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.database import Base
from app.models.project import Project
from app.services.project_agent_tools import (
    ProjectAgentTool,
    ProjectAgentToolRegistry,
    action_risk_level,
)


@pytest.fixture
async def db_session():
    db_path = f"/tmp/test_agent_risk_{uuid.uuid4().hex}.db"
    engine = create_async_engine(f"sqlite+aiosqlite:///{db_path}")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    Session = async_sessionmaker(bind=engine, expire_on_commit=False)
    async with Session() as session:
        yield session
    await engine.dispose()
    if os.path.exists(db_path):
        os.remove(db_path)


# 11 个 action 必须与 start_project_task 的 enum 一一对应（见 Step 3 的断言）
EXPECTED_ACTION_RISK = {
    "generate_outlines": 2,
    "expand_outline": 2,
    "batch_expand_outlines": 2,
    "generate_chapter": 2,
    "batch_generate_chapters": 2,
    "analyze_chapter": 1,
    "regenerate_chapter": 2,
    "partial_regenerate_chapter": 2,
    "generate_character": 1,
    "generate_organization": 1,
    "generate_careers": 1,
}


def test_start_project_task_action_risk_table_is_exhaustive():
    registry = ProjectAgentToolRegistry(
        Project(id="proj-1", user_id="test", title="测试项目"), None
    )
    tool = registry.get("start_project_task")

    assert tool.action_risk == EXPECTED_ACTION_RISK
    # 顶层 risk 必须仍是 2：OPERATIONAL_WRITE_TOOL_NAMES 由它推导，
    # 改成 <2 会让该工具被当成只读、路由到 operational.read()。
    assert tool.risk_level == 2
    # 新增 action 却没配 risk ⇒ 立刻红（禁止无信息量地滑到默认值）
    assert set(tool.action_risk) == set(tool.parameters["properties"]["action"]["enum"])


def test_read_only_tools_have_no_action_risk():
    registry = ProjectAgentToolRegistry(
        Project(id="proj-1", user_id="test", title="测试项目"), None
    )
    for name in ("list_chapters", "get_chapter_analysis", "update_project"):
        assert registry.get(name).action_risk == {}


def test_action_risk_level_prefers_action_and_falls_back():
    tool = ProjectAgentTool(
        "start_project_task", "d", {}, risk_level=2, action_risk={"analyze_chapter": 1}
    )
    assert action_risk_level(tool, {"action": "analyze_chapter"}) == 1
    assert action_risk_level(tool, {"action": "regenerate_chapter"}) == 2
    assert action_risk_level(tool, {}) == 2
    assert action_risk_level(tool, {"action": None}) == 2
    assert action_risk_level(tool, {"action": 3}) == 2
    plain = ProjectAgentTool("list_chapters", "d", {})
    assert action_risk_level(plain, {"action": "analyze_chapter"}) == 0


@pytest.mark.anyio
async def test_exempt_operational_write_tool_still_routes_to_operational(db_session):
    """免确认（risk<2）的运维写入工具必须仍走 operational.execute()。

    现状回归点：ProjectAgentToolRegistry.execute() 用 if tool.requires_confirmation
    包住名单路由，一旦 action 级降级把 start_project_task 变成免确认，
    它会掉进 _resolve_update() 并抛 "不支持的写入工具"。
    """
    registry = ProjectAgentToolRegistry(
        Project(id="proj-1", user_id="test", title="测试项目"), db_session
    )
    registry._tools["start_project_task"] = dataclasses.replace(
        registry.get("start_project_task"), risk_level=1
    )
    seen: list[tuple[str, dict]] = []

    async def fake_execute(name, arguments):
        seen.append((name, arguments))
        return {"entity_id": "task-1", "resources": ["tasks"]}

    registry.operational.execute = fake_execute

    result = await registry.execute(
        "start_project_task", {"action": "generate_character", "data": {}}
    )

    assert seen == [("start_project_task", {"action": "generate_character", "data": {}})]
    assert result["entity_id"] == "task-1"


@pytest.mark.anyio
async def test_confirmation_operational_write_tool_routing_unchanged(db_session):
    """risk_level=2 的运维写入工具路由不变（名单分派与旧判定等价）。"""
    registry = ProjectAgentToolRegistry(
        Project(id="proj-1", user_id="test", title="测试项目"), db_session
    )
    seen: list[str] = []

    async def fake_execute(name, arguments):
        seen.append(name)
        return {"entity_id": "task-2"}

    registry.operational.execute = fake_execute

    result = await registry.execute(
        "start_project_task", {"action": "regenerate_chapter", "data": {}}
    )

    assert seen == ["start_project_task"]
    assert result["entity_id"] == "task-2"


@pytest.mark.anyio
async def test_update_project_still_goes_through_resolve_update(db_session):
    """基础字段更新工具仍走 _resolve_update（免被名单分派误伤）。"""
    from app.models.project import Project as ProjectModel

    db_session.add(ProjectModel(id="proj-1", user_id="test", title="旧标题"))
    await db_session.flush()
    registry = ProjectAgentToolRegistry(
        ProjectModel(id="proj-1", user_id="test", title="旧标题"), db_session
    )

    result = await registry.execute("update_project", {"title": "新标题"})

    assert result["entity_id"] == "proj-1"
    assert result["after"]["title"] == "新标题"
    assert result["resources"] == ["projects"]
