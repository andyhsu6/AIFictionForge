"""角色关系多类型与项目级关系类型的共享 helper。"""
from __future__ import annotations

from typing import Any, Iterable, Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.relationship import (
    CharacterRelationship,
    RelationshipType,
    RelationshipTypeLink,
)


SYSTEM_CATEGORIES = {"family", "social", "hostile", "professional"}
MAX_RELATIONSHIP_TYPE_NAME = 50
MAX_PROJECT_TYPES_PER_IMPORT = 50
MAX_IMPORTED_CHARACTERS_PER_IMPORT = 30


def normalize_relationship_type_name(raw: Any) -> Optional[str]:
    """归一化关系类型名：空白清理、长度限制、过滤无效值。"""
    if raw is None:
        return None
    name = str(raw).strip()
    if not name:
        return None
    if len(name) > MAX_RELATIONSHIP_TYPE_NAME:
        name = name[:MAX_RELATIONSHIP_TYPE_NAME].strip()
    return name or None


def is_probably_proper_noun_type(name: str) -> bool:
    """判断类型名是否包含明显专名特征，应降级为描述而非进入类型池。"""
    if not name:
        return True
    generic_prefixes = ("宗门", "门派", "家族", "师门", "帮派", "国家", "皇族", "世家")
    if name.startswith(generic_prefixes):
        return False
    # 门派/家族/地名/组织名 + 称谓后缀，通常是一次性专名
    org_markers = ("宗", "门", "派", "族", "家", "国", "城", "山", "殿", "阁", "府")
    role_suffixes = ("弟子", "主人", "少主", "少掌门", "公子", "小姐", "少爷", "家主", "长老", "掌门", "公主", "皇子")
    if any(m in name for m in org_markers) and any(s in name for s in role_suffixes):
        return True
    # 生物/神兽/妖类专名 + 契约/血脉/主人等组合，例如“九尾狐契约”“龙族血脉”
    if any(part in name for part in ("狐", "龙", "凤", "麒麟", "妖", "魔", "兽", "鬼", "神")) and any(
        part in name for part in ("契约", "血脉", "主人", "坐骑", "灵宠", "宠物")
    ):
        return True
    if any(part in name for part in ("记名弟子", "亲传弟子", "少掌门", "少主", "大小姐", "三少爷")):
        return False
    return False


async def resolve_relationship_type_ids(
    db: AsyncSession,
    project_id: str,
    names: Optional[Iterable[Any]],
    source: str = "manual",
    category: str = "custom",
) -> list[int]:
    """把关系类型名解析为 ID；系统或项目内已有同名则复用，否则新建项目级类型。"""
    if not names:
        return []
    normalized_names: list[str] = []
    for raw in names:
        name = normalize_relationship_type_name(raw)
        if name and name not in normalized_names:
            normalized_names.append(name)
    if not normalized_names:
        return []

    rows = (
        await db.execute(
            select(RelationshipType).where(
                (RelationshipType.project_id == project_id)
                | (RelationshipType.project_id.is_(None))
            ).where(RelationshipType.name.in_(normalized_names))
        )
    ).scalars().all()
    existing = {r.name: r.id for r in rows}

    ids: list[int] = []
    for name in normalized_names:
        if name in existing:
            ids.append(existing[name])
            continue
        rt = RelationshipType(
            project_id=project_id,
            name=name,
            category=category if category in SYSTEM_CATEGORIES or category == "custom" else "custom",
            source=source,
            is_system=False,
        )
        db.add(rt)
        await db.flush()
        ids.append(rt.id)
        existing[name] = rt.id
    return ids


async def sync_relationship_links(
    db: AsyncSession,
    relationship: CharacterRelationship,
    type_ids: Optional[Iterable[int]],
) -> None:
    """同步关系与类型的多对多关联，并把第一个类型写回旧缓存列。"""
    unique_ids = list(dict.fromkeys(int(x) for x in (type_ids or [])))
    existing = (
        await db.execute(
            select(RelationshipTypeLink.relationship_type_id).where(
                RelationshipTypeLink.relationship_id == relationship.id
            )
        )
    ).scalars().all()
    current = set(existing)
    target = set(unique_ids)

    for type_id in target - current:
        db.add(RelationshipTypeLink(relationship_id=relationship.id, relationship_type_id=type_id))
    for type_id in current - target:
        link = (
            await db.execute(
                select(RelationshipTypeLink).where(
                    RelationshipTypeLink.relationship_id == relationship.id,
                    RelationshipTypeLink.relationship_type_id == type_id,
                )
            )
        ).scalar_one_or_none()
        if link:
            await db.delete(link)

    relationship.relationship_type_id = unique_ids[0] if unique_ids else None


async def relationship_display_names(
    db: AsyncSession,
    relationship: CharacterRelationship,
) -> list[str]:
    """返回关系关联的类型名列表（旧缓存列未命中时兜底）。"""
    if relationship.id:
        rows = (
            await db.execute(
                select(RelationshipType.name)
                .join(RelationshipTypeLink, RelationshipTypeLink.relationship_type_id == RelationshipType.id)
                .where(RelationshipTypeLink.relationship_id == relationship.id)
                .order_by(RelationshipType.id)
            )
        ).scalars().all()
        if rows:
            return list(rows)
    if relationship.relationship_type_id:
        rt = (
            await db.execute(
                select(RelationshipType).where(RelationshipType.id == relationship.relationship_type_id)
            )
        ).scalar_one_or_none()
        if rt:
            return [rt.name]
    return []


async def relationship_display_name(
    db: AsyncSession,
    relationship: CharacterRelationship,
    custom_label: Optional[str] = None,
) -> str:
    """生成展示名：自定义名（类型1、类型2）或类型并集。"""
    names = await relationship_display_names(db, relationship)
    label = (custom_label or relationship.relationship_name or "").strip()
    if names and label:
        return f"{label}（{'、'.join(names)}）"
    if names:
        return "、".join(names)
    return label or "未知关系"


async def ensure_relationship_type_not_in_use(
    db: AsyncSession,
    project_id: str,
    relationship_type_id: int,
) -> bool:
    """删除项目类型前检查是否仍被关系引用。返回 True 表示仍在使用。"""
    row = (
        await db.execute(
            select(RelationshipTypeLink.id).where(
                RelationshipTypeLink.relationship_type_id == relationship_type_id
            ).limit(1)
        )
    ).scalar_one_or_none()
    if row:
        return True
    rel = (
        await db.execute(
            select(CharacterRelationship.id).where(
                CharacterRelationship.project_id == project_id,
                CharacterRelationship.relationship_type_id == relationship_type_id,
            ).limit(1)
        )
    ).scalar_one_or_none()
    return bool(rel)
