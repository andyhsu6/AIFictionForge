"""拆书导入角色/组织质量契约测试（issue #172 / #173）。

#172 组织成员恒为 0：模型漏给 `organization_memberships`、或给的组织名对不上时，
旧实现静默 `continue`，`Organization.member_count` 恒为 0 且无任何日志/计数。
#173 角色档案过薄/为空：生成只跑一轮，必填字段（personality / background /
appearance / current_state）为空或过短时没有任何补救，且 current_state 根本没落库。

本文件钉死修复契约：
(a) 模型给出 organization_memberships → 成员关系落库、member_count 正确；
(b) 存在组织但一条成员关系都没解析上 → 触发且只触发一次补全调用；
(c) 补全调用被 `ai.content_filtered` 拦截 → 不重试、不抛异常、原因进统计；
(d) 成员名解析不上 → warning + unresolved 计数（不再静默丢弃）；
(e) 必填字段为空 → 一次补全调用；补全无果 → 记入统计且不阻塞落库；
(f) 一切正常时 AI 调用次数与旧行为一致（回归：不得凭空多一次调用）。

夹具与断言文本全部为中性占位（AGENTS.md 脱敏硬约束）。
"""
from __future__ import annotations

import json
import logging
import os
import uuid
from types import SimpleNamespace

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.core.errors import ApiError
from app.database import Base
from app.models.chapter import Chapter
from app.models.character import Character
from app.models.project import Project
from app.models.relationship import Organization, OrganizationMember
from app.services import book_import_service as bis
from app.services.book_import_service import BookImportService
from app.services.prompt_service import PromptService

PROJECT_ID = "proj-pq-1"
USER_ID = "user-pq-1"
ORG_NAME = "占位组织甲"
CHAR_A = "占位角色甲"
CHAR_B = "占位角色乙"

pytestmark = pytest.mark.anyio

CHARACTER_TEMPLATE = (
    "数量:{count}\n{time_period}|{location}|{atmosphere}|{rules}|{theme}|{genre}\n"
    "要求:{requirements}"
)

# 补全调用与主生成调用的区分标记（与 service 内提示词一致）
_MEMBERSHIP_REPAIR_MARKER = "【组织名单】"
_PROFILE_REPAIR_MARKER = "【待补全实体】"


def _profile_fields(name: str) -> dict:
    """四项必填字段的占位正文（均远超 20 字阈值）。"""
    return {
        "personality": f"{name}的占位性格：沉着克制，遇事先观察再表态，不轻易承诺。",
        "background": f"{name}的占位背景：在占位城镇长大，靠占位手艺谋生，经历过占位变故。",
        "appearance": f"{name}的占位外貌：中等身材，常穿占位式样的深色外衣，随身带占位物件。",
        "current_state": f"{name}的占位处境：暂住占位城镇，等待占位事件的结果，心里没底。",
    }


def _character(name: str, **overrides) -> dict:
    entity = {"name": name, "role_type": "supporting", **_profile_fields(name)}
    entity.update(overrides)
    return entity


def _organization(name: str = ORG_NAME) -> dict:
    entity = {
        "name": name,
        "is_organization": True,
        "role_type": "supporting",
        "organization_type": "占位类型",
        "organization_purpose": "占位目的：维持占位秩序。",
        **_profile_fields(name),
    }
    return entity


@pytest.fixture
async def db_session():
    db_path = f"/tmp/test_book_import_profile_{uuid.uuid4().hex}.db"
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
    """模板与语言解析走桩：本文件只关心生成后体检/补全语义。"""

    async def fake_get_template(name, user_id=None, db=None):
        return CHARACTER_TEMPLATE

    async def fake_generation_language(db, user_id, content_language=None):
        return "zh"

    monkeypatch.setattr(PromptService, "get_template", fake_get_template)
    monkeypatch.setattr(bis, "resolve_user_generation_language", fake_generation_language)


