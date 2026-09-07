"""拆书导入进度/状态/告警 i18n 双通道回归测试（issue #27 Phase 1+2）。

覆盖：
1. _notify（apply/retry 两条 SSE 流）携带 code/params 时，progress_callback 收到
   结构化通道；纯数据通道（step_failures JSON）不带 code（legacy 形状）。
2. _set_task_state 写 task.status_code/status_params；缺省时清空（legacy 行为）。
3. BookImportWarning 序列化 params；_build_preview 真实告警路径携带新码 + params。
4. SSE 进度 payload 契约唯一来源为 SSEResponse.send_progress（API _progress_callback
   直接透传，不再维护本地副本）：带 code 追加 message_code/message_params；
   不带 code 时 payload 与旧版 4 键字典字节形状一致。
5. 所有新 import.* 码在 ERROR_REGISTRY + zh/en errors.json 三处注册，zh 与
   registry 模板字节一致，且不使用 i18next 保留参数名 count。
6. 章节结构进度/告警按解析口径拆码：整本导入发 *Full 变体（无中文标签参数），
   末章导入发 *Tail 变体（章数为数值参数）。
"""
import asyncio
import json
import re
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.core.errors import ERROR_REGISTRY
from app.schemas.book_import import BookImportApplyRequest, BookImportWarning, ProjectSuggestion
from app.services.book_import_service import BookImportService, _BookImportTask, _StepFailure
from app.utils.sse_response import SSEResponse

BACKEND_DIR = Path(__file__).resolve().parents[1]
REPO_ROOT = BACKEND_DIR.parent


def _completed_task(task_id: str) -> _BookImportTask:
    return _BookImportTask(
        task_id=task_id,
        user_id="user-1",
        filename="book.txt",
        project_id=None,
        create_new_project=True,
        import_mode="append",
        status="completed",
    )


def _recording_callback(store: list[dict]):
    async def cb(message, progress, status="processing", code=None, params=None):
        store.append({
            "message": message,
            "progress": progress,
            "status": status,
            "code": code,
            "params": params,
        })
    return cb


def _stub_apply_flow(svc: BookImportService, monkeypatch, career_exc: Exception | None = None) -> None:
    """把 apply_import_stream 依赖的落库/AI 生成步骤替换为可控桩。"""
    async def fake_prepare_project(**kwargs):
        return SimpleNamespace(id="p1", character_count=8, current_words=0)

    async def fake_import_outlines(**kwargs):
        return {}

    async def fake_import_chapters(**kwargs):
        return 3, 1000

    async def fake_generate_world(**kwargs):
        return 1

    async def fake_generate_careers(**kwargs):
        if career_exc is not None:
            raise career_exc
        return 2

    async def fake_generate_characters(**kwargs):
        return 3

    async def fake_extract_relationships(**kwargs):
        return {"extracted_relationships": 2, "created_types": 1, "created_characters": 1}

    monkeypatch.setattr(svc, "_prepare_project", fake_prepare_project)
    monkeypatch.setattr(svc, "_import_outlines", fake_import_outlines)
    monkeypatch.setattr(svc, "_import_chapters", fake_import_chapters)
    monkeypatch.setattr(svc, "_generate_world_building_from_project", fake_generate_world)
    monkeypatch.setattr(svc, "_generate_career_system_from_project", fake_generate_careers)
    monkeypatch.setattr(svc, "_generate_characters_and_organizations_from_project", fake_generate_characters)
    monkeypatch.setattr(svc, "_extract_relationships_from_chapters", fake_extract_relationships)


