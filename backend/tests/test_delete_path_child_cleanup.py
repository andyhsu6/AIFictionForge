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
from app.api.organizations import delete_organization
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
    Organization,
    OrganizationMember,
    RelationshipType,
    RelationshipTypeLink,
)
from app.models.writing_style import WritingStyle
from app.services.book_import_service import BookImportService
from app.services.project_agent_extended_tools import ProjectAgentExtendedTools
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


async def _seed_organization_graph(db_session, *, project_id: str = "project-a"):
    """种子：项目 A + 角色 X/Y/Z + 组织 One/Two（Two 是 One 的子组织）+ 成员关系。"""
    project = Project(id=project_id, user_id=USER_ID, title="Project A")
    char_x = Character(id="char-x", project_id=project_id, name="Character X")
    char_y = Character(id="char-y", project_id=project_id, name="Character Y")
    char_z = Character(id="char-z", project_id=project_id, name="Character Z")
    org_one = Organization(id="org-1", project_id=project_id, character_id=char_x.id)
    org_two = Organization(id="org-2", project_id=project_id, character_id=char_y.id,
                           parent_org_id=org_one.id)
    members = [
        OrganizationMember(id="member-owner", organization_id=org_one.id,
                           character_id=char_x.id, position="Leader"),
        OrganizationMember(id="member-inner", organization_id=org_one.id,
                           character_id=char_z.id, position="Member"),
        OrganizationMember(id="member-cross", organization_id=org_two.id,
                           character_id=char_x.id, position="Advisor"),
        OrganizationMember(id="member-kept", organization_id=org_two.id,
                           character_id=char_z.id, position="Member"),
    ]
    db_session.add_all([project, char_x, char_y, char_z, org_one, org_two, *members])
    await db_session.commit()
    return {"project": project, "char_x": char_x, "char_y": char_y, "char_z": char_z,
            "org_one": org_one, "org_two": org_two}


async def _assert_org_graph_after_org_one_delete(db_session) -> None:
    org_two = (
        await db_session.execute(select(Organization).where(Organization.id == "org-2"))
    ).scalar_one_or_none()
    assert org_two is not None
    assert org_two.parent_org_id is None
    assert await count_where(db_session, Organization, Organization.id, "org-1") == 0
    assert await count_where(db_session, OrganizationMember,
                             OrganizationMember.organization_id, "org-1") == 0
    assert await count_where(db_session, OrganizationMember,
                             OrganizationMember.id, "member-kept") == 1
    assert await fk_violations(db_session, {"organizations", "organization_members"}) == []


@pytest.mark.anyio
async def test_delete_character_cleans_organizations_and_memberships(db_session):
    await _seed_organization_graph(db_session)

    await delete_character("char-x", make_request(), db_session)

    assert await count_where(db_session, Character, Character.id, "char-x") == 0
    assert await count_where(db_session, Character, Character.id, "char-y") == 1
    assert await count_where(db_session, Character, Character.id, "char-z") == 1
    assert await count_where(db_session, OrganizationMember,
                             OrganizationMember.character_id, "char-x") == 0
    await _assert_org_graph_after_org_one_delete(db_session)


@pytest.mark.anyio
async def test_delete_organization_cleans_members_and_child_parent(db_session):
    await _seed_organization_graph(db_session)

    await delete_organization("org-1", make_request(), db_session)

    await _assert_org_graph_after_org_one_delete(db_session)


@pytest.mark.anyio
async def test_agent_organization_delete_cleans_members_and_child_parent(db_session):
    seed = await _seed_organization_graph(db_session)
    tools = ProjectAgentExtendedTools(seed["project"], db_session)

    entity_id, _before, _summary = await tools._manage_organization_delete(
        {"organization_id": "org-1"}
    )
    await db_session.commit()

    assert entity_id == "org-1"
    await _assert_org_graph_after_org_one_delete(db_session)


@pytest.mark.anyio
async def test_agent_character_delete_cleans_owned_organizations(db_session):
    seed = await _seed_organization_graph(db_session)
    tools = ProjectAgentExtendedTools(seed["project"], db_session)

    await tools._manage_character_delete({"character_id": "char-x"})
    await db_session.commit()

    assert await count_where(db_session, Character, Character.id, "char-x") == 0
    assert await count_where(db_session, OrganizationMember,
                             OrganizationMember.character_id, "char-x") == 0
    await _assert_org_graph_after_org_one_delete(db_session)


