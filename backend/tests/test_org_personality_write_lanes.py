"""组织 personality 的「主动指定」写入车道收口（issue #214）。

#210/#213 已关掉模型生成、档案同步、导入导出三条车道，但人/agent 主动指定的
两条车道仍可写：人工更新 API 的通用 setattr 循环，以及 agent 的
update_character / manage_character。本文件覆盖这两条（含创建车道）：
组织一律被剔除、角色不受影响。
"""
import os
import uuid

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from starlette.requests import Request

import app.api.characters as characters_module
from app.api.characters import update_character
from app.database import Base
from app.models.character import Character
from app.models.project import Project
from app.schemas.character import CharacterUpdate
from app.services.project_agent_tools import ProjectAgentToolRegistry

USER_ID = "user-1"


@pytest.fixture
async def db_session():
    db_path = f"/tmp/test_org_personality_lanes_{uuid.uuid4().hex}.db"
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
def allow_project_access(monkeypatch):
    """人工 API 的用户鉴权与 project 归属不属本测试范围，直接放行。"""

    async def _allow(project_id, user_id, db):
        return None

    monkeypatch.setattr(characters_module, "verify_project_access", _allow)


def make_request(user_id: str = USER_ID) -> Request:
    req = Request({"type": "http", "method": "PUT", "path": "/", "headers": []})
    req.state.user_id = user_id
    return req


async def _make_entities(db) -> tuple[Project, Character, Character]:
    project = Project(id=f"proj-{uuid.uuid4().hex}", user_id=USER_ID, title="测试项目")
    db.add(project)
    org = Character(
        project_id=project.id,
        name="测试组织",
        is_organization=True,
        role_type="supporting",
        personality=None,
        background="旧背景",
    )
    person = Character(
        project_id=project.id,
        name="测试角色",
        is_organization=False,
        role_type="supporting",
        personality="旧性格",
    )
    db.add_all([org, person])
    await db.commit()
    return project, org, person


async def _personality_of(db, character_id: str) -> str | None:
    db.expire_all()
    return (await db.execute(
        select(Character.personality).where(Character.id == character_id)
    )).scalar_one()


# --- 人工更新 API ---------------------------------------------------------


@pytest.mark.anyio
async def test_api_update_ignores_personality_for_organization(db_session):
    """组织 PATCH/PUT 带 personality：接口成功，落库仍为 NULL。"""
    _, org, _ = await _make_entities(db_session)

    result = await update_character(
        org.id,
        CharacterUpdate(personality="野心勃勃", background="新背景"),
        make_request(),
        db_session,
    )

    assert result["background"] == "新背景"
    assert result["personality"] is None
    assert await _personality_of(db_session, org.id) is None


@pytest.mark.anyio
async def test_api_update_keeps_personality_for_character(db_session):
    """角色回归：非组织实体带 personality 必须正常写入。"""
    _, _, person = await _make_entities(db_session)

    result = await update_character(
        person.id,
        CharacterUpdate(personality="沉默寡言"),
        make_request(),
        db_session,
    )

    assert result["personality"] == "沉默寡言"
    assert await _personality_of(db_session, person.id) == "沉默寡言"


@pytest.mark.anyio
async def test_api_update_flipping_character_to_organization_clears_personality(db_session):
    """同一次 PUT 把角色改成组织：判定必须取改动生效后的类型，且旧性格文本作废。

    独立复核实测出来的边界：守卫若用改动前的 is_organization，这次请求会漏拦
    （角色变组织，同时把 personality 写进组织）。
    """
    _, _, person = await _make_entities(db_session)

    result = await update_character(
        person.id,
        CharacterUpdate(is_organization=True, personality="野心勃勃"),
        make_request(),
        db_session,
    )

    assert result["is_organization"] is True
    assert result["personality"] is None
    assert await _personality_of(db_session, person.id) is None


# --- agent update_character -------------------------------------------------


@pytest.mark.anyio
async def test_agent_update_character_drops_personality_for_organization(db_session):
    """组织：personality 不进入 prepare 出的 fields。"""
    project, org, _ = await _make_entities(db_session)
    registry = ProjectAgentToolRegistry(project, db_session)

    _, fields, _ = await registry._resolve_update("update_character", {
        "character_id": org.id,
        "personality": "野心勃勃",
        "background": "新背景",
    })

    assert fields == {"background": "新背景"}


@pytest.mark.anyio
async def test_agent_update_character_preview_omits_personality_for_organization(db_session):
    """组织：修改预览里没有 personality 这一项，且执行后落库仍为 NULL。"""
    project, org, _ = await _make_entities(db_session)
    registry = ProjectAgentToolRegistry(project, db_session)
    arguments = {
        "character_id": org.id,
        "personality": "野心勃勃",
        "background": "新背景",
    }

    preview = await registry.preview("update_character", arguments)
    assert set(preview["changes"]) == {"background"}

    await registry.execute("update_character", arguments)
    assert await _personality_of(db_session, org.id) is None


@pytest.mark.anyio
async def test_agent_update_character_keeps_personality_for_character(db_session):
    """角色回归：agent 更新非组织实体，personality 正常进入 fields 并落库。"""
    project, _, person = await _make_entities(db_session)
    registry = ProjectAgentToolRegistry(project, db_session)
    arguments = {"character_id": person.id, "personality": "沉默寡言"}

    preview = await registry.preview("update_character", arguments)
    assert set(preview["changes"]) == {"personality"}

    await registry.execute("update_character", arguments)
    assert await _personality_of(db_session, person.id) == "沉默寡言"


@pytest.mark.anyio
async def test_agent_update_character_only_personality_on_organization_raises(db_session):
    """组织只给了 personality：宁可报「缺少可修改字段」，不能返回「已更新」却零写入。"""
    project, org, _ = await _make_entities(db_session)
    registry = ProjectAgentToolRegistry(project, db_session)

    with pytest.raises(ValueError, match="缺少可修改字段"):
        await registry.preview("update_character", {
            "character_id": org.id,
            "personality": "野心勃勃",
        })


# --- agent manage_character 创建 --------------------------------------------


@pytest.mark.anyio
async def test_agent_create_organization_drops_personality(db_session):
    """创建车道：组织带 personality 不落库，预览里也不出现该字段。"""
    project, _, _ = await _make_entities(db_session)
    registry = ProjectAgentToolRegistry(project, db_session)
    arguments = {
        "action": "create",
        "data": {
            "name": "新建组织",
            "is_organization": True,
            "personality": "野心勃勃",
            "background": "新背景",
        },
    }

    preview = await registry.preview("manage_character", arguments)
    assert "personality" not in preview["changes"]

    result = await registry.execute("manage_character", arguments)
    assert result["after"]["personality"] is None
    assert await _personality_of(db_session, result["entity_id"]) is None


@pytest.mark.anyio
async def test_agent_create_character_keeps_personality(db_session):
    """创建车道回归：普通角色带 personality 正常保存。"""
    project, _, _ = await _make_entities(db_session)
    registry = ProjectAgentToolRegistry(project, db_session)
    arguments = {
        "action": "create",
        "data": {"name": "新建角色", "personality": "沉默寡言"},
    }

    result = await registry.execute("manage_character", arguments)
    assert result["after"]["personality"] == "沉默寡言"
    assert await _personality_of(db_session, result["entity_id"]) == "沉默寡言"
