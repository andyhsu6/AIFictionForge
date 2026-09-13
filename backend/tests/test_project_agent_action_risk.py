"""PR-1：action 级 risk 表与运行期条件免确认（含 fail-closed）。

本文件只测"策略层"（risk 表、纯函数解析、条件判定），
回合内的事件与持久化在 test_project_agent_inline_task.py 测。
"""
import dataclasses
import os
import uuid

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import app.services.project_agent_risk as risk_module
from app.database import Base
from app.models.chapter import Chapter
from app.models.memory import PlotAnalysis, StoryMemory
from app.models.project import Project
from app.services.project_agent_risk import resolve_tool_risk
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


def _detached_project() -> Project:
    return Project(id="proj-1", user_id="test", title="测试项目")


async def _seed_chapter(db, *, content="alpha beta gamma delta"):
    db.add(Project(id="proj-1", user_id="test", title="测试项目"))
    chapter = Chapter(
        project_id="proj-1", chapter_number=1, title="Chapter 1",
        content=content, word_count=len(content),
    )
    db.add(chapter)
    await db.flush()
    return chapter


def _start_task_tool():
    registry = ProjectAgentToolRegistry(_detached_project(), None)
    return registry.get("start_project_task")


@pytest.mark.anyio
async def test_analyze_chapter_exempt_when_no_existing_results(db_session):
    chapter = await _seed_chapter(db_session)
    decision = await resolve_tool_risk(
        db_session, project=_detached_project(), tool=_start_task_tool(),
        arguments={"action": "analyze_chapter", "chapter_id": chapter.id},
    )
    assert (decision.risk_level, decision.requires_confirmation) == (1, False)
    assert decision.reason == "action_policy"
    assert decision.action == "analyze_chapter"


@pytest.mark.anyio
async def test_analyze_chapter_needs_confirmation_when_plot_analysis_exists(db_session):
    chapter = await _seed_chapter(db_session)
    db_session.add(PlotAnalysis(project_id="proj-1", chapter_id=chapter.id, plot_stage="发展"))
    await db_session.flush()

    decision = await resolve_tool_risk(
        db_session, project=_detached_project(), tool=_start_task_tool(),
        arguments={"action": "analyze_chapter", "chapter_id": chapter.id},
    )
    assert (decision.risk_level, decision.requires_confirmation) == (2, True)
    assert decision.reason == "overwrite_existing_analysis"


@pytest.mark.anyio
async def test_analyze_chapter_needs_confirmation_when_only_memories_exist(db_session):
    chapter = await _seed_chapter(db_session)
    db_session.add(StoryMemory(
        project_id="proj-1", chapter_id=chapter.id,
        memory_type="plot_point", content="synthetic memory text",
        # story_timeline 是 NOT NULL 列（app/models/memory.py:42），缺了会在
        # flush 阶段就 IntegrityError，探测分支根本没跑到。
        story_timeline=1,
    ))
    await db_session.flush()

    decision = await resolve_tool_risk(
        db_session, project=_detached_project(), tool=_start_task_tool(),
        arguments={"action": "analyze_chapter", "chapter_number": 1},
    )
    assert (decision.risk_level, decision.requires_confirmation) == (2, True)
    assert decision.reason == "overwrite_existing_analysis"


@pytest.mark.anyio
async def test_probe_failure_fails_closed_to_confirmation(db_session, monkeypatch):
    chapter = await _seed_chapter(db_session)

    async def boom(*args, **kwargs):
        raise RuntimeError("database is unavailable")

    monkeypatch.setattr(risk_module, "_chapter_has_analysis_results", boom)
    decision = await resolve_tool_risk(
        db_session, project=_detached_project(), tool=_start_task_tool(),
        arguments={"action": "analyze_chapter", "chapter_id": chapter.id},
    )
    assert (decision.risk_level, decision.requires_confirmation) == (2, True)
    assert decision.reason == "analysis_probe_failed"


@pytest.mark.anyio
async def test_unresolvable_chapter_fails_closed_to_confirmation(db_session):
    db_session.add(Project(id="proj-1", user_id="test", title="测试项目"))
    await db_session.flush()
    decision = await resolve_tool_risk(
        db_session, project=_detached_project(), tool=_start_task_tool(),
        arguments={"action": "analyze_chapter", "chapter_id": "no-such-chapter"},
    )
    assert (decision.risk_level, decision.requires_confirmation) == (2, True)
    assert decision.reason == "analysis_probe_failed"


@pytest.mark.anyio
async def test_regenerate_chapter_is_never_exempt(db_session):
    chapter = await _seed_chapter(db_session)
    decision = await resolve_tool_risk(
        db_session, project=_detached_project(), tool=_start_task_tool(),
        arguments={"action": "regenerate_chapter", "chapter_id": chapter.id},
    )
    assert (decision.risk_level, decision.requires_confirmation) == (2, True)
    assert decision.reason == "action_policy"


@pytest.mark.anyio
async def test_read_only_tool_risk_untouched_by_probe(db_session):
    registry = ProjectAgentToolRegistry(_detached_project(), db_session)
    decision = await resolve_tool_risk(
        db_session, project=_detached_project(),
        tool=registry.get("get_chapter_analysis"),
        arguments={"chapter_number": 1},
    )
    assert (decision.risk_level, decision.requires_confirmation) == (0, False)


@pytest.mark.anyio
async def test_top_level_risk_two_and_action_level_exempt_coexist(db_session):
    """本 PR 的命门：顶层 risk 保持 2 与 action 免确认必须同时成立。

    只测"降级后仍能执行"（上面的路由用例）不够——还要证明 spec 顶层 risk
    没有被动过，OPERATIONAL_WRITE_TOOL_NAMES 才继续收养它。
    """
    from app.services.project_agent_operational_tools import (
        OPERATIONAL_READ_TOOL_NAMES,
        OPERATIONAL_WRITE_TOOL_NAMES,
    )

    tool = _start_task_tool()
    assert tool.risk_level == 2 and tool.requires_confirmation
    assert "start_project_task" in OPERATIONAL_WRITE_TOOL_NAMES
    assert "start_project_task" not in OPERATIONAL_READ_TOOL_NAMES

    exempt = {}
    for action, risk in tool.action_risk.items():
        if risk < 2:
            arguments = {"action": action, "data": {}}
            if action == "analyze_chapter":
                # analyze_chapter 免确认要靠运行期探测，先造一个无结果的章节
                chapter = await _seed_chapter(db_session)
                arguments["chapter_id"] = chapter.id
            decision = await resolve_tool_risk(
                db_session, project=_detached_project(),
                tool=tool, arguments=arguments,
            )
            assert decision.requires_confirmation is False, action
            exempt[action] = decision.risk_level
    assert exempt == {
        "analyze_chapter": 1,
        "generate_character": 1,
        "generate_organization": 1,
        "generate_careers": 1,
    }
