"""计划步骤扁平参数契约回归测试（issue #94）。

背景：用户批准的 10 步计划里 manage_* 步骤的 arguments 用了扁平形状（字段摊在
顶层、没有 data 包裹）。旧实现有两条失败路径：

- update 步骤报告成功（已更新…）但一个字段都没写库（静默 no-op）；
- resolve 步骤因 data 里没有 chapter_number 触发章节守卫，计划中途失败，
  剩余步骤全部 pending。

本文件逐条钉死修复契约：
(a) 扁平字段真的写库，且 updated_at 变化（证明发生了 UPDATE）；
(b) 扁平 chapter_number/resolution_text 的 resolve 成功；
(c) 嵌套 data 路径行为不变（回归，含与扁平等价性对照）；
(d) 无有效字段的 update 必须报错，不得报成功、不得写库；
(e) validate_plan 在提案期就拒绝无法执行的步骤参数（消息指名步骤/工具/字段），
    同时接受归一化后的扁平形状；
(f) propose_plan 描述写明 manage_* + data 约定与示例；
(g) data 内部字段的运行期契约前移到提案期（issue #112）：update 这类严格动作
    在提案期就拒掉运行期必败的键，宽松动作（plant/resolve/abandon/delete）保持
    fail-open，不得误伤。

夹具与断言文本全部为中性占位（AGENTS.md 脱敏硬约束）。
"""
from __future__ import annotations

from datetime import datetime
from types import SimpleNamespace
import os
import uuid

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.database import Base
from app.models.chapter import Chapter
from app.models.character import Character
from app.models.foreshadow import Foreshadow
from app.models.project import Project
from app.models.relationship import Organization, OrganizationMember
from app.services.agent_plan_schema import (
    PROPOSE_PLAN_TOOL_DESCRIPTION,
    PlanValidationError,
    validate_plan,
)
from app.services.project_agent_extended_tools import (
    EXTENDED_TOOL_SPECS,
    ProjectAgentExtendedTools,
)
from app.services.project_agent_operational_tools import OPERATIONAL_TOOL_SPECS
from app.services.project_agent_selectors import find_character, find_foreshadow
from app.services.project_agent_tools import ProjectAgentToolRegistry

PROJECT_ID = "proj-plan-args-1"
FORESHADOW_ID = "a1b2c3d4-e5f6-4a7b-8c9d-000000000101"
TWIN_FORESHADOW_ID = "a1b2c3d4-e5f6-4a7b-8c9d-000000000102"
CHAPTER_40_ID = "c1d2e3f4-a5b6-4c7d-8e9f-000000000040"
OLD_UPDATED_AT = datetime(2020, 1, 2, 3, 4, 5)

# 用户原计划的 s4/s5 扁平形状（占位化）
FLAT_UPDATE_ARGS = {
    "action": "update",
    "foreshadow_id": FORESHADOW_ID,
    "target_resolve_chapter_number": 39,
    "content": "placeholder updated content",
}
FLAT_RESOLVE_ARGS = {
    "action": "resolve",
    "foreshadow_id": FORESHADOW_ID,
    "chapter_number": 40,
    "resolution_text": "placeholder resolution text",
}


@pytest.fixture
async def db_session():
    """临时文件 SQLite（与 test_foreshadow_id_prefix.py 同模式）。"""
    db_path = f"/tmp/test_plan_step_args_{uuid.uuid4().hex}.db"
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
async def seeded(db_session):
    """一个项目：两条同值伏笔（扁平/嵌套对照）+ 第 40 章。"""
    project = Project(id=PROJECT_ID, user_id="user-1", title="placeholder project")
    rows = [
        Foreshadow(
            id=FORESHADOW_ID, project_id=PROJECT_ID,
            title="placeholder foreshadow", content="placeholder old content",
            status="pending", updated_at=OLD_UPDATED_AT,
        ),
        Foreshadow(
            id=TWIN_FORESHADOW_ID, project_id=PROJECT_ID,
            title="placeholder foreshadow twin", content="placeholder old content",
            status="pending", updated_at=OLD_UPDATED_AT,
        ),
    ]
    chapter = Chapter(
        id=CHAPTER_40_ID, project_id=PROJECT_ID, chapter_number=40,
        title="placeholder chapter forty", content="chapter one body text",
        word_count=21, status="draft",
    )
    db_session.add_all([project, *rows, chapter])
    await db_session.commit()
    return project, db_session


async def _reload(db, foreshadow_id: str = FORESHADOW_ID) -> Foreshadow:
    row = await find_foreshadow(db, PROJECT_ID, {"foreshadow_id": foreshadow_id})
    await db.refresh(row)
    return row


def _plan(steps: list[dict]) -> dict:
    return {"objective": "placeholder objective", "steps": steps}


def _registry_definitions() -> list[dict]:
    """registry.definitions() 是模型唯一能看到的面，验收也用它。"""
    registry = ProjectAgentToolRegistry(SimpleNamespace(id="p1"), None)  # 构造不查库
    return registry.definitions()


def _registry_schemas() -> dict[str, dict]:
    return {
        item["function"]["name"]: item["function"]["parameters"]
        for item in _registry_definitions()
    }


# --- (a)/(b) 扁平参数真的写库 -------------------------------------------------


@pytest.mark.anyio
async def test_flat_update_writes_fields_and_bumps_updated_at(seeded):
    """(a) 扁平 update：字段值与 updated_at 必须一起变化（updated_at 证明有 UPDATE）。"""
    project, db = seeded
    tools = ProjectAgentExtendedTools(project, db)

    result = await tools.execute("manage_foreshadow", dict(FLAT_UPDATE_ARGS))

    assert "更新" in result["message"]
    row = await _reload(db)
    assert row.content == "placeholder updated content"
    assert row.target_resolve_chapter_number == 39
    assert row.updated_at is not None and row.updated_at > OLD_UPDATED_AT


