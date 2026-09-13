"""PR-2a：propose_plan 规划工具 + approve-plan 一次性批准端点。

只读工具/中性夹具：禁止出现任何真实书名、人名、正文片段（AGENTS.md 脱敏硬约束）。
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.services.agent_plan_schema import (
    EXCLUDED_PLAN_TOOLS,
    PROPOSE_PLAN_TOOL_NAME,
    PlanValidationError,
    plannable_tool_names,
    validate_plan,
)


ALLOWED = {"get_project_overview", "list_outlines", "start_project_task"}


def _plan(steps):
    return {"objective": "build three chapters", "steps": steps}


def test_valid_three_step_plan_normalises():
    raw = _plan([
        {"id": "s1", "tool": "start_project_task", "action": "generate_outlines",
         "arguments": {}, "note": None},
        {"id": "s2", "tool": "start_project_task", "action": "expand_outline",
         "arguments": {"outline_id": "o1"}},
        {"id": "s3", "tool": "start_project_task", "action": "generate_chapter",
         "arguments": {"chapter_number": 3}},
    ])
    plan = validate_plan(raw, allowed_tools=ALLOWED)
    assert plan["objective"] == "build three chapters"
    assert [s["id"] for s in plan["steps"]] == ["s1", "s2", "s3"]
    assert plan["steps"][0]["note"] == ""
    assert set(plan["steps"][0]) == {"id", "tool", "action", "arguments", "note"}


@pytest.mark.parametrize("tool", sorted(EXCLUDED_PLAN_TOOLS))
def test_import_tools_are_excluded_from_plan_schema(tool):
    with pytest.raises(PlanValidationError, match="不允许出现在计划中"):
        validate_plan(_plan([{"id": "s1", "tool": tool, "arguments": {}}]),
                      allowed_tools=ALLOWED | {tool})


def test_unknown_tool_rejected_with_readable_message():
    with pytest.raises(PlanValidationError, match="未启用的工具"):
        validate_plan(_plan([{"id": "s1", "tool": "drop_database"}]), allowed_tools=ALLOWED)


def test_duplicate_step_ids_rejected():
    with pytest.raises(PlanValidationError, match="重复"):
        validate_plan(_plan([{"id": "s1", "tool": "list_outlines"},
                             {"id": "s1", "tool": "list_outlines"}]), allowed_tools=ALLOWED)


def test_start_project_task_requires_known_action():
    with pytest.raises(PlanValidationError, match="action"):
        validate_plan(_plan([{"id": "s1", "tool": "start_project_task", "arguments": {}}]),
                      allowed_tools=ALLOWED)
    with pytest.raises(PlanValidationError, match="未知 action"):
        validate_plan(_plan([{"id": "s1", "tool": "start_project_task",
                              "action": "drop_chapters"}]), allowed_tools=ALLOWED)


def test_empty_or_oversized_plan_rejected():
    with pytest.raises(PlanValidationError):
        validate_plan(_plan([]), allowed_tools=ALLOWED)
    with pytest.raises(PlanValidationError, match="上限"):
        validate_plan(_plan([{"id": f"s{i}", "tool": "list_outlines"} for i in range(13)]),
                      allowed_tools=ALLOWED)


def test_plannable_tool_names_drops_propose_plan_and_import_tools():
    definitions = [
        {"type": "function", "function": {"name": "list_outlines"}},
        {"type": "function", "function": {"name": PROPOSE_PLAN_TOOL_NAME}},
        {"type": "function", "function": {"name": "import_outlines_json"}},
    ]
    assert plannable_tool_names(definitions) == {"list_outlines"}


@pytest.mark.anyio
async def test_registry_rejects_preview_and_execute_for_propose_plan():
    """定案的安全网：propose_plan 既不进 preview() 也不进 execute()。"""
    from app.services.project_agent_tools import ProjectAgentToolRegistry

    registry = ProjectAgentToolRegistry(SimpleNamespace(id="p1"), None)  # 构造不查库
    tool = registry.get(PROPOSE_PLAN_TOOL_NAME)
    assert tool.requires_confirmation is False
    assert tool.risk_level == 0
    assert PROPOSE_PLAN_TOOL_NAME in {
        item["function"]["name"] for item in registry.definitions()
    }
    with pytest.raises(ValueError, match="终止型规划工具"):
        await registry.preview(PROPOSE_PLAN_TOOL_NAME, {})
    with pytest.raises(ValueError, match="终止型规划工具"):
        await registry.execute(PROPOSE_PLAN_TOOL_NAME, {})
