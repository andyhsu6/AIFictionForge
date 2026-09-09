"""大纲生成空模型归一化为默认模型的测试（outline-model-400-fix todo 6）。

覆盖：
- _resolve_outline_generation_model 单元：空串/None/空白/缺键 → 默认；显式模型保留
- generate_outline_task 入口：空 model 在 create_task 前已写回默认值（task_input 审计保真）
"""
import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

from app.api import outlines as outlines_module
from app.api.outlines import _resolve_outline_generation_model
from app.services.background_task_service import background_task_service

DEFAULT_MODEL = "deepseek/deepseek-v4-flash"


class _StubService:
    def __init__(self, default_model):
        self.default_model = default_model


def test_resolve_empty_string_falls_back_to_default():
    assert _resolve_outline_generation_model({"model": ""}, _StubService(DEFAULT_MODEL)) == DEFAULT_MODEL


def test_resolve_none_falls_back_to_default():
    assert _resolve_outline_generation_model({"model": None}, _StubService(DEFAULT_MODEL)) == DEFAULT_MODEL


def test_resolve_whitespace_falls_back_to_default():
    assert _resolve_outline_generation_model({"model": "   "}, _StubService(DEFAULT_MODEL)) == DEFAULT_MODEL


def test_resolve_missing_key_falls_back_to_default():
    assert _resolve_outline_generation_model({}, _StubService(DEFAULT_MODEL)) == DEFAULT_MODEL


def test_resolve_explicit_model_preserved():
    assert _resolve_outline_generation_model({"model": "gpt-x"}, _StubService(DEFAULT_MODEL)) == "gpt-x"


def test_resolve_object_without_get_uses_attr():
    obj = SimpleNamespace(model="gpt-x")
    assert _resolve_outline_generation_model(obj, _StubService(DEFAULT_MODEL)) == "gpt-x"


def _patch_task_entry(monkeypatch, captured):
    async def _fake_verify(project_id, user_id, db):
        return SimpleNamespace(id=project_id)

    async def _fake_create_task(*, user_id, project_id, task_type, task_input, db):
        captured["snapshot"] = dict(task_input)
        captured["task_type"] = task_type
        return SimpleNamespace(id="task-1")

    async def _fake_spawn(task_id, user_id, run_fn):
        captured["spawned_task_id"] = task_id

    db = AsyncMock()
    db.execute = AsyncMock(
        return_value=SimpleNamespace(scalars=lambda: SimpleNamespace(all=lambda: []))
    )

    monkeypatch.setattr(outlines_module, "verify_project_access", _fake_verify)
    monkeypatch.setattr(background_task_service, "create_task", _fake_create_task)
    monkeypatch.setattr(background_task_service, "spawn_background_task", _fake_spawn)
    return db


def test_entry_writes_back_default_before_create_task(monkeypatch):
    captured = {}
    db = _patch_task_entry(monkeypatch, captured)
    data = {"project_id": "p1", "mode": "new", "model": ""}
    request = SimpleNamespace(state=SimpleNamespace(user_id="u1"))

    result = asyncio.run(
        outlines_module.generate_outline_task(data, request, db, _StubService(DEFAULT_MODEL))
    )

    assert result["task_id"] == "task-1"
    # create_task 被调用时 task_input 已记录生效模型（写回发生在调用之前）
    assert captured["snapshot"]["model"] == DEFAULT_MODEL
    assert data["model"] == DEFAULT_MODEL


def test_entry_preserves_explicit_model(monkeypatch):
    captured = {}
    db = _patch_task_entry(monkeypatch, captured)
    data = {"project_id": "p1", "mode": "new", "model": "gpt-x"}
    request = SimpleNamespace(state=SimpleNamespace(user_id="u1"))

    asyncio.run(
        outlines_module.generate_outline_task(data, request, db, _StubService(DEFAULT_MODEL))
    )

    assert captured["snapshot"]["model"] == "gpt-x"
