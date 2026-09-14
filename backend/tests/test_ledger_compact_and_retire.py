"""Ledger tools: retire duplicate entries + read a whole large ledger.

Two real gaps on the project agent's foreshadow ledger surface:

A. A handoff asked the agent to retire duplicate rows produced by a merge
   that never took effect, but the produced plan only carried
   ``manage_foreshadow`` update/resolve steps.  The registry already exposes
   ``abandon``/``delete``; the tool descriptions never say which one to pick
   for a duplicate/void row, nor where the reason belongs, so the model does
   not choose them.
B. ``list_foreshadows`` returns full rows, so a real ``limit=100`` call
   produced ~49KB of JSON while the model-facing tool result is clipped at
   ``ProjectAgentService.TOOL_RESULT_MAX_CHARS`` (8000).  With a 200-row
   ledger the model cannot see the table at all.  ``compact`` + ``offset``
   give it the whole ledger (paged, and small enough per page to survive the
   clip).

Behavior pinned against the real handlers:
- ``abandon``: row kept, ``status`` -> ``"abandoned"``, ``data.reason``
  persisted into ``resolution_notes``.
- ``delete``: row removed from the table; ``data.reason`` is echoed in the
  result message (no row is left to persist it in).

Fixtures and assertion strings are neutral placeholders (no source text).
"""
from __future__ import annotations

import json
import os
from datetime import datetime, timedelta
from types import SimpleNamespace
import uuid

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.database import Base
from app.models.foreshadow import Foreshadow
from app.models.project import Project
from app.services.agent_plan_schema import (
    PROPOSE_PLAN_TOOL_DESCRIPTION,
    validate_plan,
)
from app.services.project_agent_extended_tools import ProjectAgentExtendedTools
from app.services.project_agent_tools import ProjectAgentToolRegistry

PROJECT_ID = "proj-ledger-tools"
USER_ID = "u-ledger-tools"
ROW_COUNT = 200
LEDGER_TOOL = "list_foreshadows"
STATUSES = ("pending", "planted", "resolved", "abandoned")
BASE_TIME = datetime(2020, 1, 1, 0, 0, 0)
RETIRE_ID = "f000"
TWIN_ID = "f100"
REASON = "placeholder duplicate created by a merge that did not take effect"


def _row_id(index: int) -> str:
    return f"f{index:03d}"


def _row_title(index: int) -> str:
    return f"n{index}"


@pytest.fixture
async def db_engine():
    db_path = f"/tmp/test_ledger_tools_{uuid.uuid4().hex}.db"
    engine = create_async_engine(f"sqlite+aiosqlite:///{db_path}")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield engine
    await engine.dispose()
    if os.path.exists(db_path):
        os.remove(db_path)


@pytest.fixture
async def ledger(db_engine):
    """One neutral project with a 200-row ledger (short unique ids/titles)."""
    Session = async_sessionmaker(bind=db_engine, expire_on_commit=False)
    async with Session() as session:
        project = Project(id=PROJECT_ID, user_id=USER_ID, title="neutral project")
        session.add(project)
        session.add_all([
            Foreshadow(
                id=_row_id(i), project_id=PROJECT_ID, title=_row_title(i),
                content="placeholder", status=STATUSES[i % len(STATUSES)],
                plant_chapter_number=i + 1 if i % 3 == 0 else None,
                target_resolve_chapter_number=i + 1 if i % 5 else None,
                created_at=BASE_TIME + timedelta(seconds=i),
            )
            for i in range(ROW_COUNT)
        ])
        await session.commit()
        yield SimpleNamespace(
            project=project,
            db=session,
            registry=ProjectAgentToolRegistry(project, session),
            extended=ProjectAgentExtendedTools(project, session),
        )


def _definitions() -> dict[str, dict]:
    """registry.definitions() is the only surface the model can see."""
    registry = ProjectAgentToolRegistry(SimpleNamespace(id="p1"), None)
    return {
        item["function"]["name"]: item["function"]
        for item in registry.definitions()
    }


def _schemas() -> dict[str, dict]:
    return {name: function["parameters"] for name, function in _definitions().items()}


def _plan(arguments: dict) -> dict:
    return {
        "objective": "retire duplicate ledger rows",
        "steps": [
            {"id": "s1", "tool": "manage_foreshadow", "arguments": arguments},
        ],
    }


async def _load(db, foreshadow_id: str) -> Foreshadow | None:
    return (await db.execute(
        select(Foreshadow).where(Foreshadow.id == foreshadow_id)
    )).scalar_one_or_none()


# --- A. retire expressibility -------------------------------------------------


def test_manage_foreshadow_description_names_the_retire_actions():
    description = _definitions()["manage_foreshadow"]["description"]
    assert "重复" in description
    assert "abandon" in description
    assert "delete" in description
    assert "foreshadow_id" in description
    assert "reason" in description


def test_propose_plan_description_documents_retire_actions():
    description = PROPOSE_PLAN_TOOL_DESCRIPTION
    assert "manage_foreshadow" in description
    assert "abandon" in description
    assert "delete" in description
    assert "reason" in description


