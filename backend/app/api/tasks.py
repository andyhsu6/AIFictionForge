"""后台任务API - 查询状态、取消任务"""
from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import case, select, update
from typing import Optional
from datetime import datetime

from app.database import get_db
from app.models.background_task import BackgroundTask
from app.models.batch_generation_task import BatchGenerationTask
from app.models.analysis_task import AnalysisTask
from app.models.chapter import Chapter
from app.services.background_task_service import background_task_service
from app.services.task_resources import affected_resources_for_task
from app.logger import get_logger

router = APIRouter(prefix="/tasks", tags=["后台任务"])
logger = get_logger(__name__)


def _background_task_data(task: BackgroundTask) -> dict:
    return {
        "id": task.id,
        "task_type": task.task_type,
        "project_id": task.project_id,
        "status": task.status,
        "progress": task.progress or 0,
        "status_message": task.status_message,
        "progress_details": task.progress_details,
        "error_message": task.error_message,
        "task_result": task.task_result,
        "retry_count": task.retry_count or 0,
        "cancel_requested": bool(task.cancel_requested),
        "affected_resources": affected_resources_for_task(task.task_type),
        "can_cancel": task.status in ("pending", "running"),
        "can_delete": task.status in ("completed", "failed", "cancelled"),
        "created_at": task.created_at.isoformat() if task.created_at else None,
        "started_at": task.started_at.isoformat() if task.started_at else None,
        "completed_at": task.completed_at.isoformat() if task.completed_at else None,
        "updated_at": task.updated_at.isoformat() if task.updated_at else None,
    }


def _batch_task_data(task: BatchGenerationTask) -> dict:
    progress = 0
    if task.total_chapters:
        progress = int((task.completed_chapters or 0) / task.total_chapters * 100)
    if task.status == "completed":
        progress = 100
    status_message = f"等待中，共 {task.total_chapters} 章"
    if task.status == "running" and task.current_chapter_number:
        status_message = f"正在生成第 {task.current_chapter_number} 章 ({task.completed_chapters}/{task.total_chapters})"
    elif task.status == "completed":
        status_message = f"已完成 {task.completed_chapters} 章"
    elif task.status == "failed":
        status_message = "批量章节生成失败"
    elif task.status == "cancelled":
        status_message = "批量章节生成已取消"
    return {
        "id": task.id,
        "task_type": "chapter_batch",
        "project_id": task.project_id,
        "status": task.status,
        "progress": progress,
        "status_message": status_message,
        "progress_details": {
            "completed": task.completed_chapters or 0,
            "total": task.total_chapters or 0,
            "current_chapter_number": task.current_chapter_number,
        },
        "error_message": task.error_message,
        "task_result": None,
        "retry_count": task.current_retry_count or 0,
        "cancel_requested": task.status == "cancelled",
        "affected_resources": affected_resources_for_task("chapter_batch"),
        "can_cancel": task.status in ("pending", "running"),
        "can_delete": task.status in ("completed", "failed", "cancelled"),
        "created_at": task.created_at.isoformat() if task.created_at else None,
        "started_at": task.started_at.isoformat() if task.started_at else None,
        "completed_at": task.completed_at.isoformat() if task.completed_at else None,
        "updated_at": task.completed_at.isoformat() if task.completed_at else (
            task.started_at.isoformat() if task.started_at else (
                task.created_at.isoformat() if task.created_at else None
            )
        ),
    }


def _analysis_task_data(
    task: AnalysisTask,
    chapter_number: int | None = None,
    chapter_title: str | None = None,
) -> dict:
    messages = {
        "pending": "章节分析等待中",
        "running": "正在分析章节",
        "completed": "章节分析已完成",
        "failed": "章节分析失败",
    }
    if chapter_number is not None and chapter_title:
        status_message = f"第{chapter_number}章《{chapter_title}》{messages.get(task.status, task.status)}"
    elif chapter_number is not None:
        status_message = f"第{chapter_number}章 {messages.get(task.status, task.status)}"
    else:
        status_message = messages.get(task.status, task.status)
    return {
        "id": task.id,
        "task_type": "chapter_analysis",
        "project_id": task.project_id,
        "status": task.status,
        "progress": task.progress or 0,
        "status_message": status_message,
        "progress_details": {
            "stage": "analysis",
            "chapter_id": task.chapter_id,
            "chapter_number": chapter_number,
            "chapter_title": chapter_title,
        },
        "error_message": task.error_message,
        "task_result": None,
        "retry_count": 0,
        "cancel_requested": False,
        "affected_resources": affected_resources_for_task("chapter_analysis"),
        "can_cancel": False,
        "can_delete": (
            task.status in ("completed", "failed", "cancelled")
            and task.archived_at is None
        ),
        "created_at": task.created_at.isoformat() if task.created_at else None,
        "started_at": task.started_at.isoformat() if task.started_at else None,
        "completed_at": task.completed_at.isoformat() if task.completed_at else None,
        "updated_at": task.completed_at.isoformat() if task.completed_at else (
            task.started_at.isoformat() if task.started_at else (
                task.created_at.isoformat() if task.created_at else None
            )
        ),
        "archived_at": task.archived_at.isoformat() if task.archived_at else None,
    }


