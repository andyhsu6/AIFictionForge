"""PR-1：低风险后台任务必须在助手回合内直接启动。

覆盖：免确认时 AgentToolCall 落 executed + 不弹确认卡 + 判定写进 step detail；
回归：regenerate_chapter / replace_chapter_text / 已有分析结果的 analyze_chapter 仍需确认。
"""
import os
import uuid
from types import SimpleNamespace

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.database import Base
from app.models.chapter import Chapter
from app.models.memory import PlotAnalysis
from app.models.project import Project
from app.models.project_agent import (
    AgentConversation,
    AgentExecutionStep,
    AgentToolCall,
)
from app.services.project_agent_service import ProjectAgentService


@pytest.fixture
async def db_session():
    db_path = f"/tmp/test_agent_inline_{uuid.uuid4().hex}.db"
    engine = create_async_engine(f"sqlite+aiosqlite:///{db_path}")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    Session = async_sessionmaker(bind=engine, expire_on_commit=False)
    async with Session() as session:
        yield session
    await engine.dispose()
    if os.path.exists(db_path):
        os.remove(db_path)


async def make_conversation(db) -> AgentConversation:
    conversation = AgentConversation(user_id="test", project_id="proj-1", title="PR-1 回合内启动")
    db.add(conversation)
    await db.flush()
    return conversation


async def seed_project_and_chapter(db, *, content="alpha beta gamma delta"):
    db.add(Project(id="proj-1", user_id="test", title="测试项目"))
    chapter = Chapter(
        project_id="proj-1", chapter_number=1, title="Chapter 1",
        content=content, word_count=len(content),
    )
    db.add(chapter)
    await db.flush()
    return chapter


def make_service(db) -> ProjectAgentService:
    return ProjectAgentService(
        db=db,
        ai_service=SimpleNamespace(default_model="test-model"),
        project=Project(id="proj-1", user_id="test", title="测试项目"),
        user_id="test",
    )


def tool_call(name: str, arguments: dict, call_id: str = "call_p1_1") -> dict:
    return {
        "content": "我先调用工具。",
        "tool_calls": [{"id": call_id, "function": {"name": name, "arguments": arguments}}],
        "usage": {"prompt_tokens": 10, "completion_tokens": 5},
    }


def answer(content: str) -> dict:
    return {"content": content, "tool_calls": [], "usage": {"prompt_tokens": 0, "completion_tokens": 0}}


def install_fake_model(svc: ProjectAgentService, responses: list[dict], calls: list[dict]) -> None:
    """两个出口都要 patch：generate_text（工具决策轮）与 generate_text_stream_full（force_answer 轮）。"""

    async def fake_generate_text(**kwargs):
        calls.append(kwargs)
        return responses[min(len(calls) - 1, len(responses) - 1)]

    async def fake_stream_full(**kwargs):
        calls.append(kwargs)
        return responses[min(len(calls) - 1, len(responses) - 1)]

    svc.ai_service.generate_text = fake_generate_text
    svc.ai_service.generate_text_stream_full = fake_stream_full


def install_fake_operational_execute(svc: ProjectAgentService, result: dict) -> list:
    """只替换运维工具的执行出口，保留 registry 的路由与循环的事件逻辑。"""
    seen: list = []

    async def fake_execute(name, arguments):
        seen.append({"stage": "execute", "name": name, "arguments": arguments})
        return dict(result)

    svc.registry.operational.execute = fake_execute
    return seen


def instrument_commits(svc: ProjectAgentService, counter: dict) -> None:
    original = svc.db.commit

    async def counted():
        counter["n"] = counter.get("n", 0) + 1
        return await original()

    svc.db.commit = counted


async def run_turn(svc, conversation, responses, *, message: str = "分析第 1 章"):
    calls: list[dict] = []
    install_fake_model(svc, responses, calls)
    events = [
        e async for e in svc.stream_chat(
            conversation_id=conversation.id,
            message=message,
            page_context={"route": "/project/1"},
            auto_approve=False,
        )
    ]
    return events, calls


async def only_tool_call(db) -> AgentToolCall:
    return (await db.execute(select(AgentToolCall))).scalars().one()


