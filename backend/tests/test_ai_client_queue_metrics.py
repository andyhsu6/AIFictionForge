"""PR-4：AI 客户端并发排队的可观测计数（waiters / queue_wait / slow_acquires）。"""
import asyncio

from app.services.ai_clients import base_client
from app.services.ai_clients.base_client import (
    _acquire_slot,
    get_queue_stats,
    reset_queue_stats,
)


class _Recorder:
    """logger 替身：项目用自研 get_logger，propagate 可能被关掉 ⇒ caplog 不可靠。"""

    def __init__(self) -> None:
        self.warnings: list[str] = []
        self.debugs: list[str] = []

    def warning(self, msg, *args):
        self.warnings.append(msg % args if args else str(msg))

    def debug(self, msg, *args):
        self.debugs.append(msg % args if args else str(msg))

    def info(self, msg, *args):
        pass

    def error(self, msg, *args):
        pass


async def _contend(sem: asyncio.Semaphore) -> int:
    async with _acquire_slot(sem, 1, "/chat/completions"):
        return int(base_client.get_queue_stats()["waiters"])


def test_acquire_slot_counts_waiters_and_latency(monkeypatch):
    reset_queue_stats()
    monkeypatch.setattr(base_client, "logger", _Recorder())
    monkeypatch.setattr(base_client, "QUEUE_WAIT_WARN_SECONDS", 0.02)

    async def scenario():
        sem = asyncio.Semaphore(1)
        async with _acquire_slot(sem, 1, "/chat/completions"):
            held = get_queue_stats()
            task = asyncio.ensure_future(_contend(sem))
            await asyncio.sleep(0.03)
            waiters_while_held = int(get_queue_stats()["waiters"])
        waited = await task
        return held, waiters_while_held, waited, get_queue_stats()

    held, waiters_while_held, waited, after = asyncio.run(scenario())
    assert held["max_concurrent_requests"] == 1
    assert held["active"] == 1
    assert held["acquire_total"] == 1
    assert waiters_while_held == 1          # 排队者真的被计数，而不是只能看见"变慢"
    assert waited == 0                      # contender 拿到许可时已无其他等待者
    assert after["waiters"] == 0
    assert after["active"] == 0             # 释放后不泄漏计数
    assert after["acquire_total"] == 2
    assert after["queue_wait_max_seconds"] >= 0.02


def test_active_count_released_on_exception(monkeypatch):
    reset_queue_stats()
    monkeypatch.setattr(base_client, "logger", _Recorder())

    async def scenario():
        sem = asyncio.Semaphore(1)
        try:
            async with _acquire_slot(sem, 1, "/messages"):
                raise RuntimeError("boom")
        except RuntimeError:
            pass
        return get_queue_stats()

    stats = asyncio.run(scenario())
    assert stats["active"] == 0
    assert stats["waiters"] == 0
    assert stats["acquire_total"] == 1


def test_slow_acquire_is_counted_and_warned(monkeypatch):
    reset_queue_stats()
    recorder = _Recorder()
    monkeypatch.setattr(base_client, "logger", recorder)
    monkeypatch.setattr(base_client, "QUEUE_WAIT_WARN_SECONDS", 0.0)

    async def scenario():
        sem = asyncio.Semaphore(2)
        async with _acquire_slot(sem, 2, "/chat/completions"):
            async with _acquire_slot(sem, 2, "/chat/completions"):
                pass
        return get_queue_stats()

    stats = asyncio.run(scenario())
    assert stats["acquire_total"] == 2
    assert stats["slow_acquires"] >= 1
    assert any("AI queue wait" in line and "queue_wait=" in line for line in recorder.warnings)


def test_stats_failure_releases_slot_and_permit(monkeypatch):
    reset_queue_stats()
    monkeypatch.setattr(base_client, "logger", _Recorder())
    sem = asyncio.Semaphore(1)

    def boom(endpoint, waited):
        raise RuntimeError("stats path failed")

    monkeypatch.setattr(base_client, "_record_queue_wait", boom)

    async def scenario():
        try:
            async with _acquire_slot(sem, 1, "/chat/completions"):
                pass
        except RuntimeError:
            pass
        else:
            raise AssertionError("_record_queue_wait 异常必须传播")
        reacquired = False
        try:
            await asyncio.wait_for(sem.acquire(), timeout=0.25)
            reacquired = True
            sem.release()
        except asyncio.TimeoutError:
            pass
        return get_queue_stats(), reacquired

    stats, reacquired = asyncio.run(scenario())
    assert stats["active"] == 0     # 计数路径抛错也不许泄漏 active
    assert stats["waiters"] == 0
    assert reacquired is True       # 许可必须已释放，后续请求能立刻拿到


def test_cancelled_waiter_releases_waiters_counter(monkeypatch):
    reset_queue_stats()
    monkeypatch.setattr(base_client, "logger", _Recorder())

    async def scenario():
        sem = asyncio.Semaphore(1)
        async with _acquire_slot(sem, 1, "/chat/completions"):
            task = asyncio.ensure_future(_contend(sem))
            await asyncio.sleep(0.01)
            waiting = int(get_queue_stats()["waiters"])
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
        return waiting, get_queue_stats()

    waiting, after = asyncio.run(scenario())
    assert waiting == 1                 # 取消前确实有等待者
    assert after["waiters"] == 0        # 取消路径不把计数减回去，归因就永远少一个
    assert after["active"] == 0
