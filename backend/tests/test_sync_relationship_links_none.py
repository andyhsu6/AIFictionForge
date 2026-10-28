"""`sync_relationship_links` 必须容忍 None 类型 id（issue #180）。

背景：关系抽取合流时会把 `matched.relationship_type_id` 一起塞进类型集合，
而新建关系在还没解析出类型时该字段是 None。旧实现 `int(x)` 直接抛
`TypeError`，把整轮抽取（以及本轮所有已抽到的关系）一起回滚。
"""
from __future__ import annotations

import os
import uuid

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import app.database  # noqa: F401 —— 必须先于 models 导入，避免循环导入
from app.database import Base
from app.models.project import Project
from app.models.relationship import CharacterRelationship, RelationshipType, RelationshipTypeLink
from app.services.relationship_service import sync_relationship_links

PROJECT_ID = "proj-none-type-1"


@pytest.fixture
async def db():
    db_path = f"/tmp/test_sync_links_{uuid.uuid4().hex}.db"
    engine = create_async_engine(f"sqlite+aiosqlite:///{db_path}")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    Session = async_sessionmaker(bind=engine, expire_on_commit=False)
    async with Session() as session:
        session.add(Project(id=PROJECT_ID, user_id="user-1", title="placeholder project"))
        session.add(RelationshipType(id=7, name="placeholder type", category="friend"))
        await session.commit()
        yield session
    await engine.dispose()
    if os.path.exists(db_path):
        os.remove(db_path)


async def _links(db, relationship_id: str) -> list[int]:
    return list((
        await db.execute(
            select(RelationshipTypeLink.relationship_type_id).where(
                RelationshipTypeLink.relationship_id == relationship_id
            )
        )
    ).scalars().all())


@pytest.mark.anyio
async def test_none_type_id_does_not_raise_and_keeps_valid_types(db):
    """None 与有效 id 混入 → 不抛异常，只写有效类型，缓存列取第一个有效 id。"""
    rel = CharacterRelationship(
        id="rel-none-1",
        project_id=PROJECT_ID,
        character_from_id="c1",
        character_to_id="c2",
        relationship_name="placeholder relation",
        relationship_type_id=None,
        status="active",
    )
    db.add(rel)
    await db.commit()

    await sync_relationship_links(db, rel, [None, 7])
    await db.commit()

    assert await _links(db, rel.id) == [7]
    await db.refresh(rel)
    assert rel.relationship_type_id == 7


@pytest.mark.anyio
async def test_only_none_leaves_relationship_untyped(db):
    """全部为 None → 不抛异常，不建链接，缓存列保持 None。"""
    rel = CharacterRelationship(
        id="rel-none-2",
        project_id=PROJECT_ID,
        character_from_id="c3",
        character_to_id="c4",
        relationship_name="placeholder relation two",
        relationship_type_id=None,
        status="active",
    )
    db.add(rel)
    await db.commit()

    await sync_relationship_links(db, rel, [None])
    await db.commit()

    assert await _links(db, rel.id) == []
    await db.refresh(rel)
    assert rel.relationship_type_id is None