@pytest.mark.anyio
async def test_analyze_chapter_without_existing_results_starts_inline(db_session):
    await seed_project_and_chapter(db_session)
    conversation = await make_conversation(db_session)
    svc = make_service(db_session)
    seen = install_fake_operational_execute(svc, {
        "message": "章节分析任务已加入后台队列",
        "entity_id": "analysis-task-1",
        "before": {},
        "after": {"action": "analyze_chapter"},
        "resources": ["tasks", "analysis", "characters", "foreshadows"],
        "task_type": "chapter_analysis",
    })

    events, _ = await run_turn(svc, conversation, [
        tool_call("start_project_task", {"action": "analyze_chapter", "chapter_number": 1}),
        answer("章节分析任务已启动，可在右下角任务面板查看进度。"),
    ])

    record = await only_tool_call(db_session)
    assert record.tool_name == "start_project_task"
    assert record.status == "executed"
    assert record.requires_confirmation is False
    assert record.risk_level == 1
    assert record.error_message is None
    assert seen == [{
        "stage": "execute",
        "name": "start_project_task",
        "arguments": {"action": "analyze_chapter", "chapter_number": 1},
    }]

    waiting = [
        s for s in (await db_session.execute(select(AgentExecutionStep))).scalars().all()
        if s.status == "waiting_confirmation"
    ]
    assert waiting == []
    assert [e for e in events if e["type"] == "result"][-1]["data"]["status"] == "completed"


@pytest.mark.anyio
async def test_risk_decision_is_audited_in_step_detail(db_session):
    await seed_project_and_chapter(db_session)
    conversation = await make_conversation(db_session)
    svc = make_service(db_session)
    install_fake_operational_execute(svc, {
        "message": "已启动", "entity_id": "t-1", "before": {}, "after": {},
        "resources": ["tasks", "characters"], "task_type": "character_generate",
    })

    await run_turn(svc, conversation, [
        tool_call("start_project_task", {"action": "generate_character", "data": {}}),
        answer("已启动。"),
    ], message="生成一个角色")

    record = await only_tool_call(db_session)
    step = (await db_session.execute(
        select(AgentExecutionStep).where(AgentExecutionStep.tool_call_id == record.id)
    )).scalars().one()
    assert step.detail["risk"] == {
        "action": "generate_character",
        "risk_level": 1,
        "requires_confirmation": False,
        "reason": "action_policy",
    }


@pytest.mark.anyio
async def test_analyze_chapter_with_existing_analysis_still_needs_confirmation(db_session):
    chapter = await seed_project_and_chapter(db_session)
    db_session.add(PlotAnalysis(project_id="proj-1", chapter_id=chapter.id, plot_stage="高潮"))
    await db_session.flush()
    conversation = await make_conversation(db_session)
    svc = make_service(db_session)
    seen = install_fake_operational_execute(svc, {"resources": ["tasks"]})

    events, _ = await run_turn(svc, conversation, [
        tool_call("start_project_task", {"action": "analyze_chapter", "chapter_number": 1}),
        answer("不该被走到"),
    ])

    assert seen == []
    record = await only_tool_call(db_session)
    assert record.status == "waiting_confirmation"
    assert record.requires_confirmation is True
    assert record.risk_level == 2
    assert [e for e in events if e["type"] == "result"][-1]["data"]["status"] == "waiting_confirmation"
    step = (await db_session.execute(
        select(AgentExecutionStep).where(AgentExecutionStep.tool_call_id == record.id)
    )).scalars().one()
    assert step.detail["risk"]["reason"] == "overwrite_existing_analysis"
    assert "覆盖既有分析" in step.content


@pytest.mark.anyio
async def test_regenerate_chapter_still_needs_confirmation(db_session):
    await seed_project_and_chapter(db_session)
    conversation = await make_conversation(db_session)
    svc = make_service(db_session)
    seen = install_fake_operational_execute(svc, {"resources": ["tasks"]})

    events, _ = await run_turn(svc, conversation, [
        tool_call("start_project_task", {"action": "regenerate_chapter", "chapter_number": 1}),
        answer("不该被走到"),
    ], message="重写第 1 章")

    assert seen == []
    record = await only_tool_call(db_session)
    assert (record.status, record.requires_confirmation, record.risk_level) == (
        "waiting_confirmation", True, 2,
    )
    step = (await db_session.execute(
        select(AgentExecutionStep).where(AgentExecutionStep.tool_call_id == record.id)
    )).scalars().one()
    assert "覆盖既有分析" not in step.content


