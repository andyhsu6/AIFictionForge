"""per-generation 语言覆盖端到端验证（i18n plan todo 19）。

验证边界：HTTP 请求（TestClient + 真实路由）→ 请求 schema（content_language）
→ resolve_user_generation_language 优先级链（读用户 Settings.preferences）
→ PromptService.format_prompt 尾部注入 → 传给模型层的最终 prompt。
在 LLM 调用边界（generate_text_stream）拦截记录 prompt，不调用真实 AI；
不断言模型输出语言（输出语言不可靠，属用户数据）。

用例矩阵（plan todo 19 a-g）：
  a. 章节流式生成 per-gen en，用户偏好 zh → prompt 含英文指令
  b. 无 per-gen，preferences.content_language=en → 全局设置生效
  c. 无 per-gen、content_language 未设、preferences.language=en → UI 语言回落
  d. per-gen zh 覆盖偏好 en（override > global）
  e. 大纲流式生成 per-gen en（覆盖偏好 zh）
  f. DB 自定义模板（prompt_templates 行覆盖章节模板）→ 指令仍追加在模板体之后
  g. 批量生成：端点 → 后台任务 payload 携带 content_language；
     worker（generate_single_chapter_for_batch）prompt 构造解析 per-gen
附加：无任何设置时回落 zh（链底默认，负向对照）。
"""
import asyncio
import json
import os
import uuid

import pytest
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import app.api.chapters as chapters_module
from app.api.chapters import (
    generate_single_chapter_for_batch,
    router as chapters_router,
)
from app.api.outlines import router as outlines_router
from app.api.settings import get_user_ai_service
from app.core.errors import register_exception_handlers
from app.database import Base, get_db
from app.models.chapter import Chapter
from app.models.project import Project
from app.models.prompt_template import PromptTemplate
from app.models.settings import Settings
from app.services.language_resolver import LANGUAGE_INSTRUCTIONS

EN_INSTRUCTION = LANGUAGE_INSTRUCTIONS["en"]
ZH_INSTRUCTION = LANGUAGE_INSTRUCTIONS["zh"]

USER_ID = "user-e2e"
PROJECT_ID = "proj-e2e"

# DB 自定义模板（覆盖 CHAPTER_GENERATION_ONE_TO_MANY，首章 1-N 路径），
# 占位符与端点传参一一对应（chapters.py 首章分支的 format kwargs）
CUSTOM_CHAPTER_TEMPLATE = (
    "【DB自定义章节模板】书名:{project_title} 第{chapter_number}章《{chapter_title}》"
    "大纲:{chapter_outline} 字数:{target_word_count} 类型:{genre}"
    "视角:{narrative_perspective} 角色:{characters_info} 职业:{chapter_careers}"
    "伏笔:{foreshadow_reminders} 记忆:{relevant_memories}"
)
CUSTOM_TEMPLATE_MARKER = "【DB自定义章节模板】"


class _RecordingAIService:
    """LLM 调用边界拦截：记录 generate_text_stream 收到的 prompt，不调用真实 AI。"""

    default_model = "test-model"

    def __init__(self):
        self.chunks = ["（AI生成正文）"]
        self.prompts: list[str] = []
        self.stream_kwargs: list[dict] = []
        self.user_id = None
        self.db_session = None

    async def resolve_full_book_budget_chars(self, model=None) -> int:
        """全书注入预算接缝（需求 #55 步骤 4 起由 AIService 提供）。

        本文件测的是生成语言解析，假服务也没绑定 user_id/db_session，
        所以这里直接给一个 ≥1M 窗口对应的正数预算（1M × 0.6），
        保证全书注入照常走全量路径。窗口门禁与预算来源本身由
        `test_context_window_probe_gate.py` / `test_no_fallback_degradation.py`
        用真实 AIService 覆盖。
        """
        return 600000

    async def generate_text_stream(self, *, prompt, **kwargs):
        self.prompts.append(prompt)
        self.stream_kwargs.append(kwargs)
        for chunk in self.chunks:
            yield chunk


class _Env:
    """TestClient + 临时库会话工厂 + 被拦截的 AI 服务。"""

    def __init__(self, client, session_factory, fake_ai):
        self.client = client
        self.session_factory = session_factory
        self.fake_ai = fake_ai


def _sse_diagnostic(resp) -> str:
    """SSE 流里出现 error 事件时把诊断信息带进断言失败消息。"""
    for line in resp.text.splitlines():
        if line.startswith("data:") and '"error"' in line:
            return f"SSE error event: {line[:400]}"
    return f"no prompt captured; SSE head: {resp.text[:400]}"