@pytest.mark.anyio
async def test_delete_project_removes_project_relationship_types_keeps_presets(db_session):
    seed = await _seed_project(db_session)
    project_id = seed["project"].id
    preset = RelationshipType(id=2, project_id=None, name="Preset Type",
                              category="social", is_system=True)
    db_session.add(preset)
    await db_session.commit()

    await delete_project(project_id, make_request(), db_session)

    assert await count_where(db_session, RelationshipType,
                             RelationshipType.project_id, project_id) == 0
    assert await count_where(db_session, RelationshipType, RelationshipType.id, preset.id) == 1
    assert await fk_violations(
        db_session, {"relationship_types", "character_relationship_type_links"}
    ) == []


@pytest.mark.anyio
async def test_delete_project_cleans_legacy_dangling_type_link(db_session):
    """(a) 历史悬空链接（relationship_id 已不存在）指向项目级类型：删项目后不得残留。"""
    project = Project(id="project-a", user_id=USER_ID, title="Project A")
    rel_type = RelationshipType(id=1, project_id=project.id, name="Type T", category="social")
    dangling = RelationshipTypeLink(relationship_id="ghost-rel",
                                    relationship_type_id=rel_type.id)
    db_session.add_all([project, rel_type, dangling])
    await db_session.commit()

    await delete_project(project.id, make_request(), db_session)

    assert await count_where(db_session, RelationshipType,
                             RelationshipType.id, rel_type.id) == 0
    assert await count_where(db_session, RelationshipTypeLink,
                             RelationshipTypeLink.id, dangling.id) == 0
    assert await fk_violations(
        db_session, {"character_relationship_type_links", "character_relationships"}
    ) == []


@pytest.mark.anyio
async def test_delete_project_nulls_cross_project_type_reference(db_session):
    """(b) 跨项目引用：项目 B 的关系指向项目 A 的类型，删 A 后 B 的缓存列置空、链接删除。"""
    project_a = Project(id="project-a", user_id=USER_ID, title="Project A")
    project_b = Project(id="project-b", user_id=USER_ID, title="Project B")
    char_x = Character(id="char-x", project_id=project_b.id, name="Character X")
    char_y = Character(id="char-y", project_id=project_b.id, name="Character Y")
    type_t = RelationshipType(id=1, project_id=project_a.id, name="Type T", category="social")
    rel_b = CharacterRelationship(
        id="rel-b", project_id=project_b.id,
        character_from_id=char_x.id, character_to_id=char_y.id,
        relationship_type_id=type_t.id,
    )
    link_b = RelationshipTypeLink(relationship_id=rel_b.id, relationship_type_id=type_t.id)
    db_session.add_all([project_a, project_b, char_x, char_y, type_t, rel_b, link_b])
    await db_session.commit()

    await delete_project(project_a.id, make_request(), db_session)

    rel = (
        await db_session.execute(
            select(CharacterRelationship).where(CharacterRelationship.id == rel_b.id)
        )
    ).scalar_one()
    assert rel.relationship_type_id is None
    assert await count_where(db_session, RelationshipTypeLink,
                             RelationshipTypeLink.id, link_b.id) == 0
    assert await count_where(db_session, Project, Project.id, project_b.id) == 1
    assert await fk_violations(
        db_session, {"character_relationship_type_links", "character_relationships"}
    ) == []


@pytest.mark.anyio
async def test_delete_project_keeps_system_relationship_type_presets(db_session):
    """系统预置（project_id IS NULL）不随项目删除，且被其它项目引用时保持引用完整。"""
    project_a = Project(id="project-a", user_id=USER_ID, title="Project A")
    project_b = Project(id="project-b", user_id=USER_ID, title="Project B")
    char_x = Character(id="char-x", project_id=project_b.id, name="Character X")
    char_y = Character(id="char-y", project_id=project_b.id, name="Character Y")
    preset = RelationshipType(id=1, project_id=None, name="Type T",
                              category="social", is_system=True)
    project_type = RelationshipType(id=2, project_id=project_a.id,
                                    name="Type A", category="social")
    rel_b = CharacterRelationship(
        id="rel-b", project_id=project_b.id,
        character_from_id=char_x.id, character_to_id=char_y.id,
        relationship_type_id=preset.id,
    )
    link_b = RelationshipTypeLink(relationship_id=rel_b.id,
                                  relationship_type_id=preset.id)
    db_session.add_all([project_a, project_b, char_x, char_y, preset,
                        project_type, rel_b, link_b])
    await db_session.commit()

    await delete_project(project_a.id, make_request(), db_session)

    assert await count_where(db_session, RelationshipType,
                             RelationshipType.id, preset.id) == 1
    assert await count_where(db_session, RelationshipType,
                             RelationshipType.id, project_type.id) == 0
    assert await count_where(db_session, RelationshipTypeLink,
                             RelationshipTypeLink.id, link_b.id) == 1
    rel = (
        await db_session.execute(
            select(CharacterRelationship).where(CharacterRelationship.id == rel_b.id)
        )
    ).scalar_one()
    assert rel.relationship_type_id == preset.id
    assert await fk_violations(
        db_session, {"character_relationship_type_links", "character_relationships"}
    ) == []


