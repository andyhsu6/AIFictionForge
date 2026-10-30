"""档案重算（预览 dry-run / apply 回放 / 预估）服务层测试（M1）。

钉死契约：
(a) dry-run 零写入，差异只进 collector；
(b) 计划回放与真实写入逐字段一致（含 ai_profile_snapshot 与 profile_facts）；
(c) bypass_watermark 能绕过已同步水位，预览与 apply 都不推进水位；
(d) fingerprint 不一致的实体在 apply 时跳过并计入 stale；
(e) 组织 personality 恒不出现在计划里，apply 后仍为空；
(f) 手改字段标记 preserved 且 apply 不改；
(g) estimate.calls_min 与真实模型调用次数相等。

夹具与断言文本全部为中性占位（AGENTS.md 脱敏硬约束）。
"""
from __future__ import annotations

import asyncio
import json
import os
import uuid
from datetime import datetime

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
    ProfileDiffCollector,
    character_profile_sync_service,
    profile_fingerprint,
)

PROJECT_ID = "proj-pr-1"
USER_ID = "user-pr-1"

QUOTE = "占位正文甲。占位正文乙"

pytestmark = pytest.mark.anyio


class FakeAIService:
    """记录调用次数的最小 AI 桩。"""

    def __init__(self, response=None):
        self.response = response
        self.calls: list[dict] = []

    async def call_with_json_retry(self, *, prompt, **kwargs):
        self.calls.append({"prompt": prompt, **kwargs})
        return self.response


def _fields_payload(name: str, **fields) -> dict:
    return {
        "characters": [
            {
                "name": name,
                "fields": {
                    "personality": fields.get("personality"),
                    "background": fields.get("background"),
                    "appearance": fields.get("appearance"),
                    "current_state": fields.get("current_state"),
                },
                "organizations": [],
            }
        ]
    }


@pytest.fixture
async def db_session():
    db_path = f"/tmp/test_character_profile_refresh_plan_{uuid.uuid4().hex}.db"
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
    CharacterProfileSyncService._in_progress.clear()
    CharacterProfileSyncService._world_in_progress.clear()
    yield
    CharacterProfileSyncService._in_progress.clear()
    CharacterProfileSyncService._world_in_progress.clear()


async def _seed(db, *, chapters, characters) -> None:
    db.add(Project(id=PROJECT_ID, user_id=USER_ID, title="占位项目", genre="通用"))
    for spec in characters:
        db.add(Character(project_id=PROJECT_ID, **spec))
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
    character = (
        await db.execute(select(Character).where(Character.name == name))
    ).scalar_one()
    await db.refresh(character)
    return character


async def _preview(db, ai, **kwargs) -> tuple[dict, ProfileDiffCollector, dict]:
    """跑一次 dry-run，返回 (plan, collector, stats)。"""
    collector = ProfileDiffCollector()
    stats = await character_profile_sync_service.sync(
        db=db,
        user_id=USER_ID,
        project_id=PROJECT_ID,
        through_chapter=1,
        ai_service=ai,
        dry_run=True,
        collector=collector,
        **kwargs,
    )
    return {"entities": list(collector.entities.values())}, collector, stats


def _row(entry: dict, field: str) -> dict:
    rows = [row for row in entry["fields"] if row["field"] == field]
    assert len(rows) == 1, f"{entry['name']}.{field} 应恰好一行，实际 {rows}"
    return rows[0]


# --- ① dry-run 零写入 -------------------------------------------------------


