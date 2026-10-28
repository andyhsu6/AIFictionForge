"""角色/组织档案回流测试（issue #174）。

钉死契约：
(a) 有新章节 + 正文提到该档案 → 档案字段被更新，水位（profile_synced_through_chapter）前进；
(b) 同一 through_chapter 再跑一次 → 零 AI 调用、零写入（幂等）；
(c) 用户手改过的字段不被覆盖，且计入 fields_preserved；
(d) 没有新章节的档案 → 不进入候选、不调用模型；
(e) ai.content_filtered → 不重试、记入 skipped、不抛异常；
(f) 同项目并发 sync → 第二次直接 no-op。

夹具与断言文本全部为中性占位（AGENTS.md 脱敏硬约束）。
"""
from __future__ import annotations

import asyncio
import json
import os
import uuid

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.core.errors import ApiError
from app.database import Base
from app.models.chapter import Chapter
from app.models.character import Character
from app.models.project import Project
from app.models.relationship import Organization, OrganizationMember
from app.services.character_profile_sync_service import (
    CharacterProfileSyncService,
    character_profile_sync_service,
)

PROJECT_ID = "proj-ps-1"
USER_ID = "user-ps-1"
OTHER_USER_ID = "user-ps-2"

pytestmark = pytest.mark.anyio


class FakeAIService:
    """记录调用次数的最小 AI 桩。"""

    def __init__(self, response=None, error=None, gate: asyncio.Event | None = None):
        self.response = response
        self.error = error
        self.gate = gate
        self.calls: list[dict] = []
        self.started = asyncio.Event()

    async def call_with_json_retry(self, *, prompt, **kwargs):
        self.calls.append({"prompt": prompt, **kwargs})
        self.started.set()
        if self.gate is not None:
            await self.gate.wait()
        if self.error is not None:
            raise self.error
        return self.response


def _ai_payload(name: str, **overrides) -> dict:
    fields = {
        "personality": overrides.get("personality"),
        "background": overrides.get("background"),
        "appearance": overrides.get("appearance"),
        "current_state": overrides.get("current_state"),
    }
    return {
        "characters": [
            {
                "name": name,
                "fields": fields,
                "organizations": overrides.get("organizations", []),
            }
        ]
    }


@pytest.fixture
async def db_session():
    """临时文件 SQLite（与 test_book_import_content_filter.py 同模式）。"""
    db_path = f"/tmp/test_character_profile_sync_{uuid.uuid4().hex}.db"
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


async def _seed(db, *, chapters: list[tuple[int, str]], markers: dict[str, int] | None = None):
    db.add(Project(id=PROJECT_ID, user_id=USER_ID, title="占位项目", genre="通用"))
    for name, marker in (markers or {}).items():
        db.add(
            Character(
                project_id=PROJECT_ID,
                name=name,
                personality="占位旧性格",
                profile_synced_through_chapter=marker,
            )
        )
    for number, content in chapters:
        db.add(
            Chapter(
                project_id=PROJECT_ID,
                chapter_number=number,
                title=f"占位章节标题{number}",
                content=content,
                word_count=len(content),
            )
        )
    await db.commit()


async def _character(db, name: str) -> Character:
    from sqlalchemy import select

    result = await db.execute(select(Character).where(Character.name == name))
    character = result.scalar_one()
    await db.refresh(character)
    return character


async def _project(db) -> Project:
    from sqlalchemy import select

    project = (await db.execute(select(Project).where(Project.id == PROJECT_ID))).scalar_one()
    await db.refresh(project)
    return project


class RoutingAIService:
    """按提示词类型分流的 AI 桩：世界设定 vs 角色档案。"""

    def __init__(self, *, char_response=None, world_response=None, world_error=None):
        self.char_response = char_response
        self.world_response = world_response
        self.world_error = world_error
        self.calls: list[str] = []

    async def call_with_json_retry(self, *, prompt, **kwargs):
        self.calls.append(prompt)
        if "【现有世界设定】" in prompt:
            if self.world_error is not None:
                raise self.world_error
            return self.world_response
        return self.char_response


def _world_payload(**fields) -> dict:
    return {
        "world": {
            "world_time_period": fields.get("world_time_period"),
            "world_location": fields.get("world_location"),
            "world_atmosphere": fields.get("world_atmosphere"),
            "world_rules": fields.get("world_rules"),
        }
    }