@pytest.mark.anyio
async def test_delete_project_still_removes_same_project_relationship_types(db_session):
    """回归：正常的同项目关系类型仍随项目删除（不能因先置空缓存列而漏删）。"""
    seed = await _seed_project(db_session)
    project_id = seed["project"].id
    assert await count_where(db_session, RelationshipType,
                             RelationshipType.project_id, project_id) == 1

    await delete_project(project_id, make_request(), db_session)

    assert await count_where(db_session, RelationshipType,
                             RelationshipType.project_id, project_id) == 0
    assert await count_where(db_session, RelationshipTypeLink,
                             RelationshipTypeLink.id, seed["link"].id) == 0
    assert await fk_violations(
        db_session, {"character_relationship_type_links", "character_relationships"}
    ) == []


@pytest.mark.anyio
async def test_overwrite_import_retains_project_relationship_types(db_session):
    """覆盖导入保留项目级关系类型定义（#132 边界：项目配置不随覆盖导入清空）。"""
    project = Project(id="project-a", user_id=USER_ID, title="Project A")
    char_x = Character(id="char-x", project_id=project.id, name="Character X")
    rel_type = RelationshipType(id=1, project_id=project.id, name="Type A", category="social")
    preset = RelationshipType(id=2, project_id=None, name="Preset Type",
                              category="social", is_system=True)
    relationship = CharacterRelationship(
        id="rel-1", project_id=project.id,
        character_from_id=char_x.id, character_to_id="char-y",
        relationship_type_id=rel_type.id,
    )
    link = RelationshipTypeLink(relationship_id=relationship.id,
                                relationship_type_id=rel_type.id)
    db_session.add_all([project, char_x, rel_type, preset, relationship, link])
    await db_session.commit()

    svc = BookImportService()
    await svc._clear_project_data(db=db_session, project_id=project.id)
    await db_session.commit()

    assert await count_where(db_session, RelationshipType,
                             RelationshipType.project_id, project.id) == 1
    assert await count_where(db_session, RelationshipType, RelationshipType.id, preset.id) == 1
    assert await count_where(db_session, CharacterRelationship,
                             CharacterRelationship.project_id, project.id) == 0
    assert await fk_violations(db_session, {"character_relationship_type_links"}) == []


@pytest.mark.anyio
async def test_delete_relationship_type_cleans_dangling_links(db_session):
    project = Project(id="project-a", user_id=USER_ID, title="Project A")
    rel_type = RelationshipType(id=1, project_id=project.id, name="Type A",
                                category="social", is_system=False)
    dangling = RelationshipTypeLink(relationship_id="ghost-rel",
                                    relationship_type_id=rel_type.id)
    db_session.add_all([project, rel_type, dangling])
    await db_session.commit()

    await delete_relationship_type(rel_type.id, make_request(), db_session)

    assert await count_where(db_session, RelationshipType,
                             RelationshipType.id, rel_type.id) == 0
    assert await count_where(db_session, RelationshipTypeLink,
                             RelationshipTypeLink.id, dangling.id) == 0
    assert await fk_violations(db_session, {"character_relationship_type_links"}) == []


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


