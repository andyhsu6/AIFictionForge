"""文笔画像一致性自检 API（issue #254 P2）。

只做本地统计比较：把传入文本与项目默认画像的 metrics 目标对比，超阈值返回告警；
不调用模型、不写数据库、不回写画像。
"""
import json

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..core.errors import ApiError
from ..database import get_db
from ..models.project import Project
from ..models.project_default_style import ProjectDefaultStyle
from ..models.writing_style import WritingStyle
from ..services.style_consistency import check_text_against_profile
from ..services.style_profile import is_profile_preset_id, profile_preset_id
from .writing_styles import get_current_user_id

router = APIRouter(prefix="/style-consistency", tags=["style-consistency"])

# 单次自检文本上限（字符）：超出直接拒绝，避免无谓的大文本统计
MAX_TEXT_CHARS = 200_000


class StyleConsistencyRequest(BaseModel):
    """一致性自检请求"""

    project_id: str
    text: str


async def _load_project_profile(
    db: AsyncSession, project_id: str
) -> dict | None:
    """读取项目默认风格的画像 JSON；无画像/非画像风格/JSON 损坏返回 None。"""
    style_id = (
        await db.execute(
            select(ProjectDefaultStyle.style_id).where(
                ProjectDefaultStyle.project_id == project_id
            )
        )
    ).scalar_one_or_none()
    if style_id is None:
        return None

    style = (
        await db.execute(select(WritingStyle).where(WritingStyle.id == style_id))
    ).scalar_one_or_none()
    if style is None or not style.profile_json:
        return None
    # 画像只在所属项目上下文生效（issue #254）：必须是本项目画像行
    if not is_profile_preset_id(style.preset_id):
        return None
    if style.preset_id != profile_preset_id(project_id):
        return None

    try:
        profile = json.loads(style.profile_json)
    except (TypeError, ValueError):
        return None
    return profile if isinstance(profile, dict) else None


@router.post("/check")
async def check_style_consistency(
    payload: StyleConsistencyRequest,
    request: Request,
    db: AsyncSession = Depends(get_db),
) -> dict:
    """对比文本与项目默认画像，返回指标、告警与一致性得分。

    项目无画像时返回 ``{"available": false}``（200），不视为错误。
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

    profile = await _load_project_profile(db, project.id)
    if profile is None:
        return {"available": False}

    return {"available": True, **check_text_against_profile(payload.text, profile)}
