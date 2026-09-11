"""W5 章内 AI 续写（continue 模式）端点 + apply 契约测试。

fixture 模式沿用 test_generation_language_e2e.py：临时文件 SQLite + 真实
chapters 路由 + register_exception_handlers + dependency_overrides[get_db]，
在 LLM 调用边界（generate_text_stream）拦截记录 prompt/kwargs，不调用真实 AI。

覆盖矩阵（W5 任务书）：
  1. 省略 mode ≡ rewrite（start==end 仍拒绝；result 带 mode=rewrite）
  2. continue 允许 start==end；result 带 mode/content_hash/分段元数据；单请求单段
  3. 分段数学：segment_count 按 effective_segment_chars（非 SEGMENT_TARGET_CHARS）；
     k>0 用 rolling_context，不注入全量累积
  4. 守卫：target 超限 / segment_index 越界 / segment_count 超限 / 能力不足
  5. 能力钳制：思考模型抬底后 max_tokens 仍 <= output_limit
  6. apply continue 插入：前后缀字节一致 + 计数增量正确 + 再应用不腐坏
  7. 陈旧 content_hash 应用 → 409 content_hash_mismatch 且内容未变
   8. #45 回归：空输出 → internal.ai_empty_response（rewrite 恰好 1 次调用，与 HEAD 字节一致；
      continue 恰好重试 1 次 = 2 次调用）
   9. D7 近空重试仅限 continue：短而非零输出 rewrite 直接成功（1 次调用）/ continue 重试 1 次（2 次调用）
  10. D7 回声守卫：输出复述锚点尾部时剥除
  11. B7 确认：apply 仅改 chapter.word_count + project.current_words，无分析/记忆副作用

全部使用合成字符串，不含任何真实书籍原文。
"""
import asyncio
import hashlib
import json
import os
import uuid

import pytest
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import app.api.chapters as chapters_module
from app.api.chapters import router as chapters_router
from app.api.settings import get_user_ai_service
from app.core.errors import register_exception_handlers
from app.database import Base, get_db
from app.models.chapter import Chapter
from app.models.project import Project

USER_ID = "user-w5"
PROJECT_ID = "proj-w5"
CHAPTER_ID = "ch-w5"

# 合成章节内容：400 个“甲” + 100 个“乙”（无任何原文数据）
CONTENT_HEAD = "甲" * 400
CONTENT_TAIL = "乙" * 100
CHAPTER_CONTENT = CONTENT_HEAD + CONTENT_TAIL

# 足量合成生成输出（避开近空重试阈值干扰：> max(50, seg*0.1)）
GEN_TEXT = "续写" * 120  # 240 字


class _RecordingAIService:
    """LLM 调用边界拦截：记录 generate_text_stream 的 prompt/kwargs，不调真实 AI。"""

    default_model = "test-model"
    base_url = "https://api.example.test/v1"

    def __init__(self, chunks=None):
        self.chunks = [GEN_TEXT] if chunks is None else chunks
        self.prompts: list[str] = []
        self.stream_kwargs: list[dict] = []

    async def generate_text_stream(self, *, prompt, **kwargs):
        self.prompts.append(prompt)
        self.stream_kwargs.append(kwargs)
        for chunk in self.chunks:
            yield chunk


class _Env:
    def __init__(self, client, session_factory, fake_ai):
        self.client = client
        self.session_factory = session_factory
        self.fake_ai = fake_ai


@pytest.fixture
def env(monkeypatch):
    """临时文件 SQLite + 真实 chapters 路由 + 生产同款错误 handler + 假 AI 服务。"""
    db_path = f"/tmp/test_w5_continue_{uuid.uuid4().hex}.db"
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

    yield _Env(TestClient(app, raise_server_exceptions=False), SessionLocal, fake_ai)

    app.dependency_overrides.clear()
    asyncio.run(engine.dispose())
    if os.path.exists(db_path):
        os.remove(db_path)


