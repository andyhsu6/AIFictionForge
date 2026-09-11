"""wrap_stream_with_heartbeat 心跳不取消底层迭代器的回归测试（W4 / B6a）。

Bug 背景：旧实现用 asyncio.wait_for(ait.__anext__(), timeout=...) 等待下一个
数据块，超时会 cancel 挂起中的 __anext__ 协程：CancelledError 被注入底层
async generator 帧并终结它，导致心跳之后真实数据全部丢失、流提前终止，
provider 异常也被静默吞掉。

修复语义（必须保持）：
- 快速流（从不超时）：item 原样透传，无 HEARTBEAT。
- 超时时至少产生一个 HEARTBEAT 哨兵，且流最终正常终止。
- 超时后继续消费同一个底层迭代器，item 不丢失、顺序不变。
- provider 真实异常向上传播。
- 消费者提前 aclose 时清理 pending task，不泄漏运行中的 task。
"""
import asyncio

import pytest

from app.utils.sse_response import HEARTBEAT, wrap_stream_with_heartbeat

pytestmark = pytest.mark.anyio

# 短心跳间隔 + 宽松 sleep 余量，避免 timing 抖动导致 flaky（总运行时 < 2s）
_INTERVAL = 0.05
_LONG_PAUSE = 0.25


async def _collect(wrapped):
    return [item async for item in wrapped]


# ---------------------------------------------------------------------------
# 基线特征测试：钉住当前实现已具备、修复后必须保留的行为（旧代码上也通过）
# ---------------------------------------------------------------------------

async def test_fast_stream_passes_items_through_unchanged():
    """Given 快速产出（永不超时）的流 / When 包装消费 / Then 原样透传、无心跳。"""
    async def fast_gen():
        yield "a"
        yield "b"
        yield "c"

    result = await _collect(wrap_stream_with_heartbeat(fast_gen(), heartbeat_interval=_INTERVAL))
    assert result == ["a", "b", "c"]


async def test_timeout_emits_heartbeat_and_terminates():
    """Given 中途长时间停顿的流 / When 包装消费 / Then 至少一个心跳且正常终止。"""
    async def pausing_gen():
        yield "A"
        await asyncio.sleep(_LONG_PAUSE)
        # 停顿后自然结束（无后续 item）

    result = await _collect(wrap_stream_with_heartbeat(pausing_gen(), heartbeat_interval=_INTERVAL))
    assert result[0] == "A"
    assert any(item is HEARTBEAT for item in result)


async def test_immediate_exception_propagates():
    """Given 立即抛错的流 / When 包装消费 / Then 异常向上传播（旧行为已具备）。"""
    async def raising_gen():
        raise ValueError("boom")
        yield  # pragma: no cover

    with pytest.raises(ValueError, match="boom"):
        await _collect(wrap_stream_with_heartbeat(raising_gen(), heartbeat_interval=_INTERVAL))


# ---------------------------------------------------------------------------
# 回归测试：复现「心跳超时 cancel 底层 __anext__ 丢数据」bug 类（修复前必须失败）
# ---------------------------------------------------------------------------

async def test_heartbeat_does_not_lose_items_after_slow_first_chunk():
    """Given 首个 item 前停顿远超心跳间隔的流 / When 消费 / Then 心跳后 item 不丢失且顺序保持。

    旧实现：首个超时 cancel 掉 __anext__，底层 generator 被终结，
    item1/item2 永远不会出现（输出只剩心跳）→ 本测试失败。
    """
    async def slow_start_gen():
        await asyncio.sleep(_LONG_PAUSE)
        yield "item1"
        yield "item2"

    result = await _collect(wrap_stream_with_heartbeat(slow_start_gen(), heartbeat_interval=_INTERVAL))

    items = [item for item in result if item is not HEARTBEAT]
    assert items == ["item1", "item2"]
    # 第一个 item 之前必须已经发过至少一个心跳
    first_item_idx = result.index("item1")
    assert any(item is HEARTBEAT for item in result[:first_item_idx])
    # 顺序：item1 在 item2 之前，且流终止（走到这里即终止）
    assert result.index("item1") < result.index("item2")


async def test_heartbeat_does_not_swallow_exception_after_pause():
    """Given 停顿后才抛错的流 / When 消费 / Then provider 异常仍向上传播。

    旧实现：超时会 cancel 挂起的 sleep，异常行永远执行不到，流以心跳静默收尾。
    """
    async def delayed_raise_gen():
        await asyncio.sleep(_LONG_PAUSE)
        raise ValueError("provider down")
        yield  # pragma: no cover

    with pytest.raises(ValueError, match="provider down"):
        await _collect(wrap_stream_with_heartbeat(delayed_raise_gen(), heartbeat_interval=_INTERVAL))


async def test_consumer_close_cancels_pending_task_without_leak():
    """Given 消费者中途 aclose 且底层 __anext__ 挂起 / When 关闭 / Then 不遗留运行中的 task。"""
    async def slow_second_gen():
        yield "A"
        await asyncio.sleep(_LONG_PAUSE)
        yield "B"  # pragma: no cover

    # 快照测试开始前已存在的 task（如 anyio runner 内部 task），只对新产生的 task 计泄漏
    base_tasks = set(asyncio.all_tasks())
    wrapped = wrap_stream_with_heartbeat(slow_second_gen(), heartbeat_interval=_INTERVAL)
    assert await wrapped.__anext__() == "A"
    # 进入等待第二个 item 的状态，先拿到一个心跳确认 pending 中
    assert await wrapped.__anext__() is HEARTBEAT

    await wrapped.aclose()
    # 给事件循环一次调度机会，任何未被取消/等待的 task 都会在这里现形
    await asyncio.sleep(0)
    leaked = set(asyncio.all_tasks()) - base_tasks
    assert leaked == set()
