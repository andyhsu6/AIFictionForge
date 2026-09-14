"""规划步骤的读列表 limit 上限：100 → 500（issue #98）。

背景（真实验收复现）：忠于交接文档的步骤用 `list_foreshadows` 取全量台账时写
`limit=120`，被本仓库自己的校验器以「limit 不能大于 100」拒收 ⇒ 整回合没有计划。
本文件钉三件事：校验器接受 120/500、schema 上限与执行器同源为 500、执行器把
超过上限的输入 clamp 到 500（默认仍是 50）。

只读工具/中性夹具：禁止出现任何真实书名、人名、正文片段（AGENTS.md 脱敏硬约束）。
"""
from __future__ import annotations

import os
import uuid
from types import SimpleNamespace

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.database import Base
from app.models.foreshadow import Foreshadow
from app.models.project import Project
from app.services.agent_plan_schema import (
    PlanValidationError,
    tool_parameter_schemas,
    validate_plan,
)
from app.services.project_agent_tools import ProjectAgentToolRegistry

PROJECT_ID = "proj-plan-cap"
USER_ID = "u-plan-cap"
LEDGER_TOOL = "list_foreshadows"


@pytest.fixture
async def db_engine():
    db_path = f"/tmp/test_plan_limit_cap_{uuid.uuid4().hex}.db"
    engine = create_async_engine(f"sqlite+aiosqlite:///{db_path}")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield engine
    await engine.dispose()
    if os.path.exists(db_path):
        os.remove(db_path)


@pytest.fixture
async def db_session(db_engine):
    Session = async_sessionmaker(bind=db_engine, expire_on_commit=False)
    async with Session() as session:
        yield session


@pytest.fixture
async def ledger_env(db_engine, db_session):
    db_session.add(Project(id=PROJECT_ID, user_id=USER_ID, title="neutral project"))
    db_session.add_all([
        Foreshadow(project_id=PROJECT_ID, title=f"neutral {i}", content="placeholder")
        for i in range(520)
    ])
    await db_session.commit()
    yield SimpleNamespace(
        registry=ProjectAgentToolRegistry(
            Project(id=PROJECT_ID, user_id=USER_ID, title="neutral project"), db_session
        ),
        db=db_session,
    )


def ledger_plan(limit: int, *, step_id: str = "s4") -> dict:
    """忠实形状的台账计划：步骤参数与交接文档同构（只有 limit 不同）。"""
    return {
        "objective": "补齐伏笔台账",
        "steps": [
            {"id": step_id, "tool": LEDGER_TOOL, "arguments": {"limit": limit}},
        ],
    }


def registry_schemas() -> dict[str, dict]:
    registry = ProjectAgentToolRegistry(SimpleNamespace(id="p1"), None)  # 构造不查库
    return tool_parameter_schemas(registry.definitions())


def test_plan_step_limit_accepts_120_and_500_but_rejects_501():
    """(c) 忠实交接形状（limit=120）必须过；500 是上界，501 被点名拒绝。"""
    schemas = registry_schemas()
    for limit in (120, 500):
        plan = validate_plan(
            ledger_plan(limit), allowed_tools={LEDGER_TOOL}, tool_schemas=schemas
        )
        assert plan["steps"][0]["arguments"]["limit"] == limit

    with pytest.raises(PlanValidationError) as exc_info:
        validate_plan(
            ledger_plan(501), allowed_tools={LEDGER_TOOL}, tool_schemas=schemas
        )
    message = str(exc_info.value)
    assert "s4" in message
    assert LEDGER_TOOL in message
    assert "limit" in message
    assert "500" in message


def test_registry_declares_the_raised_limit_cap():
    """schema 是模型唯一能看到的面：上限必须与执行器同源地写进定义。"""
    registry = ProjectAgentToolRegistry(SimpleNamespace(id="p1"), None)
    definitions = {
        item["function"]["name"]: item["function"]["parameters"]
        for item in registry.definitions()
    }
    assert definitions[LEDGER_TOOL]["properties"]["limit"]["maximum"] == 500
    # 默认值不是这次改动的一部分：声明里本来就没有 default，行为默认仍由执行器给 50。
    assert definitions[LEDGER_TOOL]["properties"]["limit"]["minimum"] == 1


@pytest.mark.anyio
async def test_list_foreshadows_clamps_to_the_raised_cap(ledger_env):
    """(d) limit=500 真的取满 500 行；超过上限被 clamp 到 500，不是报错。"""
    at_cap = await ledger_env.registry.execute(LEDGER_TOOL, {"limit": 500})
    assert len(at_cap["items"]) == 500

    over_cap = await ledger_env.registry.execute(LEDGER_TOOL, {"limit": 999999})
    assert len(over_cap["items"]) == 500

    # 默认值 50 不变
    default = await ledger_env.registry.execute(LEDGER_TOOL, {})
    assert len(default["items"]) == 50