def seed(env: _Env, content: str = CHAPTER_CONTENT, project_words: int | None = None):
    """落库：项目 + 章节（默认 500 字合成内容，字数与内容一致）。"""

    async def _run():
        async with env.session_factory() as session:
            session.add(Project(
                id=PROJECT_ID,
                user_id=USER_ID,
                title="测试项目",
                current_words=len(content) if project_words is None else project_words,
            ))
            session.add(Chapter(
                id=CHAPTER_ID,
                project_id=PROJECT_ID,
                chapter_number=1,
                title="第1章",
                content=content,
                word_count=len(content),
            ))
            await session.commit()

    asyncio.run(_run())


def sse_events(resp) -> list[dict]:
    events = []
    for line in resp.text.splitlines():
        if line.startswith("data: "):
            events.append(json.loads(line[6:]))
    return events


def result_event(resp) -> dict:
    for ev in sse_events(resp):
        if ev.get("type") == "result":
            return ev["data"]
    raise AssertionError(f"no result event; SSE head: {resp.text[:600]}")


def error_event(resp) -> dict | None:
    for ev in sse_events(resp):
        if ev.get("type") == "error":
            return ev
    return None


def regen(env: _Env, body: dict):
    return env.client.post(
        f"/chapters/{CHAPTER_ID}/partial-regenerate-stream", json=body
    )


def apply_regen(env: _Env, body: dict):
    return env.client.post(
        f"/chapters/{CHAPTER_ID}/apply-partial-regenerate", json=body
    )


def load_chapter(env: _Env) -> Chapter:
    async def _run():
        async with env.session_factory() as session:
            return (await session.execute(
                select(Chapter).where(Chapter.id == CHAPTER_ID)
            )).scalar_one()

    return asyncio.run(_run())


def load_project(env: _Env) -> Project:
    async def _run():
        async with env.session_factory() as session:
            return (await session.execute(
                select(Project).where(Project.id == PROJECT_ID)
            )).scalar_one()

    return asyncio.run(_run())


# ========== 阶段一：基线（改动前的 rewrite-only 行为） ==========


def _rewrite_body(**over):
    body = {
        "selected_text": CONTENT_HEAD[100:160],
        "start_position": 100,
        "end_position": 160,
        "user_instructions": "让节奏更紧凑一些",
    }
    body.update(over)
    return body


def test_baseline_omitted_mode_rewrite_flow_works(env):
    """省略 mode：rewrite 端到端可用，返回 result 事件与非空 new_text。"""
    seed(env)
    resp = regen(env, _rewrite_body())
    assert resp.status_code == 200, resp.text
    result = result_event(resp)
    assert result["new_text"] == GEN_TEXT
    assert result["word_count"] == len(GEN_TEXT)
    assert env.fake_ai.prompts, "未调用 generate_text_stream"


def test_baseline_rewrite_rejects_start_equals_end(env):
    """基线：rewrite 对 start==end 拒绝 polish_start_before_end。"""
    seed(env)
    resp = regen(env, _rewrite_body(selected_text="", start_position=200, end_position=200))
    assert resp.status_code == 400, resp.text
    assert resp.json()["code"] == "validation.polish_start_before_end"


# ========== 阶段二：continue 契约（实现前为红） ==========


CONTENT_HASH = hashlib.sha256(CHAPTER_CONTENT.encode("utf-8")).hexdigest()


def _continue_body(**over):
    """默认：章节末尾纯追加（start==end==len，selected_text 空 → 允许）。"""
    body = {
        "selected_text": "",
        "start_position": len(CHAPTER_CONTENT),
        "end_position": len(CHAPTER_CONTENT),
        "user_instructions": "从锚点之后向下续写",
        "mode": "continue",
        "target_word_count": 800,
    }
    body.update(over)
    return body