def _seed_cleanup_organization_orphans(db_path: str) -> None:
    conn = sqlite3.connect(db_path)
    try:
        conn.executescript(
            """
            INSERT INTO projects (id, user_id, title, outline_mode, current_words, cover_status)
                VALUES ('project-a', 'user-1', 'Project A', 'one-to-many', 0, 'none');
            INSERT INTO characters (id, project_id, name) VALUES ('char-x', 'project-a', 'Character X');
            INSERT INTO characters (id, project_id, name) VALUES ('char-y', 'project-a', 'Character Y');
            INSERT INTO characters (id, project_id, name) VALUES ('char-z', 'project-a', 'Character Z');
            INSERT INTO organizations (id, character_id, project_id)
                VALUES ('org-ok', 'char-x', 'project-a');
            INSERT INTO organizations (id, character_id, project_id, parent_org_id)
                VALUES ('org-self-dangling', 'char-z', 'project-a', 'ghost-org');
            INSERT INTO organizations (id, character_id, project_id)
                VALUES ('org-owner-missing', 'ghost-char', 'project-a');
            INSERT INTO organization_members (id, organization_id, character_id, position)
                VALUES ('member-char-missing', 'org-ok', 'ghost-char', 'Member');
            INSERT INTO organization_members (id, organization_id, character_id, position)
                VALUES ('member-org-missing', 'ghost-org', 'char-x', 'Member');
            INSERT INTO organization_members (id, organization_id, character_id, position)
                VALUES ('member-guarded', 'org-owner-missing', 'char-x', 'Member');
            INSERT INTO relationship_types (id, project_id, name, category)
                VALUES (1, 'ghost-project', 'Type Orphan', 'social');
            INSERT INTO relationship_types (id, project_id, name, category)
                VALUES (2, NULL, 'Type Preset', 'social');
            INSERT INTO relationship_types (id, project_id, name, category)
                VALUES (3, 'project-a', 'Type Live', 'social');
            """
        )
        conn.commit()
    finally:
        conn.close()


@pytest.mark.anyio
async def test_cleanup_script_handles_organization_and_relationship_type_orphans(tmp_path):
    db_path = str(tmp_path / "cleanup-org.db")
    _create_cleanup_db(db_path)
    _seed_cleanup_organization_orphans(db_path)

    dry = cleanup_run(db_path, apply=False)
    assert dry["applied"] == 0
    assert dry["planned"] == 3
    assert dry["unhandled"].get("organization_members") == 2
    assert dry["unhandled"].get("organizations") == 1

    applied = cleanup_run(db_path, apply=True)
    assert applied["applied"] == applied["planned"] == 3

    conn = sqlite3.connect(db_path)
    try:
        org_count = conn.execute("SELECT COUNT(*) FROM organizations").fetchone()[0]
        self_parent = conn.execute(
            "SELECT parent_org_id FROM organizations WHERE id = 'org-self-dangling'"
        ).fetchone()[0]
        type_ids = {row[0] for row in conn.execute("SELECT id FROM relationship_types")}
        member_ids = {row[0] for row in conn.execute("SELECT id FROM organization_members")}
    finally:
        conn.close()

    # organizations 行永不删除；自引用悬空仅断开指针（SET NULL）
    assert org_count == 3
    assert self_parent is None
    # 明确垃圾：悬空 project_id 的关系类型；系统预置与健康类型保留
    assert type_ids == {2, 3}
    # 明确垃圾：角色缺失的成员关系；组织缺失/组织悬空的成员关系转 unhandled 保留
    assert "member-char-missing" not in member_ids
    assert {"member-org-missing", "member-guarded"} <= member_ids

    second = cleanup_run(db_path, apply=True)
    assert second["planned"] == 0
    assert second["applied"] == 0


def _seed_cleanup_relationship_type_orphan_link(db_path: str) -> None:
    conn = sqlite3.connect(db_path)
    try:
        conn.executescript(
            """
            INSERT INTO relationship_types (id, project_id, name, category)
                VALUES (1, 'ghost-project', 'Type Orphan', 'social');
            INSERT INTO relationship_types (id, project_id, name, category)
                VALUES (2, NULL, 'Type Preset', 'social');
            INSERT INTO character_relationship_type_links (relationship_id, relationship_type_id)
                VALUES ('ghost-rel', 1);
            """
        )
        conn.commit()
    finally:
        conn.close()


@pytest.mark.anyio
async def test_cleanup_script_cleans_links_of_orphan_relationship_type(tmp_path):
    """悬空 relationship_type 的 link 行必须被清理，脚本结束后无外键违规残留。"""
    db_path = str(tmp_path / "cleanup-reltype.db")
    _create_cleanup_db(db_path)
    _seed_cleanup_relationship_type_orphan_link(db_path)

    conn = sqlite3.connect(db_path)
    try:
        assert conn.execute("PRAGMA foreign_key_check").fetchall()
    finally:
        conn.close()

    cleanup_run(db_path, apply=True)

    conn = sqlite3.connect(db_path)
    try:
        type_ids = {row[0] for row in conn.execute("SELECT id FROM relationship_types")}
        link_count = conn.execute(
            "SELECT COUNT(*) FROM character_relationship_type_links"
        ).fetchone()[0]
        after = conn.execute("PRAGMA foreign_key_check").fetchall()
    finally:
        conn.close()

    assert type_ids == {2}
    assert link_count == 0
    assert after == []


