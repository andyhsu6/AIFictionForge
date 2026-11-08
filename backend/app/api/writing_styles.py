"""写作风格管理 API"""
import json

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, func, delete
from typing import List

from ..core.errors import ApiError
from ..database import get_db
from ..models.chapter import Chapter
from ..models.writing_style import WritingStyle
from ..models.project import Project
from ..models.project_default_style import ProjectDefaultStyle
from ..schemas.book_import import BookImportChapter
from ..schemas.writing_style import (
    WritingStyleCreate,
    WritingStyleUpdate,
    WritingStyleResponse,
    WritingStyleListResponse,
    ProfileReextractRequest,
    ProfileReextractResponse,
    SetDefaultStyleRequest,
    VolumeProfileGenerateRequest,
    VolumeProfileGenerateResponse,
    VolumeProfileItem,
    VolumeProfileListResponse,
)
from ..logger import get_logger
from ..services.book_import_service import BookImportService
from ..services.style_anchors import create_project_style_anchors
from ..services.style_profile import (
    PROFILE_EDITED_STYLE_TYPE,
    is_profile_preset_id,
    profile_preset_id,
)
from ..services.style_volumes import (
    list_project_volumes,
    upsert_volume_profile,
    volume_preset_id,
)

router = APIRouter(prefix="/writing-styles", tags=["writing-styles"])
logger = get_logger(__name__)


def get_current_user_id(request: Request) -> str:
    """获取当前登录用户ID"""
    user_id = getattr(request.state, 'user_id', None)
    if not user_id:
        raise ApiError(code="auth.unauthorized")
    return user_id


@router.get("/presets/list", response_model=List[dict])
async def get_preset_styles(db: AsyncSession = Depends(get_db)):
    """
    获取所有预设风格列表（从数据库读取）
    
    返回格式：数组形式的预设风格列表
    [
        {"id": 1, "preset_id": "natural", "name": "自然流畅", "description": "...", "prompt_content": "..."},
        {"id": 2, "preset_id": "classical", "name": "古典优雅", ...}
    ]
    """
    # 从数据库获取全局预设风格（user_id 为 NULL）
    result = await db.execute(
        select(WritingStyle)
        .where(WritingStyle.user_id.is_(None))
        .order_by(WritingStyle.order_index)
    )
    preset_styles = result.scalars().all()
    
    # 转换为响应格式
    return [
        {
            "id": style.id,
            "preset_id": style.preset_id,
            "name": style.name,
            "description": style.description,
            "prompt_content": style.prompt_content,
            "profile_json": style.profile_json,
            "profile_version": style.profile_version,
            "style_type": style.style_type,
            "order_index": style.order_index
        }
        for style in preset_styles
    ]


@router.post("", response_model=WritingStyleResponse, status_code=201)
async def create_writing_style(
    style_data: WritingStyleCreate,
    request: Request,
    db: AsyncSession = Depends(get_db)
):
    """
    创建新的写作风格（用户级别）
    
    - **基于预设创建**：提供 preset_id，系统会从数据库查询预设内容自动填充
    - **完全自定义**：不提供 preset_id，需要手动填写所有字段
    """
    # 获取当前用户ID
    user_id = get_current_user_id(request)

    # `profile:` 是项目文笔画像的保留前缀（issue #254），禁止用户手动创建，
    # 否则会占用 upsert 槽位并破坏项目归属判定。
    if is_profile_preset_id(style_data.preset_id):
        raise ApiError(code="validation.profile_preset_reserved")

    # 如果基于预设创建，从数据库获取预设内容
    if style_data.preset_id:
        result = await db.execute(
            select(WritingStyle)
            .where(
                WritingStyle.user_id.is_(None),
                WritingStyle.preset_id == style_data.preset_id
            )
        )
        preset = result.scalar_one_or_none()
        
        if not preset:
            raise ApiError(
            code="not_found.preset_style",
            detail=f"预设风格 '{style_data.preset_id}' 不存在",
            params={"preset_id": style_data.preset_id},
        )
        
        # 使用预设内容填充（如果用户未提供）
        if not style_data.name:
            style_data.name = preset.name
        if not style_data.description:
            style_data.description = preset.description
        if not style_data.prompt_content:
            style_data.prompt_content = preset.prompt_content
    
    # 验证必填字段
    if not style_data.name or not style_data.prompt_content:
        raise ApiError(code="validation.required_fields")
    
    # 获取当前用户的最大 order_index
    count_result = await db.execute(
        select(func.count(WritingStyle.id))
        .where(WritingStyle.user_id == user_id)
    )
    max_order = count_result.scalar_one()
    
    # 创建风格记录
    new_style = WritingStyle(
        user_id=user_id,
        name=style_data.name,
        style_type=style_data.style_type or ("preset" if style_data.preset_id else "custom"),
        preset_id=style_data.preset_id,
        description=style_data.description,
        prompt_content=style_data.prompt_content,
        order_index=max_order + 1
    )
    
    db.add(new_style)
    await db.commit()
    await db.refresh(new_style)
    
    # 返回包含 is_default 字段的字典（新创建的风格默认不是默认风格）
    return {
        "id": new_style.id,
        "user_id": new_style.user_id,
        "name": new_style.name,
        "style_type": new_style.style_type,
        "preset_id": new_style.preset_id,
        "description": new_style.description,
        "prompt_content": new_style.prompt_content,
        "order_index": new_style.order_index,
        "created_at": new_style.created_at,
        "updated_at": new_style.updated_at,
        "is_default": False
    }


