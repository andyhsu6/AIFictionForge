"""删除路径子行清理回归测试（issue #128）。

背景：`app/database.py` 没有启用 `PRAGMA foreign_keys=ON`，模型里声明的
CASCADE/SET NULL 在 SQLite 上不生效。删除入口必须显式清理子行，否则留下
悬空外键（`PRAGMA foreign_key_check` 可检出），并可能阻塞后续删除。

覆盖：delete_project / delete_chapter / delete_outline / delete_character /
delete_relationship，以及两个误阻塞修复（关系类型、写作风格），另含一次性
清理脚本的 dry-run / apply / 幂等。
"""
import os
import sqlite3
import uuid

import pytest
from sqlalchemy import create_engine, func, select, text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from starlette.requests import Request

from app.database import Base
from app.core.errors import ApiError
from app.api.characters import delete_character
from app.api.chapters import delete_chapter
from app.api.outlines import delete_outline
from app.api.projects import delete_project
from app.api.relationships import delete_relationship, delete_relationship_type
from app.api.writing_styles import delete_writing_style, get_writing_style
from app.models.analysis_task import AnalysisTask
from app.models.chapter import Chapter
from app.models.character import Character
from app.models.generation_history import GenerationHistory
from app.models.memory import PlotAnalysis, StoryMemory
from app.models.outline import Outline
from app.models.project import Project
from app.models.project_default_style import ProjectDefaultStyle
from app.models.regeneration_task import RegenerationTask
from app.models.relationship import (
    CharacterRelationship,
    RelationshipType,
    RelationshipTypeLink,
)
from app.models.writing_style import WritingStyle
from app.services.book_import_service import BookImportService
from scripts.cleanup_dangling_fk_rows import run as cleanup_run

USER_ID = "user-1"
CHAPTER_CHILD_TABLES = {
    "plot_analysis",
    "story_memories",
    "analysis_tasks",
    "regeneration_tasks",
    "generation_history",
}


@pytest.fixture
async def db_session():
    """临时文件 SQLite（避免 in-memory 多连接问题），测试后清理。"""
    db_path = f"/tmp/test_delete_cleanup_{uuid.uuid4().hex}.db"
    engine = create_async_engine(f"sqlite+aiosqlite:///{db_path}")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    Session = async_sessionmaker(bind=engine, expire_on_commit=False)
    async with Session() as session:
        yield session
    await engine.dispose()
    if os.path.exists(db_path):
        os.remove(db_path)


def make_request(user_id: str = USER_ID) -> Request:
    req = Request({"type": "http", "method": "DELETE", "path": "/", "headers": []})
    req.state.user_id = user_id
    return req


async def fk_violations(db_session, tables: set[str]) -> list:
    """返回指定子表上的悬空外键行。"""
    rows = (await db_session.execute(text("PRAGMA foreign_key_check"))).all()
    return [row for row in rows if row[0] in tables]


async def count_where(db_session, model, column, value) -> int:
    return (
        await db_session.execute(
            select(func.count()).select_from(model).where(column == value)
        )
    ).scalar_one()