async def test_dry_run_writes_nothing_and_collects_diff(db_session):
    old_facts = [
        {
            "field": "background",
            "claim": "占位旧事实",
            "quote": QUOTE,
            "chapter_number": 1,
            "inferred": False,
        }
    ]
    await _seed(
        db_session,
        chapters=[(1, f"角色甲登场。{QUOTE}。占位正文丙。")],
        characters=[
            {
                "name": "角色甲",
                "personality": "占位旧性格",
                "ai_profile_snapshot": json.dumps({"personality": "占位旧性格"}, ensure_ascii=False),
                "profile_facts": json.dumps(old_facts, ensure_ascii=False),
                "profile_facts_updated_at": datetime(2026, 1, 1, 0, 0, 0),
                "profile_synced_through_chapter": 0,
            }
        ],
    )
    character = await _character(db_session, "角色甲")
    character_id = character.id

    db_session.add(Character(project_id=PROJECT_ID, name="占位组织", is_organization=True))
    await db_session.commit()
    organization_character = await _character(db_session, "占位组织")
    organization_row = Organization(
        character_id=organization_character.id, project_id=PROJECT_ID
    )
    db_session.add(organization_row)
    await db_session.commit()
    await db_session.refresh(organization_row)

    def _snapshot() -> dict:
        return {
            "personality": character.personality,
            "background": character.background,
            "appearance": character.appearance,
            "current_state": character.current_state,
            "profile_facts": character.profile_facts,
            "profile_facts_updated_at": character.profile_facts_updated_at,
            "ai_profile_snapshot": character.ai_profile_snapshot,
            "profile_synced_through_chapter": character.profile_synced_through_chapter,
            "updated_at": character.updated_at,
        }

    before = _snapshot()
    ai = FakeAIService(
        {
            "characters": [
                {
                    "name": "角色甲",
                    "profile_facts": [
                        {
                            "field": "personality",
                            "claim": "占位新特质",
                            "quote": QUOTE,
                            "chapter_number": 1,
                            "inferred": False,
                        }
                    ],
                    "organizations": [
                        {
                            "organization_name": "占位组织",
                            "position": "占位职位",
                            "status": "active",
                        }
                    ],
                }
            ]
        }
    )

    plan, collector, stats = await _preview(db_session, ai)

    await db_session.refresh(character)
    assert _snapshot() == before, "dry-run 不得写入任何列"
    assert stats["fields_updated"] == 1, "统计照常累积（只用于计划汇总）"

    memberships = list((await db_session.execute(select(OrganizationMember))).scalars())
    assert memberships == [], "dry-run 不得创建组织成员关系"
    await db_session.refresh(organization_row)
    assert organization_row.member_count == 0

    entry = collector.entities[character_id]
    assert entry["kind"] == "character"
    row = _row(entry, "personality")
    assert row["old"] == "占位旧性格"
    assert row["new"] == "占位新特质"
    assert row["action"] == "change"
    assert entry["facts_delta"] == [
        {
            "field": "personality",
            "claim": "占位新特质",
            "quote": QUOTE,
            "chapter_number": 1,
            "inferred": False,
        }
    ]
    assert entry["stats"]["facts_accepted"] == 1
    assert plan["entities"] and len(plan["entities"]) == 1


# --- ② 计划回放 == 真实写入 -------------------------------------------------


async def test_plan_replay_matches_real_write(db_session):
    content = f"角色甲登场。角色乙登场。角色丙登场。角色丁登场。{QUOTE}。"
    await _seed(
        db_session,
        chapters=[(1, content)],
        characters=[
            {"name": "角色甲"},
            {
                "name": "角色乙",
                "personality": "占位旧乙",
                "ai_profile_snapshot": json.dumps({"personality": "占位旧乙"}, ensure_ascii=False),
            },
            {
                "name": "角色丙",
                "personality": "占位手改丙",
                "ai_profile_snapshot": json.dumps({"personality": "占位旧丙"}, ensure_ascii=False),
            },
            {"name": "角色丁"},
        ],
    )
    payload = {
        "characters": [
            {"name": "角色甲", "fields": {"personality": "占位新甲"}, "organizations": []},
            {"name": "角色乙", "fields": {"personality": "占位新乙"}, "organizations": []},
            {"name": "角色丙", "fields": {"personality": "占位新丙"}, "organizations": []},
            {
                "name": "角色丁",
                "profile_facts": [
                    {
                        "field": "background",
                        "claim": "占位出身背景",
                        "quote": QUOTE,
                        "chapter_number": 1,
                        "inferred": False,
                    }
                ],
                "organizations": [],
            },
        ]
    }

    plan, collector, _stats = await _preview(db_session, FakeAIService(payload))

    actions = {
        _row(collector.entities[key], "personality")["action"]
        for key in collector.entities
        if any(row["field"] == "personality" for row in collector.entities[key]["fields"])
    }
    assert actions == {"add", "change", "preserved"}

    await character_profile_sync_service.sync(
        db=db_session,
        user_id=USER_ID,
        project_id=PROJECT_ID,
        through_chapter=1,
        ai_service=FakeAIService(payload),
    )

    for entry in plan["entities"]:
        character = await _character(db_session, entry["name"])
        for row in entry["fields"]:
            actual = getattr(character, row["field"])
            if row["action"] in ("add", "change"):
                assert actual == row["new"]
            elif row["action"] == "preserved":
                assert actual == row["old"]
            else:  # same
                assert actual == row["old"] == row["new"]

    snapshot = {
        name: json.loads((await _character(db_session, name)).ai_profile_snapshot or "{}")
        for name in ("角色甲", "角色乙", "角色丙", "角色丁")
    }
    assert snapshot["角色甲"]["personality"] == "占位新甲"
    assert snapshot["角色乙"]["personality"] == "占位新乙"
    assert snapshot["角色丙"] == {"personality": "占位旧丙"}, "preserved 字段不得刷新快照"
    assert snapshot["角色丁"]["background"] == "占位出身背景"

    character_d = await _character(db_session, "角色丁")
    assert [fact["claim"] for fact in json.loads(character_d.profile_facts)] == ["占位出身背景"]
    assert character_d.profile_facts_updated_at is not None