async def _seed_cross_project_organization_graph(db_session):
    """种子：项目 A/B + 角色 X/Y + 组织 A（父，属 A）/B（子，属 B）+ 双向跨项目成员。

    组织 A 属于项目 A、组织 B 属于项目 B 且以 A 为父组织；成员关系同时覆盖
    “项目 B 角色加入项目 A 组织”与“项目 A 角色加入项目 B 组织”两个方向 ——
    接口层未做同项目校验，这两种跨项目行都可能在真实数据中出现。
    """
    project_a = Project(id="project-a", user_id=USER_ID, title="Project A")
    project_b = Project(id="project-b", user_id=USER_ID, title="Project B")
    char_x = Character(id="char-a", project_id=project_a.id, name="Character X")
    char_y = Character(id="char-b", project_id=project_b.id, name="Character Y")
    org_a = Organization(id="org-a", project_id=project_a.id, character_id=char_x.id)
    org_b = Organization(id="org-b", project_id=project_b.id, character_id=char_y.id,
                         parent_org_id=org_a.id)
    members = [
        OrganizationMember(id="member-y-in-a", organization_id=org_a.id,
                           character_id=char_y.id, position="Advisor"),
        OrganizationMember(id="member-x-in-b", organization_id=org_b.id,
                           character_id=char_x.id, position="Advisor"),
    ]
    db_session.add_all([project_a, project_b, char_x, char_y, org_a, org_b, *members])
    await db_session.commit()
    return {"project_a": project_a, "project_b": project_b, "char_x": char_x,
            "char_y": char_y, "org_a": org_a, "org_b": org_b}


async def _assert_cross_project_organization_references_gone(db_session) -> None:
    """删除/清空项目 A 后：组织 A 消失、组织 B 幸存且断父、跨项目成员行全部清理。"""
    assert await count_where(db_session, Organization, Organization.id, "org-a") == 0
    org_b = (
        await db_session.execute(select(Organization).where(Organization.id == "org-b"))
    ).scalar_one_or_none()
    assert org_b is not None
    assert org_b.parent_org_id is None
    assert await count_where(db_session, OrganizationMember,
                             OrganizationMember.id, "member-y-in-a") == 0
    assert await count_where(db_session, OrganizationMember,
                             OrganizationMember.id, "member-x-in-b") == 0
    assert await fk_violations(
        db_session, {"organizations", "organization_members"}
    ) == []


@pytest.mark.anyio
async def test_delete_project_cleans_cross_project_organization_rows(db_session):
    """删项目 A：组织 B 幸存的跨项目成员/父组织引用必须清理，不得悬空。"""
    await _seed_cross_project_organization_graph(db_session)

    await delete_project("project-a", make_request(), db_session)

    assert await count_where(db_session, Project, Project.id, "project-a") == 0
    assert await count_where(db_session, Project, Project.id, "project-b") == 1
    assert await count_where(db_session, Character, Character.id, "char-b") == 1
    await _assert_cross_project_organization_references_gone(db_session)


@pytest.mark.anyio
async def test_clear_project_data_cleans_cross_project_organization_rows(db_session):
    """覆盖导入清空项目 A：跨项目成员/父组织引用同上；项目配置（#132 边界）保留。"""
    await _seed_cross_project_organization_graph(db_session)
    style = WritingStyle(id=1, user_id=USER_ID, name="Style A",
                         style_type="custom", prompt_content="p")
    default_style = ProjectDefaultStyle(project_id="project-a", style_id=style.id)
    rel_type = RelationshipType(id=1, project_id="project-a", name="Type A", category="social")
    db_session.add_all([style, default_style, rel_type])
    await db_session.commit()

    svc = BookImportService()
    await svc._clear_project_data(db=db_session, project_id="project-a")
    await db_session.commit()

    assert await count_where(db_session, Project, Project.id, "project-a") == 1
    assert await count_where(db_session, Project, Project.id, "project-b") == 1
    assert await count_where(db_session, Character, Character.id, "char-a") == 0
    assert await count_where(db_session, Character, Character.id, "char-b") == 1
    await _assert_cross_project_organization_references_gone(db_session)
    # #132 边界：项目级关系类型与默认写作风格（include_default_styles=False）均保留
    assert await count_where(db_session, RelationshipType,
                             RelationshipType.project_id, "project-a") == 1
    assert await count_where(db_session, ProjectDefaultStyle,
                             ProjectDefaultStyle.project_id, "project-a") == 1


