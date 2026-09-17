"""issue #136 part B：关系/组织 API 同项目引用校验回归。

契约变更：此前 create_relationship / add_organization_member / 组织 parent_org_id
会静默接受跨项目引用；现在创建时即以 400 拒绝。系统预置关系类型（project_id 为
NULL）仍可跨项目复用。

测试值一律中性占位（Project A / Character A / Type A），不含任何导入原文、
角色人名或书名（AGENTS.md 原文数据脱敏硬约束）。
"""
import json
import os
import uuid
from pathlib import Path

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from starlette.requests import Request

from app.api.organizations import (
    add_organization_member,
    create_organization,
    update_organization,
)
from app.api.relationships import create_relationship
from app.core.errors import ERROR_REGISTRY, ApiError
from app.database import Base
from app.models.character import Character
from app.models.project import Project
from app.models.relationship import (
    Organization,
    RelationshipType,
)
from app.schemas.relationship import (
    CharacterRelationshipCreate,
    OrganizationCreate,
    OrganizationMemberCreate,
    OrganizationUpdate,
)

USER_ID = "user-1"
REPO_BACKEND = Path(__file__).resolve().parents[1]
FRONTEND_LOCALES = REPO_BACKEND.parent / "frontend/src/locales"

CHAR_NOT_IN_PROJECT = "validation.character_not_in_project"
REL_TYPE_NOT_IN_PROJECT = "validation.relationship_type_not_in_project"
ORG_NOT_IN_PROJECT = "validation.organization_not_in_project"
NEW_CODES = (CHAR_NOT_IN_PROJECT, REL_TYPE_NOT_IN_PROJECT, ORG_NOT_IN_PROJECT)


@pytest.fixture
async def db_session():
    """临时文件 SQLite（避免 in-memory 多连接问题），测试后清理。"""
    db_path = f"/tmp/test_same_project_{uuid.uuid4().hex}.db"
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
    req = Request({"type": "http", "method": "POST", "path": "/", "headers": []})
    req.state.user_id = user_id
    return req


@pytest.fixture
async def seed(db_session):
    """项目 A / B 各含角色、组织与项目级关系类型；另有一个系统预置类型。"""
    project_a = Project(id="proj-a", user_id=USER_ID, title="Project A")
    project_b = Project(id="proj-b", user_id=USER_ID, title="Project B")
    char_a = Character(id="char-a", project_id=project_a.id, name="Character A")
    char_a2 = Character(id="char-a2", project_id=project_a.id, name="Character A2")
    char_b = Character(id="char-b", project_id=project_b.id, name="Character B")
    org_char_a = Character(id="org-char-a", project_id=project_a.id, name="Org A", is_organization=True)
    org_char_a2 = Character(id="org-char-a2", project_id=project_a.id, name="Org A2", is_organization=True)
    org_char_a3 = Character(id="org-char-a3", project_id=project_a.id, name="Org A3", is_organization=True)
    org_char_b = Character(id="org-char-b", project_id=project_b.id, name="Org B", is_organization=True)
    org_a = Organization(id="org-a", character_id=org_char_a.id, project_id=project_a.id)
    org_a2 = Organization(id="org-a2", character_id=org_char_a2.id, project_id=project_a.id)
    org_b = Organization(id="org-b", character_id=org_char_b.id, project_id=project_b.id)
    type_a = RelationshipType(id=101, project_id=project_a.id, name="Type A", category="social")
    type_b = RelationshipType(id=102, project_id=project_b.id, name="Type B", category="social")
    system_type = RelationshipType(
        id=103, project_id=None, name="System Type", category="family", is_system=True
    )
    db_session.add_all([
        project_a, project_b, char_a, char_a2, char_b,
        org_char_a, org_char_a2, org_char_a3, org_char_b, org_a, org_a2, org_b,
        type_a, type_b, system_type,
    ])
    await db_session.commit()
    return {
        "char_a": char_a, "char_a2": char_a2, "char_b": char_b,
        "org_char_a": org_char_a, "org_char_a2": org_char_a2, "org_char_b": org_char_b,
        "org_a": org_a, "org_a2": org_a2, "org_b": org_b,
    }


def _relationship(**overrides) -> CharacterRelationshipCreate:
    data = {"project_id": "proj-a", "character_from_id": "char-a", "character_to_id": "char-a2"}
    data.update(overrides)
    return CharacterRelationshipCreate(**data)


# ---------------------------------------------------------------------------
# 错误码注册 + i18n
# ---------------------------------------------------------------------------

def test_new_codes_registered_with_400():
    for code in NEW_CODES:
        assert code in ERROR_REGISTRY, f"{code} 未注册"
        detail, status = ERROR_REGISTRY[code]
        assert detail, f"{code} 缺少默认文案"
        assert status == 400, f"{code} 应为 400（请求内容可自助修正）"


def test_new_codes_have_zh_and_en_locale_entries():
    zh = json.loads((FRONTEND_LOCALES / "zh/errors.json").read_text(encoding="utf-8"))
    en = json.loads((FRONTEND_LOCALES / "en/errors.json").read_text(encoding="utf-8"))
    for code in NEW_CODES:
        leaf = code.split(".", 1)[1]
        assert zh["validation"].get(leaf), f"zh errors.json 缺 {code}"
        assert en["validation"].get(leaf), f"en errors.json 缺 {code}"
        assert zh["validation"][leaf] == ERROR_REGISTRY[code][0], f"{code} zh 与 registry 不一致"


