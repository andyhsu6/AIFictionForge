"""数据库连接和会话管理 - SQLite 单库文件，按 user_id 隔离数据"""
import asyncio
from typing import Dict, Any
from datetime import datetime
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession, async_sessionmaker
from sqlalchemy.orm import declarative_base
from fastapi import Request, HTTPException
from app.config import settings
from app.logger import get_logger
from app.core.errors import ApiError

logger = get_logger(__name__)

# 创建基类
Base = declarative_base()

# 导入所有模型，确保 Base.metadata 能够发现它们
# 这必须在 Base 创建之后、init_db 之前导入
from app.models import (
    Project, Outline, Character, Chapter, GenerationHistory,
    Settings, WritingStyle, ProjectDefaultStyle,
    RelationshipType, CharacterRelationship, RelationshipTypeLink, Organization, OrganizationMember,
    StoryMemory, PlotAnalysis, AnalysisTask, BatchGenerationTask,
    RegenerationTask, Career, CharacterCareer, User, MCPPlugin, PromptTemplate,
    BackgroundTask, Announcement,
    AgentConversation, AgentMessage, AgentToolCall, AgentExecutionStep,
)

# 引擎缓存：每个用户一个引擎
_engine_cache: Dict[str, Any] = {}

# 锁管理：用于保护引擎创建过程
_engine_locks: Dict[str, asyncio.Lock] = {}
_cache_lock = asyncio.Lock()

# 会话统计（用于监控连接泄漏）
_session_stats = {
    "created": 0,
    "closed": 0,
    "active": 0,
    "errors": 0,
    "generator_exits": 0,
    "last_check": None
}


async def get_engine(user_id: str):
    """获取或创建用户专属的数据库引擎（线程安全）

    SQLite: 所有用户共享同一个库文件（进程内同一引擎），通过 user_id 字段隔离数据

    Args:
        user_id: 用户ID

    Returns:
        用户专属的异步引擎
    """
    cache_key = "shared_sqlite"
    if cache_key in _engine_cache:
        return _engine_cache[cache_key]

    async with _cache_lock:
        if cache_key not in _engine_cache:
            # 本项目仅支持 SQLite：静默按其他方言连会以更难懂的方式失败，故显式拦截
            if "sqlite" not in settings.database_url.lower():
                raise RuntimeError(
                    "仅支持 SQLite，请检查 DATABASE_URL"
                    f"（当前值不是 sqlite 方言：{settings.database_url.split(':')[0]}）"
                )

            engine_args = {
                "echo": settings.database_echo_pool,
                "echo_pool": settings.database_echo_pool,
                "future": True,
                "connect_args": {
                    "check_same_thread": False,
                    "timeout": 30.0,  # 等待锁释放的超时时间（秒）
                },
                # 启用连接前检测以支持更好的并发
                "pool_pre_ping": True,
            }

            logger.info("📊 使用 SQLite 数据库（超时30秒，WAL模式）")

            engine = create_async_engine(settings.database_url, **engine_args)
            _engine_cache[cache_key] = engine

            # 启用 WAL 模式以支持读写并发
            try:
                from sqlalchemy import event

                @event.listens_for(engine.sync_engine, "connect")
                def set_sqlite_pragma(dbapi_conn, connection_record):
                    cursor = dbapi_conn.cursor()
                    cursor.execute("PRAGMA journal_mode=WAL")
                    cursor.execute("PRAGMA synchronous=NORMAL")
                    cursor.execute("PRAGMA cache_size=-64000")  # 64MB 缓存
                    cursor.execute("PRAGMA busy_timeout=30000")  # 30秒超时
                    cursor.close()

                logger.info("✅ SQLite WAL 模式已启用（支持读写并发）")
            except Exception as e:
                logger.warning(f"⚠️ 启用 WAL 模式失败: {e}，使用默认配置")

        return _engine_cache[cache_key]