async def _seed_project(db_session, *, outline_mode: str = "one-to-many"):
    """种子：项目 A + 章节 1 + 两个角色 + 关系/类型/链接 + 全部章节子行。"""
    project = Project(id="project-a", user_id=USER_ID, title="Project A",
                      outline_mode=outline_mode, current_words=0)
    outline = Outline(id="outline-1", project_id=project.id, title="Outline 1", order_index=1)
    chapter = Chapter(id="chapter-1", project_id=project.id, chapter_number=1,
                      title="Chapter 1", outline_id=outline.id, word_count=0)
    char_x = Character(id="char-x", project_id=project.id, name="Character X")
    char_y = Character(id="char-y", project_id=project.id, name="Character Y")
    rel_type = RelationshipType(id=1, project_id=project.id, name="Type A", category="social")
    relationship = CharacterRelationship(
        id="rel-1", project_id=project.id,
        character_from_id=char_x.id, character_to_id=char_y.id,
        relationship_type_id=rel_type.id,
    )
    link = RelationshipTypeLink(relationship_id=relationship.id, relationship_type_id=rel_type.id)
    style = WritingStyle(id=1, user_id=USER_ID, name="Style A",
                         style_type="custom", prompt_content="p")
    default_style = ProjectDefaultStyle(project_id=project.id, style_id=style.id)
    plot = PlotAnalysis(id="plot-1", project_id=project.id, chapter_id=chapter.id)
    memory = StoryMemory(id="mem-1", project_id=project.id, chapter_id=chapter.id,
                         memory_type="plot_point", content="c", story_timeline=1)
    analysis = AnalysisTask(id="task-1", chapter_id=chapter.id, user_id=USER_ID,
                            project_id=project.id)
    regen = RegenerationTask(id="regen-1", chapter_id=chapter.id, user_id=USER_ID,
                             project_id=project.id, modification_instructions="i")
    history = GenerationHistory(id="hist-1", project_id=project.id, chapter_id=chapter.id)

    db_session.add_all([
        project, outline, chapter, char_x, char_y, rel_type, relationship, link,
        style, default_style, plot, memory, analysis, regen, history,
    ])
    await db_session.commit()
    return {
        "project": project, "outline": outline, "chapter": chapter,
        "char_x": char_x, "char_y": char_y, "relationship": relationship,
        "link": link, "style": style, "default_style": default_style,
        "plot": plot, "memory": memory, "analysis": analysis, "regen": regen,
        "history": history,
    }


async def _assert_chapter_children_gone(db_session, chapter_id: str) -> None:
    assert await count_where(db_session, PlotAnalysis, PlotAnalysis.chapter_id, chapter_id) == 0
    assert await count_where(db_session, StoryMemory, StoryMemory.chapter_id, chapter_id) == 0
    assert await count_where(db_session, AnalysisTask, AnalysisTask.chapter_id, chapter_id) == 0
    assert await count_where(db_session, RegenerationTask, RegenerationTask.chapter_id, chapter_id) == 0
    assert await fk_violations(db_session, CHAPTER_CHILD_TABLES) == []


@pytest.mark.anyio
async def test_delete_project_cleans_all_project_children(db_session):
    seed = await _seed_project(db_session)
    project_id = seed["project"].id

    await delete_project(project_id, make_request(), db_session)

    assert await count_where(db_session, Project, Project.id, project_id) == 0
    assert await count_where(db_session, Chapter, Chapter.project_id, project_id) == 0
    assert await count_where(db_session, Outline, Outline.project_id, project_id) == 0
    assert await count_where(db_session, Character, Character.project_id, project_id) == 0
    assert await count_where(db_session, CharacterRelationship,
                             CharacterRelationship.project_id, project_id) == 0
    assert await count_where(db_session, PlotAnalysis, PlotAnalysis.project_id, project_id) == 0
    assert await count_where(db_session, StoryMemory, StoryMemory.project_id, project_id) == 0
    assert await count_where(db_session, AnalysisTask, AnalysisTask.project_id, project_id) == 0
    assert await count_where(db_session, RegenerationTask, RegenerationTask.project_id, project_id) == 0
    assert await count_where(db_session, ProjectDefaultStyle,
                             ProjectDefaultStyle.project_id, project_id) == 0
    assert await count_where(db_session, GenerationHistory,
                             GenerationHistory.project_id, project_id) == 0
    assert await count_where(db_session, RelationshipTypeLink,
                             RelationshipTypeLink.relationship_id, seed["relationship"].id) == 0
    assert await fk_violations(db_session, CHAPTER_CHILD_TABLES | {"character_relationship_type_links"}) == []


