"""ApiError envelope 基建测试（i18n plan todo 4）。

验证三类全局 handler 的响应契约：
- ApiError → {detail, code, params} + 对应 status
- RequestValidationError → 422, code 取自 Pydantic 错误 type (errors.validation.<type>)
- 未注册异常 → 500 internal.error，不外泄异常内部信息
另验证 SSE send_error 双模式与 BackgroundTask 结构化列。
"""
import json
from typing import Any, Dict, Optional, Tuple

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient
from pydantic import BaseModel

from app.core.errors import (
    DYNAMIC_DETAIL_CODE,
    ERROR_REGISTRY,
    _STATUS_DETAIL_TO_CODE,
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

    @app.get("/probe/http-unknown")
    async def probe_http_unknown():
        raise HTTPException(status_code=418, detail="现场未知文案")

    @app.get("/probe/http-known")
    async def probe_http_known():
        raise HTTPException(status_code=404, detail="章节不存在")

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


def test_status_detail_reverse_map_has_no_collisions():
    """registry 完整性：(status, detail) 反查表不得有撞车。

    _STATUS_DETAIL_TO_CODE 由 dict 推导式建表，两个 code 若共用同一
    (默认 detail, 默认 status)，后注册的会被静默覆盖，导致
    code_for_http_exception 永远归不到它。todo 11 全量扩充码表时此风险
    陡增，故加硬约束；失败信息点名撞车的 (status, detail) 与相关 code。
    """
    # 除 dynamic_detail（不参与反查）外，每个 code 必须占一个唯一键
    assert len(_STATUS_DETAIL_TO_CODE) == len(ERROR_REGISTRY) - 1, (
        "ERROR_REGISTRY 撞车：多个 code 共用同一 (detail, status)，"
        f"反查表 {len(_STATUS_DETAIL_TO_CODE)} 项 < 期望 {len(ERROR_REGISTRY) - 1} 项。"
        f" 撞车项：{_collision_report()}"
    )


def _collision_report() -> str:
    """列出 (status, detail) → [codes] 中 code 数 > 1 的项，供断言失败时定位。"""
    from collections import defaultdict

    buckets: Dict[Tuple[str, int], list] = defaultdict(list)
    for code, (detail, status_code) in ERROR_REGISTRY.items():
        if code == DYNAMIC_DETAIL_CODE:
            continue
        buckets[(status_code, detail)].append(code)
    clashes = {key: codes for key, codes in buckets.items() if len(codes) > 1}
    return json.dumps(
        [{"status": s, "detail": d, "codes": c} for (s, d), c in sorted(clashes.items())],
        ensure_ascii=False,
    ) if clashes else "无（长度差异另有原因）"


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


def test_background_task_data_exposes_structured_columns():
    """_background_task_data 输出透传 status_code/status_params（None-safe）。"""
    from app.api.tasks import _background_task_data
    from app.models.background_task import BackgroundTask

    task = BackgroundTask(
        id="t9", user_id="u", project_id="p", task_type="chapter_generate",
        status="failed", status_code="internal.error", status_params={"reason": "x"},
    )
    data = _background_task_data(task)
    assert data["status_code"] == "internal.error"
    assert data["status_params"] == {"reason": "x"}

    # 空参数容器归一为 None（None-safe），避免前端拿到 {} 误判为已结构化
    empty = _background_task_data(
        BackgroundTask(id="t8", user_id="u", project_id="p", task_type="chapter_generate", status_params={})
    )
    assert empty["status_params"] is None

    legacy = _background_task_data(
        BackgroundTask(id="t7", user_id="u", project_id="p", task_type="chapter_generate")
    )
    assert legacy["status_code"] is None
    assert legacy["status_params"] is None


# ---------------------------------------------------------------------------
# task 14a：双通道 raw 字段 — raw 必须是纯增量（不设置时不出现该键）
# ---------------------------------------------------------------------------

def test_api_error_raw_field_additive():
    """raw 设置时进 envelope；未设置时 envelope 不含 raw 键。"""
    body = ApiError(DYNAMIC_DETAIL_CODE, raw="boom", status=502).to_envelope()
    assert body["raw"] == "boom"
    assert body["code"] == DYNAMIC_DETAIL_CODE

    plain = ApiError("not_found.chapter").to_envelope()
    assert "raw" not in plain
    assert plain == {"detail": "章节不存在", "code": "not_found.chapter", "params": {}}


def test_envelope_helper_raw_additive():
    """模块级 envelope()：传 raw 含 raw 键，不传则省略键（旧精确断言不受影响）。"""
    assert envelope("x", "c", raw="r") == {"detail": "x", "code": "c", "params": {}, "raw": "r"}
    assert "raw" not in envelope("x", "c")
    assert envelope("x", "c", {"a": 1}) == {"detail": "x", "code": "c", "params": {"a": 1}}


def test_http_exception_unknown_detail_carries_raw(client: TestClient):
    """未注册 (status, detail) → fallback code http_error，且 raw = detail（现场诊断）。"""
    resp = client.get("/probe/http-unknown")
    assert resp.status_code == 418
    body = resp.json()
    assert body["code"] == "http_error"
    assert body["detail"] == "现场未知文案"  # 旧字段 byte-identical
    assert body["raw"] == "现场未知文案"


def test_http_exception_registered_detail_has_no_raw(client: TestClient):
    """已注册 (status, detail) → 归码成功，不额外注入 raw 键。"""
    resp = client.get("/probe/http-known")
    assert resp.status_code == 404
    body = resp.json()
    assert body == {"detail": "章节不存在", "code": "not_found.chapter", "params": {}}


def test_unhandled_exception_500_raw_only_in_debug(client: TestClient, monkeypatch):
    """500 兜底：生产不含 raw（原文只进日志）；debug 模式 raw/message 均带原文。"""
    from app.core import errors as errors_module

    monkeypatch.setattr(errors_module, "config_debug", lambda: False)
    body = client.get("/probe/crash").json()
    assert "raw" not in body
    assert "secret internal detail" not in str(body)

    monkeypatch.setattr(errors_module, "config_debug", lambda: True)
    body = client.get("/probe/crash").json()
    assert body["raw"] == "secret internal detail should-not-leak"
    assert body["message"] == "secret internal detail should-not-leak"


def test_not_found_route_registry_entries():
    """main.py 三个裸 404 归码所需 registry 条目。"""
    assert ERROR_REGISTRY["not_found.api_route"] == ("API路径不存在", 404)
    assert ERROR_REGISTRY["not_found.frontend_route"] == ("页面不存在", 404)


@pytest.mark.anyio
async def test_sse_structured_raw_fields():
    """SSE 结构化模式：send_error 带 raw → error_raw；send_progress 带 raw → message_raw。"""
    import json

    from app.utils.sse_response import SSEResponse

    err_raw = json.loads(
        (await SSEResponse.send_error(code="not_found.chapter", params={"chapter_id": "ch-1"}, raw="自定义诊断"))
        .split("data: ", 1)[1].strip()
    )
    assert err_raw["error_code"] == "not_found.chapter"
    assert err_raw["error_raw"] == "自定义诊断"
    assert err_raw["error"] == "章节不存在"  # 旧字段仍回填默认 detail

    # 不带 raw 时无 error_raw 键（纯增量）
    err_plain = json.loads(
        (await SSEResponse.send_error(code="not_found.chapter")).split("data: ", 1)[1].strip()
    )
    assert "error_raw" not in err_plain

    # 旧式位置调用形状完全不变
    legacy = json.loads((await SSEResponse.send_error("旧文案", 500)).split("data: ", 1)[1].strip())
    assert legacy == {"type": "error", "error": "旧文案", "code": 500}

    prog_raw = json.loads(
        (await SSEResponse.send_progress("m", 5, raw="原始诊断")).split("data: ", 1)[1].strip()
    )
    assert prog_raw["message_raw"] == "原始诊断"

    prog_plain = json.loads((await SSEResponse.send_progress("m", 5)).split("data: ", 1)[1].strip())
    assert "message_raw" not in prog_plain


@pytest.mark.anyio
async def test_wizard_tracker_error_widened_signature():
    """tracker.error：带 error_code 走结构化（raw 缺省回填 error_message）；旧位置调用不变。"""
    import json

    from app.utils.sse_response import WizardProgressTracker

    tracker = WizardProgressTracker("测试")

    structured = json.loads(
        (await tracker.error("生成失败", error_code="internal.error", params={"k": 1}))
        .split("data: ", 1)[1].strip()
    )
    assert structured["error_code"] == "internal.error"
    assert structured["error_raw"] == "生成失败"  # raw 缺省时回填 error_message
    assert structured["error"] == "生成失败"

    legacy = json.loads(
        (await tracker.error("旧文案", 418)).split("data: ", 1)[1].strip()
    )
    assert legacy == {"type": "error", "error": "旧文案", "code": 418}


# ---------------------------------------------------------------------------
# i18n todo13 part 2：SSE / task 通道调用点结构化码
# ---------------------------------------------------------------------------

@pytest.mark.anyio
async def test_tracker_error_dual_shape_legacy_plus_structured():
    """tracker.error 结构化模式：旧 (error, code) 字段保留 + error_code/error_params/error_raw 齐备。"""
    from app.utils.sse_response import WizardProgressTracker

    tracker = WizardProgressTracker("测试")

    structured = json.loads(
        (
            await tracker.error(
                f"生成失败: boom", 500,
                error_code="internal.generation_failed", params={"error": "boom"},
            )
        ).split("data: ", 1)[1].strip()
    )
    # 旧字段：error 文案字节不变，code 取 registry 默认 status（T1 语义）
    assert structured["type"] == "error"
    assert structured["error"] == "生成失败: boom"
    assert structured["code"] == 200
    # 结构化字段
    assert structured["error_code"] == "internal.generation_failed"
    assert structured["error_params"] == {"error": "boom"}
    assert structured["error_raw"] == "生成失败: boom"  # raw 缺省回填 error_message


@pytest.mark.anyio
async def test_tracker_warning_retry_structured_additive():
    """tracker.warning/retry 扩展签名：旧 message 文本字节不变，追加 message_code/message_params/message_raw。"""
    from app.utils.sse_response import WizardProgressTracker

    tracker = WizardProgressTracker("测试")

    legacy = json.loads(
        (await tracker.warning("背景受限")).split("data: ", 1)[1].strip()
    )
    assert legacy == {"type": "progress", "message": "⚠️ 背景受限", "progress": 0, "status": "warning"}

    coded = json.loads(
        (
            await tracker.warning(
                "《X》已展开过，已跳过",
                code="progress.outline_expand_skipped", params={"outline_title": "X"},
            )
        ).split("data: ", 1)[1].strip()
    )
    assert coded["message"] == "⚠️ 《X》已展开过，已跳过"  # 旧文案不变
    assert coded["message_code"] == "progress.outline_expand_skipped"
    assert coded["message_params"] == {"outline_title": "X"}
    assert coded["message_raw"] == "《X》已展开过，已跳过"

    legacy_retry = json.loads(
        (await tracker.retry(1, 3, "JSON解析失败")).split("data: ", 1)[1].strip()
    )
    assert legacy_retry == {"type": "progress", "message": "⚠️ JSON解析失败... (1/3)", "progress": 0, "status": "warning"}

    coded_retry = json.loads(
        (
            await tracker.retry(2, 3, "JSON解析失败", code="progress.retry_json_parse")
        ).split("data: ", 1)[1].strip()
    )
    assert coded_retry["message"] == "⚠️ JSON解析失败... (2/3)"  # 旧文案不变
    assert coded_retry["message_code"] == "progress.retry_json_parse"
    assert coded_retry["message_raw"] == "JSON解析失败"


@pytest.mark.anyio
async def test_task_tracker_failure_persists_structured_columns(tmp_path, monkeypatch):
    """TaskProgressTracker.error：中文 status_message 组装字节不变，
    同一行写入结构化 status_code/status_params（动态文案 → task.failed，诊断在 error_message）。"""
    from sqlalchemy import create_engine, select
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

    from app.models.background_task import BackgroundTask
    from app.services import background_task_service as bts

    db_file = tmp_path / "task_channel.db"
    sync_engine = create_engine(f"sqlite:///{db_file}")
    BackgroundTask.__table__.create(sync_engine)

    aengine = create_async_engine(f"sqlite+aiosqlite:///{db_file}")

    async def fake_get_engine(user_id):
        return aengine

    monkeypatch.setattr(bts, "get_engine", fake_get_engine)

    maker = async_sessionmaker(aengine, class_=AsyncSession, expire_on_commit=False)
    async with maker() as session:
        session.add(BackgroundTask(
            id="tt1", user_id="u", project_id="p", task_type="chapter_generate",
            status="running",
        ))
        await session.commit()

    tracker = bts.TaskProgressTracker("tt1", "u", "章节")
    await tracker.error("boom 诊断")

    async with maker() as session:
        row = (
            await session.execute(select(BackgroundTask).where(BackgroundTask.id == "tt1"))
        ).scalar_one()
        assert row.status == "failed"
        assert row.status_message == "失败: boom 诊断"  # 中文组装字节不变
        assert row.error_message == "boom 诊断"  # 诊断原文独立成列
        assert row.status_code == "task.failed"  # 结构化码默认 task.failed
        assert row.status_params == {}


@pytest.mark.anyio
async def test_task_tracker_error_opt_out_legacy_shape(tmp_path, monkeypatch):
    """TaskProgressTracker.error(error_code=None) 显式退出结构化通道：
    旧行为只写 status/error_message/status_message，不写 status_code/status_params。"""
    from sqlalchemy import create_engine, select
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

    from app.models.background_task import BackgroundTask
    from app.services import background_task_service as bts

    db_file = tmp_path / "task_optout.db"
    sync_engine = create_engine(f"sqlite:///{db_file}")
    BackgroundTask.__table__.create(sync_engine)

    aengine = create_async_engine(f"sqlite+aiosqlite:///{db_file}")

    async def fake_get_engine(user_id):
        return aengine

    monkeypatch.setattr(bts, "get_engine", fake_get_engine)

    maker = async_sessionmaker(aengine, class_=AsyncSession, expire_on_commit=False)
    async with maker() as session:
        session.add(BackgroundTask(
            id="tt3", user_id="u", project_id="p", task_type="chapter_generate",
            status="running",
        ))
        await session.commit()

    tracker = bts.TaskProgressTracker("tt3", "u", "章节")
    await tracker.error("boom 诊断", error_code=None)

    async with maker() as session:
        row = (
            await session.execute(select(BackgroundTask).where(BackgroundTask.id == "tt3"))
        ).scalar_one()
        assert row.status == "failed"
        assert row.status_message == "失败: boom 诊断"  # 中文组装字节不变
        assert row.error_message == "boom 诊断"
        assert row.status_code is None  # opt-out：无结构化列写入
        assert row.status_params is None


@pytest.mark.anyio
async def test_task_tracker_warning_complete_structured_path(tmp_path, monkeypatch):
    """TaskProgressTracker.warning/complete 结构化路径：code 设置时同一行写入
    status_code/status_params，旧 status_message 文本字节不变。"""
    from sqlalchemy import create_engine, select
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

    from app.models.background_task import BackgroundTask
    from app.services import background_task_service as bts

    db_file = tmp_path / "task_struct_path.db"
    sync_engine = create_engine(f"sqlite:///{db_file}")
    BackgroundTask.__table__.create(sync_engine)

    aengine = create_async_engine(f"sqlite+aiosqlite:///{db_file}")

    async def fake_get_engine(user_id):
        return aengine

    monkeypatch.setattr(bts, "get_engine", fake_get_engine)

    maker = async_sessionmaker(aengine, class_=AsyncSession, expire_on_commit=False)
    async with maker() as session:
        session.add(BackgroundTask(
            id="tt4", user_id="u", project_id="p", task_type="chapter_generate",
            status="running",
        ))
        await session.commit()

    tracker = bts.TaskProgressTracker("tt4", "u", "章节")
    await tracker.warning("注意点", code="progress.loading", params={"p": 1})

    async with maker() as session:
        row = (
            await session.execute(select(BackgroundTask).where(BackgroundTask.id == "tt4"))
        ).scalar_one()
        assert row.status_message == "⚠️ 注意点"  # 旧文案不变
        assert row.status_code == "progress.loading"
        assert row.status_params == {"p": 1}

    await tracker.complete("搞定", code="progress.done", params={"n": 2})

    async with maker() as session:
        row = (
            await session.execute(select(BackgroundTask).where(BackgroundTask.id == "tt4"))
        ).scalar_one()
        assert row.status == "completed"
        assert row.progress == 100
        assert row.status_message == "搞定"
        assert row.status_code == "progress.done"
        assert row.status_params == {"n": 2}


@pytest.mark.anyio
async def test_cancel_task_writes_structured_code(tmp_path):
    """cancel_task：status_message '任务已取消' 不变，同一行写入 task.cancelled。"""
    from sqlalchemy import create_engine, select
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

    from app.models.background_task import BackgroundTask
    from app.services.background_task_service import BackgroundTaskService

    db_file = tmp_path / "task_cancel.db"
    sync_engine = create_engine(f"sqlite:///{db_file}")
    BackgroundTask.__table__.create(sync_engine)

    aengine = create_async_engine(f"sqlite+aiosqlite:///{db_file}")
    maker = async_sessionmaker(aengine, class_=AsyncSession, expire_on_commit=False)
    async with maker() as session:
        session.add(BackgroundTask(
            id="tt2", user_id="u", project_id="p", task_type="chapter_generate",
            status="running",
        ))
        await session.commit()

    async with maker() as session:
        assert await BackgroundTaskService.cancel_task("tt2", "u", session) is True

    async with maker() as session:
        row = (
            await session.execute(select(BackgroundTask).where(BackgroundTask.id == "tt2"))
        ).scalar_one()
        assert row.status == "cancelled"
        assert row.status_message == "任务已取消"  # 中文文案字节不变
        assert row.status_code == "task.cancelled"
        assert row.status_params == {}
