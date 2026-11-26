"""已确立专名块（生成期提示词）的行为测试。"""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.database import Base
from app.models.character import Character
from app.models.outline import Outline
from app.models.project import Project
from app.services.prompt_names import (
    NEW_NAME_BUDGET,
    build_established_names_block,
    render_names_block,
)

PROJECT_ID = "p-names"
USER_ID = "u-names"


def _char(name: str, *, org: bool = False, aliases: list[str] | None = None) -> SimpleNamespace:
    return SimpleNamespace(
        name=name,
        is_organization=org,
        aliases=json.dumps(aliases or [], ensure_ascii=False),
    )


def test_render_includes_people_aliases_orgs_and_budget():
    block = render_names_block(
        people=[_char("甲", aliases=["甲哥", "老甲"]), _char("乙")],
        organizations=[_char("某堂", org=True)],
        outline_names=["甲", "某堂"],
        scenes=["某县书房（甲整理案卷）", "某楼"],
    )
    assert "人物：" in block and "甲（甲哥/老甲）" in block
    assert "组织：某堂" in block
    assert "本章大纲涉及：甲、某堂" in block
    assert "某县书房" in block and "（甲整理案卷）" not in block
    assert f"最多新增 {NEW_NAME_BUDGET} 个专名" in block
    assert "泛称" in block


def test_render_caps_alias_count_and_returns_empty_without_sources():
    block = render_names_block(
        people=[_char("甲", aliases=["一", "二", "三"])],
        organizations=[],
        outline_names=[],
        scenes=[],
    )
    assert "甲（一/二）" in block
    assert "三" not in block

    assert render_names_block(people=[], organizations=[], outline_names=[], scenes=[]) == ""


def test_build_reads_characters_and_outline():
    async def scenario():
        engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        factory = async_sessionmaker(bind=engine, expire_on_commit=False)
        async with factory() as session:
            session.add(Project(id=PROJECT_ID, user_id=USER_ID, title="t"))
            session.add(Character(id="c1", project_id=PROJECT_ID, name="甲", aliases='["甲哥"]'))
            session.add(Character(id="c2", project_id=PROJECT_ID, name="某堂", is_organization=True))
            session.add(
                Outline(
                    id="o1",
                    project_id=PROJECT_ID,
                    title="第9章",
                    order_index=9,
                    structure=json.dumps(
                        {"characters": [{"name": "乙"}], "scenes": ["某楼（人多）"], "title": "第9章"},
                        ensure_ascii=False,
                    ),
                )
            )
            await session.commit()

            project = (await session.get(Project, PROJECT_ID))
            block = await build_established_names_block(
                session, project=project, chapter_number=9
            )
            assert "甲（甲哥）" in block
            assert "某堂" in block
            assert "乙" in block
            assert "某楼" in block

            other = await build_established_names_block(
                session, project=project, chapter_number=99
            )
            assert "本章大纲涉及" not in other

        await engine.dispose()

    asyncio.run(scenario())