async def get_db(request: Request):
    """获取数据库会话的依赖函数
    
    从 request.state.user_id 获取用户ID，然后返回该用户的数据库会话
    """
    user_id = getattr(request.state, "user_id", None)
    
    if not user_id:
        raise ApiError(code="auth.identity_missing")
    
    engine = await get_engine(user_id)
    
    AsyncSessionLocal = async_sessionmaker(
        engine,
        class_=AsyncSession,
        expire_on_commit=False
    )
    
    session = AsyncSessionLocal()
    session_id = id(session)
    
    global _session_stats
    _session_stats["created"] += 1
    _session_stats["active"] += 1
    
    # logger.debug(f"📊 会话创建 [User:{user_id}][ID:{session_id}] - 活跃:{_session_stats['active']}, 总创建:{_session_stats['created']}, 总关闭:{_session_stats['closed']}")
    
    try:
        yield session
        if session.in_transaction():
            await session.rollback()
    except GeneratorExit:
        _session_stats["generator_exits"] += 1
        # logger.warning(f"⚠️ GeneratorExit [User:{user_id}][ID:{session_id}] - SSE连接断开（总计:{_session_stats['generator_exits']}次）")
        try:
            if session.in_transaction():
                await session.rollback()
                logger.info(f"✅ 事务已回滚 [User:{user_id}][ID:{session_id}]（GeneratorExit）")
        except Exception as rollback_error:
            _session_stats["errors"] += 1
            logger.error(f"❌ GeneratorExit回滚失败 [User:{user_id}][ID:{session_id}]: {str(rollback_error)}")
    except Exception as e:
        _session_stats["errors"] += 1
        logger.error(f"❌ 会话异常 [User:{user_id}][ID:{session_id}]: {str(e)}")
        try:
            if session.in_transaction():
                await session.rollback()
                logger.info(f"✅ 事务已回滚 [User:{user_id}][ID:{session_id}]（异常）")
        except Exception as rollback_error:
            logger.error(f"❌ 异常回滚失败 [User:{user_id}][ID:{session_id}]: {str(rollback_error)}")
        raise
    finally:
        try:
            if session.in_transaction():
                await session.rollback()
                logger.warning(f"⚠️ finally中发现未提交事务 [User:{user_id}][ID:{session_id}]，已回滚")
            
            await session.close()
            
            _session_stats["closed"] += 1
            _session_stats["active"] -= 1
            _session_stats["last_check"] = datetime.now().isoformat()
            
            # logger.debug(f"📊 会话关闭 [User:{user_id}][ID:{session_id}] - 活跃:{_session_stats['active']}, 总创建:{_session_stats['created']}, 总关闭:{_session_stats['closed']}, 错误:{_session_stats['errors']}")
            
            # 使用优化后的会话监控阈值
            if _session_stats["active"] > settings.database_session_leak_threshold:
                logger.error(f"🚨 严重告警：活跃会话数 {_session_stats['active']} 超过泄漏阈值 {settings.database_session_leak_threshold}！")
            elif _session_stats["active"] > settings.database_session_max_active:
                logger.warning(f"⚠️ 警告：活跃会话数 {_session_stats['active']} 超过警告阈值 {settings.database_session_max_active}，可能存在连接泄漏！")
            elif _session_stats["active"] < 0:
                logger.error(f"🚨 活跃会话数异常: {_session_stats['active']}，统计可能不准确！")
                
        except Exception as e:
            _session_stats["errors"] += 1
            logger.error(f"❌ 关闭会话时出错 [User:{user_id}][ID:{session_id}]: {str(e)}", exc_info=True)
            try:
                await session.close()
            except Exception:
                pass

async def init_db(user_id: str = None):
    """
    初始化数据库（已弃用）
    
    ⚠️ 此函数已弃用，仅保留用于向后兼容
    
    新的最佳实践:
    - 表结构管理: 使用 'alembic upgrade head'
    - 用户配置: Settings 在首次访问时自动创建（延迟初始化）
    
    Args:
        user_id: 用户ID (已不再使用)
    """
    logger.warning(
        "⚠️ init_db() 已弃用且无实际作用！\n"
        "   - 表结构: 由 Alembic 管理\n"
        "   - 用户配置: Settings API 自动创建\n"
        "   建议移除此调用"
    )


