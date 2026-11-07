"""计划 §6.4：5 条生成路径的锚点注入快照测试（issue #254 P1）。

覆盖范围（计划 §6.2）：

1. 主生成流式 `chapters.generate_chapter_content_stream`（POST /{id}/generate-stream）
2. 后台生成 `chapters._run_chapter_generation_bg`（文件内两份同名定义，后一份生效）
3. 批量生成 `chapters.generate_single_chapter_for_batch`
4. 重生成 `chapter_regenerator.ChapterRegenerator.regenerate_with_feedback`
5. 局部重写 `chapters.partial_regenerate_stream` / `_stream_partial_generate` —— 断言**不注入**

全部走真实调用路径（临时文件 SQLite + `Base.metadata.create_all` + 桩 AI 在
`generate_text_stream` 边界记录 prompt，不触达 embedding / 真实 AI / 网络）。
断言两层：

- 系统提示词（system_prompt）里出现锚点块 `【本书原文语感锚点`，且原有风格注入
  （生成路径为 `【🎨 写作风格要求 - 最高优先级】` 包装）仍在，锚点块排在风格正文之后；
- 局部重写的最终 prompt 仍保留 `<style priority="P1">` 风格块，且不含锚点块
  （即便库里有可用锚点）。
"""
import asyncio
import inspect
import os
import uuid

import pytest
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import app.api.chapters as chapters_module
from app.api.chapters import (
    _run_chapter_generation_bg,
    _stream_partial_generate,
    generate_chapter_content_stream,
    generate_single_chapter_for_batch,
    partial_regenerate_stream,
    router as chapters_router,
)
from app.api.settings import get_user_ai_service
from app.core.errors import register_exception_handlers
from app.database import Base, get_db
from app.models.analysis_task import AnalysisTask
from app.models.chapter import Chapter
from app.models.project import Project
from app.models.writing_style import WritingStyle
from app.schemas.regeneration import ChapterRegenerateRequest
from app.services.chapter_regenerator import ChapterRegenerator
from app.services.style_anchors import (
    create_project_style_anchors,
    load_style_anchor_fragments,
)

USER_ID = "user-anchor-paths"
PROJECT_ID = "proj-anchor-paths"
STYLE_ID = 1
CHAPTER_1_ID = "ch-anchor-1"
CHAPTER_2_ID = "ch-anchor-2"

STYLE_CONTENT = "语感要求：句子偏短，多写动作与物件，少用形容词。"

ANCHOR_HEADER = "【本书原文语感锚点"
STYLE_SYSTEM_HEADER = "【🎨 写作风格要求 - 最高优先级】"
STYLE_P1_BLOCK = '<style priority="P1">'

MARKER_A = "雨落在青石板上"
MARKER_B = "海风从窗缝里钻进来"

TEXT_A = (
    "雨落在青石板上，声音细密。他沿着巷子往里走，墙头爬着半枯的藤。"
    "第三家没有点灯，门缝里透出一线灰白。他停下，抬手敲了三下。"
) * 4
TEXT_B = (
    "海风从窗缝里钻进来，带着咸味。桌上摊着一叠旧信，边角已经卷起。"
    "他没有点灯，只是把窗推开一条缝，看远处的船影在雾里慢慢淡去。"
) * 4


class _RecordingAIService:
    """LLM 调用边界拦截：记录 prompt / system_prompt，不调用真实 AI。"""

    default_model = "test-model"

    def __init__(self):
        self.chunks = ["（AI生成正文）"]
        self.prompts: list[str] = []
        self.stream_kwargs: list[dict] = []
        self.user_id = None
        self.db_session = None

    async def resolve_full_book_budget_chars(self, model=None) -> int:
        # 全书注入预算接缝；本文件的桩服务不绑定窗口探测，给正数预算走全量路径。
        return 600000

    async def generate_text_stream(self, *, prompt, **kwargs):
        self.prompts.append(prompt)
        self.stream_kwargs.append(kwargs)
        for chunk in self.chunks:
            yield chunk


