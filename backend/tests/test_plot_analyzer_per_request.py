"""#63 回归测试：PlotAnalyzer 必须按请求构造，不能缓存全局实例。

背景：`get_plot_analyzer` 把首个请求的 AIService 缓存在模块全局里。AIService
携带 user_id + db_session，且上下文窗口结论缓存按 (user, provider, base_url,
model) 取键，于是第一个调用者的身份与被 request 生命周期绑定的会话被后续所有
用户复用。

本测试走真实路由 `POST /api/memories/projects/{project_id}/analyze-chapter/{chapter_id}`
（直接调用路由处理函数 + 真实 db session，沿用 tests/test_careers_delete.py 的约定），
用两个不同用户各调用一次，断言：

1. 每次请求都构造了新的 PlotAnalyzer（不是复用某个缓存实例）；
2. 每次构造拿到的 `ai_service.user_id` 对应当次请求的用户；
3. `plot_analyzer` 模块不再导出缓存的全局实例符号。

AI 调用点在 `ai_service` 边界被替换为预置结果（测试关注构造/身份，不关注模型输出）。
"""
import os
import uuid
from types import SimpleNamespace

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.database import Base
from app.models.chapter import Chapter
from app.models.project import Project
from app.models.settings import Settings
from app.api import memories as memories_api
from app.services import plot_analyzer as plot_analyzer_module
from app.services.plot_analyzer import PlotAnalyzer
from app.services.memory_service import memory_service


# 预置分析结果：满足路由对 analysis_result 的字段访问；空列表让下游实体更新直接跳过。
CANNED_ANALYSIS = {
    "plot_stage": "发展",
    "conflict": {"level": 5, "types": ["人与人"]},
    "emotional_arc": {"primary_emotion": "紧张", "intensity": 5, "start": 0.3, "middle": 0.7, "end": 0.5},
    "hooks": [],
    "foreshadows": [],
    "plot_points": [],
    "character_states": [],
    "organization_states": [],
    "scenes": [],
    "pacing": "moderate",
    "dialogue_ratio": 0.4,
    "description_ratio": 0.3,
    "scores": {"overall": 8, "pacing": 8, "engagement": 7, "coherence": 8},
    "suggestions": [],
}


@pytest.fixture
async def db_session():
    """临时文件 SQLite（避免 in-memory 多连接问题），测试后清理。"""
    db_path = f"/tmp/test_plot_analyzer_{uuid.uuid4().hex}.db"
    engine = create_async_engine(f"sqlite+aiosqlite:///{db_path}")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    Session = async_sessionmaker(bind=engine, expire_on_commit=False)
    async with Session() as session:
        yield session
    await engine.dispose()
    if os.path.exists(db_path):
        os.remove(db_path)


def _make_request(user_id: str) -> SimpleNamespace:
    """构造最小 request，模拟已登录用户（路由读 request.state.user_id）。"""
    return SimpleNamespace(state=SimpleNamespace(user_id=user_id))


async def _seed_user(db, user_id: str, suffix: str):
    """建一个属于该用户的项目 + 有内容的章节 + AI 设置，返回 (project_id, chapter_id)。"""
    project = Project(id=f"proj-{suffix}", user_id=user_id, title=f"项目{suffix}")
    chapter = Chapter(
        id=f"chap-{suffix}",
        project_id=project.id,
        chapter_number=1,
        title="第一章",
        content="这是章节内容。",
        word_count=6,
    )
    settings = Settings(
        id=f"set-{suffix}",
        user_id=user_id,
        api_provider="openai",
        api_key="test-key",
        api_base_url="http://example.invalid/v1",
        llm_model="test-model",
    )
    db.add_all([project, chapter, settings])
    await db.commit()
    return project.id, chapter.id


@pytest.mark.anyio
async def test_analyze_chapter_builds_plot_analyzer_per_request(db_session, monkeypatch):
    """两个不同用户的 analyze-chapter 请求必须各自构造 PlotAnalyzer 并拿到自己的 AIService。"""
    constructions = []
    ai_services = []

    original_init = PlotAnalyzer.__init__

    def spy_init(self, ai_service):
        original_init(self, ai_service)
        constructions.append((self, ai_service))

    async def fake_analyze_chapter(self, **kwargs):
        return dict(CANNED_ANALYSIS)

    def fake_create_user_ai_service(**kwargs):
        service = SimpleNamespace(
            user_id=kwargs.get("user_id"),
            db_session=kwargs.get("db_session"),
        )
        ai_services.append(service)
        return service

    async def noop_delete_chapter_memories(*args, **kwargs):
        return None

    monkeypatch.setattr(PlotAnalyzer, "__init__", spy_init)
    monkeypatch.setattr(PlotAnalyzer, "analyze_chapter", fake_analyze_chapter)
    monkeypatch.setattr(memories_api, "create_user_ai_service", fake_create_user_ai_service)
    monkeypatch.setattr(memory_service, "delete_chapter_memories", noop_delete_chapter_memories)

    user_a, user_b = "user-a", "user-b"
    proj_a, chap_a = await _seed_user(db_session, user_a, "a")
    proj_b, chap_b = await _seed_user(db_session, user_b, "b")

    await memories_api.analyze_chapter(
        project_id=proj_a, chapter_id=chap_a, request=_make_request(user_a), db=db_session
    )
    await memories_api.analyze_chapter(
        project_id=proj_b, chapter_id=chap_b, request=_make_request(user_b), db=db_session
    )

    # 1. 每个请求各构造一次 PlotAnalyzer（缓存共享实例时这里只会是 1）。
    assert len(constructions) == 2, (
        f"expected one PlotAnalyzer per request, got {len(constructions)}"
    )
    instances = [instance for instance, _ in constructions]
    assert instances[0] is not instances[1], "requests reused a single PlotAnalyzer instance"

    # 2. 每次构造拿到的 ai_service 身份对应当次请求的用户。
    assert [service.user_id for _, service in constructions] == [user_a, user_b]

    # 3. 模块不再导出缓存的全局实例符号。
    assert not hasattr(plot_analyzer_module, "_plot_analyzer_instance")
    assert not hasattr(plot_analyzer_module, "get_plot_analyzer")
