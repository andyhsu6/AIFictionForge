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
