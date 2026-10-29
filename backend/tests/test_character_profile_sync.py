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
from app.schemas.book_import import BookImportApplyRequest, ProjectSuggestion
from app.services import book_import_service as bis
from app.services.book_import_service import BookImportService, _BookImportTask
from app.services.prompt_service import PromptService
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
                # 模拟「上一次 AI 写入」的档案：有快照的字段才允许后续覆盖
                ai_profile_snapshot=json.dumps({"personality": "占位旧性格"}, ensure_ascii=False),
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


async def test_legacy_profile_value_is_preserved_by_default(db_session):
    """历史数据（无快照）+ 当前值非空 → 默认不覆盖；空字段照常填入。"""
    await _seed(db_session, chapters=[(1, "第1章占位正文。角色丙登场。")])
    db_session.add(
        Character(
            project_id=PROJECT_ID,
            name="角色丙",
            personality="占位历史手写性格",
            ai_profile_snapshot=None,
            profile_synced_through_chapter=0,
        )
    )
    await db_session.commit()

    ai = FakeAIService(_ai_payload("角色丙", personality="占位AI新性格", background="占位AI新背景"))
    stats = await character_profile_sync_service.sync(
        db=db_session, user_id=USER_ID, project_id=PROJECT_ID, through_chapter=1, ai_service=ai
    )

    character = await _character(db_session, "角色丙")
    assert character.personality == "占位历史手写性格"  # 默认保护历史值
    assert character.background == "占位AI新背景"  # 空字段允许填入
    assert json.loads(character.ai_profile_snapshot) == {"background": "占位AI新背景"}
    assert stats["fields_preserved"] == 1
    assert stats["fields_updated"] == 1


async def test_force_overwrites_legacy_profile_values(db_session):
    """同一份历史数据 + force=True → 整段覆盖并写入快照。"""
    await _seed(db_session, chapters=[(1, "第1章占位正文。角色丙登场。")])
    db_session.add(
        Character(
            project_id=PROJECT_ID,
            name="角色丙",
            personality="占位历史手写性格",
            ai_profile_snapshot=None,
            profile_synced_through_chapter=0,
        )
    )
    await db_session.commit()

    ai = FakeAIService(_ai_payload("角色丙", personality="占位AI新性格", background="占位AI新背景"))
    stats = await character_profile_sync_service.sync(
        db=db_session,
        user_id=USER_ID,
        project_id=PROJECT_ID,
        through_chapter=1,
        ai_service=ai,
        force=True,
    )

    character = await _character(db_session, "角色丙")
    assert character.personality == "占位AI新性格"
    assert character.background == "占位AI新背景"
    snapshot = json.loads(character.ai_profile_snapshot)
    assert snapshot["personality"] == "占位AI新性格"
    assert snapshot["background"] == "占位AI新背景"
    assert stats["fields_preserved"] == 0
    assert stats["fields_updated"] == 2


async def test_legacy_world_empty_field_is_filled_by_default(db_session):
    """世界设定同样默认保护历史值，但空字段默认会被填入。"""
    await _seed(db_session, chapters=[(1, "第1章占位正文。占位城邦登场。")])
    project = await _project(db_session)
    project.world_location = "占位历史地点"
    project.ai_world_profile_snapshot = None
    await db_session.commit()

    ai = FakeAIService(
        _world_payload(world_location="占位AI新地点", world_rules="占位AI新规则")
    )
    stats = await character_profile_sync_service.sync_world(
        db=db_session, user_id=USER_ID, project_id=PROJECT_ID, through_chapter=1, ai_service=ai
    )

    refreshed = await _project(db_session)
    assert refreshed.world_location == "占位历史地点"  # 历史值保留
    assert refreshed.world_rules == "占位AI新规则"  # 空字段填入
    assert json.loads(refreshed.ai_world_profile_snapshot) == {"world_rules": "占位AI新规则"}
    assert stats["fields_preserved"] == 1
    assert stats["fields_updated"] == 1


def _make_import_task(task_id: str) -> _BookImportTask:
    task = _BookImportTask(
        task_id=task_id,
        user_id=USER_ID,
        filename="placeholder.txt",
        project_id=None,
        create_new_project=True,
        import_mode="append",
        status="completed",
    )
    return task


def _patch_import_steps(monkeypatch, svc, *, prepare_project):
    async def fake_import_outlines(**kwargs):
        return {}

    async def fake_import_chapters(**kwargs):
        return 3, 1000

    async def fake_generate_world(**kwargs):
        return 1

    async def fake_generate_careers(**kwargs):
        return 2

    async def fake_generate_characters(**kwargs):
        return 3

    async def fake_extract_relationships(**kwargs):
        return {
            "extracted_relationships": 1,
            "created_types": 1,
            "created_characters": 1,
            "skipped_chapters": [],
        }

    monkeypatch.setattr(svc, "_prepare_project", prepare_project)
    monkeypatch.setattr(svc, "_import_outlines", fake_import_outlines)
    monkeypatch.setattr(svc, "_import_chapters", fake_import_chapters)
    monkeypatch.setattr(svc, "_generate_world_building_from_project", fake_generate_world)
    monkeypatch.setattr(svc, "_generate_career_system_from_project", fake_generate_careers)
    monkeypatch.setattr(
        svc, "_generate_characters_and_organizations_from_project", fake_generate_characters
    )
    monkeypatch.setattr(svc, "_extract_relationships_from_chapters", fake_extract_relationships)


