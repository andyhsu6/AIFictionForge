"""拆书导入内容审核降级回归测试（issue #168）。

背景：上游内容审核返回 HTTP 200 + finish_reason="content_filter" + 0 字正文时，
`call_with_json_retry` 抛 `ApiError(code="ai.content_filtered")`。旧实现里
「一次调用失败 = 整批/整步归零」，导致拆书导入 characters=0、relationships=0。

本文件钉死修复契约：
(a) 关系抽取：5 章批里 1 章被拦 → 其余 4 章的关系照常产出，被拦章进 skipped；
(b) 角色生成：默认窗口/章被拦 → 以 900/章重试；仍被拦 → 章节二分，单章仍被拦则跳过；
(c) 非审核类异常必须继续上抛，不得被降级逻辑吞掉；
(d) 无内容审核时调用次数与旧行为一致（回归）；
(e) skipped_chapters 的落点：响应顶层 + statistics + 任务态 + step_failures 消息。

夹具与断言文本全部为中性占位（AGENTS.md 脱敏硬约束）。
"""
from __future__ import annotations

import json
import os
import re
import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.core.errors import ApiError
from app.database import Base
from app.models.chapter import Chapter
from app.models.project import Project
from app.schemas.book_import import BookImportApplyRequest, BookImportChapter, ProjectSuggestion
from app.services import book_import_service as bis
from app.services.book_import_service import BookImportService, _BookImportTask
from app.services.prompt_service import PromptService

PROJECT_ID = "proj-cf-1"
USER_ID = "user-cf-1"

# 与其他异步测试一致：anyio 插件（默认 asyncio backend）
pytestmark = pytest.mark.anyio

RELATIONSHIP_TEMPLATE = "标题:{title}\n类型:{genre}\n原文:\n{chapters_text}"
CHARACTER_TEMPLATE = (
    "数量:{count}\n{time_period}|{location}|{atmosphere}|{rules}|{theme}|{genre}\n"
    "要求:{requirements}"
)

# 占位填充：让「默认窗口」与「900 窗口」的摘录内容可区分（TAIL 标记落在 900 之后）
_FILLER = "占位正文" * 250  # 1000 字符


def _chapter_content(number: int) -> str:
    """占位章节正文：前 1000 字符 + TAIL 标记（默认 3000 窗口能读到，900 窗口读不到）。"""
    return f"占位章节{number}开头。" + _FILLER + f"TAIL{number}"


def _chapter_numbers_in_prompt(prompt: str) -> list[int]:
    return [int(n) for n in re.findall(r"【第(\d+)章 ", prompt)]


def _content_filter_error() -> ApiError:
    return ApiError(code="ai.content_filtered")


@pytest.fixture
async def db_session():
    """临时文件 SQLite（与 test_plan_step_argument_contract.py 同模式）。"""
    db_path = f"/tmp/test_book_import_content_filter_{uuid.uuid4().hex}.db"
    engine = create_async_engine(f"sqlite+aiosqlite:///{db_path}")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    Session = async_sessionmaker(bind=engine, expire_on_commit=False)
    async with Session() as session:
        yield session
    await engine.dispose()
    if os.path.exists(db_path):
        os.remove(db_path)


@pytest.fixture
def patched_prompts(monkeypatch):
    """模板与语言解析走桩：测试只关心降级策略，不查模板表/设置表。"""

    async def fake_get_template(name, user_id=None, db=None):
        return RELATIONSHIP_TEMPLATE if name == "RELATIONSHIP_EXTRACTION" else CHARACTER_TEMPLATE

    async def fake_generation_language(db, user_id, content_language=None):
        return "zh"

    monkeypatch.setattr(PromptService, "get_template", fake_get_template)
    monkeypatch.setattr(bis, "resolve_user_generation_language", fake_generation_language)


def _install_ai_service(monkeypatch, handler):
    """构造 AI 服务桩并 monkeypatch 其 call_with_json_retry（async）。"""
    ai_service = SimpleNamespace(default_model="stub-model")
    calls: list[str] = []

    async def resolve_budget_chars(model_name=None):
        return 1_000_000

    async def call_with_json_retry(*, prompt, **kwargs):
        calls.append(prompt)
        return handler(prompt, kwargs)

    ai_service.resolve_full_book_budget_chars = resolve_budget_chars
    monkeypatch.setattr(ai_service, "call_with_json_retry", call_with_json_retry, raising=False)
    return ai_service, calls


async def _seed_project(db, title: str = "占位项目") -> Project:
    project = Project(id=PROJECT_ID, user_id=USER_ID, title=title, genre="通用", character_count=5)
    db.add(project)
    await db.commit()
    return project