def _install_ai_service(
    monkeypatch, *, generation, membership_repair=None, profile_repair=None
):
    """AI 服务桩：按提示词标记分流主生成 / 两类补全调用，并分别计数。"""
    ai_service = SimpleNamespace(default_model="stub-model")
    calls = {"generation": 0, "membership_repair": 0, "profile_repair": 0}

    async def resolve_budget_chars(model_name=None):
        return 1_000_000

    def _empty(_prompt):
        return []

    async def call_with_json_retry(*, prompt, **kwargs):
        if _MEMBERSHIP_REPAIR_MARKER in prompt:
            calls["membership_repair"] += 1
            return (membership_repair or _empty)(prompt)
        if _PROFILE_REPAIR_MARKER in prompt:
            calls["profile_repair"] += 1
            return (profile_repair or _empty)(prompt)
        calls["generation"] += 1
        return generation(prompt)

    ai_service.resolve_full_book_budget_chars = resolve_budget_chars
    monkeypatch.setattr(ai_service, "call_with_json_retry", call_with_json_retry, raising=False)
    return ai_service, calls


async def _seed(db) -> Project:
    project = Project(
        id=PROJECT_ID,
        user_id=USER_ID,
        title="占位项目",
        genre="通用",
        theme="占位主题",
        world_time_period="占位年代",
        world_location="占位地区",
        character_count=5,
    )
    db.add(project)
    content = "占位章节正文。" + "占位填充内容。" * 50
    db.add(
        Chapter(
            project_id=PROJECT_ID,
            chapter_number=1,
            title="占位章节标题1",
            content=content,
            word_count=len(content),
            status="draft",
        )
    )
    await db.commit()
    return project


async def _run(project, db, ai_service, profile_stats=None) -> int:
    return await BookImportService()._generate_characters_and_organizations_from_project(
        db=db,
        user_id=USER_ID,
        project=project,
        count=5,
        ai_service=ai_service,
        content_language="zh",
        profile_stats=profile_stats,
    )


# --- (a) 模型给出成员关系：落库 + member_count ------------------------------


async def test_memberships_from_model_are_persisted(db_session, patched_prompts, monkeypatch):
    project = await _seed(db_session)

    def generation(_prompt):
        return [
            _organization(),
            _character(
                CHAR_A,
                organization_memberships=[
                    {"organization_name": ORG_NAME, "position": "首领", "rank": 9, "loyalty": 90}
                ],
            ),
            _character(
                CHAR_B,
                organization_memberships=[
                    {"organization_name": ORG_NAME, "position": "成员", "rank": 1, "loyalty": 60}
                ],
            ),
        ]

    ai_service, calls = _install_ai_service(monkeypatch, generation=generation)
    stats: dict = {}

    created = await _run(project, db_session, ai_service, profile_stats=stats)

    assert created == 3
    orgs = (await db_session.execute(select(Organization))).scalars().all()
    assert len(orgs) == 1
    assert orgs[0].member_count == 2
    members = (await db_session.execute(select(OrganizationMember))).scalars().all()
    assert len(members) == 2
    assert {m.position for m in members} == {"首领", "成员"}
    assert stats["memberships_requested"] == 2
    assert stats["memberships_resolved"] == 2
    assert stats["memberships_unresolved"] == 0
    # 关系齐全 → 不需要补全调用
    assert calls["membership_repair"] == 0
    assert stats["membership_repair"] == "not_needed"


# --- (b) 模型漏给成员关系：一次补全调用 -------------------------------------


async def test_missing_memberships_trigger_single_repair(db_session, patched_prompts, monkeypatch):
    project = await _seed(db_session)

    def generation(_prompt):
        return [_organization(), _character(CHAR_A)]

    def membership_repair(prompt):
        assert ORG_NAME in prompt and CHAR_A in prompt
        return [{"character_name": CHAR_A, "organization_name": ORG_NAME, "position": "成员"}]

    ai_service, calls = _install_ai_service(
        monkeypatch, generation=generation, membership_repair=membership_repair
    )
    stats: dict = {}

    created = await _run(project, db_session, ai_service, profile_stats=stats)

    assert created == 2
    assert calls["membership_repair"] == 1
    assert stats["membership_repair"] == "applied:1"
    org = (await db_session.execute(select(Organization))).scalars().one()
    assert org.member_count == 1
    members = (await db_session.execute(select(OrganizationMember))).scalars().all()
    assert [(m.organization_id, m.position) for m in members] == [(org.id, "成员")]
    member_character = (
        await db_session.execute(select(Character).where(Character.id == members[0].character_id))
    ).scalars().one()
    assert member_character.name == CHAR_A
    assert stats["memberships_requested"] == 1
    assert stats["memberships_resolved"] == 1
    assert stats["memberships_unresolved"] == 0