async def test_new_chapter_updates_profile_and_marker(db_session):
    await _seed(
        db_session,
        chapters=[(1, "第1章占位正文。角色甲登场。")],
        markers={"角色甲": 0},
    )
    ai = FakeAIService(_ai_payload("角色甲", personality="占位新性格", current_state="占位新处境"))

    stats = await character_profile_sync_service.sync(
        db=db_session, user_id=USER_ID, project_id=PROJECT_ID, through_chapter=1, ai_service=ai
    )

    character = await _character(db_session, "角色甲")
    assert len(ai.calls) == 1
    assert character.personality == "占位新性格"
    assert character.current_state == "占位新处境"
    assert character.profile_synced_through_chapter == 1
    assert json.loads(character.ai_profile_snapshot)["personality"] == "占位新性格"
    assert stats["characters_considered"] == 1
    assert stats["characters_updated"] == 1
    assert stats["fields_updated"] == 2
    assert stats["fields_preserved"] == 0
    assert stats["through_chapter"] == 1


async def test_same_through_chapter_is_idempotent(db_session):
    await _seed(
        db_session,
        chapters=[(1, "第1章占位正文。角色甲登场。")],
        markers={"角色甲": 0},
    )
    ai = FakeAIService(_ai_payload("角色甲", personality="占位新性格"))

    await character_profile_sync_service.sync(
        db=db_session, user_id=USER_ID, project_id=PROJECT_ID, through_chapter=1, ai_service=ai
    )
    second = await character_profile_sync_service.sync(
        db=db_session, user_id=USER_ID, project_id=PROJECT_ID, through_chapter=1, ai_service=ai
    )

    character = await _character(db_session, "角色甲")
    assert len(ai.calls) == 1  # 第二次没有再调用模型
    assert character.personality == "占位新性格"
    assert character.profile_synced_through_chapter == 1
    assert second["characters_considered"] == 0
    assert second["characters_updated"] == 0
    assert second["fields_updated"] == 0


async def test_user_edited_field_is_preserved(db_session):
    await _seed(db_session, chapters=[(2, "第2章占位正文。角色甲再次登场。")])
    character = Character(
        project_id=PROJECT_ID,
        name="角色甲",
        personality="占位用户手写性格",
        background="占位旧背景",
        profile_synced_through_chapter=1,
        ai_profile_snapshot=json.dumps(
            {"personality": "占位AI旧性格", "background": "占位旧背景"}, ensure_ascii=False
        ),
    )
    db_session.add(character)
    await db_session.commit()

    ai = FakeAIService(_ai_payload("角色甲", personality="占位AI新性格", background="占位新背景"))
    stats = await character_profile_sync_service.sync(
        db=db_session, user_id=USER_ID, project_id=PROJECT_ID, through_chapter=2, ai_service=ai
    )

    refreshed = await _character(db_session, "角色甲")
    assert refreshed.personality == "占位用户手写性格"  # 用户手改值保留
    assert refreshed.background == "占位新背景"  # 未被改过的字段照常覆盖
    snapshot = json.loads(refreshed.ai_profile_snapshot)
    assert snapshot["personality"] == "占位AI旧性格"  # 快照不因保留而漂移
    assert snapshot["background"] == "占位新背景"
    assert stats["fields_preserved"] == 1
    assert stats["fields_updated"] == 1


async def test_character_without_new_chapters_is_not_sent_to_model(db_session):
    await _seed(
        db_session,
        chapters=[(3, "第3章占位正文。角色甲登场。")],
        markers={"角色甲": 0, "角色乙": 3},
    )

    ai = FakeAIService(_ai_payload("角色甲", personality="占位新性格"))
    stats = await character_profile_sync_service.sync(
        db=db_session, user_id=USER_ID, project_id=PROJECT_ID, through_chapter=3, ai_service=ai
    )

    assert stats["characters_considered"] == 1
    assert len(ai.calls) == 1
    assert "角色乙" not in ai.calls[0]["prompt"]
    other = await _character(db_session, "角色乙")
    assert other.personality == "占位旧性格"
    assert other.profile_synced_through_chapter == 3