@router.get("/user", response_model=WritingStyleListResponse)
async def get_user_styles(
    request: Request,
    db: AsyncSession = Depends(get_db)
):
    """
    获取用户的所有可用写作风格
    
    返回：全局预设风格 + 该用户的自定义风格
    按 order_index 排序
    """
    # 获取当前用户ID
    user_id = get_current_user_id(request)
    
    # 获取全局预设风格（user_id 为 NULL）
    result = await db.execute(
        select(WritingStyle)
        .where(WritingStyle.user_id.is_(None))
        .order_by(WritingStyle.order_index)
    )
    preset_styles = list(result.scalars().all())
    
    # 获取用户自定义风格
    result = await db.execute(
        select(WritingStyle)
        .where(WritingStyle.user_id == user_id)
        .order_by(WritingStyle.order_index)
    )
    custom_styles = list(result.scalars().all())
    
    # 合并：预设风格 + 自定义风格；画像属于项目（issue #254），
    # 用户级风格库不返回任何画像行，避免泄漏到其他项目
    all_styles = [
        style for style in preset_styles + custom_styles
        if not is_profile_preset_id(style.preset_id)
    ]
    
    # 转换为响应格式
    styles_with_default = []
    for style in all_styles:
        style_dict = {
            "id": style.id,
            "user_id": style.user_id,
            "name": style.name,
            "style_type": style.style_type,
            "preset_id": style.preset_id,
            "description": style.description,
            "prompt_content": style.prompt_content,
            "profile_json": style.profile_json,
            "profile_version": style.profile_version,
            "order_index": style.order_index,
            "created_at": style.created_at,
            "updated_at": style.updated_at,
            "is_default": False  # 用户级别不再需要默认风格标记
        }
        styles_with_default.append(style_dict)
    
    return {"styles": styles_with_default, "total": len(styles_with_default)}


