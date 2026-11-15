"""章节生成的断点保存、断点续写与篇幅补足。

实测依据（issue #295 后续，2026-11-15）：

- 原实现只在流结束后一次性落库，进程/请求一断就零产出——一次真实故障里同一章
  生成了四次，界面端一条都没拿到（另一半原因见任务轮询的 401 死循环）。
- 低/中思考档下模型会提前收尾：3,000 字目标实测出现过 2,252 字且
  ``finish_reason == "stop"``，所以"字数下限"只是提示词指令，不是保证。

本模块只提供纯函数与一个写入器，不调用模型、不依赖 request 上下文：

1. ``ChapterCheckpointer``：流式过程中按字数/时间阈值把草稿写进章节，``status``
   置为 ``writing``，中断后已写内容不丢；
2. ``should_resume`` / ``build_continuation_block``：下次生成时若发现 ``writing``
   状态且有足量草稿，从断点继续而不是从头重写；
3. ``needs_top_up``：正文正常收尾但低于下限时，调用方补写一轮。

``status`` 用既有的 ``draft | writing | completed`` 三态，不新增列、不改 schema。
"""

from __future__ import annotations

import logging
import time
from typing import Any, Optional

from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.models.chapter import Chapter

logger = logging.getLogger(__name__)

# 字数下限相对目标字数的比例（与提示词尾注同一口径，见 chapters._length_floor_note）
LENGTH_FLOOR_RATIO = 0.93
# 低于这个长度不当作"可续写的草稿"：可能是失败的空壳，重写比接续更安全
MIN_RESUMABLE_CHARS = 300
# 断点写入节奏：写完这么多字，或距上次写入超过这么久且新增了不少于 200 字
CHECKPOINT_MIN_CHARS = 1200
CHECKPOINT_MIN_SECONDS = 5.0
CHECKPOINT_MIN_CHARS_ON_TIME = 200
# 接续/补写时带上文末尾多少字，避免模型接错语气
CONTINUATION_TAIL_CHARS = 1200


def length_floor_for(target_chars: Optional[int]) -> int:
    """目标字数对应的下限；无法解析或非正数时返回 0（表示不设下限）。"""
    try:
        target = int(target_chars or 0)
    except (TypeError, ValueError):
        return 0
    if target <= 0:
        return 0
    return max(500, int(target * LENGTH_FLOOR_RATIO))


def should_resume(status: Optional[str], content: Optional[str]) -> bool:
    """该章节是否是"上一次生成被打断"留下的草稿。"""
    return status == "writing" and len(content or "") >= MIN_RESUMABLE_CHARS


def needs_top_up(
    content_chars: int,
    target_chars: Optional[int],
    finish_reason: Optional[str],
) -> bool:
    """正文是否需要在收尾后补写一轮。

    只处理"模型自己写完了但偏短"这一种情况（``finish_reason == "stop"``）；
    预算耗尽（``length``）与近乎空产出交给截断判定，不走补写。
    """
    floor = length_floor_for(target_chars)
    if finish_reason != "stop" or floor <= 0:
        return False
    if content_chars < MIN_RESUMABLE_CHARS:
        return False
    return content_chars < floor


def build_resume_plan(
    status: Optional[str],
    content: Optional[str],
    target_chars: Optional[int],
    language: Optional[str] = "zh",
) -> tuple[str, str]:
    """返回 ``(提示词块, 生成初值)``；不续写时两者都是空串。

    两个返回值必须同源：判定一次、用一次，避免"注入了续写块但初值仍为空"
    （模型会把已有正文再写一遍）或反之。
    """
    if not should_resume(status, content):
        return "", ""
    text = content or ""
    return (
        build_continuation_block(text, target_chars, language, reason="resume"),
        text,
    )


def restore_before_top_up(content: str, snapshot_len: int) -> str:
    """补写失败时把正文截回补写前的长度。

    补写是逐块 append 的：中途失败时 ``content`` 里已经混进了半截续写，直接落库
    会在章尾留下断句垃圾。调用方必须先记下快照长度。
    """
    text = content or ""
    return text if snapshot_len <= 0 else text[:snapshot_len]


def _tail(text: Optional[str], limit: int = CONTINUATION_TAIL_CHARS) -> str:
    text = text or ""
    return text[-limit:] if len(text) > limit else text


def _is_en(language: Optional[str]) -> bool:
    return (language or "").strip().lower().startswith("en")