async def test_content_filtered_is_skipped_without_raising(db_session):
    await _seed(
        db_session,
        chapters=[(1, "第1章占位正文。角色甲登场。")],
        markers={"角色甲": 0},
    )
    ai = FakeAIService(error=ApiError(code="ai.content_filtered"))

    stats = await character_profile_sync_service.sync(
        db=db_session, user_id=USER_ID, project_id=PROJECT_ID, through_chapter=1, ai_service=ai
    )

    character = await _character(db_session, "角色甲")
    assert len(ai.calls) == 1  # 不重试
    assert character.personality == "占位旧性格"
    assert character.profile_synced_through_chapter == 0  # 水位不前进，下次还能补
    assert stats["characters_updated"] == 0
    assert len(stats["skipped"]) == 1
    assert stats["skipped"][0]["code"] == "ai.content_filtered"


async def test_concurrent_sync_for_same_project_is_noop(db_session):
    await _seed(
        db_session,
        chapters=[(1, "第1章占位正文。角色甲登场。")],
        markers={"角色甲": 0},
    )
    gate = asyncio.Event()
    ai = FakeAIService(_ai_payload("角色甲", personality="占位新性格"), gate=gate)

    first = asyncio.create_task(
        character_profile_sync_service.sync(
            db=db_session, user_id=USER_ID, project_id=PROJECT_ID, through_chapter=1, ai_service=ai
        )
    )
    await asyncio.wait_for(ai.started.wait(), timeout=5)

    second = await character_profile_sync_service.sync(
        db=db_session, user_id=OTHER_USER_ID, project_id=PROJECT_ID, through_chapter=1, ai_service=ai
    )

    gate.set()
    first_stats = await asyncio.wait_for(first, timeout=5)

    assert second["characters_considered"] == 0
    assert second["fields_updated"] == 0
    assert second["skipped"][0]["reason"] == "sync_in_progress"
    assert first_stats["fields_updated"] == 1
    assert len(ai.calls) == 1


async def test_organization_affiliation_is_created_but_manual_kept(db_session):
    await _seed(db_session, chapters=[(1, "第1章占位正文。角色甲加入占位组织。")], markers={"角色甲": 0})
    org_character = Character(project_id=PROJECT_ID, name="占位组织", is_organization=True)
    db_session.add(org_character)
    await db_session.commit()
    db_session.add(Organization(character_id=org_character.id, project_id=PROJECT_ID))
    await db_session.commit()

    ai = FakeAIService(
        _ai_payload(
            "角色甲",
            organizations=[{"organization_name": "占位组织", "position": "占位职位", "status": "active"}],
        )
    )
    stats = await character_profile_sync_service.sync(
        db=db_session, user_id=USER_ID, project_id=PROJECT_ID, through_chapter=1, ai_service=ai
    )

    from sqlalchemy import select

    memberships = list((await db_session.execute(select(OrganizationMember))).scalars())
    assert len(memberships) == 1
    assert memberships[0].position == "占位职位"
    assert memberships[0].source == "ai"
    assert stats["characters_updated"] == 1

    # 用户手工维护的成员关系（source=manual）不被覆盖
    memberships[0].source = "manual"
    memberships[0].position = "占位用户职位"
    await db_session.commit()

    ai2 = FakeAIService(
        _ai_payload(
            "角色甲",
            organizations=[{"organization_name": "占位组织", "position": "占位AI职位", "status": "active"}],
        )
    )
    db_session.add(Chapter(project_id=PROJECT_ID, chapter_number=2, title="占位章节标题2", content="第2章占位正文。角色甲仍在占位组织。"))
    await db_session.commit()
    await character_profile_sync_service.sync(
        db=db_session, user_id=USER_ID, project_id=PROJECT_ID, through_chapter=2, ai_service=ai2
    )

    await db_session.refresh(memberships[0])
    assert memberships[0].position == "占位用户职位"


async def test_world_new_chapter_updates_fields_and_marker(db_session):
    await _seed(db_session, chapters=[(1, "第1章占位正文。占位城邦与占位势力登场。")])
    ai = FakeAIService(
        _world_payload(
            world_time_period="占位纪元第七年，占位战争结束后",
            world_location="占位大陆东侧的占位城邦",
            world_atmosphere="占领与抵抗并存，街道上布满占位告示",
            world_rules="占位晶石可换取通行权，每人每月限三枚",
        )
    )

    stats = await character_profile_sync_service.sync_world(
        db=db_session, user_id=USER_ID, project_id=PROJECT_ID, through_chapter=1, ai_service=ai
    )

    project = await _project(db_session)
    assert len(ai.calls) == 1
    assert project.world_time_period == "占位纪元第七年，占位战争结束后"
    assert project.world_location == "占位大陆东侧的占位城邦"
    assert project.world_atmosphere == "占领与抵抗并存，街道上布满占位告示"
    assert project.world_rules == "占位晶石可换取通行权，每人每月限三枚"
    assert project.world_profile_synced_through_chapter == 1
    assert json.loads(project.ai_world_profile_snapshot)["world_rules"].startswith("占位晶石")
    assert stats["updated"] is True
    assert stats["fields_updated"] == 4
    assert stats["fields_preserved"] == 0
    assert stats["through_chapter"] == 1