@router.get("/project/{project_id}", response_model=WritingStyleListResponse)
async def get_project_styles(
    project_id: str,
    request: Request,
    db: AsyncSession = Depends(get_db)
):
    """
    获取项目可用的所有写作风格（保留用于向后兼容）
    
    返回：全局预设风格 + 该用户的自定义风格
    按 order_index 排序，并标记哪个是当前项目的默认风格
    """
    # 获取当前用户ID
    user_id = get_current_user_id(request)
    
    # 验证项目访问权限
    result = await db.execute(
        select(Project).where(
            Project.id == project_id,
            Project.user_id == user_id
        )
    )
    project = result.scalar_one_or_none()
    if not project:
        raise ApiError(code="not_found.project_or_forbidden")
    
    # 获取该项目的默认风格ID
    result = await db.execute(
        select(ProjectDefaultStyle.style_id)
        .where(ProjectDefaultStyle.project_id == project_id)
    )
    default_style_id = result.scalar_one_or_none()
    
    # 获取全局预设风格（user_id 为 NULL）
    result = await db.execute(
        select(WritingStyle)
        .where(WritingStyle.user_id.is_(None))
        .order_by(WritingStyle.order_index)
    )
    preset_styles = list(result.scalars().all())
    
    # 获取用户自定义风格
    result = await db.execute(
        select(WritingStyle)
        .where(WritingStyle.user_id == user_id)
        .order_by(WritingStyle.order_index)
    )
    custom_styles = list(result.scalars().all())
    
    # 合并：预设风格 + 自定义风格；画像只在所属项目上下文可见（issue #254），
    # 过滤掉其他项目的画像行，仅保留 preset_id == profile_preset_id(project_id) 的那条
    project_profile_id = profile_preset_id(project_id)
    all_styles = [
        style for style in preset_styles + custom_styles
        if not is_profile_preset_id(style.preset_id)
        or style.preset_id == project_profile_id
    ]
    
    # 为每个风格添加 is_default 标记（用于前端显示）
    styles_with_default = []
    for style in all_styles:
        style_dict = {
            "id": style.id,
            "user_id": style.user_id,
            "name": style.name,
            "style_type": style.style_type,
            "preset_id": style.preset_id,
            "description": style.description,
            "prompt_content": style.prompt_content,
            "profile_json": style.profile_json,
            "profile_version": style.profile_version,
            "order_index": style.order_index,
            "created_at": style.created_at,
            "updated_at": style.updated_at,
            "is_default": style.id == default_style_id
        }
        styles_with_default.append(style_dict)
    
    return {"styles": styles_with_default, "total": len(styles_with_default)}


@router.post(
    "/project/{project_id}/profile/reextract",
    response_model=ProfileReextractResponse,
)
async def reextract_project_profile(
    project_id: str,
    request: Request,
    db: AsyncSession = Depends(get_db),
    payload: ProfileReextractRequest = ProfileReextractRequest(),
):
    """基于项目现有正文重新抽取文笔画像（issue #254 P1）。

    - 只使用项目 ``Chapter`` 正文，不导入原文、不改动正文；
    - ``dry_run=True``（默认）只返回 diff 预览，不写库；
    - ``dry_run=False`` 写入 ``preset_id=profile:<project_id>`` 画像行并重建锚点；
    - 画像已被用户手改（``profile_edited``）且未显式授权时保留用户编辑；
    - 样本不足时直接返回 ``skipped=True``，不写库。
    """
    user_id = get_current_user_id(request)

    # 校验项目归属（与 get_project_styles 同语义）
    result = await db.execute(
        select(Project).where(
            Project.id == project_id,
            Project.user_id == user_id
        )
    )
    project = result.scalar_one_or_none()
    if not project:
        raise ApiError(code="not_found.project_or_forbidden")

    # 读项目正文（按章号升序），构造拆书链路使用的章节结构
    db_chapters = list(
        (
            await db.execute(
                select(Chapter)
                .where(Chapter.project_id == project_id)
                .order_by(Chapter.chapter_number)
            )
        ).scalars().all()
    )
    chapters = [
        BookImportChapter(
            title=chapter.title or f"第{chapter.chapter_number}章",
            content=chapter.content or "",
            summary=chapter.summary,
            chapter_number=chapter.chapter_number,
        )
        for chapter in db_chapters
        if chapter.chapter_number and chapter.chapter_number >= 1
    ]

    # AI 服务/模板/语言解析都在方法内部按用户配置构建
    service = BookImportService()
    profile = await service._extract_style_profile(
        db=db,
        user_id=user_id,
        project=project,
        chapters=chapters,
        content_language=None,
        extract_mode="full",
    )

    if profile.get("skipped"):
        return {"skipped": True}

    new_prompt_content = profile["prompt_content"]
    new_profile = profile.get("profile_data")
    description = profile.get("description") or ""
    confidence = profile.get("confidence")
    degraded = bool(profile.get("degraded"))

    # 现有画像行（如有）
    existing = (
        await db.execute(
            select(WritingStyle).where(
                WritingStyle.user_id == user_id,
                WritingStyle.preset_id == profile_preset_id(project_id),
            )
        )
    ).scalars().first()
    old_prompt_content = existing.prompt_content if existing is not None else None
    old_profile = None
    if existing is not None and existing.profile_json:
        try:
            parsed = json.loads(existing.profile_json)
        except (TypeError, ValueError):
            parsed = None
        old_profile = parsed if isinstance(parsed, dict) else None

    # 手改保护在预览阶段也要可见：前端据此提示并禁用"应用"
    kept_reason = (
        "user_edited"
        if existing is not None
        and existing.style_type == PROFILE_EDITED_STYLE_TYPE
        and not payload.overwrite_edited
        else None
    )

    diff = {
        "skipped": False,
        "changed": old_prompt_content != new_prompt_content,
        "old_prompt_content": old_prompt_content,
        "new_prompt_content": new_prompt_content,
        "old_profile": old_profile,
        "new_profile": new_profile,
        "description": description,
        "confidence": confidence,
        "degraded": degraded,
        "kept_reason": kept_reason,
    }

    if payload.dry_run:
        return diff

    style_id = await service._upsert_project_profile_style(
        db=db,
        project=project,
        user_id=user_id,
        prompt_content=new_prompt_content,
        description=description,
        replace_default=payload.set_default,
        allow_overwrite_edited=payload.overwrite_edited,
        profile_data=new_profile,
        profile_version=profile.get("profile_version"),
    )
    if style_id is None:
        # 用户已手改画像（profile_edited），本次不覆盖，也不产生任何写入
        return {**diff, "updated": False, "style_id": None, "kept_reason": "user_edited"}

    await create_project_style_anchors(
        db,
        project_id=project_id,
        ranges=profile.get("ranges") or [],
        chapter_contents={chapter.chapter_number: chapter.content or "" for chapter in db_chapters},
    )
    await db.commit()

    logger.info(
        f"项目 {project_id} 文笔画像已重新抽取（降级={degraded}，"
        f"默认={payload.set_default}，样本 {len(chapters)} 章）"
    )
    return {**diff, "updated": True, "style_id": style_id, "kept_reason": None}