@pytest.mark.anyio
async def test_flat_resolve_writes_chapter_and_resolution_text(seeded):
    """(b) 扁平 resolve：chapter_number/resolution_text 必须被采纳。"""
    project, db = seeded
    tools = ProjectAgentExtendedTools(project, db)

    result = await tools.execute("manage_foreshadow", dict(FLAT_RESOLVE_ARGS))

    assert "回收" in result["message"]
    row = await _reload(db)
    assert row.status == "resolved"
    assert row.actual_resolve_chapter_number == 40
    assert row.resolution_text == "placeholder resolution text"


@pytest.mark.anyio
async def test_user_plan_flat_steps_apply_in_sequence(seeded):
    """用户原计划形状：s4 update -> s5 resolve 顺序执行都必须落库。"""
    project, db = seeded
    tools = ProjectAgentExtendedTools(project, db)

    await tools.execute("manage_foreshadow", dict(FLAT_UPDATE_ARGS))
    await tools.execute("manage_foreshadow", dict(FLAT_RESOLVE_ARGS))

    row = await _reload(db)
    assert row.content == "placeholder updated content"
    assert row.target_resolve_chapter_number == 39
    assert row.status == "resolved"
    assert row.actual_resolve_chapter_number == 40
    assert row.resolution_text == "placeholder resolution text"


# --- (c) 嵌套 data 路径回归 ---------------------------------------------------


@pytest.mark.anyio
async def test_nested_data_path_is_unchanged(seeded):
    """(c1) 嵌套 data 调用行为不变。"""
    project, db = seeded
    tools = ProjectAgentExtendedTools(project, db)

    result = await tools.execute("manage_foreshadow", {
        "action": "update", "foreshadow_id": FORESHADOW_ID,
        "data": {"importance": 0.9, "content": "placeholder nested content"},
    })

    assert "更新" in result["message"]
    row = await _reload(db)
    assert row.importance == 0.9
    assert row.content == "placeholder nested content"


@pytest.mark.anyio
async def test_flat_and_nested_payloads_are_equivalent(seeded):
    """(c2) 同值扁平/嵌套调用返回相同的 after 快照（行为等价）。"""
    project, db = seeded
    tools = ProjectAgentExtendedTools(project, db)

    flat = await tools.execute("manage_foreshadow", {
        "action": "update", "foreshadow_id": FORESHADOW_ID,
        "content": "placeholder same content", "importance": 0.7,
    })
    nested = await tools.execute("manage_foreshadow", {
        "action": "update", "foreshadow_id": TWIN_FORESHADOW_ID,
        "data": {"content": "placeholder same content", "importance": 0.7},
    })

    assert flat["after"] == nested["after"]


@pytest.mark.anyio
async def test_nested_empty_data_still_raises(seeded):
    """(c3) 嵌套空 data 与扁平无字段同样必须报错（不得静默成功）。"""
    project, db = seeded
    tools = ProjectAgentExtendedTools(project, db)
    with pytest.raises(ValueError, match="没有检测到需要修改的字段"):
        await tools.execute("manage_foreshadow", {
            "action": "update", "foreshadow_id": FORESHADOW_ID, "data": {},
        })


# --- (d) 空字段 update 必须响亮失败 ------------------------------------------


@pytest.mark.anyio
async def test_update_without_effective_fields_raises_and_writes_nothing(seeded):
    """(d) 没有有效字段的 update：报 preview 同类错误，且不产生任何写入。"""
    project, db = seeded
    tools = ProjectAgentExtendedTools(project, db)

    with pytest.raises(ValueError, match="没有检测到需要修改的字段"):
        await tools.execute("manage_foreshadow", {
            "action": "update", "foreshadow_id": FORESHADOW_ID,
        })

    row = await _reload(db)
    assert row.content == "placeholder old content"
    assert row.updated_at == OLD_UPDATED_AT


# --- (e) validate_plan 提案期校验 ---------------------------------------------


def test_validate_plan_accepts_normalized_flat_steps():
    """(e1) 用户形状（扁平）经归一化后必须通过校验，且 raw arguments 原样透传。"""
    raw_steps = [
        {"id": "s4", "tool": "manage_foreshadow", "arguments": dict(FLAT_UPDATE_ARGS)},
        {"id": "s5", "tool": "manage_foreshadow", "arguments": dict(FLAT_RESOLVE_ARGS)},
    ]
    plan = validate_plan(
        _plan(raw_steps),
        allowed_tools={"manage_foreshadow"},
        tool_schemas=_registry_schemas(),
    )
    assert plan["steps"][0]["arguments"] == FLAT_UPDATE_ARGS
    assert plan["steps"][1]["arguments"] == FLAT_RESOLVE_ARGS


def test_validate_plan_rejects_wrong_typed_tool_field():
    """(e2) 类型错误的字段在提案期被拒，消息指名步骤+工具+字段。"""
    step = {
        "id": "s4", "tool": "manage_foreshadow",
        "arguments": {"action": "update", "foreshadow_id": 12345,
                      "data": {"content": "placeholder content"}},
    }
    with pytest.raises(PlanValidationError) as exc_info:
        validate_plan(_plan([step]), allowed_tools={"manage_foreshadow"},
                      tool_schemas=_registry_schemas())
    message = str(exc_info.value)
    assert "s4" in message
    assert "manage_foreshadow" in message
    assert "foreshadow_id" in message


