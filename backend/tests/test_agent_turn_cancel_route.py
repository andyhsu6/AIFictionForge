"""创作助手取消接口的路由契约（issue #257）。

断言的是「路由本身」而不是整条 HTTP：关键是 (1) 接口存在且是 POST；
(2) 它走进程内登记表取消，而不是靠连接断开；(3) 没有运行中回合时也能返回
可读结果并触发卡死自愈。
"""
import asyncio

import pytest
from starlette.requests import Request

from app.api import project_agent
from app.services import agent_turn_registry


def _routes():
    return {
        route.path: route
        for route in project_agent.router.routes
        if hasattr(route, "methods")
    }


def _handler():
    match = [
        route
        for route in project_agent.router.routes
        if getattr(route, "path", "").endswith(
            "/agent/conversations/{conversation_id}/cancel"
        )
    ]
    assert match, "创作助手取消路由缺失"
    return match[0]


def _request_for(user_id: str) -> Request:
    request = Request({"type": "http", "method": "POST", "path": "/", "headers": []})
    request.state.user_id = user_id
    return request


@pytest.fixture(autouse=True)
def clean_registry():
    agent_turn_registry.reset_for_tests()
    yield
    agent_turn_registry.reset_for_tests()


@pytest.fixture
def allow_access(monkeypatch):
    async def _allow(project_id, user_id, db):
        return None

    async def _conversation(db, **kwargs):
        return None

    async def _reconcile(db, *, conversation_id, min_age_seconds=0.0):
        return 0

    monkeypatch.setattr(project_agent, "verify_project_access", _allow)
    monkeypatch.setattr(project_agent, "_get_conversation", _conversation)
    monkeypatch.setattr(project_agent, "reconcile_stale_turns", _reconcile)


def test_cancel_route_exists():
    route = _handler()
    assert any(
        path.endswith("/agent/conversations/{conversation_id}/cancel")
        for path in _routes()
    )
    assert "POST" in route.methods


@pytest.mark.anyio
async def test_cancel_route_reports_nothing_to_cancel(allow_access):
    result = await _handler().endpoint(
        "proj-1", "conv-1", _request_for("user-1"), db=None
    )
    assert result["cancelled"] is False
    assert result["repaired_steps"] == 0
    assert result["message"] == "当前没有正在执行的回合"


@pytest.mark.anyio
async def test_cancel_route_requests_registry_cancel(allow_access):
    handle = agent_turn_registry.begin_turn("conv-1")

    async def _finish_later():
        await asyncio.sleep(0.02)
        agent_turn_registry.finish_turn(handle)
        # 路由的 finalize 写完 DB 后才置「已结算」。
        agent_turn_registry.mark_settled(handle)

    finisher = asyncio.create_task(_finish_later())
    try:
        result = await _handler().endpoint(
            "proj-1", "conv-1", _request_for("user-1"), db=None
        )
    finally:
        await finisher

    assert handle.cancelled is True, "取消接口没有置取消标记 ⇒ 回合会继续烧 token"
    assert result["cancelled"] is True
    assert result["settled"] is True
    assert result["message"] == "已请求停止本次执行"
    assert handle.finished_event.is_set()


@pytest.mark.anyio
async def test_chat_stream_emits_cancelled_event_and_finalizes(monkeypatch):
    """取消回合的 SSE 契约：先收尾，再下发 cancelled + done，最后注销登记。"""
    from app.schemas.project_agent import AgentChatRequest
    from app.services.project_agent_service import AgentTurnCancelled

    events: list[str] = []

    async def _fake_detached(**kwargs):
        events.append(
            f"finalize:{kwargs['cancelled']}:{kwargs.get('content')}"
        )

    class _FakeService:
        active_conversation_id = "conv-1"
        active_user_message_id = "um-1"

        def __init__(self, **kwargs):
            pass

        async def stream_chat(self, **kwargs):
            yield {"type": "conversation", "data": {"conversation_id": "conv-1", "title": "t"}}
            raise AgentTurnCancelled()

        async def finalize_interrupted_turn(self, reason, *, cancelled, content=None):
            events.append(f"finalize:{cancelled}:{content}")

        def close_turn(self):
            events.append("close")

        async def release_turn_transaction(self):
            events.append("release-tx")

        def mark_turn_settled(self):
            events.append("settled")

    async def _allow(project_id, user_id, db):
        return None

    monkeypatch.setattr(project_agent, "verify_project_access", _allow)
    monkeypatch.setattr(project_agent, "ProjectAgentService", _FakeService)
    monkeypatch.setattr(
        project_agent, "finalize_interrupted_turn_detached", _fake_detached
    )

    payload = AgentChatRequest(message="帮我查资料")
    response = await project_agent.chat_stream(
        project_id="proj-1",
        payload=payload,
        request=_request_for("user-1"),
        db=None,
        ai_service=None,
    )
    raw = "".join([chunk async for chunk in response.body_iterator])

    assert '"type": "cancelled"' in raw
    assert "本次执行已由用户停止。" in raw
    assert raw.rstrip().endswith("data: [DONE]") or "done" in raw
    # 先释放写事务（否则独立会话自锁），再收尾；收尾必须先于注销登记完成
    # （取消接口靠 finished_event 等 DB 落盘）。
    assert events[0] == "release-tx"
    assert events[1] == "finalize:True:本次执行已由用户停止。"
    assert "settled" in events
    assert events[-1] == "close"