@pytest.mark.anyio
async def test_replace_chapter_text_still_needs_confirmation(db_session):
    chapter = await seed_project_and_chapter(db_session)
    conversation = await make_conversation(db_session)
    svc = make_service(db_session)
    registry_execute_calls: list = []
    original_operational_execute = svc.registry.operational.execute

    async def spy_execute(name, arguments):
        registry_execute_calls.append(name)
        return await original_operational_execute(name, arguments)

    svc.registry.operational.execute = spy_execute

    events, _ = await run_turn(svc, conversation, [
        tool_call("replace_chapter_text", {
            "chapter_id": chapter.id, "start_position": 6, "end_position": 10,
            "expected_text": "beta", "new_text": "omega",
        }),
        answer("不该被走到"),
    ], message="把第 1 章里的 beta 换成 omega")

    assert registry_execute_calls == []
    record = await only_tool_call(db_session)
    assert (record.status, record.requires_confirmation, record.risk_level) == (
        "waiting_confirmation", True, 2,
    )
    assert record.preview["changes"]["selected_text"]["before"] == "beta"
    assert record.preview["changes"]["selected_text"]["after"] == "omega"
    assert [e for e in events if e["type"] == "tool_executed"] == []


@pytest.mark.anyio
async def test_exempt_branch_emits_tool_executed_with_task_type(db_session):
    """架构计划 §0/PR-1：前端 notifyToolResources 要 resources，
    轮询要能反查表 ⇒ 事件必须同时带 resources 与 task_type。"""
    await seed_project_and_chapter(db_session)
    conversation = await make_conversation(db_session)
    svc = make_service(db_session)
    install_fake_operational_execute(svc, {
        "message": "角色生成任务已加入后台队列",
        "entity_id": "bg-task-1",
        "before": {},
        "after": {"action": "generate_character"},
        "resources": ["tasks", "characters"],
        "task_type": "character_generate",
    })

    events, _ = await run_turn(svc, conversation, [
        tool_call("start_project_task", {"action": "generate_character", "data": {}}),
        answer("已启动。"),
    ], message="生成一个角色")

    executed = [e for e in events if e["type"] == "tool_executed"]
    assert len(executed) == 1
    data = executed[0]["data"]
    assert data["resources"] == ["tasks", "characters"]
    assert data["task_type"] == "character_generate"
    assert data["approval_mode"] == "inline"
    assert data["tool_call"]["id"] == (await only_tool_call(db_session)).id
    assert data["tool_call"]["result"]["entity_id"] == "bg-task-1"
    assert data["tool_call"]["requires_confirmation"] is False


@pytest.mark.anyio
async def test_read_only_tool_does_not_emit_tool_executed(db_session):
    """只读工具无 resources 可刷新；不带条件地下发会把未截断的
    result（如 get_chapter_detail 的 50000 字符正文）整体重复推给前端。"""
    await seed_project_and_chapter(db_session)
    conversation = await make_conversation(db_session)
    svc = make_service(db_session)

    events, _ = await run_turn(svc, conversation, [
        tool_call("get_chapter_detail", {"chapter_number": 1, "include_content": True}),
        answer("看到了。"),
    ], message="看下第 1 章正文")

    record = await only_tool_call(db_session)
    assert record.status == "executed"
    assert record.requires_confirmation is False
    assert [e for e in events if e["type"] == "tool_executed"] == []