def test_validate_plan_rejects_missing_required_tool_field():
    """(e3) 缺少工具 schema 必填字段的步骤在提案期被拒，消息指名字段。"""
    step = {
        "id": "s7", "tool": "manage_background_task",
        "arguments": {"action": "cancel"},
    }
    with pytest.raises(PlanValidationError) as exc_info:
        validate_plan(_plan([step]), allowed_tools={"manage_background_task"},
                      tool_schemas=_registry_schemas())
    message = str(exc_info.value)
    assert "s7" in message
    assert "task_id" in message


def test_validate_plan_without_schemas_keeps_shape_checks_only():
    """兼容：未提供 tool_schemas 的旧调用方不因新校验被拒。"""
    step = {
        "id": "s1", "tool": "manage_foreshadow",
        "arguments": {"action": "update", "foreshadow_id": 12345},
    }
    plan = validate_plan(_plan([step]), allowed_tools={"manage_foreshadow"})
    assert plan["steps"][0]["arguments"]["foreshadow_id"] == 12345


def test_validate_plan_accepts_relationship_update_subset():
    """(e4) 嵌套 data 的 required 是 action 作用域：update 只传子集不得被误拒。"""
    step = {
        "id": "s3", "tool": "manage_relationship",
        "arguments": {"action": "update", "relationship_id": "r-1",
                      "data": {"status": "active"}},
    }
    plan = validate_plan(_plan([step]), allowed_tools={"manage_relationship"},
                         tool_schemas=_registry_schemas())
    assert plan["steps"][0]["arguments"]["data"] == {"status": "active"}


def test_tool_parameter_schemas_extracts_declared_parameters():
    """(e5) schema 映射直接来自 registry.definitions()，不是手抄。"""
    from app.services.agent_plan_schema import tool_parameter_schemas

    schemas = tool_parameter_schemas(_registry_definitions())
    assert "manage_foreshadow" in schemas
    assert schemas["manage_foreshadow"]["properties"]["action"]["enum"]
    assert tool_parameter_schemas([{"type": "function", "function": {"name": "x"}}]) == {}


# --- (f) propose_plan 描述文档化 ----------------------------------------------


def test_propose_plan_description_documents_manage_data_convention():
    """(f) 描述必须写明 manage_* + data 约定，并给出一段可读的示例。"""
    description = PROPOSE_PLAN_TOOL_DESCRIPTION
    assert "manage_" in description
    assert '"data"' in description
    assert "arguments" in description or "示例" in description


def test_propose_plan_description_exempts_flat_parameter_manage_tools():
    """(f2) 扁平参数的 manage_* 工具不得被 data 约定套住（issue #110）。

    real-machine 证据：模型按描述把 manage_outline 字段塞进 arguments.data，
    被 validate_plan 拒为「不支持的字段 data」；随后又猜了不存在的 action=update
    （manage_outline 只有 create/delete/reorder），三次重试全部失败、没有计划卡。

    这里从工具 spec 反推「无 data 字段的 manage_* 工具」集合，描述必须逐个点名并
    写明其扁平契约，避免描述与 schema 各自漂移。
    """
    description = PROPOSE_PLAN_TOOL_DESCRIPTION
    advertised_flat = sorted(
        spec["name"]
        for spec in (*EXTENDED_TOOL_SPECS, *OPERATIONAL_TOOL_SPECS)
        if spec["name"].startswith("manage_")
        and spec["name"] in description
        and "data" not in (spec.get("parameters", {}).get("properties") or {})
    )
    assert advertised_flat == ["manage_outline"], (
        "描述点名且扁平参数的 manage_* 工具集合变了；data 约定需要同步调整"
    )
    for name in advertised_flat:
        assert name in description
    assert "顶层参数" in description
    assert "create/delete/reorder" in description
    assert "update_outline" in description


# --- (g) data 字段运行期契约前移到提案期（issue #112）-------------------------


def test_validate_plan_rejects_unsupported_update_data_key():
    """(g1) real-machine 缺陷：update 的 data 里带 chapter_number 运行期必败。

    旧实现只在运行期由 `_manage_foreshadow_update` → `_fields(..., FORESHADOW_FIELDS)`
    拒绝，提案期因为 data 是 additionalProperties:true 而放行，导致用户批准的计划
    在第 1 步就失败。提案期必须先拒，消息指名步骤/工具/字段。
    """
    step = {
        "id": "s1", "tool": "manage_foreshadow",
        "arguments": {
            "action": "update", "foreshadow_id": FORESHADOW_ID,
            "data": {"chapter_number": 41, "target_resolve_chapter_number": 50},
        },
    }
    with pytest.raises(PlanValidationError) as exc_info:
        validate_plan(_plan([step]), allowed_tools={"manage_foreshadow"},
                      tool_schemas=_registry_schemas())
    message = str(exc_info.value)
    assert "s1" in message
    assert "manage_foreshadow" in message
    assert "chapter_number" in message


def test_validate_plan_accepts_supported_update_data_key():
    """(g2) 真实字段 target_resolve_chapter_number 必须继续通过（不得误伤）。"""
    step = {
        "id": "s2", "tool": "manage_foreshadow",
        "arguments": {
            "action": "update", "foreshadow_id": FORESHADOW_ID,
            "data": {"target_resolve_chapter_number": 50},
        },
    }
    plan = validate_plan(_plan([step]), allowed_tools={"manage_foreshadow"},
                         tool_schemas=_registry_schemas())
    assert plan["steps"][0]["arguments"]["data"] == {"target_resolve_chapter_number": 50}


def test_validate_plan_accepts_chapter_number_for_resolve():
    """(g3) resolve 的 chapter_number 是合法上下文键，提案期必须通过（宽松动作）。"""
    step = {
        "id": "s3", "tool": "manage_foreshadow",
        "arguments": {
            "action": "resolve", "foreshadow_id": FORESHADOW_ID,
            "data": {"chapter_number": 40, "resolution_text": "placeholder resolution"},
        },
    }
    plan = validate_plan(_plan([step]), allowed_tools={"manage_foreshadow"},
                         tool_schemas=_registry_schemas())
    assert plan["steps"][0]["arguments"]["data"]["chapter_number"] == 40