@router.get("/{task_id}", summary="获取任务状态")
async def get_task_status(
    task_id: str,
    request: Request,
    db: AsyncSession = Depends(get_db)
):
    """获取后台任务的状态和进度"""
    user_id = getattr(request.state, 'user_id', None)
    if not user_id:
        raise HTTPException(status_code=401, detail="未登录")

    task = await background_task_service.get_task(task_id, user_id, db)
    if task:
        return _background_task_data(task)
    batch_task = (await db.execute(select(BatchGenerationTask).where(
        BatchGenerationTask.id == task_id,
        BatchGenerationTask.user_id == user_id,
    ))).scalar_one_or_none()
    if batch_task:
        return _batch_task_data(batch_task)
    analysis_task = (await db.execute(select(AnalysisTask).where(
        AnalysisTask.id == task_id,
        AnalysisTask.user_id == user_id,
    ))).scalar_one_or_none()
    if analysis_task:
        chapter = (await db.execute(select(Chapter).where(Chapter.id == analysis_task.chapter_id))).scalar_one_or_none()
        return _analysis_task_data(
            analysis_task,
            chapter.chapter_number if chapter else None,
            chapter.title if chapter else None,
        )
    raise HTTPException(status_code=404, detail="任务不存在")


@router.get("", summary="获取任务列表")
async def get_tasks(
    project_id: str,
    request: Request,
    task_type: Optional[str] = None,
    limit: int = 20,
    db: AsyncSession = Depends(get_db)
):
    """获取项目后台任务列表，统一合并普通、批量生成和章节分析任务。"""
    user_id = getattr(request.state, 'user_id', None)
    if not user_id:
        raise HTTPException(status_code=401, detail="未登录")

    # 查询 BackgroundTask
    bg_tasks = await background_task_service.get_project_tasks(
        project_id, user_id, db, task_type=task_type, limit=limit
    )

    items = [_background_task_data(task) for task in bg_tasks]

    # 查询 BatchGenerationTask（不按 task_type 过滤，或过滤 chapter_batch 时才查）
    if not task_type or task_type == 'chapter_batch':
        batch_result = await db.execute(
            select(BatchGenerationTask)
            .where(
                BatchGenerationTask.project_id == project_id,
                BatchGenerationTask.user_id == user_id
            )
            .order_by(
                case(
                    (BatchGenerationTask.status.in_(["pending", "running"]), 0),
                    else_=1,
                ),
                BatchGenerationTask.created_at.desc(),
            )
            .limit(limit)
        )
        batch_tasks = batch_result.scalars().all()

        items.extend(_batch_task_data(task) for task in batch_tasks)

    if not task_type or task_type == "chapter_analysis":
        analysis_result = await db.execute(
            select(AnalysisTask, Chapter.chapter_number, Chapter.title)
            .join(Chapter, Chapter.id == AnalysisTask.chapter_id)
            .where(
                AnalysisTask.project_id == project_id,
                AnalysisTask.user_id == user_id,
                AnalysisTask.archived_at.is_(None),
            )
            .order_by(
                case(
                    (AnalysisTask.status.in_(["pending", "running"]), 0),
                    else_=1,
                ),
                AnalysisTask.created_at.desc(),
            )
            .limit(limit)
        )
        items.extend(
            _analysis_task_data(task, chapter_number, chapter_title)
            for task, chapter_number, chapter_title in analysis_result.all()
        )

    # 按创建时间降序排序
    items.sort(
        key=lambda item: (
            item.get("status") in {"pending", "running"},
            item.get("created_at") or "",
        ),
        reverse=True,
    )

    return {"items": items[:limit]}