class _FakeTracker:
    """后台任务进度追踪桩：记录 error 调用，其余为 no-op。"""

    def __init__(self):
        self.error_calls: list[tuple] = []

    async def loading(self, *args, **kwargs):
        return None

    async def preparing(self, *args, **kwargs):
        return None

    async def generating(self, *args, **kwargs):
        return None

    async def generating_chunk(self, *args, **kwargs):
        return None

    async def saving(self, *args, **kwargs):
        return None

    async def analyzing(self, *args, **kwargs):
        return None

    async def complete(self, *args, **kwargs):
        return None

    async def set_result(self, *args, **kwargs):
        return None

    async def check_cancelled(self):
        return False

    async def error(self, *args, **kwargs):
        self.error_calls.append((args, kwargs))


class _Env:
    def __init__(self, client, session_factory, fake_ai):
        self.client = client
        self.session_factory = session_factory
        self.fake_ai = fake_ai


def _sse_diagnostic(resp) -> str:
    for line in resp.text.splitlines():
        if line.startswith("data:") and '"error"' in line:
            return f"SSE error event: {line[:400]}"
    return f"no prompt captured; SSE head: {resp.text[:400]}"


@pytest.fixture
def env(monkeypatch):
    """临时文件 SQLite + 真实 chapters 路由 + 被拦截的 AI 服务。"""
    db_path = f"/tmp/test_style_anchors_paths_{uuid.uuid4().hex}.db"
    engine = create_async_engine(f"sqlite+aiosqlite:///{db_path}")
    session_factory = async_sessionmaker(bind=engine, expire_on_commit=False)

    async def _create_all():
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

    asyncio.run(_create_all())

    fake_ai = _RecordingAIService()

    app = FastAPI()
    register_exception_handlers(app)
    app.include_router(chapters_router)

    async def override_get_db():
        async with session_factory() as session:
            yield session

    app.dependency_overrides[get_db] = override_get_db
    app.dependency_overrides[get_user_ai_service] = lambda: fake_ai

    @app.middleware("http")
    async def fake_auth(request: Request, call_next):
        request.state.user_id = USER_ID
        return await call_next(request)

    # 章节流式端点在生成器内部直接调用 get_db(request)（非 Depends），需换模块绑定
    async def stream_get_db(request):
        async with session_factory() as session:
            yield session

    monkeypatch.setattr(chapters_module, "get_db", stream_get_db)

    # 生成后的分析管线与本测试无关：置为 no-op（返回 True 表示分析成功）
    async def _noop_analysis(**kwargs):
        return True

    monkeypatch.setattr(chapters_module, "analyze_chapter_background", _noop_analysis)

    # 伏笔自动标记不参与断言：置为 no-op，避免触达真实服务
    async def _noop_plant(**kwargs):
        return {"planted_count": 0}

    monkeypatch.setattr(
        chapters_module.foreshadow_service,
        "auto_plant_pending_foreshadows",
        _noop_plant,
    )

    yield _Env(TestClient(app, raise_server_exceptions=False), session_factory, fake_ai)

    app.dependency_overrides.clear()
    asyncio.run(engine.dispose())
    if os.path.exists(db_path):
        os.remove(db_path)


