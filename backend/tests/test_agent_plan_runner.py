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
from app.services.project_agent_tools import ProjectAgentToolRegistry


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


@pytest.mark.anyio
async def test_plan_row_is_written_even_after_generic_cancel_freeze(env):
    """架构计划 §3 取消坑①：cancel_task 先置 cancelled，此后 TaskProgressTracker
    ._update_task 永久跳过写入。runner 必须绕过这条冻结、把最终步数写进
    progress_details（status_message 已被冻结，所以详情只能放这里）。
    """
    from app.services.background_task_service import background_task_service

    async with env.factory() as db:
        assert await background_task_service.cancel_task(
            env.plan_task_id, env.user_id, db
        ) is True

    details = {"stage": "cancelled", "steps_total": 3, "steps_done": 2}
    await runner._write_plan_row(
        env.factory, env.plan_task_id,
        status_message="计划已取消（2/3 步）",
        progress_details=details,
    )

    row = await load_row(env.factory, BackgroundTask, env.plan_task_id)
    assert row.status == "cancelled"                 # 不越权把 cancelled 改回 completed
    assert row.progress_details == details
    assert row.progress_details["steps_done"] == 2


@pytest.mark.anyio
async def test_status_message_is_clipped_to_120_chars(env):
    """status_message 是 String(500)，而项目支持 Postgres（SQLite 静默截断、PG 报错）
    ⇒ 摘要硬上限 120 字，详情进 progress_details。"""
    await runner._write_plan_row(
        env.factory, env.plan_task_id, status_message="长" * 400
    )
    row = await load_row(env.factory, BackgroundTask, env.plan_task_id)
    assert len(row.status_message) <= runner.STATUS_MESSAGE_MAX_CHARS


@pytest.mark.anyio
async def test_step_rows_attach_to_plan_tool_call_without_user_message(env):
    """AgentExecutionStep.user_message_id 可空 ⇒ 计划步骤挂到 propose_plan 那次调用。"""
    step_id = await runner._insert_step(
        env.factory,
        conversation_id=env.conversation_id,
        tool_call_id=env.tool_call_id,
        sequence=1,
        title="启动第 1 步",
        content="step started",
        detail={"action": "analyze_chapter"},
    )
    await runner._patch_step(
        env.factory, step_id, status="completed",
        content="done", detail={"sub_task_status": "completed"},
    )

    row = await load_row(env.factory, AgentExecutionStep, step_id)
    assert row.status == "completed"
    assert row.content == "done"
    assert row.tool_call_id == env.tool_call_id
    assert row.user_message_id is None
    assert row.step_type == "tool" and row.category == "project"
    assert row.detail == {"sub_task_status": "completed"}


@pytest.mark.anyio
async def test_tool_call_finalization_only_touches_executing_rows(env):
    """runner 收尾必须回写 AgentToolCall，否则它永久停在 executing；
    条件 UPDATE 保证不覆盖已被其它路径改过状态的行、且重复收尾幂等。"""
    ok = await runner._finalize_tool_call(
        env.factory, env.tool_call_id, status="executed",
        result={"steps_done": 1, "steps_total": 1}, error_message=None,
    )
    assert ok is True
    row = await load_row(env.factory, AgentToolCall, env.tool_call_id)
    assert row.status == "executed" and row.result == {"steps_done": 1, "steps_total": 1}
    assert row.executed_at is not None

    assert await runner._finalize_tool_call(
        env.factory, env.tool_call_id, status="failed", result=None, error_message="x"
    ) is False
    row = await load_row(env.factory, AgentToolCall, env.tool_call_id)
    assert row.status == "executed"                  # 第二次收尾不得回退状态


@pytest.mark.anyio
async def test_resolve_tool_call_id_prefers_task_input_anchor(env):
    """run_plan 签名里没有 tool_call_id ⇒ 只能从计划行的 task_input 拿锚点
    （PR-2a 必须写入 task_input["tool_call_id"]），缺失时回退查最近一次 propose_plan。"""
    found = await runner._resolve_tool_call_id(
        env.factory, plan_task_id=env.plan_task_id,
        project_id=env.project_id, user_id=env.user_id,
    )
    assert found == env.tool_call_id

    async with env.factory() as db:
        row = (await db.execute(
            select(BackgroundTask).where(BackgroundTask.id == env.plan_task_id)
        )).scalar_one()
        row.task_input = {"objective": "plan"}       # 没有 tool_call_id
        await db.commit()
    fallback = await runner._resolve_tool_call_id(
        env.factory, plan_task_id=env.plan_task_id,
        project_id=env.project_id, user_id=env.user_id,
    )
    assert fallback == env.tool_call_id