# --- ③ bypass_watermark ----------------------------------------------------


async def test_bypass_watermark_reaches_synced_project_without_moving_marker(db_session):
    await _seed(
        db_session,
        chapters=[(1, "角色甲登场。占位正文甲。")],
        characters=[
            {
                "name": "角色甲",
                "personality": "占位旧性格",
                "ai_profile_snapshot": json.dumps({"personality": "占位旧性格"}, ensure_ascii=False),
                "profile_synced_through_chapter": 5,
            },
            # 正文从未出现其名：只有它才算 no_mention
            {"name": "角色乙"},
        ],
    )
    incremental = await character_profile_sync_service.estimate_refresh(
        db=db_session, project_id=PROJECT_ID, mode="incremental"
    )
    full = await character_profile_sync_service.estimate_refresh(
        db=db_session, project_id=PROJECT_ID, mode="full"
    )
    assert incremental["candidates"] == 0
    assert incremental["entities_total"] == 2
    assert incremental["no_mention"] == 1, "水位已达标的角色甲不算「正文没提及」"
    assert full["candidates"] == 1
    assert full["no_mention"] == 1
    assert full["chunks"] == 1
    assert full["calls_min"] == 1
    assert full["calls_max"] == 3

    ai = FakeAIService(_fields_payload("角色甲", personality="占位新性格"))
    plan, collector, _stats = await _preview(db_session, ai, bypass_watermark=True)
    assert len(collector.entities) == 1

    character = await _character(db_session, "角色甲")
    assert character.personality == "占位旧性格", "预览不写库"
    assert character.profile_synced_through_chapter == 5, "预览不推进水位"

    result = await character_profile_sync_service.apply_plan(
        db=db_session, project_id=PROJECT_ID, plan=plan
    )
    assert result == {"applied": 1, "fields_written": 1, "facts_merged": 0, "stale": []}

    character = await _character(db_session, "角色甲")
    assert character.personality == "占位新性格"
    assert character.profile_synced_through_chapter == 5, "全量重算不推进水位"


# --- ④ stale --------------------------------------------------------------


async def test_apply_skips_entity_changed_after_preview(db_session):
    await _seed(
        db_session,
        chapters=[(1, "角色甲登场。角色乙登场。占位正文甲。")],
        characters=[
            {
                "name": "角色甲",
                "personality": "占位旧甲",
                "ai_profile_snapshot": json.dumps({"personality": "占位旧甲"}, ensure_ascii=False),
            },
            {
                "name": "角色乙",
                "personality": "占位旧乙",
                "ai_profile_snapshot": json.dumps({"personality": "占位旧乙"}, ensure_ascii=False),
            },
        ],
    )
    payload = {
        "characters": [
            {"name": "角色甲", "fields": {"personality": "占位新甲"}, "organizations": []},
            {"name": "角色乙", "fields": {"personality": "占位新乙"}, "organizations": []},
        ]
    }
    plan, _collector, _stats = await _preview(db_session, FakeAIService(payload))

    stale_target = await _character(db_session, "角色甲")
    stale_target.current_state = "占位手改状态"
    await db_session.commit()

    result = await character_profile_sync_service.apply_plan(
        db=db_session, project_id=PROJECT_ID, plan=plan
    )

    assert result["stale"] == [{"id": stale_target.id, "name": "角色甲"}]
    assert result["applied"] == 1
    assert result["fields_written"] == 1

    skipped = await _character(db_session, "角色甲")
    assert skipped.personality == "占位旧甲", "stale 实体不得写入任何内容"
    applied = await _character(db_session, "角色乙")
    assert applied.personality == "占位新乙"


# --- ⑤ 组织 personality ---------------------------------------------------


async def test_organization_personality_never_planned_and_stays_empty(db_session):
    await _seed(
        db_session,
        chapters=[(1, "占位组织登场。占位正文甲。")],
        characters=[{"name": "占位组织", "is_organization": True}],
    )
    payload = _fields_payload(
        "占位组织", personality="占位组织性格", background="占位组织背景"
    )
    plan, collector, _stats = await _preview(db_session, FakeAIService(payload))

    entry = next(iter(collector.entities.values()))
    assert entry["kind"] == "organization"
    assert [row["field"] for row in entry["fields"]] == ["background"]
    assert _row(entry, "background")["action"] == "add"

    await character_profile_sync_service.apply_plan(
        db=db_session, project_id=PROJECT_ID, plan=plan
    )

    organization = await _character(db_session, "占位组织")
    assert organization.personality is None
    assert organization.background == "占位组织背景"
    assert "personality" not in json.loads(organization.ai_profile_snapshot or "{}")


