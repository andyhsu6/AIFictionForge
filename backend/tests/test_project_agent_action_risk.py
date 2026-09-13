"""PR-1：action 级 risk 表与运行期条件免确认（含 fail-closed）。

本文件只测"策略层"（risk 表、纯函数解析、条件判定），
回合内的事件与持久化在 test_project_agent_inline_task.py 测。
"""
import pytest

from app.models.project import Project
from app.services.project_agent_tools import (
    ProjectAgentTool,
    ProjectAgentToolRegistry,
    action_risk_level,
)

# 11 个 action 必须与 start_project_task 的 enum 一一对应（见 Step 3 的断言）
EXPECTED_ACTION_RISK = {
    "generate_outlines": 2,
    "expand_outline": 2,
    "batch_expand_outlines": 2,
    "generate_chapter": 2,
    "batch_generate_chapters": 2,
    "analyze_chapter": 1,
    "regenerate_chapter": 2,
    "partial_regenerate_chapter": 2,
    "generate_character": 1,
    "generate_organization": 1,
    "generate_careers": 1,
}


def test_start_project_task_action_risk_table_is_exhaustive():
    registry = ProjectAgentToolRegistry(
        Project(id="proj-1", user_id="test", title="测试项目"), None
    )
    tool = registry.get("start_project_task")

    assert tool.action_risk == EXPECTED_ACTION_RISK
    # 顶层 risk 必须仍是 2：OPERATIONAL_WRITE_TOOL_NAMES 由它推导，
    # 改成 <2 会让该工具被当成只读、路由到 operational.read()。
    assert tool.risk_level == 2
    # 新增 action 却没配 risk ⇒ 立刻红（禁止无信息量地滑到默认值）
    assert set(tool.action_risk) == set(tool.parameters["properties"]["action"]["enum"])


def test_read_only_tools_have_no_action_risk():
    registry = ProjectAgentToolRegistry(
        Project(id="proj-1", user_id="test", title="测试项目"), None
    )
    for name in ("list_chapters", "get_chapter_analysis", "update_project"):
        assert registry.get(name).action_risk == {}


def test_action_risk_level_prefers_action_and_falls_back():
    tool = ProjectAgentTool(
        "start_project_task", "d", {}, risk_level=2, action_risk={"analyze_chapter": 1}
    )
    assert action_risk_level(tool, {"action": "analyze_chapter"}) == 1
    assert action_risk_level(tool, {"action": "regenerate_chapter"}) == 2
    assert action_risk_level(tool, {}) == 2
    assert action_risk_level(tool, {"action": None}) == 2
    assert action_risk_level(tool, {"action": 3}) == 2
    plain = ProjectAgentTool("list_chapters", "d", {})
    assert action_risk_level(plain, {"action": "analyze_chapter"}) == 0
