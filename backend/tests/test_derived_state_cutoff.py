"""issue #288：派生状态的章节上限钳制与一致性自检。"""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.database import Base
from app.models.chapter import Chapter
from app.models.character import Character
from app.models.project import Project
from app.services.chapter_context_service import _append_current_state_lines
from app.services.derived_state import (
    clamp_derived_state_after_delete,
    find_stale_derived_state,
)

PROJECT_ID = "p-288"
USER_ID = "u-288"


def _run(scenario) -> None:
    async def wrapper() -> None:
        engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        factory = async_sessionmaker(bind=engine, expire_on_commit=False)
        async with factory() as session:
            await scenario(session)
        await engine.dispose()

    asyncio.run(wrapper())


def _facts(*chapters: int) -> str:
    return json.dumps(
        [{"field": "background", "claim": f"c{n}", "chapter_number": n, "inferred": False} for n in chapters],
        ensure_ascii=False,
    )


async def _seed(session, *, max_chapter: int) -> None:
    session.add(
        Project(
            id=PROJECT_ID,
            user_id=USER_ID,
            title="t",
            outline_mode="one-to-many",
            world_profile_synced_through_chapter=62,
            world_facts=json.dumps(
                [
                    {"field": "world_rules", "text": "来自第10章的规则。", "evidence": [{"chapter": 10, "quote": "x"}]},
                    {"field": "world_rules", "text": "来自第62章的规则。", "evidence": [{"chapter": 62, "quote": "y"}]},
                    {"field": "world_rules", "text": "无证据的规则。", "evidence": []},
                ],
                ensure_ascii=False,
            ),
        )
    )
    for number in range(1, max_chapter + 1):
        session.add(
            Chapter(id=f"c-{number}", project_id=PROJECT_ID, chapter_number=number, title=f"第{number}章", content="x")
        )
    session.add(
        Character(
            id="char-1",
            project_id=PROJECT_ID,
            name="角色甲",
            profile_facts=_facts(10, 57, 61, 62),
            current_state="来自第62章的状态",
            state_updated_chapter=62,
            profile_synced_through_chapter=62,
        )
    )
    await session.commit()


def test_clamp_drops_facts_and_clears_state_beyond_last_chapter():
    async def scenario(session):
        await _seed(session, max_chapter=57)

        stats = await clamp_derived_state_after_delete(session, PROJECT_ID)
        await session.commit()

        assert stats["cutoff"] == 57
        assert stats["dropped_facts"] == 2  # 61, 62
        assert stats["cleared_state"] == 1

        character = (
            await session.execute(select(Character).where(Character.id == "char-1"))
        ).scalar_one()
        facts = json.loads(character.profile_facts)
        assert [f["chapter_number"] for f in facts] == [10, 57]
        assert character.current_state is None
        assert character.state_updated_chapter is None  # 置空以便更早章节重跑分析时重建
        assert character.profile_synced_through_chapter == 57

        project = (
            await session.execute(select(Project).where(Project.id == PROJECT_ID))
        ).scalar_one()
        assert project.world_profile_synced_through_chapter == 57

        check = await find_stale_derived_state(session, PROJECT_ID)
        assert check["ok"] is True
        assert check["facts_beyond_cutoff"] == 0
        assert check["characters"] == []
        assert check["world_facts_beyond_cutoff"] == 0
        assert check["total_offending"] == 0

    _run(scenario)


def test_consistency_check_reports_stale_data_without_mutating():
    async def scenario(session):
        await _seed(session, max_chapter=57)

        check = await find_stale_derived_state(session, PROJECT_ID)
        assert check["ok"] is False
        assert check["facts_beyond_cutoff"] == 2
        assert check["characters"][0]["stale_current_state"] is True

        # 只读：状态没有被改动
        character = (
            await session.execute(select(Character).where(Character.id == "char-1"))
        ).scalar_one()
        assert character.state_updated_chapter == 62

    _run(scenario)