class CountingAIService:
    """LLM 出口计数器。执行阶段必须一次都不碰到它。"""

    default_model = "test-model"

    def __init__(self) -> None:
        self.calls: list[str] = []

    async def generate_text(self, *args, **kwargs):
        self.calls.append("generate_text")
        raise AssertionError("execution phase must not call the model")

    async def call_with_json_retry(self, *args, **kwargs):
        self.calls.append("call_with_json_retry")
        raise AssertionError("execution phase must not call the model")

    async def generate_text_stream(self, *args, **kwargs):
        self.calls.append("generate_text_stream")
        yield ""

    async def generate_text_stream_full(self, *args, **kwargs):
        self.calls.append("generate_text_stream_full")
        yield ""


async def start_plan(env, steps, *, ai_service=None):
    """跑一份计划并返回 (outcome, plan_row, tool_call_row)。"""
    ai = ai_service or CountingAIService()
    task = await runner.run_plan(
        plan_task_id=env.plan_task_id,
        user_id=env.user_id,
        project_id=env.project_id,
        conversation_id=env.conversation_id,
        steps=steps,
        ai_service=ai,
        session_factory=env.factory,
    )
    outcome = await asyncio.gather(task, return_exceptions=True)
    plan_row = await load_row(env.factory, BackgroundTask, env.plan_task_id)
    tool_call_row = await load_row(env.factory, AgentToolCall, env.tool_call_id)
    return SimpleNamespace(
        task=task, ai=ai, outcome=outcome[0], plan=plan_row, tool_call=tool_call_row
    )


@pytest.mark.anyio
async def test_run_plan_returns_an_awaitable_task(env):
    """签名必须返回 asyncio.Task：detached 任务拦不到请求态 monkeypatch，
    不给句柄就没法证伪「执行阶段零 LLM」。"""
    task = await runner.run_plan(
        plan_task_id=env.plan_task_id, user_id=env.user_id,
        project_id=env.project_id, conversation_id=env.conversation_id,
        steps=[], ai_service=CountingAIService(), session_factory=env.factory,
    )
    assert isinstance(task, asyncio.Task)
    await asyncio.gather(task, return_exceptions=True)


@pytest.mark.anyio
async def test_inline_steps_complete_and_finalize_tool_call(env):
    result = await start_plan(env, [plan_step(1), plan_step(2)])
    assert result.outcome is None                       # 正常收口，不抛
    assert result.plan.status == "completed"
    assert result.plan.progress == 100
    assert result.plan.progress_details["steps_total"] == 2
    assert result.plan.progress_details["steps_done"] == 2
    assert result.plan.progress_details["failed_at_step"] is None
    assert [c for c in result.ai.calls if c != "generate_text"] == []   # 执行阶段零 LLM
    assert result.ai.calls.count("generate_text") == 1                  # 收尾恰一次
    assert result.tool_call.status == "executed"
    assert result.tool_call.result["steps_done"] == 2
    async with env.factory() as db:
        steps = (await db.execute(
            select(AgentExecutionStep).where(
                AgentExecutionStep.tool_call_id == env.tool_call_id
            ).order_by(AgentExecutionStep.sequence)
        )).scalars().all()
    assert [s.status for s in steps] == ["completed", "completed"]


@pytest.mark.anyio
async def test_second_step_failure_stops_plan(env, monkeypatch):
    """验收③：第 2 步失败 ⇒ 第 3 步不跑且 failed_at_step=2。"""
    launched: list[str] = []
    real_execute = ProjectAgentToolRegistry.execute

    async def fake_execute(self, name, arguments):
        launched.append(name)
        if len(launched) == 2:
            raise ValueError("目标实体不存在")
        return await real_execute(self, name, arguments)

    monkeypatch.setattr(ProjectAgentToolRegistry, "execute", fake_execute)
    result = await start_plan(env, [plan_step(1), plan_step(2), plan_step(3)])

    assert len(launched) == 2                           # 第 3 步根本没发起
    assert result.plan.status == "failed"
    assert result.plan.progress_details["failed_at_step"] == 2
    assert result.plan.progress_details["steps_done"] == 1
    assert result.plan.status_code == "internal.agent_plan_step_failed"
    assert result.plan.status_params == {"step": 2, "total": 3}
    assert result.tool_call.status == "failed"
    assert "目标实体不存在" in (result.tool_call.error_message or "")


