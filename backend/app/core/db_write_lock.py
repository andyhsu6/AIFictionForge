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
"""
from asyncio import Lock
from contextlib import asynccontextmanager
from typing import AsyncIterator

# 全局数据库写入锁（每个用户一个锁，用于保护 SQLite 写入操作）
db_write_locks: dict[str, Lock] = {}


async def get_db_write_lock(user_id: str) -> Lock:
    """获取或创建用户的数据库写入锁。"""
    if user_id not in db_write_locks:
        db_write_locks[user_id] = Lock()
    return db_write_locks[user_id]


@asynccontextmanager
async def db_write_lock(user_id: str) -> AsyncIterator[Lock]:
    """写锁的 context manager 形态：`async with db_write_lock(user_id): ...`。

    临界区内完成「重新读取 -> 合并 -> commit」；退出时才释放，
    因此 commit 必须写在 with 块内部。
    """
    lock = await get_db_write_lock(user_id)
    async with lock:
        yield lock
