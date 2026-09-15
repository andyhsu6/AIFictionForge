"""issue #33 残留中文 i18n 回归。

覆盖四类残留（失败步骤标签、任务 error、向导 tracker 阶段码、取消响应）中
可后端验证的部分：
1. 任务状态轮询响应携带 error_code/error_params；error 原文（zh 逐字节）不变；
   无码写入清空两字段（last-state 语义）。
2. _run_pipeline 章节识别失败写结构化 error_code，error 文案仍是原文。
3. 向导 tracker 阶段方法 code+params：无 message 默认自动走 progress.wizard.*，
   显式文案站透传 code，无 code 时 payload 形状与旧版一致。
4. wizard_stream.py 全部带显式 message 的阶段调用都带 code。
5. 全部 progress.wizard.* 码在 registry + zh/en errors.json 注册，zh 与模板字节一致，
   en 不含中文。
6. cancel_task 返回带码 payload，zh message 逐字节不变。
7. 拆书失败步骤 payload 保留稳定 step_name 键。
"""
import ast
import asyncio
import json
import re
from pathlib import Path

from app.core.errors import ERROR_REGISTRY
from app.services import book_import_service as bis
from app.services.book_import_service import BookImportService, _BookImportTask
from app.utils.sse_response import WizardProgressTracker

BACKEND_DIR = Path(__file__).resolve().parents[1]
REPO_ROOT = BACKEND_DIR.parent
FRONTEND_LOCALES = REPO_ROOT / "frontend" / "src" / "locales"

CJK = re.compile(r"[\u4e00-\u9fff\u3400-\u4dbf]")
WIZARD_CODE_RE = re.compile(r'"(progress\.wizard\.[A-Za-z0-9]+)"')


def _new_task(task_id: str, status: str = "pending") -> _BookImportTask:
    return _BookImportTask(
        task_id=task_id,
        user_id="user-1",
        filename="book.txt",
        project_id=None,
        create_new_project=True,
        import_mode="append",
        status=status,
    )


def _flatten(d: dict, prefix: str = "") -> dict:
    out: dict = {}
    for k, v in d.items():
        if isinstance(v, dict):
            out.update(_flatten(v, prefix=f"{prefix}{k}."))
        else:
            out[f"{prefix}{k}"] = v
    return out


def _payload(sse: str) -> dict:
    return json.loads(sse.split("data: ", 1)[1].strip())


# ---------------------------------------------------------------------------
# Category 2 — task error code
# ---------------------------------------------------------------------------

def test_set_task_state_error_code_roundtrip_and_clear():
    svc = BookImportService()
    task = _new_task("t-error-code")
    svc._set_task_state(
        task, status="failed", progress=5,
        message="解析失败",
        error="未能识别到有效章节，请检查TXT内容",
        code="import.task.parseFailed",
        error_code="import.task.noChaptersDetected",
        error_params={"k": 1},
    )
    assert task.error == "未能识别到有效章节，请检查TXT内容"
    assert task.error_code == "import.task.noChaptersDetected"
    assert task.error_params == {"k": 1}
    # 无码写入清空（last-state 语义）
    svc._set_task_state(task, status="running", progress=10, message="x")
    assert task.error_code is None
    assert task.error_params is None


def test_get_task_status_carries_error_code():
    svc = BookImportService()
    task = _new_task("t-poll-error")
    svc._tasks[task.task_id] = task
    svc._set_task_state(
        task, status="failed", progress=0,
        message="解析失败",
        error="未能识别到有效章节，请检查TXT内容",
        code="import.task.parseFailed",
        error_code="import.task.noChaptersDetected",
        error_params=None,
    )
    resp = asyncio.run(svc.get_task_status(task_id=task.task_id, user_id=task.user_id))
    assert resp.error == "未能识别到有效章节，请检查TXT内容"
    assert resp.error_code == "import.task.noChaptersDetected"
    assert resp.error_params is None
    assert resp.status_code == "import.task.parseFailed"


def test_run_pipeline_no_chapters_sets_error_code(monkeypatch):
    svc = BookImportService()
    task = _new_task("t-no-chapters")
    svc._tasks[task.task_id] = task
    monkeypatch.setattr(bis.txt_parser_service, "decode_bytes", lambda content: ("text", "utf-8"))
    monkeypatch.setattr(bis.txt_parser_service, "clean_text", lambda text: text)
    monkeypatch.setattr(bis.txt_parser_service, "split_chapters", lambda text: [])

    asyncio.run(svc._run_pipeline(task_id=task.task_id, file_content=b"x"))

    assert task.status == "failed"
    assert task.message == "解析失败"
    assert task.status_code == "import.task.parseFailed"
    assert task.error == "未能识别到有效章节，请检查TXT内容"
    assert task.error_code == "import.task.noChaptersDetected"


