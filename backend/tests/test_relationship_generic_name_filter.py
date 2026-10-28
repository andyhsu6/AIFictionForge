"""关系抽取的自动补角色路径必须跳过亲属/称谓词（issue #182）。

实测：对 5 章做关系抽取后，角色表里多出「母亲」「妻子」「师父」「宫装丽人」
这类条目——它们只是称谓/描述，不是人物。`_GENERIC_PERSON_NAMES` 原先只挡
「他/她/男人/女子/小孩」，遗漏了亲属称谓。

本文件钉死：关系里出现的称谓词不得建角色行；同一条关系里的正常人名仍照常补建。
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

PROJECT_ID = "proj-generic-1"
USER_ID = "user-generic-1"

pytestmark = pytest.mark.anyio

RELATIONSHIP_TEMPLATE = "标题:{title}\n类型:{genre}\n原文:\n{chapters_text}"


@pytest.fixture
async def db_session():
    db_path = f"/tmp/test_generic_names_{uuid.uuid4().hex}.db"
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
    calls: list[str] = []

    async def resolve_budget_chars(model_name=None):
        return 1_000_000

    async def call_with_json_retry(*, prompt, **kwargs):
        calls.append(prompt)
        return items

    ai_service.resolve_full_budget = resolve_budget_chars
    ai_service.resolve_full_book_budget_chars = resolve_budget_chars
    monkeypatch.setattr(ai_service, "call_with_json_retry", call_with_json_retry, raising=False)
    return ai_service, calls


async def _seed(db):
    project = Project(id=PROJECT_ID, user_id=USER_ID, title="placeholder project")
    chapter = Chapter(
        id="chapter-generic-1",
        project_id=PROJECT_ID,
        chapter_number=1,
        title="placeholder chapter one",
        content="占位章节正文：母亲与占位人物乙在场。",
        word_count=20,
        status="draft",
    )
    db.add_all([project, chapter])
    await db.commit()
    return project, [chapter]


@pytest.mark.anyio
async def test_relational_terms_are_not_created_as_characters(db_session, patched_prompts, monkeypatch):
    project, chapters = await _seed(db_session)
    items = [
        {
            "character_a": "母亲",
            "character_b": "占位人物乙",
            "relationship_types": ["盟友"],
            "description": "占位关系描述",
        }
    ]
    ai_service, _calls = _install_ai_service(monkeypatch, items)

    result = await BookImportService()._extract_relationships_from_chapters(
        db=db_session,
        user_id=USER_ID,
        project=project,
        chapters=chapters,
        ai_service=ai_service,
    )
    await db_session.commit()

    names = set(
        (await db_session.execute(select(Character.name).where(Character.project_id == PROJECT_ID)))
        .scalars().all()
    )
    assert "母亲" not in names, "称谓词不得被建成角色"
    assert "占位人物乙" in names, "同一关系里的正常人名仍应照常补建"
    # 源角色解析不到 → 该条关系不落库
    assert result["extracted_relationships"] == 0


@pytest.mark.anyio
async def test_generic_set_covers_kinship_and_address_terms():
    for term in ("母亲", "父亲", "妻子", "丈夫", "师父", "师兄", "儿子", "女儿", "叔叔", "阿姨"):
        assert term in bis._GENERIC_PERSON_NAMES