async def close_db():
    """关闭所有数据库连接"""
    try:
        logger.info("正在关闭所有数据库连接...")
        for user_id, engine in _engine_cache.items():
            await engine.dispose()
            logger.info(f"用户 {user_id} 的数据库连接已关闭")
        _engine_cache.clear()
        logger.info("所有数据库连接已关闭")
    except Exception as e:
        logger.error(f"关闭数据库连接失败: {str(e)}", exc_info=True)
        raise

async def get_database_stats():
    """获取数据库连接和会话统计信息

    Returns:
        dict: 包含数据库统计信息的字典
    """
    from app.config import settings

    stats = {
        "session_stats": {
            "created": _session_stats["created"],
            "closed": _session_stats["closed"],
            "active": _session_stats["active"],
            "errors": _session_stats["errors"],
            "generator_exits": _session_stats["generator_exits"],
            "last_check": _session_stats["last_check"],
        },
        "engine_cache": {
            "total_engines": len(_engine_cache),
            "engine_keys": list(_engine_cache.keys()),
        },
        "config": {
            "database_type": "SQLite",
            "session_max_active_threshold": settings.database_session_max_active,
            "session_leak_threshold": settings.database_session_leak_threshold,
        },
        "health": {
            "status": "healthy",
            "warnings": [],
            "errors": [],
        }
    }

    # 健康检查
    if _session_stats["active"] > settings.database_session_leak_threshold:
        stats["health"]["status"] = "critical"
        stats["health"]["errors"].append(
            f"活跃会话数 {_session_stats['active']} 超过泄漏阈值 {settings.database_session_leak_threshold}"
        )
    elif _session_stats["active"] > settings.database_session_max_active:
        stats["health"]["status"] = "warning"
        stats["health"]["warnings"].append(
            f"活跃会话数 {_session_stats['active']} 超过警告阈值 {settings.database_session_max_active}"
        )

    if _session_stats["active"] < 0:
        stats["health"]["status"] = "error"
        stats["health"]["errors"].append(f"活跃会话数异常: {_session_stats['active']}")

    error_rate = (_session_stats["errors"] / max(_session_stats["created"], 1)) * 100
    if error_rate > 5:
        if stats["health"]["status"] == "healthy":
            stats["health"]["status"] = "warning"
        stats["health"]["warnings"].append(f"会话错误率过高: {error_rate:.2f}%")

    stats["health"]["error_rate"] = f"{error_rate:.2f}%"

    return stats


async def check_database_health(user_id: str = None) -> dict:
    """检查数据库连接健康状态

    Args:
        user_id: 可选的用户ID，如果提供则检查特定用户的数据库

    Returns:
        dict: 健康检查结果
    """
    result = {
        "healthy": True,
        "checks": {},
        "timestamp": datetime.now().isoformat()
    }

    try:
        # 检查引擎是否存在
        cache_key = "shared_sqlite"
        if user_id:
            engine = await get_engine(user_id)
        else:
            if cache_key not in _engine_cache:
                result["checks"]["engine"] = {"status": "not_initialized", "healthy": True}
                return result
            engine = _engine_cache[cache_key]

        # 测试数据库连接
        AsyncSessionLocal = async_sessionmaker(
            engine,
            class_=AsyncSession,
            expire_on_commit=False
        )

        async with AsyncSessionLocal() as session:
            # 执行简单查询测试连接
            await session.execute(text("SELECT 1"))
            result["checks"]["connection"] = {"status": "ok", "healthy": True}

    except Exception as e:
        result["healthy"] = False
        result["checks"]["error"] = {
            "status": "error",
            "message": str(e),
            "healthy": False
        }
        logger.error(f"数据库健康检查失败: {str(e)}", exc_info=True)

    return result


async def reset_session_stats():
    """重置会话统计信息（用于测试或维护）"""
    global _session_stats
    _session_stats = {
        "created": 0,
        "closed": 0,
        "active": 0,
        "errors": 0,
        "generator_exits": 0,
        "last_check": datetime.now().isoformat()
    }
    logger.info("✅ 会话统计信息已重置")
    return _session_stats
