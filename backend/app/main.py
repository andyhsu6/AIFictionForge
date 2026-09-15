"""FastAPI应用主入口"""
from fastapi import FastAPI, Request, Depends, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import JSONResponse, FileResponse
from contextlib import asynccontextmanager
from pathlib import Path
import sys

from app.config import settings as config_settings
from app.core.errors import ApiError, envelope, register_exception_handlers
from app.database import close_db, _session_stats
from app.logger import setup_logging, get_logger
from app.middleware import RequestIDMiddleware
from app.middleware.auth_middleware import AuthMiddleware
from app.mcp import mcp_client, register_status_sync

setup_logging(
    level=config_settings.log_level,
    log_to_file=config_settings.log_to_file,
    log_file_path=config_settings.log_file_path,
    max_bytes=config_settings.log_max_bytes,
    backup_count=config_settings.log_backup_count,
    message_max_chars=config_settings.log_message_max_chars,
)
logger = get_logger(__name__)


async def _run_plan_with_closing_ai_service(**kwargs):
    """注册给 dispatch 的 run_plan 包装：detached runner 收尾需要一个用户级 AIService，
    请求态实例传不进后台任务，所以调度时按 user_id 构建（失败则 None，收尾降级）。"""
    from app.services.agent_plan_dispatch import build_closing_ai_service
    from app.services.agent_plan_runner import run_plan

    kwargs["ai_service"] = await build_closing_ai_service(str(kwargs.get("user_id") or ""))
    return await run_plan(**kwargs)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """应用生命周期管理"""
    # 注册MCP状态同步服务
    register_status_sync()

    # 计划执行器注册（PR-2a 的 approve-plan 未注册时返回 501；revert 这一段即回滚）
    try:
        from app.api.project_agent import register_plan_runner

        register_plan_runner(_run_plan_with_closing_ai_service)
        logger.info("plan runner registered")
    except Exception as exc:  # noqa: BLE001 —— 注册失败不得挡住启动
        logger.warning(f"计划执行器注册失败（批准将返回 501）: {exc}")

    # 安全保障：确保后台任务表存在（兼容未执行Alembic迁移的旧部署）
    try:
        await _sweep_interrupted_tasks()
        logger.info("后台任务表检查完成")
    except Exception as e:
        logger.warning(f"后台任务表检查失败（不影响启动）: {e}")

    logger.info("应用启动完成")
    
    yield
    
    # 清理MCP插件
    await mcp_client.cleanup()
    
    # 清理HTTP客户端池
    from app.services.ai_service import cleanup_http_clients
    await cleanup_http_clients()
    
    # 关闭数据库连接
    await close_db()
    
    logger.info("应用已关闭")