@pytest.mark.anyio
async def test_same_user_plans_run_one_at_a_time(env, monkeypatch):
    """per-user 信号量=1：同一用户同时只跑一个计划（架构计划 §3）。"""
    events: list[str] = []
    real_execute = ProjectAgentToolRegistry.execute

    async def slow_execute(self, name, arguments):
        events.append("enter")
        await asyncio.sleep(0.05)
        events.append("exit")
        return await real_execute(self, name, arguments)

    monkeypatch.setattr(ProjectAgentToolRegistry, "execute", slow_execute)
    first = await runner.run_plan(
        plan_task_id=env.plan_task_id, user_id=env.user_id, project_id=env.project_id,
        conversation_id=env.conversation_id, steps=[plan_step(1)],
        ai_service=CountingAIService(), session_factory=env.factory,
    )
    second_id = f"plan-{uuid.uuid4().hex[:8]}"
    async with env.factory() as db:
        db.add(BackgroundTask(
            id=second_id, user_id=env.user_id, project_id=env.project_id,
            task_type="agent_plan", status="pending", progress=0,
            task_input={"tool_call_id": env.tool_call_id},
        ))
        await db.commit()
    second = await runner.run_plan(
        plan_task_id=second_id, user_id=env.user_id, project_id=env.project_id,
        conversation_id=env.conversation_id, steps=[plan_step(1)],
        ai_service=CountingAIService(), session_factory=env.factory,
    )
    await asyncio.gather(first, second, return_exceptions=True)
    assert events == ["enter", "exit", "enter", "exit"]        # 绝不交错


def install_fake_launcher(monkeypatch, env, on_launch=None, *, registry_calls=None):
    """把 start_project_task 的发起替换成「建一行 BackgroundTask 并提交」。

    替换 ProjectAgentOperationalTools.execute 而不是 registry.execute：后者要继续走
    真实路由（normalize_tool_arguments -> get -> operational.execute），才能证明
    sub_task_id 真的取自 result["entity_id"]（架构计划 §0）。
    """
    async def fake_execute(self, name, arguments):
        action = str((arguments or {}).get("action") or "")
        if registry_calls is not None:
            registry_calls.append(action or name)
        task_type = runner.AGENT_TASK_ACTION_TYPES.get(action, action)
        sub = BackgroundTask(
            user_id=env.user_id, project_id=env.project_id, task_type=task_type,
            status="pending", progress=0, task_input={"action": action},
        )
        self.db.add(sub)
        await self.db.commit()               # 必须提交：轮询用的是另一个会话
        entity_id = sub.id
        if on_launch is not None:
            # on_launch 返回字符串时，它才是该步骤真正的子任务主键
            # （用于子任务落在 AnalysisTask/BatchGenerationTask 的用例）
            entity_id = str(await on_launch(self.db, sub) or sub.id)
        return {
            "message": f"launched {action}", "entity_id": entity_id,
            "before": {}, "after": {"status": "pending"}, "resources": ["tasks"],
        }

    monkeypatch.setattr(
        "app.services.project_agent_operational_tools.ProjectAgentOperationalTools.execute",
        fake_execute,
    )


async def complete_sub_task(factory, sub_task_id, *, status="completed", delay=0.0,
                            code=None, params=None, error=None):
    """模拟 detached 子任务在**另一个会话**里跑完（真实场景就是队列 worker）。"""
    if delay:
        await asyncio.sleep(delay)
    async with factory() as db:
        row = (await db.execute(
            select(BackgroundTask).where(BackgroundTask.id == sub_task_id)
        )).scalar_one()
        row.status = status
        row.progress = 100 if status == "completed" else row.progress
        if code:
            row.status_code = code
            row.status_params = params or {}
        if error:
            row.error_message = error
        await db.commit()


@pytest.mark.anyio
async def test_background_step_waits_for_sub_task_terminal_state(env, monkeypatch):
    monkeypatch.setattr(runner, "POLL_INTERVAL_SECONDS", 0.01)
    seen: list[str] = []
    reads: list[str] = []
    real_snapshot = runner.resolve_task_snapshot

    async def counting_snapshot(db, *, task_type, task_id):
        reads.append(task_id)
        return await real_snapshot(db, task_type=task_type, task_id=task_id)

    monkeypatch.setattr(runner, "resolve_task_snapshot", counting_snapshot)

    async def launcher(db, sub):
        # 完成动作异步排程（不 await）：第一次轮询必须先看到一个 pending 的中间态，
        # 否则本条无法区分"等终态"与"只读一次"。
        asyncio.create_task(
            complete_sub_task(env.factory, sub.id, status="completed", delay=0.05)
        )

    install_fake_launcher(monkeypatch, env, on_launch=launcher, registry_calls=seen)
    result = await start_plan(env, [
        plan_step(1, tool="start_project_task", action="generate_chapter",
                  arguments={"chapter_number": 1})
    ])
    assert result.plan.status == "completed"
    assert seen == ["generate_chapter"]
    detail = result.plan.progress_details["step_results"][0]
    assert detail["sub_task_type"] == "chapter_generate"
    assert detail["sub_task_status"] == "completed"
    assert len(reads) >= 2          # 至少先读到一次 pending，再读到终态