# --- ⑥ 手改字段 preserved -------------------------------------------------


async def test_manual_edit_is_planned_as_preserved_and_not_written(db_session):
    await _seed(
        db_session,
        chapters=[(1, "角色甲登场。占位正文甲。")],
        characters=[
            {
                "name": "角色甲",
                "personality": "占位手改",
                "ai_profile_snapshot": json.dumps({"personality": "占位旧"}, ensure_ascii=False),
            }
        ],
    )
    plan, collector, _stats = await _preview(
        db_session, FakeAIService(_fields_payload("角色甲", personality="占位新性格"))
    )

    entry = next(iter(collector.entities.values()))
    row = _row(entry, "personality")
    assert row == {
        "field": "personality",
        "old": "占位手改",
        "new": "占位新性格",
        "action": "preserved",
    }

    result = await character_profile_sync_service.apply_plan(
        db=db_session, project_id=PROJECT_ID, plan=plan
    )
    assert result["fields_written"] == 0

    character = await _character(db_session, "角色甲")
    assert character.personality == "占位手改"
    assert json.loads(character.ai_profile_snapshot) == {"personality": "占位旧"}


# --- ①b dry-run 抑制点（不依赖 rollback 掩盖） ------------------------------


async def test_dry_run_suppresses_writes_at_chunk_level(db_session):
    """直接调 `_sync_chunk`（不 commit、不 rollback），断言 ORM 状态本身没被改。

    上面整条 preview 的零写入断言会被结尾的 `db.rollback()` 掩盖，这里换成
    「连脏标记都没有」来钉死每个抑制点。
    """
    await _seed(
        db_session,
        chapters=[(1, f"角色甲登场。占位组织。{QUOTE}。")],
        characters=[
            {
                "name": "角色甲",
                "personality": "占位旧性格",
                "ai_profile_snapshot": json.dumps({"personality": "占位旧性格"}, ensure_ascii=False),
                "profile_synced_through_chapter": 0,
            },
            {"name": "占位组织", "is_organization": True},
        ],
    )
    organization_character = await _character(db_session, "占位组织")
    db_session.add(
        Organization(character_id=organization_character.id, project_id=PROJECT_ID)
    )
    await db_session.commit()

    character = await _character(db_session, "角色甲")
    chapter = (
        await db_session.execute(
            select(Chapter).where(Chapter.chapter_number == 1)
        )
    ).scalar_one()
    collector = ProfileDiffCollector()
    stats = {
        "fields_updated": 0,
        "fields_preserved": 0,
        "fields_rejected": 0,
        "skipped": [],
    }
    ai = FakeAIService(
        {
            "characters": [
                {
                    "name": "角色甲",
                    "profile_facts": [
                        {
                            "field": "personality",
                            "claim": "占位新特质",
                            "quote": QUOTE,
                            "chapter_number": 1,
                            "inferred": False,
                        }
                    ],
                    "organizations": [
                        {
                            "organization_name": "占位组织",
                            "position": "占位职位",
                            "status": "active",
                        }
                    ],
                }
            ]
        }
    )

    await character_profile_sync_service._sync_chunk(
        db=db_session,
        chunk=[(character, [chapter])],
        ai_service=ai,
        stats=stats,
        dry_run=True,
        collector=collector,
    )

    assert character.personality == "占位旧性格", "dry-run 不得 setattr"
    assert json.loads(character.ai_profile_snapshot) == {"personality": "占位旧性格"}
    assert character.profile_facts is None, "dry-run 不得写 profile_facts"
    assert character.profile_facts_updated_at is None
    assert character.profile_synced_through_chapter == 0, "dry-run 不得推进水位"
    assert db_session.is_modified(character) is False, "dry-run 不得留下任何脏标记"

    memberships = list((await db_session.execute(select(OrganizationMember))).scalars())
    assert memberships == [], "dry-run 不得创建组织成员关系"
    assert stats["fields_updated"] == 1, "统计照常累积"
    assert _row(collector.entities[character.id], "personality")["action"] == "change"
    assert collector.entities[character.id]["facts_delta"] is not None


# --- ⑧ 同值 same ----------------------------------------------------------


async def test_unchanged_value_is_planned_as_same_and_stays_idempotent(db_session):
    await _seed(
        db_session,
        chapters=[(1, "角色甲登场。占位正文甲。")],
        characters=[
            {
                "name": "角色甲",
                "personality": "占位同值",
                "ai_profile_snapshot": json.dumps({"personality": "占位同值"}, ensure_ascii=False),
            }
        ],
    )
    plan, collector, _stats = await _preview(
        db_session, FakeAIService(_fields_payload("角色甲", personality="占位同值"))
    )

    row = _row(next(iter(collector.entities.values())), "personality")
    assert row["action"] == "same"
    assert row["old"] == row["new"] == "占位同值"

    result = await character_profile_sync_service.apply_plan(
        db=db_session, project_id=PROJECT_ID, plan=plan
    )
    assert result == {"applied": 1, "fields_written": 0, "facts_merged": 0, "stale": []}

    character = await _character(db_session, "角色甲")
    assert character.personality == "占位同值"
    assert json.loads(character.ai_profile_snapshot) == {"personality": "占位同值"}


