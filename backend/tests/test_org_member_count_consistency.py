"""组织成员计数一致性测试（issue #195）。

钉死契约：
(a) `sync_organization_member_count` 按「成员行数」统计，不按 status 过滤；
(b) 档案同步新建成员行时 member_count 恰好 +1；
(c) 已有成员行改 position/status 时 member_count 不变（行数没变）；
(d) source='manual' 的既有成员不被覆盖、计数不变；
(e) `fix_organization_member_counts` 跑完后 member_count 等于成员行数。

夹具与断言文本全部为中性占位（AGENTS.md 脱敏硬约束）。
"""
from __future__ import annotations

import json
import os
import uuid

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.database import Base
from app.models.chapter import Chapter
from app.models.character import Character
from app.models.project import Project
from app.models.relationship import Organization, OrganizationMember
from app.services.character_profile_sync_service import (
    CharacterProfileSyncService,
    character_profile_sync_service,
)
from app.utils.data_consistency import (
    fix_organization_member_counts,
    sync_organization_member_count,
)

PROJECT_ID = "proj-mc-1"
USER_ID = "user-mc-1"

pytestmark = pytest.mark.anyio


class FakeAIService:
    """记录调用次数的最小 AI 桩。"""

    def __init__(self, response=None):
        self.response = response
        self.calls: list[dict] = []

    async def call_with_json_retry(self, *, prompt, **kwargs):
        self.calls.append({"prompt": prompt, **kwargs})
        return self.response


def _ai_payload(name: str, organizations: list[dict]) -> dict:
    return {
        "characters": [
            {
                "name": name,
                "fields": {
                    "personality": None,
                    "background": None,
                    "appearance": None,
                    "current_state": None,
                },
                "organizations": organizations,
            }
        ]
    }


@pytest.fixture
async def db_session():
    db_path = f"/tmp/test_org_member_count_{uuid.uuid4().hex}.db"
    engine = create_async_engine(f"sqlite+aiosqlite:///{db_path}")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    Session = async_sessionmaker(bind=engine, expire_on_commit=False)
    async with Session() as session:
        yield session
    await engine.dispose()
    if os.path.exists(db_path):
        os.remove(db_path)


@pytest.fixture(autouse=True)
def _clear_reentry_guard():
    """防重入集合是类级状态，用例之间必须清空。"""
    CharacterProfileSyncService._in_progress.clear()
    CharacterProfileSyncService._world_in_progress.clear()
    yield
    CharacterProfileSyncService._in_progress.clear()
    CharacterProfileSyncService._world_in_progress.clear()


async def _seed_project(db) -> None:
    db.add(Project(id=PROJECT_ID, user_id=USER_ID, title="占位项目", genre="通用"))
    await db.commit()


async def _add_org(db, name: str, *, member_count: int = 0) -> Organization:
    org_character = Character(project_id=PROJECT_ID, name=name, is_organization=True)
    db.add(org_character)
    await db.flush()
    organization = Organization(
        character_id=org_character.id, project_id=PROJECT_ID, member_count=member_count
    )
    db.add(organization)
    await db.commit()
    return organization


async def _add_character(db, name: str) -> Character:
    character = Character(project_id=PROJECT_ID, name=name)
    db.add(character)
    await db.commit()
    return character


async def _add_member(
    db,
    organization: Organization,
    character_id: str,
    *,
    position: str = "占位职位",
    status: str = "active",
    source: str = "ai",
) -> OrganizationMember:
    membership = OrganizationMember(
        organization_id=organization.id,
        character_id=character_id,
        position=position,
        status=status,
        source=source,
    )
    db.add(membership)
    await db.commit()
    return membership


async def _member_rows(db, organization: Organization) -> list[OrganizationMember]:
    return list(
        (
            await db.execute(
                select(OrganizationMember).where(
                    OrganizationMember.organization_id == organization.id
                )
            )
        ).scalars()
    )


async def test_sync_member_count_counts_all_rows_and_is_idempotent(db_session):
    """3 条成员行（2 active + 1 deceased）→ 返回 3 并写入 member_count=3。"""
    await _seed_project(db_session)
    organization = await _add_org(db_session, "占位组织", member_count=0)
    for index, status in enumerate(["active", "active", "deceased"]):
        character = await _add_character(db_session, f"占位角色{index}")
        await _add_member(db_session, organization, character.id, status=status)

    actual = await sync_organization_member_count(organization, db_session)
    await db_session.commit()

    assert actual == 3
    await db_session.refresh(organization)
    assert organization.member_count == 3

    # 重复调用幂等：值不变、返回值不变
    assert await sync_organization_member_count(organization, db_session) == 3
    await db_session.commit()
    await db_session.refresh(organization)
    assert organization.member_count == 3