def test_apply_stream_notify_with_code_forwards_structured_channel(monkeypatch):
    """_notify 携带 code/params 时回调收到结构化字段；message 文案保持原文。"""
    svc = BookImportService()
    task = _completed_task("t-i18n-apply")
    svc._tasks[task.task_id] = task
    _stub_apply_flow(svc, monkeypatch, career_exc=RuntimeError("career boom"))
    db = AsyncMock()

    calls: list[dict] = []
    result = asyncio.run(svc.apply_import_stream(
        task_id=task.task_id,
        user_id=task.user_id,
        payload=BookImportApplyRequest(
            project_suggestion=ProjectSuggestion(title="i18n回归测试"),
            chapters=[],
        ),
        db=db,
        progress_callback=_recording_callback(calls),
        ai_service=None,
    ))
    assert result.success is True
    assert result.project_id == "p1"

    # 带 code 的站点：回调收到 code/params，message 保持原文案
    assert calls[0]["message"] == "正在创建项目..."
    assert calls[0]["code"] == "import.progress.creatingProject"
    assert calls[9]["message"] == "⚠️ 职业体系生成失败：career boom，将继续后续步骤"
    assert calls[9]["status"] == "warning"
    assert calls[9]["code"] == "import.progress.careersFailed"
    assert calls[9]["params"] == {"error": "career boom"}
    chapters_site = next(c for c in calls if c["code"] == "import.progress.chaptersImported")
    assert chapters_site["message"] == "已导入 3 个章节（1000字）"
    assert chapters_site["params"] == {"chapters": 3, "words": 1000}
    rel_site = next(c for c in calls if c["code"] == "import.progress.relationshipsDone")
    assert rel_site["params"] == {"relationships": 2}

    # 全流程 code 序列（含失败步骤汇总）
    codes = [c["code"] for c in calls]
    assert codes == [
        "import.progress.creatingProject",
        "import.progress.projectCreated",
        "import.progress.importingOutlines",
        "import.progress.outlinesImported",
        "import.progress.importingChapters",
        "import.progress.chaptersImported",
        "import.progress.generatingWorld",
        "import.progress.worldDone",
        "import.progress.generatingCareers",
        "import.progress.careersFailed",
        "import.progress.generatingCharacters",
        "import.progress.charactersDone",
        "import.progress.extractingRelationships",
        "import.progress.relationshipsDone",
        "import.progress.savingDb",
        "import.progress.savedDb",
        "import.progress.doneWithFailures",
        None,  # step_failures JSON 数据通道（直调 progress_callback，不带 code）
    ]

    # 不带 code 的调用（step_failures 数据通道）：回调只收到 legacy 三个位置参数
    data_call = calls[-1]
    assert data_call["message"].startswith('{"failed_steps":')
    assert data_call["progress"] == 98
    assert data_call["status"] == "step_failures"
    assert data_call["code"] is None
    assert data_call["params"] is None


def test_retry_stream_notify_with_code_forwards_structured_channel(monkeypatch):
    """retry 流 _notify 同样透传 code/params。"""
    import app.api.common as api_common

    svc = BookImportService()
    task = _completed_task("t-i18n-retry")
    task.imported_project_id = "p1"
    task.failed_steps = [_StepFailure(
        step_name="world_building", step_label="世界观生成", error_message="boom",
    )]
    svc._tasks[task.task_id] = task

    async def fake_verify(project_id, user_id, db):
        return SimpleNamespace(id="p1", character_count=8)

    async def fake_generate_world(**kwargs):
        return 1

    monkeypatch.setattr(api_common, "verify_project_access", fake_verify)
    monkeypatch.setattr(svc, "_generate_world_building_from_project", fake_generate_world)
    # db.execute 被 await 后返回同步结果，其 .scalars().all() 为同步链（MagicMock）
    exec_result = MagicMock()
    exec_result.scalars.return_value.all.return_value = []
    db = AsyncMock()
    db.execute.return_value = exec_result

    calls: list[dict] = []
    result = asyncio.run(svc.retry_failed_steps_stream(
        task_id=task.task_id,
        user_id=task.user_id,
        steps_to_retry=["world_building"],
        db=db,
        progress_callback=_recording_callback(calls),
        ai_service=None,
    ))
    assert result["success"] is True

    assert calls[0]["message"] == "🔄 正在重试世界观生成..."
    assert calls[0]["progress"] == 5
    assert calls[0]["code"] == "import.progress.retryWorld"
    assert calls[1]["message"] == "✅ 世界观重试成功"
    assert calls[1]["progress"] == 90
    assert calls[1]["code"] == "import.progress.retryWorldDone"
    assert calls[-1]["code"] == "import.progress.savedDb"


def test_set_task_state_with_code_sets_structured_fields():
    """_set_task_state 带 code/params → 写入 task.status_code/status_params，旧字段不变。"""
    svc = BookImportService()
    task = _completed_task("t-code")
    svc._set_task_state(
        task, status="running", progress=42, message="正在初始化AI服务...",
        code="import.task.initAiService", params={"k": 1},
    )
    assert task.status == "running"
    assert task.progress == 42
    assert task.message == "正在初始化AI服务..."
    assert task.status_code == "import.task.initAiService"
    assert task.status_params == {"k": 1}