async def _seed_chapters(db, numbers: list[int]) -> None:
    for number in numbers:
        db.add(
            Chapter(
                project_id=PROJECT_ID,
                chapter_number=number,
                title=f"占位章节标题{number}",
                content=_chapter_content(number),
                word_count=len(_chapter_content(number)),
                status="draft",
            )
        )
    await db.commit()


def _book_chapters(numbers: list[int]) -> list[BookImportChapter]:
    return [
        BookImportChapter(
            title=f"占位章节标题{n}",
            content=_chapter_content(n),
            chapter_number=n,
        )
        for n in numbers
    ]


def _relationship_items(numbers: list[int]) -> list[dict]:
    return [
        {
            "character_a": f"占位人物{n}甲",
            "character_b": f"占位人物{n}乙",
            "relationship_types": ["盟友"],
            "description": "占位关系描述",
        }
        for n in numbers
    ]


def _complete_profile(name: str) -> dict:
    """档案齐全的占位实体（issue #173）：这些用例只关心降级/调用次数，
    实体字段若为空会额外触发一次档案补全调用，污染调用计数断言。"""
    return {
        "name": name,
        "role_type": "supporting",
        "personality": f"{name}的占位性格：沉着克制，遇事先观察再表态。",
        "background": f"{name}的占位背景：在占位城镇长大，靠占位手艺谋生。",
        "appearance": f"{name}的占位外貌：中等身材，常穿占位式样的深色外衣。",
        "current_state": f"{name}的占位处境：暂住占位城镇，等待占位事件的结果。",
    }


# --- (a) 关系抽取：批内二分降级 ------------------------------------------------


async def test_relationship_extraction_bisects_batch_on_content_filter(
    db_session, patched_prompts, monkeypatch
):
    project = await _seed_project(db_session)
    chapters = _book_chapters([1, 2, 3, 4, 5])
    blocked = {3}

    def handler(prompt: str, kwargs: dict):
        numbers = _chapter_numbers_in_prompt(prompt)
        if blocked & set(numbers):
            raise _content_filter_error()
        return _relationship_items(numbers)

    ai_service, calls = _install_ai_service(monkeypatch, handler)

    result = await BookImportService()._extract_relationships_from_chapters(
        db=db_session,
        user_id=USER_ID,
        project=project,
        chapters=chapters,
        ai_service=ai_service,
        content_language="zh",
    )

    # 4 个未被拦的章节仍然各自产出关系
    assert result["extracted_relationships"] == 4
    assert result["skipped_chapters"] == [
        {"chapter_number": 3, "reason": "content_filter", "scope": "relationships"}
    ]
    # 5 章批被拦 → 左半 [1,2] 一次；右半 [3,4,5] 被拦 → [3] 与 [4,5]
    assert len(calls) == 5


async def test_relationship_extraction_reraises_non_content_filter_errors(
    db_session, patched_prompts, monkeypatch
):
    project = await _seed_project(db_session)
    chapters = _book_chapters([1, 2])

    def handler(prompt: str, kwargs: dict):
        raise RuntimeError("placeholder upstream failure")

    ai_service, _ = _install_ai_service(monkeypatch, handler)

    with pytest.raises(RuntimeError, match="placeholder upstream failure"):
        await BookImportService()._extract_relationships_from_chapters(
            db=db_session,
            user_id=USER_ID,
            project=project,
            chapters=chapters,
            ai_service=ai_service,
            content_language="zh",
        )


# --- (b) 角色生成：摘录阶梯降级 ------------------------------------------------


async def test_character_generation_downgrades_to_900_window(
    db_session, patched_prompts, monkeypatch
):
    """默认窗口/章被拦 → 900/章重试成功（TAIL 标记落在 900 之后，故 900 档不再命中）。"""
    project = await _seed_project(db_session)
    await _seed_chapters(db_session, [1, 2, 3])

    def handler(prompt: str, kwargs: dict):
        if "TAIL" in prompt:
            raise _content_filter_error()
        return [_complete_profile(f"占位角色{n}") for n in _chapter_numbers_in_prompt(prompt)]

    ai_service, calls = _install_ai_service(monkeypatch, handler)
    skipped: list[dict] = []

    created = await BookImportService()._generate_characters_and_organizations_from_project(
        db=db_session,
        user_id=USER_ID,
        project=project,
        count=5,
        ai_service=ai_service,
        content_language="zh",
        skipped_chapters=skipped,
    )

    assert created == 3
    assert skipped == []
    # count=5 → 单批：3000 档一次（被拦）+ 900 档一次（成功）
    assert len(calls) == 2


