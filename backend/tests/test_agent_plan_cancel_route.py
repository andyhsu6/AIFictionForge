"""Route-level contract for plan cancellation (PR-3 frontend's only cancel door).

Asserting on the router instead of issuing HTTP keeps this honest without a DB:
the behaviour that matters is (1) the endpoint exists, (2) it delegates to the
runner's cascading cancellation, and (3) it does NOT go through the generic
task-cancel endpoint, which would mark the plan row cancelled before the runner
can record the final step count (see architecture section 3).
"""
import inspect

from app.api import project_agent


def _routes():
    return {route.path: route for route in project_agent.router.routes if hasattr(route, "methods")}


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