@pytest.mark.anyio
async def test_foreshadow_update_data_contract_matches_runtime(seeded):
    """(g4) 提案期与运行期同判：update 带 chapter_number 两边都必须拒绝。"""
    project, db = seeded
    tools = ProjectAgentExtendedTools(project, db)
    payload = {
        "action": "update", "foreshadow_id": FORESHADOW_ID,
        "data": {"chapter_number": 41},
    }
    with pytest.raises(ValueError, match="包含不支持的字段"):
        await tools.preview("manage_foreshadow", dict(payload))
    step = {"id": "s1", "tool": "manage_foreshadow", "arguments": dict(payload)}
    with pytest.raises(PlanValidationError, match="chapter_number"):
        validate_plan(_plan([step]), allowed_tools={"manage_foreshadow"},
                      tool_schemas=_registry_schemas())


@pytest.mark.anyio
async def test_foreshadow_resolve_data_is_lenient(seeded):
    """(g5) 反向守卫：resolve 运行期容忍未知键，提案期也不得误拒（fail-open）。"""
    project, db = seeded
    tools = ProjectAgentExtendedTools(project, db)
    payload = {
        "action": "resolve", "foreshadow_id": FORESHADOW_ID,
        "data": {
            "chapter_number": 40, "resolution_text": "placeholder resolution",
            "extra_context_key": 7,
        },
    }
    await tools.preview("manage_foreshadow", dict(payload))
    step = {"id": "s1", "tool": "manage_foreshadow", "arguments": dict(payload)}
    plan = validate_plan(_plan([step]), allowed_tools={"manage_foreshadow"},
                         tool_schemas=_registry_schemas())
    assert plan["steps"][0]["arguments"]["action"] == "resolve"


# --- (h) 计划步骤按真实执行契约校验（issue #160）-------------------------------

ORG_CHARACTER_ID = "a1b2c3d4-e5f6-4a7b-8c9d-000000000160"


def _org_create_step(step_id: str, data: dict) -> dict:
    return {
        "id": step_id, "tool": "manage_organization",
        "arguments": {"action": "create", "data": data},
    }


def _task_step(step_id: str, action: str, data: dict) -> dict:
    return {
        "id": step_id, "tool": "start_project_task", "action": action,
        "arguments": {"data": data},
    }


def test_validate_plan_rejects_organization_create_without_character_id():
    """(h1) 旧缺陷：create 缺 character_id 提案期放行，批准后第 1 步必败。"""
    step = _org_create_step("s1", {"level": 2, "location": "placeholder location"})
    with pytest.raises(PlanValidationError) as exc_info:
        validate_plan(_plan([step]), allowed_tools={"manage_organization"},
                      tool_schemas=_registry_schemas())
    message = str(exc_info.value)
    assert "s1" in message
    assert "manage_organization" in message
    assert "character_id" in message
    assert "manage_character" in message


def test_validate_plan_rejects_organization_create_with_unsupported_data_key():
    """(h2) 严格集合外的 data 键（如 name）提案期即拒，指名 data.name。"""
    step = _org_create_step("s2", {"character_id": "placeholder-character", "name": "placeholder organization"})
    with pytest.raises(PlanValidationError) as exc_info:
        validate_plan(_plan([step]), allowed_tools={"manage_organization"},
                      tool_schemas=_registry_schemas())
    message = str(exc_info.value)
    assert "s2" in message
    assert "data.name" in message


def test_validate_plan_rejects_task_data_missing_request_fields():
    """(h3) task 的 data 必须满足请求模型：只给 instruction 时提案期就拒。"""
    step = _task_step("s3", "partial_regenerate_chapter",
                      {"instruction": "placeholder instruction"})
    with pytest.raises(PlanValidationError) as exc_info:
        validate_plan(_plan([step]), allowed_tools={"start_project_task"},
                      tool_schemas=_registry_schemas())
    message = str(exc_info.value)
    assert "s3" in message
    assert "start_project_task" in message
    assert "selected_text" in message or "start_position" in message


def test_validate_plan_accepts_task_data_satisfying_request_model():
    """(h4) 反向守卫：给全 PartialRegenerateRequest 必填字段必须通过。"""
    step = _task_step("s4", "partial_regenerate_chapter", {
        "selected_text": "placeholder selected text",
        "start_position": 0,
        "end_position": 5,
        "user_instructions": "placeholder instruction",
    })
    plan = validate_plan(_plan([step]), allowed_tools={"start_project_task"},
                         tool_schemas=_registry_schemas())
    assert plan["steps"][0]["action"] == "partial_regenerate_chapter"
    assert plan["steps"][0]["arguments"]["data"]["selected_text"] == "placeholder selected text"


# --- (i) 写工具 data 契约 + 组织别名（issue #160 追加）------------------------


def _character_create_step(step_id: str, data: dict) -> dict:
    return {
        "id": step_id, "tool": "manage_character",
        "arguments": {"action": "create", "data": data},
    }


def _career_create_step(step_id: str, data: dict) -> dict:
    return {
        "id": step_id, "tool": "manage_career",
        "arguments": {"action": "create", "data": data},
    }


def test_validate_plan_accepts_organization_character_alias_keys():
    """(i1) real-machine 形状：组织角色 create 写 type/purpose，提案期必须放行。

    别名只影响校验与落库字段，raw arguments 原样透传（执行期再归一）。
    """
    step = _character_create_step("s1", {
        "name": "placeholder organization", "is_organization": True,
        "type": "guild", "purpose": "placeholder purpose",
        "level": 2, "location": "placeholder location",
    })
    plan = validate_plan(_plan([step]), allowed_tools={"manage_character"},
                         tool_schemas=_registry_schemas())
    assert plan["steps"][0]["arguments"]["data"]["type"] == "guild"