async def test_character_generation_bisects_chapters_when_900_still_filtered(
    db_session, patched_prompts, monkeypatch
):
    """900 档仍被拦 → 章节二分；单章仍被拦则跳过并记入 skipped。"""
    project = await _seed_project(db_session)
    await _seed_chapters(db_session, [1, 2, 3, 4])

    def handler(prompt: str, kwargs: dict):
        numbers = _chapter_numbers_in_prompt(prompt)
        if 3 in numbers:
            raise _content_filter_error()
        return [_complete_profile(f"占位角色{n}") for n in numbers]

    ai_service, calls = _install_ai_service(monkeypatch, handler)
    skipped: list[dict] = []

    created = await BookImportService()._generate_characters_and_organizations_from_project(
        db=db_session,
        user_id=USER_ID,
        project=project,
        count=5,
        ai_service=ai_service,
        content_language="zh",
        skipped_chapters=skipped,
    )

    # 第 1、2、4 章仍能产出实体；第 3 章被跳过
    assert created == 3
    assert skipped == [
        {"chapter_number": 3, "reason": "content_filter", "scope": "characters"}
    ]
    # 3000 档 + 900 档 + [1,2] + [3,4] + [3] + [4] = 6 次
    assert len(calls) == 6


async def test_character_generation_reraises_non_content_filter_errors(
    db_session, patched_prompts, monkeypatch
):
    project = await _seed_project(db_session)
    await _seed_chapters(db_session, [1, 2])

    def handler(prompt: str, kwargs: dict):
        raise RuntimeError("placeholder upstream failure")

    ai_service, _ = _install_ai_service(monkeypatch, handler)

    with pytest.raises(RuntimeError, match="placeholder upstream failure"):
        await BookImportService()._generate_characters_and_organizations_from_project(
            db=db_session,
            user_id=USER_ID,
            project=project,
            count=5,
            ai_service=ai_service,
            content_language="zh",
            skipped_chapters=[],
        )


# --- (d) 回归：无内容审核时不增加调用次数 --------------------------------------


async def test_no_content_filter_keeps_single_relationship_call(
    db_session, patched_prompts, monkeypatch
):
    project = await _seed_project(db_session)
    chapters = _book_chapters([1, 2, 3, 4, 5])

    def handler(prompt: str, kwargs: dict):
        return _relationship_items(_chapter_numbers_in_prompt(prompt))

    ai_service, calls = _install_ai_service(monkeypatch, handler)

    result = await BookImportService()._extract_relationships_from_chapters(
        db=db_session,
        user_id=USER_ID,
        project=project,
        chapters=chapters,
        ai_service=ai_service,
        content_language="zh",
    )

    assert len(calls) == 1
    assert result["extracted_relationships"] == 5
    assert result["skipped_chapters"] == []


async def test_no_content_filter_keeps_single_character_round(
    db_session, patched_prompts, monkeypatch
):
    project = await _seed_project(db_session)
    await _seed_chapters(db_session, [1, 2, 3])

    def handler(prompt: str, kwargs: dict):
        return [_complete_profile(f"占位角色{n}") for n in _chapter_numbers_in_prompt(prompt)]

    ai_service, calls = _install_ai_service(monkeypatch, handler)
    skipped: list[dict] = []

    created = await BookImportService()._generate_characters_and_organizations_from_project(
        db=db_session,
        user_id=USER_ID,
        project=project,
        count=5,
        ai_service=ai_service,
        content_language="zh",
        skipped_chapters=skipped,
    )

    assert created == 3
    assert skipped == []
    assert len(calls) == 1


# --- (e) skipped_chapters 落点：响应顶层 / statistics / 任务态 / step_failures --


