"""PR-2c：错误码收口与预算配置面（架构计划 A §3/§4/§7）。"""
from app.core.errors import ERROR_REGISTRY as ERROR_MESSAGES
from app.services import agent_plan_runner as runner


def test_pr2c_error_codes_are_registered_with_final_names():
    # 架构文档的 conflict.agent_plan_state / not_found.agent_run 已按裁定改名，
    # 见 plan-a-pr2c.md「跨 PR 命名裁定」表。禁止再注册第二个同语义码。
    assert "conflict.agent_plan_running" in ERROR_MESSAGES
    assert "conflict.agent_plan_state" not in ERROR_MESSAGES
    assert ERROR_MESSAGES["not_found.agent_plan"] == ("计划任务不存在", 404)
    assert ERROR_MESSAGES["internal.agent_plan_step_failed"][1] == 500
    # PR-2a 的 501 码不得被本 PR 改动
    assert ERROR_MESSAGES["internal.agent_plan_not_available"][1] == 501


def test_step_failure_status_code_switches_to_internal_code():
    handle = runner._PlanHandle(
        plan_task_id="plan-1", user_id="u-1", project_id="p-1",
        conversation_id="c-1", steps=[{"id": "s1", "tool": "get_project_stats"}],
    )
    handle.failed_at_step = 1
    fields = runner._status_fields(handle, "failed", "第 1 步失败")
    assert fields["status_code"] == "internal.agent_plan_step_failed"
    assert fields["status_params"] == {"step": 1, "total": 1}


def test_cancelled_plan_keeps_task_cancelled_code():
    handle = runner._PlanHandle(
        plan_task_id="plan-2", user_id="u-1", project_id="p-1",
        conversation_id="c-1", steps=[],
    )
    handle.failed_at_step = 3          # 取消前已失败过也不得抢走 cancel 语义
    fields = runner._status_fields(handle, "cancelled", "计划已取消")
    assert fields["status_code"] == "task.cancelled"


def test_settings_expose_all_pr2c_plan_keys():
    from app.config import settings

    assert settings.agent_plan_max_steps == 30
    assert settings.agent_plan_wall_clock_seconds == 7200.0
    assert settings.agent_plan_step_poll_timeout_seconds == 900.0
    assert settings.agent_plan_poll_interval_seconds == 2.0
    assert settings.agent_plan_step_grace_seconds == 0.0
    assert settings.agent_plan_status_message_max_chars == 120
    assert settings.agent_plan_summary_max_chars == 8000
    assert settings.agent_plan_running_guardrail_enabled is True


def test_limit_reads_settings_when_constant_untouched(monkeypatch):
    from app.config import settings

    monkeypatch.setattr(settings, "agent_plan_max_steps", 7)
    assert runner._limit("agent_plan_max_steps", runner.MAX_PLAN_STEPS) == 7


def test_patched_module_constant_still_wins(monkeypatch):
    """PR-2b 的用例靠 patch 常量驱动预算，PR-2c 不得把它变成死值。"""
    from app.config import settings

    monkeypatch.setattr(settings, "agent_plan_max_steps", 7)
    monkeypatch.setattr(runner, "MAX_PLAN_STEPS", 3)
    assert runner._limit("agent_plan_max_steps", runner.MAX_PLAN_STEPS) == 3


def test_summary_clip_uses_its_own_key(monkeypatch):
    from app.config import settings

    monkeypatch.setattr(settings, "agent_plan_summary_max_chars", 40)
    assert len(runner._clip({"a": "x" * 500}, runner._limit("agent_plan_summary_max_chars", runner.SUMMARY_MAX_CHARS))) <= 40