async def test_profile_sync_new_membership_increments_count_by_one(db_session):
    """档案同步新建成员行 → member_count +1，成员表行数 +1。"""
    await _seed_project(db_session)
    character = Character(
        project_id=PROJECT_ID,
        name="角色甲",
        ai_profile_snapshot=json.dumps({"personality": "占位旧性格"}, ensure_ascii=False),
        profile_synced_through_chapter=0,
    )
    db_session.add(character)
    db_session.add(
        Chapter(
            project_id=PROJECT_ID,
            chapter_number=1,
            title="占位章节标题1",
            content="第1章占位正文。角色甲加入占位组织。",
            word_count=18,
        )
    )
    await db_session.commit()
    organization = await _add_org(db_session, "占位组织", member_count=2)
    for index in range(2):
        other = await _add_character(db_session, f"占位旧成员{index}")
        await _add_member(db_session, organization, other.id)

    ai = FakeAIService(
        _ai_payload(
            "角色甲",
            [{"organization_name": "占位组织", "position": "占位职位", "status": "active"}],
        )
    )
    await character_profile_sync_service.sync(
        db=db_session, user_id=USER_ID, project_id=PROJECT_ID, through_chapter=1, ai_service=ai
    )

    await db_session.refresh(organization)
    assert organization.member_count == 3
    assert len(await _member_rows(db_session, organization)) == 3


async def test_profile_sync_existing_membership_position_change_keeps_count(db_session):
    """已有成员行只改 position → member_count 不变。"""
    await _seed_project(db_session)
    organization = await _add_org(db_session, "占位组织", member_count=1)
    character = await _add_character(db_session, "占位角色甲")
    membership = await _add_member(
        db_session, organization, character.id, position="占位旧职位"
    )

    await character_profile_sync_service._apply_organizations(
        db=db_session,
        character=character,
        organizations=[
            {"organization_name": "占位组织", "position": "占位新职位", "status": "active"}
        ],
    )
    await db_session.commit()

    await db_session.refresh(organization)
    await db_session.refresh(membership)
    assert organization.member_count == 1
    assert membership.position == "占位新职位"
    assert len(await _member_rows(db_session, organization)) == 1


async def test_profile_sync_status_change_keeps_count(db_session):
    """active → deceased（及反向）不改行数，member_count 不变。"""
    await _seed_project(db_session)
    organization = await _add_org(db_session, "占位组织", member_count=1)
    character = await _add_character(db_session, "占位角色甲")
    membership = await _add_member(db_session, organization, character.id, status="active")

    await character_profile_sync_service._apply_organizations(
        db=db_session,
        character=character,
        organizations=[
            {"organization_name": "占位组织", "position": "占位职位", "status": "deceased"}
        ],
    )
    await db_session.commit()

    await db_session.refresh(organization)
    await db_session.refresh(membership)
    assert organization.member_count == 1
    assert membership.status == "deceased"

    await character_profile_sync_service._apply_organizations(
        db=db_session,
        character=character,
        organizations=[
            {"organization_name": "占位组织", "position": "占位职位", "status": "active"}
        ],
    )
    await db_session.commit()

    await db_session.refresh(organization)
    await db_session.refresh(membership)
    assert organization.member_count == 1
    assert membership.status == "active"


async def test_profile_sync_keeps_manual_membership_and_count(db_session):
    """source='manual' 的既有成员不被覆盖，计数不变。"""
    await _seed_project(db_session)
    organization = await _add_org(db_session, "占位组织", member_count=1)
    character = await _add_character(db_session, "占位角色甲")
    membership = await _add_member(
        db_session,
        organization,
        character.id,
        position="占位用户职位",
        source="manual",
    )

    await character_profile_sync_service._apply_organizations(
        db=db_session,
        character=character,
        organizations=[
            {"organization_name": "占位组织", "position": "占位AI职位", "status": "deceased"}
        ],
    )
    await db_session.commit()

    await db_session.refresh(organization)
    await db_session.refresh(membership)
    assert organization.member_count == 1
    assert membership.position == "占位用户职位"
    assert membership.status == "active"
    assert len(await _member_rows(db_session, organization)) == 1


async def test_fix_organization_member_counts_matches_member_rows(db_session):
    """计数错误的数据被修正为成员行数；再次修复不再有变更。"""
    await _seed_project(db_session)
    organization = await _add_org(db_session, "占位组织", member_count=99)
    for index, status in enumerate(["active", "deceased"]):
        character = await _add_character(db_session, f"占位角色{index}")
        await _add_member(db_session, organization, character.id, status=status)

    fixed, checked = await fix_organization_member_counts(PROJECT_ID, db_session)

    assert (fixed, checked) == (1, 1)
    await db_session.refresh(organization)
    assert organization.member_count == 2

    # 幂等：再跑一遍没有需要修复的组织
    assert await fix_organization_member_counts(PROJECT_ID, db_session) == (0, 1)
    await db_session.refresh(organization)
    assert organization.member_count == 2