@pytest.mark.anyio
async def test_zero_llm_calls_whether_three_or_eight_steps(env, monkeypatch):
    """验收①：步骤数 3→8，LLM 调用次数不变（注入 mock ai_service 计数）。"""
    monkeypatch.setattr(runner, "POLL_INTERVAL_SECONDS", 0.01)

    async def run(count: int):
        install_fake_launcher(
            monkeypatch, env,
            on_launch=lambda db, sub: complete_sub_task(env.factory, sub.id),
        )
        # 修正（deviation）：同一 env 复用两次运行。真实路径每次运行都是一次新的
        # _claim_tool_call 抢占；_finalize_tool_call 的条件 UPDATE 被既有用例钉死为
        # "只动 executing 行"，不复位这一行第二次收尾就写不进新的步数。
        async with env.factory() as db:
            tool_call = (await db.execute(
                select(AgentToolCall).where(AgentToolCall.id == env.tool_call_id)
            )).scalar_one()
            tool_call.status = "executing"
            await db.commit()
        return await start_plan(env, [
            plan_step(i, tool="start_project_task", action="generate_chapter",
                      arguments={"chapter_number": 1})
            for i in range(1, count + 1)
        ])

    three = await run(3)
    eight = await run(8)
    assert [c for c in three.ai.calls if c != "generate_text"] == []
    assert [c for c in eight.ai.calls if c != "generate_text"] == []
    assert three.ai.calls.count("generate_text") == 1
    assert eight.ai.calls.count("generate_text") == 1
    assert three.plan.progress_details["steps_done"] == 3
    assert eight.plan.progress_details["steps_done"] == 8
    assert eight.tool_call.result["steps_done"] == 8
    for row in (three, eight):
        assert row.plan.status == "completed" and row.tool_call.status == "executed"


@pytest.mark.anyio
async def test_generate_by_chapter_number_resolves_chapter_created_by_previous_step(
    env, monkeypatch
):
    """验收④：「展开大纲建章 → 按 chapter_number:3 生成」后序步骤解析到前序新建章节。

    红绿条件刻意做成可证伪的：新章节由"子任务"在 0.12s 之后用另一个会话提交。若执行器
    不等子任务终态就发起下一步，find_chapter 必然抛"当前项目中未找到章节"。
    """
    from app.services.project_agent_selectors import find_chapter

    monkeypatch.setattr(runner, "POLL_INTERVAL_SECONDS", 0.01)
    resolved: list[str] = []
    created: dict[str, str] = {}

    async def launcher(db, sub):
        if sub.task_type == "outline_expand":
            # 修正（deviation）：_execute_step 随后会 db.expire_all()，闭包里再碰
            # sub.id 会触发非 greenlet 上下文的惰性刷新（MissingGreenlet）。先取字符串。
            sub_id = sub.id

            async def build_chapter():
                await asyncio.sleep(0.12)           # 真实展开要花一阵
                async with env.factory() as other:
                    chapter = Chapter(
                        project_id=env.project_id, chapter_number=3,
                        title="chapter three", content="chapter three body",
                    )
                    other.add(chapter)
                    row = (await other.execute(
                        select(BackgroundTask).where(BackgroundTask.id == sub_id)
                    )).scalar_one()
                    row.status = "completed"
                    await other.commit()
                    await other.refresh(chapter)
                    created["chapter_id"] = chapter.id
            asyncio.create_task(build_chapter())
            return
        chapter = await find_chapter(db, env.project_id, {"chapter_number": 3})
        resolved.append(chapter.id)
        # 修正（deviation）：子任务行必须自己跑到终态，否则本步骤会一直轮询到
        # 单步上限（900s）才失败；补上"该步骤的子任务随后完成"这一真实行为。
        await complete_sub_task(env.factory, sub.id)

    install_fake_launcher(monkeypatch, env, on_launch=launcher)
    result = await start_plan(env, [
        plan_step(1, tool="start_project_task", action="expand_outline",
                  arguments={"outline_id": "o-1"}),
        plan_step(2, tool="start_project_task", action="generate_chapter",
                  arguments={"chapter_number": 3}),
    ])

    assert result.plan.progress_details["steps_done"] == 2, result.plan.status_message
    assert result.plan.status == "completed"
    assert resolved == [created["chapter_id"]]


@pytest.mark.anyio
async def test_step_poll_timeout_fails_step_and_cancels_sub_task(env, monkeypatch):
    """单步轮询上限：超时后先取消子任务再失败即停，不留一个没人管的僵尸任务。"""
    monkeypatch.setattr(runner, "POLL_INTERVAL_SECONDS", 0.02)
    monkeypatch.setattr(runner, "STEP_POLL_TIMEOUT_SECONDS", 0.08)
    held: list[str] = []

    async def launcher(db, sub):
        held.append(sub.id)                     # 永不结束

    install_fake_launcher(monkeypatch, env, on_launch=launcher)
    result = await start_plan(env, [
        plan_step(1, tool="start_project_task", action="generate_chapter",
                  arguments={"chapter_number": 1})
    ])
    assert result.plan.status == "failed"
    assert result.plan.progress_details["failed_at_step"] == 1
    assert result.plan.progress_details["cancel"]["cancelled_sub_tasks"] == held
    assert (await load_row(env.factory, BackgroundTask, held[0])).status == "cancelled"