# --- ⑨ 项目信号与忙闲 ------------------------------------------------------


class _BusyProbeAIService(FakeAIService):
    """在模型调用当下采样 is_project_busy，用于钉死防重入语义。"""

    def __init__(self, response):
        super().__init__(response)
        self.busy_during_call: list[bool] = []

    async def call_with_json_retry(self, *, prompt, **kwargs):
        self.busy_during_call.append(
            CharacterProfileSyncService.is_project_busy(PROJECT_ID)
        )
        return await super().call_with_json_retry(prompt=prompt, **kwargs)


async def test_project_signal_and_busy_probe(db_session):
    await _seed(
        db_session,
        chapters=[(1, "角色甲登场。占位正文甲。")],
        characters=[{"name": "角色甲"}],
    )
    db_session.add(
        Chapter(project_id=PROJECT_ID, chapter_number=2, title="占位空章", content="")
    )
    await db_session.commit()

    signal = await character_profile_sync_service.project_signal(db_session, PROJECT_ID)
    chapter = (
        await db_session.execute(select(Chapter).where(Chapter.chapter_number == 1))
    ).scalar_one()
    await db_session.refresh(chapter)
    assert signal["chapters_with_content"] == 1, "空正文章节不计入信号"
    assert signal["chapters_max_updated_at"] == chapter.updated_at.isoformat()

    assert CharacterProfileSyncService.is_project_busy(PROJECT_ID) is False
    ai = _BusyProbeAIService(_fields_payload("角色甲", personality="占位新性格"))
    await character_profile_sync_service.sync(
        db=db_session,
        user_id=USER_ID,
        project_id=PROJECT_ID,
        through_chapter=1,
        ai_service=ai,
    )
    assert ai.busy_during_call == [True], "同步进行中必须报告为忙"
    assert CharacterProfileSyncService.is_project_busy(PROJECT_ID) is False


# --- ⑩ apply 与真实写入口径一致 / 忙则零写入 -------------------------------


def _manual_plan(character, *, fields: list[dict], facts_delta=None) -> dict:
    return {
        "entities": [
            {
                "id": character.id,
                "name": character.name,
                "kind": "organization" if character.is_organization else "character",
                "fingerprint": profile_fingerprint(character),
                "fields": fields,
                "facts_delta": facts_delta,
                "stats": {},
            }
        ]
    }


async def test_apply_merges_facts_even_when_all_rows_rejected(db_session):
    """与 `_apply_entry` 的真实写入对齐：facts 非空就合并，不看字段动作。"""
    await _seed(
        db_session,
        chapters=[(1, f"角色甲登场。{QUOTE}。")],
        characters=[{"name": "角色甲", "personality": "占位旧性格"}],
    )
    character = await _character(db_session, "角色甲")
    plan = _manual_plan(
        character,
        fields=[
            {
                "field": "personality",
                "old": "占位旧性格",
                "new": "疑似剧情复述内容",
                "action": "rejected",
            }
        ],
        facts_delta=[
            {
                "field": "background",
                "claim": "占位出身背景",
                "quote": QUOTE,
                "chapter_number": 1,
                "inferred": False,
            }
        ],
    )

    result = await character_profile_sync_service.apply_plan(
        db=db_session, project_id=PROJECT_ID, plan=plan
    )
    assert result == {"applied": 1, "fields_written": 0, "facts_merged": 1, "stale": []}

    character = await _character(db_session, "角色甲")
    assert character.personality == "占位旧性格", "rejected 行不得写字段"
    assert character.ai_profile_snapshot is None
    assert [fact["claim"] for fact in json.loads(character.profile_facts)] == ["占位出身背景"]
    assert character.profile_facts_updated_at is not None


async def test_apply_plan_returns_busy_without_writing(db_session):
    await _seed(
        db_session,
        chapters=[(1, "角色甲登场。占位正文甲。")],
        characters=[{"name": "角色甲", "personality": "占位旧性格"}],
    )
    character = await _character(db_session, "角色甲")
    plan = _manual_plan(
        character,
        fields=[
            {
                "field": "personality",
                "old": "占位旧性格",
                "new": "占位新性格",
                "action": "change",
            }
        ],
    )

    CharacterProfileSyncService._in_progress.add(PROJECT_ID)
    try:
        result = await character_profile_sync_service.apply_plan(
            db=db_session, project_id=PROJECT_ID, plan=plan
        )
    finally:
        CharacterProfileSyncService._in_progress.discard(PROJECT_ID)

    assert result == {"applied": 0, "fields_written": 0, "facts_merged": 0, "stale": [], "busy": True}
    character = await _character(db_session, "角色甲")
    assert character.personality == "占位旧性格"
    assert character.ai_profile_snapshot is None


