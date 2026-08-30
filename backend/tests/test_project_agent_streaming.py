"""创作助手流式化测试（#13 流式化原则）。

最终回答轮（force_answer=True，无工具）必须走流式累积，避免长回复
触发网关 524；工具决策轮（有工具）保持非流式，因为工具路由必须经
agent 自己的 registry 执行。
"""
import pytest

from app.models.project import Project
from app.services.project_agent_service import ProjectAgentService


class _FakeAI:
    """记录两种调用方式的假 AI 服务。"""

    def __init__(self):
        self.stream_full_calls = 0
        self.gen_calls = 0
        self.gen_kwargs = None

    async def generate_text_stream_full(self, **kwargs):
        self.stream_full_calls += 1
        return {"content": "流式回答", "tool_calls": None, "finish_reason": "stop", "usage": {}}

    async def generate_text(self, **kwargs):
        self.gen_calls += 1
        self.gen_kwargs = kwargs
        return {"content": "", "tool_calls": [], "finish_reason": "stop", "usage": {}}


def _make_service(fake) -> ProjectAgentService:
    project = Project(id="proj-1", user_id="test", title="测试项目")
    return ProjectAgentService(db=None, ai_service=fake, project=project, user_id="test")


@pytest.mark.anyio
async def test_force_answer_round_uses_streaming():
    """最终回答轮必须走流式累积。"""
    fake = _FakeAI()
    svc = _make_service(fake)

    resp = await svc._call_round(
        prompt="总结项目", system_prompt="sys", force_answer=True,
        available_tools=[{"function": {"name": "get_characters"}}],
    )

    assert fake.stream_full_calls == 1
    assert fake.gen_calls == 0
    assert resp["content"] == "流式回答"


@pytest.mark.anyio
async def test_tool_round_uses_non_streaming():
    """工具决策轮必须保持非流式（工具由 agent registry 路由）。"""
    fake = _FakeAI()
    svc = _make_service(fake)

    resp = await svc._call_round(
        prompt="查询角色", system_prompt="sys", force_answer=False,
        available_tools=[{"function": {"name": "get_characters"}}],
    )

    assert fake.gen_calls == 1
    assert fake.stream_full_calls == 0
    assert fake.gen_kwargs is not None
    assert fake.gen_kwargs["tools"] == [{"function": {"name": "get_characters"}}]  # 工具传回 AI