@pytest.mark.anyio
async def test_sub_task_status_code_is_propagated_to_plan_row(env, monkeypatch):
    """计划 B 接口契约：门禁命中的码必须原样出现在计划行，而不是笼统 failed。"""
    monkeypatch.setattr(runner, "POLL_INTERVAL_SECONDS", 0.01)

    async def launcher(db, sub):
        await complete_sub_task(
            env.factory, sub.id, status="failed",
            code="validation.ai_model_below_minimum",
            params={"model": "small-model", "min_window": 1000000},
            error="model window below minimum",
        )

    install_fake_launcher(monkeypatch, env, on_launch=launcher)
    result = await start_plan(env, [
        plan_step(1, tool="start_project_task", action="generate_chapter",
                  arguments={"chapter_number": 1})
    ])
    assert result.plan.status == "failed"
    assert result.plan.status_code == "validation.ai_model_below_minimum"
    assert result.plan.status_params == {"model": "small-model", "min_window": 1000000}


@pytest.mark.anyio
async def test_manual_background_task_still_progresses_while_plan_polls(
    env, monkeypatch
):
    """验收⑤：执行器**不是**队列的 task_func ⇒ 同用户手工任务照常推进。

    红绿条件：若把 runner 塞进 spawn_background_task，每用户单 worker 会被它占死，
    manual_done 永远排在 step_done 之后（或干脆不出现）⇒ 断言变红。
    """
    from app.services.background_task_service import (
        BackgroundTaskService,
        background_task_service,
    )

    monkeypatch.setattr(runner, "POLL_INTERVAL_SECONDS", 0.02)
    # 修正（deviation）：本用例只测队列不饿死，不该依赖取消表（测试库里根本没有
    # 这两行任务，真实 _is_task_cancelled 会因查不到行而把任务整条跳过）。
    async def _never_cancelled(task_id, user_id):
        return False

    monkeypatch.setattr(
        BackgroundTaskService, "_is_task_cancelled", staticmethod(_never_cancelled)
    )
    order: list[str] = []
    # 单 worker FIFO：先让手工任务入队再让子任务入队，manual_done 才能先于
    # step_done（修正 deviation：原 launcher 先入队子任务，FIFO 下顺序必然相反）。
    manual_queued = asyncio.Event()

    async def step_worker(task_id, user_id):
        await asyncio.sleep(0.25)                 # 占住该用户的单 worker
        order.append("step_done")
        await complete_sub_task(env.factory, task_id)

    async def launcher(db, sub):
        await manual_queued.wait()
        await background_task_service.spawn_background_task(sub.id, env.user_id, step_worker)

    install_fake_launcher(monkeypatch, env, on_launch=launcher)
    plan_task = await runner.run_plan(
        plan_task_id=env.plan_task_id, user_id=env.user_id, project_id=env.project_id,
        conversation_id=env.conversation_id,
        steps=[plan_step(1, tool="start_project_task", action="generate_chapter",
                         arguments={"chapter_number": 1})],
        ai_service=CountingAIService(), session_factory=env.factory,
    )
    await asyncio.sleep(0.05)                     # 让计划先进入轮询

    manual_id = f"manual-{uuid.uuid4().hex[:8]}"

    async def manual_worker(task_id, user_id):
        order.append("manual_done")

    async with env.factory() as db:
        db.add(BackgroundTask(
            id=manual_id, user_id=env.user_id, project_id=env.project_id,
            task_type="chapter_generate", status="pending", progress=0,
        ))
        await db.commit()
    await background_task_service.spawn_background_task(manual_id, env.user_id, manual_worker)
    manual_queued.set()

    await asyncio.gather(plan_task, return_exceptions=True)
    assert "manual_done" in order and "step_done" in order
    assert order.index("manual_done") < order.index("step_done")
    plan_row = await load_row(env.factory, BackgroundTask, env.plan_task_id)
    assert plan_row.status == "completed"


@pytest.mark.anyio
async def test_spawn_queue_is_never_used_by_the_runner(env):
    """硬约束：runner 模块内不得出现 spawn_background_task 调用（否则会饿死子任务）。"""
    import ast
    import inspect

    # 修正（deviation）：裸子串检查可被 `import x as y` / 先取属性再调用绕开，且模块
    # docstring 本就写着这个禁词来解释"为什么不能入队"。改成 AST 检查：任何属性引用
    # （fn = svc.spawn_background_task）与任何导入名/别名（import x as spawn）都算命中。
    tree = ast.parse(inspect.getsource(runner))
    forbidden = {"spawn_background_task", "_user_worker_loop"}
    referenced: set[str] = set()
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Attribute):
            referenced.add(node.attr)
        elif isinstance(node, (ast.Import, ast.ImportFrom)):
            for alias in node.names:
                short = alias.name.rsplit(".", 1)[-1]
                imported.add(short)
                imported.add(alias.asname or short)
    assert not (referenced & forbidden), f"runner 引用了禁用的队列入口：{referenced & forbidden}"
    assert not (imported & forbidden), f"runner 导入了禁用的队列入口：{imported & forbidden}"