# --- ⑪ 进度与取消回调 ------------------------------------------------------


async def _six_character_project(db) -> list[str]:
    names = [f"角色{index}" for index in range(1, 7)]
    await _seed(
        db,
        chapters=[(1, "、".join(names) + "登场。占位正文。")],
        characters=[
            {
                "name": name,
                "personality": "占位旧性格",
                "ai_profile_snapshot": json.dumps({"personality": "占位旧性格"}, ensure_ascii=False),
            }
            for name in names
        ],
    )
    return names


def _six_character_payload(names: list[str]) -> dict:
    return {
        "characters": [
            {"name": name, "fields": {"personality": f"{name}占位新性格"}, "organizations": []}
            for name in names
        ]
    }


async def test_progress_callback_reports_each_chunk(db_session):
    names = await _six_character_project(db_session)
    ai = FakeAIService(_six_character_payload(names))
    progress: list[tuple[int, int]] = []

    async def _on_progress(done: int, total: int) -> None:
        progress.append((done, total))

    stats = await character_profile_sync_service.sync(
        db=db_session,
        user_id=USER_ID,
        project_id=PROJECT_ID,
        through_chapter=1,
        ai_service=ai,
        on_progress=_on_progress,
    )

    assert progress == [(1, 2), (2, 2)], "6 个实体 = 2 个 chunk，进度逐批上报"
    assert len(ai.calls) == 2
    assert stats["characters_updated"] == 6


class _CancelSecondChunk:
    """第 1 批放行、第 2 批取消。"""

    def __init__(self) -> None:
        self.calls = 0

    async def __call__(self) -> bool:
        self.calls += 1
        return self.calls > 1


async def test_cancel_after_first_chunk_stops_without_writing(db_session):
    names = await _six_character_project(db_session)
    payload = _six_character_payload(names)

    # dry-run（预览）：只剩第 1 批的结果在 collector 里，库里零变化
    progress: list[tuple[int, int]] = []

    async def _on_progress(done: int, total: int) -> None:
        progress.append((done, total))

    ai = FakeAIService(payload)
    collector = ProfileDiffCollector()
    stats = await character_profile_sync_service.sync(
        db=db_session,
        user_id=USER_ID,
        project_id=PROJECT_ID,
        through_chapter=1,
        ai_service=ai,
        dry_run=True,
        collector=collector,
        on_progress=_on_progress,
        should_cancel=_CancelSecondChunk(),
    )
    assert stats["cancelled"] is True
    assert len(ai.calls) == 1, "取消后不得继续调用模型"
    assert progress == [(1, 2)], "被取消的批次不上报进度"
    assert len(collector.entities) == 5, "只有第 1 批进入了计划"
    for name in names:
        character = await _character(db_session, name)
        assert character.personality == "占位旧性格"
        assert character.background is None
        assert character.profile_synced_through_chapter == 0

    # 真实模式（should_cancel 显式传入）：中断同样零写入
    ai2 = FakeAIService(payload)
    stats2 = await character_profile_sync_service.sync(
        db=db_session,
        user_id=USER_ID,
        project_id=PROJECT_ID,
        through_chapter=1,
        ai_service=ai2,
        should_cancel=_CancelSecondChunk(),
    )
    assert stats2["cancelled"] is True
    assert len(ai2.calls) == 1
    for name in names:
        character = await _character(db_session, name)
        assert character.personality == "占位旧性格", "取消后已完成的批次也不得落库"
        assert character.background is None
        assert character.appearance is None
        assert character.current_state is None
        assert character.profile_synced_through_chapter == 0
        assert json.loads(character.ai_profile_snapshot) == {"personality": "占位旧性格"}


# --- ⑫ 同一实体跨文本批的字段收口 ------------------------------------------

_NARRATION = (
    "因为她和丈夫不仅是夫妻，还是师兄妹，她的骑术和刀法都不在角色乙之下，"
    "「占位双刀」的名头她可是占了一半的。"
)


class _RotatingFieldsAIService:
    """每个文本批返回不同的字段值，用来复现「同一实体被多批返回」。"""

    def __init__(self, values: list[str]) -> None:
        self.values = values
        self.calls = 0

    async def call_with_json_retry(self, *, prompt, **kwargs):
        value = self.values[min(self.calls, len(self.values) - 1)]
        self.calls += 1
        return _fields_payload("角色甲", personality=value)