@router.post("/{task_id}/cancel", summary="取消任务")
async def cancel_task(
    task_id: str,
    request: Request,
    db: AsyncSession = Depends(get_db)
):
    """请求取消后台任务"""
    user_id = getattr(request.state, 'user_id', None)
    if not user_id:
        raise HTTPException(status_code=401, detail="未登录")

    success = await background_task_service.cancel_task(task_id, user_id, db)
    if success:
        return {"message": "任务已取消", "task_id": task_id}

    result = await db.execute(
        select(BatchGenerationTask).where(
            BatchGenerationTask.id == task_id,
            BatchGenerationTask.user_id == user_id
        )
    )
    batch_task = result.scalar_one_or_none()
    if batch_task and batch_task.status in ("pending", "running"):
        batch_task.status = "cancelled"
        batch_task.completed_at = datetime.now()
        await db.commit()
        return {"message": "任务已取消", "task_id": task_id}

    raise HTTPException(status_code=400, detail="无法取消任务（不存在或已完成）")


@router.delete("/{task_id}", summary="删除任务记录")
async def delete_task(
    task_id: str,
    request: Request,
    db: AsyncSession = Depends(get_db)
):
    """删除或归档已完成/失败的任务记录。分析任务采用归档，保留章节分析状态。"""
    user_id = getattr(request.state, 'user_id', None)
    if not user_id:
        raise HTTPException(status_code=401, detail="未登录")

    # 先尝试从 BackgroundTask 查找
    task = await background_task_service.get_task(task_id, user_id, db)
    if task:
        if task.status in ("pending", "running"):
            raise HTTPException(status_code=400, detail="无法删除进行中的任务，请先取消")
        await db.delete(task)
        await db.commit()
        return {"message": "任务记录已删除"}

    # 再尝试从 BatchGenerationTask 查找
    result = await db.execute(
        select(BatchGenerationTask).where(
            BatchGenerationTask.id == task_id,
            BatchGenerationTask.user_id == user_id
        )
    )
    batch_task = result.scalar_one_or_none()
    if batch_task:
        if batch_task.status in ("pending", "running"):
            raise HTTPException(status_code=400, detail="无法删除进行中的任务，请先取消")
        await db.delete(batch_task)
        await db.commit()
        return {"message": "任务记录已删除"}

    # 分析任务不能物理删除：章节分析状态接口依赖每章最新记录。
    analysis_task = (await db.execute(
        select(AnalysisTask).where(
            AnalysisTask.id == task_id,
            AnalysisTask.user_id == user_id,
        )
    )).scalar_one_or_none()
    if analysis_task:
        if analysis_task.status in ("pending", "running"):
            raise HTTPException(status_code=400, detail="无法归档进行中的分析任务")
        if analysis_task.archived_at is None:
            analysis_task.archived_at = datetime.now()
            await db.commit()
        return {"message": "分析任务已从任务面板归档"}

    raise HTTPException(status_code=404, detail="任务不存在")


@router.delete("/project/{project_id}/clear", summary="清理项目已结束的任务记录")
async def clear_project_tasks(
    project_id: str,
    request: Request,
    db: AsyncSession = Depends(get_db)
):
    """清理项目中已完成/失败/已取消的任务记录"""
    user_id = getattr(request.state, 'user_id', None)
    if not user_id:
        raise HTTPException(status_code=401, detail="未登录")

    from sqlalchemy import delete as sql_delete

    # 清理 BackgroundTask
    bg_result = await db.execute(
        sql_delete(BackgroundTask).where(
            BackgroundTask.project_id == project_id,
            BackgroundTask.user_id == user_id,
            BackgroundTask.status.in_(["completed", "failed", "cancelled"])
        )
    )

    # 清理 BatchGenerationTask
    batch_result = await db.execute(
        sql_delete(BatchGenerationTask).where(
            BatchGenerationTask.project_id == project_id,
            BatchGenerationTask.user_id == user_id,
            BatchGenerationTask.status.in_(["completed", "failed", "cancelled"])
        )
    )

    # 分析任务采用归档而非删除，避免章节状态被误判为未分析。
    analysis_result = await db.execute(
        update(AnalysisTask)
        .where(
            AnalysisTask.project_id == project_id,
            AnalysisTask.user_id == user_id,
            AnalysisTask.archived_at.is_(None),
            AnalysisTask.status.in_(["completed", "failed", "cancelled"]),
        )
        .values(archived_at=datetime.now())
    )

    await db.commit()

    total = (bg_result.rowcount or 0) + (batch_result.rowcount or 0) + (analysis_result.rowcount or 0)
    return {"message": f"已清理 {total} 条任务记录", "deleted_count": total}