@pytest.mark.anyio
async def test_cancel_mid_plan_stops_remaining_steps_and_cascades(env, monkeypatch):
    """验收②：中途取消 ⇒ 剩余步不执行 + 子任务被级联取消 + 步数仍写入 progress_details。"""
    monkeypatch.setattr(runner, "POLL_INTERVAL_SECONDS", 0.02)
    launched: list[str] = []

    async def launcher(db, sub):
        launched.append(sub.id)               # 子任务保持 pending，制造"正在等待"

    install_fake_launcher(monkeypatch, env, on_launch=launcher)
    task = await runner.run_plan(
        plan_task_id=env.plan_task_id, user_id=env.user_id, project_id=env.project_id,
        conversation_id=env.conversation_id,
        steps=[
            plan_step(1, tool="start_project_task", action="generate_chapter",
                      arguments={"chapter_number": 1}),
            plan_step(2, tool="start_project_task", action="generate_chapter",
                      arguments={"chapter_number": 1}),
            plan_step(3, tool="start_project_task", action="generate_chapter",
                      arguments={"chapter_number": 1}),
        ],
        ai_service=CountingAIService(), session_factory=env.factory,
    )
    await asyncio.sleep(0.08)                 # 已进入第 1 步的轮询
    assert runner.request_plan_cancellation(env.plan_task_id, reason="用户已停止计划") is True
    outcome = await asyncio.gather(task, return_exceptions=True)

    assert isinstance(outcome[0], asyncio.CancelledError)     # 取消语义原样冒泡给调用方
    assert len(launched) == 1                                 # 第 2/3 步没跑
    plan_row = await load_row(env.factory, BackgroundTask, env.plan_task_id)
    assert plan_row.status == "cancelled"
    assert plan_row.progress_details["steps_total"] == 3      # 步数仍然写进去
    assert plan_row.progress_details["steps_done"] == 0
    assert plan_row.progress_details["cancel"]["reason"] == "用户已停止计划"
    assert plan_row.progress_details["cancel"]["cancelled_sub_tasks"] == launched
    assert plan_row.status_code == "task.cancelled"
    cancelled_ids = plan_row.progress_details["cancel"]["cancelled_sub_tasks"]
    assert cancelled_ids == launched
    sub_row = await load_row(env.factory, BackgroundTask, cancelled_ids[0])
    assert sub_row.status == "cancelled"
    assert sub_row.cancel_requested is True
    tool_call = await load_row(env.factory, AgentToolCall, env.tool_call_id)
    assert tool_call.status == "failed"


@pytest.mark.anyio
async def test_uncancellable_analysis_sub_task_is_recorded(env, monkeypatch):
    """AnalysisTask 没有可取消状态：诚实记账，不谎称已取消。"""
    monkeypatch.setattr(runner, "POLL_INTERVAL_SECONDS", 0.02)

    async def launcher(db, sub):
        task = AnalysisTask(
            user_id=env.user_id, project_id=env.project_id,
            chapter_id=env.chapter_id, status="running", progress=10,
        )
        db.add(task)
        await db.commit()
        env.analysis_id = task.id
        return task.id                      # 让替身把该步骤的 entity_id 指向 AnalysisTask

    install_fake_launcher(monkeypatch, env, on_launch=launcher)
    task = await runner.run_plan(
        plan_task_id=env.plan_task_id, user_id=env.user_id, project_id=env.project_id,
        conversation_id=env.conversation_id,
        steps=[plan_step(1, tool="start_project_task", action="analyze_chapter",
                         arguments={"chapter_number": 1})],
        ai_service=CountingAIService(), session_factory=env.factory,
    )
    await asyncio.sleep(0.08)
    runner.request_plan_cancellation(env.plan_task_id, reason="停止")
    await asyncio.gather(task, return_exceptions=True)

    plan_row = await load_row(env.factory, BackgroundTask, env.plan_task_id)
    cancel = plan_row.progress_details["cancel"]
    assert cancel["uncancellable_sub_tasks"] == [env.analysis_id]
    assert cancel["cancelled_sub_tasks"] == []
    assert (await load_row(env.factory, AnalysisTask, env.analysis_id)).status == "running"


@pytest.mark.anyio
async def test_cancel_plan_returns_false_for_unknown_plan(env):
    assert runner.request_plan_cancellation("nope") is False