async def _require_owned_project(
    db: AsyncSession, project_id: str, user_id: str
) -> Project:
    """校验项目归属（与画像相关端点同语义）。"""
    project = (
        await db.execute(
            select(Project).where(
                Project.id == project_id,
                Project.user_id == user_id,
            )
        )
    ).scalar_one_or_none()
    if not project:
        raise ApiError(code="not_found.project_or_forbidden")
    return project


@router.get(
    "/project/{project_id}/profile/volumes",
    response_model=VolumeProfileListResponse,
)
async def get_project_profile_volumes(
    project_id: str,
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    """列出项目的分卷画像（计划 §13-3），按卷序号升序。"""
    user_id = get_current_user_id(request)
    await _require_owned_project(db, project_id, user_id)
    volumes = await list_project_volumes(db, project_id)
    return {"volumes": volumes, "total": len(volumes)}


@router.post(
    "/project/{project_id}/profile/volumes",
    response_model=VolumeProfileGenerateResponse,
)
async def generate_project_profile_volumes(
    project_id: str,
    request: Request,
    payload: VolumeProfileGenerateRequest,
    db: AsyncSession = Depends(get_db),
):
    """按章节区间生成分卷文笔画像（计划 §13-3）。

    - 卷序号按 ``volumes`` 列表顺序从 1 开始，``preset_id=profile:<project_id>:v<index>``；
    - 每卷只用区间内章节抽样，调用拆书链路的 ``_extract_style_profile``；
    - ``dry_run=True`` 只返回抽取摘要（不写库）；
    - 卷画像已被用户手改（``profile_edited``）时默认跳过，``overwrite_edited``
      才覆盖；
    - 锚点是项目级共用一个池，这里不重建（沿用全书画像导入时建立的锚点）。
    """
    user_id = get_current_user_id(request)
    project = await _require_owned_project(db, project_id, user_id)

    if len(payload.volumes) > 999:
        # preset_id 列宽 50：profile:<uuid>:v<index> 最多容纳三位卷号
        raise ApiError(
            code="validation.volume_count_exceeded",
            detail="卷数量不能超过 999",
            status=400,
        )

    db_chapters = list(
        (
            await db.execute(
                select(Chapter)
                .where(Chapter.project_id == project_id)
                .order_by(Chapter.chapter_number)
            )
        ).scalars().all()
    )
    chapters = [
        BookImportChapter(
            title=chapter.title or f"第{chapter.chapter_number}章",
            content=chapter.content or "",
            summary=chapter.summary,
            chapter_number=chapter.chapter_number,
        )
        for chapter in db_chapters
        if chapter.chapter_number and chapter.chapter_number >= 1
    ]

    # 先整体校验，避免校验失败前已产生 AI 调用/部分写入
    subsets: list[tuple[int, int, int, list[BookImportChapter]]] = []
    for index, spec in enumerate(payload.volumes, start=1):
        start, end = spec.start_chapter, spec.end_chapter
        if end < start:
            raise ApiError(
                code="validation.volume_range_invalid",
                detail=f"第 {index} 卷区间非法：结束章 {end} 小于起始章 {start}",
                status=400,
                params={"index": index, "start_chapter": start, "end_chapter": end},
            )
        subset = [chapter for chapter in chapters if start <= chapter.chapter_number <= end]
        if not subset:
            raise ApiError(
                code="validation.volume_range_empty",
                detail=f"第 {index} 卷区间 {start}-{end} 内没有任何章节",
                status=400,
                params={"index": index, "start_chapter": start, "end_chapter": end},
            )
        subsets.append((index, start, end, subset))

    existing = {item["index"]: item for item in await list_project_volumes(db, project_id)}

    service = BookImportService()
    items: list[VolumeProfileItem] = []
    for index, start, end, subset in subsets:
        existing_row = None
        if index in existing:
            existing_row = await db.get(WritingStyle, existing[index]["style_id"])

        # 手改保护：不覆盖用户编辑过的卷画像（与全书画像 reextract 同语义）
        if (
            existing_row is not None
            and existing_row.style_type == PROFILE_EDITED_STYLE_TYPE
            and not payload.overwrite_edited
        ):
            items.append(
                VolumeProfileItem(
                    index=index,
                    start_chapter=start,
                    end_chapter=end,
                    skipped=True,
                    skip_reason="user_edited",
                    style_id=existing_row.id,
                )
            )
            continue

        profile = await service._extract_style_profile(
            db=db,
            user_id=user_id,
            project=project,
            chapters=subset,
            content_language=None,
            extract_mode="full",
        )
        if profile.get("skipped"):
            items.append(
                VolumeProfileItem(
                    index=index,
                    start_chapter=start,
                    end_chapter=end,
                    skipped=True,
                    skip_reason="insufficient_sample",
                )
            )
            continue

        item = VolumeProfileItem(
            index=index,
            start_chapter=start,
            end_chapter=end,
            confidence=profile.get("confidence"),
            degraded=bool(profile.get("degraded")),
        )
        if not payload.dry_run:
            item.style_id = await upsert_volume_profile(
                db=db,
                project=project,
                index=index,
                start_chapter=start,
                end_chapter=end,
                prompt_content=profile["prompt_content"],
                description=profile.get("description") or "",
                profile_data=profile.get("profile_data"),
                profile_version=profile.get("profile_version"),
            )
        items.append(item)

    if not payload.dry_run:
        await db.commit()
        logger.info(
            f"项目 {project_id} 分卷画像已生成（{len(items)} 卷，"
            f"覆盖手改={payload.overwrite_edited}）"
        )

    return {
        "volumes": items,
        "total": len(items),
        "dry_run": payload.dry_run,
        "anchors_reused": True,
    }


@router.delete("/project/{project_id}/profile/volumes/{index}", status_code=204)
async def delete_project_profile_volume(
    project_id: str,
    index: int,
    request: Request,
    force: bool = False,
    db: AsyncSession = Depends(get_db),
):
    """删除指定卷画像；``profile_edited`` 需 ``force=true``。

    卷画像不应是项目默认风格；如被手工设为默认，删除时一并清理该默认关系，
    避免悬挂外键。
    """
    user_id = get_current_user_id(request)
    await _require_owned_project(db, project_id, user_id)

    row = (
        await db.execute(
            select(WritingStyle).where(
                WritingStyle.preset_id == volume_preset_id(project_id, index)
            )
        )
    ).scalars().first()
    if row is None or (row.user_id is not None and row.user_id != user_id):
        raise ApiError(code="not_found.writing_style")

    if row.style_type == PROFILE_EDITED_STYLE_TYPE and not force:
        raise ApiError(
            code="conflict.profile_volume_edited",
            detail="卷画像已被用户编辑，需 force=true 才能删除",
            status=409,
        )

    await db.execute(
        delete(ProjectDefaultStyle).where(ProjectDefaultStyle.style_id == row.id)
    )
    await db.delete(row)
    await db.commit()
    return None


@router.get("/{style_id}", response_model=WritingStyleResponse)
async def get_writing_style(
    style_id: int,
    request: Request,
    db: AsyncSession = Depends(get_db)
):
    """获取单个写作风格详情"""
    user_id = get_current_user_id(request)
    result = await db.execute(
        select(WritingStyle).where(WritingStyle.id == style_id)
    )
    style = result.scalar_one_or_none()
    if not style:
        raise ApiError(code="not_found.writing_style")
    if style.user_id is not None and style.user_id != user_id:
        raise ApiError(code="not_found.writing_style")
    
    # 检查是否有项目将其设置为默认风格（一个风格可能被多个项目使用，使用 first() 避免 MultipleResultsFound）
    result = await db.execute(
        select(ProjectDefaultStyle)
        .join(Project, Project.id == ProjectDefaultStyle.project_id)
        .where(ProjectDefaultStyle.style_id == style_id)
    )
    is_default = result.scalars().first() is not None
    
    # 返回包含 is_default 字段的字典
    return {
        "id": style.id,
        "user_id": style.user_id,
        "name": style.name,
        "style_type": style.style_type,
        "preset_id": style.preset_id,
        "description": style.description,
        "prompt_content": style.prompt_content,
        "profile_json": style.profile_json,
        "profile_version": style.profile_version,
        "order_index": style.order_index,
        "created_at": style.created_at,
        "updated_at": style.updated_at,
        "is_default": is_default
    }


@router.put("/{style_id}", response_model=WritingStyleResponse)
async def update_writing_style(
    style_id: int,
    style_data: WritingStyleUpdate,
    request: Request,
    db: AsyncSession = Depends(get_db)
):
    """
    更新写作风格
    
    - 只能修改自定义风格
    - 不能修改全局预设风格
    """
    # 获取当前用户ID
    user_id = get_current_user_id(request)
    
    result = await db.execute(
        select(WritingStyle).where(WritingStyle.id == style_id)
    )
    style = result.scalar_one_or_none()
    if not style:
        raise ApiError(code="not_found.writing_style")
    
    # 检查是否为全局预设风格（不允许修改）
    if style.user_id is None:
        raise ApiError(code="forbidden.preset_style_readonly")
    
    # 验证用户权限（只能修改自己的风格）
    if style.user_id != user_id:
        raise ApiError(code="forbidden.other_user_style")
    
    # 更新字段
    update_data = style_data.model_dump(exclude_unset=True)
    
    # 如果修改了内容，将 style_type 改为 custom；画像行改为 profile_edited（issue #254），
    # 以便自动重抽时区分机器画像与用户改过的画像
    if any(key in update_data for key in ["name", "description", "prompt_content"]):
        update_data["style_type"] = (
            PROFILE_EDITED_STYLE_TYPE
            if is_profile_preset_id(style.preset_id)
            else "custom"
        )
    
    for key, value in update_data.items():
        setattr(style, key, value)
    
    await db.commit()
    await db.refresh(style)
    
    # 检查是否有项目将其设置为默认风格（一个风格可能被多个项目使用，使用 first() 避免 MultipleResultsFound）
    result = await db.execute(
        select(ProjectDefaultStyle)
        .join(Project, Project.id == ProjectDefaultStyle.project_id)
        .where(ProjectDefaultStyle.style_id == style_id)
    )
    is_default = result.scalars().first() is not None
    
    # 返回包含 is_default 字段的字典
    return {
        "id": style.id,
        "user_id": style.user_id,
        "name": style.name,
        "style_type": style.style_type,
        "preset_id": style.preset_id,
        "description": style.description,
        "prompt_content": style.prompt_content,
        "profile_json": style.profile_json,
        "profile_version": style.profile_version,
        "order_index": style.order_index,
        "created_at": style.created_at,
        "updated_at": style.updated_at,
        "is_default": is_default
    }


@router.delete("/{style_id}", status_code=204)
async def delete_writing_style(
    style_id: int,
    request: Request,
    db: AsyncSession = Depends(get_db)
):
    """
    删除写作风格
    
    注意：
    - 只能删除自定义风格，不能删除全局预设风格
    - 不能删除默认风格（必须先设置其他风格为默认）
    - 删除后无法恢复
    """
    # 获取当前用户ID
    user_id = get_current_user_id(request)
    
    result = await db.execute(
        select(WritingStyle).where(WritingStyle.id == style_id)
    )
    style = result.scalar_one_or_none()
    if not style:
        raise ApiError(code="not_found.writing_style")
    
    # 检查是否为全局预设风格（不允许删除）
    if style.user_id is None:
        raise ApiError(code="forbidden.preset_style_readonly", detail="不能删除全局预设风格，只能删除自定义风格")
    
    # 验证用户权限（只能删除自己的风格）
    if style.user_id != user_id:
        raise ApiError(code="forbidden.other_user_style", detail="无权删除其他用户的风格")
    
    # 检查是否有项目将其设置为默认风格（一个风格可能被多个项目使用，使用 first() 避免 MultipleResultsFound）
    result = await db.execute(
        select(ProjectDefaultStyle)
        .join(Project, Project.id == ProjectDefaultStyle.project_id)
        .where(ProjectDefaultStyle.style_id == style_id)
    )
    default_relation = result.scalars().first()
    if default_relation:
        raise ApiError(code="validation.style_default_delete_blocked")
    
    await db.delete(style)
    await db.commit()
    
    return None


@router.post("/{style_id}/set-default", response_model=dict)
async def set_default_style(
    style_id: int,
    request_data: SetDefaultStyleRequest,
    request: Request,
    db: AsyncSession = Depends(get_db)
):
    """
    将指定风格设置为项目的默认风格
    
    使用 project_default_styles 表记录项目的默认风格选择
    每个项目只能有一个默认风格（通过 UniqueConstraint 保证）
    
    参数：
    - style_id: 要设置为默认的风格ID（路径参数）
    - project_id: 项目ID（请求体），用于确定在哪个项目上下文中设置默认
    """
    project_id = request_data.project_id
    
    # 获取当前用户ID
    user_id = get_current_user_id(request)
    
    # 验证项目访问权限
    result = await db.execute(
        select(Project).where(
            Project.id == project_id,
            Project.user_id == user_id
        )
    )
    project = result.scalar_one_or_none()
    if not project:
        raise ApiError(code="not_found.project_or_forbidden")
    
    # 验证风格是否存在
    result = await db.execute(
        select(WritingStyle).where(WritingStyle.id == style_id)
    )
    style = result.scalar_one_or_none()
    if not style:
        raise ApiError(code="not_found.writing_style")
    
    # 验证风格是否属于该用户（自定义风格）或是全局预设风格
    if style.user_id is not None and style.user_id != user_id:
        raise ApiError(code="forbidden.other_user_style", detail="无权操作其他用户的风格")
    
    # 使用 UPSERT 逻辑：先删除该项目的旧默认风格记录，再插入新的
    await db.execute(
        delete(ProjectDefaultStyle).where(ProjectDefaultStyle.project_id == project_id)
    )
    
    # 插入新的默认风格记录
    new_default = ProjectDefaultStyle(
        project_id=project_id,
        style_id=style_id
    )
    db.add(new_default)
    await db.commit()
    
    return {
        "message": "默认风格设置成功",
        "project_id": project_id,
        "style_id": style_id,
        "style_name": style.name
    }


@router.post("/project/{project_id}/init-defaults", response_model=WritingStyleListResponse)
async def initialize_default_styles(
    project_id: str,
    request: Request,
    db: AsyncSession = Depends(get_db)
):
    """
    【已废弃】为项目初始化默认风格
    
    新架构下，预设风格是全局的，不需要为每个项目单独初始化
    该接口保留用于兼容性，直接返回项目可用的所有风格
    """
    # 直接返回项目可用的所有风格（全局预设 + 用户自定义）
    return await get_project_styles(project_id, request, db)