def test_validate_plan_rejects_character_create_without_name():
    """(i2) 运行期报「创建角色需要 name」，提案期必须先拒且提示字段。"""
    step = _character_create_step("s2", {
        "is_organization": True, "type": "guild",
    })
    with pytest.raises(PlanValidationError) as exc_info:
        validate_plan(_plan([step]), allowed_tools={"manage_character"},
                      tool_schemas=_registry_schemas())
    message = str(exc_info.value)
    assert "s2" in message
    assert "name" in message


def test_validate_plan_rejects_character_create_with_unknown_data_key():
    """(i3) 真正非法键：拒绝并给出可用字段，模型一次重试即可改对。"""
    step = _character_create_step("s3", {
        "name": "placeholder organization", "is_organization": True, "foo": "bar",
    })
    with pytest.raises(PlanValidationError) as exc_info:
        validate_plan(_plan([step]), allowed_tools={"manage_character"},
                      tool_schemas=_registry_schemas())
    message = str(exc_info.value)
    assert "data.foo" in message
    assert "可用字段" in message
    assert "organization_type" in message


def test_validate_plan_rejects_career_create_without_stages():
    """(i4) 运行期报「创建职业需要 name、type 和 stages」，提案期同判。"""
    step = _career_create_step("s4", {"name": "placeholder career", "type": "main"})
    with pytest.raises(PlanValidationError) as exc_info:
        validate_plan(_plan([step]), allowed_tools={"manage_career"},
                      tool_schemas=_registry_schemas())
    assert "stages" in str(exc_info.value)


def test_validate_plan_accepts_relationship_create_and_rejects_unknown_key():
    """(i5) 关系 create 的 character_from_id/character_to_id 合法；data.foo 拒绝。"""
    step = {
        "id": "s5", "tool": "manage_relationship",
        "arguments": {
            "action": "create",
            "data": {
                "character_from_id": "placeholder-character-a",
                "character_to_id": "placeholder-character-b",
                "status": "active",
            },
        },
    }
    plan = validate_plan(_plan([step]), allowed_tools={"manage_relationship"},
                         tool_schemas=_registry_schemas())
    assert plan["steps"][0]["arguments"]["data"]["character_from_id"] == "placeholder-character-a"

    bad_step = {
        "id": "s6", "tool": "manage_relationship",
        "arguments": {
            "action": "create",
            "data": {
                "character_from_id": "placeholder-character-a",
                "character_to_id": "placeholder-character-b",
                "foo": "bar",
            },
        },
    }
    with pytest.raises(PlanValidationError) as exc_info:
        validate_plan(_plan([bad_step]), allowed_tools={"manage_relationship"},
                      tool_schemas=_registry_schemas())
    assert "data.foo" in str(exc_info.value)


def test_career_type_is_not_treated_as_organization_alias():
    """(i7) 回归：manage_career 的 type=main 是合法字段，不得被组织别名改写。"""
    step = _career_create_step("s7", {
        "name": "placeholder career", "type": "main",
        "stages": [{"name": "placeholder stage", "level": 1}],
    })
    plan = validate_plan(_plan([step]), allowed_tools={"manage_career"},
                         tool_schemas=_registry_schemas())
    data = plan["steps"][0]["arguments"]["data"]
    assert data["type"] == "main"
    assert "organization_type" not in data


@pytest.mark.anyio
async def test_character_create_persists_alias_keys_as_organization_fields(seeded):
    """(i6) 运行期别名真的落库：type/purpose -> organization_type/organization_purpose。"""
    project, db = seeded
    tools = ProjectAgentExtendedTools(project, db)

    result = await tools.execute("manage_character", {
        "action": "create",
        "data": {
            "name": "placeholder organization", "is_organization": True,
            "type": "guild", "purpose": "placeholder purpose",
        },
    })

    row = await find_character(db, PROJECT_ID, {"character_id": result["entity_id"]})
    await db.refresh(row)
    assert row.organization_type == "guild"
    assert row.organization_purpose == "placeholder purpose"
    assert row.name == "placeholder organization"


# --- (j) 组织成员 add_member 的角色选择器与 position 缺省（issue #160 追加）------
#
# real-machine 形状：12 步全是
# {"action":"add_member","organization_id":"<org>","member_id":"<character>"}，
# member_id 装的是**角色 ID**、data 整块缺失（position NOT NULL 随之炸），
# 计划第 1 步就失败。这里钉死提案期放行 + 运行期落库。

ORG_ID = "d1e2f3a4-b5c6-4d7e-8f90-0000000001a0"
ORG_ROOT_CHARACTER_ID = "d1e2f3a4-b5c6-4d7e-8f90-0000000001a1"
ORG_MEMBER_CHARACTER_ID = "d1e2f3a4-b5c6-4d7e-8f90-0000000001a2"
EXISTING_MEMBER_ID = "d1e2f3a4-b5c6-4d7e-8f90-0000000001a3"


@pytest.fixture
async def org_seeded(db_session):
    """一个项目：组织角色（含 organization 行）+ 待加入角色 + 一条既有成员关系。"""
    project = Project(id=PROJECT_ID, user_id="user-1", title="placeholder project")
    org_char = Character(
        id=ORG_ROOT_CHARACTER_ID, project_id=PROJECT_ID,
        name="placeholder organization", is_organization=True,
    )
    member_char = Character(
        id=ORG_MEMBER_CHARACTER_ID, project_id=PROJECT_ID,
        name="placeholder member character",
    )
    org = Organization(
        id=ORG_ID, project_id=PROJECT_ID, character_id=org_char.id, member_count=1,
    )
    existing = OrganizationMember(
        id=EXISTING_MEMBER_ID, organization_id=ORG_ID,
        character_id=ORG_ROOT_CHARACTER_ID, position="长老",
    )
    db_session.add_all([project, org_char, member_char, org, existing])
    await db_session.commit()
    return project, db_session