def build_continuation_block(
    existing: Optional[str],
    target_chars: Optional[int],
    language: Optional[str] = "zh",
    *,
    reason: str = "top_up",
) -> str:
    """拼出"接续已有正文"的提示词尾块。

    ``reason="resume"`` 用于中断后继续，``reason="top_up"`` 用于篇幅补足。
    """
    existing = existing or ""
    remaining = max(0, length_floor_for(target_chars) - len(existing))
    tail = _tail(existing)
    resumed = reason == "resume"
    if _is_en(language):
        header = (
            "【Resume the interrupted draft】This chapter was interrupted last time. "
            if resumed
            else "【Continue the chapter】The text below is the current ending of this chapter. "
        )
        return (
            f"\n\n{header}"
            "Continue from the last character; do not restate, rewrite or summarise what is above.\n"
            f"--- existing text ---\n{tail}\n--- end ---\n"
            f"Write at least {remaining} more characters to finish the chapter."
        )
    header = (
        "【续写中断的草稿】本章上次生成被中断，下面是已经写好的结尾原文。"
        if resumed
        else "【接续本章】下面这段话是本章目前的结尾原文。"
    )
    return (
        f"\n\n{header}\n"
        "请从最后一个字之后接着写，禁止复述、改写或总结前文，也不要重开场景。\n"
        f"--- 已有正文 ---\n{tail}\n--- 已有正文结束 ---\n"
        f"至少再写 {remaining} 字把本章写完。"
    )


def top_up_max_tokens(content_chars: int, target_chars: Optional[int], ceiling: int) -> int:
    """补写轮次的输出预算：按还差多少字给，且不超过本轮计算的窗口上限。"""
    floor = length_floor_for(target_chars)
    remaining = max(0, floor - int(content_chars or 0))
    wanted = remaining * 2 + 800
    return max(800, min(wanted, int(ceiling or wanted)))


class ChapterCheckpointer:
    """把生成中的正文按节奏写进章节行，供中断后继续。

    **用独立短会话按主键写入**，不复用调用方 session：断点写入随时可能失败
    （``database is locked`` 是真实故障），而 SQLAlchemy 的 ``rollback()`` 会过期
    session 里全部 ORM 对象，之后调用方任何属性访问都会抛 ``MissingGreenlet``，
    一次瞬时锁冲突就会把整次生成打死 —— 那正好违背断点保存的目的。

    只写 ``content`` / ``status``，**不动 ``word_count``**：落库阶段用它作为
    "生成前字数"修正项目总字数，断点改写它会让增量算成 0。
    """

    def __init__(
        self,
        db: Any,
        chapter: Any,
        *,
        min_chars: int = CHECKPOINT_MIN_CHARS,
        min_seconds: float = CHECKPOINT_MIN_SECONDS,
        clock=time.monotonic,
    ) -> None:
        self.db = db
        self.chapter = chapter
        # 先把主键取出来：之后不再触碰调用方 session 的 ORM 对象
        self.chapter_id = getattr(chapter, "id", None)
        self.min_chars = min_chars
        self.min_seconds = min_seconds
        self._clock = clock
        self._last_chars = 0
        self._last_at = clock()
        self.writes = 0
        # 已有成稿（completed）的章节不能被断点覆盖：重生成失败时用户要能拿回原文
        self.enabled = bool(self.chapter_id) and getattr(chapter, "status", None) != "completed"
        self._session_factory = self._make_factory(db)

    @staticmethod
    def _make_factory(db: Any):
        try:
            bind = getattr(db, "bind", None) or db.get_bind()
        except Exception:  # pragma: no cover - 拿不到 bind 时退化为不保存
            return None
        if bind is None:
            return None
        return async_sessionmaker(bind, class_=AsyncSession, expire_on_commit=False)

    def _due(self, content_chars: int) -> bool:
        grown = content_chars - self._last_chars
        if grown >= self.min_chars:
            return True
        return (
            grown >= CHECKPOINT_MIN_CHARS_ON_TIME
            and (self._clock() - self._last_at) >= self.min_seconds
        )

    async def _write(self, text: str) -> None:
        async with self._session_factory() as session:
            await session.execute(
                update(Chapter)
                .where(Chapter.id == self.chapter_id)
                .values(content=text, status="writing")
            )
            await session.commit()

    async def maybe_checkpoint(self, content: str, *, force: bool = False) -> bool:
        """到点就落一次草稿；任何失败只记日志，绝不打断生成。"""
        text = content or ""
        if not text or not self.enabled or self._session_factory is None:
            return False
        if not force and not self._due(len(text)):
            return False
        try:
            await self._write(text)
        except Exception as exc:  # noqa: BLE001 - 断点保存失败必须降级为"不保存"
            logger.warning(
                "断点保存失败（已继续生成，不影响正文）: chapter=%s, chars=%s, %s: %s",
                self.chapter_id,
                len(text),
                type(exc).__name__,
                exc,
            )
            return False
        # 同步内存对象上的展示字段，但**不 commit 调用方 session**
        try:
            self.chapter.content = text
            self.chapter.status = "writing"
        except Exception:  # pragma: no cover - 对象已分离时忽略
            pass
        self._last_chars = len(text)
        self._last_at = self._clock()
        self.writes += 1
        logger.info(
            "💾 断点已保存: chapter=%s, chars=%s, 第 %s 次",
            self.chapter_id,
            len(text),
            self.writes,
        )
        return True
