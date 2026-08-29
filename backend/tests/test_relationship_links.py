"""关系多类型 helper 的数据库集成测试。"""
import asyncio

from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker, AsyncSession

from app.models.relationship import RelationshipType, CharacterRelationship, RelationshipTypeLink
from app.services.relationship_service import (
    resolve_relationship_type_ids,
    sync_relationship_links,
    relationship_display_names,
)


from app.database import Base


async def _run_in_memory(coro_factory):
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with factory() as session:
        await coro_factory(session)
    await engine.dispose()


def test_project_type_auto_create_and_reuse():
    async def scenario(session):
        ids_a = await resolve_relationship_type_ids(session, "project-a", ["道侣", "剑侍"], source="import")
        ids_a2 = await resolve_relationship_type_ids(session, "project-a", ["道侣"], source="import")
        ids_b = await resolve_relationship_type_ids(session, "project-b", ["道侣"], source="import")
        assert ids_a[0] == ids_a2[0]
        assert ids_a[0] != ids_b[0]
    asyncio.run(_run_in_memory(scenario))


def test_sync_links_and_display_names():
    async def scenario(session):
        type_ids = await resolve_relationship_type_ids(session, "project-a", ["师生", "姐弟", "情侣"], source="import")
        rel = CharacterRelationship(
            id="rel-1",
            project_id="project-a",
            character_from_id="char-1",
            character_to_id="char-2",
            source="manual",
        )
        session.add(rel)
        await session.flush()
        await sync_relationship_links(session, rel, type_ids)
        names = await relationship_display_names(session, rel)
        assert set(names) == {"师生", "姐弟", "情侣"}
        assert rel.relationship_type_id == type_ids[0]
    asyncio.run(_run_in_memory(scenario))