@pytest.mark.anyio
async def test_overwrite_clear_preserves_existing_default_style(db_session):
    """覆盖导入清空数据后，项目已选定的默认写作风格不得被重置为首个全局预设。"""
    project = Project(id="project-a", user_id=USER_ID, title="Project A")
    preset_first = WritingStyle(id=1, user_id=None, name="Preset First",
                                style_type="preset", prompt_content="p", order_index=1)
    preset_chosen = WritingStyle(id=2, user_id=None, name="Preset Chosen",
                                 style_type="preset", prompt_content="p", order_index=2)
    chosen = ProjectDefaultStyle(project_id=project.id, style_id=preset_chosen.id)
    db_session.add_all([project, preset_first, preset_chosen, chosen])
    await db_session.commit()

    svc = BookImportService()
    await svc._clear_project_data(db=db_session, project_id=project.id)
    await svc._ensure_project_default_style(db=db_session, project_id=project.id)
    await db_session.commit()

    rows = (
        await db_session.execute(
            select(ProjectDefaultStyle).where(ProjectDefaultStyle.project_id == project.id)
        )
    ).scalars().all()
    assert len(rows) == 1
    assert rows[0].style_id == preset_chosen.id


@pytest.mark.anyio
async def test_overwrite_clear_seeds_default_style_when_missing(db_session):
    """无默认风格的项目在覆盖导入清空后，仍应自动种子化为首个全局预设。"""
    project = Project(id="project-a", user_id=USER_ID, title="Project A")
    preset = WritingStyle(id=1, user_id=None, name="Preset First",
                          style_type="preset", prompt_content="p", order_index=1)
    db_session.add_all([project, preset])
    await db_session.commit()

    svc = BookImportService()
    await svc._clear_project_data(db=db_session, project_id=project.id)
    await svc._ensure_project_default_style(db=db_session, project_id=project.id)
    await db_session.commit()

    rows = (
        await db_session.execute(
            select(ProjectDefaultStyle).where(ProjectDefaultStyle.project_id == project.id)
        )
    ).scalars().all()
    assert len(rows) == 1
    assert rows[0].style_id == preset.id


@pytest.mark.anyio
async def test_delete_chapter_cleans_chapter_children(db_session):
    seed = await _seed_project(db_session)
    chapter_id = seed["chapter"].id

    await delete_chapter(chapter_id, make_request(), db_session)

    assert await count_where(db_session, Chapter, Chapter.id, chapter_id) == 0
    await _assert_chapter_children_gone(db_session, chapter_id)
    history = (
        await db_session.execute(select(GenerationHistory).where(GenerationHistory.id == "hist-1"))
    ).scalar_one_or_none()
    assert history is not None and history.chapter_id is None


@pytest.mark.anyio
async def test_delete_outline_cleans_chapter_children_one_to_many(db_session):
    seed = await _seed_project(db_session, outline_mode="one-to-many")
    chapter_id = seed["chapter"].id

    await delete_outline(seed["outline"].id, make_request(), db_session)

    assert await count_where(db_session, Outline, Outline.id, seed["outline"].id) == 0
    assert await count_where(db_session, Chapter, Chapter.id, chapter_id) == 0
    await _assert_chapter_children_gone(db_session, chapter_id)


@pytest.mark.anyio
async def test_delete_outline_cleans_chapter_children_one_to_one(db_session):
    seed = await _seed_project(db_session, outline_mode="one-to-one")
    chapter_id = seed["chapter"].id
    seed["outline"].order_index = 1
    seed["chapter"].chapter_number = 1
    await db_session.commit()

    await delete_outline(seed["outline"].id, make_request(), db_session)

    assert await count_where(db_session, Outline, Outline.id, seed["outline"].id) == 0
    assert await count_where(db_session, Chapter, Chapter.id, chapter_id) == 0
    await _assert_chapter_children_gone(db_session, chapter_id)


