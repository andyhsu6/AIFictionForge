"""`_background_task_data` is the single serializer behind GET /api/tasks.

PR-3 needs `conversation_id` surfaced so the agent panel can attribute a
BACKGROUND_TASK_SETTLED event to the right conversation. These tests call the
serializer directly on an in-memory model instance: no DB, no fixtures, so
they stay honest even when the SQLite fixture set drifts.
"""
from app.api.tasks import _background_task_data
from app.models.background_task import BackgroundTask


def _plan_task(**overrides) -> BackgroundTask:
    base = dict(
        id="plan-1",
        user_id="user-1",
        project_id="proj-1",
        task_type="agent_plan",
        status="running",
        progress=40,
        task_input={
            "tool_call_id": "tc-1",
            "conversation_id": "conv-9",
            "objective": "plan objective",
            "steps": [{"id": "s1", "action": "analyze_chapter"}],
        },
    )
    base.update(overrides)
    return BackgroundTask(**base)


def test_agent_plan_snapshot_exposes_conversation_id():
    data = _background_task_data(_plan_task())
    assert data["conversation_id"] == "conv-9"
    assert data["task_type"] == "agent_plan"


def test_non_plan_task_snapshot_has_null_conversation_id():
    data = _background_task_data(_plan_task(task_type="outline_new", task_input={"action": "generate_outlines"}))
    assert data["conversation_id"] is None


def test_task_input_is_not_leaked_into_the_snapshot():
    data = _background_task_data(_plan_task())
    assert "task_input" not in data


def test_snapshot_tolerates_missing_or_non_dict_task_input():
    assert _background_task_data(_plan_task(task_input=None))["conversation_id"] is None
    assert _background_task_data(_plan_task(task_input="not-a-dict"))["conversation_id"] is None