# --- (c) 补全调用被内容审核拦截：不重试、不抛异常 ---------------------------


async def test_membership_repair_content_filtered_aborts_without_retry(
    db_session, patched_prompts, monkeypatch
):
    project = await _seed(db_session)

    def generation(_prompt):
        return [_organization(), _character(CHAR_A)]

    def membership_repair(_prompt):
        raise ApiError(code="ai.content_filtered")

    ai_service, calls = _install_ai_service(
        monkeypatch, generation=generation, membership_repair=membership_repair
    )
    stats: dict = {}

    created = await _run(project, db_session, ai_service, profile_stats=stats)

    # 不抛异常、不重试（max_retries=1 ⇒ 单次尝试），主流程实体照常落库
    assert created == 2
    assert calls["membership_repair"] == 1
    assert stats["membership_repair"] == "aborted:ai.content_filtered"
    org = (await db_session.execute(select(Organization))).scalars().one()
    assert org.member_count == 0


# --- (c2) 补全调用其他异常：记录原因，不拖垮整批落库 -------------------------


async def test_membership_repair_failure_does_not_block_import(
    db_session, patched_prompts, monkeypatch
):
    """补全调用是可选增补：非审核类失败也只记原因，不能让已生成的整批实体落空。"""
    project = await _seed(db_session)

    def generation(_prompt):
        return [_organization(), _character(CHAR_A)]

    def membership_repair(_prompt):
        raise RuntimeError("placeholder upstream failure")

    ai_service, calls = _install_ai_service(
        monkeypatch, generation=generation, membership_repair=membership_repair
    )
    stats: dict = {}

    created = await _run(project, db_session, ai_service, profile_stats=stats)

    assert created == 2
    assert calls["membership_repair"] == 1
    assert stats["membership_repair"] == "aborted:RuntimeError"


# --- (d) 成员名解析不上：warning + unresolved 计数 --------------------------


async def test_unresolved_membership_names_are_logged_and_counted(
    db_session, patched_prompts, monkeypatch, caplog
):
    project = await _seed(db_session)
    missing_org = "占位不存在组织"

    def generation(_prompt):
        return [
            _organization(),
            _character(
                CHAR_A,
                organization_memberships=[{"organization_name": ORG_NAME}],
            ),
            _character(
                CHAR_B,
                organization_memberships=[{"organization_name": missing_org}],
            ),
        ]

    ai_service, calls = _install_ai_service(monkeypatch, generation=generation)
    stats: dict = {}

    with caplog.at_level(logging.WARNING, logger="app.services.book_import_service"):
        created = await _run(project, db_session, ai_service, profile_stats=stats)

    assert created == 3
    org = (await db_session.execute(select(Organization))).scalars().one()
    assert org.member_count == 1
    assert stats["memberships_requested"] == 2
    assert stats["memberships_resolved"] == 1
    assert stats["memberships_unresolved"] == 1
    assert stats["unresolved_membership_names"] == [missing_org]
    assert any(missing_org in record.getMessage() for record in caplog.records)
    # 已有一条可解析 → 不触发补全调用
    assert calls["membership_repair"] == 0


# --- (e) 必填字段为空：一次补全调用 -----------------------------------------


