"""文笔画像一致性自检 API（issue #254 P2）。

只做本地统计比较：把传入文本与项目默认画像的 metrics 目标对比，超阈值返回告警；
不调用模型、不写数据库、不回写画像。画像读取与比较统一走
``build_style_check_for_project``，与生成流程共用同一份结果结构。
"""
from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..core.errors import ApiError
from ..database import get_db
from ..models.project import Project
from ..services.style_consistency import build_style_check_for_project
from .writing_styles import get_current_user_id

router = APIRouter(prefix="/style-consistency", tags=["style-consistency"])

# 单次自检文本上限（字符）：超出直接拒绝，避免无谓的大文本统计
MAX_TEXT_CHARS = 200_000


class StyleConsistencyRequest(BaseModel):
    """一致性自检请求"""

    project_id: str
    text: str


@router.post("/check")
async def check_style_consistency(
    payload: StyleConsistencyRequest,
    request: Request,
    db: AsyncSession = Depends(get_db),
) -> dict:
    """对比文本与项目默认画像，返回指标、告警与一致性得分。

    项目无画像时返回 ``{"available": false}``（200），不视为错误。
    响应含 ``metrics``/``alerts``/``score``/``ok``/``compared_metrics``，
    以及增量的 ``plot_baseline`` 剧情分析对照字段。
    """
    user_id = get_current_user_id(request)

    project = (
        await db.execute(
            select(Project).where(
                Project.id == payload.project_id,
                Project.user_id == user_id,
            )
        )
    ).scalar_one_or_none()
    if not project:
        raise ApiError(code="not_found.project_or_forbidden")

    if len(payload.text) > MAX_TEXT_CHARS:
        raise ApiError(
            code="validation.error",
            detail=f"文本超过 {MAX_TEXT_CHARS} 字符上限",
            status=422,
            params={"max_chars": MAX_TEXT_CHARS},
        )

    return await build_style_check_for_project(db, project, payload.text)
