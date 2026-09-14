"""Route-level contract for plan cancellation (PR-3 frontend's only cancel door).

Asserting on the router instead of issuing HTTP keeps this honest without a DB:
the behaviour that matters is (1) the endpoint exists, (2) it delegates to the
runner's cascading cancellation, and (3) it does NOT go through the generic
task-cancel endpoint, which would mark the plan row cancelled before the runner
can record the final step count (see architecture section 3).
"""
import asyncio
import inspect

import pytest
from starlette.requests import Request

from app.api import project_agent
from app.core.errors import ApiError
from app.services import agent_plan_runner as runner


def _routes():
    return {route.path: route for route in project_agent.router.routes if hasattr(route, "methods")}


def _request_for(user_id: str) -> Request:
    request = Request({"type": "http", "method": "POST", "path": "/", "headers": []})
    request.state.user_id = user_id
    return request


def _register_handle(plan_task_id: str, *, user_id: str, project_id: str) -> runner._PlanHandle:
    handle = runner._PlanHandle(
        plan_task_id=plan_task_id,
        user_id=user_id,
        project_id=project_id,
        conversation_id="conv-test",
        steps=[],
    )
    runner._PLAN_HANDLES[plan_task_id] = handle
    return handle


@pytest.fixture
def allow_project_access(monkeypatch):
    async def _allow(project_id, user_id, db):
        return None

    monkeypatch.setattr(project_agent, "verify_project_access", _allow)


def test_plan_cancel_route_exists():
    match = [path for path in _routes() if path.endswith("/agent/plans/{plan_task_id}/cancel")]
    assert match, f"no plan cancel route; paths={[p for p in _routes() if 'plan' in p]}"
    assert "POST" in _routes()[match[0]].methods


def test_plan_cancel_handler_uses_the_cascading_runner_entry_point():
    match = [
        route for route in project_agent.router.routes
        if getattr(route, "path", "").endswith("/agent/plans/{plan_task_id}/cancel")
    ]
    source = inspect.getsource(match[0].endpoint)
    assert "request_plan_cancellation" in source
    assert "cancel_task" not in source, "plan cancel must not reuse the generic task cancel path"


def test_plan_cancel_returns_404_code_when_no_plan_row_matched():
    match = [
        route for route in project_agent.router.routes
        if getattr(route, "path", "").endswith("/agent/plans/{plan_task_id}/cancel")
    ]
    source = inspect.getsource(match[0].endpoint)
    assert "not_found.agent_plan" in source


@pytest.mark.anyio
async def test_plan_cancel_refuses_handle_owned_by_another_user_and_project(allow_project_access):
    """A caller may only stop a plan that belongs to their own project.

    The URL project is authorized on the caller's own project; the runner-side
    owner binding is what stops a foreign plan_task_id from being cancelled
    through an unrelated project the caller happens to own.
    """
    foreign_id = "plan-foreign"
    foreign = _register_handle(foreign_id, user_id="victim-user", project_id="victim-project")
    foreign_task = asyncio.create_task(asyncio.sleep(3600))
    foreign.task = foreign_task
    try:
        with pytest.raises(ApiError) as exc:
            await project_agent.cancel_agent_plan(
                project_id="caller-project",
                plan_task_id=foreign_id,
                request=_request_for("caller-user"),
                db=None,
            )
        assert exc.value.code == "not_found.agent_plan"
        assert foreign.cancel_requested is False
        assert foreign_task.cancelling() == 0
    finally:
        foreign_task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await foreign_task
        runner._PLAN_HANDLES.pop(foreign_id, None)


@pytest.mark.anyio
async def test_plan_cancel_allows_the_owning_project(allow_project_access):
    own_id = "plan-own"
    own = _register_handle(own_id, user_id="caller-user", project_id="caller-project")
    try:
        result = await project_agent.cancel_agent_plan(
            project_id="caller-project",
            plan_task_id=own_id,
            request=_request_for("caller-user"),
            db=None,
        )
        assert result["status"] == "cancelling"
        assert own.cancel_requested is True
    finally:
        runner._PLAN_HANDLES.pop(own_id, None)