def test_clamp_is_idempotent_and_keeps_in_range_data():
    async def scenario(session):
        await _seed(session, max_chapter=57)
        first = await clamp_derived_state_after_delete(session, PROJECT_ID)
        await session.commit()
        second = await clamp_derived_state_after_delete(session, PROJECT_ID)
        await session.commit()

        assert first["dropped_facts"] == 2
        assert second["dropped_facts"] == 0
        assert second["cleared_state"] == 0

    _run(scenario)


def test_context_skips_state_advanced_beyond_the_target_chapter():
    character = SimpleNamespace(
        status="active",
        status_changed_chapter=62,
        current_state="来自第62章的状态",
        state_updated_chapter=62,
    )

    future_lines: list[str] = []
    _append_current_state_lines(future_lines, character, target_chapter=58)
    assert not any("当前心理/处境" in line for line in future_lines)
    assert not any("第62章变更" in line for line in future_lines)

    past_lines: list[str] = []
    _append_current_state_lines(past_lines, character, target_chapter=62)
    assert any("当前心理/处境" in line for line in past_lines)
    assert any("第62章更新" in line for line in past_lines)


def test_clamp_prunes_world_facts_whose_evidence_is_gone():
    async def scenario(session):
        await _seed(session, max_chapter=57)
        stats = await clamp_derived_state_after_delete(session, PROJECT_ID)
        await session.commit()

        assert stats["dropped_world_facts"] == 1
        project = (
            await session.execute(select(Project).where(Project.id == PROJECT_ID))
        ).scalar_one()
        facts = json.loads(project.world_facts)
        texts = [f["text"] for f in facts]
        assert "来自第62章的规则。" not in texts
        assert "来自第10章的规则。" in texts
        assert "无证据的规则。" in texts  # 无法归属的保留
        assert project.world_profile_synced_through_chapter == 57

    _run(scenario)


def test_clamp_does_not_touch_clean_data():
    async def scenario(session):
        await _seed(session, max_chapter=57)
        await clamp_derived_state_after_delete(session, PROJECT_ID)
        await session.commit()
        session.expunge_all()

        before = (
            await session.execute(select(Character).where(Character.id == "char-1"))
        ).scalar_one()
        snapshot = (before.profile_facts, before.current_state, before.state_updated_chapter)

        second = await clamp_derived_state_after_delete(session, PROJECT_ID)
        assert second["dropped_facts"] == 0
        assert second["dropped_world_facts"] == 0
        assert second["cleared_state"] == 0
        assert second["clamped_markers"] == 0

        after = (
            await session.execute(select(Character).where(Character.id == "char-1"))
        ).scalar_one()
        assert (after.profile_facts, after.current_state, after.state_updated_chapter) == snapshot
        assert not session.dirty, "数据本就合规时不应产生任何 UPDATE"

    _run(scenario)