def seed(env: _Env, *, chapter_2_content: str = "") -> int:
    """播种 项目 + 2 章 + 全局预设风格 + 第 1 章锚点；返回创建的锚点数。"""

    async def _run() -> int:
        async with env.session_factory() as session:
            session.add(
                Project(
                    id=PROJECT_ID,
                    user_id=USER_ID,
                    title="占位项目",
                    current_words=0,
                    outline_mode="one-to-many",
                )
            )
            session.add(
                Chapter(
                    id=CHAPTER_1_ID,
                    project_id=PROJECT_ID,
                    chapter_number=1,
                    title="第1章",
                    content=TEXT_A,
                    word_count=len(TEXT_A),
                    status="completed",
                )
            )
            session.add(
                Chapter(
                    id=CHAPTER_2_ID,
                    project_id=PROJECT_ID,
                    chapter_number=2,
                    title="第2章",
                    content=chapter_2_content,
                    word_count=len(chapter_2_content),
                )
            )
            session.add(
                WritingStyle(
                    id=STYLE_ID,
                    user_id=None,
                    name="全局预设",
                    style_type="preset",
                    prompt_content=STYLE_CONTENT,
                )
            )
            session.add(
                AnalysisTask(
                    id="task-ch-1",
                    chapter_id=CHAPTER_1_ID,
                    user_id=USER_ID,
                    project_id=PROJECT_ID,
                    status="completed",
                    progress=100,
                )
            )
            await session.commit()
            created = await create_project_style_anchors(
                session,
                project_id=PROJECT_ID,
                ranges=[{"chapter": 1, "start": 0, "end": len(TEXT_A)}],
            )
            await session.commit()
            return created

    created = asyncio.run(_run())
    assert created > 0, "fixture 未生成锚点，后续注入断言无意义"
    return created


def _assert_generation_injected(system_prompt: str | None) -> None:
    """生成路径公共断言：风格包装仍在，锚点块追加在风格正文之后。"""
    assert system_prompt, "system_prompt 未注入（style_content 可能为空）"
    assert STYLE_SYSTEM_HEADER in system_prompt
    assert STYLE_CONTENT in system_prompt
    assert ANCHOR_HEADER in system_prompt
    assert MARKER_A in system_prompt
    # 目标章（第 2 章）之前的章号过滤：第 2 章自身的文本不得作为锚点
    assert MARKER_B not in system_prompt
    assert system_prompt.index(STYLE_CONTENT) < system_prompt.index(ANCHOR_HEADER)


# ========== 1. 主生成流式端点 ==========


def test_generate_stream_injects_anchor_block(env):
    seed(env)
    resp = env.client.post(
        f"/chapters/{CHAPTER_2_ID}/generate-stream",
        json={"style_id": STYLE_ID, "target_word_count": 800},
    )
    assert resp.status_code == 200
    assert env.fake_ai.stream_kwargs, _sse_diagnostic(resp)
    _assert_generation_injected(env.fake_ai.stream_kwargs[0]["system_prompt"])


# ========== 2. 后台生成（生效定义：文件内后一份同名函数） ==========


def test_background_generation_injects_anchor_block(env):
    seed(env)
    tracker = _FakeTracker()

    async def _run():
        async with env.session_factory() as session:
            await _run_chapter_generation_bg(
                task_input={
                    "chapter_id": CHAPTER_2_ID,
                    "style_id": STYLE_ID,
                    "target_word_count": 800,
                },
                db=session,
                ai_service=env.fake_ai,
                tracker=tracker,
                user_id=USER_ID,
                task_id="task-bg",
            )

    asyncio.run(_run())
    assert tracker.error_calls == []
    assert env.fake_ai.stream_kwargs, "后台生成未调用 generate_text_stream"
    _assert_generation_injected(env.fake_ai.stream_kwargs[0]["system_prompt"])


# ========== 3. 批量生成 worker ==========


def test_batch_generation_injects_anchor_block(env):
    seed(env)

    async def _run():
        async with env.session_factory() as session:
            chapter = (
                await session.execute(
                    select(Chapter).where(Chapter.id == CHAPTER_2_ID)
                )
            ).scalar_one()
            await generate_single_chapter_for_batch(
                db_session=session,
                chapter=chapter,
                user_id=USER_ID,
                style_id=STYLE_ID,
                target_word_count=800,
                ai_service=env.fake_ai,
                write_lock=asyncio.Lock(),
            )

    asyncio.run(_run())
    assert env.fake_ai.stream_kwargs, "批量生成未调用 generate_text_stream"
    _assert_generation_injected(env.fake_ai.stream_kwargs[0]["system_prompt"])