def test_set_task_state_without_code_keeps_legacy_none():
    """_set_task_state 缺省 code → status_code/status_params 清空（legacy 行为）。"""
    svc = BookImportService()
    task = _completed_task("t-nocode")
    svc._set_task_state(task, status="running", progress=10, message="x", code="import.task.detectEncoding")
    assert task.status_code == "import.task.detectEncoding"
    svc._set_task_state(task, status="running", progress=20, message="y")
    assert task.status_code is None
    assert task.status_params is None


def test_get_task_status_polling_carries_status_code_and_last_state_none():
    """轮询响应（get_task_status）透传任务态结构化码/参数；last-state 语义：
    任意不带 code 的 _set_task_state 写入后，响应两字段回 None（前端按旧版
    逻辑原样展示 message）。"""
    svc = BookImportService()
    task = _completed_task("t-poll")
    svc._tasks[task.task_id] = task

    svc._set_task_state(
        task, status="running", progress=30, message="正在初始化AI服务...",
        code="import.task.initAiService", params={"attempt": 2},
    )
    resp = asyncio.run(svc.get_task_status(task_id=task.task_id, user_id=task.user_id))
    assert resp.status_code == "import.task.initAiService"
    assert resp.status_params == {"attempt": 2}
    assert resp.message == "正在初始化AI服务..."

    svc._set_task_state(task, status="running", progress=40, message="无码状态写入")
    resp = asyncio.run(svc.get_task_status(task_id=task.task_id, user_id=task.user_id))
    assert resp.status_code is None
    assert resp.status_params is None
    assert resp.message == "无码状态写入"


def test_warning_schema_serializes_params_and_tolerates_none():
    """BookImportWarning.params 序列化；旧构造（无 params）容忍为 None。"""
    w = BookImportWarning(
        code="import.warning.chapterTooShort",
        message="章节「示例」内容较短，建议检查切分结果",
        level="warning",
        params={"title": "示例"},
    )
    data = w.model_dump()
    assert data["params"] == {"title": "示例"}
    assert data["code"] == "import.warning.chapterTooShort"

    legacy = BookImportWarning(code="legacy_code", message="m")
    assert legacy.params is None
    assert legacy.model_dump()["params"] is None
    # exclude_none 序列化时缺省（前端/旧客户端不受影响）
    assert "params" not in legacy.model_dump(exclude_none=True)


def test_build_preview_warnings_carry_codes_and_params(monkeypatch):
    """_build_preview 真实告警路径：新码 + params（title/occurrences 等）。"""
    svc = BookImportService()
    task = _completed_task("t-preview")
    svc._tasks[task.task_id] = task

    chapters_data = [
        {"title": "第1章 开端", "content": "短" * 50},
        {"title": "第1章 开端", "content": "复" * 400},
        {"title": "第2章 长章", "content": "长" * 13000},
    ]

    async def fake_reverse_suggestion(**kwargs):
        return kwargs["suggestion"]

    async def fake_reverse_outlines(**kwargs):
        return []

    monkeypatch.setattr(svc, "_generate_reverse_project_suggestion", fake_reverse_suggestion)
    monkeypatch.setattr(svc, "_generate_reverse_outlines", fake_reverse_outlines)

    # 侦听 _set_task_state 写入的结构化码序列
    state_codes: list[tuple] = []
    original_set_state = svc._set_task_state

    def spy_set_state(task_arg, **kwargs):
        state_codes.append((kwargs.get("code"), kwargs.get("params")))
        original_set_state(task_arg, **kwargs)

    monkeypatch.setattr(svc, "_set_task_state", spy_set_state)

    preview = asyncio.run(svc._build_preview(
        task=task, filename="book.txt", task_id=task.task_id, chapters_data=chapters_data,
    ))

    by_code: dict[str, list] = {}
    for w in preview.warnings:
        by_code.setdefault(w.code, []).append(w)

    shorts = by_code["import.warning.chapterTooShort"]
    assert len(shorts) == 1
    assert shorts[0].params == {"title": "开端"}

    longs = by_code["import.warning.chapterTooLong"]
    assert len(longs) == 1
    assert longs[0].params == {"title": "长章"}

    dups = by_code["import.warning.duplicateTitles"]
    assert len(dups) == 1
    assert dups[0].params == {"title": "开端", "occurrences": 2}

    # 全部告警码已切换到 import.warning.* 命名空间
    assert all(w.code.startswith("import.warning.") for w in preview.warnings)
    # 章节结构任务态已携带结构化码与参数（默认 tail 模式 → Tail 变体，含数值章数）
    chapter_state = [entry for entry in state_codes if entry[0] == "import.task.chapterStructuresTail"]
    assert chapter_state, "章节结构进度应携带 import.task.chapterStructuresTail"
    assert chapter_state[-1][1]["total"] == 3
    assert chapter_state[-1][1]["index"] == 3
    assert chapter_state[-1][1]["chapters"] == 3
    assert not [entry for entry in state_codes if entry[0] == "import.task.chapterStructuresFull"]