async def test_import_success_schedules_profile_sync(monkeypatch):
    from unittest.mock import AsyncMock

    scheduled: list[tuple[str, str]] = []
    monkeypatch.setattr(
        bis, "_schedule_profile_sync", lambda user_id, project_id: scheduled.append((user_id, project_id))
    )

    svc = BookImportService()
    task = _make_import_task("t-hook-ok")
    svc._tasks[task.task_id] = task

    async def fake_prepare_project(**kwargs):
        from types import SimpleNamespace

        return SimpleNamespace(id="p-hook", character_count=5, current_words=0)

    _patch_import_steps(monkeypatch, svc, prepare_project=fake_prepare_project)

    result = await svc.apply_import_stream(
        task_id=task.task_id,
        user_id=USER_ID,
        payload=BookImportApplyRequest(
            project_suggestion=ProjectSuggestion(title="占位项目"),
            chapters=[],
        ),
        db=AsyncMock(),
        progress_callback=None,
        ai_service=None,
    )

    assert result.success is True
    assert scheduled == [(USER_ID, "p-hook")]


async def test_import_failure_does_not_schedule_profile_sync(monkeypatch):
    from unittest.mock import AsyncMock

    scheduled: list[tuple[str, str]] = []
    monkeypatch.setattr(
        bis, "_schedule_profile_sync", lambda user_id, project_id: scheduled.append((user_id, project_id))
    )

    svc = BookImportService()
    task = _make_import_task("t-hook-fail")
    svc._tasks[task.task_id] = task

    async def failing_prepare_project(**kwargs):
        raise RuntimeError("占位落库失败")

    _patch_import_steps(monkeypatch, svc, prepare_project=failing_prepare_project)

    with pytest.raises(ApiError):
        await svc.apply_import_stream(
            task_id=task.task_id,
            user_id=USER_ID,
            payload=BookImportApplyRequest(
                project_suggestion=ProjectSuggestion(title="占位项目"),
                chapters=[],
            ),
            db=AsyncMock(),
            progress_callback=None,
            ai_service=None,
        )

    assert scheduled == []


WORLD_TEMPLATE = "占位模板 {title}|{genre}|{theme}|{description}|{full_book_context}"
CHARACTER_TEMPLATE = (
    "数量:{count}\n{time_period}|{location}|{atmosphere}|{rules}|{theme}|{genre}\n要求:{requirements}"
)


def _patch_import_prompts(monkeypatch):
    """导入侧模板/语言解析走桩：只关心「落库时是否登记 AI 快照」。"""

    async def fake_get_template(name, user_id=None, db=None):
        return CHARACTER_TEMPLATE if name.startswith("CHARACTER") else WORLD_TEMPLATE

    async def fake_generation_language(db, user_id, content_language=None):
        return "zh"

    monkeypatch.setattr(PromptService, "get_template", fake_get_template)
    monkeypatch.setattr(bis, "resolve_user_generation_language", fake_generation_language)


async def _run_import_world_generation(monkeypatch, db, project, world: dict):
    """真实跑一遍导入的世界设定生成（AI 桩返回给定 world 数据）。"""
    _patch_import_prompts(monkeypatch)
    ai = FakeAIService(world)
    updated = await BookImportService()._generate_world_building_from_project(
        db=db,
        user_id=USER_ID,
        project=project,
        ai_service=ai,
        content_language="zh",
    )
    await db.commit()
    return updated, ai


async def test_import_world_generation_seeds_ai_snapshot(db_session, monkeypatch):
    await _seed(db_session, chapters=[(1, "第1章占位正文。占位城邦登场。")])
    project = await _project(db_session)

    world = {
        "time_period": "占位纪元第七年",
        "location": "占位大陆东侧",
        "atmosphere": "占领与抵抗并存",
        "rules": "占位晶石换取通行权，每人每月限三枚",
    }
    updated, ai = await _run_import_world_generation(monkeypatch, db_session, project, world)

    refreshed = await _project(db_session)
    snapshot = json.loads(refreshed.ai_world_profile_snapshot)
    assert updated == 1  # 返回值是「是否写入过」的标记（服务原语义），非字段计数
    assert len(ai.calls) == 1
    assert set(snapshot) == {
        "world_time_period",
        "world_location",
        "world_atmosphere",
        "world_rules",
    }
    # 快照值与落库值逐字一致
    for field, value in snapshot.items():
        assert getattr(refreshed, field) == value
    assert refreshed.world_rules == world["rules"]
    # 水位保持在 0：首轮同步仍会补看全书
    assert refreshed.world_profile_synced_through_chapter == 0