async def test_world_same_through_chapter_is_idempotent(db_session):
    await _seed(db_session, chapters=[(1, "第1章占位正文。占位城邦登场。")])
    ai = FakeAIService(_world_payload(world_location="占位城邦"))

    await character_profile_sync_service.sync_world(
        db=db_session, user_id=USER_ID, project_id=PROJECT_ID, through_chapter=1, ai_service=ai
    )
    second = await character_profile_sync_service.sync_world(
        db=db_session, user_id=USER_ID, project_id=PROJECT_ID, through_chapter=1, ai_service=ai
    )

    project = await _project(db_session)
    assert len(ai.calls) == 1  # 第二次没有再调用模型
    assert project.world_location == "占位城邦"
    assert project.world_profile_synced_through_chapter == 1
    assert second["updated"] is False
    assert second["fields_updated"] == 0


async def test_world_user_edited_rules_is_preserved(db_session):
    await _seed(db_session, chapters=[(1, "第1章占位正文。占位城邦登场。")])
    project = await _project(db_session)
    project.world_rules = "占位用户手写规则"
    project.world_location = "占位旧地点"
    project.ai_world_profile_snapshot = json.dumps(
        {"world_rules": "占位AI旧规则", "world_location": "占位旧地点"}, ensure_ascii=False
    )
    await db_session.commit()

    ai = FakeAIService(
        _world_payload(
            world_time_period="占位新纪元",
            world_location="占位新地点",
            world_atmosphere="占位新氛围",
            world_rules="占位AI新规则",
        )
    )
    stats = await character_profile_sync_service.sync_world(
        db=db_session, user_id=USER_ID, project_id=PROJECT_ID, through_chapter=1, ai_service=ai
    )

    refreshed = await _project(db_session)
    assert refreshed.world_rules == "占位用户手写规则"  # 用户手改值保留
    assert refreshed.world_location == "占位新地点"  # 未被改过的字段照常覆盖
    snapshot = json.loads(refreshed.ai_world_profile_snapshot)
    assert snapshot["world_rules"] == "占位AI旧规则"  # 快照不因保留而漂移
    assert snapshot["world_location"] == "占位新地点"
    assert stats["fields_preserved"] == 1
    assert stats["fields_updated"] == 3


async def test_world_content_filtered_is_skipped_without_raising(db_session):
    await _seed(db_session, chapters=[(1, "第1章占位正文。占位城邦登场。")])
    ai = FakeAIService(error=ApiError(code="ai.content_filtered"))

    stats = await character_profile_sync_service.sync_world(
        db=db_session, user_id=USER_ID, project_id=PROJECT_ID, through_chapter=1, ai_service=ai
    )

    project = await _project(db_session)
    assert len(ai.calls) == 1  # 不重试
    assert project.world_location is None
    assert project.world_profile_synced_through_chapter == 0  # 水位不前进，下次还能补
    assert project.ai_world_profile_snapshot is None
    assert stats["updated"] is False
    assert len(stats["skipped"]) == 1
    assert stats["skipped"][0]["code"] == "ai.content_filtered"


async def test_world_failure_does_not_break_character_sync(db_session):
    await _seed(
        db_session,
        chapters=[(1, "第1章占位正文。角色甲在占位城邦登场。")],
        markers={"角色甲": 0},
    )
    ai = RoutingAIService(
        char_response=_ai_payload("角色甲", personality="占位新性格"),
        world_error=RuntimeError("世界设定桩失败"),
    )

    stats = await character_profile_sync_service.sync_all(
        db=db_session, user_id=USER_ID, project_id=PROJECT_ID, through_chapter=1, ai_service=ai
    )

    character = await _character(db_session, "角色甲")
    project = await _project(db_session)
    assert character.personality == "占位新性格"  # 角色档案照常更新
    assert stats["fields_updated"] == 1
    assert stats["characters_updated"] == 1
    assert stats["world"]["updated"] is False
    assert stats["world"]["skipped"][0]["reason"] == "world_sync_failed"
    assert project.world_location is None  # 世界设定保持原样