async def _sweep_interrupted_tasks(engine=None) -> int:
    """启动期把上一进程遗留的运行中/待跑任务判失败。

    PR-4：agent_plan 额外写「可读中断说明」——它中断时收尾聚合永远不会产出，
    用户只看得到一句 "failed" 就无从判断该重新做什么。**刻意不自动重发**：
    analyze_chapter 会覆盖既有 PlotAnalysis/StoryMemory/伏笔，import 与 repair
    同样非幂等，自动重放等于拿用户数据赌博。

    返回被中断的 agent_plan 行数（供日志与测试断言）。
    """
    from app.database import get_engine
    from app.models.analysis_task import AnalysisTask
    from app.models.background_task import BackgroundTask
    from app.models.batch_generation_task import BatchGenerationTask
    from app.models.project_agent import _naive_utc_now
    from sqlalchemy import select as sql_select
    from sqlalchemy import text
    from sqlalchemy import update as sql_update

    if engine is None:
        engine = await get_engine("system")
    # naive UTC：三张表的 created_at 都是 server_default=func.now()(UTC)，收尾戳必须同基准。
    interrupted_at = _naive_utc_now()
    async with engine.begin() as conn:
        # 仅创建 background_tasks 表（如果不存在），不影响其他表
        await conn.run_sync(
            lambda sync_conn: BackgroundTask.__table__.create(sync_conn, checkfirst=True)
        )
        # 补齐 i18n 结构化状态列（无迁移框架，旧库 ALTER ADD COLUMN，存量行保持 NULL）
        existing_cols = {
            row[1] for row in await conn.execute(text("PRAGMA table_info(background_tasks)"))
        }
        for col, decl in (("status_code", "VARCHAR(100)"), ("status_params", "JSON")):
            if col not in existing_cols:
                await conn.execute(text(f"ALTER TABLE background_tasks ADD COLUMN {col} {decl}"))
                logger.info(f"background_tasks 表已补列: {col}")

        plan_rows = (
            await conn.execute(
                sql_select(
                    BackgroundTask.id,
                    BackgroundTask.progress_details,
                ).where(
                    BackgroundTask.task_type == "agent_plan",
                    BackgroundTask.status.in_(["pending", "running"]),
                )
            )
        ).all()
        from app.services.agent_plan_runner import STATUS_MESSAGE_MAX_CHARS

        for row in plan_rows:
            details = dict(row.progress_details or {})
            steps = details.get("step_results") or []
            steps_done = int(details.get("steps_done") or len(steps))
            steps_total = int(details.get("steps_total") or len(steps))
            note = f"服务重启，计划执行已中断（已完成 {steps_done}/{steps_total} 步），结果未定稿，请重新发起"
            await conn.execute(
                sql_update(BackgroundTask)
                .where(BackgroundTask.id == row.id)
                .values(
                    status="failed",
                    error_message="服务重启，计划执行已中断",
                    status_message=note[:STATUS_MESSAGE_MAX_CHARS],
                    status_code="progress.agent_plan_interrupted",
                    status_params={"steps_done": steps_done, "steps_total": steps_total},
                    # 只追加 interrupted 块；写方（PR-2b）的 stage/message/step_results 原样保留
                    progress_details={
                        **details,
                        "interrupted": {
                            "reason": "服务重启",
                            "stage": "interrupted_by_restart",
                            "steps_done": steps_done,
                            "steps_total": steps_total,
                            "auto_retry": False,
                            "at": interrupted_at.isoformat(timespec="seconds"),
                        },
                    },
                    completed_at=interrupted_at,
                    updated_at=interrupted_at,
                )
            )
        if plan_rows:
            logger.info(f"启动期中断计划任务: {len(plan_rows)} 个 agent_plan 已标记为 failed 并写入中断说明")

        # 以下三条 UPDATE 为既有行为，逐字保留（计划行此时已是 failed，不会再命中）
        await conn.execute(
            sql_update(BackgroundTask)
            .where(BackgroundTask.status.in_(["pending", "running"]))
            .values(
                status="failed",
                error_message="服务重启，后台任务已中断",
                status_message="服务重启，任务已中断，请重新发起",
                completed_at=interrupted_at,
                updated_at=interrupted_at,
            )
        )
        await conn.execute(
            sql_update(BatchGenerationTask)
            .where(BatchGenerationTask.status.in_(["pending", "running"]))
            .values(
                status="failed",
                error_message="服务重启，批量生成任务已中断",
                completed_at=interrupted_at,
            )
        )
        await conn.execute(
            sql_update(AnalysisTask)
            .where(AnalysisTask.status.in_(["pending", "running"]))
            .values(
                status="failed",
                error_message="服务重启，章节分析任务已中断",
                progress=0,
                completed_at=interrupted_at,
            )
        )
    return len(plan_rows)


app = FastAPI(
    title=config_settings.app_name,
    version=config_settings.app_version,
    description="AI写小说工具 - 智能小说创作助手",
    lifespan=lifespan
)

register_exception_handlers(app)

app.add_middleware(RequestIDMiddleware)
app.add_middleware(AuthMiddleware)

if config_settings.debug:
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )
else:
    app.add_middleware(
        CORSMiddleware,
        allow_origins=config_settings.cors_origins,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )


def _git_info() -> dict:
    """服务代码身份（分支+commit），供验收前核对端口上跑的是哪个 checkout。"""
    import subprocess
    try:
        root = Path(__file__).resolve().parents[1]
        branch = subprocess.run(["git", "-C", str(root), "rev-parse", "--abbrev-ref", "HEAD"],
                                capture_output=True, text=True, timeout=5).stdout.strip()
        commit = subprocess.run(["git", "-C", str(root), "rev-parse", "--short", "HEAD"],
                                capture_output=True, text=True, timeout=5).stdout.strip()
        return {"branch": branch or "unknown", "commit": commit or "unknown"}
    except Exception:
        return {"branch": "unknown", "commit": "unknown"}


GIT_INFO = _git_info()


async def _count_running_plans(engine=None) -> int:
    """当前在跑的 agent_plan 行数（PR-4：/health 用，全局口径，只返回整数）。

    只数 running：pending 是"已批准还没开跑"，属 PR-2c 并发护栏的窗口期语义，
    混进来会让"有没有计划在跑"这个运维问题答错。
    """
    from app.database import get_engine
    from app.models.background_task import BackgroundTask
    from sqlalchemy import func, select

    if engine is None:
        engine = await get_engine("system")
    async with engine.connect() as conn:
        return int(
            (
                await conn.execute(
                    select(func.count())
                    .select_from(BackgroundTask)
                    .where(BackgroundTask.task_type == "agent_plan", BackgroundTask.status == "running")
                )
            ).scalar_one()
        )


def _health_engine():
    """测试注入点：返回 None 表示按生产路径自取 system 引擎。"""
    return None


