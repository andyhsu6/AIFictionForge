"""per-user 数据库写锁：进程内「读整行 -> 改 -> commit」临界区的串行化机制。

原先定义在 `app.api.chapters`（章节后台写 SQLite 用）。移到中立的 core 模块，
是因为 `Settings.preferences` 这类「单行 JSON blob 读改写」的写入方集中在
`app.api.settings`，而 `app.api.chapters` 本身 import `app.api.settings`：
写锁留在章节模块里会让 settings 反向依赖 chapters 形成导入环。

约定：
- 一个 user_id 一把 `asyncio.Lock`，全仓库共用同一个注册表 `db_write_locks`
  （章节写入与 preferences 写入必须互斥，因此不允许出现第二套私有注册表）。
- 锁 **不可重入**：临界区内禁止再次获取同一用户的锁（会自锁死）。
- 临界区内必须完成 commit；且读取要绕过 ORM 身份映射（见
  `app.api.settings.get_user_settings(refresh=True)`），否则拿到的是进入
  临界区之前的陈旧快照，锁形同虚设。
- 语义是「单进程内串行」。多 worker/多进程部署下由 SQLite 自身的写锁与
  busy_timeout 兜底（与既有章节写锁同一前提）。

注册表生命周期契约（weakref 自驱逐，#62 二次清理）：
- `db_write_locks` 是 `weakref.WeakValueDictionary`：条目只在**仍有任务持有该
  `Lock` 的强引用**期间存活。最后一个强引用消失（临界区结束、调用方不再引用该锁）
  后，键由 weakref 回调自动移除 ⇒ 不再「每个 user_id 一把锁、进程生命周期内永不释放」。
- 由此得出**每个调用方必须遵守的契约**：取到锁后必须把它绑定到局部变量，并让该
  变量活到整段临界区结束。若调用方丢弃返回值、之后再调一次 `get_db_write_lock`，
  第二次会拿到**另一把**新锁，同一用户的两次写就会并发 ⇒ 互斥静默失效。
  现有调用方（`db_write_lock` 的 with 形态、`write_verdict`、`api.chapters` 的四个
  后台任务）都在整段临界区内持有返回值，符合该契约。
"""
from asyncio import Lock
from contextlib import asynccontextmanager
from typing import AsyncIterator
from weakref import WeakValueDictionary

# 全局数据库写入锁（每个用户一个锁，用于保护 SQLite 写入操作）。
# WeakValueDictionary：无强引用时条目自动驱逐（存活契约见模块 docstring）。
db_write_locks: "WeakValueDictionary[str, Lock]" = WeakValueDictionary()


async def get_db_write_lock(user_id: str) -> Lock:
    """获取或创建用户的数据库写入锁。

    返回值**必须**由调用方绑定到局部变量并持有到临界区结束；丢弃后重新获取会得到
    另一把新锁，互斥失效（见模块 docstring 的生命周期契约）。
    """
    lock = db_write_locks.get(user_id)
    if lock is None:
        lock = Lock()
        db_write_locks[user_id] = lock
    return lock


@asynccontextmanager
async def db_write_lock(user_id: str) -> AsyncIterator[Lock]:
    """写锁的 context manager 形态：`async with db_write_lock(user_id): ...`。

    临界区内完成「重新读取 -> 合并 -> commit」；退出时才释放，
    因此 commit 必须写在 with 块内部。`lock` 局部变量让该锁在整段临界区内保持强引用，
    不会被 weakref 注册表驱逐。
    """
    lock = await get_db_write_lock(user_id)
    async with lock:
        yield lock
