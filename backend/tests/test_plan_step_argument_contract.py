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
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.database import Base
from app.models.chapter import Chapter
from app.models.foreshadow import Foreshadow
from app.models.project import Project
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
from app.services.project_agent_selectors import find_foreshadow
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

