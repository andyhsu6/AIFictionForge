"""issue #254 P2：生成后文笔一致性自检的接入（hook）测试。

覆盖 `compute_style_check_safely` 的三条降级路径（有画像 / 无画像 / 计算异常）
与「构建函数尚未落地」的兜底，`ChapterGenerateRequest` 的锚点覆盖字段边界，
以及各完成载荷确实带 `style_check` 的接线断言。临时 SQLite + `asyncio.run`，
不触达真实 AI 与 embedding。
"""
import asyncio
import inspect
import json
import os
import uuid

import pytest
from pydantic import ValidationError
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import app.services.style_consistency as style_consistency
from app.api import chapters as chapters_api
from app.database import Base
from app.models.project import Project
from app.models.project_default_style import ProjectDefaultStyle
from app.models.writing_style import WritingStyle
from app.schemas.chapter import ChapterGenerateRequest
from app.services.chapter_regenerator import compute_style_check_safely
from app.services.style_profile import profile_preset_id

METRICS = {
    "avg_sentence_chars": 21.0,
    "median_sentence_chars": 19.0,
    "short_sentence_ratio": 0.18,
    "dialogue_ratio": 0.12,
    "paragraph_chars_p50": 90.0,
}

TEXT = (
    "雨落在青石板上，声音细密。他沿着巷子往里走，墙头爬着半枯的藤。"
    "第三家没有点灯，门缝里透出一线灰白。他停下，抬手敲了三下。"
) * 4


def _run(scenario) -> None:
    """为每个测试建立独立临时 SQLite，跑异步场景后清理。"""

    async def _main() -> None:
        db_path = f"/tmp/test_style_check_hook_{uuid.uuid4().hex}.db"
        engine = create_async_engine(f"sqlite+aiosqlite:///{db_path}")
        try:
            async with engine.begin() as conn:
                await conn.run_sync(Base.metadata.create_all)
            session_factory = async_sessionmaker(bind=engine, expire_on_commit=False)
            async with session_factory() as session:
                await scenario(session)
        finally:
            await engine.dispose()
            if os.path.exists(db_path):
                os.remove(db_path)

    asyncio.run(_main())


async def _seed_project(db) -> Project:
    project = Project(
        id=f"project-{uuid.uuid4().hex[:8]}",
        user_id="user-1",
        title="占位项目",
        current_words=0,
    )
    db.add(project)
    await db.commit()
    return project


async def _attach_profile(db, project_id: str, metrics: dict = METRICS) -> WritingStyle:
    """挂一份本项目画像（与画像车道同构：自定义 preset_id + metrics）。"""
    style = WritingStyle(
        user_id="user-1",
        name="文笔画像",
        style_type="profile",
        preset_id=profile_preset_id(project_id),
        prompt_content="占位画像",
        profile_json=json.dumps({"version": 1, "metrics": metrics}),
        order_index=0,
    )
    db.add(style)
    await db.flush()
    db.add(ProjectDefaultStyle(project_id=project_id, style_id=style.id))
    await db.commit()
    return style


def test_available_with_profile_metrics():
    """有画像：真实构建函数返回 available + metrics，直接进入结果。"""

    async def scenario(db):
        project = await _seed_project(db)
        await _attach_profile(db, project.id)

        result = await compute_style_check_safely(db, project, TEXT)

        assert result["available"] is True
        assert result["metrics"]["avg_sentence_chars"] > 0
        assert set(METRICS).issubset(result["metrics"])
        assert result["ok"] in (True, False)

    _run(scenario)


def test_builder_is_called_and_result_passes_through(monkeypatch):
    """构建函数被以 (db, project, text) 调用，返回值原样进入 style_check。"""
    calls = {}

    async def fake_build(db, project, text, **kwargs):
        calls["args"] = (db, project, text)
        return {"available": True, "metrics": {"avg_sentence_chars": 21.5}, "ok": True}

    monkeypatch.setattr(
        style_consistency, "build_style_check_for_project", fake_build, raising=False
    )

    async def scenario(db):
        project = await _seed_project(db)
        result = await compute_style_check_safely(db, project, TEXT)

        assert result["available"] is True
        assert result["metrics"]["avg_sentence_chars"] == 21.5
        assert calls["args"][0] is db
        assert calls["args"][1] is project
        assert calls["args"][2] == TEXT

    _run(scenario)


def test_sync_builder_result_is_supported(monkeypatch):
    """构建函数若为同步实现也能用（避免 await 非 awaitable 报错）。"""
    monkeypatch.setattr(
        style_consistency,
        "build_style_check_for_project",
        lambda db, project, text: {"available": True, "metrics": {"avg_sentence_chars": 18.0}},
        raising=False,
    )

    async def scenario(db):
        project = await _seed_project(db)
        result = await compute_style_check_safely(db, project, TEXT)
        assert result["available"] is True

    _run(scenario)


def test_no_profile_returns_unavailable():
    """无画像：返回 {"available": False}，生成结果字段不受影响。"""

    async def scenario(db):
        project = await _seed_project(db)
        style_check = await compute_style_check_safely(db, project, TEXT)
        assert style_check == {"available": False}

        # 完成载荷（与 tracker.result 同形）照常可构造
        payload = {
            "word_count": len(TEXT),
            "analysis_task_id": "task-1",
            "style_check": style_check,
        }
        assert payload["word_count"] == len(TEXT)
        assert payload["analysis_task_id"] == "task-1"

    _run(scenario)