def test_build_preview_extract_mode_picks_full_or_tail_code(monkeypatch):
    """整本导入发 chapterStructuresFull（无章数标签参数）；末章导入发
    chapterStructuresTail（chapters 数值参数），并触发 filteredChaptersTail；
    各变体 message 与 zh 模板逐字节一致（模板占位符按 params 代入）。"""
    svc = BookImportService()

    chapters_data = [
        {"title": f"第{i}章", "content": "内" * 500}
        for i in range(1, 9)
    ]

    async def fake_reverse_suggestion(**kwargs):
        return kwargs["suggestion"]

    async def fake_reverse_outlines(**kwargs):
        return []

    monkeypatch.setattr(svc, "_generate_reverse_project_suggestion", fake_reverse_suggestion)
    monkeypatch.setattr(svc, "_generate_reverse_outlines", fake_reverse_outlines)

    state_codes: list[tuple] = []
    original_set_state = svc._set_task_state

    def spy_set_state(task_arg, **kwargs):
        state_codes.append((kwargs.get("code"), kwargs.get("params"), kwargs.get("message")))
        original_set_state(task_arg, **kwargs)

    monkeypatch.setattr(svc, "_set_task_state", spy_set_state)

    # ---- 整本导入：Full 变体，无 chapters 参数 ----
    full_task = _completed_task("t-preview-full")
    full_task.extract_mode = "full"
    asyncio.run(svc._build_preview(
        task=full_task, filename="book.txt", task_id=full_task.task_id, chapters_data=chapters_data,
    ))
    full_states = [e for e in state_codes if e[0] == "import.task.chapterStructuresFull"]
    tail_states = [e for e in state_codes if e[0] == "import.task.chapterStructuresTail"]
    assert full_states and not tail_states
    assert full_states[-1][1] == {"index": 8, "total": 8}
    assert full_states[-1][2] == "已处理整本 8/8 个章节结构..."
    full_last = full_states[-1]

    # ---- 末章导入（8 章仅取末 5 章）：Tail 变体 + filteredChaptersTail ----
    state_codes.clear()
    tail_task = _completed_task("t-preview-tail")
    tail_task.extract_mode = "tail"
    tail_task.tail_chapter_count = 5
    preview = asyncio.run(svc._build_preview(
        task=tail_task, filename="book.txt", task_id=tail_task.task_id, chapters_data=chapters_data,
    ))
    full_states = [e for e in state_codes if e[0] == "import.task.chapterStructuresFull"]
    tail_states = [e for e in state_codes if e[0] == "import.task.chapterStructuresTail"]
    assert tail_states and not full_states
    assert tail_states[-1][1] == {"chapters": 5, "index": 5, "total": 5}
    assert tail_states[-1][2] == "已处理末5章 5/5 个章节结构..."

    trimmed = [w for w in preview.warnings if w.code == "import.warning.filteredChaptersTail"]
    assert len(trimmed) == 1
    assert trimmed[0].params == {"kept": 5, "detected": 8}
    assert trimmed[0].message == "已按解析配置仅保留末5章 5 章用于导入（原始识别 8 章）"

    # zh 模板按 params 代入后与运行时 message 逐字节一致（byte-identity 自证）
    zh = json.loads((REPO_ROOT / "frontend" / "src" / "locales" / "zh" / "errors.json").read_text(encoding="utf-8"))

    def _render(template: str, params: dict) -> str:
        out = template
        for key, value in params.items():
            out = out.replace("{{" + key + "}}", str(value))
        return out

    assert _render(zh["import"]["task"]["chapterStructuresFull"], full_last[1]) == full_last[2]
    assert _render(zh["import"]["task"]["chapterStructuresTail"], tail_states[-1][1]) == tail_states[-1][2]
    assert _render(zh["import"]["warning"]["filteredChaptersTail"], trimmed[0].params) == trimmed[0].message


