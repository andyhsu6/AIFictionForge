"""断点保存 / 断点续写 / 篇幅补足的契约测试。

背景（issue #295 后续）：生成期间原本只在流结束后一次性落库，请求一断就零产出；
低/中思考档还会提前收尾（3,000 字目标实测出现 2,252 字且 finish_reason=stop）。

这些行为都发生在"模型说了算"的边界上，只能靠单元 + 源码接线守卫钉住：
- 断点写入必须失败降级（不能因为保存失败把生成打断）；
- 只有 writing + 足够长的草稿才允许续写（否则会接着一个空壳/垃圾继续）；
- 只有 finish_reason=stop 且正文偏短才补写（预算耗尽是另一条路径）。
"""
import asyncio

import pytest

from app.services.chapter_generation_guard import (
    ChapterCheckpointer,
    LENGTH_FLOOR_RATIO,
    MIN_RESUMABLE_CHARS,
    build_continuation_block,
    build_resume_plan,
    length_floor_for,
    needs_top_up,
    restore_before_top_up,
    should_resume,
    top_up_max_tokens,
)


# ===== 下限计算 =====


@pytest.mark.parametrize(
    "target,expected",
    [(3000, int(3000 * LENGTH_FLOOR_RATIO)), (5000, int(5000 * LENGTH_FLOOR_RATIO))],
)
def test_length_floor_tracks_target(target, expected):
    assert length_floor_for(target) == expected


def test_length_floor_has_a_minimum_and_handles_missing_target():
    assert length_floor_for(500) == 500  # max(500, 465)
    assert length_floor_for(None) == 0
    assert length_floor_for(0) == 0
    assert length_floor_for("bad") == 0


# ===== 续写判定 =====


def test_should_resume_only_for_interrupted_drafts():
    assert should_resume("writing", "字" * 400) is True
    assert should_resume("writing", "字" * (MIN_RESUMABLE_CHARS - 1)) is False
    assert should_resume("completed", "字" * 4000) is False
    assert should_resume("draft", "字" * 4000) is False
    assert should_resume(None, None) is False


# ===== 补写判定 =====


def test_needs_top_up_for_short_but_completed_output():
    assert needs_top_up(2252, 3000, "stop") is True


def test_needs_top_up_skips_budget_exhaustion_and_empty_output():
    assert needs_top_up(2252, 3000, "length") is False
    assert needs_top_up(120, 3000, "stop") is False
    assert needs_top_up(3000, 3000, "stop") is False
    assert needs_top_up(2252, None, "stop") is False
    # 3000-6000 软提倡下，达到 2500 字自然成章不再被 6000 目标强迫凑字补写
    assert needs_top_up(3200, 6000, "stop") is False


def test_generation_system_prompt_prioritizes_style_over_skill():
    from app.api.chapters import _build_generation_system_prompt

    prompt = _build_generation_system_prompt("本书文笔画像内容", "SKILL_STORY_DESLOP")
    assert prompt is not None
    assert "本书写作风格要求 - 最高优先级" in prompt
    assert prompt.index("本书文笔画像内容") < prompt.index("Skill 辅助参考")
    assert "严禁输出 Skill 自带的检测报告、统计表格或目录结构模板" in prompt


def test_every_generation_path_uses_unified_style_skill_builder():
    source = _CHAPTERS_PY.read_text(encoding="utf-8")
    assert source.count("= _build_generation_system_prompt(") == 3, (
        "SSE/后台/批量三条生成路径必须共用同一系统提示词构建器，避免优先级漂移"
    )


# ===== 接续提示词 =====


def test_continuation_block_carries_tail_and_remaining_budget():
    block = build_continuation_block("甲" * 2000, 3000, "zh")
    assert "已有正文" in block
    assert "至少再写" in block
    # 只带末尾 1200 字，避免把整章再灌一遍
    assert block.count("甲") == 1200
    assert "500" in block  # 软兜底下限 2500，已写 2000

    # 6000 目标且触发异常短章补写时，仍只按软兜底 2500 提示剩余字数，不逼写 3580 字
    block_6k = build_continuation_block("甲" * 2000, 6000, "zh")
    assert "500" in block_6k
    assert "3580" not in block_6k