async def test_import_character_generation_seeds_ai_snapshot(db_session, monkeypatch):
    await _seed(db_session, chapters=[])
    project = await _project(db_session)
    _patch_import_prompts(monkeypatch)

    fields = {
        "personality": "占位角色甲的性格：沉着克制，遇事先观察再表态，不轻易承诺。",
        "background": "占位角色甲的背景：在占位城镇长大，靠占位手艺谋生，经历过占位变故。",
        "appearance": "占位角色甲的外貌：中等身材，常穿占位式样的深色外衣，随身带占位物件。",
        "current_state": "占位角色甲的处境：暂住占位城镇，等待占位事件的结果，心里没底。",
    }
    ai = FakeAIService([{"name": "角色甲", "role_type": "supporting", **fields}])

    created = await BookImportService()._generate_characters_and_organizations_from_project(
        db=db_session,
        user_id=USER_ID,
        project=project,
        count=3,
        ai_service=ai,
        content_language="zh",
    )
    await db_session.commit()

    character = await _character(db_session, "角色甲")
    snapshot = json.loads(character.ai_profile_snapshot)
    assert created == 1
    assert set(snapshot) == {"personality", "background", "appearance", "current_state"}
    for field in snapshot:
        assert snapshot[field] == getattr(character, field) == fields[field]
    assert character.profile_synced_through_chapter == 0


async def test_world_profile_can_evolve_after_import_snapshot(db_session, monkeypatch):
    """导入登记快照后，同步不再把它判为历史值：字段可被模型新值覆盖。"""
    await _seed(db_session, chapters=[(1, "第1章占位正文。占位城邦登场。")])
    project = await _project(db_session)
    await _run_import_world_generation(
        monkeypatch,
        db_session,
        project,
        {
            "time_period": "占位纪元第七年",
            "location": "占位大陆东侧",
            "atmosphere": "占领与抵抗并存",
            "rules": "占位晶石换取通行权",
        },
    )

    ai = FakeAIService(
        _world_payload(
            world_time_period="占位纪元第九年",
            world_location="占位大陆西侧",
            world_atmosphere="停战后的清算气氛",
            world_rules="占位晶石已废止，改用占位契书",
        )
    )
    stats = await character_profile_sync_service.sync_world(
        db=db_session, user_id=USER_ID, project_id=PROJECT_ID, through_chapter=1, ai_service=ai
    )

    refreshed = await _project(db_session)
    assert refreshed.world_time_period == "占位纪元第九年"
    assert refreshed.world_location == "占位大陆西侧"
    assert refreshed.world_rules == "占位晶石已废止，改用占位契书"
    assert stats["fields_preserved"] == 0  # 不再被判为历史值
    assert stats["fields_updated"] == 4
    assert refreshed.world_profile_synced_through_chapter == 1


async def test_narration_personality_is_rejected_not_written(db_session):
    """issue #188：剧情复述不得写进性格字段，同批的合法字段仍要落库。"""
    await _seed(
        db_session,
        chapters=[(1, "第1章占位正文。角色甲登场。")],
        markers={"角色甲": 0},
    )
    narration = (
        "因为她和丈夫不仅是夫妻，还是师兄妹，她的骑术和刀法都不在角色乙之下，"
        "「占位双刀」的名头她可是占了一半的。"
    )
    ai = FakeAIService(
        _ai_payload("角色甲", personality=narration, background="占位出身：山门弟子，行侠多年。")
    )

    stats = await character_profile_sync_service.sync(
        db=db_session, user_id=USER_ID, project_id=PROJECT_ID, through_chapter=1, ai_service=ai
    )
    character = await _character(db_session, "角色甲")

    assert character.personality == "占位旧性格", "疑似剧情复述的内容不得覆盖已有性格"
    assert character.background == "占位出身：山门弟子，行侠多年。"
    assert stats["fields_rejected"] == 1
    assert stats["fields_updated"] == 1
    snapshot = json.loads(character.ai_profile_snapshot)
    assert "background" in snapshot
    assert snapshot.get("personality") == "占位旧性格", "被拒内容不得进入快照"


class _RotatingAIService:
    """每次调用返回不同性格，用来区分「按批计数」与「按人计数」。"""

    def __init__(self, name: str):
        self.name = name
        self.calls = 0

    async def call_with_json_retry(self, *, prompt, **kwargs):
        self.calls += 1
        return _ai_payload(self.name, personality=f"占位性格第{self.calls}版")


async def test_updated_counter_is_per_character_not_per_chunk(db_session, monkeypatch):
    """issue #191：同一角色出现在多个章节批次里只应计一次。"""
    import app.services.character_profile_sync_service as mod

    monkeypatch.setattr(mod, "_PROMPT_CHAR_BUDGET", 5)  # 每章单独成批

    await _seed(
        db_session,
        chapters=[(1, "占位正文甲，角色甲登场。"), (2, "占位正文乙，角色甲再次出场。")],
        markers={"角色甲": 0},
    )
    ai = _RotatingAIService("角色甲")

    stats = await character_profile_sync_service.sync(
        db=db_session, user_id=USER_ID, project_id=PROJECT_ID, through_chapter=2, ai_service=ai
    )

    assert ai.calls >= 2, "两章应各自成批"
    assert stats["characters_updated"] == 1, "同一角色多批次只算一个人"
    assert stats["fields_updated"] >= 2
