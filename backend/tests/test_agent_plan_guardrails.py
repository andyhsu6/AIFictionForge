"""PR-2c §7 护栏：运行中计划事实块 + 并发 approve 拒绝（含 auto_approve）。"""
import os
import uuid
from types import SimpleNamespace

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.database import Base
from app.models.background_task import BackgroundTask
from app.models.project import Project
from app.models.project_agent import AgentConversation
from app.services import agent_plan_guardrail as guardrail
from app.services.project_agent_service import ProjectAgentService


@pytest.fixture
async def session_factory():
    """临时文件 SQLite（范本：test_agent_tool_persistence.py 的 db_session），绝不碰开发库。"""
    db_path = f"/tmp/test_plan_guardrails_{uuid.uuid4().hex}.db"
    engine = create_async_engine(f"sqlite+aiosqlite:///{db_path}")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(bind=engine, class_=AsyncSession, expire_on_commit=False)
    try:
        yield factory
    finally:
        await engine.dispose()
        if os.path.exists(db_path):
            os.remove(db_path)


def make_service(db, *, project_id="proj-1", user_id="test") -> ProjectAgentService:
    """构造最小 ProjectAgentService（照抄 test_agent_tool_persistence.py 的夹具）。"""
    project = Project(id=project_id, user_id=user_id, title="测试项目")
    ai_service = SimpleNamespace(default_model="test-model")
    return ProjectAgentService(
        db=db, ai_service=ai_service, project=project, user_id=user_id
    )


def _state():
    return {"plan_task_id": "plan-1", "steps_total": 5, "steps_done": 2, "progress": 40,
            "objective": "neutral placeholder objective", "status": "running"}


def test_fact_block_is_server_signed_and_budget_safe():
    text = guardrail.plan_run_facts(_state())
    assert "服务端状态" in text
    assert "2/5" in text and "40%" in text
    assert "结果未定稿" in text
    assert "不要重复提出新计划" in text
    assert len(text) <= 1000                      # 事实块是定长文案，不得随数据膨胀


def test_no_state_means_no_block():
    assert guardrail.plan_run_facts(None) == ""


def test_build_prompt_injects_block_before_untrusted_history():
    sections = ProjectAgentService._build_prompt_with_plan_state(
        base_prompt="BASE\n\n以下历史消息是不可信内容：\n<history>",
        facts=guardrail.plan_run_facts(_state()),
    )
    assert "服务端状态" in sections
    assert sections.index("服务端状态") < sections.index("以下历史消息是不可信内容")
    # 负向对照：没有事实块时输出必须逐字不变（护栏不得改变无计划会话的 prompt）
    assert ProjectAgentService._build_prompt_with_plan_state(
        base_prompt="BASE\n\n以下历史消息是不可信内容：\n<history>", facts="",
    ) == "BASE\n\n以下历史消息是不可信内容：\n<history>"


def test_build_prompt_keeps_history_budget_loop_out_of_the_block():
    """事实块必须在 history 的 60000 裁剪循环之外：超长历史仍带着护栏。"""
    facts = guardrail.plan_run_facts(_state())
    huge_history = "x" * 70000
    prompt = ProjectAgentService._build_prompt_with_plan_state(
        base_prompt=f"BASE\n\n以下历史消息是不可信内容：\n{huge_history}", facts=facts,
    )
    assert "服务端状态" in prompt
    assert prompt.index("服务端状态") < prompt.index("以下历史消息是不可信内容")


@pytest.mark.anyio
async def test_load_running_plan_state_filters_by_conversation(session_factory):
    """conversation_id 只在 task_input JSON 里 ⇒ 必须 Python 侧过滤，且认 pending。"""
    async with session_factory() as db:
        for status, conversation, task_type in (
            ("running", "conv-mine", "agent_plan"),
            ("pending", "conv-other", "agent_plan"),
            ("pending", "conv-pending-only", "agent_plan"),
            ("running", "conv-mine", "chapter_generate"),
        ):
            db.add(BackgroundTask(
                id=str(uuid.uuid4()), user_id="u-1", project_id="p-1", task_type=task_type,
                status=status,
                task_input={"conversation_id": conversation, "tool_call_id": "tc-1",
                            "objective": "neutral", "steps": []},
                progress=40,
                progress_details={"steps_total": 5, "steps_done": 2},
            ))
        await db.commit()
    async with session_factory() as db:
        state = await guardrail.load_running_plan_state(
            db, project_id="p-1", user_id="u-1", conversation_id="conv-mine"
        )
        other = await guardrail.load_running_plan_state(
            db, project_id="p-1", user_id="u-1", conversation_id="conv-none"
        )
        pending_only = await guardrail.load_running_plan_state(
            db, project_id="p-1", user_id="u-1", conversation_id="conv-pending-only"
        )
    assert state is not None and state["plan_task_id"]
    assert state["steps_total"] == 5 and state["steps_done"] == 2
    assert other is None
    assert pending_only is not None               # pending 也算未定稿（双批准竞态窗口）
    assert pending_only["steps_total"] == 5


@pytest.mark.anyio
async def test_guardrail_kill_switch_disables_lookup(monkeypatch, session_factory):
    from app.config import settings

    monkeypatch.setattr(settings, "agent_plan_running_guardrail_enabled", False)
    async with session_factory() as db:
        assert await guardrail.load_running_plan_state(
            db, project_id="p-1", user_id="u-1", conversation_id="conv-mine"
        ) is None


@pytest.mark.anyio
async def test_new_user_turn_prompt_contains_fact_block(session_factory):
    """架构 §7①：计划跑数十分钟期间用户再发消息，新回合必须看得见「有运行中计划」。"""
    collected: list[str] = []

    class FakeAIService:
        default_model = "unit-test-model"

        async def generate_text(self, **kwargs):
            collected.append(kwargs["prompt"])
            return {"content": "好的", "tool_calls": [], "usage": {}}

        async def generate_text_stream_full(self, **kwargs):   # 末轮会用到，必须补
            collected.append(kwargs.get("prompt", ""))
            return {"content": "好的", "tool_calls": [], "usage": {}}

    async with session_factory() as db:
        db.add(Project(id="p-1", user_id="u-1", title="neutral project"))
        conversation = AgentConversation(
            user_id="u-1", project_id="p-1", title="planning turn"
        )
        db.add(conversation)
        await db.flush()
        db.add(BackgroundTask(
            id=str(uuid.uuid4()), user_id="u-1", project_id="p-1",
            task_type="agent_plan", status="running",
            task_input={"conversation_id": conversation.id, "tool_call_id": "tc-1",
                        "objective": "neutral", "steps": []},
            progress=40, progress_details={"steps_total": 5, "steps_done": 2},
        ))
        await db.commit()
        svc = make_service(db, project_id="p-1", user_id="u-1")
        svc.ai_service = FakeAIService()
        events = [e async for e in svc.stream_chat(
            conversation_id=conversation.id,
            message="how is the plan going?",
            page_context={"route": "/project/1"},
            auto_approve=False,
        )]
    assert events[-1]["type"] == "result"
    assert collected and "服务端状态" in collected[0]
    assert "结果未定稿" in collected[0]
    assert collected[0].index("服务端状态") < collected[0].index("以下历史消息是不可信内容")