async def test_thin_profile_triggers_single_repair_and_is_persisted(
    db_session, patched_prompts, monkeypatch
):
    project = await _seed(db_session)
    # 老路径契约：档案补全（issue #172/#173）只在旧提示词下存在。
    # issue #190 起默认改走 profile_facts，补全按设计关闭；这里固定回退开关。
    monkeypatch.setattr(bis, "_PROFILE_FACTS_PROMPT_ENABLED", False)

    def generation(_prompt):
        # 四项必填字段全空（模型只给了名字）
        return [{"name": CHAR_A, "role_type": "supporting"}]

    def profile_repair(prompt):
        assert CHAR_A in prompt
        return [{"name": CHAR_A, **_profile_fields(CHAR_A)}]

    ai_service, calls = _install_ai_service(
        monkeypatch, generation=generation, profile_repair=profile_repair
    )
    stats: dict = {}

    created = await _run(project, db_session, ai_service, profile_stats=stats)

    assert created == 1
    assert calls["profile_repair"] == 1
    assert stats["profile_repair"] == "applied:4"
    assert "profiles_still_thin" not in stats
    character = (await db_session.execute(select(Character))).scalars().one()
    assert character.personality == _profile_fields(CHAR_A)["personality"]
    assert character.current_state == _profile_fields(CHAR_A)["current_state"]


async def test_thin_profile_still_thin_is_counted_and_does_not_block(
    db_session, patched_prompts, monkeypatch
):
    project = await _seed(db_session)
    # 老路径契约：档案补全（issue #172/#173）只在旧提示词下存在。
    # issue #190 起默认改走 profile_facts，补全按设计关闭；这里固定回退开关。
    monkeypatch.setattr(bis, "_PROFILE_FACTS_PROMPT_ENABLED", False)

    def generation(_prompt):
        return [{"name": CHAR_A, "role_type": "supporting", "personality": "同上"}]

    def profile_repair(_prompt):
        return []  # 补全无果

    ai_service, calls = _install_ai_service(
        monkeypatch, generation=generation, profile_repair=profile_repair
    )
    stats: dict = {}

    created = await _run(project, db_session, ai_service, profile_stats=stats)

    # 不阻塞落库
    assert created == 1
    assert calls["profile_repair"] == 1
    assert stats["profile_repair"] == "empty"
    assert stats["profiles_thin"] == [CHAR_A]
    assert stats["profiles_still_thin"] == [CHAR_A]
    assert stats["profiles_still_thin_count"] == 1


# --- (f) 回归：无问题时不多发一次调用 ---------------------------------------


async def test_healthy_result_keeps_single_ai_call(db_session, patched_prompts, monkeypatch):
    project = await _seed(db_session)
    # 老路径契约：档案补全（issue #172/#173）只在旧提示词下存在。
    # issue #190 起默认改走 profile_facts，补全按设计关闭；这里固定回退开关。
    monkeypatch.setattr(bis, "_PROFILE_FACTS_PROMPT_ENABLED", False)

    def generation(_prompt):
        return [
            _organization(),
            _character(
                CHAR_A,
                organization_memberships=[{"organization_name": ORG_NAME, "position": "首领"}],
            ),
        ]

    ai_service, calls = _install_ai_service(monkeypatch, generation=generation)
    stats: dict = {}

    created = await _run(project, db_session, ai_service, profile_stats=stats)

    assert created == 2
    assert calls == {"generation": 1, "membership_repair": 0, "profile_repair": 0}
    assert stats["membership_repair"] == "not_needed"
    assert stats["profile_repair"] == "not_needed"
    assert "profiles_thin" not in stats


# --- (g) issue #190：facts 模式——引文校验后渲染落库，补全按设计关闭 ----------


_FACT_QUOTE = "占位章节正文。占位填充内容。"


def test_resolve_profile_fields_quote_hit_and_miss():
    """纯函数层：有效引文渲染落字段，编造引文留空（不阻塞）。"""
    from app.services.profile_facts import resolve_profile_fields

    chapters = {1: "占位章节正文。占位填充内容。占位填充内容。"}
    payload = {
        "name": CHAR_A,
        "profile_facts": [
            {
                "field": "personality",
                "claim": "沉着克制",
                "quote": _FACT_QUOTE,
                "chapter_number": 1,
                "inferred": False,
            },
            {
                "field": "background",
                "claim": "编造的背景",
                "quote": "正文里根本没有出现过的句子",
                "chapter_number": 1,
                "inferred": False,
            },
        ],
    }

    rendered, stats, facts = resolve_profile_fields(payload, chapters)

    assert rendered == {"personality": "沉着克制"}
    # 溯源只保留通过校验的事实
    assert [f["claim"] for f in facts] == ["沉着克制"]
    assert stats["payloads_facts"] == 1
    assert stats["facts_accepted"] == 1
    assert stats["quote_not_found"] == 1
    assert stats["facts_field_personality"] == 1