def test_continuation_block_english_and_resume_wording():
    zh = build_continuation_block("字" * 500, 3000, "zh", reason="resume")
    assert "续写中断的草稿" in zh
    en = build_continuation_block("word " * 100, 2000, "en")
    assert "at least" in en and "existing text" in en


def test_top_up_budget_scales_with_remaining_and_respects_ceiling():
    budget = top_up_max_tokens(1000, 3000, 99999)
    assert 800 < budget <= 99999
    assert top_up_max_tokens(9000, 3000, 99999) == 800  # 已达标 → 仍给最小预算
    assert top_up_max_tokens(1000, 3000, 3000) == 3000  # 不超过本轮计算的上限


# ===== 源码接线守卫 =====

from pathlib import Path as _Path  # noqa: E402

_CHAPTERS_PY = _Path(__file__).resolve().parents[1] / "app" / "api" / "chapters.py"


def test_every_full_chapter_path_checkpoints_and_continues():
    """三条全章路径都必须接上断点保存、断点续写、篇幅补足。

    这三件事都没有行为级断言能守住（要真跑模型），因此和写作规格尾块一样
    用源码守卫钉住接线：漏一处就等于该路径回到"中断即零产出 / 短章照收"。
    """
    source = _CHAPTERS_PY.read_text(encoding="utf-8")
    assert source.count("ChapterCheckpointer(") == 3, "断点写入器必须覆盖 SSE/后台/批量"
    assert source.count("build_resume_plan(") == 3, (
        "续写判定必须三条路径各一次（提示词块与初值同源，不能各判一次）"
    )
    assert source.count("needs_top_up(") == 3, "篇幅补足必须覆盖 SSE/后台/批量"
    assert source.count("restore_before_top_up(") == 3, "补写失败必须三条路径都截回快照点"


# ===== 续写计划：提示词块与初值必须同源 =====


def test_resume_plan_returns_block_and_initial_content_together():
    content = "字" * 1500
    block, initial = build_resume_plan("writing", content, 3000, "zh")
    assert initial == content
    assert "已有正文" in block
    assert block.count  # 非空


def test_resume_plan_is_empty_when_not_resumable():
    for status, content in (("completed", "字" * 4000), ("draft", ""), ("writing", "字" * 10)):
        block, initial = build_resume_plan(status, content, 3000, "zh")
        assert (block, initial) == ("", "")


def test_restore_before_top_up_drops_partial_continuation():
    saved = "甲" * 2000 + "乙" * 40  # 补写中途追加了 40 字
    assert restore_before_top_up(saved, 2000) == "甲" * 2000
    assert restore_before_top_up("甲" * 10, 0) == "甲" * 10


# ===== 真实 AsyncSession：断点必须用独立会话，锁冲突不得打断生成 =====


def _sqlite_engine(path, timeout=0.2):
    from sqlalchemy.ext.asyncio import create_async_engine

    return create_async_engine(
        f"sqlite+aiosqlite:///{path}", future=True, connect_args={"timeout": timeout}
    )


async def _prepare_sqlite(engine):
    from app.database import Base
    from app.models.chapter import Chapter  # noqa: F401 - 注册表
    from app.models.outline import Outline  # noqa: F401 - Chapter 的外键目标
    from app.models.project import Project  # noqa: F401 - Chapter 的外键目标

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)