@app.get("/health")
async def health_check():
    """健康检查"""
    try:
        plans_running = await _count_running_plans(engine=_health_engine())
    except Exception as e:  # 计数失败绝不能把健康检查带崩（aistoryforge.sh 依赖本端点）
        logger.warning(f"/health 统计运行中计划失败（忽略）: {e}")
        plans_running = None
    return {
        "status": "ok",
        "branch": GIT_INFO["branch"],
        "commit": GIT_INFO["commit"],
        "plans_running": plans_running,
    }


@app.get("/health/db-sessions")
async def db_session_stats(request: Request):
    """
    数据库会话统计（监控连接泄漏）
    
    返回：
    - created: 总创建会话数
    - closed: 总关闭会话数
    - active: 当前活跃会话数（应该接近0）
    - errors: 错误次数
    - generator_exits: SSE断开次数
    - last_check: 最后检查时间
    """
    if not getattr(request.state, "is_admin", False):
        raise ApiError(code="auth.admin_required")
    return {
        "status": "ok",
        "session_stats": _session_stats,
        "warning": "活跃会话数过多" if _session_stats["active"] > 10 else None
    }


from app.api import (
    projects, outlines, outline_transfer, characters, chapters,
    wizard_stream, relationships, organizations,
    auth, users, settings, writing_styles, memories,
    mcp_plugins, admin, inspiration, prompt_templates,
    changelog, careers, foreshadows, book_import,
    project_covers, project_agent, tasks, skills
)

app.include_router(auth.router, prefix="/api")
app.include_router(users.router, prefix="/api")
app.include_router(settings.router, prefix="/api")
app.include_router(admin.router, prefix="/api")

app.include_router(projects.router, prefix="/api")
app.include_router(project_covers.router, prefix="/api")
app.include_router(project_agent.router, prefix="/api")
app.include_router(wizard_stream.router, prefix="/api")
app.include_router(inspiration.router, prefix="/api")
app.include_router(outlines.router, prefix="/api")
app.include_router(outline_transfer.router, prefix="/api")
app.include_router(characters.router, prefix="/api")
app.include_router(careers.router, prefix="/api")  # 职业管理API
app.include_router(chapters.router, prefix="/api")
app.include_router(relationships.router, prefix="/api")
app.include_router(organizations.router, prefix="/api")
app.include_router(writing_styles.router, prefix="/api")
app.include_router(memories.router)  # 记忆管理API (已包含/api前缀)
app.include_router(foreshadows.router)  # 伏笔管理API (已包含/api前缀)
app.include_router(mcp_plugins.router, prefix="/api")  # MCP插件管理API
app.include_router(prompt_templates.router, prefix="/api")  # 提示词模板管理API
app.include_router(changelog.router, prefix="/api")  # 更新日志API
app.include_router(skills.router)  # Skill API（已包含/api前缀）
app.include_router(book_import.router, prefix="/api")  # 拆书导入API
app.include_router(tasks.router, prefix="/api")  # 后台任务API

if getattr(sys, "frozen", False):
    static_dir = Path(sys._MEIPASS) / "backend" / "static"
    generated_assets_root_dir = Path(sys.executable).parent / "storage"
else:
    static_dir = Path(__file__).parent.parent / "static"
    generated_assets_root_dir = Path(__file__).parent.parent / "storage"
generated_covers_dir = generated_assets_root_dir / "generated_covers"
generated_covers_dir.mkdir(parents=True, exist_ok=True)
if static_dir.exists():
    app.mount("/assets", StaticFiles(directory=str(static_dir / "assets")), name="assets")
    app.mount("/generated-assets/covers", StaticFiles(directory=str(generated_covers_dir)), name="generated-covers")
    
    @app.get("/{full_path:path}")
    async def serve_spa(full_path: str):
        """服务单页应用，所有非API路径返回index.html"""
        if full_path.startswith("api/"):
            return JSONResponse(
                status_code=404,
                content=envelope("API路径不存在", "not_found.api_route")
            )
        
        file_path = static_dir / full_path
        try:
            resolved_file = file_path.resolve()
            resolved_static = static_dir.resolve()
            resolved_file.relative_to(resolved_static)
        except ValueError:
            return JSONResponse(
                status_code=404,
                content=envelope("页面不存在", "not_found.frontend_route")
            )

        if resolved_file.is_file():
            return FileResponse(resolved_file)
        
        index_file = static_dir / "index.html"
        if index_file.exists():
            return FileResponse(index_file)
        
        return JSONResponse(
            status_code=404,
            content=envelope("页面不存在", "not_found.frontend_route")
        )
else:
    logger.warning("静态文件目录不存在，请先构建前端: cd frontend && npm run build")
    
    @app.get("/")
    async def root():
        return {
            "message": "欢迎使用AIFictionForge",
            "version": config_settings.app_version,
            "docs": "/docs",
            "notice": "请先构建前端: cd frontend && npm run build"
        }


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(
        "app.main:app",
        host=config_settings.app_host,
        port=config_settings.app_port,
        reload=config_settings.debug
    )