async def _three_batch_project(db, monkeypatch) -> None:
    """3 章各超预算 → 同一实体在 3 个文本批里各返回一次。"""
    import app.services.character_profile_sync_service as mod

    monkeypatch.setattr(mod, "_PROMPT_CHAR_BUDGET", 100)
    await _seed(
        db,
        chapters=[
            (number, f"角色甲登场。占位正文{number}。" + "占位正文" * 30)
            for number in (1, 2, 3)
        ],
        characters=[
            {
                "name": "角色甲",
                "personality": "占位旧性格",
                "ai_profile_snapshot": json.dumps({"personality": "占位旧性格"}, ensure_ascii=False),
            }
        ],
    )


async def test_same_field_across_batches_collapses_to_last_value(db_session, monkeypatch):
    await _three_batch_project(db_session, monkeypatch)
    ai = _RotatingFieldsAIService(["占位性格甲", "占位性格乙", "占位性格丙"])

    collector = ProfileDiffCollector()
    await character_profile_sync_service.sync(
        db=db_session,
        user_id=USER_ID,
        project_id=PROJECT_ID,
        through_chapter=3,
        ai_service=ai,
        dry_run=True,
        collector=collector,
    )

    assert ai.calls == 3, "3 个文本批各调用一次"
    entry = next(iter(collector.entities.values()))
    assert len(entry["fields"]) == 1, "同一字段只保留一行"
    row = entry["fields"][0]
    assert row["field"] == "personality"
    assert row["action"] == "change"
    assert row["new"] == "占位性格丙", "后一批的值胜出（与 apply 顺序回放一致）"


async def test_rejected_batch_does_not_override_earlier_usable_value(db_session, monkeypatch):
    await _three_batch_project(db_session, monkeypatch)
    ai = _RotatingFieldsAIService(["占位新性格", _NARRATION, "占位新性格"])

    collector = ProfileDiffCollector()
    stats = await character_profile_sync_service.sync(
        db=db_session,
        user_id=USER_ID,
        project_id=PROJECT_ID,
        through_chapter=3,
        ai_service=ai,
        dry_run=True,
        collector=collector,
    )

    entry = next(iter(collector.entities.values()))
    assert len(entry["fields"]) == 1
    assert entry["fields"][0]["action"] == "change"
    assert entry["fields"][0]["new"] == "占位新性格"
    assert stats["fields_rejected"] == 1, "rejected 计数是全集，不因收口少计"
    assert entry["stats"]["fields_rejected"] == 1


async def test_all_batches_rejected_keeps_one_rejected_row(db_session, monkeypatch):
    await _three_batch_project(db_session, monkeypatch)
    ai = _RotatingFieldsAIService(
        [
            f"{_NARRATION}占位甲",
            f"{_NARRATION}占位乙",
            f"{_NARRATION}占位丙",
        ]
    )

    collector = ProfileDiffCollector()
    stats = await character_profile_sync_service.sync(
        db=db_session,
        user_id=USER_ID,
        project_id=PROJECT_ID,
        through_chapter=3,
        ai_service=ai,
        dry_run=True,
        collector=collector,
    )

    entry = next(iter(collector.entities.values()))
    assert len(entry["fields"]) == 1, "全部被丢弃时也只保留一行"
    assert entry["fields"][0]["action"] == "rejected"
    assert entry["fields"][0]["new"].endswith("占位甲"), "保留首次出现的那条"
    assert stats["fields_rejected"] == 3