@pytest.fixture
def env(monkeypatch):
    """临时文件 SQLite + 真实 chapters/outlines 路由 + 生产同款错误 handler。

    - Depends(get_db)（批量/大纲端点）→ dependency_overrides；
    - 章节流式端点在生成器内直接调用 get_db(request)（非 Depends），
      需替换 app.api.chapters 命名空间绑定；
    - 章节生成后的分析管线（analyze_chapter_background）与本任务无关，置为 no-op。
    """
    db_path = f"/tmp/test_gen_lang_e2e_{uuid.uuid4().hex}.db"
    engine = create_async_engine(f"sqlite+aiosqlite:///{db_path}")
    SessionLocal = async_sessionmaker(bind=engine, expire_on_commit=False)

    async def _create_all():
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

    asyncio.run(_create_all())

    fake_ai = _RecordingAIService()

    app = FastAPI()
    register_exception_handlers(app)
    app.include_router(chapters_router)
    app.include_router(outlines_router)

    async def override_get_db():
        async with SessionLocal() as session:
            yield session

    app.dependency_overrides[get_db] = override_get_db
    app.dependency_overrides[get_user_ai_service] = lambda: fake_ai

    @app.middleware("http")
    async def fake_auth(request: Request, call_next):
        request.state.user_id = USER_ID
        return await call_next(request)

    async def stream_get_db(request):
        async with SessionLocal() as session:
            yield session

    monkeypatch.setattr(chapters_module, "get_db", stream_get_db)

    async def _noop_analysis(**kwargs):
        return None

    monkeypatch.setattr(chapters_module, "analyze_chapter_background", _noop_analysis)

    yield _Env(TestClient(app, raise_server_exceptions=False), SessionLocal, fake_ai)

    app.dependency_overrides.clear()
    asyncio.run(engine.dispose())
    if os.path.exists(db_path):
        os.remove(db_path)


def seed(
    env: _Env,
    *,
    preferences=None,
    chapters=(1,),
    prompt_template=None,
    project_id=PROJECT_ID,
):
    """按用例需要落库：用户偏好 / 项目 / 章节 / DB 自定义提示词模板。"""
    template_key, template_content = prompt_template or (None, None)

    async def _run():
        async with env.session_factory() as session:
            if preferences is not None:
                session.add(Settings(
                    user_id=USER_ID,
                    preferences=json.dumps(preferences, ensure_ascii=False),
                ))
            session.add(Project(id=project_id, user_id=USER_ID, title="测试项目"))
            for num in chapters:
                session.add(Chapter(
                    id=f"ch-{num}-{project_id}",
                    project_id=project_id,
                    chapter_number=num,
                    title=f"第{num}章",
                ))
            if template_key:
                session.add(PromptTemplate(
                    user_id=USER_ID,
                    template_key=template_key,
                    template_name="测试自定义模板",
                    template_content=template_content,
                ))
            await session.commit()

    asyncio.run(_run())


# ========== a. 章节流式生成：per-gen override（en）覆盖偏好 zh ==========


def test_chapter_stream_per_gen_en_beats_zh_prefs(env):
    seed(env, preferences={"content_language": "zh"})
    resp = env.client.post(
        f"/chapters/ch-1-{PROJECT_ID}/generate-stream",
        json={"content_language": "en"},
    )
    assert resp.status_code == 200
    assert env.fake_ai.prompts, _sse_diagnostic(resp)
    prompt = env.fake_ai.prompts[0]
    assert EN_INSTRUCTION in prompt
    assert ZH_INSTRUCTION not in prompt


# ========== b. 全局设置：preferences.content_language=en（无 per-gen）==========


def test_chapter_stream_global_content_language_en(env):
    seed(env, preferences={"content_language": "en"})
    resp = env.client.post(f"/chapters/ch-1-{PROJECT_ID}/generate-stream", json={})
    assert resp.status_code == 200
    assert env.fake_ai.prompts, _sse_diagnostic(resp)
    prompt = env.fake_ai.prompts[0]
    assert EN_INSTRUCTION in prompt
    assert ZH_INSTRUCTION not in prompt


# ========== c. UI 语言回落：content_language 未设，preferences.language=en ==========


def test_chapter_stream_falls_back_to_ui_language(env):
    seed(env, preferences={"language": "en"})
    resp = env.client.post(f"/chapters/ch-1-{PROJECT_ID}/generate-stream", json={})
    assert resp.status_code == 200
    assert env.fake_ai.prompts, _sse_diagnostic(resp)
    prompt = env.fake_ai.prompts[0]
    assert EN_INSTRUCTION in prompt
    assert ZH_INSTRUCTION not in prompt


def test_chapter_stream_defaults_to_zh_without_any_setting(env):
    """链底负向对照：无 per-gen、无任何偏好 → 默认注入中文指令。"""
    seed(env)
    resp = env.client.post(f"/chapters/ch-1-{PROJECT_ID}/generate-stream", json={})
    assert resp.status_code == 200
    assert env.fake_ai.prompts, _sse_diagnostic(resp)
    prompt = env.fake_ai.prompts[0]
    assert ZH_INSTRUCTION in prompt
    assert EN_INSTRUCTION not in prompt


