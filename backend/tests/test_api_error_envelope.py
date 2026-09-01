"""ApiError envelope 基建测试（i18n plan todo 4）。

验证三类全局 handler 的响应契约：
- ApiError → {detail, code, params} + 对应 status
- RequestValidationError → 422, code 取自 Pydantic 错误 type (errors.validation.<type>)
- 未注册异常 → 500 internal.error，不外泄异常内部信息
另验证 SSE send_error 双模式与 BackgroundTask 结构化列。
"""
from typing import Any, Dict, Optional

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import BaseModel

from app.core.errors import (
    DYNAMIC_DETAIL_CODE,
    ERROR_REGISTRY,
    ApiError,
    envelope,
    register_exception_handlers,
)


class ProbeBody(BaseModel):
    count: int


@pytest.fixture
def client() -> TestClient:
    """最小 app：装配生产同款全局 handler + 探针路由。"""
    app = FastAPI()
    register_exception_handlers(app)

    @app.get("/probe/api-error")
    async def probe_api_error():
        raise ApiError("not_found.chapter", params={"chapter_id": "ch-1"})

    @app.get("/probe/api-error/override")
    async def probe_api_error_override():
        raise ApiError("custom.dynamic", detail="现场文案", status=409, params={"k": 1})

    @app.post("/probe/validate")
    async def probe_validate(body: ProbeBody):
        return {"ok": True}

    @app.get("/probe/crash")
    async def probe_crash():
        raise ValueError("secret internal detail should-not-leak")

    return TestClient(app, raise_server_exceptions=False)


def test_api_error_envelope_shape(client: TestClient):
    """ApiError → {detail, code, params} envelope，detail 保留 registry 中文默认。"""
    resp = client.get("/probe/api-error")
    assert resp.status_code == 404
    body = resp.json()
    assert body == {"detail": "章节不存在", "code": "not_found.chapter", "params": {"chapter_id": "ch-1"}}


def test_api_error_explicit_override(client: TestClient):
    """显式 detail/status/params 覆盖 registry 默认。"""
    resp = client.get("/probe/api-error/override")
    assert resp.status_code == 409
    assert resp.json() == {"detail": "现场文案", "code": "custom.dynamic", "params": {"k": 1}}


def test_validation_error_maps_pydantic_type_to_code(client: TestClient):
    """RequestValidationError → 422，code = errors.validation.<首条 Pydantic type>。"""
    resp = client.post("/probe/validate", json={"count": "not-an-int"})
    assert resp.status_code == 422
    body = resp.json()
    assert body["detail"] == "请求参数验证失败"
    assert body["code"] == "errors.validation.int_parsing"
    assert "errors" in body  # 旧字段保留


def test_unhandled_exception_500_no_internals_leak(client: TestClient):
    """未捕获异常 → 500 internal.error envelope，响应不含异常原文。"""
    resp = client.get("/probe/crash")
    assert resp.status_code == 500
    body = resp.json()
    assert body["detail"] == "服务器内部错误"
    assert body["code"] == "internal.error"
    assert body["params"] == {}
    assert "secret internal detail" not in resp.text


def test_seed_registry_and_dynamic_marker():
    """种子码表与 dynamic_detail 标记符合计划。"""
    assert ERROR_REGISTRY["auth.unauthorized"] == ("未登录", 401)
    assert ERROR_REGISTRY["not_found.chapter"] == ("章节不存在", 404)
    assert ERROR_REGISTRY["not_found.project"] == ("项目不存在", 404)
    assert ERROR_REGISTRY["not_found.outline"] == ("大纲不存在", 404)
    assert ERROR_REGISTRY["validation.config"] == ("配置数据格式错误", 500)
    assert DYNAMIC_DETAIL_CODE in ERROR_REGISTRY


def test_envelope_helper():
    assert envelope("x", "c", {"a": 1}) == {"detail": "x", "code": "c", "params": {"a": 1}}


@pytest.mark.anyio
async def test_sse_send_error_dual_mode():
    """send_error 结构化模式追加 error_code/error_params 且保留旧 error/code 形状。"""
    import json

    from app.utils.sse_response import SSEResponse

    legacy = await SSEResponse.send_error("旧文案", 500)
    legacy_payload = json.loads(legacy.split("data: ", 1)[1].strip())
    assert legacy_payload == {"type": "error", "error": "旧文案", "code": 500}

    structured = await SSEResponse.send_error(code="not_found.chapter", params={"chapter_id": "ch-1"})
    payload = json.loads(structured.split("data: ", 1)[1].strip())
    assert payload["type"] == "error"
    assert payload["error_code"] == "not_found.chapter"
    assert payload["error_params"] == {"chapter_id": "ch-1"}
    assert payload["error"] == "章节不存在"  # 旧字段回填默认 detail
    assert payload["code"] == 404  # 旧字段回填默认 status


@pytest.mark.anyio
async def test_sse_send_progress_optional_code():
    """send_progress 缺省形状不变；传 code/params 时追加 message_code/message_params。"""
    import json

    from app.utils.sse_response import SSEResponse

    plain = json.loads((await SSEResponse.send_progress("m", 5)).split("data: ", 1)[1].strip())
    assert plain == {"type": "progress", "message": "m", "progress": 5, "status": "processing"}

    coded = json.loads((await SSEResponse.send_progress("m", 5, code="progress.loading", params={"p": 1})).split("data: ", 1)[1].strip())
    assert coded["message_code"] == "progress.loading"
    assert coded["message_params"] == {"p": 1}


def test_background_task_structured_columns(tmp_path):
    """BackgroundTask 新增 status_code/status_params 列，模型可建表读写。"""
    from sqlalchemy import create_engine, inspect
    from sqlalchemy.orm import Session

    from app.database import Base
    from app.models.background_task import BackgroundTask

    db_file = tmp_path / "bt.db"
    engine = create_engine(f"sqlite:///{db_file}")
    BackgroundTask.__table__.create(engine)
    cols = {c["name"] for c in inspect(engine).get_columns("background_tasks")}
    assert {"status_code", "status_params"} <= cols

    with Session(engine) as session:
        task = BackgroundTask(
            id="t1", user_id="u", project_id="p", task_type="chapter_generate",
            status_code="internal.error", status_params={"reason": "x"},
        )
        session.add(task)
        session.commit()
        loaded = session.get(BackgroundTask, "t1")
        assert loaded.status_code == "internal.error"
        assert loaded.status_params == {"reason": "x"}
        assert loaded.status_message is None  # 兼容列保持独立