async def test_collapsed_plan_replay_equals_un_collapsed_replay(db_session):
    """收口不改变写入结果：两份计划（收口前/后）回放终态必须一致。"""
    await _seed(
        db_session,
        chapters=[(1, "角色甲登场。角色乙登场。占位正文甲。")],
        characters=[
            {
                "name": "角色甲",
                "personality": "占位原性格",
                "appearance": "占位手改外貌",
                "ai_profile_snapshot": json.dumps({"personality": "占位原性格"}, ensure_ascii=False),
            },
            {
                "name": "角色乙",
                "personality": "占位原性格",
                "appearance": "占位手改外貌",
                "ai_profile_snapshot": json.dumps({"personality": "占位原性格"}, ensure_ascii=False),
            },
        ],
    )
    fact = {
        "field": "background",
        "claim": "占位出身背景",
        "quote": QUOTE,
        "chapter_number": 1,
        "inferred": False,
    }
    char_a = await _character(db_session, "角色甲")
    char_b = await _character(db_session, "角色乙")

    # 同一实体被多批返回的字段序列（未收口）：background 两次（后写覆盖），
    # personality 一次 same，appearance 一次 preserved（用户手改）。
    script = [
        {"field": "background", "old": None, "new": "占位背景甲", "action": "add"},
        {"field": "background", "old": None, "new": "占位背景乙", "action": "change"},
        {"field": "personality", "old": "占位原性格", "new": "占位原性格", "action": "same"},
        {"field": "appearance", "old": "占位手改外貌", "new": "占位新外貌", "action": "preserved"},
    ]

    un_collapsed_plan = _manual_plan(char_a, fields=script, facts_delta=[fact])

    # 收口后的计划由 collector 自己算出来，避免手写「预期收口结果」
    collector = ProfileDiffCollector()
    for row in script:
        collector.record(char_b, row["field"], row["old"], row["new"], row["action"])
    collapsed_entry = collector.entity_entry(char_b)
    collapsed_entry["facts_delta"] = [fact]
    assert [row["field"] for row in collapsed_entry["fields"]] == [
        "background",
        "personality",
        "appearance",
    ]
    assert len(collapsed_entry["fields"]) == 3, "4 行收口成 3 行（background 去重）"
    collapsed_plan = {"entities": [collapsed_entry]}

    await character_profile_sync_service.apply_plan(
        db=db_session, project_id=PROJECT_ID, plan=un_collapsed_plan
    )
    await character_profile_sync_service.apply_plan(
        db=db_session, project_id=PROJECT_ID, plan=collapsed_plan
    )

    un_collapsed = await _character(db_session, "角色甲")
    collapsed = await _character(db_session, "角色乙")
    for field in ("personality", "background", "appearance", "current_state"):
        assert getattr(un_collapsed, field) == getattr(collapsed, field), field
    assert un_collapsed.background == "占位背景乙"
    assert json.loads(un_collapsed.ai_profile_snapshot) == json.loads(collapsed.ai_profile_snapshot)
    assert json.loads(un_collapsed.ai_profile_snapshot) == {
        "background": "占位背景乙",
        "personality": "占位原性格",
    }, "preserved 字段不进快照"
    assert un_collapsed.profile_facts == collapsed.profile_facts


async def test_collapse_keeps_first_seen_field_order(db_session, monkeypatch):
    """`fields` 顺序按字段首次出现的批次固定，后续批次不改变位置。"""
    await _three_batch_project(db_session, monkeypatch)

    class _ScriptedAIService:
        def __init__(self, payloads: list[dict]) -> None:
            self.payloads = payloads
            self.calls = 0

        async def call_with_json_retry(self, *, prompt, **kwargs):
            payload = self.payloads[min(self.calls, len(self.payloads) - 1)]
            self.calls += 1
            return payload

    ai = _ScriptedAIService(
        [
            _fields_payload("角色甲", background="占位背景甲"),
            _fields_payload("角色甲", personality="占位性格甲"),
            _fields_payload("角色甲", background="占位背景乙"),
        ]
    )
    collector = ProfileDiffCollector()
    await character_profile_sync_service.sync(
        db=db_session,
        user_id=USER_ID,
        project_id=PROJECT_ID,
        through_chapter=3,
        ai_service=ai,
        dry_run=True,
        collector=collector,
    )

    entry = next(iter(collector.entities.values()))
    assert [row["field"] for row in entry["fields"]] == ["background", "personality"]
    assert _row(entry, "background")["new"] == "占位背景乙"


# --- ⑦ estimate 真算 ------------------------------------------------------


async def test_estimate_calls_min_matches_actual_calls(db_session, monkeypatch):
    import app.services.character_profile_sync_service as mod

    monkeypatch.setattr(mod, "_PROMPT_CHAR_BUDGET", 200)

    names = [f"角色{index}" for index in range(1, 7)]
    filler = "占位正文" * 60
    chapters = [
        (number, "、".join(names) + "登场。" + filler)
        for number in (1, 2, 3)
    ]
    await _seed(
        db_session,
        chapters=chapters,
        characters=[{"name": name} for name in names],
    )

    estimate = await character_profile_sync_service.estimate_refresh(
        db=db_session, project_id=PROJECT_ID, mode="incremental"
    )
    assert estimate["candidates"] == 6
    assert estimate["entities_total"] == 6
    assert estimate["no_mention"] == 0
    assert estimate["chunks"] == 2, "6 个实体按 5 个一批 = 2 个 chunk"
    assert estimate["calls_min"] == 6, "每个 chunk 内 3 章各超预算 → 各 1 批"
    assert estimate["calls_max"] == 18

    ai = FakeAIService(
        {
            "characters": [
                {"name": name, "fields": {"personality": f"{name}占位性格"}, "organizations": []}
                for name in names
            ]
        }
    )
    await character_profile_sync_service.sync(
        db=db_session,
        user_id=USER_ID,
        project_id=PROJECT_ID,
        through_chapter=3,
        ai_service=ai,
    )

    assert len(ai.calls) == estimate["calls_min"]
