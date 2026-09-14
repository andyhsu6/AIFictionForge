"""PR-4：步间 grace 的默认值与「唯一消费点」守卫。

夹具复用 PR-2b 的假环境（tests/test_agent_plan_runner.py 的 env/plan_step/start_plan），
不新建第二套假环境。
"""
import asyncio
import inspect
import re
from datetime import datetime

import pytest
from sqlalchemy import select

from app.config import Settings
from app.models.background_task import BackgroundTask
from app.models.project_agent import AgentExecutionStep
from app.services import agent_plan_runner as runner
from test_agent_plan_runner import (  # noqa: F401
    CountingAIService,
    env,
    load_row,
    plan_step,
    start_plan,
)

KEY = "agent_plan_step_grace_seconds"


def test_default_step_grace_is_three_seconds():
    """SQLite WAL 可见性靠它；0.0 意味着「完成即写 + 立刻再发起」，竞态会变常态。"""
    assert Settings.model_fields[KEY].default == 3.0
    assert Settings().agent_plan_step_grace_seconds == 3.0


def test_runner_reads_the_setting_and_not_the_bare_constant():
    src = inspect.getsource(runner)
    assert f'"{KEY}"' in src, f"runner 必须经 {KEY} 读取 grace，否则 PR-2c 的配置面是空转的"
    assert "asyncio.sleep(STEP_GRACE_SECONDS)" not in src, "禁止直接睡裸常量：配置会被静默忽略"
    # _DEFAULT_LIMITS 里的同名键只是默认值映射，不是读取点；读取点只能是 _limit(...) 调用。
    reads = re.findall(r'_limit\(\s*"agent_plan_step_grace_seconds"\s*,', src)
    assert len(reads) == 1, f"grace 只能有一个 _limit 读取点，找到 {len(reads)} 处就会互相覆盖"
    code_lines = [ln.split("#", 1)[0] for ln in src.splitlines()]
    direct = [ln for ln in code_lines if "settings.agent_plan_step_grace_seconds" in ln]
    assert not direct, f"禁止绕过 _limit 直读 settings：{direct[:2]}"


def test_fallback_constant_does_not_undercut_default():
    """fallback 只在设置读不出来时兜底；它必须 >= 默认值，否则配错就等于没有 grace。"""
    assert float(runner.STEP_GRACE_SECONDS) >= Settings.model_fields[KEY].default


@pytest.mark.anyio
async def test_default_grace_paces_steps_by_at_least_three_seconds(env):  # noqa: F811
    """端到端：不覆盖配置，跑一份 2 步计划，相邻 step_started_at 间隔必须 >= 3s。

    这是架构计划 PR-4 验收「步间 grace 时间戳间隔 ≥3s」的字面落地，约耗时 6s。
    """
    result = await start_plan(env, [plan_step(1), plan_step(2)])
    entries = result.plan.progress_details["step_results"]
    assert len(entries) == 2
    stamps = [datetime.fromisoformat(e["step_started_at"]) for e in entries]
    gap = (stamps[1] - stamps[0]).total_seconds()
    assert gap >= 3.0, f"步间只隔了 {gap:.3f}s：grace 没接上或被别处覆盖"
    assert float(entries[0]["grace_seconds"]) >= 3.0, "实睡时长必须被记录，且不小于配置值"


@pytest.mark.anyio
async def test_cancel_during_grace_leaves_step_row_completed(env, monkeypatch):  # noqa: F811
    """取消落在 grace sleep 上时，步骤行不得停在 running，步数必须自洽（顺序约束）。"""
    monkeypatch.setattr(runner.settings, KEY, 0.5, raising=True)
    task = await runner.run_plan(
        plan_task_id=env.plan_task_id, user_id=env.user_id, project_id=env.project_id,
        conversation_id=env.conversation_id,
        steps=[plan_step(1)],
        ai_service=CountingAIService(), session_factory=env.factory,
    )
    await asyncio.sleep(0.2)          # 步骤已执行完，取消落在 grace sleep 窗口内
    assert runner.request_plan_cancellation(env.plan_task_id, reason="grace 期间停止") is True
    await asyncio.gather(task, return_exceptions=True)

    plan_row = await load_row(env.factory, BackgroundTask, env.plan_task_id)
    assert plan_row.status == "cancelled"
    details = plan_row.progress_details
    assert details["steps_done"] == len(details["step_results"]) == 1
    async with env.factory() as db:
        steps = (await db.execute(
            select(AgentExecutionStep).where(
                AgentExecutionStep.tool_call_id == env.tool_call_id
            )
        )).scalars().all()
    assert [s.status for s in steps] == ["completed"]


class _Recorder:
    def __init__(self) -> None:
        self.infos: list[str] = []

    def info(self, msg, *args, **kwargs):
        self.infos.append(msg % args if args else str(msg))

    def warning(self, msg, *args, **kwargs):
        self.infos.append(msg % args if args else str(msg))

    def debug(self, msg, *args, **kwargs):
        self.infos.append(str(msg))

    def error(self, msg, *args, **kwargs):
        self.infos.append(msg % args if args else str(msg))


@pytest.mark.anyio
async def test_twelve_step_plan_logs_every_step_latency(env, monkeypatch):  # noqa: F811
    """12 步 × 极小 grace：每步一条含 dispatch_latency 与 queue 字段的日志。"""
    recorder = _Recorder()
    monkeypatch.setattr(runner, "logger", recorder)
    monkeypatch.setattr(runner.settings, KEY, 0.01, raising=True)

    result = await start_plan(env, [plan_step(i) for i in range(1, 13)])
    timed = [line for line in recorder.infos if "dispatch_latency=" in line and "ai_slow_queue_waits=" in line]
    assert len(timed) >= 12, f"只记录到 {len(timed)} 条逐步计时日志，应有 >=12"
    assert any("计划计时汇总" in line for line in recorder.infos)
    assert len(result.plan.progress_details["step_results"]) == 12
    for entry in result.plan.progress_details["step_results"]:
        assert set(("step_started_at", "dispatch_latency_seconds", "grace_seconds",
                    "ai_calls_during_step", "ai_slow_queue_waits_during_step")) <= set(entry)