def _add_member_step(step_id: str, arguments: dict) -> dict:
    return {"id": step_id, "tool": "manage_organization", "arguments": dict(arguments)}


async def _organization(db) -> Organization:
    row = (
        await db.execute(select(Organization).where(Organization.id == ORG_ID))
    ).scalar_one()
    await db.refresh(row)
    return row


def test_validate_plan_rejects_add_member_top_level_member_id_without_position():
    """(j1) 顶层 member_id 装角色 ID、无 data：选择器回退仍被识别，但缺 position 必须拒绝。"""
    arguments = {
        "action": "add_member",
        "organization_id": ORG_ID,
        "member_id": ORG_MEMBER_CHARACTER_ID,
    }
    with pytest.raises(PlanValidationError) as exc_info:
        validate_plan(
            _plan([_add_member_step("s1", arguments)]),
            allowed_tools={"manage_organization"},
            tool_schemas=_registry_schemas(),
        )
    message = str(exc_info.value)
    assert "position" in message
    assert "s1" in message


def test_validate_plan_accepts_add_member_data_selector():
    """(j1b) 规范形状 data.character_id / data.character_name + 必填 position 放行。"""
    arguments = {
        "action": "add_member",
        "organization_id": ORG_ID,
        "data": {"character_name": "placeholder member character", "position": "长老"},
    }
    plan = validate_plan(
        _plan([_add_member_step("s1", arguments)]),
        allowed_tools={"manage_organization"},
        tool_schemas=_registry_schemas(),
    )
    assert plan["steps"][0]["arguments"]["data"] == arguments["data"]


def test_validate_plan_rejects_add_member_data_selector_without_position():
    """(j1c) 选择器齐全但缺 data.position：提案期即拒（运行期不再补默认「成员」）。"""
    for data in (
        {"character_id": ORG_MEMBER_CHARACTER_ID},
        {"character_name": "placeholder member character"},
        {"character_id": ORG_MEMBER_CHARACTER_ID, "position": "   "},
    ):
        arguments = {"action": "add_member", "organization_id": ORG_ID, "data": data}
        with pytest.raises(PlanValidationError) as exc_info:
            validate_plan(
                _plan([_add_member_step("s1", arguments)]),
                allowed_tools={"manage_organization"},
                tool_schemas=_registry_schemas(),
            )
        assert "position" in str(exc_info.value)


def test_validate_plan_rejects_add_member_without_character_selector():
    """(j2) 只有 organization_id：提案期即拒，消息指名 character_id（运行期必败）。"""
    arguments = {"action": "add_member", "organization_id": ORG_ID}
    with pytest.raises(PlanValidationError) as exc_info:
        validate_plan(
            _plan([_add_member_step("s1", arguments)]),
            allowed_tools={"manage_organization"},
            tool_schemas=_registry_schemas(),
        )
    message = str(exc_info.value)
    assert "s1" in message
    assert "manage_organization" in message
    assert "character_id" in message


@pytest.mark.anyio
async def test_add_member_without_position_raises_and_writes_nothing(org_seeded):
    """(j3) 运行期：缺 data.position（含顶层 member_id 形状、纯空白）直接 ValueError，不落库。"""
    project, db = org_seeded
    tools = ProjectAgentExtendedTools(project, db)

    for arguments in (
        {
            "action": "add_member",
            "organization_id": ORG_ID,
            "member_id": ORG_MEMBER_CHARACTER_ID,
        },
        {
            "action": "add_member",
            "organization_id": ORG_ID,
            "data": {"character_id": ORG_MEMBER_CHARACTER_ID, "position": "   "},
        },
    ):
        with pytest.raises(ValueError) as exc_info:
            await tools.execute("manage_organization", arguments)
        assert "position" in str(exc_info.value)

    rows = (
        await db.execute(
            select(OrganizationMember).where(
                OrganizationMember.organization_id == ORG_ID,
                OrganizationMember.character_id == ORG_MEMBER_CHARACTER_ID,
            )
        )
    ).scalars().all()
    assert rows == []
    assert (await _organization(db)).member_count == 1


@pytest.mark.anyio
async def test_add_member_with_explicit_position_is_not_overridden(org_seeded):
    """(j4) 显式 position 必须保留，不被缺省值覆盖。"""
    project, db = org_seeded
    tools = ProjectAgentExtendedTools(project, db)

    result = await tools.execute("manage_organization", {
        "action": "add_member",
        "organization_id": ORG_ID,
        "data": {"character_id": ORG_MEMBER_CHARACTER_ID, "position": "长老"},
    })

    row = (
        await db.execute(
            select(OrganizationMember).where(OrganizationMember.id == result["entity_id"])
        )
    ).scalar_one()
    await db.refresh(row)
    assert row.position == "长老"
    assert row.character_id == ORG_MEMBER_CHARACTER_ID


