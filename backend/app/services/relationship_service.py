"""角色关系多类型与项目级关系类型的共享 helper。"""
from __future__ import annotations

from typing import Any, Iterable, Optional

from sqlalchemy import func, select
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

# 非规范称谓 → 规范关系类型名（仅收录语义明确等同的别名，避免过度映射）。
# 规范名选型：恋人（模板家族关系清单中的首选）、丈夫/妻子、父亲/母亲、哥哥/弟弟。
RELATIONSHIP_TYPE_SYNONYMS: dict[str, str] = {
    "情侣": "恋人",
    "男友": "恋人",
    "女友": "恋人",
    "爱人": "恋人",
    "对象": "恋人",
    "老公": "丈夫",
    "老婆": "妻子",
    "爹": "父亲",
    "爸": "父亲",
    "爸爸": "父亲",
    "娘": "母亲",
    "妈": "母亲",
    "妈妈": "母亲",
    "闺女": "女儿",
    "兄长": "哥哥",
    "小弟": "弟弟",
}

# 层级包含规则：具体类型 → 其上位泛称；两者同时出现时保留具体类型、折叠泛称。
# 仅收录 血亲 ⊃ 核心直系/手足关系，泛称如 家人/亲属 语义更宽（含姻亲），不做折叠。
RELATIONSHIP_TYPE_CONTAINED_BY: dict[str, str] = {
    "父子": "血亲",
    "父女": "血亲",
    "母子": "血亲",
    "母女": "血亲",
    "兄弟姐妹": "血亲",
    "姐弟": "血亲",
    "兄妹": "血亲",
    "兄弟": "血亲",
    "姐妹": "血亲",
    "祖孙": "血亲",
}


def normalize_relationship_type_set(type_names: Iterable[Any]) -> list[str]:
    """把关系类型集合归并为最小集：同义词折叠 → 层级冗余折叠 → 去重。

    输入按出现顺序保留；仅当两个名称语义完全等同（同义词映射）或存在
    明确的包含关系（具体 ⊂ 泛称）时才移除冗余项，其余类型原样保留。
    """
    names: list[str] = []
    for raw in type_names or ():
        name = normalize_relationship_type_name(raw)
        if not name:
            continue
        names.append(RELATIONSHIP_TYPE_SYNONYMS.get(name, name))

    result: list[str] = []
    covered: set[str] = set()
    for name in dict.fromkeys(names):
        if name in covered:
            continue
        supertype = RELATIONSHIP_TYPE_CONTAINED_BY.get(name)
        if supertype and supertype in result:
            result.remove(supertype)
        if supertype:
            covered.add(supertype)
        result.append(name)
    return result


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
    max_auto_types: Optional[int] = None,
) -> list[int]:
    """把关系类型名解析为 ID；系统或项目内已有同名则复用，否则新建项目级类型。

    max_auto_types: 若提供，则当"待新建"项目级类型数达到该上限后停止自动注册
    （已有类型与系统类型不受影响），用于限制批量导入时类型池膨胀。
    """
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

    # 项目已注册的类型总数（含新建计数），供自动注册上限使用
    project_type_count = 0
    if max_auto_types is not None:
        project_type_count = (
            await db.execute(
                select(func.count(RelationshipType.id)).where(
                    RelationshipType.project_id == project_id
                )
            )
        ).scalar_one()

    ids: list[int] = []
    for name in normalized_names:
        if name in existing:
            ids.append(existing[name])
            continue
        if max_auto_types is not None and project_type_count >= max_auto_types:
            # 达到自动注册上限：跳过新建，不再为本次未命中类型注册
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
        project_type_count += 1
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


async def find_existing_relationship(
    db: AsyncSession,
    project_id: str,
    character_a_id: str,
    character_b_id: str,
) -> Optional[CharacterRelationship]:
    """按角色对（无视方向）查找已存在的关系记录，用于生成路径去重。

    同一条角色关系无论以 A→B 还是 B→A 写入，都应命中同一条记录，
    避免 AI 生成方向不稳定时产生对称重复。
    """
    if not character_a_id or not character_b_id:
        return None
    row = (
        await db.execute(
            select(CharacterRelationship).where(
                CharacterRelationship.project_id == project_id,
                CharacterRelationship.character_from_id.in_([character_a_id, character_b_id]),
                CharacterRelationship.character_to_id.in_([character_a_id, character_b_id]),
            ).limit(1)
        )
    ).scalar_one_or_none()
    return row


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