@pytest.mark.anyio
async def test_abandon_plan_step_validates_and_retires_the_row(ledger):
    arguments = {
        "action": "abandon", "foreshadow_id": RETIRE_ID,
        "data": {"reason": REASON},
    }
    plan = validate_plan(
        _plan(arguments),
        allowed_tools={"manage_foreshadow"},
        tool_schemas=_schemas(),
    )
    assert plan["steps"][0]["arguments"] == arguments

    result = await ledger.extended.execute("manage_foreshadow", dict(arguments))

    row = await _load(ledger.db, RETIRE_ID)
    assert row is not None                    # a trace is kept
    assert row.status == "abandoned"          # observed handler behavior
    assert row.resolution_notes == REASON     # data.reason persisted
    assert result["after"]["status"] == "abandoned"
    assert result["after"]["resolution_notes"] == REASON


@pytest.mark.anyio
async def test_delete_plan_step_validates_and_removes_the_row(ledger):
    arguments = {
        "action": "delete", "foreshadow_id": TWIN_ID,
        "data": {"reason": REASON},
    }
    plan = validate_plan(
        _plan(arguments),
        allowed_tools={"manage_foreshadow"},
        tool_schemas=_schemas(),
    )
    assert plan["steps"][0]["arguments"] == arguments

    result = await ledger.extended.execute("manage_foreshadow", dict(arguments))

    assert await _load(ledger.db, TWIN_ID) is None   # truly removed
    assert REASON in result["message"]               # reason survives in the result


# --- B. compact + paged list --------------------------------------------------


def test_list_description_recommends_compact_paging():
    definition = _definitions()[LEDGER_TOOL]
    description = definition["description"]
    assert "compact" in description
    assert "offset" in description
    assert "500" in description          # limit cap stays advertised

    properties = definition["parameters"]["properties"]
    assert properties["compact"]["type"] == "boolean"
    assert properties["offset"]["type"] == "integer"
    assert properties["offset"]["minimum"] == 0
    assert properties["limit"]["maximum"] == 500


@pytest.mark.anyio
async def test_compact_200_rows_survives_the_tool_result_clip(ledger):
    result = await ledger.registry.execute(
        LEDGER_TOOL, {"compact": True, "limit": ROW_COUNT}
    )
    stored = json.dumps(
        {"tool": LEDGER_TOOL, "error": None, "result": result},
        ensure_ascii=False,
        default=str,
    )
    assert len(stored) < 8000

    assert result["total"] == ROW_COUNT
    assert result["returned"] == ROW_COUNT
    assert result["offset"] == 0
    assert len(result["items"]) == ROW_COUNT

    by_id: dict[str, list] = {}
    for item in result["items"]:
        assert isinstance(item, list)
        assert len(item) <= 6
        by_id[item[0]] = item
    assert len(by_id) == ROW_COUNT
    for i in range(ROW_COUNT):
        item = by_id[_row_id(i)]
        assert item[1] == _row_title(i)
        assert item[2] == STATUSES[i % len(STATUSES)]
    # chapter numbers ride along when available; trailing nulls are trimmed
    assert by_id[_row_id(0)] == [_row_id(0), _row_title(0), "pending", 1]
    assert by_id[_row_id(5)] == [_row_id(5), _row_title(5), STATUSES[1]]
    # a null between two chapter numbers is kept (positional meaning stays stable)
    assert by_id[_row_id(1)] == [_row_id(1), _row_title(1), "planted", None, 2]


@pytest.mark.anyio
async def test_compact_offset_paging_returns_disjoint_windows(ledger):
    windows = [
        await ledger.registry.execute(
            LEDGER_TOOL, {"compact": True, "limit": 50, "offset": offset}
        )
        for offset in (0, 50, 150)
    ]
    ids = [[item[0] for item in window["items"]] for window in windows]

    assert ids[0] == [_row_id(i) for i in range(50)]
    assert ids[1] == [_row_id(i) for i in range(50, 100)]
    assert ids[2] == [_row_id(i) for i in range(150, 200)]
    assert not set(ids[0]) & set(ids[1])
    assert not set(ids[1]) & set(ids[2])
    assert not set(ids[0]) & set(ids[2])
    for window, offset in zip(windows, (0, 50, 150)):
        assert window["total"] == ROW_COUNT
        assert window["returned"] == 50
        assert window["offset"] == offset


@pytest.mark.anyio
async def test_non_compact_payload_is_unchanged(ledger):
    result = await ledger.registry.execute(LEDGER_TOOL, {"limit": ROW_COUNT})

    assert set(result.keys()) == {"total", "items"}
    assert result["total"] == ROW_COUNT
    assert result["items"] == [
        {
            "id": _row_id(i),
            "title": _row_title(i),
            "content": "placeholder",
            "status": STATUSES[i % len(STATUSES)],
            "importance": 0.5,
            "target_resolve_chapter_number": i + 1 if i % 5 else None,
        }
        for i in range(ROW_COUNT)
    ]