@pytest.mark.anyio
async def test_update_and_remove_member_still_use_member_id(org_seeded):
    """(j5) 回归：update_member/remove_member 的 member_id 仍是成员关系 ID。"""
    project, db = org_seeded
    tools = ProjectAgentExtendedTools(project, db)

    await tools.execute("manage_organization", {
        "action": "update_member",
        "member_id": EXISTING_MEMBER_ID,
        "data": {"position": "首领"},
    })
    row = (
        await db.execute(
            select(OrganizationMember).where(OrganizationMember.id == EXISTING_MEMBER_ID)
        )
    ).scalar_one()
    await db.refresh(row)
    assert row.position == "首领"
    assert row.character_id == ORG_ROOT_CHARACTER_ID

    await tools.execute("manage_organization", {
        "action": "remove_member",
        "member_id": EXISTING_MEMBER_ID,
    })
    remaining = (
        await db.execute(
            select(OrganizationMember).where(OrganizationMember.id == EXISTING_MEMBER_ID)
        )
    ).scalar_one_or_none()
    assert remaining is None





# --- (k) add_member 批量形状（一个组织多名成员一步挂靠）------------------------
#
# 背景：提案期上限 12 步 + 一次 add_member 只能加一个成员，33 名成员必然被切成
# 多批（实测 5 次提案全部恰好 12 步）。这里钉死「一步批量」契约：提案期形状校验、
# 运行期全部或全无、member_count 增量 = 实际新增人数。

BATCH_CHARACTER_IDS = [
    "d1e2f3a4-b5c6-4d7e-8f90-0000000001b1",
    "d1e2f3a4-b5c6-4d7e-8f90-0000000001b2",
    "d1e2f3a4-b5c6-4d7e-8f90-0000000001b3",
]


@pytest.fixture
async def org_batch_seeded(org_seeded):
    """在 org_seeded 之上再加 3 个待挂靠角色（成员/组织/既有成员关系不动）。"""
    project, db = org_seeded
    db.add_all([
        Character(
            id=character_id, project_id=PROJECT_ID,
            name=f"placeholder batch member {index}",
        )
        for index, character_id in enumerate(BATCH_CHARACTER_IDS, start=1)
    ])
    await db.commit()
    return project, db


async def _members_of(db, character_ids: list[str]) -> list[OrganizationMember]:
    return list((await db.execute(
        select(OrganizationMember).where(
            OrganizationMember.organization_id == ORG_ID,
            OrganizationMember.character_id.in_(character_ids),
        )
    )).scalars().all())


def _batch_arguments(members: list[dict]) -> dict:
    return {"action": "add_member", "organization_id": ORG_ID, "data": {"members": members}}


def test_validate_plan_accepts_add_member_batch():
    """(k1) 3 人批量形状提案期放行，raw arguments 原样透传。"""
    arguments = _batch_arguments([
        {"character_id": BATCH_CHARACTER_IDS[0], "position": "长老"},
        {"character_name": "placeholder batch member 2", "position": "弟子"},
        {"character_id": BATCH_CHARACTER_IDS[2], "position": "客卿"},
    ])
    plan = validate_plan(
        _plan([_add_member_step("s1", arguments)]),
        allowed_tools={"manage_organization"},
        tool_schemas=_registry_schemas(),
    )
    assert plan["steps"][0]["arguments"]["data"] == arguments["data"]


def test_validate_plan_rejects_add_member_batch_item_without_position():
    """(k2) 第 2 项缺 position：提案期即拒，消息点名「第 2 项」与 position。"""
    arguments = _batch_arguments([
        {"character_id": BATCH_CHARACTER_IDS[0], "position": "长老"},
        {"character_id": BATCH_CHARACTER_IDS[1]},
        {"character_id": BATCH_CHARACTER_IDS[2], "position": "客卿"},
    ])
    with pytest.raises(PlanValidationError) as exc_info:
        validate_plan(
            _plan([_add_member_step("s1", arguments)]),
            allowed_tools={"manage_organization"},
            tool_schemas=_registry_schemas(),
        )
    message = str(exc_info.value)
    assert "第 2 项" in message
    assert "position" in message
    assert "第 3 项" not in message


def test_validate_plan_rejects_add_member_batch_mixed_with_single_selector():
    """(k3) 批量 + 单条选择器混用：提案期拒绝并说明二选一。"""
    for arguments in (
        {
            "action": "add_member", "organization_id": ORG_ID,
            "member_id": ORG_MEMBER_CHARACTER_ID,
            "data": {"members": [{"character_id": BATCH_CHARACTER_IDS[0], "position": "长老"}]},
        },
        {
            "action": "add_member", "organization_id": ORG_ID,
            "data": {
                "character_id": ORG_MEMBER_CHARACTER_ID,
                "members": [{"character_id": BATCH_CHARACTER_IDS[0], "position": "长老"}],
            },
        },
    ):
        with pytest.raises(PlanValidationError) as exc_info:
            validate_plan(
                _plan([_add_member_step("s1", arguments)]),
                allowed_tools={"manage_organization"},
                tool_schemas=_registry_schemas(),
            )
        message = str(exc_info.value)
        assert "data.members" in message
        assert "二选一" in message


def test_validate_plan_rejects_add_member_batch_item_with_unknown_key():
    """(k4) 元素内白名单外的键提案期就拒（运行期 _fields 必败），并给可用字段。"""
    arguments = _batch_arguments([
        {"character_id": BATCH_CHARACTER_IDS[0], "position": "长老", "foo": "bar"},
    ])
    with pytest.raises(PlanValidationError) as exc_info:
        validate_plan(
            _plan([_add_member_step("s1", arguments)]),
            allowed_tools={"manage_organization"},
            tool_schemas=_registry_schemas(),
        )
    message = str(exc_info.value)
    assert "第 1 项" in message
    assert "data.foo" in message