def _small_output_limit(monkeypatch, limit: int):
    """钳制 detect_max_output_tokens（chapters 命名空间绑定），驱动分段数学。"""
    monkeypatch.setattr(chapters_module, "detect_max_output_tokens", lambda m, b=None: limit)


# ---- 1. rewrite 结果携带 mode/content_hash（省略 mode ≡ rewrite） ----


def test_omitted_mode_result_carries_rewrite_mode_and_hash(env):
    seed(env)
    resp = regen(env, _rewrite_body())
    result = result_event(resp)
    assert result["mode"] == "rewrite"
    assert result["content_hash"] == CONTENT_HASH
    # rewrite 旧字段保持
    assert result["new_text"] == GEN_TEXT
    assert result["start_position"] == 100
    assert result["end_position"] == 160
    assert result["original_word_count"] == 60


# ---- 2. continue 允许 start==end；结果形状逐字段冻结 ----


def test_continue_allows_start_equals_end_and_freezes_result_shape(env):
    from app.services.language_resolver import LANGUAGE_INSTRUCTIONS

    seed(env)
    resp = regen(env, _continue_body())
    assert resp.status_code == 200, resp.text
    result = result_event(resp)
    assert result == {
        "new_text": GEN_TEXT,
        "word_count": len(GEN_TEXT),
        "original_word_count": len("（续写起点）"),
        "start_position": len(CHAPTER_CONTENT),
        "end_position": len(CHAPTER_CONTENT),
        "mode": "continue",
        "content_hash": CONTENT_HASH,
        "segment_index": 0,
        "segment_count": 1,
        "requested_chars": 800,
        "generated_chars": len(GEN_TEXT),
        "complete": True,
    }
    # 默认模型 test-model → 保守输出上限 8192 → effective 4096 ≥ target → 单段
    prompt = env.fake_ai.prompts[-1]
    assert "目标约800字" in prompt
    assert LANGUAGE_INSTRUCTIONS["zh"] in prompt  # content_language 经 format_prompt 注入
    assert "（这是章节结尾）" in prompt  # 末尾追加 → 后文占位


# ---- 3. 分段数学跟随 effective_segment_chars；k>0 用 rolling_context ----


def test_segmentation_follows_effective_segment_chars(env, monkeypatch):
    seed(env)
    _small_output_limit(monkeypatch, 3000)  # effective = min(8000, 1500) = 1500
    resp = regen(env, _continue_body(target_word_count=4000))
    result = result_event(resp)
    assert result["segment_count"] == 3  # ceil(4000/1500)，非 naive 8000 → 1
    assert result["requested_chars"] == 1500
    assert result["segment_index"] == 0
    assert result["complete"] is False
    assert len(env.fake_ai.prompts) == 1  # 单请求单段，客户端驱动 0..N-1

    # 第二段：rolling 尾摘要（80字）而非全量累积（2000字）注入
    rolling = "生成" * 40
    resp2 = regen(env, _continue_body(
        target_word_count=4000,
        segment_index=1,
        already_generated_chars=2000,
        rolling_context=rolling,
    ))
    result2 = result_event(resp2)
    assert result2["segment_index"] == 1
    assert result2["segment_count"] == 3
    assert result2["requested_chars"] == 1500  # min(1500, 4000-2000)
    assert result2["complete"] is False
    prompt2 = env.fake_ai.prompts[-1]
    assert rolling in prompt2
    assert "生成" * 100 not in prompt2  # 全量累积未注入
    assert "甲" * 50 not in prompt2  # 前窗未注入（context_before = rolling_context）
    assert "乙" * 50 not in prompt2  # 后窗未注入
    assert "（续写中，暂无后文）" in prompt2