def test_builder_exception_degrades_without_breaking(monkeypatch):
    """计算抛异常：降级为 available=False，调用方无需 try/except。"""

    async def boom(db, project, text):
        raise RuntimeError("profile decode failed")

    monkeypatch.setattr(
        style_consistency, "build_style_check_for_project", boom, raising=False
    )

    async def scenario(db):
        project = await _seed_project(db)
        style_check = await compute_style_check_safely(db, project, TEXT)

        assert style_check["available"] is False
        # 后续生成结果仍可正常组装（主流程不被打断）
        assert {"word_count": 1, "style_check": style_check}["style_check"]["available"] is False

    _run(scenario)


def test_missing_builder_falls_back(monkeypatch):
    """构建函数尚未落地（ImportError）：返回 available=False 而不是抛错。"""
    monkeypatch.delattr(
        style_consistency, "build_style_check_for_project", raising=False
    )

    async def scenario(db):
        project = await _seed_project(db)
        assert await compute_style_check_safely(db, project, TEXT) == {"available": False}

    _run(scenario)


def test_empty_text_and_missing_project_skip_builder(monkeypatch):
    """空正文 / 无项目时不调用构建函数，直接降级。"""
    called = []

    async def fake_build(db, project, text):
        called.append(text)
        return {"available": True}

    monkeypatch.setattr(
        style_consistency, "build_style_check_for_project", fake_build, raising=False
    )

    async def scenario(db):
        project = await _seed_project(db)
        assert await compute_style_check_safely(db, project, "") == {"available": False}
        assert await compute_style_check_safely(None, project, TEXT) == {"available": False}
        assert called == []

    _run(scenario)


# ---------- 请求字段：锚点每请求覆盖 ----------


def test_anchor_fields_default_to_none():
    request = ChapterGenerateRequest()
    assert request.style_anchor_enabled is None
    assert request.style_anchor_chars is None
    assert request.style_anchor_count is None
    assert (
        chapters_api._build_style_anchor_override(
            enabled=request.style_anchor_enabled,
            chars=request.style_anchor_chars,
            count=request.style_anchor_count,
        )
        is None
    )


def test_anchor_override_only_includes_provided_fields():
    assert chapters_api._build_style_anchor_override(
        enabled=False, chars=600, count=None
    ) == {"enabled": False, "chars": 600}
    assert chapters_api._build_style_anchor_override(
        enabled=None, chars=None, count=4
    ) == {"count": 4}


def test_anchor_field_bounds():
    assert ChapterGenerateRequest(style_anchor_chars=150).style_anchor_chars == 150
    assert ChapterGenerateRequest(style_anchor_chars=2000).style_anchor_chars == 2000
    assert ChapterGenerateRequest(style_anchor_count=2).style_anchor_count == 2
    assert ChapterGenerateRequest(style_anchor_count=4).style_anchor_count == 4

    # 越界由 schema 层（ge/le）拒绝，对应 API 的 422
    with pytest.raises(ValidationError):
        ChapterGenerateRequest(style_anchor_chars=5000)
    with pytest.raises(ValidationError):
        ChapterGenerateRequest(style_anchor_chars=149)
    with pytest.raises(ValidationError):
        ChapterGenerateRequest(style_anchor_count=1)
    with pytest.raises(ValidationError):
        ChapterGenerateRequest(style_anchor_count=5)


# ---------- 接线断言：完成载荷确实带 style_check ----------


def test_completion_payloads_carry_style_check():
    source = inspect.getsource(chapters_api)
    # 流式 / 重生成（tracker.result）
    assert source.count("'style_check': style_check") >= 2
    # 后台生成（tracker.set_result）
    assert '"style_check": style_check' in source
    # 批量生成（返回值 dict）
    assert 'return {"summary": summary_preview, "style_check": style_check}' in source


def test_batch_anchor_settings_and_style_check_contract():
    """批量请求可携带锚点覆盖；任务与状态响应可承载自检结果。"""
    from app.models.batch_generation_task import BatchGenerationTask
    from app.schemas.chapter import BatchGenerateRequest, BatchGenerateStatusResponse

    request = BatchGenerateRequest(
        start_chapter_number=1,
        count=2,
        style_anchor_enabled=False,
        style_anchor_chars=300,
        style_anchor_count=2,
    )
    assert request.style_anchor_chars == 300

    task = BatchGenerationTask(
        id="batch-1",
        project_id="project-a",
        user_id="user-1",
        start_chapter_number=1,
        chapter_count=2,
        chapter_ids=["c1", "c2"],
        anchor_settings={"chars": 300},
        style_check={"available": True, "alerts": []},
    )
    assert task.anchor_settings["chars"] == 300

    status = BatchGenerateStatusResponse(
        batch_id="batch-1",
        status="completed",
        total=2,
        completed=2,
        style_check=task.style_check,
    )
    assert status.style_check["available"] is True


def test_batch_style_check_persisted_after_refresh():
    """批量 worker 的赋值必须在 refresh 之后，否则会被 DB 值抹掉（审核 BLOCKER）。"""
    from app.models.batch_generation_task import BatchGenerationTask

    async def scenario(db):
        task = BatchGenerationTask(
            id="batch-persist",
            project_id="project-a",
            user_id="user-1",
            start_chapter_number=1,
            chapter_count=2,
            chapter_ids=["c1", "c2"],
            status="running",
            total_chapters=2,
            completed_chapters=0,
        )
        db.add(task)
        await db.commit()
        task.status = "running"
        await db.commit()

        # 模拟 worker 顺序：refresh 之后赋值，再随进度 commit
        await db.refresh(task)
        task.style_check = {"available": True, "alerts": [], "score": 0.9}
        task.completed_chapters += 1
        await db.commit()

        refreshed = await db.get(BatchGenerationTask, "batch-persist")
        assert refreshed.style_check["available"] is True
        assert refreshed.completed_chapters == 1

    _run(scenario)
