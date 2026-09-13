"""PR-2b 确定性计划执行器单测（架构计划 A §3 + PR-2b 验收 ①-⑤）。

所有用例走注入的 session_factory + 类级 monkeypatch，绝不实例化 TaskProgressTracker
（它会自己 get_engine 连到应用开发库）。
"""
import asyncio
import os
import uuid
from types import SimpleNamespace

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.database import Base
from app.models.analysis_task import AnalysisTask
from app.models.background_task import BackgroundTask
from app.models.batch_generation_task import BatchGenerationTask
from app.models.chapter import Chapter
from app.models.project import Project
from app.models.project_agent import (
    AgentConversation,
    AgentExecutionStep,
    AgentToolCall,
)
from app.services import agent_plan_runner as runner


@pytest.fixture
async def env():
    """临时文件 SQLite + 项目/会话/计划任务行/已被抢占的 propose_plan 行。"""
    db_path = f"/tmp/test_plan_runner_{uuid.uuid4().hex}.db"
    engine = create_async_engine(f"sqlite+aiosqlite:///{db_path}")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(bind=engine, class_=AsyncSession, expire_on_commit=False)
    # 队列与信号量都按 user_id 维度存在，且 anyio 每个用例一个新 loop ⇒ user 必须唯一
    user_id = f"u-{uuid.uuid4().hex[:8]}"
    project_id = f"p-{uuid.uuid4().hex[:8]}"
    async with factory() as db:
        db.add(Project(id=project_id, user_id=user_id, title="plan project"))
        conversation = AgentConversation(user_id=user_id, project_id=project_id, title="t")
        db.add(conversation)
        await db.flush()
        chapter = Chapter(
            project_id=project_id, chapter_number=1, title="chapter one", content="body"
        )
        db.add(chapter)
        tool_call = AgentToolCall(
            conversation_id=conversation.id,
            user_id=user_id,
            project_id=project_id,
            tool_name="propose_plan",
            arguments={"objective": "plan", "steps": []},
            risk_level=2,
            requires_confirmation=True,
            status="executing",
        )
        db.add(tool_call)
        await db.flush()
        plan = BackgroundTask(
            user_id=user_id,
            project_id=project_id,
            task_type="agent_plan",
            status="pending",
            progress=0,
            task_input={"tool_call_id": tool_call.id, "objective": "plan"},
        )
        db.add(plan)
        await db.commit()
        yield SimpleNamespace(
            engine=engine,
            factory=factory,
            user_id=user_id,
            project_id=project_id,
            conversation_id=conversation.id,
            chapter_id=chapter.id,
            plan_task_id=plan.id,
            tool_call_id=tool_call.id,
        )
    await engine.dispose()
    if os.path.exists(db_path):
        os.remove(db_path)


def plan_step(index: int, *, tool: str = "list_background_tasks", action: str = "",
              arguments: dict | None = None) -> dict:
    """构造一条计划步骤；默认用只读工具（内联完成，无子任务）。"""
    return {
        "id": f"s{index}",
        "tool": tool,
        "action": action,
        "arguments": dict(arguments or {}),
        "note": f"step {index}",
    }


async def load_row(factory, model, row_id):
    async with factory() as session:
        return (await session.execute(
            select(model).where(model.id == row_id)
        )).scalar_one_or_none()


@pytest.mark.anyio
async def test_resolve_task_snapshot_maps_task_type_to_table(env):
    """三张表用同一个主键字符串，仍必须按 task_type 各自解析到正确行。"""
    shared_id = f"t-{uuid.uuid4().hex[:8]}"
    async with env.factory() as db:
        db.add(BackgroundTask(
            id=shared_id, user_id=env.user_id, project_id=env.project_id,
            task_type="chapter_generate", status="running", progress=40,
        ))
        db.add(BatchGenerationTask(
            id=shared_id, user_id=env.user_id, project_id=env.project_id,
            start_chapter_number=1, chapter_count=4, chapter_ids=[],
            status="running", total_chapters=4, completed_chapters=1,
        ))
        db.add(AnalysisTask(
            id=shared_id, user_id=env.user_id, project_id=env.project_id,
            chapter_id=env.chapter_id, status="completed", progress=100,
        ))
        await db.commit()

    async with env.factory() as db:
        bg = await runner.resolve_task_snapshot(
            db, task_type="chapter_generate", task_id=shared_id
        )
        batch = await runner.resolve_task_snapshot(
            db, task_type="chapter_batch", task_id=shared_id
        )
        analysis = await runner.resolve_task_snapshot(
            db, task_type="chapter_analysis", task_id=shared_id
        )

    assert (bg.status, bg.progress, bg.finished) == ("running", 40, False)
    assert (batch.status, batch.finished) == ("running", False)
    assert batch.progress == 25          # 1/4 章，形状照 api/tasks.py:48-53
    assert (analysis.status, analysis.progress, analysis.finished) == ("completed", 100, True)


@pytest.mark.anyio
async def test_resolve_task_snapshot_rejects_unknown_task_type(env):
    """未知 task_type 必须 fail-loud，禁止悄悄回落到 BackgroundTask。"""
    with pytest.raises(ValueError, match="未知的计划步骤任务类型"):
        async with env.factory() as db:
            await runner.resolve_task_snapshot(db, task_type="wizard", task_id="x")


@pytest.mark.anyio
async def test_resolve_task_snapshot_reports_missing_row_as_none(env):
    async with env.factory() as db:
        assert await runner.resolve_task_snapshot(
            db, task_type="chapter_generate", task_id="nope"
        ) is None


@pytest.mark.anyio
async def test_resolve_task_snapshot_accepts_agent_plan_for_running_lookup(env):
    """agent_plan 自身的行也要能查——PR-2c 的运行中护栏复用同一入口。"""
    async with env.factory() as db:
        snap = await runner.resolve_task_snapshot(
            db, task_type="agent_plan", task_id=env.plan_task_id
        )
    assert (snap.status, snap.finished) == ("pending", False)