# ---------------------------------------------------------------------------
# Category 3 — wizard tracker stage codes
# ---------------------------------------------------------------------------

def test_tracker_default_stage_calls_auto_assign_codes():
    tracker = WizardProgressTracker("世界观")
    start = _payload(asyncio.run(tracker.start()))
    assert start["message"] == "开始生成世界观..."
    assert start["message_code"] == "progress.wizard.start"
    assert start["message_params"] == {"stage": "世界观"}
    assert start["message_raw"] == "开始生成世界观..."

    gen = _payload(asyncio.run(tracker.generating(current_chars=10, estimated_total=100)))
    assert gen["message"] == "生成世界观中... (10字符)"
    assert gen["message_code"] == "progress.wizard.generating"
    assert gen["message_params"] == {"stage": "世界观", "current_chars": 10, "retry_suffix": ""}

    done = _payload(asyncio.run(tracker.complete()))
    assert done["message"] == "世界观生成完成!"
    assert done["message_code"] == "progress.wizard.complete"
    assert done["message_params"] == {"stage": "世界观"}


def test_tracker_explicit_stage_codes_passthrough():
    tracker = WizardProgressTracker("角色")
    saved = _payload(asyncio.run(tracker.saving("保存角色到数据库...", code="progress.wizard.savingCharacters")))
    assert saved["message"] == "保存角色到数据库..."
    assert saved["message_code"] == "progress.wizard.savingCharacters"
    assert saved["message_raw"] == "保存角色到数据库..."

    retried = _payload(asyncio.run(tracker.generating(
        current_chars=0, estimated_total=100,
        message="重新生成世界观", retry_count=1, max_retries=3,
        code="progress.wizard.regeneratingWorld",
    )))
    assert retried["message"] == "重新生成世界观 (重试 1/3)"
    assert retried["message_code"] == "progress.wizard.regeneratingWorld"
    assert retried["message_params"]["retry_suffix"] == " (重试 1/3)"


def test_wizard_zh_templates_reproduce_messages_byte_for_byte():
    """zh 渲染自证：注册模板按 params 代入后与运行时 message 逐字节一致。"""
    zh = _flatten(json.loads((FRONTEND_LOCALES / "zh" / "errors.json").read_text(encoding="utf-8")))

    def render(template: str, params: dict) -> str:
        out = template
        for key, value in params.items():
            out = out.replace("{{" + key + "}}", str(value))
        return out

    cases = [
        ("progress.wizard.start", "开始生成世界观...", {"stage": "世界观"}),
        ("progress.wizard.generating", "生成世界观中... (10字符)", {"stage": "世界观", "current_chars": 10, "retry_suffix": ""}),
        ("progress.wizard.generating", "生成世界观中... (10字符) (重试 1/3)", {"stage": "世界观", "current_chars": 10, "retry_suffix": " (重试 1/3)"}),
        ("progress.wizard.complete", "世界观生成完成!", {"stage": "世界观"}),
        ("progress.wizard.regeneratingWorld", "重新生成世界观 (重试 2/3)", {"retry_suffix": " (重试 2/3)"}),
        ("progress.wizard.charactersBatch", "生成第1/2批角色 (5个) (重试 1/3)", {"batch": 1, "total": 2, "batch_size": 5, "retry_suffix": " (重试 1/3)"}),
        ("progress.wizard.charactersBatchGenerating", "生成第1/2批角色中", {"batch": 1, "total": 2, "retry_suffix": ""}),
        ("progress.wizard.preparingOutlines", "准备生成3个大纲节点...", {"outline_count": 3}),
        ("progress.wizard.cleanedReferences", "已清理2个无效引用", {"cleaned": 2}),
        ("progress.wizard.autoCreatedCharacters", "🎭 自动创建了 2 个角色: A, B", {"created": 2, "names": "A, B"}),
        ("progress.wizard.charactersBatchMismatch", "批次1生成数量不正确: 期望5个, 实际3个", {"batch": 1, "expected": 5, "actual": 3}),
    ]
    for code, message, params in cases:
        assert render(zh[code], params) == message, f"{code} zh 模板代入后与原文案不一致"


def test_tracker_no_code_keeps_legacy_payload_shape():
    tracker = WizardProgressTracker("测试")
    assert _payload(asyncio.run(tracker.start("自定义开始"))) == {
        "type": "progress", "message": "自定义开始", "progress": 0, "status": "processing",
    }
    legacy_parsing = _payload(asyncio.run(tracker.parsing("解析数据...")))
    assert "message_code" not in legacy_parsing
    assert legacy_parsing["message"] == "解析数据..."


