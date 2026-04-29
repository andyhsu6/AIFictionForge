"""后台任务API - 查询状态、取消任务"""
from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy.ext.asyncio import AsyncSession
from typing import Optional

from app.database import get_db
from app.models.background_task import BackgroundTask
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
    """获取项目的后台任务列表"""
    user_id = getattr(request.state, 'user_id', None)
    if not user_id:
        raise HTTPException(status_code=401, detail="未登录")

    tasks = await background_task_service.get_project_tasks(
        project_id, user_id, db, task_type=task_type, limit=limit
    )

    return {
        "items": [
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
            for t in tasks
        ]
    }


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
    """删除已完成/失败的任务记录"""
    user_id = getattr(request.state, 'user_id', None)
    if not user_id:
        raise HTTPException(status_code=401, detail="未登录")

    task = await background_task_service.get_task(task_id, user_id, db)
    if not task:
        raise HTTPException(status_code=404, detail="任务不存在")

    if task.status in ("pending", "running"):
        raise HTTPException(status_code=400, detail="无法删除进行中的任务，请先取消")

    await db.delete(task)
    await db.commit()
    return {"message": "任务记录已删除"}