@pytest.mark.anyio
async def test_checkpointer_writes_through_its_own_session(tmp_path):
    from sqlalchemy import select
    from sqlalchemy.ext.asyncio import async_sessionmaker, AsyncSession

    from app.models.chapter import Chapter
    from app.models.project import Project

    engine = _sqlite_engine(tmp_path / "ok.db")
    await _prepare_sqlite(engine)
    Session = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    try:
        async with Session() as seed:
            seed.add(Project(id="p1", user_id="u1", title="t", description="d"))
            seed.add(Chapter(id="c1", project_id="p1", chapter_number=1, title="c", status="draft"))
            await seed.commit()

        async with Session() as db:
            chapter = (
                await db.execute(select(Chapter).where(Chapter.id == "c1"))
            ).scalar_one()
            cp = ChapterCheckpointer(db, chapter)
            assert await cp.maybe_checkpoint("字" * 1500, force=True) is True
            assert chapter.status == "writing"
            assert cp.writes == 1

        # 独立会话读到的是真正的断点（不是内存对象）
        async with Session() as verify:
            saved = (
                await verify.execute(select(Chapter).where(Chapter.id == "c1"))
            ).scalar_one()
            assert saved.status == "writing"
            assert saved.content == "字" * 1500
    finally:
        await engine.dispose()


@pytest.mark.anyio
async def test_checkpointer_survives_a_database_lock(tmp_path):
    """真实 database is locked：断点返回 False，不抛异常，调用方 session 仍可用。"""
    from sqlalchemy import select, text
    from sqlalchemy.ext.asyncio import async_sessionmaker, AsyncSession

    from app.models.chapter import Chapter
    from app.models.project import Project

    path = tmp_path / "locked.db"
    engine = _sqlite_engine(path)
    await _prepare_sqlite(engine)
    Session = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    blocker = None
    try:
        async with Session() as seed:
            seed.add(Project(id="p1", user_id="u1", title="t", description="d"))
            seed.add(Chapter(id="c1", project_id="p1", chapter_number=1, title="c", status="draft"))
            await seed.commit()

        # 另一个连接持有写锁
        blocker = await engine.connect()
        await blocker.execute(text("BEGIN IMMEDIATE"))

        async with Session() as db:
            chapter = (
                await db.execute(select(Chapter).where(Chapter.id == "c1"))
            ).scalar_one()
            cp = ChapterCheckpointer(db, chapter)
            assert await cp.maybe_checkpoint("字" * 1500, force=True) is False
            assert cp.writes == 0
            # 关键：调用方 session 没有被 rollback 搞坏（rollback 会过期 ORM 对象，
            # 之后再碰 chapter 的属性就会抛 MissingGreenlet）
            assert chapter.status == "draft"
            assert (await db.execute(text("SELECT 1"))).scalar() == 1
    finally:
        if blocker is not None:
            try:
                await blocker.rollback()
            finally:
                await blocker.close()
        await engine.dispose()


@pytest.mark.anyio
async def test_checkpointer_never_overwrites_a_completed_chapter(tmp_path):
    """重生成失败不能把已有成稿替换成半截草稿。"""
    from sqlalchemy import select
    from sqlalchemy.ext.asyncio import async_sessionmaker, AsyncSession

    from app.models.chapter import Chapter
    from app.models.project import Project

    engine = _sqlite_engine(tmp_path / "done.db")
    await _prepare_sqlite(engine)
    Session = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    try:
        async with Session() as db:
            db.add(Project(id="p1", user_id="u1", title="t", description="d"))
            db.add(
                Chapter(
                    id="c1",
                    project_id="p1",
                    chapter_number=1,
                    title="c",
                    status="completed",
                    content="原来的成稿",
                )
            )
            await db.commit()

        async with Session() as db:
            chapter = (await db.execute(select(Chapter).where(Chapter.id == "c1"))).scalar_one()
            cp = ChapterCheckpointer(db, chapter)
            assert cp.enabled is False
            assert await cp.maybe_checkpoint("字" * 1500, force=True) is False

        async with Session() as verify:
            saved = (await verify.execute(select(Chapter).where(Chapter.id == "c1"))).scalar_one()
            assert saved.content == "原来的成稿"
            assert saved.status == "completed"
    finally:
        await engine.dispose()