async def test_apply_stream_reports_skipped_chapters(monkeypatch):
    svc = BookImportService()
    task = _BookImportTask(
        task_id="t-cf-apply",
        user_id=USER_ID,
        filename="placeholder.txt",
        project_id=None,
        create_new_project=True,
        import_mode="append",
        status="completed",
    )
    svc._tasks[task.task_id] = task

    async def fake_prepare_project(**kwargs):
        return SimpleNamespace(id="p-cf", character_count=5, current_words=0)

    async def fake_import_outlines(**kwargs):
        return {}

    async def fake_import_chapters(**kwargs):
        return 3, 1000

    async def fake_generate_world(**kwargs):
        return 1

    async def fake_generate_careers(**kwargs):
        raise RuntimeError("placeholder career failure")

    async def fake_generate_characters(**kwargs):
        kwargs["skipped_chapters"].append(
            {"chapter_number": 2, "reason": "content_filter", "scope": "characters"}
        )
        return 2

    async def fake_extract_relationships(**kwargs):
        return {
            "extracted_relationships": 1,
            "created_types": 1,
            "created_characters": 1,
            "skipped_chapters": [
                {"chapter_number": 7, "reason": "content_filter", "scope": "relationships"}
            ],
        }

    monkeypatch.setattr(svc, "_prepare_project", fake_prepare_project)
    monkeypatch.setattr(svc, "_import_outlines", fake_import_outlines)
    monkeypatch.setattr(svc, "_import_chapters", fake_import_chapters)
    monkeypatch.setattr(svc, "_generate_world_building_from_project", fake_generate_world)
    monkeypatch.setattr(svc, "_generate_career_system_from_project", fake_generate_careers)
    monkeypatch.setattr(svc, "_generate_characters_and_organizations_from_project", fake_generate_characters)
    monkeypatch.setattr(svc, "_extract_relationships_from_chapters", fake_extract_relationships)

    calls: list[dict] = []

    async def progress_callback(message, progress, status="processing", code=None, params=None):
        calls.append({"message": message, "status": status})

    result = await svc.apply_import_stream(
        task_id=task.task_id,
        user_id=USER_ID,
        payload=BookImportApplyRequest(
            project_suggestion=ProjectSuggestion(title="占位项目"),
            chapters=[],
        ),
        db=AsyncMock(),
        progress_callback=progress_callback,
        ai_service=None,
    )

    expected = [
        {"chapter_number": 2, "reason": "content_filter", "scope": "characters"},
        {"chapter_number": 7, "reason": "content_filter", "scope": "relationships"},
    ]
    assert result.skipped_chapters == expected
    assert result.statistics["skipped_chapters"] == expected
    assert task.skipped_chapters == expected
    # 任务态响应透出（刷新后仍能显示）
    assert svc._to_status(task).skipped_chapters == expected

    # step_failures 数据通道同一条消息带出 skipped_chapters
    data_call = next(c for c in calls if c["status"] == "step_failures")
    payload = json.loads(data_call["message"])
    assert [f["step_name"] for f in payload["failed_steps"]] == ["career_system"]
    assert payload["skipped_chapters"] == expected


async def test_apply_stream_omits_skipped_payload_when_clean(monkeypatch):
    """无跳过时：skipped_chapters 为空 list，且失败步骤消息里的列表也为空。"""
    svc = BookImportService()
    task = _BookImportTask(
        task_id="t-cf-clean",
        user_id=USER_ID,
        filename="placeholder.txt",
        project_id=None,
        create_new_project=True,
        import_mode="append",
        status="completed",
    )
    svc._tasks[task.task_id] = task

    async def fake_prepare_project(**kwargs):
        return SimpleNamespace(id="p-cf", character_count=5, current_words=0)

    async def fake_import_outlines(**kwargs):
        return {}

    async def fake_import_chapters(**kwargs):
        return 3, 1000

    async def fake_generate_world(**kwargs):
        return 1

    async def fake_generate_careers(**kwargs):
        return 2

    async def fake_generate_characters(**kwargs):
        return 3

    async def fake_extract_relationships(**kwargs):
        return {
            "extracted_relationships": 1,
            "created_types": 1,
            "created_characters": 1,
            "skipped_chapters": [],
        }

    monkeypatch.setattr(svc, "_prepare_project", fake_prepare_project)
    monkeypatch.setattr(svc, "_import_outlines", fake_import_outlines)
    monkeypatch.setattr(svc, "_import_chapters", fake_import_chapters)
    monkeypatch.setattr(svc, "_generate_world_building_from_project", fake_generate_world)
    monkeypatch.setattr(svc, "_generate_career_system_from_project", fake_generate_careers)
    monkeypatch.setattr(svc, "_generate_characters_and_organizations_from_project", fake_generate_characters)
    monkeypatch.setattr(svc, "_extract_relationships_from_chapters", fake_extract_relationships)

    calls: list[dict] = []

    async def progress_callback(message, progress, status="processing", code=None, params=None):
        calls.append({"message": message, "status": status})

    result = await svc.apply_import_stream(
        task_id=task.task_id,
        user_id=USER_ID,
        payload=BookImportApplyRequest(
            project_suggestion=ProjectSuggestion(title="占位项目"),
            chapters=[],
        ),
        db=AsyncMock(),
        progress_callback=progress_callback,
        ai_service=None,
    )

    assert result.skipped_chapters == []
    assert result.statistics["skipped_chapters"] == []
    assert task.skipped_chapters == []
    assert not [c for c in calls if c["status"] == "step_failures"]
