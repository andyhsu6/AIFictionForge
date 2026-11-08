"""分卷文笔画像（计划 §13-3）：一书多卷，每卷一份画像。

卷画像复用 ``WritingStyle`` 行，``preset_id = profile:<project_id>:v<index>``
（``is_profile_preset_id`` 前缀判定仍有效）；区间写在 ``profile_json.volume``：

    {"index": 1, "start_chapter": 1, "end_chapter": 25}

项目默认风格仍指向全书画像 ``profile:<project_id>``，卷画像不设为默认。
本模块只做数据读写与区间匹配，AI 抽取由导入服务负责。
"""
from __future__ import annotations

import json
from typing import Any, Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..models.project import Project
from ..models.writing_style import WritingStyle
from .style_profile import PROFILE_PRESET_PREFIX, PROFILE_STYLE_TYPE, profile_preset_id

VOLUME_MARKER = ":v"


def volume_preset_id(project_id: str, index: int) -> str:
    """卷画像的 preset_id（全书画像 44 字符 + ``:v<index>``，列宽 50）。"""
    return f"{profile_preset_id(project_id)}{VOLUME_MARKER}{int(index)}"


def parse_volume_preset_id(preset_id: Optional[str]) -> Optional[tuple[str, int]]:
    """解析卷画像 preset_id；非卷画像（含全书画像）返回 None。"""
    if not isinstance(preset_id, str) or not preset_id.startswith(PROFILE_PRESET_PREFIX):
        return None
    rest = preset_id[len(PROFILE_PRESET_PREFIX):]
    if VOLUME_MARKER not in rest:
        return None
    project_id, _, suffix = rest.rpartition(VOLUME_MARKER)
    if not project_id or not suffix.isdigit():
        return None
    index = int(suffix)
    return (project_id, index) if index >= 1 else None


def _volume_meta(row: WritingStyle) -> Optional[dict[str, Any]]:
    """从画像行读取 ``profile_json.volume``；缺失/不合法返回 None。"""
    raw = row.profile_json
    if not raw:
        return None
    try:
        parsed = json.loads(raw)
    except (TypeError, ValueError):
        return None
    if not isinstance(parsed, dict):
        return None
    volume = parsed.get("volume")
    if not isinstance(volume, dict):
        return None
    start = volume.get("start_chapter")
    end = volume.get("end_chapter")
    index = volume.get("index")
    if not all(isinstance(value, int) for value in (start, end, index)):
        return None
    return {"index": index, "start_chapter": start, "end_chapter": end, "parsed": parsed}


async def _volume_rows(db: AsyncSession, project_id: str) -> list[WritingStyle]:
    rows = (
        await db.execute(
            select(WritingStyle).where(
                WritingStyle.preset_id.like(f"{profile_preset_id(project_id)}{VOLUME_MARKER}%")
            )
        )
    ).scalars().all()
    return list(rows)


async def list_project_volumes(db: AsyncSession, project_id: str) -> list[dict[str, Any]]:
    """列出项目的卷画像（按 index 升序）；``profile_json.volume`` 缺失的行忽略。"""
    volumes: list[dict[str, Any]] = []
    for row in await _volume_rows(db, project_id):
        meta = _volume_meta(row)
        if meta is None:
            continue
        confidence = meta["parsed"].get("confidence")
        volumes.append(
            {
                "index": meta["index"],
                "start_chapter": meta["start_chapter"],
                "end_chapter": meta["end_chapter"],
                "style_id": row.id,
                "confidence": confidence if isinstance(confidence, (int, float)) else None,
                "updated_at": row.updated_at,
            }
        )
    volumes.sort(key=lambda item: item["index"])
    return volumes


async def resolve_volume_for_chapter(
    db: AsyncSession, project_id: str, chapter_number: Optional[int]
) -> Optional[WritingStyle]:
    """返回覆盖该章号的卷画像行；多卷重叠取 index 最小，无匹配返回 None。"""
    if chapter_number is None:
        return None
    try:
        number = int(chapter_number)
    except (TypeError, ValueError):
        return None

    best: Optional[WritingStyle] = None
    best_index: Optional[int] = None
    for row in await _volume_rows(db, project_id):
        meta = _volume_meta(row)
        if meta is None:
            continue
        if not (meta["start_chapter"] <= number <= meta["end_chapter"]):
            continue
        if best_index is None or meta["index"] < best_index:
            best, best_index = row, meta["index"]
    return best


async def upsert_volume_profile(
    db: AsyncSession,
    *,
    project: Project,
    index: int,
    start_chapter: int,
    end_chapter: int,
    prompt_content: str,
    description: str = "",
    profile_data: Optional[dict[str, Any]] = None,
    profile_version: Optional[int] = None,
) -> int:
    """创建/更新卷画像行（``style_type="profile"``，不设默认），返回 style id。

    ``profile_data`` 为本次抽取的结构化画像；``volume`` 字段由本函数注入，
    调用方传入的 ``profile_data`` 里即使带 ``volume`` 也会被覆盖。
    """
    preset_id = volume_preset_id(project.id, index)
    row = (
        await db.execute(
            select(WritingStyle).where(
                WritingStyle.user_id == project.user_id,
                WritingStyle.preset_id == preset_id,
            )
        )
    ).scalars().first()

    volume = {
        "index": int(index),
        "start_chapter": int(start_chapter),
        "end_chapter": int(end_chapter),
    }
    base: dict[str, Any] = {}
    if profile_data is not None:
        base = dict(profile_data)
    elif row is not None and row.profile_json:
        try:
            existing = json.loads(row.profile_json)
        except (TypeError, ValueError):
            existing = None
        if isinstance(existing, dict):
            base = dict(existing)
    base["volume"] = volume
    profile_json = json.dumps(base, ensure_ascii=False)

    title = (project.title or "").strip() or "未命名项目"
    name = f"文笔画像·{title[:20]}·卷{int(index)}"

    if row is not None:
        row.name = name
        row.description = description
        row.prompt_content = prompt_content
        row.style_type = PROFILE_STYLE_TYPE
        row.profile_json = profile_json
        row.profile_version = profile_version
        await db.flush()
        return row.id

    row = WritingStyle(
        user_id=project.user_id,
        name=name,
        style_type=PROFILE_STYLE_TYPE,
        preset_id=preset_id,
        description=description,
        prompt_content=prompt_content,
        profile_json=profile_json,
        profile_version=profile_version,
        order_index=0,
    )
    db.add(row)
    await db.flush()
    return row.id