async def test_profile_facts_are_rendered_and_repair_is_skipped(
    db_session, patched_prompts, monkeypatch
):
    """facts 模式：落库的是渲染结果；补全调用一次都不发（无依据文本不得填空）。"""
    project = await _seed(db_session)

    def generation(_prompt):
        return [
            {
                "name": CHAR_A,
                "role_type": "supporting",
                "profile_facts": [
                    {
                        "field": "personality",
                        "claim": "沉着克制",
                        "quote": _FACT_QUOTE,
                        "chapter_number": 1,
                        "inferred": False,
                    },
                    {
                        "field": "appearance",
                        "claim": "中等身材",
                        "quote": _FACT_QUOTE,
                        "chapter_number": 1,
                        "inferred": False,
                    },
                    {
                        "field": "background",
                        "claim": "编造的背景",
                        "quote": "正文里根本没有出现过的句子",
                        "chapter_number": 1,
                        "inferred": False,
                    },
                ],
            }
        ]

    def _no_repair(_prompt):
        raise AssertionError("facts 模式不应触发档案补全调用")

    ai_service, calls = _install_ai_service(
        monkeypatch, generation=generation, profile_repair=_no_repair
    )
    stats: dict = {}

    created = await _run(project, db_session, ai_service, profile_stats=stats)

    assert created == 1
    assert calls["profile_repair"] == 0
    assert stats["profile_repair"] == "skipped:profile_facts"
    assert stats["profile_facts"]["facts_accepted"] == 2
    assert stats["profile_facts"]["quote_not_found"] == 1

    character = (await db_session.execute(select(Character))).scalars().one()
    assert character.personality == "沉着克制"
    assert character.appearance == "中等身材"
    # 引文不存在 → 该字段留空，不落任何无依据文本
    assert character.background is None
    # 溯源落库：只含 accepted 事实
    assert character.profile_facts is not None
    facts = json.loads(character.profile_facts)
    assert sorted(f["claim"] for f in facts) == ["中等身材", "沉着克制"]
    assert character.profile_facts_updated_at is not None


# issue #210：组织落库前的 personality 剥离（接线层，非 helper 层）
def test_apply_rendered_profile_drops_all_personality_facts_for_organization():
    """一列全是 personality 事实时，溯源不能退回模型原始列表。"""
    from app.services.book_import_service import _apply_rendered_profile

    raw_facts = [{"field": "personality", "claim": "性格甲"}]
    item = {"name": "占位组织", "personality": "性格甲", "profile_facts": raw_facts}
    wrote = _apply_rendered_profile(item, {}, [], True)
    assert wrote is False
    assert "personality" not in item
    assert item["profile_facts"] is None


def test_apply_rendered_profile_keeps_organization_background_facts():
    from app.services.book_import_service import _apply_rendered_profile

    item = {"name": "占位组织", "personality": "性格甲"}
    facts = [
        {"field": "personality", "claim": "性格甲"},
        {"field": "background", "claim": "山门立于城北"},
    ]
    wrote = _apply_rendered_profile(item, {"background": "山门立于城北"}, facts, True)
    assert wrote is True
    assert "personality" not in item
    assert [f["field"] for f in item["profile_facts"]] == ["background"]


def test_apply_rendered_profile_keeps_character_personality():
    from app.services.book_import_service import _apply_rendered_profile

    item = {"name": "占位角色"}
    facts = [{"field": "personality", "claim": "性情沉稳"}]
    wrote = _apply_rendered_profile(item, {"personality": "性情沉稳"}, facts, False)
    assert wrote is True
    assert item["personality"] == "性情沉稳"
    assert [f["field"] for f in item["profile_facts"]] == ["personality"]