def test_continue_segment_with_empty_rolling_falls_back_to_segment0(env, monkeypatch):
    seed(env)
    _small_output_limit(monkeypatch, 3000)
    resp = regen(env, _continue_body(
        target_word_count=4000, segment_index=1, already_generated_chars=1500,
        rolling_context="   ",
    ))
    assert resp.status_code == 200, resp.text
    prompt = env.fake_ai.prompts[-1]
    assert "甲" * 50 in prompt  # 回落 segment-0 窗口：前文注入章节正文


# ---- 4. 守卫 ----


def test_guard_target_exceeds_continue_max(env):
    seed(env)
    resp = regen(env, _continue_body(target_word_count=chapters_module.CONTINUE_MAX_TARGET_CHARS + 1))
    assert resp.status_code == 400, resp.text
    assert resp.json()["code"] == "validation.continue_target_too_large"


def test_guard_segment_index_beyond_count(env, monkeypatch):
    seed(env)
    _small_output_limit(monkeypatch, 3000)  # N = ceil(4000/1500) = 3
    resp = regen(env, _continue_body(target_word_count=4000, segment_index=3))
    assert resp.status_code == 400, resp.text
    assert resp.json()["code"] == "validation.continue_segment_index_invalid"


def test_guard_segment_count_over_max(env, monkeypatch):
    seed(env)
    _small_output_limit(monkeypatch, 2000)  # effective = 1000；ceil(25001/1000) = 26 > 24
    resp = regen(env, _continue_body(target_word_count=25001))
    assert resp.status_code == 400, resp.text
    assert resp.json()["code"] == "validation.continue_segment_limit_exceeded"


def test_guard_capability_insufficient_output_limit(env, monkeypatch):
    seed(env)
    _small_output_limit(monkeypatch, 1998)  # effective = 999 < CONTINUE_MIN_SEGMENT_CHARS
    resp = regen(env, _continue_body())
    body = resp.json()
    assert body["code"] == "internal.continue_capability_insufficient", resp.text
    assert resp.status_code == 200  # registry 默认 status（SSE 邻近内部码约定）


def test_guard_continue_anchor_exact_match_only_no_fuzzy(env):
    """continue 不做 ±50 模糊校正：偏移即 selection_mismatch。"""
    seed(env)
    resp = regen(env, _continue_body(
        selected_text="乙" * 30, start_position=398, end_position=428,
    ))
    assert resp.status_code == 400, resp.text
    assert resp.json()["code"] == "validation.polish_selection_mismatch"


def test_guard_continue_range_out_of_bounds(env):
    seed(env)
    resp = regen(env, _continue_body(start_position=501, end_position=501))
    assert resp.status_code == 400, resp.text
    assert resp.json()["code"] == "validation.polish_range_out_of_bounds"
    assert resp.json()["params"]["bound"] == "start"


# ---- 5. 能力钳制：思考模型抬底不得穿透输出上限 ----


def test_thinking_model_budget_clamped_to_output_limit(env, monkeypatch):
    seed(env)
    env.fake_ai.default_model = "deepseek-r1-test"  # 思考型（推理与正文共享预算）
    _small_output_limit(monkeypatch, 2000)  # effective 1000；floor 64000 > limit
    resp = regen(env, _continue_body(target_word_count=1500))
    assert resp.status_code == 200, resp.text
    result_event(resp)
    max_tokens = env.fake_ai.stream_kwargs[-1]["max_tokens"]
    assert max_tokens <= 2000  # 抬底后仍被钳回 provider 输出上限


# ---- 6. apply continue 插入 + 计数增量 ----