def test_send_progress_with_code_adds_structured_fields():
    """SSEResponse.send_progress（两条 _progress_callback 的唯一 payload 实现）：
    带 code 追加 message_code/message_params（params 缺省回填 {}），SSE 字符串
    形状与重构前 _progress_sse_payload + format_sse 完全一致。"""
    payload = {
        "type": "progress",
        "message": "正在保存到数据库...",
        "progress": 96,
        "status": "processing",
        "message_code": "import.progress.savingDb",
        "message_params": {"x": 1},
    }
    sse = asyncio.run(SSEResponse.send_progress(
        "正在保存到数据库...", 96, "processing",
        code="import.progress.savingDb", params={"x": 1},
    ))
    assert sse == f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"
    # params 缺省回填 {}
    sse_no_params = asyncio.run(SSEResponse.send_progress("m", 1, code="import.progress.savedDb"))
    assert json.loads(sse_no_params.removeprefix("data: ").removesuffix("\n\n"))["message_params"] == {}


def test_send_progress_without_code_byte_identical_to_legacy():
    """不带 code 时 payload 与旧版 4 键字典完全一致（无 message_code 键）。"""
    legacy = {"type": "progress", "message": "m", "progress": 50, "status": "processing"}
    sse = asyncio.run(SSEResponse.send_progress("m", 50))
    assert sse == f"data: {json.dumps(legacy, ensure_ascii=False)}\n\n"
    # params 为 None 时同样保持旧形状
    sse_none_params = asyncio.run(SSEResponse.send_progress("m", 50, params=None))
    assert sse_none_params == sse


def _flatten(d: dict, prefix: str = "") -> dict:
    out = {}
    for k, v in d.items():
        key = f"{prefix}{k}"
        if isinstance(v, dict):
            out.update(_flatten(v, prefix=f"{key}."))
        else:
            out[key] = v
    return out


def _emitted_import_codes() -> list[str]:
    """从源码中提取 book-import 流实际发出的全部 import.* 码。"""
    sources = [
        BACKEND_DIR / "app" / "services" / "book_import_service.py",
        BACKEND_DIR / "app" / "api" / "book_import.py",
    ]
    codes: set[str] = set()
    for src in sources:
        codes |= set(re.findall(r'"(import\.(?:progress|task|warning)\.[A-Za-z0-9]+)"', src.read_text(encoding="utf-8")))
    return sorted(codes)


def test_all_import_codes_registered_in_registry_and_locales():
    """每个发出的 import.* 码都在 registry + zh/en errors.json 注册，zh 与模板字节一致。"""
    codes = _emitted_import_codes()
    assert len(codes) >= 70, f"应至少注册 70 个码，实际提取到 {len(codes)}"

    zh = _flatten(json.loads((REPO_ROOT / "frontend" / "src" / "locales" / "zh" / "errors.json").read_text(encoding="utf-8")))
    en = _flatten(json.loads((REPO_ROOT / "frontend" / "src" / "locales" / "en" / "errors.json").read_text(encoding="utf-8")))

    for code in codes:
        assert code in ERROR_REGISTRY, f"{code} 未注册到 ERROR_REGISTRY"
        assert code in zh, f"{code} 缺少 zh locale"
        assert code in en, f"{code} 缺少 en locale"
        template, status = ERROR_REGISTRY[code]
        assert status == 200, f"{code} status 应为 200（progress.* 约定）"
        assert zh[code] == template, f"{code} zh locale 与 registry 模板不一致"
        assert zh[code].strip() and en[code].strip(), f"{code} locale 值为空"


def test_import_codes_never_use_i18next_reserved_count_param():
    """count 是 i18next 复数保留参数名：码化站点与模板均不得使用。"""
    service_source = (BACKEND_DIR / "app" / "services" / "book_import_service.py").read_text(encoding="utf-8")
    assert not re.search(r'params=\{[^}]*"count"', service_source)
    zh = json.loads((REPO_ROOT / "frontend" / "src" / "locales" / "zh" / "errors.json").read_text(encoding="utf-8"))
    en = json.loads((REPO_ROOT / "frontend" / "src" / "locales" / "en" / "errors.json").read_text(encoding="utf-8"))
    for locale in (zh, en):
        for subgroup in ("progress", "task", "warning"):
            for value in locale["import"][subgroup].values():
                assert "{{count}}" not in value