# ---------------------------------------------------------------------------
# create_relationship
# ---------------------------------------------------------------------------

@pytest.mark.anyio
async def test_cross_project_character_from_rejected(db_session, seed):
    with pytest.raises(ApiError) as exc:
        await create_relationship(
            _relationship(character_from_id="char-b"), make_request(), db_session
        )
    assert exc.value.code == CHAR_NOT_IN_PROJECT
    assert exc.value.status == 400


@pytest.mark.anyio
async def test_cross_project_character_to_rejected(db_session, seed):
    with pytest.raises(ApiError) as exc:
        await create_relationship(
            _relationship(character_to_id="char-b"), make_request(), db_session
        )
    assert exc.value.code == CHAR_NOT_IN_PROJECT


@pytest.mark.anyio
async def test_cross_project_relationship_type_rejected(db_session, seed):
    with pytest.raises(ApiError) as exc:
        await create_relationship(
            _relationship(relationship_type_ids=[102]), make_request(), db_session
        )
    assert exc.value.code == REL_TYPE_NOT_IN_PROJECT

    with pytest.raises(ApiError) as exc_singular:
        await create_relationship(
            _relationship(relationship_type_id=102), make_request(), db_session
        )
    assert exc_singular.value.code == REL_TYPE_NOT_IN_PROJECT


@pytest.mark.anyio
async def test_system_preset_type_accepted_across_projects(db_session, seed):
    rel = await create_relationship(
        _relationship(relationship_type_ids=[103]), make_request(), db_session
    )
    assert rel.relationship_type_id == 103


@pytest.mark.anyio
async def test_same_project_relationship_accepted(db_session, seed):
    rel = await create_relationship(
        _relationship(relationship_type_ids=[101]), make_request(), db_session
    )
    assert rel.project_id == "proj-a"
    assert rel.character_from_id == "char-a"
    assert rel.character_to_id == "char-a2"
    assert rel.relationship_type_id == 101


@pytest.mark.anyio
async def test_missing_character_still_404(db_session, seed):
    with pytest.raises(ApiError) as exc:
        await create_relationship(
            _relationship(character_from_id="no-such-char"), make_request(), db_session
        )
    assert exc.value.code == "not_found.relationship_character"
    assert exc.value.status == 404


# ---------------------------------------------------------------------------
# add_organization_member
# ---------------------------------------------------------------------------

@pytest.mark.anyio
async def test_cross_project_org_member_rejected(db_session, seed):
    with pytest.raises(ApiError) as exc:
        await add_organization_member(
            "org-a",
            OrganizationMemberCreate(character_id="char-b", position="Member"),
            make_request(),
            db_session,
        )
    assert exc.value.code == CHAR_NOT_IN_PROJECT


@pytest.mark.anyio
async def test_same_project_org_member_accepted(db_session, seed):
    member = await add_organization_member(
        "org-a",
        OrganizationMemberCreate(character_id="char-a", position="Member"),
        make_request(),
        db_session,
    )
    assert member.organization_id == "org-a"
    assert member.character_id == "char-a"


# ---------------------------------------------------------------------------
# 组织 parent_org_id
# ---------------------------------------------------------------------------

@pytest.mark.anyio
async def test_cross_project_parent_org_rejected_on_create(db_session, seed):
    with pytest.raises(ApiError) as exc:
        await create_organization(
            OrganizationCreate(
                character_id="org-char-a3", project_id="proj-a", parent_org_id="org-b"
            ),
            make_request(),
            db_session,
        )
    assert exc.value.code == ORG_NOT_IN_PROJECT


@pytest.mark.anyio
async def test_same_project_parent_org_accepted_on_create(db_session, seed):
    org = await create_organization(
        OrganizationCreate(
            character_id="org-char-a3", project_id="proj-a", parent_org_id="org-a"
        ),
        make_request(),
        db_session,
    )
    assert org.project_id == "proj-a"
    assert org.parent_org_id == "org-a"


@pytest.mark.anyio
async def test_cross_project_parent_org_rejected_on_update(db_session, seed):
    with pytest.raises(ApiError) as exc:
        await update_organization(
            "org-a", OrganizationUpdate(parent_org_id="org-b"), make_request(), db_session
        )
    assert exc.value.code == ORG_NOT_IN_PROJECT


@pytest.mark.anyio
async def test_same_project_parent_org_accepted(db_session, seed):
    org = await update_organization(
        "org-a", OrganizationUpdate(parent_org_id="org-a2"), make_request(), db_session
    )
    assert org.parent_org_id == "org-a2"


@pytest.mark.anyio
async def test_missing_parent_org_rejected(db_session, seed):
    with pytest.raises(ApiError) as exc:
        await update_organization(
            "org-a", OrganizationUpdate(parent_org_id="no-such-org"), make_request(), db_session
        )
    assert exc.value.code == ORG_NOT_IN_PROJECT