def test_apply_continue_insert_grows_counts_by_delta_without_corruption(env):
    seed(env)
    new_text = "插入的续写段落" * 10
    resp = apply_regen(env, {
        "new_text": new_text, "start_position": 300, "end_position": 300,
        "mode": "continue", "content_hash": CONTENT_HASH,
    })
    assert resp.status_code == 200, resp.text
    chapter = load_chapter(env)
    assert chapter.content == CHAPTER_CONTENT[:300] + new_text + CHAPTER_CONTENT[300:]
    assert chapter.word_count == len(chapter.content)
    assert load_project(env).current_words == len(CHAPTER_CONTENT) + len(new_text)

    # 再应用一次（新哈希，尾部追加）：增量数学不腐坏
    fresh_hash = hashlib.sha256(chapter.content.encode("utf-8")).hexdigest()
    resp2 = apply_regen(env, {
        "new_text": new_text, "start_position": len(chapter.content),
        "end_position": len(chapter.content), "mode": "continue",
        "content_hash": fresh_hash,
    })
    assert resp2.status_code == 200, resp2.text
    chapter2 = load_chapter(env)
    assert chapter2.content == CHAPTER_CONTENT[:300] + new_text + CHAPTER_CONTENT[300:] + new_text
    assert chapter2.word_count == len(chapter2.content)
    assert load_project(env).current_words == len(chapter2.content)


def test_apply_rewrite_still_rejects_start_equals_end(env):
    seed(env)
    resp = apply_regen(env, {"new_text": "替换文本" * 5, "start_position": 100, "end_position": 100})
    assert resp.status_code == 400, resp.text
    assert resp.json()["code"] == "validation.polish_position_invalid"


# ---- 7. 陈旧 content_hash → 409 且零写入 ----


def test_apply_stale_content_hash_rejected_without_write(env):
    seed(env)
    stale = hashlib.sha256("与当前章节内容无关的字符串".encode("utf-8")).hexdigest()
    resp = apply_regen(env, {
        "new_text": "篡改内容" * 5, "start_position": 250, "end_position": 250,
        "mode": "continue", "content_hash": stale,
    })
    assert resp.status_code == 409, resp.text
    assert resp.json()["code"] == "validation.content_hash_mismatch"
    chapter = load_chapter(env)
    assert chapter.content == CHAPTER_CONTENT
    assert chapter.word_count == len(CHAPTER_CONTENT)
    assert load_project(env).current_words == len(CHAPTER_CONTENT)


def test_apply_without_hash_still_works_for_legacy_clients(env):
    """旧客户端不带 content_hash：不触发 D6 校验（向后兼容）。"""
    seed(env)
    new_text = "旧版应用文本" * 5
    resp = apply_regen(env, {
        "new_text": new_text, "start_position": 100, "end_position": 160, "mode": "continue",
    })
    assert resp.status_code == 200, resp.text
    assert load_chapter(env).content == CHAPTER_CONTENT[:100] + new_text + CHAPTER_CONTENT[160:]


# ---- 8. #45 回归：空输出 → internal.ai_empty_response（rewrite 无重试 / continue 恰好重试一次） ----


@pytest.mark.parametrize("mode,expected_calls", [("rewrite", 1), ("continue", 2)])
def test_empty_ai_output_then_ai_empty_response(env, mode, expected_calls):
    """rewrite 空输出字节一致地不重试（HEAD 行为）；continue 有界重试恰好 1 次（D7）。"""
    seed(env)
    env.fake_ai.chunks = []  # provider 始终返回空
    body = _continue_body() if mode == "continue" else _rewrite_body()
    resp = regen(env, body)
    assert resp.status_code == 200, resp.text  # SSE 通道内报错
    ev = error_event(resp)
    assert ev is not None, f"missing error event; {resp.text[:400]}"
    assert ev["error_code"] == "internal.ai_empty_response"
    assert len(env.fake_ai.prompts) == expected_calls
    assert not any(e.get("type") == "result" for e in sse_events(resp))


# ---- 9. D7 近空重试仅限 continue：近空而非零输出的分流 ----