# ========== d. per-gen zh 覆盖偏好 en（override > global，反向）==========


def test_chapter_stream_per_gen_zh_beats_en_prefs(env):
    seed(env, preferences={"content_language": "en"})
    resp = env.client.post(
        f"/chapters/ch-1-{PROJECT_ID}/generate-stream",
        json={"content_language": "zh"},
    )
    assert resp.status_code == 200
    assert env.fake_ai.prompts, _sse_diagnostic(resp)
    prompt = env.fake_ai.prompts[0]
    assert ZH_INSTRUCTION in prompt
    assert EN_INSTRUCTION not in prompt


# ========== e. 大纲流式生成：per-gen en（覆盖偏好 zh）==========


def test_outline_stream_per_gen_en_beats_zh_prefs(env):
    seed(env, preferences={"content_language": "zh"}, chapters=())
    env.fake_ai.chunks = [
        '[{"title": "第一章", "summary": "概述一"}, {"title": "第二章", "summary": "概述二"}]'
    ]
    resp = env.client.post("/outlines/generate-stream", json={
        "project_id": PROJECT_ID,
        "chapter_count": 2,
        "mode": "new",
        "theme": "测试主题",
        "content_language": "en",
    })
    assert resp.status_code == 200
    assert env.fake_ai.prompts, _sse_diagnostic(resp)
    prompt = env.fake_ai.prompts[0]
    # 大纲路径无全书注入尾块，语言指令即最终 prompt 的最后一段
    assert prompt.endswith(EN_INSTRUCTION)
    assert ZH_INSTRUCTION not in prompt


# ========== f. DB 自定义模板：指令仍以独立尾段追加在模板体之后 ==========


def test_db_custom_template_still_gets_appended_language_tail(env):
    seed(
        env,
        preferences={"content_language": "en"},
        prompt_template=("CHAPTER_GENERATION_ONE_TO_MANY", CUSTOM_CHAPTER_TEMPLATE),
    )
    resp = env.client.post(f"/chapters/ch-1-{PROJECT_ID}/generate-stream", json={})
    assert resp.status_code == 200
    assert env.fake_ai.prompts, _sse_diagnostic(resp)
    prompt = env.fake_ai.prompts[0]
    # 走的是 DB 自定义模板（而非系统默认）
    assert CUSTOM_TEMPLATE_MARKER in prompt
    # 追加式注入保持：语言指令出现在模板渲染体之后（全书注入块之前亦成立）
    assert EN_INSTRUCTION in prompt
    assert prompt.index(CUSTOM_TEMPLATE_MARKER) < prompt.index(EN_INSTRUCTION)


# ========== g. 批量生成：payload 携带 + worker prompt 构造解析 ==========


def test_batch_endpoint_threads_content_language_into_worker_payload(env, monkeypatch):
    """端点把 content_language 原样传给后台任务 worker（payload 不断链）。"""
    seed(env, preferences={"content_language": "zh"}, chapters=(1,))

    captured: dict = {}

    async def fake_worker(**kwargs):
        captured.update(kwargs)

    monkeypatch.setattr(chapters_module, "execute_batch_generation_in_order", fake_worker)

    resp = env.client.post(
        f"/chapters/project/{PROJECT_ID}/batch-generate",
        json={"start_chapter_number": 1, "count": 1, "content_language": "en"},
    )
    assert resp.status_code == 200, resp.text
    assert captured.get("content_language") == "en"
    assert captured.get("ai_service") is env.fake_ai


def test_batch_worker_prompt_construction_resolves_per_gen_language(env):
    """worker（generate_single_chapter_for_batch）prompt 构造解析 per-gen：
    任务级 content_language=en 覆盖用户偏好 zh。"""
    seed(env, preferences={"content_language": "zh"}, chapters=(1,))

    async def _run():
        async with env.session_factory() as session:
            chapter = (await session.execute(
                select(Chapter).where(Chapter.id == f"ch-1-{PROJECT_ID}")
            )).scalar_one()
            await generate_single_chapter_for_batch(
                db_session=session,
                chapter=chapter,
                user_id=USER_ID,
                style_id=None,
                target_word_count=3000,
                ai_service=env.fake_ai,
                write_lock=asyncio.Lock(),
                content_language="en",
            )

    asyncio.run(_run())
    assert env.fake_ai.prompts, "worker 未调用 generate_text_stream"
    prompt = env.fake_ai.prompts[0]
    assert EN_INSTRUCTION in prompt
    assert ZH_INSTRUCTION not in prompt
