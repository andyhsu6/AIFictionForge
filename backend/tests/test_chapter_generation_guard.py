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
    length_floor_for,
    needs_top_up,
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


# ===== 接续提示词 =====


def test_continuation_block_carries_tail_and_remaining_budget():
    block = build_continuation_block("甲" * 2000, 3000, "zh")
    assert "已有正文" in block
    assert "至少再写" in block
    # 只带末尾 1200 字，避免把整章再灌一遍
    assert block.count("甲") == 1200
    assert "790" in block  # 下限 2790，已写 2000


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


# ===== 断点写入器 =====


class _FakeDB:
    def __init__(self, fail: bool = False):
        self.fail = fail
        self.commits = 0
        self.rollbacks = 0

    async def commit(self):
        if self.fail:
            raise RuntimeError("database is locked")
        self.commits += 1

    async def rollback(self):
        self.rollbacks += 1


class _FakeChapter:
    id = "chapter-1"
    content = None
    word_count = 0
    status = "draft"


def test_checkpointer_writes_draft_and_marks_writing():
    db, chapter = _FakeDB(), _FakeChapter()
    cp = ChapterCheckpointer(db, chapter)
    assert asyncio.run(cp.maybe_checkpoint("字" * 1500)) is True
    assert chapter.content == "字" * 1500
    # word_count 必须留给落库阶段当"生成前字数"用，断点不能改它
    assert chapter.word_count == 0
    assert chapter.status == "writing"
    assert db.commits == 1


def test_checkpointer_never_overwrites_a_completed_chapter():
    """重生成失败不能把已有成稿替换成半截草稿。"""
    db, chapter = _FakeDB(), _FakeChapter()
    chapter.status = "completed"
    chapter.content = "原来的成稿"
    cp = ChapterCheckpointer(db, chapter)
    assert asyncio.run(cp.maybe_checkpoint("字" * 1500, force=True)) is False
    assert chapter.content == "原来的成稿"
    assert db.commits == 0


def test_checkpointer_respects_threshold():
    db, chapter = _FakeDB(), _FakeChapter()
    cp = ChapterCheckpointer(db, chapter, min_seconds=10_000)
    assert asyncio.run(cp.maybe_checkpoint("字" * 300)) is False
    assert db.commits == 0


def test_checkpointer_degrades_when_db_is_locked():
    """断点保存失败只能降级，绝不能把生成打断（database is locked 是真实故障）。"""
    db, chapter = _FakeDB(fail=True), _FakeChapter()
    cp = ChapterCheckpointer(db, chapter)
    assert asyncio.run(cp.maybe_checkpoint("字" * 1500)) is False
    assert db.rollbacks == 1
    assert cp.writes == 0


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
    assert source.count("should_resume(") >= 6, "三条路径都要在提示词与初值处判定续写"
    assert source.count("needs_top_up(") == 3, "篇幅补足必须覆盖 SSE/后台/批量"
    assert source.count("build_continuation_block(") >= 6, "续写与补写都要复用同一提示词块"