@pytest.mark.parametrize("mode,expected_calls", [("rewrite", 1), ("continue", 2)])
def test_near_empty_nonzero_output_retry_scoped_to_continue(env, mode, expected_calls):
    """短输出（清洗后30字）低于 max(50, 目标*0.1) 近空阈值：
    rewrite 必须按 HEAD 行为直接成功且仅 1 次上游调用（无近空重试）；
    continue 触发恰好 1 次有界重试（2 次调用）后成功。"""
    seed(env)
    short_text = "短句" * 15  # 30 字，非空；rewrite 阈值 max(50, 90*0.1)=50，continue 阈值 max(50, 800*0.1)=80
    env.fake_ai.chunks = [short_text]
    body = _continue_body() if mode == "continue" else _rewrite_body()
    resp = regen(env, body)
    assert resp.status_code == 200, resp.text
    assert error_event(resp) is None, f"不应报错: {resp.text[:400]}"
    result = result_event(resp)
    assert result["mode"] == mode
    assert result["new_text"] == short_text
    assert result["word_count"] == len(short_text)
    assert len(env.fake_ai.prompts) == expected_calls


# ---- 10. D7 回声守卫：剥除复述锚点尾部的输出 ----


def test_continue_strips_anchored_echo_prefix(env):
    seed(env)
    anchor = "甲" * 30  # 探针 = 锚点后 ≤40 字（len 30 ≥ 8）
    env.fake_ai.chunks = [anchor + GEN_TEXT]
    resp = regen(env, _continue_body(
        selected_text=anchor, start_position=370, end_position=400,
    ))
    assert resp.status_code == 200, resp.text
    result = result_event(resp)
    assert result["new_text"] == GEN_TEXT  # 重复的锚点尾部回声已剥除
    assert result["start_position"] == 400 and result["end_position"] == 400


# ---- 11. B7 确认：apply 仅动 chapter.word_count / project.current_words ----


def _table_counts(env: _Env) -> dict:
    async def _run():
        async with env.session_factory() as s:
            return {
                name: (await s.execute(select(func.count()).select_from(table))).scalar_one()
                for name, table in Base.metadata.tables.items()
            }

    return asyncio.run(_run())


def _column_map(env: _Env, model, row_id: str) -> dict:
    from sqlalchemy import inspect

    async def _run():
        async with env.session_factory() as s:
            obj = (await s.execute(select(model).where(model.id == row_id))).scalar_one()
            return {c.name: getattr(obj, c.name) for c in inspect(model).columns}

    return asyncio.run(_run())


def test_apply_side_effects_bounded_to_word_counts(env):
    seed(env)
    counts_before = _table_counts(env)
    chapter_before = _column_map(env, Chapter, CHAPTER_ID)
    project_before = _column_map(env, Project, PROJECT_ID)
    # 前提：分析/记忆表全空，任何新行都是副作用
    assert counts_before["analysis_tasks"] == 0
    assert counts_before["plot_analysis"] == 0
    assert counts_before["story_memories"] == 0

    new_text = "确认无副作用的续写" * 6
    resp = apply_regen(env, {
        "new_text": new_text, "start_position": 10, "end_position": 10,
        "mode": "continue", "content_hash": CONTENT_HASH,
    })
    assert resp.status_code == 200, resp.text

    # 行数不变：没有任何 INSERT/DELETE（无 AnalysisTask/记忆写入等副作用）
    assert _table_counts(env) == counts_before

    chapter_after = _column_map(env, Chapter, CHAPTER_ID)
    allowed_ch = {"content", "word_count", "updated_at"}
    changed = {k for k in chapter_after if chapter_after[k] != chapter_before[k]}
    assert changed <= allowed_ch, f"chapter 多余变更: {changed - allowed_ch}"
    assert chapter_after["word_count"] == len(chapter_after["content"])

    project_after = _column_map(env, Project, PROJECT_ID)
    allowed_pr = {"current_words", "updated_at"}
    changed_pr = {k for k in project_after if project_after[k] != project_before[k]}
    assert changed_pr <= allowed_pr, f"project 多余变更: {changed_pr - allowed_pr}"
    assert project_after["current_words"] == project_before["current_words"] + len(new_text)