@pytest.mark.anyio
async def test_add_member_batch_persists_positions_and_member_count(org_batch_seeded):
    """(k5) 批量 3 人：各自 position 落库、member_count 恰好 +3、after.count==3。"""
    project, db = org_batch_seeded
    tools = ProjectAgentExtendedTools(project, db)
    positions = ["长老", "弟子", "客卿"]
    members = [
        {"character_id": character_id, "position": position}
        for character_id, position in zip(BATCH_CHARACTER_IDS, positions)
    ]

    # preview 先跑完整校验且不写行（before 为空、after 报 count），随后 execute 落库。
    preview = await tools.preview("manage_organization", _batch_arguments(members))
    assert preview["changes"]["count"]["after"] == 3
    assert preview["entity_id"] == ORG_ID
    assert (await _organization(db)).member_count == 1

    result = await tools.execute("manage_organization", _batch_arguments(members))

    rows = await _members_of(db, BATCH_CHARACTER_IDS)
    assert {row.character_id: row.position for row in rows} == dict(
        zip(BATCH_CHARACTER_IDS, positions)
    )
    assert result["after"]["count"] == 3
    assert [item["character_id"] for item in result["after"]["members"]] == BATCH_CHARACTER_IDS
    assert result["entity_id"] == ORG_ID
    assert "3 人" in result["message"]
    assert (await _organization(db)).member_count == 4


@pytest.mark.anyio
async def test_add_member_batch_unknown_character_writes_nothing(org_batch_seeded):
    """(k6) 第 2 项角色不存在：preview/execute 都 ValueError，零写入、member_count 不变。"""
    project, db = org_batch_seeded
    tools = ProjectAgentExtendedTools(project, db)
    arguments = _batch_arguments([
        {"character_id": BATCH_CHARACTER_IDS[0], "position": "长老"},
        {"character_id": "d1e2f3a4-b5c6-4d7e-8f90-0000000001ff", "position": "弟子"},
    ])

    with pytest.raises(ValueError, match="第 2 项"):
        await tools.preview("manage_organization", dict(arguments))
    with pytest.raises(ValueError, match="第 2 项"):
        await tools.execute("manage_organization", dict(arguments))

    assert await _members_of(db, BATCH_CHARACTER_IDS) == []
    assert (await _organization(db)).member_count == 1


@pytest.mark.anyio
async def test_add_member_batch_rejects_duplicate_character(org_batch_seeded):
    """(k7) 同一角色在批量内重复：拒绝且零写入。"""
    project, db = org_batch_seeded
    tools = ProjectAgentExtendedTools(project, db)
    arguments = _batch_arguments([
        {"character_id": BATCH_CHARACTER_IDS[0], "position": "长老"},
        {"character_id": BATCH_CHARACTER_IDS[0], "position": "弟子"},
    ])

    with pytest.raises(ValueError, match="重复"):
        await tools.execute("manage_organization", dict(arguments))

    assert await _members_of(db, BATCH_CHARACTER_IDS) == []
    assert (await _organization(db)).member_count == 1


@pytest.mark.anyio
async def test_add_member_batch_rejects_existing_member(org_batch_seeded):
    """(k8) 批量里混进已是成员的角色：沿用「该角色已经是组织成员」，且整批不写。"""
    project, db = org_batch_seeded
    tools = ProjectAgentExtendedTools(project, db)
    arguments = _batch_arguments([
        {"character_id": BATCH_CHARACTER_IDS[0], "position": "长老"},
        {"character_id": ORG_ROOT_CHARACTER_ID, "position": "首领"},
    ])

    with pytest.raises(ValueError, match="该角色已经是组织成员"):
        await tools.execute("manage_organization", dict(arguments))

    assert await _members_of(db, BATCH_CHARACTER_IDS) == []
    assert (await _organization(db)).member_count == 1


@pytest.mark.anyio
async def test_add_member_batch_rejects_mixed_single_selector_at_runtime(org_batch_seeded):
    """(k9) 运行期同样拒绝批量 + 单条选择器混用（不静默择一）。"""
    project, db = org_batch_seeded
    tools = ProjectAgentExtendedTools(project, db)

    with pytest.raises(ValueError, match="二选一"):
        await tools.execute("manage_organization", {
            "action": "add_member", "organization_id": ORG_ID,
            "member_id": ORG_MEMBER_CHARACTER_ID,
            "data": {"members": [{"character_id": BATCH_CHARACTER_IDS[0], "position": "长老"}]},
        })

    assert await _members_of(db, BATCH_CHARACTER_IDS) == []
    assert (await _organization(db)).member_count == 1


# --- (l) 步数上限对齐到 40 -----------------------------------------------------


def _steps_with(count: int) -> list[dict]:
    return [
        {
            "id": f"s{index}", "tool": "manage_foreshadow",
            "arguments": {"action": "update", "foreshadow_id": FORESHADOW_ID},
        }
        for index in range(1, count + 1)
    ]


def test_validate_plan_accepts_forty_steps_and_rejects_forty_one():
    """(l1) 提案期上限 40：40 步通过，41 步拒绝且消息写明上限。"""
    plan = validate_plan(_plan(_steps_with(40)), allowed_tools={"manage_foreshadow"})
    assert len(plan["steps"]) == 40

    with pytest.raises(PlanValidationError, match="40"):
        validate_plan(_plan(_steps_with(41)), allowed_tools={"manage_foreshadow"})


def test_plan_step_limit_is_aligned_across_schema_runner_and_settings():
    """(l2) 三处上限同值：提案期 schema / 执行期 runner / settings 不得各自漂移。"""
    from app.config import settings
    from app.services import agent_plan_runner
    from app.services.agent_plan_schema import MAX_PLAN_STEPS, PLAN_TOOL_PARAMETERS

    assert MAX_PLAN_STEPS == 40
    assert PLAN_TOOL_PARAMETERS["properties"]["steps"]["maxItems"] == 40
    assert agent_plan_runner.MAX_PLAN_STEPS == 40
    assert settings.agent_plan_max_steps == 40