@pytest.mark.anyio
async def test_tool_executed_is_emitted_only_after_the_agent_rows_are_committed(db_session):
    """tool_executed 会让前端立刻另开请求读库（刷新会话、查任务）。
    未提交就下发 ⇒ 前端读到旧数据（与 auto_approve 分支同一决定）。"""
    await seed_project_and_chapter(db_session)
    conversation = await make_conversation(db_session)
    svc = make_service(db_session)
    counter: dict = {"n": 0}
    instrument_commits(svc, counter)
    commits_at_execute: list[int] = []

    async def fake_execute(name, arguments):
        commits_at_execute.append(counter["n"])
        return {
            "message": "已启动", "entity_id": "bg-task-2", "before": {}, "after": {},
            "resources": ["tasks", "characters"], "task_type": "character_generate",
        }

    svc.registry.operational.execute = fake_execute

    commits_at_event: list[int] = []
    calls: list[dict] = []
    install_fake_model(svc, [
        tool_call("start_project_task", {"action": "generate_character", "data": {}}),
        answer("已启动。"),
    ], calls)
    async for event in svc.stream_chat(
        conversation_id=conversation.id, message="生成角色",
        page_context={"route": "/project/1"}, auto_approve=False,
    ):
        if event["type"] == "tool_executed":
            commits_at_event.append(counter["n"])

    # 1 = stream_chat 开头的 user 消息提交（project_agent_service.py:215）；
    # 工具循环本身在调用工具前不得再提交任何行。
    assert commits_at_execute == [1]
    assert commits_at_event and commits_at_event[0] > commits_at_execute[0]


@pytest.mark.anyio
async def test_failed_inline_tool_reports_no_tool_executed(db_session):
    await seed_project_and_chapter(db_session)
    conversation = await make_conversation(db_session)
    svc = make_service(db_session)

    async def failing_execute(name, arguments):
        raise ValueError("章节正文为空，无法分析")

    svc.registry.operational.execute = failing_execute

    events, _ = await run_turn(svc, conversation, [
        tool_call("start_project_task", {"action": "analyze_chapter", "chapter_number": 1}),
        answer("失败了"),
    ])

    record = await only_tool_call(db_session)
    assert record.status == "failed"
    assert "章节正文为空" in (record.error_message or "")
    assert [e for e in events if e["type"] == "tool_executed"] == []
    step = (await db_session.execute(
        select(AgentExecutionStep).where(AgentExecutionStep.tool_call_id == record.id)
    )).scalars().one()
    assert step.status == "failed"
    assert step.detail["risk"]["action"] == "analyze_chapter"


@pytest.mark.anyio
async def test_operational_execute_result_carries_resolvable_task_type(db_session):
    """三张任务表主键无跨表唯一性 ⇒ execute() 必须回一个可反查的 task_type。"""
    from app.services.project_agent_operational_tools import (
        ProjectAgentOperationalTools,
    )
    from app.services.task_resources import AGENT_TASK_ACTION_TYPES

    ops = ProjectAgentOperationalTools(
        Project(id="proj-1", user_id="test", title="测试项目"), db_session
    )

    async def fake_preview(arguments):
        return {}, {"action": arguments["action"]}, "启动后台任务", "proj-1"

    async def fake_handler(arguments):
        return "task-id-1", {"task_id": "task-id-1"}, "已启动"

    for action in AGENT_TASK_ACTION_TYPES:
        setattr(ops, f"_start_project_task_preview_{action}", fake_preview)
        setattr(ops, f"_start_project_task_{action}", fake_handler)

    result = await ops.execute("start_project_task", {"action": "analyze_chapter"})

    assert result["entity_id"] == "task-id-1"
    assert result["task_type"] == "chapter_analysis"
    assert "tasks" in result["resources"]

    other = await ops.execute("start_project_task", {"action": "regenerate_chapter"})
    assert other["task_type"] == "chapter_regenerate"


def test_agent_task_action_types_covers_every_start_task_action():
    """新增 action 忘记配 task_type ⇒ 本用例先红（防 PR-2b 轮询漏表）。"""
    from app.services.project_agent_operational_tools import OPERATIONAL_TOOL_SPECS
    from app.services.task_resources import AGENT_TASK_ACTION_TYPES

    spec = next(item for item in OPERATIONAL_TOOL_SPECS if item["name"] == "start_project_task")
    assert set(spec["parameters"]["properties"]["action"]["enum"]) == set(AGENT_TASK_ACTION_TYPES)
