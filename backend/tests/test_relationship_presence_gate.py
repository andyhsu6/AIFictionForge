"""自动补角色的存在度门禁（issue #184）。

只出现在 1-2 章的"两句领盒饭"角色不该建行：它们不会在后续剧情里承接作用，
只会占满 MAX_IMPORTED_CHARACTERS_PER_IMPORT 名额、让档案同步与成员统计反复花预算。

钉死三件事：
(a) 1 章登场的名字不建行，引用它的关系被跳过且不抛异常；
(b) 3 章登场的名字照常建行（阈值边界）；
(c) 称谓词即使出现很多章也不建行——频率不能覆盖具名性门禁。
"""
from __future__ import annotations

import os
import uuid
from types import SimpleNamespace

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import app.database  # noqa: F401 —— 必须先于 models 导入，避免循环导入
from app.database import Base
from app.models.chapter import Chapter
from app.models.character import Character
from app.models.project import Project
from app.services import book_import_service as bis
from app.services.book_import_service import BookImportService
from app.services.prompt_service import PromptService

PROJECT_ID = "proj-presence-1"
USER_ID = "user-presence-1"

pytestmark = pytest.mark.anyio

RELATIONSHIP_TEMPLATE = "标题:{title}\n类型:{genre}\n原文:\n{chapters_text}"


@pytest.fixture
async def db_session():
    db_path = f"/tmp/test_presence_gate_{uuid.uuid4().hex}.db"
    engine = create_async_engine(f"sqlite+aiosqlite:///{db_path}")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    Session = async_sessionmaker(bind=engine, expire_on_commit=False)
    async with Session() as session:
        yield session
    await engine.dispose()
    if os.path.exists(db_path):
        os.remove(db_path)


@pytest.fixture
def patched_prompts(monkeypatch):
    async def fake_get_template(name, user_id=None, db=None):
        return RELATIONSHIP_TEMPLATE

    async def fake_generation_language(db, user_id, content_language=None):
        return "zh"

    monkeypatch.setattr(PromptService, "get_template", fake_get_template)
    monkeypatch.setattr(bis, "resolve_user_generation_language", fake_generation_language)


def _install_ai_service(monkeypatch, items):
    ai_service = SimpleNamespace(default_model="stub-model")

    async def resolve_budget_chars(model_name=None):
        return 1_000_000

    async def call_with_json_retry(*, prompt, **kwargs):
        return items

    ai_service.resolve_full_budget = resolve_budget_chars
    ai_service.resolve_full_book_budget_chars = resolve_budget_chars
    monkeypatch.setattr(ai_service, "call_with_json_retry", call_with_json_retry, raising=False)
    return ai_service


async def _seed(db, chapter_texts: list[str]):
    project = Project(id=PROJECT_ID, user_id=USER_ID, title="placeholder project")
    chapters = [
        Chapter(
            id=f"chapter-presence-{index}",
            project_id=PROJECT_ID,
            chapter_number=index,
            title=f"placeholder chapter {index}",
            content=text,
            word_count=len(text),
            status="draft",
        )
        for index, text in enumerate(chapter_texts, start=1)
    ]
    db.add_all([project, *chapters])
    await db.commit()
    return project, chapters


async def _extract(db, project, chapters, monkeypatch, items):
    ai_service = _install_ai_service(monkeypatch, items)
    result = await BookImportService()._extract_relationships_from_chapters(
        db=db,
        user_id=USER_ID,
        project=project,
        chapters=chapters,
        ai_service=ai_service,
    )
    await db.commit()
    names = set(
        (await db.execute(select(Character.name).where(Character.project_id == PROJECT_ID)))
        .scalars().all()
    )
    return result, names


@pytest.mark.anyio
async def test_single_chapter_name_is_not_created(db_session, patched_prompts, monkeypatch):
    """(a) 只在 1 章出现的名字不建行，关系被跳过且不抛异常。"""
    project, chapters = await _seed(db_session, ["占位正文：路人甲长老现身，随即退场。"])
    items = [{
        "character_a": "路人甲长老",
        "character_b": "占位人物乙",
        "relationship_types": ["盟友"],
        "description": "占位关系描述",
    }]

    result, names = await _extract(db_session, project, chapters, monkeypatch, items)

    assert "路人甲长老" not in names
    assert result["extracted_relationships"] == 0


@pytest.mark.anyio
async def test_three_chapter_name_is_created(db_session, patched_prompts, monkeypatch):
    """(b) 出现在 3 章的名字照常建行（阈值边界）。"""
    project, chapters = await _seed(
        db_session,
        [
            "占位正文：路人乙长老登场。",
            "占位正文：路人乙长老再出场。",
            "占位正文：路人乙长老第三次出场。",
        ],
    )
    items = [{
        "character_a": "路人乙长老",
        "character_b": "占位人物丙",
        "relationship_types": ["盟友"],
        "description": "占位关系描述",
    }]

    _result, names = await _extract(db_session, project, chapters, monkeypatch, items)

    assert "路人乙长老" in names
    assert "占位人物丙" not in names, "另一端的 1 章名字仍应被门禁拦下"


@pytest.mark.anyio
async def test_kinship_term_with_high_frequency_is_still_blocked(db_session, patched_prompts, monkeypatch):
    """(c) 称谓词出现 3 章也不建行——频率不能覆盖具名性门禁。"""
    project, chapters = await _seed(
        db_session,
        ["占位正文：母亲在场。", "占位正文：母亲开口。", "占位正文：母亲离开。"],
    )
    items = [{
        "character_a": "母亲",
        "character_b": "占位人物丁",
        "relationship_types": ["母子"],
        "description": "占位关系描述",
    }]

    _result, names = await _extract(db_session, project, chapters, monkeypatch, items)

    assert "母亲" not in names


def test_threshold_constant_is_documented():
    assert bis._MIN_CHAPTERS_FOR_AUTO_CHARACTER >= 2


@pytest.mark.anyio
async def test_auto_created_character_carries_no_fabricated_profile(db_session, patched_prompts, monkeypatch):
    """自动补建的角色不得携带伪造档案（issue #186）。

    关系抽取的输出契约里没有 personality/background/appearance/age/gender，
    旧实现去读这些键只会拿到 None，并把 evidence 当成 personality 留下原文碎片。
    正确行为是建一个空档案的骨架，交由档案同步按章节原文回填。
    """
    project, chapters = await _seed(
        db_session,
        [
            "占位正文：路人丙登场。",
            "占位正文：路人丙再说一句。",
            "占位正文：路人丙退场。",
        ],
    )
    items = [{
        "character_a": "路人丙",
        "character_b": "占位人物戊",
        "relationship_types": ["盟友"],
        "description": "占位关系描述",
        "evidence": "路人丙说了一句占位台词……",
    }]

    _result, names = await _extract(db_session, project, chapters, monkeypatch, items)
    assert "路人丙" in names

    row = (
        await db_session.execute(
            select(Character).where(Character.project_id == PROJECT_ID, Character.name == "路人丙")
        )
    ).scalar_one()
    assert row.personality is None
    assert row.background is None
    assert row.appearance is None
    assert row.age is None
    assert row.gender is None
    assert row.ai_profile_snapshot is None, "没有写入任何字段时不应登记快照，否则后续同步会把它当历史值保护"