@pytest.mark.anyio
async def test_delete_character_cleans_relationships_and_links(db_session):
    seed = await _seed_project(db_session)

    await delete_character(seed["char_x"].id, make_request(), db_session)

    assert await count_where(db_session, Character, Character.id, seed["char_x"].id) == 0
    assert await count_where(db_session, CharacterRelationship,
                             CharacterRelationship.id, seed["relationship"].id) == 0
    assert await count_where(db_session, RelationshipTypeLink,
                             RelationshipTypeLink.relationship_id, seed["relationship"].id) == 0
    assert await count_where(db_session, Character, Character.id, seed["char_y"].id) == 1
    assert await fk_violations(db_session, {"character_relationship_type_links"}) == []


@pytest.mark.anyio
async def test_delete_relationship_cleans_type_links(db_session):
    seed = await _seed_project(db_session)

    await delete_relationship(seed["relationship"].id, make_request(), db_session)

    assert await count_where(db_session, CharacterRelationship,
                             CharacterRelationship.id, seed["relationship"].id) == 0
    assert await count_where(db_session, RelationshipTypeLink,
                             RelationshipTypeLink.relationship_id, seed["relationship"].id) == 0
    assert await count_where(db_session, Character, Character.id, seed["char_x"].id) == 1


@pytest.mark.anyio
async def test_delete_relationship_type_not_blocked_by_dangling_link(db_session):
    """悬空链接（关系行已删除）不应把未使用的关系类型误判为“使用中”。"""
    project = Project(id="project-a", user_id=USER_ID, title="Project A")
    rel_type = RelationshipType(id=1, project_id=project.id, name="Type A",
                                category="social", is_system=False)
    dangling = RelationshipTypeLink(relationship_id="ghost-rel", relationship_type_id=rel_type.id)
    db_session.add_all([project, rel_type, dangling])
    await db_session.commit()

    result = await delete_relationship_type(rel_type.id, make_request(), db_session)

    assert result["id"] == rel_type.id
    assert await count_where(db_session, RelationshipType, RelationshipType.id, rel_type.id) == 0


@pytest.mark.anyio
async def test_delete_relationship_type_still_blocked_by_live_link(db_session):
    """仍被真实关系引用的类型应继续阻断删除（不能过度放宽）。"""
    seed = await _seed_project(db_session)
    seed["relationship"].relationship_type_id = None
    await db_session.commit()

    with pytest.raises(ApiError) as exc_info:
        await delete_relationship_type(1, make_request(), db_session)
    assert exc_info.value.code == "conflict.relationship_type_in_use"


@pytest.mark.anyio
async def test_get_writing_style_ignores_dangling_default(db_session):
    style = WritingStyle(id=1, user_id=USER_ID, name="Style A",
                         style_type="custom", prompt_content="p")
    dangling = ProjectDefaultStyle(project_id="ghost-project", style_id=style.id)
    db_session.add_all([style, dangling])
    await db_session.commit()

    data = await get_writing_style(style.id, make_request(), db_session)
    assert data["is_default"] is False


@pytest.mark.anyio
async def test_delete_writing_style_not_blocked_by_dangling_default(db_session):
    """默认关联指向已删除项目时，不得误判 is_default 而阻断风格删除。"""
    style = WritingStyle(id=1, user_id=USER_ID, name="Style A",
                         style_type="custom", prompt_content="p")
    dangling = ProjectDefaultStyle(project_id="ghost-project", style_id=style.id)
    db_session.add_all([style, dangling])
    await db_session.commit()

    await delete_writing_style(style.id, make_request(), db_session)

    assert await count_where(db_session, WritingStyle, WritingStyle.id, style.id) == 0


@pytest.mark.anyio
async def test_delete_writing_style_still_blocked_by_live_default(db_session):
    """项目仍存在且使用该风格为默认时，应继续阻断删除。"""
    project = Project(id="project-a", user_id=USER_ID, title="Project A")
    style = WritingStyle(id=1, user_id=USER_ID, name="Style A",
                         style_type="custom", prompt_content="p")
    default_style = ProjectDefaultStyle(project_id=project.id, style_id=style.id)
    db_session.add_all([project, style, default_style])
    await db_session.commit()

    with pytest.raises(ApiError) as exc_info:
        await delete_writing_style(style.id, make_request(), db_session)
    assert exc_info.value.code == "validation.style_default_delete_blocked"


