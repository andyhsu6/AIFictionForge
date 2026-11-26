"""已确立专名名单（生成期提示词块）。

为什么需要
----------
模型在没有约束时会自造地名/组织名（实测某一章凭空造了三个未在正文出现过的镇名）。
自造本身不是错误，但专名会沿「正文 → 分析 → 派生事实 → 下一章提示词」这条链路进入
正史，越写越难对齐。

策略是**分级**而不是禁止：
1. 已确立专名优先复用（人物、别名、组织、本章大纲涉及的场景）；
2. 允许新增，但每章有限额，且必须带锚点（距某地几日路程 / 隶属某组织）；
3. 一次性地点用泛称，不给专名。
"""

from __future__ import annotations

import json
import re
from typing import Any, Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.character import Character
from app.models.outline import Outline
from app.models.project import Project

MAX_PEOPLE = 12
MAX_ORGS = 8
MAX_OUTLINE_NAMES = 8
MAX_SCENES = 4
MAX_ALIASES = 2
NEW_NAME_BUDGET = 2

# 「湖阳县衙书房（汤泰安梳理案卷…）」→ 取括号前的场景名
_SCENE_SUFFIX_RE = re.compile(r"[（(].*?[）)]\s*$")


def _aliases(raw: Any) -> list[str]:
    try:
        items = json.loads(raw or "[]")
    except (TypeError, ValueError):
        return []
    if not isinstance(items, list):
        return []
    return [str(item).strip() for item in items if isinstance(item, str) and item.strip()]


def _render_people(characters: list[Character]) -> str:
    parts: list[str] = []
    for character in characters[:MAX_PEOPLE]:
        aliases = _aliases(character.aliases)[:MAX_ALIASES]
        label = character.name or ""
        if not label:
            continue
        if aliases:
            label += f"（{'/'.join(aliases)}）"
        parts.append(label)
    return "、".join(parts)


def render_names_block(
    *,
    people: list[Character],
    organizations: list[Character],
    outline_names: list[str],
    scenes: list[str],
) -> str:
    """渲染专名块；三类来源都没有时返回空串。"""
    people_text = _render_people([c for c in people if not c.is_organization])
    orgs_text = "、".join(
        c.name for c in organizations if c.is_organization and c.name
    )
    orgs_text = "、".join(orgs_text.split("、")[:MAX_ORGS])

    outline_text = "、".join(dict.fromkeys(n for n in outline_names if n))[:400]
    scene_text = "、".join(
        dict.fromkeys(_SCENE_SUFFIX_RE.sub("", scene).strip() for scene in scenes if scene)
    )

    if not any((people_text, orgs_text, outline_text, scene_text)):
        return ""

    lines = ["【已确立专名（优先复用，不要另造同义名称）】"]
    if people_text:
        lines.append(f"人物：{people_text}")
    if orgs_text:
        lines.append(f"组织：{orgs_text}")
    if outline_text:
        lines.append(f"本章大纲涉及：{outline_text}")
    if scene_text:
        lines.append(f"本章场景：{scene_text}")
    lines.append(
        f"命名规则：本章最多新增 {NEW_NAME_BUDGET} 个专名；新增必须带锚点"
        "（距某地几日路程、隶属某组织等）；只出现一次的地点用泛称（如“邻县某镇”），不给专名。"
    )
    return "\n".join(lines)


async def build_established_names_block(
    db: AsyncSession,
    *,
    project: Optional[Project],
    chapter_number: Optional[int],
) -> str:
    """从角色表 + 本章大纲收集已确立专名；查不到时返回空串（不阻断生成）。"""
    if project is None:
        return ""
    characters = (
        await db.execute(
            select(Character).where(Character.project_id == project.id).order_by(Character.created_at)
        )
    ).scalars().all()
    if not characters:
        return ""

    outline_names: list[str] = []
    scenes: list[str] = []
    if chapter_number:
        outline = (
            await db.execute(
                select(Outline).where(
                    Outline.project_id == project.id,
                    Outline.order_index == chapter_number,
                )
            )
        ).scalar_one_or_none()
        if outline is not None:
            try:
                structure = json.loads(outline.structure or "{}")
            except ValueError:
                structure = {}
            if isinstance(structure, dict):
                for entry in structure.get("characters") or []:
                    if isinstance(entry, dict) and entry.get("name"):
                        outline_names.append(str(entry["name"]))
                    elif isinstance(entry, str) and entry.strip():
                        outline_names.append(entry.strip())
                scenes = [str(s) for s in (structure.get("scenes") or []) if isinstance(s, str)][:MAX_SCENES]
                title = structure.get("title")
                if isinstance(title, str) and title.strip():
                    outline_names.append(title.strip())

    return render_names_block(
        people=[c for c in characters if not c.is_organization][:MAX_PEOPLE],
        organizations=[c for c in characters if c.is_organization][:MAX_ORGS],
        outline_names=outline_names[:MAX_OUTLINE_NAMES],
        scenes=scenes,
    )