@pytest.mark.anyio
async def test_delete_project_cleans_cross_project_relationships(db_session):
    """删项目 A：以项目 A 角色为端点的项目 B 关系行及其类型链接不得悬空。"""
    project_a = Project(id="project-a", user_id=USER_ID, title="Project A")
    project_b = Project(id="project-b", user_id=USER_ID, title="Project B")
    char_a = Character(id="char-a", project_id=project_a.id, name="Character X")
    char_b = Character(id="char-b", project_id=project_b.id, name="Character Y")
    char_c = Character(id="char-c", project_id=project_b.id, name="Character Z")
    type_b = RelationshipType(id=1, project_id=project_b.id, name="Type B", category="social")
    rel_from_a = CharacterRelationship(
        id="rel-from-a", project_id=project_b.id,
        character_from_id=char_a.id, character_to_id=char_b.id,
        relationship_type_id=type_b.id,
    )
    rel_to_a = CharacterRelationship(
        id="rel-to-a", project_id=project_b.id,
        character_from_id=char_c.id, character_to_id=char_a.id,
    )
    link = RelationshipTypeLink(relationship_id=rel_from_a.id,
                                relationship_type_id=type_b.id)
    db_session.add_all([project_a, project_b, char_a, char_b, char_c,
                        type_b, rel_from_a, rel_to_a, link])
    await db_session.commit()

    await delete_project("project-a", make_request(), db_session)

    assert await count_where(db_session, CharacterRelationship,
                             CharacterRelationship.id, "rel-from-a") == 0
    assert await count_where(db_session, CharacterRelationship,
                             CharacterRelationship.id, "rel-to-a") == 0
    assert await count_where(db_session, RelationshipTypeLink,
                             RelationshipTypeLink.id, link.id) == 0
    # 项目 B 的类型定义与存活角色不受影响
    assert await count_where(db_session, RelationshipType,
                             RelationshipType.id, type_b.id) == 1
    assert await count_where(db_session, Character, Character.id, "char-b") == 1
    assert await count_where(db_session, Project, Project.id, "project-b") == 1
    assert await fk_violations(
        db_session, {"character_relationships", "character_relationship_type_links"}
    ) == []


def _seed_cleanup_relationship_type_live_reference(db_path: str) -> None:
    conn = sqlite3.connect(db_path)
    try:
        conn.executescript(
            """
            INSERT INTO projects (id, user_id, title, outline_mode, current_words, cover_status)
                VALUES ('project-a', 'user-1', 'Project A', 'one-to-many', 0, 'none');
            INSERT INTO characters (id, project_id, name) VALUES ('char-x', 'project-a', 'Character X');
            INSERT INTO characters (id, project_id, name) VALUES ('char-y', 'project-a', 'Character Y');
            INSERT INTO relationship_types (id, project_id, name, category)
                VALUES (1, 'ghost-project', 'Type Orphan', 'social');
            INSERT INTO relationship_types (id, project_id, name, category)
                VALUES (2, NULL, 'Type Preset', 'social');
            INSERT INTO character_relationships
                (id, project_id, character_from_id, character_to_id, relationship_type_id)
                VALUES ('rel-live', 'project-a', 'char-x', 'char-y', 1);
            """
        )
        conn.commit()
    finally:
        conn.close()


@pytest.mark.anyio
async def test_cleanup_script_nulls_live_relationship_type_reference(tmp_path):
    """孤立 relationship_type 仍被活关系引用：先置空缓存列，再删类型，不得 FK 中止。"""
    db_path = str(tmp_path / "cleanup-reltype-live.db")
    _create_cleanup_db(db_path)
    _seed_cleanup_relationship_type_live_reference(db_path)

    conn = sqlite3.connect(db_path)
    try:
        assert conn.execute("PRAGMA foreign_key_check").fetchall()
    finally:
        conn.close()

    applied = cleanup_run(db_path, apply=True)

    conn = sqlite3.connect(db_path)
    try:
        type_ids = {row[0] for row in conn.execute("SELECT id FROM relationship_types")}
        rel_type_id = conn.execute(
            "SELECT relationship_type_id FROM character_relationships WHERE id = 'rel-live'"
        ).fetchone()[0]
        after = conn.execute("PRAGMA foreign_key_check").fetchall()
    finally:
        conn.close()

    assert applied["after"] == {}
    assert type_ids == {2}
    assert rel_type_id is None
    assert after == []
