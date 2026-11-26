"""创作助手单回合运行登记表：服务端取消与「卡死回合」判定的唯一事实来源。

背景（issue #257）：旧实现只有前端 `AbortController.abort()` 掐断 SSE 连接，服务端
没有任何可见的「本回合还在跑」登记，于是：

- 浏览器断开没被传到 ASGI 层（代理/标签页后台节流）时，回合继续烧 token；
- 回合中途异常时收尾逻辑可能二次崩溃，`agent_execution_steps.status='running'`
  永久残留，界面一直转圈，用户无处可点；
- 无法区分「真有回合在跑」和「上一进程遗留的脏 running 行」。

本模块只做进程内登记（单进程部署，SQLite + 单 uvicorn），生命周期与一次
`stream_chat` 调用对齐：`begin_turn` → 取消接口 `request_cancel` → 回合收尾
`finish`。**不持久化**：服务重启后登记表为空，遗留 running 行由启动期
`_sweep_interrupted_tasks()` 与打开会话时的 `reconcile_stale_turns()` 兜底。
"""
from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field


@dataclass
class AgentTurnHandle:
    """一次仍在运行（或已停止、待数据库收尾落盘）的创作助手回合。"""

    conversation_id: str
    cancel_event: asyncio.Event = field(default_factory=asyncio.Event)
    finished_event: asyncio.Event = field(default_factory=asyncio.Event)
    started_at: float = field(default_factory=time.monotonic)
    #: 回合已停止运行、finalize 还没落盘的时间点（见 `finish_turn(settling=True)`）。
    settling_since: float | None = None

    @property
    def cancelled(self) -> bool:
        return self.cancel_event.is_set()

    def request_cancel(self) -> None:
        self.cancel_event.set()


# conversation_id -> 当前运行中的回合。同会话并发回合是异常态：新回合接管，
# 旧回合按「被新回合取代」停止（旧回合的收尾不会覆盖新回合的登记）。
_active_turns: dict[str, AgentTurnHandle] = {}
#: 已停止运行、但数据库收尾（finalize）尚未落盘的会话。这层状态存在的唯一原因：
#: finalize 可能排队等 SQLite 写锁（busy_timeout=30s），而 registry 注销发生在
#: 生成器退出时、早于 finalize 落盘。若这段时间里做卡死自愈，会把刚取消的回合
#: 误写成「服务重启/连接异常」的终止文案（且 finalize 之后会因已绑定 assistant
#: 消息而早退，错误文案永久留存）。
_settling_turns: dict[str, AgentTurnHandle] = {}


def begin_turn(conversation_id: str) -> AgentTurnHandle:
    """登记新回合；同会话已有回合时先请求其停止，再由新回合接管。"""
    old = _active_turns.get(conversation_id)
    if old is not None:
        old.request_cancel()
    # 新回合接管：上一回合的收尾状态不再有阻拦意义。
    _settling_turns.pop(conversation_id, None)
    handle = AgentTurnHandle(conversation_id=conversation_id)
    _active_turns[conversation_id] = handle
    return handle


def finish_turn(handle: AgentTurnHandle, *, settling: bool = False) -> None:
    """回合停止运行（幂等）。只清除仍指向自己的登记，避免误删接管者。

    `settling=True` 表示「异常/取消退出，数据库收尾马上要做」：把句柄移入
    `_settling_turns`，让卡死自愈在这段时间里不要动手（见模块顶部说明）。
    正常完成传 False——没有收尾要等，直接结算。

    刻意**不**无条件置 `finished_event`：取消接口等的是「数据库收尾也写完了」。
    """
    if _active_turns.get(handle.conversation_id) is handle:
        _active_turns.pop(handle.conversation_id, None)
    if _settling_turns.get(handle.conversation_id) is handle:
        _settling_turns.pop(handle.conversation_id, None)
    if settling:
        handle.settling_since = time.monotonic()
        _settling_turns[handle.conversation_id] = handle
    else:
        handle.finished_event.set()


def mark_settled(handle: AgentTurnHandle | None) -> None:
    """收尾（DB 状态已落盘）后才能叫醒等待者；幂等。"""
    if handle is None:
        return
    if _settling_turns.get(handle.conversation_id) is handle:
        _settling_turns.pop(handle.conversation_id, None)
    handle.settling_since = None
    handle.finished_event.set()


def is_settling(conversation_id: str, *, max_seconds: float = 60.0) -> bool:
    """该会话是否「已停止、数据库收尾未落盘」。

    `max_seconds` 是防呆上限：收尾任务若因进程级问题永远没跑完，不能让这个会话
    的卡死自愈被永久挡住（那时宁可让自愈写一次保守文案）。
    """
    handle = _settling_turns.get(conversation_id)
    if handle is None:
        return False
    if handle.settling_since is None:
        return True
    if time.monotonic() - handle.settling_since > max_seconds:
        _settling_turns.pop(conversation_id, None)
        return False
    return True


def is_active(conversation_id: str) -> bool:
    return conversation_id in _active_turns


def active_conversation_ids() -> set[str]:
    return set(_active_turns)


def request_cancel(conversation_id: str) -> AgentTurnHandle | None:
    """请求停止某会话的回合；没有运行中的回合时返回 None。"""
    handle = _active_turns.get(conversation_id)
    if handle is None:
        return None
    handle.request_cancel()
    return handle


def reset_for_tests() -> None:
    """测试隔离用：清空登记表（不触发 finished）。"""
    _active_turns.clear()
    _settling_turns.clear()