# ========== 4. 重生成 ==========


def test_regeneration_injects_anchor_block(env):
    seed(env, chapter_2_content=TEXT_B)

    async def _run():
        async with env.session_factory() as session:
            chapter = await session.get(Chapter, CHAPTER_2_ID)
            regenerator = ChapterRegenerator(env.fake_ai)
            events = [
                event
                async for event in regenerator.regenerate_with_feedback(
                    chapter=chapter,
                    analysis=None,
                    regenerate_request=ChapterRegenerateRequest(
                        custom_instructions="加强节奏",
                        target_word_count=800,
                    ),
                    project_context={"project_title": "占位项目"},
                    style_content=STYLE_CONTENT,
                    user_id=USER_ID,
                    db=session,
                )
            ]
            return events

    events = asyncio.run(_run())
    assert events, "重生成未产出事件"
    assert env.fake_ai.stream_kwargs, "重生成未调用 generate_text_stream"
    _assert_generation_injected(env.fake_ai.stream_kwargs[0]["system_prompt"])
    # 重生成把 style_content 同时注入 prompt（风格段），锚点随之进入
    assert ANCHOR_HEADER in env.fake_ai.prompts[0]


# ========== 5. 局部重写：不注入锚点 ==========


def test_partial_rewrite_does_not_inject_anchors(env):
    seed(env, chapter_2_content=TEXT_B)

    # 先确认库内确有可用锚点（负向断言的对照组）
    async def _check_anchors():
        async with env.session_factory() as session:
            project = await session.get(Project, PROJECT_ID)
            return await load_style_anchor_fragments(
                session, project=project, before_chapter_number=2
            )

    assert asyncio.run(_check_anchors()), "fixture 无可用锚点，负向断言无意义"

    selected = "海风从窗缝里钻进来，带着咸味。"
    start_pos = TEXT_B.index(selected)
    resp = env.client.post(
        f"/chapters/{CHAPTER_2_ID}/partial-regenerate-stream",
        json={
            "selected_text": selected,
            "start_position": start_pos,
            "end_position": start_pos + len(selected),
            "user_instructions": "加强感官细节",
            "style_id": STYLE_ID,
        },
    )
    assert resp.status_code == 200
    assert env.fake_ai.prompts, _sse_diagnostic(resp)
    prompt = env.fake_ai.prompts[0]
    # 原有风格块仍在
    assert STYLE_P1_BLOCK in prompt
    assert STYLE_CONTENT in prompt
    # 局部重写不注入锚点
    assert ANCHOR_HEADER not in prompt
    assert MARKER_A not in prompt


# ========== 静态护栏：注入点在哪、不在哪 ==========


def test_anchor_wiring_is_static_contract():
    """源码层护栏：4 条生成路径显式接锚点，两条局部重写函数不得接。

    真实调用测试已覆盖行为；这里防止未来改动把注入点挪走（或误接到局部重写）
    后仍因夹具巧合而通过。
    """
    wired = {
        "generate_chapter_content_stream": generate_chapter_content_stream,
        "generate_single_chapter_for_batch": generate_single_chapter_for_batch,
    }
    for name, func in wired.items():
        assert "append_project_style_anchors(" in inspect.getsource(func), name

    # 文件内有两份同名 _run_chapter_generation_bg，模块属性是后一份（生效定义）
    assert "append_project_style_anchors(" in inspect.getsource(
        _run_chapter_generation_bg
    )

    assert "append_project_style_anchors(" in inspect.getsource(
        ChapterRegenerator.regenerate_with_feedback
    )

    for name, func in {
        "partial_regenerate_stream": partial_regenerate_stream,
        "_stream_partial_generate": _stream_partial_generate,
    }.items():
        assert "append_project_style_anchors" not in inspect.getsource(func), name