def test_clamp_cleans_status_relationships_foreshadows_and_rerenders_text():
    from app.models.foreshadow import Foreshadow
    from app.models.relationship import CharacterRelationship, RelationshipTypeLink

    async def scenario(session):
        await _seed(session, max_chapter=57)
        c1 = (await session.execute(select(Character).where(Character.id == "char-1"))).scalar_one()
        c1.status = "deceased"
        c1.status_changed_chapter = 62
        c1.background = "c10\nc57\nc61\nc62"
        c1.personality = "用户手改性格"
        c1.ai_profile_snapshot = json.dumps(
            {"background": "c10\nc57\nc61\nc62", "personality": "旧AI性格"},
            ensure_ascii=False,
        )

        c2 = Character(id="char-2", project_id=PROJECT_ID, name="角色乙", status="active")
        session.add(c2)

        rel_mixed = CharacterRelationship(
            id="rel-mixed",
            project_id=PROJECT_ID,
            character_from_id="char-1",
            character_to_id="char-2",
            relationship_name="旧识",
            status="past",
            ended_at="第62章",
            description="[第10章] 初识\n[第58章] 超前冲突\n[第62章] 决裂",
            source="analysis",
        )
        rel_future = CharacterRelationship(
            id="rel-future",
            project_id=PROJECT_ID,
            character_from_id="char-2",
            character_to_id="char-1",
            relationship_name="",
            status="active",
            description="[第60章] 仅超前章新建",
            source="analysis",
        )
        session.add_all([rel_mixed, rel_future])
        await session.flush()
        session.add(
            RelationshipTypeLink(
                id=1,
                relationship_id="rel-future",
                relationship_type_id=1,
            )
        )

        fs_resolved_future = Foreshadow(
            id="fs-1",
            project_id=PROJECT_ID,
            title="旧伏笔",
            content="第7章埋下",
            status="resolved",
            plant_chapter_number=7,
            actual_resolve_chapter_number=62,
            resolution_notes="第62章回收",
            source_type="analysis",
        )
        fs_planted_future = Foreshadow(
            id="fs-2",
            project_id=PROJECT_ID,
            title="超前伏笔",
            content="第60章埋下",
            status="planted",
            plant_chapter_number=60,
            source_type="analysis",
        )
        session.add_all([fs_resolved_future, fs_planted_future])
        await session.commit()

        stats = await clamp_derived_state_after_delete(session, PROJECT_ID)
        await session.commit()

        assert stats["cleaned_relationships"] == 2
        assert stats["rolled_back_foreshadows"] == 2

        c1_after = (await session.execute(select(Character).where(Character.id == "char-1"))).scalar_one()
        assert c1_after.status == "active"
        assert c1_after.status_changed_chapter is None
        assert "c61" not in (c1_after.background or "")
        assert "c62" not in (c1_after.background or "")
        assert "c57" in (c1_after.background or "")
        assert c1_after.personality == "用户手改性格"

        rel_mixed_after = (
            await session.execute(select(CharacterRelationship).where(CharacterRelationship.id == "rel-mixed"))
        ).scalar_one()
        assert rel_mixed_after.status == "active"
        assert rel_mixed_after.ended_at is None
        assert rel_mixed_after.description == "[第10章] 初识"

        assert (
            await session.execute(select(CharacterRelationship).where(CharacterRelationship.id == "rel-future"))
        ).scalar_one_or_none() is None
        assert (
            await session.execute(select(RelationshipTypeLink).where(RelationshipTypeLink.id == 1))
        ).scalar_one_or_none() is None

        fs1 = (await session.execute(select(Foreshadow).where(Foreshadow.id == "fs-1"))).scalar_one()
        assert fs1.status == "planted"
        assert fs1.actual_resolve_chapter_number is None
        assert fs1.resolution_notes is None
        assert (await session.execute(select(Foreshadow).where(Foreshadow.id == "fs-2"))).scalar_one_or_none() is None

    _run(scenario)


