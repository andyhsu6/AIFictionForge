"""后台任务API - 查询状态、取消任务"""
from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from typing import Optional

from app.database import get_db
from app.models.background_task import BackgroundTask
from app.models.batch_generation_task import BatchGenerationTask
from app.services.background_task_service import background_task_service
from app.logger import get_logger

router = APIRouter(prefix="/tasks", tags=["后台任务"])
logger = get_logger(__name__)


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
    if not task:
        raise HTTPException(status_code=404, detail="任务不存在")

    return {
        "id": task.id,
        "task_type": task.task_type,
        "project_id": task.project_id,
        "status": task.status,
        "progress": task.progress,
        "status_message": task.status_message,
        "progress_details": task.progress_details,
        "error_message": task.error_message,
        "task_result": task.task_result,
        "retry_count": task.retry_count,
        "cancel_requested": task.cancel_requested,
        "created_at": task.created_at.isoformat() if task.created_at else None,
        "started_at": task.started_at.isoformat() if task.started_at else None,
        "completed_at": task.completed_at.isoformat() if task.completed_at else None,
        "updated_at": task.updated_at.isoformat() if task.updated_at else None,
    }


@router.get("", summary="获取任务列表")
async def get_tasks(
    project_id: str,
    request: Request,
    task_type: Optional[str] = None,
    limit: int = 20,
    db: AsyncSession = Depends(get_db)
):
    """获取项目的后台任务列表（合并 BackgroundTask 和 BatchGenerationTask）"""
    user_id = getattr(request.state, 'user_id', None)
    if not user_id:
        raise HTTPException(status_code=401, detail="未登录")

    # 查询 BackgroundTask
    bg_tasks = await background_task_service.get_project_tasks(
        project_id, user_id, db, task_type=task_type, limit=limit
    )

    items = [
        {
            "id": t.id,
            "task_type": t.task_type,
            "status": t.status,
            "progress": t.progress,
            "status_message": t.status_message,
            "progress_details": t.progress_details,
            "error_message": t.error_message,
            "created_at": t.created_at.isoformat() if t.created_at else None,
            "completed_at": t.completed_at.isoformat() if t.completed_at else None,
        }
        for t in bg_tasks
    ]

    # 查询 BatchGenerationTask（不按 task_type 过滤，或过滤 chapter_batch 时才查）
    if not task_type or task_type == 'chapter_batch':
        batch_result = await db.execute(
            select(BatchGenerationTask)
            .where(
                BatchGenerationTask.project_id == project_id,
                BatchGenerationTask.user_id == user_id
            )
            .order_by(BatchGenerationTask.created_at.desc())
            .limit(limit)
        )
        batch_tasks = batch_result.scalars().all()

        for bt in batch_tasks:
            progress = bt.total_chapters * 100 if bt.total_chapters > 0 else 0
            if bt.total_chapters > 0 and bt.status in ('pending', 'running'):
                progress = int((bt.completed_chapters / bt.total_chapters) * 100)
            elif bt.status == 'completed':
                progress = 100

            status_message = None
            if bt.status == 'running' and bt.current_chapter_number:
                status_message = f"正在生成第 {bt.current_chapter_number} 章 ({bt.completed_chapters}/{bt.total_chapters})"
            elif bt.status == 'completed':
                status_message = f"已完成 {bt.completed_chapters} 章"
            elif bt.status == 'pending':
                status_message = f"等待中，共 {bt.total_chapters} 章"

            items.append({
                "id": bt.id,
                "task_type": "chapter_batch",
                "status": bt.status,
                "progress": progress,
                "status_message": status_message,
                "progress_details": None,
                "error_message": bt.error_message,
                "created_at": bt.created_at.isoformat() if bt.created_at else None,
                "completed_at": bt.completed_at.isoformat() if bt.completed_at else None,
            })

    # 按创建时间降序排序
    items.sort(key=lambda x: x.get("created_at") or "", reverse=True)

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
    if not success:
        raise HTTPException(status_code=400, detail="无法取消任务（不存在或已完成）")

    return {"message": "任务已取消", "task_id": task_id}


@router.delete("/{task_id}", summary="删除任务记录")
async def delete_task(
    task_id: str,
    request: Request,
    db: AsyncSession = Depends(get_db)
):
    """删除已完成/失败的任务记录（支持 BackgroundTask 和 BatchGenerationTask）"""
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

    await db.commit()

    total = (bg_result.rowcount or 0) + (batch_result.rowcount or 0)
    return {"message": f"已清理 {total} 条任务记录", "deleted_count": total}