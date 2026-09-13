"""PR-2c：错误码收口与预算配置面（架构计划 A §3/§4/§7）。"""
import pytest

from app.core.errors import ERROR_REGISTRY as ERROR_MESSAGES
from app.services import agent_plan_runner as runner


@pytest.fixture
def env_factory():
    return None


def test_pr2c_error_codes_are_registered_with_final_names():
    # 架构文档的 conflict.agent_plan_state / not_found.agent_run 已按裁定改名，
    # 见 plan-a-pr2c.md「跨 PR 命名裁定」表。禁止再注册第二个同语义码。
    assert "conflict.agent_plan_running" in ERROR_MESSAGES
    assert "conflict.agent_plan_state" not in ERROR_MESSAGES
    assert ERROR_MESSAGES["not_found.agent_plan"] == ("计划任务不存在", 404)
    assert ERROR_MESSAGES["internal.agent_plan_step_failed"][1] == 500
    # PR-2a 的 501 码不得被本 PR 改动
    assert ERROR_MESSAGES["internal.agent_plan_not_available"][1] == 501


def test_step_failure_status_code_switches_to_internal_code(env_factory):
    handle = runner._PlanHandle(
        plan_task_id="plan-1", user_id="u-1", project_id="p-1",
        conversation_id="c-1", steps=[{"id": "s1", "tool": "get_project_stats"}],
    )
    handle.failed_at_step = 1
    fields = runner._status_fields(handle, "failed", "第 1 步失败")
    assert fields["status_code"] == "internal.agent_plan_step_failed"
    assert fields["status_params"] == {"step": 1, "total": 1}


def test_cancelled_plan_keeps_task_cancelled_code(env_factory):
    handle = runner._PlanHandle(
        plan_task_id="plan-2", user_id="u-1", project_id="p-1",
        conversation_id="c-1", steps=[],
    )
    handle.failed_at_step = 3          # 取消前已失败过也不得抢走 cancel 语义
    fields = runner._status_fields(handle, "cancelled", "计划已取消")
    assert fields["status_code"] == "task.cancelled"
