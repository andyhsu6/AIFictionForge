"""PR-4：计划执行器的每步计时（发起延迟 / 实际 grace / 期间 AI 排队次数）。"""
import inspect
import re
import time

import pytest

from app.services import agent_plan_runner as runner


def test_step_timing_reports_latency_and_queue_deltas(monkeypatch):
    monkeypatch.setattr(runner, "get_queue_stats", lambda: {
        "acquire_total": 6,
        "slow_acquires": 2,
        "queue_wait_max_seconds": 4.5,
        "waiters": 1,
    })
    monkeypatch.setattr(time, "monotonic", lambda: 1000.5)

    timing = runner._step_timing(
        step_started_at="2026-09-13T10:00:00.000",
        dispatch_t0=1000.0,
        grace_seconds=3.0,
        stats_before={"acquire_total": 3, "slow_acquires": 0, "queue_wait_max_seconds": 2.0},
    )
    assert timing == {
        "step_started_at": "2026-09-13T10:00:00.000",
        "dispatch_latency_seconds": 0.5,
        "grace_seconds": 3.0,
        "ai_calls_during_step": 3,
        "ai_slow_queue_waits_during_step": 2,
        "ai_max_queue_wait_seconds": 2.5,   # 每步增量，不是进程累计最大值
    }


def test_iso_now_is_parseable_and_millisecond_precision():
    from datetime import datetime

    stamp = runner._iso_now()
    parsed = datetime.fromisoformat(stamp)
    assert "." in stamp                      # 毫秒级，否则 3s grace 的间隔断言会因取整失真
    assert parsed.microsecond % 1000 == 0


def test_every_step_result_carries_timing():
    src = inspect.getsource(runner)
    appends = re.findall(r"step_results\.append\(", src)
    assert appends, "找不到 step_results 追加点 ⇒ PR-2b 锚点已漂移，停下来报 DEVIATIONS"
    assert src.count("**timing") == len(appends), (
        f"每个 step_results 追加点都必须带 **timing（找到 {len(appends)} 处追加，"
        f"只有 {src.count('**timing')} 处计时）"
    )


def test_step_grace_sleep_is_instrumented():
    src = inspect.getsource(runner)
    assert "asyncio.sleep(" in src
    assert re.search(r"grace\s*=\s*", src), "grace 必须落在一个具名局部变量上，才能记录实睡秒数"


def test_runner_logs_per_step_dispatch_latency():
    src = inspect.getsource(runner)
    assert "dispatch_latency=" in src and "plan_task_id=" in src