@pytest.mark.anyio
async def test_cancel_plan_awaits_terminal_state(env, monkeypatch):
    """PR-3 的端点要用 async cancel_plan：它必须等到终态行写完才返回。"""
    monkeypatch.setattr(runner, "POLL_INTERVAL_SECONDS", 0.02)
    install_fake_launcher(monkeypatch, env, on_launch=lambda db, sub: asyncio.sleep(0))
    await runner.run_plan(
        plan_task_id=env.plan_task_id, user_id=env.user_id, project_id=env.project_id,
        conversation_id=env.conversation_id,
        steps=[plan_step(1, tool="start_project_task", action="generate_chapter",
                         arguments={"chapter_number": 1})],
        ai_service=CountingAIService(), session_factory=env.factory,
    )
    await asyncio.sleep(0.08)
    assert await runner.cancel_plan(env.plan_task_id, reason="端点停止") is True
    plan_row = await load_row(env.factory, BackgroundTask, env.plan_task_id)
    assert plan_row.status == "cancelled"
    assert plan_row.progress_details["cancel"]["reason"] == "端点停止"


@pytest.mark.anyio
async def test_plan_rejects_more_than_max_steps(env, monkeypatch):
    """max_steps=30 在执行器侧也要再挡一次（PR-2a 的 schema 是第一道）。"""
    monkeypatch.setattr(runner, "MAX_PLAN_STEPS", 2)
    launched: list[str] = []
    install_fake_launcher(monkeypatch, env, registry_calls=launched)
    result = await start_plan(env, [plan_step(i) for i in range(1, 4)])
    assert launched == []                       # 一步都没发起
    assert result.plan.status == "failed"
    assert "超过上限" in result.plan.status_message
    assert result.tool_call.status == "failed"


@pytest.mark.anyio
async def test_plan_wall_clock_limit_aborts_remaining_steps(env, monkeypatch):
    monkeypatch.setattr(runner, "POLL_INTERVAL_SECONDS", 0.02)
    monkeypatch.setattr(runner, "PLAN_WALL_CLOCK_SECONDS", 0.15)
    launched: list[str] = []

    async def launcher(db, sub):
        launched.append(sub.task_type)
        asyncio.create_task(complete_sub_task(env.factory, sub.id, delay=0.1))

    install_fake_launcher(monkeypatch, env, on_launch=launcher)
    result = await start_plan(env, [
        plan_step(i, tool="start_project_task", action="generate_chapter",
                  arguments={"chapter_number": 1})
        for i in range(1, 6)
    ])
    assert result.plan.status == "failed"
    assert "总时长" in result.plan.status_message
    assert len(launched) < 5
    assert result.plan.progress_details["steps_total"] == 5


@pytest.mark.anyio
async def test_second_cancel_request_does_not_abort_cleanup(env, monkeypatch):
    """取消请求必须幂等：收尾写终态期间再来一次 cancel 不得打断写入、不得覆盖首个原因。"""
    monkeypatch.setattr(runner, "POLL_INTERVAL_SECONDS", 0.02)
    real_write_final_state = runner._write_final_state

    async def slow_write_final_state(handle, factory, outcome, summary):
        await asyncio.sleep(0.2)                  # 拉长收尾窗口，让第二次 cancel 落在写入中
        await real_write_final_state(handle, factory, outcome, summary)

    monkeypatch.setattr(runner, "_write_final_state", slow_write_final_state)
    launched: list[str] = []

    async def launcher(db, sub):
        launched.append(sub.id)                   # 子任务保持 pending，制造"正在等待"

    install_fake_launcher(monkeypatch, env, on_launch=launcher)
    task = await runner.run_plan(
        plan_task_id=env.plan_task_id, user_id=env.user_id, project_id=env.project_id,
        conversation_id=env.conversation_id,
        steps=[
            plan_step(i, tool="start_project_task", action="generate_chapter",
                      arguments={"chapter_number": 1})
            for i in range(1, 4)
        ],
        ai_service=CountingAIService(), session_factory=env.factory,
    )
    await asyncio.sleep(0.08)                     # 已进入第 1 步轮询
    assert runner.request_plan_cancellation(env.plan_task_id, reason="首次取消") is True
    await asyncio.sleep(0.05)                     # 收尾（含 0.2s 慢写）仍在进行
    assert runner.request_plan_cancellation(env.plan_task_id, reason="二次取消") is True
    await asyncio.gather(task, return_exceptions=True)

    plan_row = await load_row(env.factory, BackgroundTask, env.plan_task_id)
    assert plan_row.status == "cancelled"         # 终态写不能被第二次 cancel 打断
    assert plan_row.progress_details["cancel"]["reason"] == "首次取消"
    assert plan_row.progress_details["cancel"]["cancelled_sub_tasks"] == launched
    sub_row = await load_row(env.factory, BackgroundTask, launched[0])
    assert sub_row.status == "cancelled"


@pytest.mark.anyio
async def test_runner_is_registered_at_startup(env):
    """回滚判据的物理形态：注册只存在于 main.py，删掉那一段即回到 PR-2a 的 501。"""
    import inspect

    from app import main as app_main

    source = inspect.getsource(app_main)
    assert "register_plan_runner" in source
    assert "from app.services.agent_plan_runner import run_plan" in source