def test_state_update_restores_missing_is_idempotent_and_guards_org_chapter():
    from app.models.relationship import CharacterRelationship, Organization, OrganizationMember
    from app.services.character_state_update_service import CharacterStateUpdateService

    async def scenario(session):
        await _seed(session, max_chapter=58)
        missing_char = Character(
            id="char-miss",
            project_id=PROJECT_ID,
            name="失踪者",
            status="missing",
            status_changed_chapter=40,
            current_state="失踪（第40章）",
            state_updated_chapter=40,
        )
        dead_char = Character(
            id="char-dead",
            project_id=PROJECT_ID,
            name="已故者",
            status="deceased",
            status_changed_chapter=30,
            current_state="死亡（第30章）",
            state_updated_chapter=30,
        )
        other_char = Character(id="char-other", project_id=PROJECT_ID, name="同伴", status="active")
        org_char = Character(
            id="char-org",
            project_id=PROJECT_ID,
            name="静泽堂",
            is_organization=True,
            status="active",
            state_updated_chapter=55,
        )
        session.add_all([missing_char, dead_char, other_char, org_char])
        await session.flush()

        org = Organization(id="org-1", character_id="char-org", project_id=PROJECT_ID, power_level=60)
        session.add(org)
        await session.flush()

        session.add(
            OrganizationMember(
                id="mem-1",
                organization_id="org-1",
                character_id="char-miss",
                position="弟子",
                status="retired",
                left_at="第40章",
            )
        )
        session.add(
            CharacterRelationship(
                id="rel-miss",
                project_id=PROJECT_ID,
                character_from_id="char-miss",
                character_to_id="char-other",
                relationship_name="同门",
                intimacy_level=50,
                status="past",
                ended_at="第40章",
                description="[第40章] 失散",
            )
        )
        await session.commit()

        payload = [
            {
                "character_name": "失踪者",
                "state_after": "平安归来",
                "relationship_changes": {"同伴": "重逢后更加信任"},
            },
            {
                "character_name": "已故者",
                "state_after": "回忆片段",
            },
        ]
        await CharacterStateUpdateService.update_from_analysis(
            db=session,
            project_id=PROJECT_ID,
            character_states=payload,
            chapter_id="c-58",
            chapter_number=58,
        )
        await CharacterStateUpdateService.update_from_analysis(
            db=session,
            project_id=PROJECT_ID,
            character_states=payload,
            chapter_id="c-58",
            chapter_number=58,
        )

        miss_after = (await session.execute(select(Character).where(Character.id == "char-miss"))).scalar_one()
        assert miss_after.status == "active"
        assert miss_after.status_changed_chapter is None
        assert miss_after.current_state == "平安归来"

        dead_after = (await session.execute(select(Character).where(Character.id == "char-dead"))).scalar_one()
        assert dead_after.status == "deceased"

        mem_after = (await session.execute(select(OrganizationMember).where(OrganizationMember.id == "mem-1"))).scalar_one()
        assert mem_after.status == "active"
        assert mem_after.left_at is None

        rel_after = (
            await session.execute(select(CharacterRelationship).where(CharacterRelationship.id == "rel-miss"))
        ).scalar_one()
        assert rel_after.status == "active"
        assert rel_after.ended_at is None
        assert rel_after.description.count("[第58章]") == 1
        assert rel_after.intimacy_level == 60

        res_old = await CharacterStateUpdateService.update_organization_states(
            db=session,
            project_id=PROJECT_ID,
            organization_states=[{"organization_name": "静泽堂", "power_change": 20}],
            chapter_number=50,
        )
        assert res_old["updated_count"] == 0
        res_new = await CharacterStateUpdateService.update_organization_states(
            db=session,
            project_id=PROJECT_ID,
            organization_states=[{"organization_name": "静泽堂", "power_change": 5}],
            chapter_number=58,
        )
        assert res_new["updated_count"] == 1
        org_char_after = (await session.execute(select(Character).where(Character.id == "char-org"))).scalar_one()
        assert org_char_after.state_updated_chapter == 58

    _run(scenario)


def test_build_characters_info_filters_future_chapter_state_and_status():
    from app.api.chapters import build_characters_info_with_careers

    async def scenario(session):
        await _seed(session, max_chapter=57)
        c1 = (await session.execute(select(Character).where(Character.id == "char-1"))).scalar_one()
        c1.status = "deceased"
        c1.status_changed_chapter = 62
        await session.commit()

        info_at_58 = await build_characters_info_with_careers(
            db=session,
            project_id=PROJECT_ID,
            characters=[c1],
            target_chapter=58,
        )
        assert "已死亡" not in info_at_58
        assert "来自第62章的状态" not in info_at_58

        info_at_62 = await build_characters_info_with_careers(
            db=session,
            project_id=PROJECT_ID,
            characters=[c1],
            target_chapter=62,
        )
        assert "已死亡" in info_at_62
        assert "来自第62章的状态" in info_at_62

    _run(scenario)