def test_wizard_stream_explicit_stage_calls_all_carry_codes():
    src = (BACKEND_DIR / "app" / "api" / "wizard_stream.py").read_text(encoding="utf-8")
    tree = ast.parse(src)
    stages = {"start", "loading", "preparing", "generating", "parsing", "saving", "complete"}
    missing: list[tuple[int, str]] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if not (isinstance(func, ast.Attribute) and func.attr in stages):
            continue
        if not (isinstance(func.value, ast.Name) and func.value.id == "tracker"):
            continue
        segment = ast.get_source_segment(src, node) or ""
        has_message = any(kw.arg == "message" for kw in node.keywords)
        if has_message and "code=" not in segment:
            missing.append((node.lineno, func.attr))
    assert missing == [], f"显式 message 的阶段调用缺少 code: {missing}"


def _emitted_wizard_codes() -> list[str]:
    sources = [
        BACKEND_DIR / "app" / "api" / "wizard_stream.py",
        BACKEND_DIR / "app" / "utils" / "sse_response.py",
    ]
    codes: set[str] = set()
    for src in sources:
        codes |= set(WIZARD_CODE_RE.findall(src.read_text(encoding="utf-8")))
    return sorted(codes)


def test_all_wizard_codes_registered_and_localized():
    codes = _emitted_wizard_codes()
    assert len(codes) >= 35, f"应至少 35 个向导阶段码，实际 {len(codes)}"

    zh = _flatten(json.loads((FRONTEND_LOCALES / "zh" / "errors.json").read_text(encoding="utf-8")))
    en = _flatten(json.loads((FRONTEND_LOCALES / "en" / "errors.json").read_text(encoding="utf-8")))

    for code in codes:
        assert code in ERROR_REGISTRY, f"{code} 未注册到 ERROR_REGISTRY"
        template, status = ERROR_REGISTRY[code]
        assert status == 200, f"{code} status 应为 200"
        assert zh.get(code) == template, f"{code} zh locale 与 registry 模板不一致"
        assert en.get(code, "").strip(), f"{code} en locale 缺失或为空"
        assert not CJK.search(en[code]), f"{code} en locale 含中文: {en[code]}"


# ---------------------------------------------------------------------------
# Category 4 — cancel responses
# ---------------------------------------------------------------------------

def test_cancel_task_returns_coded_payload():
    svc = BookImportService()
    running = _new_task("t-cancel-running", status="running")
    svc._tasks[running.task_id] = running
    cancelled = asyncio.run(svc.cancel_task(task_id=running.task_id, user_id=running.user_id))
    assert cancelled == {
        "success": True, "message": "取消成功",
        "code": "task.cancel_success", "params": {},
    }
    assert running.status == "cancelled"

    done = _new_task("t-cancel-done", status="completed")
    svc._tasks[done.task_id] = done
    terminal = asyncio.run(svc.cancel_task(task_id=done.task_id, user_id=done.user_id))
    assert terminal["message"] == "任务已是终态：completed"
    assert terminal["code"] == "task.already_terminal"
    assert terminal["params"] == {"status": "completed"}


# ---------------------------------------------------------------------------
# Category 1 — stable step_name keys in the failure payload
# ---------------------------------------------------------------------------

def test_step_failure_dataclass_keeps_stable_step_name():
    from app.services.book_import_service import _StepFailure

    failure = _StepFailure(
        step_name="world_building", step_label="世界观生成", error_message="boom",
    )
    assert failure.step_name == "world_building"
    assert failure.step_label == "世界观生成"
    assert failure.retry_count == 0
    assert failure.error_code is None
    assert failure.error_params is None


def test_apply_stream_step_failure_carries_api_error_code(monkeypatch):
    """自家 ApiError（模型守卫等）失败步骤带 error_code/error_params；
    上游诊断（RuntimeError）不带码。"""
    from unittest.mock import AsyncMock

    from app.core.errors import ApiError
    from app.schemas.book_import import BookImportApplyRequest, ProjectSuggestion
    from test_book_import_progress_i18n import _recording_callback, _stub_apply_flow

    svc = BookImportService()
    task = _new_task("t-step-api-code", status="completed")
    svc._tasks[task.task_id] = task
    _stub_apply_flow(svc, monkeypatch, career_exc=ApiError(code="validation.ai_model_not_configured"))

    calls: list[dict] = []
    asyncio.run(svc.apply_import_stream(
        task_id=task.task_id,
        user_id=task.user_id,
        payload=BookImportApplyRequest(project_suggestion=ProjectSuggestion(title="t"), chapters=[]),
        db=AsyncMock(),
        progress_callback=_recording_callback(calls),
        ai_service=None,
    ))

    failures = json.loads(calls[-1]["message"])["failed_steps"]
    assert failures[0]["step_name"] == "career_system"
    assert failures[0]["step_label"] == "职业体系生成"
    assert failures[0]["error_code"] == "validation.ai_model_not_configured"
    assert failures[0]["error_params"] is None
