"""PR-1：action 级 risk 表与运行期条件免确认（含 fail-closed）。

本文件只测"策略层"（risk 表、纯函数解析、条件判定），
回合内的事件与持久化在 test_project_agent_inline_task.py 测。
"""
import dataclasses
import os
import uuid

import pytest
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import app.services.project_agent_risk as risk_module
from app.database import Base
from app.models.chapter import Chapter
from app.models.memory import PlotAnalysis, StoryMemory
from app.models.project import Project
from app.models.project_agent import AgentConversation, AgentMessage
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
    它就掉出写入分派、落到 execute() 末尾的 handler 兜底并抛
    "工具尚未实现：start_project_task"（注册表上没有 _start_project_task 方法）。
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
        # story_timeline 是 NOT NULL 列（见 app/models/memory.py 的 StoryMemory），缺了会在
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


async def _seed_an_uncommitted_turn_row(db) -> AgentMessage:
    """造一条"本轮已写、尚未提交"的行，代表 stream_chat 循环里已 flush 的 step/记录。

    探针的善后写法会不会把它一起丢掉，只能靠这种未提交的行看出来。
    """
    conversation = AgentConversation(user_id="test", project_id="proj-1", title="探针事务护栏")
    db.add(conversation)
    await db.flush()
    message = AgentMessage(conversation_id=conversation.id, role="user", content="分析第 1 章")
    db.add(message)
    await db.flush()
    return message


@pytest.mark.anyio
async def test_probe_sql_error_keeps_the_outer_transaction_committable(db_session, monkeypatch):
    """F1：探针里的真实 SQL 异常不得把会话带进不可提交状态。

    app/config.py 的默认 DATABASE_URL 是 postgresql+asyncpg：PG 上语句报错会中止
    整个事务，没有 SAVEPOINT 时主流程随后的 flush/commit 直接抛 PendingRollbackError
    ⇒ 用户拿到 500 而不是确认卡。SQLite 不中止事务，所以本用例能证的是
    "risk 2 + 外层仍可提交 + 本轮未提交的行仍在"（后者专门打掉 db.rollback() 这种修法）；
    PG 的中止行为本身无法在本机复现。
    """
    chapter = await _seed_chapter(db_session)
    message = await _seed_an_uncommitted_turn_row(db_session)

    async def sql_error(db, **kwargs):
        await db.execute(text("SELECT * FROM pr1_table_that_does_not_exist"))

    monkeypatch.setattr(risk_module, "_chapter_has_analysis_results", sql_error)
    decision = await resolve_tool_risk(
        db_session, project=_detached_project(), tool=_start_task_tool(),
        arguments={"action": "analyze_chapter", "chapter_id": chapter.id},
    )
    assert (decision.risk_level, decision.requires_confirmation) == (2, True)
    assert decision.reason == "analysis_probe_failed"

    await db_session.commit()  # 拿掉 SAVEPOINT 的 PG、或改用 db.rollback() 的写法都会在此失败/丢行
    committed = (await db_session.execute(
        select(AgentMessage).where(AgentMessage.id == message.id)
    )).scalars().all()
    assert len(committed) == 1, "本轮已写的行被探针善后逻辑一起回滚掉了"


@pytest.mark.anyio
async def test_probe_runs_inside_a_savepoint_so_its_own_writes_are_undone(db_session, monkeypatch):
    """F1 的可证伪护栏：探针的语句必须跑在 SAVEPOINT 内。

    SQLite 不会因语句错误中止事务 ⇒ 单靠"报错后仍可提交"拿不到变异信号，
    所以让探针写一条脏数据再抛错，用事务级可见性证明保存点真的在包住它：
    - 拿掉 `db.begin_nested()` ⇒ 脏写留在外层事务、随 commit 落库 ⇒ 第一条断言红；
    - 改成 `db.rollback()` ⇒ 本轮未提交的行被一起丢掉 ⇒ 第二条断言红。
    """
    chapter = await _seed_chapter(db_session)
    message = await _seed_an_uncommitted_turn_row(db_session)

    async def dirty_then_boom(db, **kwargs):
        await db.execute(
            text("UPDATE projects SET title = '脏写：探针没有被保存点包住' WHERE id = 'proj-1'")
        )
        raise RuntimeError("探针写脏之后才报错")

    monkeypatch.setattr(risk_module, "_chapter_has_analysis_results", dirty_then_boom)
    decision = await resolve_tool_risk(
        db_session, project=_detached_project(), tool=_start_task_tool(),
        arguments={"action": "analyze_chapter", "chapter_id": chapter.id},
    )
    assert (decision.risk_level, decision.requires_confirmation) == (2, True)
    assert decision.reason == "analysis_probe_failed"

    await db_session.commit()
    title = (await db_session.execute(
        select(Project.title).where(Project.id == "proj-1")
    )).scalar_one()
    assert title == "测试项目", "探针的写入没被 SAVEPOINT 撤销 ⇒ 它跑在外层事务里"
    survivors = (await db_session.execute(
        select(AgentMessage).where(AgentMessage.id == message.id)
    )).scalars().all()
    assert len(survivors) == 1, "外层事务被回滚 ⇒ 本轮已写的行丢了"


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


def test_no_spec_has_top_level_risk_below_confirmation_threshold():
    """守卫：写/读名单由 spec 顶层 risk_level 的**真值**推导，
    而 ProjectAgentToolRegistry.preview() 由 requires_confirmation（risk>=2）把门。

    将来谁写一个顶层 `risk_level: 1` 的 spec，就会同时踩两个坑：
    1. 静默落进 OPERATIONAL_WRITE_TOOL_NAMES（锚点审计跨 PR 修正第 2 条）；
    2. preview() 抛「只读工具不需要修改预览」。
    工具级 risk 只允许 0（只读）或 >=2（需确认）；降级只能发生在 action 级。
    """
    from app.services.project_agent_operational_tools import OPERATIONAL_TOOL_SPECS

    registry = ProjectAgentToolRegistry(_detached_project(), None)
    offenders = [
        (tool.name, tool.risk_level)
        for tool in registry._tools.values()
        if tool.risk_level and not tool.requires_confirmation
    ]
    assert offenders == []

    # 非空守卫：确认本用例真的覆盖到写入工具，而不是在空集合上真空通过。
    write_tools = {
        tool.name for tool in registry._tools.values() if tool.risk_level
    }
    assert {"start_project_task", "replace_chapter_text", "update_project"} <= write_tools

    spec_offenders = [
        (spec["name"], spec["risk_level"])
        for spec in OPERATIONAL_TOOL_SPECS
        if spec.get("risk_level") and spec["risk_level"] < 2
    ]
    assert spec_offenders == []


def test_system_prompt_forbids_polling_after_start_project_task():
    """PR-1：任务只是入队，助手不得轮询、不得声称完成、不得预告结果。"""
    from app.services.project_agent_service import SYSTEM_PROMPT

    rule = next(
        line for line in SYSTEM_PROMPT.splitlines() if line.startswith("7.")
    )
    assert "start_project_task 返回后任务只是进入后台队列" in rule
    assert "不得再调用任务查询工具轮询其状态" in rule
    assert "不得声称任务已完成" in rule
    assert "不得预告" in rule