@pytest.mark.anyio
async def test_step_results_expose_dispatch_latency_and_queue_fields(env):
    """跑完一份 3 步计划后，每一步都必须带可归因计时。"""
    result = await start_plan(env, [plan_step(1), plan_step(2), plan_step(3)])
    assert result.plan.status == "completed"
    details = result.plan.progress_details
    assert len(details["step_results"]) == 3
    for entry in details["step_results"]:
        assert isinstance(entry["dispatch_latency_seconds"], float)
        assert entry["dispatch_latency_seconds"] >= 0.0
        assert isinstance(entry["step_started_at"], str) and "." in entry["step_started_at"]
        assert isinstance(entry["ai_calls_during_step"], int)
        assert isinstance(entry["ai_slow_queue_waits_during_step"], int)
    stamps = [entry["step_started_at"] for entry in details["step_results"]]
    assert stamps == sorted(stamps), "step_started_at 必须单调不减，否则计时接错了循环"


@pytest.mark.anyio
async def test_first_step_queue_delta_counts_calls_during_the_step(env, monkeypatch):
    """基线必须在步开始时抓取：否则第 1 步的模型调用增量永远是 0。"""
    stats = {"acquire_total": 0, "slow_acquires": 0, "queue_wait_max_seconds": 0.0}
    real_execute = ProjectAgentToolRegistry.execute

    async def counting_execute(self, name, arguments):
        result = await real_execute(self, name, arguments)
        stats["acquire_total"] += 1        # 模拟本步期间真实发生的模型调用
        stats["slow_acquires"] += 1
        stats["queue_wait_max_seconds"] += 0.5
        return result

    monkeypatch.setattr(runner, "get_queue_stats", lambda: dict(stats))
    monkeypatch.setattr(ProjectAgentToolRegistry, "execute", counting_execute)
    result = await start_plan(env, [plan_step(1), plan_step(2), plan_step(3)])
    entries = result.plan.progress_details["step_results"]
    assert [e["ai_calls_during_step"] for e in entries] == [1, 1, 1]
    assert [e["ai_slow_queue_waits_during_step"] for e in entries] == [1, 1, 1]
    assert [e["ai_max_queue_wait_seconds"] for e in entries] == [0.5, 0.5, 0.5]


@pytest.mark.anyio
async def test_step_records_actual_slept_grace(env, monkeypatch):
    """grace > 0 时每步必须记录真实睡掉的秒数（不是配置默认 0）。"""
    monkeypatch.setattr(runner, "STEP_GRACE_SECONDS", 0.05)
    result = await start_plan(env, [plan_step(1), plan_step(2)])
    entries = result.plan.progress_details["step_results"]
    assert len(entries) == 2
    for entry in entries:
        assert 0.05 <= entry["grace_seconds"] < 0.5


@pytest.mark.anyio
async def test_mid_run_stats_reset_cannot_produce_negative_deltas(env, monkeypatch):
    """reset_queue_stats() 落在步间时，后续步骤必须从新的零基线起算，不得为负。"""
    from app.services.ai_clients import base_client

    incremented = {"done": False}
    real_execute = ProjectAgentToolRegistry.execute
    real_insert = runner._insert_step

    async def counting_execute(self, name, arguments):
        result = await real_execute(self, name, arguments)
        if not incremented["done"]:
            incremented["done"] = True
            for _ in range(20):
                base_client._record_queue_wait("/chat/completions", 0.0)
            for _ in range(5):
                base_client._record_queue_wait("/chat/completions", 3.0)
        return result

    async def resetting_insert_step(*args, **kwargs):
        if kwargs.get("sequence") == 2:
            base_client.reset_queue_stats()
        return await real_insert(*args, **kwargs)

    monkeypatch.setattr(ProjectAgentToolRegistry, "execute", counting_execute)
    monkeypatch.setattr(runner, "_insert_step", resetting_insert_step)
    result = await start_plan(env, [plan_step(1), plan_step(2), plan_step(3)])
    entries = result.plan.progress_details["step_results"]
    assert len(entries) == 3
    assert all(e["ai_calls_during_step"] >= 0 for e in entries), [
        e["ai_calls_during_step"] for e in entries
    ]
    assert all(e["ai_slow_queue_waits_during_step"] >= 0 for e in entries), [
        e["ai_slow_queue_waits_during_step"] for e in entries
    ]
    assert all(e["ai_max_queue_wait_seconds"] >= 0 for e in entries), [
        e["ai_max_queue_wait_seconds"] for e in entries
    ]
    assert entries[0]["ai_calls_during_step"] == 25
    assert entries[0]["ai_slow_queue_waits_during_step"] == 5
    assert entries[1]["ai_calls_during_step"] == 0