def _create_cleanup_db(path: str) -> None:
    engine = create_engine(f"sqlite:///{path}")
    Base.metadata.create_all(engine)
    engine.dispose()


def _seed_cleanup_orphans(db_path: str) -> None:
    conn = sqlite3.connect(db_path)
    try:
        conn.executescript(
            """
            INSERT INTO projects (id, user_id, title, outline_mode, current_words, cover_status)
                VALUES ('project-a', 'user-1', 'Project A', 'one-to-many', 0, 'none');
            INSERT INTO writing_styles (id, user_id, name, style_type, prompt_content)
                VALUES (1, NULL, 'Style A', 'preset', 'p');
            INSERT INTO characters (id, project_id, name) VALUES ('char-x', 'project-a', 'Character X');
            INSERT INTO character_relationship_type_links (relationship_id, relationship_type_id)
                VALUES ('ghost-rel', 999);
            INSERT INTO story_memories (id, project_id, chapter_id, memory_type, content, story_timeline)
                VALUES ('mem-1', 'ghost-project', 'ghost-chapter', 'plot_point', 'c', 1);
            INSERT INTO plot_analysis (id, project_id, chapter_id) VALUES ('plot-1', 'ghost-project', 'ghost-chapter');
            INSERT INTO analysis_tasks (id, chapter_id, user_id, project_id, status)
                VALUES ('task-1', 'ghost-chapter', 'user-1', 'ghost-project', 'pending');
            INSERT INTO regeneration_tasks (id, chapter_id, user_id, project_id, modification_instructions)
                VALUES ('regen-1', 'ghost-chapter', 'user-1', 'ghost-project', 'i');
            INSERT INTO project_default_styles (project_id, style_id) VALUES ('ghost-project', 1);
            INSERT INTO chapters (id, project_id, chapter_number, title, outline_id, word_count)
                VALUES ('chapter-orphan', 'project-a', 9, 'Chapter 1', 'ghost-outline', 0);
            INSERT INTO generation_history (id, project_id, chapter_id)
                VALUES ('hist-1', 'project-a', 'ghost-chapter');
            """
        )
        conn.commit()
    finally:
        conn.close()


def _count_table(db_path: str, table: str) -> int:
    conn = sqlite3.connect(db_path)
    try:
        return conn.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0]
    finally:
        conn.close()


@pytest.mark.anyio
async def test_cleanup_script_dry_run_apply_idempotent(tmp_path):
    db_path = str(tmp_path / "cleanup.db")
    _create_cleanup_db(db_path)
    _seed_cleanup_orphans(db_path)

    dry = cleanup_run(db_path, apply=False)
    assert dry["planned"] > 0
    assert dry["applied"] == 0
    assert _count_table(db_path, "story_memories") == 1

    applied = cleanup_run(db_path, apply=True)
    assert applied["applied"] == applied["planned"] > 0
    for table in (
        "character_relationship_type_links",
        "story_memories",
        "plot_analysis",
        "analysis_tasks",
        "regeneration_tasks",
        "project_default_styles",
    ):
        assert _count_table(db_path, table) == 0, table
    assert applied["after"] == {}

    conn = sqlite3.connect(db_path)
    try:
        chapter = conn.execute(
            "SELECT outline_id FROM chapters WHERE id = 'chapter-orphan'"
        ).fetchone()
        history = conn.execute(
            "SELECT chapter_id FROM generation_history WHERE id = 'hist-1'"
        ).fetchone()
    finally:
        conn.close()
    assert _count_table(db_path, "chapters") == 1
    assert chapter[0] is None
    assert _count_table(db_path, "generation_history") == 1
    assert history[0] is None

    second = cleanup_run(db_path, apply=True)
    assert second["planned"] == 0
    assert second["applied"] == 0
