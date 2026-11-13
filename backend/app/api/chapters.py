"""章节管理API"""
from fastapi import APIRouter, Depends, HTTPException, Request, Query, BackgroundTasks
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlalchemy import select, func, update
from sqlalchemy.orm import selectinload
import json
import asyncio
import hashlib
import re
from typing import Any, Awaitable, Callable, Dict, List, Optional
from datetime import datetime, timedelta
from asyncio import Queue, Lock
from math import ceil

from app.database import get_db, get_engine
from app.api.common import verify_project_access
from app.core.errors import ApiError, DYNAMIC_DETAIL_CODE
from app.core.db_write_lock import get_db_write_lock
from app.services.chapter_context_service import (
    OneToManyContextBuilder,
    OneToOneContextBuilder,
    relationship_display_name,
)
from app.models.chapter import Chapter
from app.models.project import Project
from app.models.outline import Outline
from app.models.character import Character
from app.models.career import Career, CharacterCareer
from app.models.relationship import CharacterRelationship, Organization, OrganizationMember
from app.models.generation_history import GenerationHistory
from app.models.writing_style import WritingStyle
from app.models.analysis_task import AnalysisTask
from app.models.memory import PlotAnalysis, StoryMemory
from app.models.batch_generation_task import BatchGenerationTask
from app.models.regeneration_task import RegenerationTask
from app.models.background_task import BackgroundTask
from app.services.cascade_cleanup import delete_chapter_children
from app.services.style_anchors import append_project_style_anchors
from app.services.style_profile import (
    extract_rendered_metrics,
    profile_preset_id,
    render_style_tail_block,
    style_content_language,
)
from app.services.style_volumes import parse_volume_preset_id, resolve_volume_for_chapter
from app.schemas.chapter import (
    ChapterCreate,
    ChapterUpdate,
    ChapterResponse,
    ChapterListResponse,
    AnalysisTaskStatusResponse,
    BatchAnalysisStatusRequest,
    BatchAnalysisStatusResponse,
    BatchAnalyzeUnanalyzedRequest,
    BatchAnalyzeUnanalyzedResponse,
    ChapterGenerateRequest,
    BatchGenerateRequest,
    BatchGenerateResponse,
    BatchGenerateStatusResponse,
    ExpansionPlanUpdate,
    PartialRegenerateRequest
)
from app.schemas.regeneration import (
    ChapterRegenerateRequest,
    RegenerationTaskResponse,
    RegenerationTaskStatus
)
from app.services.agent_prompt_budget import CHARS_PER_TOKEN
from app.services.model_capability_probe import CAPABILITY_INCOMPLETE_CODE
from app.services.ai_service import (
    AIService,
    THINKING_MODEL_DEFAULT_MAX_TOKENS,
    plan_output_budget,
)
from app.services.prompt_service import prompt_service, PromptService, WritingStyleManager
from app.services.plot_analyzer import PlotAnalyzer
from app.services.outline_plan_adapter import to_storage_plan
from app.services.memory_service import memory_service
from app.services.foreshadow_service import foreshadow_service
from app.services.derived_state import (
    clamp_derived_state_after_delete,
    find_stale_derived_state,
)
from app.services.chapter_regenerator import (
    ChapterRegenerator,
    STYLE_CHECK_UNAVAILABLE,
    compute_style_check_safely,
)
from app.services.language_resolver import resolve_user_generation_language
from app.logger import get_logger
from app.api.settings import get_user_ai_service, get_user_ai_service_from_db_by_usage
from app.utils.sse_response import HEARTBEAT, SSEResponse, create_sse_response, wrap_stream_with_heartbeat

router = APIRouter(prefix="/chapters", tags=["章节管理"])
logger = get_logger(__name__)

analysis_background_tasks: set[asyncio.Task] = set()
batch_background_tasks: set[asyncio.Task] = set()

ANALYSIS_TASK_TIMEOUT_SECONDS = 600
ANALYSIS_TASK_STALE_SECONDS = 720

# 批量生成任务心跳超时阈值（分钟）。注意：该阈值在心跳 ticker 正常工作的前提下生效
# —— ticker（_batch_heartbeat_loop）每 60s 独立刷新心跳，协程长时间执行也不会窒息；
# 只有 worker 整体消失（重启/被杀）时心跳才会停止。模块常量便于测试 monkeypatch。
BATCH_STALE_MINUTES = 10
# 心跳 ticker 间隔（秒）。单章可能包含 600s 同步分析 + 生成 + 退避，必须远小于阈值。
BATCH_HEARTBEAT_INTERVAL_SECONDS = 60
# 心跳 ticker 连续失败上限：瞬时 DB 抖动（database is locked）不应让 ticker 永久退出，
# 连续失败达到该次数才放弃（放弃后由陈旧回收兜底）。
BATCH_HEARTBEAT_MAX_FAILURES = 3

# 章内 AI 续写（continue 模式）分段/能力参数（W5 冻结契约；字数即 len(str)）
SINGLE_REQUEST_MAX_CHARS = 12000
SEGMENT_TARGET_CHARS = 8000
MAX_CONTINUE_SEGMENTS = 24
CONTINUE_MAX_TARGET_CHARS = 200000
CONTINUE_MIN_SEGMENT_CHARS = 1000


async def _resolve_full_book_budget(
    ai_service: AIService,
    model_name: Optional[str],
) -> int:
    """全书注入字符预算：本次**实发**模型的实测/声明窗口换算（单一来源）。

    需求 #55 步骤 4：原先的三档语义（1M → 大预算 / 128K → 中预算 /
    小窗口与未知 → 保守预算）已随 `resolve_context_budget_chars` 一起删除。
    窗口一律取 `get_effective_context_window` 的结论，因此拿不到合格结论时这里是
    **抛错**（`validation.ai_model_not_configured` / `validation.ai_model_below_minimum`），
    不存在「预算为 0 ⇒ 悄悄不注入全书」这种中间态。
    """
    return await ai_service.resolve_full_book_budget_chars(model_name)


def _append_full_book_context(prompt: str, full_book_context: Optional[str]) -> str:
    """把全书注入上下文追加到生成 prompt（Tier3）。

    以独立块追加而非模板占位符，避免用户自定义模板缺少占位符时
    触发 format KeyError；无注入内容时原样返回。
    """
    if not full_book_context:
        return prompt
    return (
        prompt
        + "\n\n<full_book_context priority=\"P1\">\n"
        + "【全书剧情上下文 - 保持角色、设定与已发生剧情一致性】\n"
        + full_book_context
        + "\n</full_book_context>"
    )


def _build_style_anchor_override(
    *,
    enabled: Optional[bool] = None,
    chars: Optional[int] = None,
    count: Optional[int] = None,
) -> Optional[dict]:
    """构造本次生成的锚点覆盖参数；全为 None 时返回 None（沿用项目设置）。"""
    override: dict = {}
    if enabled is not None:
        override["enabled"] = enabled
    if chars is not None:
        override["chars"] = chars
    if count is not None:
        override["count"] = count
    return override or None


def _schedule_analysis_background(coroutine) -> asyncio.Task:
    """持有后台任务引用，直到任务结束。"""
    task = asyncio.create_task(coroutine)
    analysis_background_tasks.add(task)
    task.add_done_callback(analysis_background_tasks.discard)
    return task


async def _mark_batch_failed_after_crash(
    batch_id: str,
    user_id: str,
    exc: BaseException,
) -> None:
    """用独立会话把异常退出的批量任务置为 failed（请求级会话可能已关闭）。"""
    from app.database import get_engine
    from sqlalchemy.ext.asyncio import async_sessionmaker, AsyncSession

    try:
        engine = await get_engine(user_id)
        session_factory = async_sessionmaker(
            engine,
            class_=AsyncSession,
            expire_on_commit=False,
        )
        async with session_factory() as session:
            await session.execute(
                update(BatchGenerationTask)
                .where(
                    BatchGenerationTask.id == batch_id,
                    # 幂等条件：只有仍 running 才改，避免覆盖陈旧回收/已结案的结果
                    BatchGenerationTask.status == "running",
                )
                .values(
                    status="failed",
                    completed_at=datetime.now(),
                    error_message=(
                        f"批量生成异常中断：{type(exc).__name__}: {exc}"
                    )[:500],
                )
                .execution_options(synchronize_session=False)
            )
            await session.commit()
    except Exception as cleanup_error:
        logger.error(
            f"❌ 兜底标记批量生成任务失败时出错: {batch_id}: {cleanup_error}"
        )


def _on_batch_generation_task_done(
    task: asyncio.Task,
    *,
    batch_id: str,
    user_id: str,
) -> None:
    """后台批量协程结束回调：取走异常并兜底标记失败。"""
    batch_background_tasks.discard(task)
    if task.cancelled():
        return
    exc = task.exception()  # 必须取走，否则 Task exception was never retrieved
    if exc is None:
        return
    logger.error(f"❌ 批量生成后台任务异常退出: {batch_id}", exc_info=exc)
    cleanup_task = asyncio.create_task(
        _mark_batch_failed_after_crash(batch_id, user_id, exc)
    )
    batch_background_tasks.add(cleanup_task)
    cleanup_task.add_done_callback(batch_background_tasks.discard)


async def _make_heartbeat_session(user_id: str):
    """构造 ticker 的独立会话工厂与写锁（初始化放在循环内，失败可重试）。"""
    from app.database import get_engine
    from sqlalchemy.ext.asyncio import async_sessionmaker, AsyncSession

    engine = await get_engine(user_id)
    session_factory = async_sessionmaker(
        engine,
        class_=AsyncSession,
        expire_on_commit=False,
    )
    write_lock = await get_db_write_lock(user_id)
    return session_factory, write_lock


async def _batch_heartbeat_loop(
    batch_id: str,
    user_id: str,
    interval: float = BATCH_HEARTBEAT_INTERVAL_SECONDS,
) -> None:
    """独立心跳 ticker：每 interval 秒刷新一次 heartbeat_at。

    不用打点频率保证存活：单章可能内联 600s 同步分析 + 生成 + 退避，打点间隔
    会超过 BATCH_STALE_MINUTES。单次 DB 抖动不得让 ticker 永久退出（否则 10 分钟后
    正常协程会被误杀）；连续失败 BATCH_HEARTBEAT_MAX_FAILURES 次才放弃。
    rowcount==0（任务已不在 running）即自行退出。
    """
    consecutive_failures = 0
    # 惰性初始化：get_engine / 建工厂 / 取写锁都放在循环内，启动瞬间 DB 忙也能重试
    session_factory = None
    write_lock = None

    while True:
        await asyncio.sleep(interval)
        try:
            if session_factory is None or write_lock is None:
                session_factory, write_lock = await _make_heartbeat_session(user_id)
            async with write_lock:
                async with session_factory() as session:
                    result = await session.execute(
                        update(BatchGenerationTask)
                        .where(
                            BatchGenerationTask.id == batch_id,
                            BatchGenerationTask.status == "running",
                        )
                        .values(heartbeat_at=datetime.now())
                        .execution_options(synchronize_session=False)
                    )
                    await session.commit()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            consecutive_failures += 1
            logger.warning(
                f"⚠️ 批量任务心跳失败（连续 {consecutive_failures}/{BATCH_HEARTBEAT_MAX_FAILURES}）"
                f": {batch_id}: {exc}"
            )
            if consecutive_failures >= BATCH_HEARTBEAT_MAX_FAILURES:
                logger.error(
                    f"❌ 批量任务心跳连续失败 {consecutive_failures} 次，ticker 退出: {batch_id}"
                )
                return
            continue
        consecutive_failures = 0
        if not result.rowcount:
            # 任务已终态（完成/失败/取消/被回收）：ticker 自行退出，不再写库
            return


def _spawn_batch_generation(
    coroutine,
    *,
    batch_id: str,
    user_id: str,
    heartbeat_interval: float = BATCH_HEARTBEAT_INTERVAL_SECONDS,
) -> asyncio.Task:
    """启动批量生成协程与心跳 ticker，并持有引用直到主任务结束。"""

    async def _run_with_heartbeat():
        ticker = asyncio.create_task(
            _batch_heartbeat_loop(batch_id, user_id, heartbeat_interval)
        )
        try:
            return await coroutine
        finally:
            # 主任务正常结束 / 抛异常 / 被取消，都必须停掉 ticker，避免泄漏与幽灵写库
            ticker.cancel()
            try:
                await ticker
            except asyncio.CancelledError:
                pass
            except Exception as exc:  # ticker 自身异常不得掩盖主任务结果
                logger.warning(
                    f"⚠️ 批量任务心跳 ticker 结束异常: {batch_id}: {exc}"
                )

    task = asyncio.create_task(_run_with_heartbeat())
    batch_background_tasks.add(task)
    task.add_done_callback(
        lambda finished: _on_batch_generation_task_done(
            finished, batch_id=batch_id, user_id=user_id
        )
    )
    return task


def _build_lightweight_chapter_summary(content: str, max_length: int = 300) -> str:
    """生成可立即用于下一章衔接的轻量摘要兜底。"""
    if not content:
        return ""
    normalized = " ".join(content.split())
    return normalized[:max_length]


# 「疑似空转」阈值：只在 finish_reason=stop 且正文明显偏短时用。
_TRUNCATION_MIN_RATIO = 0.5


def _is_truncated_chapter_content(
    content: str,
    target_word_count: int,
    finish_reason: Optional[str],
    chapter_label: Optional[str] = None,
) -> bool:
    """判断这一章是否算「没写完」：空正文 / 被上限切断。

    - 空正文（含纯空白）：一律失败 —— 0 字章节不得冒充成功。
    - ``finish_reason == "length"``：**一律**算截断，与目标字数占比无关。该值的
      语义就是「被输出上限切断、还想继续写」，50%~100% 的半截章节同样是残稿。
    - 其它结束原因（含 ``stop``）：正文非空即视为成功。正文明显偏短（不足目标一半）
      只打 WARNING 做可观测性统计（「模型偷懒」频率），**不走失败路径** ——
      「短但完整」是写作质量问题，不是数据完整性问题。

    ``target_word_count`` 在该函数里**只用于那条告警**；告警阈值（0.5）仅在中文口径
    （1 字符 ≈ 1 字，见 ai_service `_FULL_BOOK_BUDGET_RATIO` 注释）下有意义。
    """
    text = content or ""
    label = chapter_label or "章节"
    if not text.strip():
        return True
    if finish_reason == "length":
        return True
    try:
        target = int(target_word_count or 0)
    except (TypeError, ValueError):
        target = 0
    if target > 0 and len(text) < target * _TRUNCATION_MIN_RATIO:
        logger.warning(
            f"⚠️ {label}偏短但已自然收尾（finish_reason={finish_reason or '未知'}）："
            f"正文 {len(text)} 字 / 目标 {target} 字，按成功处理"
        )
    return False


def _reasoning_chars_from_usage(usage: Any, content_chars: int) -> Optional[int]:
    """推理字符的估算值：completion_tokens - 正文字符（无 usage 时返回 None）。

    OpenAI 兼容客户端目前只把 reasoning 计入日志、不单独回传，因此这里只能给
    估算值；消息里必须标 `≈`，不得冒充精确读数。
    """
    completion_tokens = getattr(usage, "completion_tokens", None)
    if completion_tokens is None:
        return None
    try:
        estimated = int(completion_tokens) - int(content_chars)
    except (TypeError, ValueError):
        return None
    return estimated if estimated > 0 else None


# 截断即失败：预算已按窗口剩余一次给足（不再抬预算重试，见
# `AIService.resolve_effective_window_tokens` + `plan_output_budget`）。
#
# 但「预算给足」不等于「截断必然复现」：思考型模型的推理量随运行波动，偶发一次
# 吃满上限是可能的。所以截断类仍保留 **1 次**外层重试（首发生成 + 1 次 = 2 次调用），
# 而不是跟着 max_retries 跑满 4 次全额生成——抬预算已经无意义，多跑只会多烧钱。
_TRUNCATION_OUTER_RETRIES = 1


class ChapterGenerationTruncatedError(RuntimeError):
    """截断类失败：预算已按窗口剩余一次给足；外层最多重试 `_TRUNCATION_OUTER_RETRIES` 次。"""


class NonRetryableChapterGenerationError(ChapterGenerationTruncatedError):
    """不可重试的生成失败（外层批量重试 0 次）。

    预算已按窗口剩余一次给足，截断不再走预算阶梯；该类型保留给「已证明
    再怎么重放也写不出来」的失败，避免整段重放只多烧全额调用。

    注意：当前生产代码**没有抛出点**（预算重试已删除后不再有「重放必然同样失败」
    的判定处）。保留它是为了让 `_retry_allowance` 仍有一个表达「allowance=0」的
    语义位，并维持 `tests/test_batch_retry_control_flow.py` 的既有契约；
    不要据此以为某个真实失败路径仍在生效。
    """


def _retry_allowance(exc: BaseException, task_max_retries: int) -> int:
    """外层对该异常允许的重试次数（批量 max_retries 的有效值）。"""
    if isinstance(exc, NonRetryableChapterGenerationError):
        return 0
    if isinstance(exc, ChapterGenerationTruncatedError):
        # 截断类固定 1 次（覆盖推理量偶发波动），不受任务 max_retries 放大。
        return min(_TRUNCATION_OUTER_RETRIES, task_max_retries)
    return task_max_retries


def _log_budget_capability_failure(exc: BaseException, *, model: Optional[str], scope: str) -> None:
    """能力预算降级时的日志分级（续写/重写与三条章节主路径同口径）。

    - `CAPABILITY_INCOMPLETE`（门禁判合格但缓存窗口数值缺失）：门禁在派发处**不会**重抛，
      所以这里必须 ERROR —— 否则本次会带着保守上限静默跑完，没人知道预算退化了；
    - 其它门禁类 `ApiError`：派发处会按同一三元组重抛真实错误，WARNING 足够；
    - 未知异常（DB 抖动、解析 bug）：同样不会被重抛，ERROR 并带上异常类型。
    """
    code = getattr(exc, "code", None)
    if code == CAPABILITY_INCOMPLETE_CODE:
        logger.error(f"❌ {scope}上下文窗口结论缺失有效数值，预算退化: model={model}, code={code}")
    elif isinstance(exc, ApiError):
        logger.warning(f"⚠️ {scope}上下文窗口不可得，预算退回保守上限: model={model}, code={code}")
    else:
        logger.error(
            f"❌ {scope}读取上下文窗口异常，预算退化: "
            f"model={model}, {type(exc).__name__}: {exc}"
        )


def _append_style_tail(prompt: str, style_content: Optional[str]) -> str:
    """把实测「写作规格」贴在用户提示词末尾（近因位置）。

    实测依据：风格块放 system 开头 + 12 万 token 全书上下文时，模型仍写 17 字短句；
    同一模型只给 106 字风格块（无上下文）能写到 28.1 字/句。关键约束必须贴近生成点。
    """
    text = style_content or ""
    metrics = extract_rendered_metrics(text)
    block = render_style_tail_block(metrics, language=style_content_language(text))
    if not block and ("文笔画像" in text or "Style profile" in text):
        # 有画像却取不到指标 = 渲染措辞与正则脱节，尾块会静默消失（回到被上下文稀释的
        # 原始缺陷）。这里必须留痕，不能静默退化。
        logger.warning("⚠️ 画像量化指标解析失败，写作规格尾块未注入（检查渲染措辞与正则是否一致）")
    return f"{prompt}\n{block}" if block else prompt


def _extract_chapter_numbers(message: str) -> set[int]:
    """从错误文案里提取「第 N 章」的章号（兼容「第 57, 58 章」合写）。"""
    numbers: set[int] = set()
    for group in re.findall(r"第\s*([\d,，\s]+?)\s*章", message or ""):
        for part in re.split(r"[,，\s]+", group):
            if part.isdigit():
                numbers.add(int(part))
    return numbers


def _truncation_error_message(
    *,
    chapter_label: str,
    content_chars: int,
    target_word_count: int,
    finish_reason: Optional[str],
    reasoning_tokens: Optional[int],
    reasoning_chars: Optional[int],
    breakdown: Dict[str, Any],
    budget_attempts: List[int],
    reasoning_estimated: bool = False,
) -> str:
    """截断失败的诊断信息（含推理用量、结束原因与每次尝试的预算）。"""
    if reasoning_tokens is None:
        reasoning_field = "reasoning_tokens=未知（客户端未回传）"
    elif reasoning_estimated:
        reasoning_field = f"reasoning_tokens≈{reasoning_tokens}（估算）"
    else:
        reasoning_field = f"reasoning_tokens={reasoning_tokens}"
    reasoning_chars_text = f"≈{reasoning_chars}" if reasoning_chars is not None else "未知"
    return (
        f"{chapter_label}生成被截断（预算已按窗口剩余给足，截断即失败）"
        f"（正文 {content_chars} 字 / 目标 {target_word_count} 字，"
        f"finish_reason={finish_reason or '未知'}，"
        f"{reasoning_field}，"
        f"reasoning_chars={reasoning_chars_text}，"
        f"尝试预算={budget_attempts}，最终 max_tokens={budget_attempts[-1]}，"
        f"content={breakdown.get('content')}，cap={breakdown.get('cap')}，"
        f"measured_cap={breakdown.get('measured_cap')}，"
        f"context_room={breakdown.get('context_room')}，"
        f"window={breakdown.get('window')}，"
        f"input_tokens={breakdown.get('input_tokens')}）"
    )


async def _set_analysis_task_terminal_state(
    user_id: str,
    task_id: str,
    status: str,
    error_message: Optional[str] = None,
) -> bool:
    """使用独立事务写入终态，避免分析会话失败后无法更新任务。"""
    engine = await get_engine(user_id)
    session_factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    completed_at = datetime.now()

    for attempt in range(1, 4):
        async with session_factory() as terminal_db:
            try:
                result = await terminal_db.execute(
                    update(AnalysisTask)
                    .where(
                        AnalysisTask.id == task_id,
                        AnalysisTask.status.in_(["pending", "running"]),
                    )
                    .values(
                        status=status,
                        progress=100 if status == "completed" else 0,
                        error_message=error_message if status == "failed" else None,
                        completed_at=completed_at,
                    )
                )
                await terminal_db.commit()
                if result.rowcount == 0:
                    logger.info(f"分析任务已处于终态，无需重复更新: {task_id}")
                    return False
                return True
            except Exception as exc:
                await terminal_db.rollback()
                logger.error(f"更新分析任务终态失败({attempt}/3): {task_id}, {exc}")
                if attempt < 3:
                    await asyncio.sleep(0.1)

    return False


@router.post("", response_model=ChapterResponse, summary="创建章节")
async def create_chapter(
    chapter: ChapterCreate,
    request: Request,
    db: AsyncSession = Depends(get_db)
):
    """创建新的章节"""
    # 验证用户权限和项目是否存在
    user_id = getattr(request.state, 'user_id', None)
    project = await verify_project_access(chapter.project_id, user_id, db)
    
    # 计算字数(处理content可能为None的情况)
    word_count = len(chapter.content) if chapter.content else 0
    
    db_chapter = Chapter(
        **chapter.model_dump(),
        word_count=word_count
    )
    db.add(db_chapter)
    
    # 更新项目的当前字数
    project.current_words = project.current_words + word_count
    
    await db.commit()
    await db.refresh(db_chapter)
    return db_chapter


@router.get("/project/{project_id}/stats", summary="项目章节统计（轻量）")
async def get_project_chapter_stats(
    project_id: str,
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    """章节统计：数量、最大章号，以及「有大纲但还没有章节」的数量。

    最后一项是为了让前端能区分「全部写完」与「大纲还有几章没建」——只扫章节行时，
    被删过章节的项目会被误判成「所有章节都已生成」（issue #290）。
    """
    user_id = getattr(request.state, 'user_id', None)
    await verify_project_access(project_id, user_id, db)
    row = (
        await db.execute(
            select(func.count(Chapter.id), func.max(Chapter.chapter_number)).where(
                Chapter.project_id == project_id
            )
        )
    ).one()
    outline_row = (
        await db.execute(
            select(func.count(Outline.id), func.max(Outline.order_index)).where(
                Outline.project_id == project_id
            )
        )
    ).one()
    outline_total = int(outline_row[0] or 0)
    chapter_numbers = set(
        (
            await db.execute(
                select(Chapter.chapter_number).where(Chapter.project_id == project_id)
            )
        ).scalars().all()
    )
    outline_numbers = set(
        (
            await db.execute(
                select(Outline.order_index).where(
                    Outline.project_id == project_id, Outline.order_index.is_not(None)
                )
            )
        ).scalars().all()
    )
    missing = sorted(n for n in outline_numbers if int(n) > 0 and int(n) not in chapter_numbers)
    return {
        "total": int(row[0] or 0),
        "max_chapter_number": int(row[1] or 0),
        "outline_total": outline_total,
        "max_outline_number": int(outline_row[1] or 0),
        "missing_from_outlines": len(missing),
    }


@router.post("/project/{project_id}/materialize-outlines", summary="按大纲补齐缺失章节")
async def materialize_chapters_from_outlines(
    project_id: str,
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    """把「有大纲但没有章节行」的章节建出来（幂等，不覆盖已有章节）。

    章节被删除后大纲仍然保留（大纲是计划，章节是稿子），但没有任何路径能把稿子位
    重新建出来，批量生成于是只看得到已有章节（issue #290）。
    """
    user_id = getattr(request.state, 'user_id', None)
    await verify_project_access(project_id, user_id, db)

    outlines = (
        await db.execute(
            select(Outline)
            .where(Outline.project_id == project_id)
            .order_by(Outline.order_index)
        )
    ).scalars().all()
    existing = set(
        int(n)
        for n in (
            await db.execute(
                select(Chapter.chapter_number).where(Chapter.project_id == project_id)
            )
        ).scalars().all()
    )

    created: list[int] = []
    for outline in outlines:
        number = int(outline.order_index or 0)
        if number <= 0 or number in existing:
            continue
        try:
            structure = json.loads(outline.structure or "{}")
        except ValueError:
            structure = {}
        title = str(structure.get("title") or outline.title or f"第{number}章")[:200]
        db.add(
            Chapter(
                project_id=project_id,
                chapter_number=number,
                title=title,
                content=None,
                summary=None,
                word_count=0,
                status="draft",
                outline_id=outline.id,
            )
        )
        created.append(number)

    if created:
        await db.commit()
        logger.info(f"📄 按大纲补齐章节: project={project_id[:8]} 新建 {len(created)} 章")
    return {"created": created, "count": len(created)}


@router.get("/project/{project_id}/consistency", summary="派生状态一致性自检")
async def check_derived_state_consistency(
    project_id: str,
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    """报告仍引用「不存在的章节」的角色事实/状态标记（只读，issue #288）。

    这类偏差不会报错，只会让生成链路悄悄用上已删内容，所以需要能主动查出来。
    """
    user_id = getattr(request.state, 'user_id', None)
    await verify_project_access(project_id, user_id, db)
    return await find_stale_derived_state(db, project_id)


@router.get("/project/{project_id}", response_model=ChapterListResponse, summary="获取项目的所有章节")
async def get_project_chapters(
    project_id: str,
    request: Request,
    db: AsyncSession = Depends(get_db)
):
    """获取指定项目的所有章节（带大纲信息）"""
    # 验证用户权限
    user_id = getattr(request.state, 'user_id', None)
    await verify_project_access(project_id, user_id, db)
    
    # 获取总数
    count_result = await db.execute(
        select(func.count(Chapter.id)).where(Chapter.project_id == project_id)
    )
    total = count_result.scalar_one()
    
    # 获取章节列表，同时加载关联的大纲信息
    result = await db.execute(
        select(Chapter)
        .where(Chapter.project_id == project_id)
        .order_by(Chapter.chapter_number)
    )
    chapters = result.scalars().all()
    
    # 获取所有大纲信息（用于填充outline_title）
    outline_ids = [ch.outline_id for ch in chapters if ch.outline_id]
    outlines_map = {}
    if outline_ids:
        outlines_result = await db.execute(
            select(Outline).where(Outline.id.in_(outline_ids))
        )
        outlines_map = {o.id: o for o in outlines_result.scalars().all()}
    
    # 为所有章节添加大纲信息（统一处理）
    chapters_with_outline = []
    for chapter in chapters:
        chapter_dict = {
            "id": chapter.id,
            "project_id": chapter.project_id,
            "chapter_number": chapter.chapter_number,
            "title": chapter.title,
            "content": chapter.content,
            "summary": chapter.summary,
            "word_count": chapter.word_count,
            "status": chapter.status,
            "outline_id": chapter.outline_id,
            "sub_index": chapter.sub_index,
            "expansion_plan": chapter.expansion_plan,
            "created_at": chapter.created_at,
            "updated_at": chapter.updated_at,
        }
        
        # 添加大纲信息
        if chapter.outline_id and chapter.outline_id in outlines_map:
            outline = outlines_map[chapter.outline_id]
            chapter_dict["outline_title"] = outline.title
            chapter_dict["outline_order"] = outline.order_index
        else:
            chapter_dict["outline_title"] = None
            chapter_dict["outline_order"] = None
        
        chapters_with_outline.append(chapter_dict)
    
    return ChapterListResponse(total=total, items=chapters_with_outline)


@router.get("/{chapter_id}", response_model=ChapterResponse, summary="获取章节详情")
async def get_chapter(
    chapter_id: str,
    request: Request,
    db: AsyncSession = Depends(get_db)
):
    """根据ID获取章节详情"""
    result = await db.execute(
        select(Chapter).where(Chapter.id == chapter_id)
    )
    chapter = result.scalar_one_or_none()
    
    if not chapter:
        raise ApiError(code="not_found.chapter")
    
    # 验证用户权限
    user_id = getattr(request.state, 'user_id', None)
    await verify_project_access(chapter.project_id, user_id, db)
    
    return chapter


@router.get("/{chapter_id}/navigation", summary="获取章节导航信息")
async def get_chapter_navigation(
    chapter_id: str,
    request: Request,
    db: AsyncSession = Depends(get_db)
):
    """
    获取章节的导航信息（上一章/下一章）
    用于章节阅读器的翻页功能
    """
    # 获取当前章节
    result = await db.execute(
        select(Chapter).where(Chapter.id == chapter_id)
    )
    current_chapter = result.scalar_one_or_none()
    
    if not current_chapter:
        raise ApiError(code="not_found.chapter")
    
    # 验证用户权限
    user_id = getattr(request.state, 'user_id', None)
    await verify_project_access(current_chapter.project_id, user_id, db)
    
    # 获取上一章
    prev_result = await db.execute(
        select(Chapter)
        .where(Chapter.project_id == current_chapter.project_id)
        .where(Chapter.chapter_number < current_chapter.chapter_number)
        .order_by(Chapter.chapter_number.desc())
        .limit(1)
    )
    prev_chapter = prev_result.scalar_one_or_none()
    
    # 获取下一章
    next_result = await db.execute(
        select(Chapter)
        .where(Chapter.project_id == current_chapter.project_id)
        .where(Chapter.chapter_number > current_chapter.chapter_number)
        .order_by(Chapter.chapter_number.asc())
        .limit(1)
    )
    next_chapter = next_result.scalar_one_or_none()
    
    return {
        "current": {
            "id": current_chapter.id,
            "chapter_number": current_chapter.chapter_number,
            "title": current_chapter.title
        },
        "previous": {
            "id": prev_chapter.id,
            "chapter_number": prev_chapter.chapter_number,
            "title": prev_chapter.title
        } if prev_chapter else None,
        "next": {
            "id": next_chapter.id,
            "chapter_number": next_chapter.chapter_number,
            "title": next_chapter.title
        } if next_chapter else None
    }


@router.put("/{chapter_id}", response_model=ChapterResponse, summary="更新章节")
async def update_chapter(
    chapter_id: str,
    chapter_update: ChapterUpdate,
    request: Request,
    db: AsyncSession = Depends(get_db)
):
    """更新章节信息"""
    result = await db.execute(
        select(Chapter).where(Chapter.id == chapter_id)
    )
    chapter = result.scalar_one_or_none()
    
    if not chapter:
        raise ApiError(code="not_found.chapter")
    
    # 验证用户权限
    user_id = getattr(request.state, 'user_id', None)
    await verify_project_access(chapter.project_id, user_id, db)
    
    # 记录旧字数
    old_word_count = chapter.word_count or 0
    
    # 更新字段
    update_data = chapter_update.model_dump(exclude_unset=True)
    for field, value in update_data.items():
        setattr(chapter, field, value)
    
    # 如果内容更新了，重新计算字数（包括清空内容的情况）
    if "content" in update_data:
        new_word_count = len(chapter.content) if chapter.content else 0
        chapter.word_count = new_word_count
        
        # 更新项目字数
        result = await db.execute(
            select(Project).where(Project.id == chapter.project_id)
        )
        project = result.scalar_one_or_none()
        if project:
            project.current_words = project.current_words - old_word_count + new_word_count
        
        # 如果内容被清空，清理相关数据
            if not chapter.content or chapter.content.strip() == "":
                chapter.status = "draft"
                
                # 清理章节子行（分析任务/分析结果/故事记忆/再生成任务），并断开生成历史的章节外键
                cleanup_counts = await delete_chapter_children(db, [chapter_id])
                logger.info(f"🗑️ 章节 {chapter_id[:8]} 子行清理: {cleanup_counts}")
                
                # 清理向量数据库中的记忆数据
                try:
                    await memory_service.delete_chapter_memories(
                        user_id=user_id,
                        project_id=chapter.project_id,
                        chapter_id=chapter_id
                    )
                    logger.info(f"✅ 已清理章节 {chapter_id[:8]} 的向量记忆数据")
                except Exception as e:
                    logger.warning(f"⚠️ 清理向量记忆数据失败: {str(e)}")
                
                # 🔮 清理章节相关的分析伏笔数据
                try:
                    foreshadow_result = await foreshadow_service.delete_chapter_foreshadows(
                        db=db,
                        project_id=chapter.project_id,
                        chapter_id=chapter_id,
                        only_analysis_source=True  # 只删除分析来源的伏笔，保留手动创建的
                    )
                    if foreshadow_result['deleted_count'] > 0:
                        logger.info(f"🔮 已清理章节 {chapter_id[:8]} 的 {foreshadow_result['deleted_count']} 个伏笔数据")
                except Exception as e:
                    logger.warning(f"⚠️ 清理伏笔数据失败: {str(e)}")
                
                logger.info(f"🗑️ 章节 {chapter_id[:8]} 内容已清空，已清理分析、记忆和伏笔数据")
    
    await db.commit()
    await db.refresh(chapter)
    
    chapter_dict = {
        "id": chapter.id,
        "project_id": chapter.project_id,
        "chapter_number": chapter.chapter_number,
        "title": chapter.title,
        "content": chapter.content,
        "summary": chapter.summary,
        "word_count": chapter.word_count,
        "status": chapter.status,
        "outline_id": chapter.outline_id,
        "sub_index": chapter.sub_index,
        "expansion_plan": chapter.expansion_plan,
        "created_at": chapter.created_at,
        "updated_at": chapter.updated_at,
        "outline_title": None,
        "outline_order": None
    }
    
    # 如果章节关联了大纲，查询大纲信息
    if chapter.outline_id:
        outline_result = await db.execute(
            select(Outline).where(Outline.id == chapter.outline_id)
        )
        outline = outline_result.scalar_one_or_none()
        if outline:
            chapter_dict["outline_title"] = outline.title
            chapter_dict["outline_order"] = outline.order_index
    
    return chapter_dict


@router.delete("/{chapter_id}", summary="删除章节")
async def delete_chapter(
    chapter_id: str,
    request: Request,
    db: AsyncSession = Depends(get_db)
):
    """删除章节"""
    result = await db.execute(
        select(Chapter).where(Chapter.id == chapter_id)
    )
    chapter = result.scalar_one_or_none()
    
    if not chapter:
        raise ApiError(code="not_found.chapter")
    
    # 验证用户权限
    user_id = getattr(request.state, 'user_id', None)
    await verify_project_access(chapter.project_id, user_id, db)
    
    # 更新项目字数
    result = await db.execute(
        select(Project).where(Project.id == chapter.project_id)
    )
    project = result.scalar_one_or_none()
    if project:
        # 处理 word_count 和 current_words 可能为 None 的情况
        chapter_word_count = chapter.word_count or 0
        project.current_words = max(0, (project.current_words or 0) - chapter_word_count)
    
    # 🗑️ 清理向量数据库中的记忆数据
    try:
        await memory_service.delete_chapter_memories(
            user_id=user_id,
            project_id=chapter.project_id,
            chapter_id=chapter_id
        )
        logger.info(f"✅ 已清理章节 {chapter_id[:8]} 的向量记忆数据")
    except Exception as e:
        logger.warning(f"⚠️ 清理向量记忆数据失败: {str(e)}")
        # 不阻断删除流程，继续执行
    
    # 🔮 清理与该章节相关的伏笔数据（仅分析来源的伏笔）
    try:
        foreshadow_result = await foreshadow_service.delete_chapter_foreshadows(
            db=db,
            project_id=chapter.project_id,
            chapter_id=chapter_id,
            only_analysis_source=True  # 只删除分析来源的伏笔，保留手动创建的
        )
        if foreshadow_result['deleted_count'] > 0:
            logger.info(f"🔮 已清理章节 {chapter_id[:8]} 的 {foreshadow_result['deleted_count']} 个伏笔数据")
    except Exception as e:
        logger.warning(f"⚠️ 清理伏笔数据失败: {str(e)}")
        # 不阻断删除流程，继续执行
    
    # 显式清理章节子行（SQLite 未启用 PRAGMA foreign_keys，CASCADE 不生效）
    await delete_chapter_children(db, [chapter_id])
    
    # 删除章节
    await db.delete(chapter)
    await db.commit()

    # 派生状态按章节号推进，删章不会自动回退（issue #288）：钳到当前最大存活章，
    # 否则 profile_facts / current_state 会继续引用已经不存在的章节，并被注入提示词。
    try:
        await clamp_derived_state_after_delete(db, chapter.project_id)
        await db.commit()
    except Exception as exc:  # 清理失败不应阻断删除本身
        await db.rollback()
        logger.warning(f"⚠️ 派生状态回滚失败（章节已删除）: {exc}")
    
    return {"message": "章节删除成功"}


async def check_prerequisites(db: AsyncSession, chapter: Chapter) -> tuple[bool, str, list[Chapter]]:
    """
    检查章节前置条件
    
    Args:
        db: 数据库会话
        chapter: 当前章节
        
    Returns:
        (可否生成, 错误信息, 前置章节列表)
    """
    # 如果是第一章，无需检查前置
    if chapter.chapter_number == 1:
        return True, "", []
    
    # 查询所有前置章节（序号小于当前章节的）
    result = await db.execute(
        select(Chapter)
        .where(Chapter.project_id == chapter.project_id)
        .where(Chapter.chapter_number < chapter.chapter_number)
        .order_by(Chapter.chapter_number)
    )
    previous_chapters = result.scalars().all()
    
    # 检查是否所有前置章节都有内容
    incomplete_chapters = [
        ch for ch in previous_chapters
        if not ch.content or ch.content.strip() == ""
    ]
    
    if incomplete_chapters:
        missing_numbers = [str(ch.chapter_number) for ch in incomplete_chapters]
        error_msg = f"需要先完成前置章节：第 {', '.join(missing_numbers)} 章"
        return False, error_msg, previous_chapters
    
    return True, "", previous_chapters


async def check_previous_analysis_ready(db: AsyncSession, chapter: Chapter) -> tuple[bool, str]:
    """检查上一章分析是否完成，避免下一章使用滞后的记忆和角色状态。"""
    if chapter.chapter_number <= 1:
        return True, ""

    prev_result = await db.execute(
        select(Chapter)
        .where(Chapter.project_id == chapter.project_id)
        .where(Chapter.chapter_number < chapter.chapter_number)
        .order_by(Chapter.chapter_number.desc())
        .limit(1)
    )
    prev_chapter = prev_result.scalar_one_or_none()
    if not prev_chapter or not prev_chapter.content:
        return True, ""

    # 只查询标量字段，避免复用当前 Session identity map 中的旧 ORM 实例。
    analysis_result = await db.execute(
        select(AnalysisTask.id, AnalysisTask.status)
        .where(AnalysisTask.chapter_id == prev_chapter.id)
        .order_by(AnalysisTask.created_at.desc())
        .limit(1)
    )
    analysis_task = analysis_result.first()
    if analysis_task and analysis_task.status == 'completed':
        return True, ""

    return False, f"上一章（第{prev_chapter.chapter_number}章）内容分析尚未完成，请等待分析完成后再生成下一章，以保证角色状态、记忆和伏笔连贯"


async def build_characters_info_with_careers(
    db: AsyncSession,
    project_id: str,
    characters: list[Character],
    filter_character_names: Optional[list[str]] = None
) -> str:
    """
    构建包含职业信息的角色上下文
    
    Args:
        db: 数据库会话
        project_id: 项目ID
        characters: 角色列表
        filter_character_names: 可选，筛选特定角色名称列表（用于1-1模式的structure.characters或1-n模式的expansion_plan.character_focus）
        
    Returns:
        格式化的角色信息字符串，包含职业信息
    """
    if not characters:
        return '暂无角色信息'
    
    # 如果提供了筛选名单，只保留匹配的角色
    if filter_character_names:
        filtered_characters = [c for c in characters if c.name in filter_character_names]
        if not filtered_characters:
            logger.warning(f"筛选后无匹配角色，使用全部角色。筛选名单: {filter_character_names}")
            filtered_characters = characters
        else:
            logger.info(f"根据筛选名单保留 {len(filtered_characters)}/{len(characters)} 个角色: {[c.name for c in filtered_characters]}")
        characters = filtered_characters
    
    # 获取所有职业信息（一次性查询，提高效率）
    careers_result = await db.execute(
        select(Career).where(Career.project_id == project_id)
    )
    careers_map = {c.id: c for c in careers_result.scalars().all()}
    
    # 获取所有角色的职业关联（一次性查询）
    character_ids = [c.id for c in characters]
    if not character_ids:
        return '暂无角色信息'
    
    # 构建全局角色名称映射（用于关系显示）
    all_chars_result = await db.execute(
        select(Character.id, Character.name).where(Character.project_id == project_id)
    )
    all_char_name_map = {row.id: row.name for row in all_chars_result.all()}
        
    character_careers_result = await db.execute(
        select(CharacterCareer).where(CharacterCareer.character_id.in_(character_ids))
    )
    character_careers = character_careers_result.scalars().all()
    
    # 获取所有角色的关系（一次性查询）
    from sqlalchemy import or_
    rels_result = await db.execute(
        select(CharacterRelationship).where(
            CharacterRelationship.project_id == project_id,
            or_(
                CharacterRelationship.character_from_id.in_(character_ids),
                CharacterRelationship.character_to_id.in_(character_ids)
            )
        )
    )
    all_relationships = rels_result.scalars().all()
    
    # 按角色ID分组关系
    char_rels_map: dict[str, list] = {cid: [] for cid in character_ids}
    for r in all_relationships:
        if r.character_from_id in char_rels_map:
            char_rels_map[r.character_from_id].append(r)
        if r.character_to_id in char_rels_map:
            char_rels_map[r.character_to_id].append(r)
    
    # 获取所有组织及其成员关系（一次性查询）
    orgs_result = await db.execute(
        select(Organization).where(Organization.project_id == project_id)
    )
    all_orgs = orgs_result.scalars().all()
    
    # 构建组织ID到组织名称的映射（通过关联的Character记录）
    org_name_map = {}  # org_id -> org_name
    char_id_to_org = {}  # character_id -> Organization（用于组织实体补充详情）
    for org in all_orgs:
        org_name_map[org.id] = all_char_name_map.get(org.character_id, '未知组织')
        char_id_to_org[org.character_id] = org
    
    # 获取所有组织的成员关系（一次性查询）
    org_ids = [org.id for org in all_orgs]
    all_org_members = []
    if org_ids:
        all_org_members_result = await db.execute(
            select(OrganizationMember).where(
                OrganizationMember.organization_id.in_(org_ids)
            )
        )
        all_org_members = all_org_members_result.scalars().all()
    
    # 按组织ID分组成员（用于组织实体显示成员列表）
    org_members_map: dict[str, list] = {oid: [] for oid in org_ids}
    for m in all_org_members:
        if m.organization_id in org_members_map:
            org_members_map[m.organization_id].append(m)
    
    # 获取涉及当前非组织角色的成员关系
    non_org_char_ids = [c.id for c in characters if not c.is_organization]
    char_org_map: dict[str, list] = {cid: [] for cid in non_org_char_ids}
    for m in all_org_members:
        if m.character_id in char_org_map:
            char_org_map[m.character_id].append(m)
    
    # 构建角色ID到职业信息的映射
    char_career_map = {}
    for cc in character_careers:
        if cc.character_id not in char_career_map:
            char_career_map[cc.character_id] = {'main': None, 'sub': []}
        
        career = careers_map.get(cc.career_id)
        if not career:
            continue
            
        career_info = {
            'name': career.name,
            'stage': cc.current_stage,
            'max_stage': career.max_stage,
            'stage_progress': cc.stage_progress
        }
        
        if cc.career_type == 'main':
            char_career_map[cc.character_id]['main'] = career_info
        else:
            char_career_map[cc.character_id]['sub'].append(career_info)
    
    # 构建角色信息字符串
    characters_info_parts = []
    for c in characters:
        # 基本信息（含存活状态标记）
        entity_type = '组织' if c.is_organization else '角色'
        status_marker = ""
        char_status = getattr(c, 'status', None) or 'active'
        if char_status != 'active':
            STATUS_MARKERS = {
                'deceased': '💀已死亡',
                'missing': '❓已失踪',
                'retired': '📤已退场',
                'destroyed': '💀已覆灭'
            }
            status_marker = f" [{STATUS_MARKERS.get(char_status, char_status)}]"
        base_info = f"- {c.name}({entity_type}, {c.role_type}){status_marker}"
        
        # 组织实体：补充组织详情
        org_detail_str = ""
        if c.is_organization and c.id in char_id_to_org:
            org = char_id_to_org[c.id]
            org_detail_parts = []
            if c.organization_type:
                org_detail_parts.append(f"类型:{c.organization_type}")
            if c.organization_purpose:
                purpose_preview = c.organization_purpose[:60] if len(c.organization_purpose) > 60 else c.organization_purpose
                org_detail_parts.append(f"宗旨:{purpose_preview}")
            if org.power_level is not None:
                org_detail_parts.append(f"势力等级:{org.power_level}")
            if org.location:
                org_detail_parts.append(f"据点:{org.location}")
            if org.motto:
                org_detail_parts.append(f"口号:{org.motto}")
            if org.member_count:
                org_detail_parts.append(f"成员数:{org.member_count}")
            if org_detail_parts:
                org_detail_str = f" | {', '.join(org_detail_parts)}"
            
            # 显示组织的核心成员列表（最多5个）
            if org.id in org_members_map and org_members_map[org.id]:
                member_parts = []
                for m in sorted(org_members_map[org.id], key=lambda x: -(x.rank or 0))[:5]:
                    m_name = all_char_name_map.get(m.character_id, '未知')
                    m_desc = f"{m_name}({m.position})"
                    if m.status and m.status != 'active':
                        m_desc += f"[{m.status}]"
                    member_parts.append(m_desc)
                if member_parts:
                    org_detail_str += f" | 成员: {', '.join(member_parts)}"
        
        # 职业信息
        career_info_str = ""
        if c.id in char_career_map:
            career_data = char_career_map[c.id]
            
            # 主职业
            if career_data['main']:
                main = career_data['main']
                stage_desc = f"{main['stage']}/{main['max_stage']}阶"
                career_info_str += f" | 主职业: {main['name']}({stage_desc})"
            
            # 副职业
            if career_data['sub']:
                sub_list = []
                for sub in career_data['sub']:
                    stage_desc = f"{sub['stage']}/{sub['max_stage']}阶"
                    sub_list.append(f"{sub['name']}({stage_desc})")
                career_info_str += f" | 副职业: {', '.join(sub_list)}"
        
        # 心理状态（由章节分析自动更新）
        state_str = ""
        if c.current_state:
            state_preview = c.current_state[:50] if len(c.current_state) > 50 else c.current_state
            state_str = f" | 当前状态: {state_preview}"
            if c.state_updated_chapter:
                state_str += f"(第{c.state_updated_chapter}章)"
        
        # 组织成员信息（非组织角色才显示所属组织）
        org_str = ""
        if not c.is_organization and c.id in char_org_map and char_org_map[c.id]:
            org_parts = []
            for m in char_org_map[c.id][:3]:  # 最多显示3个组织
                o_name = org_name_map.get(m.organization_id, '未知组织')
                o_desc = f"{o_name}({m.position})"
                if m.loyalty is not None and m.loyalty != 50:
                    o_desc += f"[忠诚度:{m.loyalty}]"
                if m.status and m.status != 'active':
                    o_desc += f"[{m.status}]"
                org_parts.append(o_desc)
            if org_parts:
                org_str = f" | 所属组织: {', '.join(org_parts)}"
        
        # 关系信息
        rel_str = ""
        if c.id in char_rels_map and char_rels_map[c.id]:
            rel_parts = []
            seen_pairs = set()  # 避免重复显示同一对关系
            for r in char_rels_map[c.id][:5]:  # 最多显示5个关系
                # 确定对方角色名
                if r.character_from_id == c.id:
                    other_name = all_char_name_map.get(r.character_to_id, '未知')
                else:
                    other_name = all_char_name_map.get(r.character_from_id, '未知')
                
                pair_key = tuple(sorted([c.id, r.character_from_id if r.character_from_id != c.id else r.character_to_id]))
                if pair_key in seen_pairs:
                    continue
                seen_pairs.add(pair_key)
                
                rel_name = await relationship_display_name(db, r)
                rel_desc = f"{other_name}({rel_name})"
                if r.intimacy_level is not None and r.intimacy_level != 50:
                    rel_desc += f"[亲密度:{r.intimacy_level}]"
                rel_parts.append(rel_desc)
            
            if rel_parts:
                rel_str = f" | 关系: {', '.join(rel_parts)}"
        
        # 性格描述
        personality_str = ""
        if c.personality:
            personality_preview = c.personality[:100] if len(c.personality) > 100 else c.personality
            personality_str = f": {personality_preview}"
        
        # 组合完整信息
        full_info = base_info + org_detail_str + career_info_str + state_str + org_str + rel_str + personality_str
        characters_info_parts.append(full_info)
    
    return "\n".join(characters_info_parts)


@router.get("/{chapter_id}/can-generate", summary="检查章节是否可以生成")
async def check_can_generate(
    chapter_id: str,
    request: Request,
    db: AsyncSession = Depends(get_db)
):
    """
    检查章节是否满足生成条件
    返回可生成状态和前置章节信息
    """
    # 获取章节
    result = await db.execute(
        select(Chapter).where(Chapter.id == chapter_id)
    )
    chapter = result.scalar_one_or_none()
    if not chapter:
        raise ApiError(code="not_found.chapter")
    
    # 验证用户权限
    user_id = getattr(request.state, 'user_id', None)
    await verify_project_access(chapter.project_id, user_id, db)
    
    # 检查前置条件
    can_generate, error_msg, previous_chapters = await check_prerequisites(db, chapter)
    
    # 构建前置章节信息
    previous_info = [
        {
            "id": ch.id,
            "chapter_number": ch.chapter_number,
            "title": ch.title,
            "has_content": bool(ch.content and ch.content.strip()),
            "word_count": ch.word_count or 0
        }
        for ch in previous_chapters
    ]
    
    return {
        "can_generate": can_generate,
        "reason": error_msg if not can_generate else "",
        "previous_chapters": previous_info,
        "chapter_number": chapter.chapter_number
    }


async def analyze_chapter_background(
    chapter_id: str,
    user_id: str,
    project_id: str,
    task_id: str,
    ai_service: Optional[AIService] = None,
    progress_callback: Optional[Callable[[int], Awaitable[None]]] = None,
) -> bool:
    """执行有硬超时保障的章节分析，并确保中断后任务进入终态。"""
    try:
        success = await asyncio.wait_for(
            _analyze_chapter_background_impl(
                chapter_id=chapter_id,
                user_id=user_id,
                project_id=project_id,
                task_id=task_id,
                ai_service=ai_service,
                progress_callback=progress_callback,
            ),
            timeout=ANALYSIS_TASK_TIMEOUT_SECONDS,
        )
    except TimeoutError:
        error_message = f"分析任务超时（超过{ANALYSIS_TASK_TIMEOUT_SECONDS // 60}分钟）"
        logger.error(f"❌ {error_message}: chapter_id={chapter_id}, task_id={task_id}")
        await _set_analysis_task_terminal_state(user_id, task_id, "failed", error_message)
        return False
    except asyncio.CancelledError:
        terminal_update = asyncio.create_task(
            _set_analysis_task_terminal_state(
                user_id,
                task_id,
                "failed",
                "分析任务被取消或服务关闭",
            )
        )
        await asyncio.shield(terminal_update)
        raise

    # 章节正文已落库且分析收尾完成 → 刷新角色/组织档案（issue #174）。
    # 放在终态之后、fire-and-forget，失败不影响主任务，也不阻塞任务完成。
    if success:
        _spawn_character_profile_sync(user_id, project_id, chapter_id)
    return success


def _spawn_character_profile_sync(user_id: str, project_id: str, chapter_id: str) -> None:
    """后台异步刷新档案：独立会话 + 自吞异常（issue #174）。"""
    from app.services.character_profile_sync_service import character_profile_sync_service

    async def _run() -> None:
        await character_profile_sync_service.sync_after_chapter(
            user_id=user_id,
            project_id=project_id,
            chapter_id=chapter_id,
        )

    task = asyncio.create_task(_run())
    analysis_background_tasks.add(task)
    task.add_done_callback(analysis_background_tasks.discard)


async def _analyze_chapter_background_impl(
    chapter_id: str,
    user_id: str,
    project_id: str,
    task_id: str,
    ai_service: Optional[AIService] = None,
    progress_callback: Optional[Callable[[int], Awaitable[None]]] = None,
) -> bool:
    """
    后台异步分析章节（支持并发，使用锁保护数据库写入）
    
    Args:
        chapter_id: 章节ID
        user_id: 用户ID
        project_id: 项目ID
        task_id: 任务ID
        ai_service: AI服务实例
        
    Returns:
        bool: True表示分析成功，False表示分析失败
    """
    db_session = None
    write_lock = await get_db_write_lock(user_id)

    async def report_progress(progress: int) -> None:
        if not progress_callback:
            return
        try:
            await progress_callback(progress)
        except Exception as callback_error:
            logger.warning(f"⚠️ 同步章节分析进度失败: {callback_error}")
    
    try:
        logger.info(f"🔍 开始分析章节: {chapter_id}, 任务ID: {task_id}")
        
        # 创建独立数据库会话
        from app.database import get_engine
        from sqlalchemy.ext.asyncio import async_sessionmaker, AsyncSession
        
        engine = await get_engine(user_id)
        AsyncSessionLocal = async_sessionmaker(
            engine,
            class_=AsyncSession,
            expire_on_commit=False
        )
        db_session = AsyncSessionLocal()
        
        # 1. 获取任务（读操作）
        task_result = await db_session.execute(
            select(AnalysisTask).where(AnalysisTask.id == task_id)
        )
        task = task_result.scalar_one_or_none()
        
        if not task:
            logger.error(f"❌ 任务不存在: {task_id}")
            return False
        if task.status not in ("pending", "running"):
            logger.warning(f"分析任务已结束，跳过执行: {task_id}, status={task.status}")
            return False
        
        # 更新任务状态（写操作，需要锁）
        async with write_lock:
            task.status = 'running'
            task.started_at = datetime.now()
            task.progress = 10
            await db_session.commit()
        await report_progress(10)
        
        # 2. 获取章节信息（读操作）
        chapter_result = await db_session.execute(
            select(Chapter).where(Chapter.id == chapter_id)
        )
        chapter = chapter_result.scalar_one_or_none()
        if not chapter or not chapter.content:
            async with write_lock:
                task.status = 'failed'
                task.error_message = '章节不存在或内容为空'
                task.completed_at = datetime.now()
                await db_session.commit()
            logger.error(f"❌ 章节不存在或内容为空: {chapter_id}")
            return False
        
        async with write_lock:
            task.progress = 20
            await db_session.commit()
        await report_progress(20)
        
        if ai_service is None:
            ai_service = await get_user_ai_service_from_db_by_usage(
                user_id=user_id,
                db=db_session,
                usage="chapter_analysis"
            )

        # 获取已埋入的伏笔列表（用于回收匹配，传入当前章节号以启用智能标记）
        existing_foreshadows = await foreshadow_service.get_planted_foreshadows_for_analysis(
            db=db_session,
            project_id=project_id,
            current_chapter_number=chapter.chapter_number  # 传入当前章节号以启用智能标记
        )
        logger.info(f"📋 后台分析 - 已获取{len(existing_foreshadows)}个已埋入伏笔用于匹配（含智能回收标记）")
        
        # 获取项目角色信息（根据大纲/展开规划筛选本章相关角色）
        filter_character_names = None
        
        # 1-N模式：从expansion_plan中提取character_focus
        if chapter.expansion_plan:
            try:
                plan = json.loads(chapter.expansion_plan)
                focus_names = plan.get('character_focus', [])
                if focus_names:
                    filter_character_names = focus_names
                    logger.info(f"📋 从expansion_plan提取角色焦点: {filter_character_names}")
            except (json.JSONDecodeError, Exception):
                pass
        
        # 1-1模式：从outline.structure中提取characters
        if not filter_character_names and chapter.outline_id:
            try:
                outline_result = await db_session.execute(
                    select(Outline).where(Outline.id == chapter.outline_id)
                )
                chapter_outline = outline_result.scalar_one_or_none()
                if chapter_outline and chapter_outline.structure:
                    structure = json.loads(chapter_outline.structure)
                    raw_characters = structure.get('characters', [])
                    if raw_characters:
                        filter_character_names = [
                            c['name'] if isinstance(c, dict) else c
                            for c in raw_characters
                        ]
                        logger.info(f"📋 从outline.structure提取角色: {filter_character_names}")
            except (json.JSONDecodeError, Exception):
                pass
        
        # 查询角色（根据筛选名单或全部）
        characters_query = select(Character).where(Character.project_id == project_id)
        if filter_character_names:
            characters_query = characters_query.where(Character.name.in_(filter_character_names))
        characters_result = await db_session.execute(characters_query)
        project_characters = characters_result.scalars().all()
        
        # 如果筛选后无角色，降级为全部角色
        if not project_characters and filter_character_names:
            logger.warning(f"⚠️ 筛选后无匹配角色，降级为全部角色")
            characters_result = await db_session.execute(
                select(Character).where(Character.project_id == project_id)
            )
            project_characters = characters_result.scalars().all()
            filter_character_names = None
        
        characters_info = await build_characters_info_with_careers(
            db=db_session,
            project_id=project_id,
            characters=project_characters,
            filter_character_names=filter_character_names
        )
        logger.info(f"📋 后台分析 - 已获取{len(project_characters)}个角色信息用于分析")
        
        # 定义重试回调函数，用于在重试时更新任务状态
        async def on_retry_callback(attempt: int, max_retries: int, wait_time: int, error_reason: str):
            """重试时更新任务状态，让前端能感知到重试进度"""
            try:
                async with write_lock:
                    # 重新获取任务（确保获取最新状态）
                    task_result_retry = await db_session.execute(
                        select(AnalysisTask).where(AnalysisTask.id == task_id)
                    )
                    task_retry = task_result_retry.scalar_one_or_none()
                    if task_retry:
                        # 更新任务状态，保持 running 但更新 started_at 以重置超时计时器
                        task_retry.status = 'running'
                        task_retry.started_at = datetime.now()  # 重置开始时间，防止超时检测误判
                        task_retry.progress = 25 + attempt * 5  # 根据重试次数更新进度
                        task_retry.error_message = f"正在重试({attempt}/{max_retries})：{error_reason[:100]}"
                        await db_session.commit()
                        logger.info(f"🔄 分析任务重试状态已更新: 尝试 {attempt}/{max_retries}, 等待 {wait_time}s, 原因: {error_reason[:50]}...")
                await report_progress(25 + attempt * 5)
            except Exception as callback_error:
                logger.warning(f"⚠️ 更新重试状态失败: {callback_error}")
        
        # 3. 使用PlotAnalyzer分析章节（传入已有伏笔列表、角色信息和重试回调）
        analyzer = PlotAnalyzer(ai_service)
        analysis_result = await analyzer.analyze_chapter(
            chapter_number=chapter.chapter_number,
            title=chapter.title,
            content=chapter.content,
            word_count=chapter.word_count or len(chapter.content),
            existing_foreshadows=existing_foreshadows,
            on_retry=on_retry_callback,
            characters_info=characters_info
        )
        
        if not analysis_result:
            async with write_lock:
                task.status = 'failed'
                task.error_message = 'AI分析失败，请检查日志'
                task.completed_at = datetime.now()
                await db_session.commit()
            logger.error(f"❌ AI分析失败: {chapter_id}")
            return False
        
        async with write_lock:
            task.progress = 60
            await db_session.commit()
        await report_progress(60)
        
        # 4. 保存分析结果到数据库（写操作，需要锁）
        async with write_lock:
            existing_analysis_result = await db_session.execute(
                select(PlotAnalysis).where(PlotAnalysis.chapter_id == chapter_id)
            )
            existing_analysis = existing_analysis_result.scalar_one_or_none()
            
            if existing_analysis:
                # 更新现有记录
                logger.info(f"  更新现有分析记录: {existing_analysis.id}")
                existing_analysis.plot_stage = analysis_result.get('plot_stage', '发展')
                existing_analysis.conflict_level = analysis_result.get('conflict', {}).get('level', 0)
                existing_analysis.conflict_types = analysis_result.get('conflict', {}).get('types', [])
                existing_analysis.emotional_tone = analysis_result.get('emotional_arc', {}).get('primary_emotion', '')
                existing_analysis.emotional_intensity = analysis_result.get('emotional_arc', {}).get('intensity', 0) / 10.0
                existing_analysis.hooks = analysis_result.get('hooks', [])
                existing_analysis.hooks_count = len(analysis_result.get('hooks', []))
                existing_analysis.foreshadows = analysis_result.get('foreshadows', [])
                existing_analysis.foreshadows_planted = sum(1 for f in analysis_result.get('foreshadows', []) if f.get('type') == 'planted')
                existing_analysis.foreshadows_resolved = sum(1 for f in analysis_result.get('foreshadows', []) if f.get('type') == 'resolved')
                existing_analysis.plot_points = analysis_result.get('plot_points', [])
                existing_analysis.plot_points_count = len(analysis_result.get('plot_points', []))
                existing_analysis.character_states = analysis_result.get('character_states', [])
                existing_analysis.scenes = analysis_result.get('scenes', [])
                existing_analysis.pacing = analysis_result.get('pacing', 'moderate')
                existing_analysis.overall_quality_score = analysis_result.get('scores', {}).get('overall', 0)
                existing_analysis.pacing_score = analysis_result.get('scores', {}).get('pacing', 0)
                existing_analysis.engagement_score = analysis_result.get('scores', {}).get('engagement', 0)
                existing_analysis.coherence_score = analysis_result.get('scores', {}).get('coherence', 0)
                existing_analysis.analysis_report = analyzer.generate_analysis_summary(analysis_result)
                existing_analysis.suggestions = analysis_result.get('suggestions', [])
                existing_analysis.dialogue_ratio = analysis_result.get('dialogue_ratio', 0)
                existing_analysis.description_ratio = analysis_result.get('description_ratio', 0)
            else:
                # 创建新记录
                logger.info(f"  创建新的分析记录")
                plot_analysis = PlotAnalysis(
                    chapter_id=chapter_id,
                    project_id=project_id,
                    plot_stage=analysis_result.get('plot_stage', '发展'),
                    conflict_level=analysis_result.get('conflict', {}).get('level', 0),
                    conflict_types=analysis_result.get('conflict', {}).get('types', []),
                    emotional_tone=analysis_result.get('emotional_arc', {}).get('primary_emotion', ''),
                    emotional_intensity=analysis_result.get('emotional_arc', {}).get('intensity', 0) / 10.0,
                    hooks=analysis_result.get('hooks', []),
                    hooks_count=len(analysis_result.get('hooks', [])),
                    foreshadows=analysis_result.get('foreshadows', []),
                    foreshadows_planted=sum(1 for f in analysis_result.get('foreshadows', []) if f.get('type') == 'planted'),
                    foreshadows_resolved=sum(1 for f in analysis_result.get('foreshadows', []) if f.get('type') == 'resolved'),
                    plot_points=analysis_result.get('plot_points', []),
                    plot_points_count=len(analysis_result.get('plot_points', [])),
                    character_states=analysis_result.get('character_states', []),
                    scenes=analysis_result.get('scenes', []),
                    pacing=analysis_result.get('pacing', 'moderate'),
                    overall_quality_score=analysis_result.get('scores', {}).get('overall', 0),
                    pacing_score=analysis_result.get('scores', {}).get('pacing', 0),
                    engagement_score=analysis_result.get('scores', {}).get('engagement', 0),
                    coherence_score=analysis_result.get('scores', {}).get('coherence', 0),
                    analysis_report=analyzer.generate_analysis_summary(analysis_result),
                    suggestions=analysis_result.get('suggestions', []),
                    dialogue_ratio=analysis_result.get('dialogue_ratio', 0),
                    description_ratio=analysis_result.get('description_ratio', 0)
                )
                db_session.add(plot_analysis)
            
            await db_session.commit()
            
            task.progress = 80
            await db_session.commit()
        await report_progress(80)
        
        # 5. 清理旧的分析伏笔（重新分析时需要先清理）
        try:
            async with write_lock:
                clean_result = await foreshadow_service.clean_chapter_analysis_foreshadows(
                    db=db_session,
                    project_id=project_id,
                    chapter_id=chapter_id
                )
            if clean_result['cleaned_count'] > 0:
                logger.info(f"🧹 重新分析前清理了 {clean_result['cleaned_count']} 个旧伏笔")
        except Exception as clean_error:
            logger.warning(f"⚠️ 清理旧伏笔失败（继续分析）: {str(clean_error)}")
        
        # 6. 提取记忆并保存到向量数据库（传入章节内容用于计算位置）
        memories = analyzer.extract_memories_from_analysis(
            analysis=analysis_result,
            chapter_id=chapter_id,
            chapter_number=chapter.chapter_number,
            chapter_content=chapter.content or "",
            chapter_title=chapter.title or ""
        )
        
        # 先删除该章节的旧记忆（写操作，需要锁）
        async with write_lock:
            old_memories_result = await db_session.execute(
                select(StoryMemory).where(StoryMemory.chapter_id == chapter_id)
            )
            old_memories = old_memories_result.scalars().all()
            for old_mem in old_memories:
                await db_session.delete(old_mem)
            await db_session.commit()
            logger.info(f"  删除旧记忆: {len(old_memories)}条")
        
        # 准备批量添加的记忆数据（不需要锁）
        memory_records = []
        for mem in memories:
            memory_id = f"{chapter_id}_{mem['type']}_{len(memory_records)}"
            memory_records.append({
                'id': memory_id,
                'content': mem['content'],
                'type': mem['type'],
                'metadata': mem['metadata']
            })
            
        # 保存到关系数据库（写操作，需要锁）
        async with write_lock:
            for index, mem in enumerate(memories):
                memory_id = memory_records[index]['id']
                text_position = mem['metadata'].get('text_position', -1)
                text_length = mem['metadata'].get('text_length', 0)
                
                story_memory = StoryMemory(
                    id=memory_id,
                    project_id=project_id,
                    chapter_id=chapter_id,
                    memory_type=mem['type'],
                    content=mem['content'],
                    title=mem['title'],
                    importance_score=mem['metadata'].get('importance_score', 0.5),
                    tags=mem['metadata'].get('tags', []),
                    is_foreshadow=mem['metadata'].get('is_foreshadow', 0),
                    story_timeline=chapter.chapter_number,
                    chapter_position=text_position,
                    text_length=text_length,
                    related_characters=mem['metadata'].get('related_characters', []),
                    related_locations=mem['metadata'].get('related_locations', [])
                )
                db_session.add(story_memory)
                
                if text_position >= 0:
                    logger.debug(f"  保存记忆 {memory_id}: position={text_position}, length={text_length}")
            
            await db_session.commit()
        
        # 批量添加到向量数据库
        if memory_records:
            added_count = await memory_service.batch_add_memories(
                user_id=user_id,
                project_id=project_id,
                memories=memory_records
            )
            logger.info(f"✅ 添加{added_count}条记忆到向量库")
        
        # 💼 更新角色职业（根据分析结果）
        if analysis_result.get('character_states'):
            try:
                from app.services.career_update_service import CareerUpdateService
                
                logger.info(f"💼 开始根据分析结果更新角色职业...")
                career_update_result = await CareerUpdateService.update_careers_from_analysis(
                    db=db_session,
                    project_id=project_id,
                    character_states=analysis_result.get('character_states', []),
                    chapter_id=chapter_id,
                    chapter_number=chapter.chapter_number
                )
                
                if career_update_result['updated_count'] > 0:
                    logger.info(
                        f"✅ 更新了 {career_update_result['updated_count']} 个角色的职业信息"
                    )
                    if career_update_result['changes']:
                        for change in career_update_result['changes']:
                            logger.info(f"  - {change}")
                else:
                    logger.info("ℹ️ 本章节无角色职业变化")
                    
            except Exception as career_error:
                # 职业更新失败不应影响整个分析流程
                logger.error(f"⚠️ 更新角色职业失败: {str(career_error)}", exc_info=True)
        else:
            logger.debug("📋 分析结果中无角色状态信息，跳过职业更新")
        
        # 👤 更新角色心理状态和关系（根据分析结果）
        if analysis_result.get('character_states'):
            try:
                from app.services.character_state_update_service import CharacterStateUpdateService
                
                logger.info(f"👤 开始根据分析结果更新角色状态、关系和组织成员...")
                async with write_lock:
                    state_update_result = await CharacterStateUpdateService.update_from_analysis(
                        db=db_session,
                        project_id=project_id,
                        character_states=analysis_result.get('character_states', []),
                        chapter_id=chapter_id,
                        chapter_number=chapter.chapter_number
                    )
                
                total_state_changes = (
                    state_update_result['state_updated_count'] +
                    state_update_result['relationship_created_count'] +
                    state_update_result['relationship_updated_count'] +
                    state_update_result.get('org_updated_count', 0)
                )
                if total_state_changes > 0:
                    logger.info(
                        f"✅ 角色状态更新: 心理状态{state_update_result['state_updated_count']}个, "
                        f"新建关系{state_update_result['relationship_created_count']}个, "
                        f"更新关系{state_update_result['relationship_updated_count']}个, "
                        f"组织变动{state_update_result.get('org_updated_count', 0)}个"
                    )
                    if state_update_result['changes']:
                        for change in state_update_result['changes'][:8]:
                            logger.info(f"  - {change}")
                else:
                    logger.info("ℹ️ 本章节无角色状态、关系或组织变化")
                    
            except Exception as state_error:
                # 角色状态更新失败不应影响整个分析流程
                logger.error(f"⚠️ 更新角色状态、关系和组织失败: {str(state_error)}", exc_info=True)
        
        # 🏛️ 更新组织自身状态（根据分析结果）
        if analysis_result.get('organization_states'):
            try:
                from app.services.character_state_update_service import CharacterStateUpdateService
                
                logger.info(f"🏛️ 开始根据分析结果更新组织自身状态...")
                async with write_lock:
                    org_state_result = await CharacterStateUpdateService.update_organization_states(
                        db=db_session,
                        project_id=project_id,
                        organization_states=analysis_result.get('organization_states', []),
                        chapter_number=chapter.chapter_number
                    )
                
                if org_state_result['updated_count'] > 0:
                    logger.info(
                        f"✅ 组织状态更新: {org_state_result['updated_count']}个组织"
                    )
                    if org_state_result['changes']:
                        for change in org_state_result['changes'][:5]:
                            logger.info(f"  - {change}")
                else:
                    logger.info("ℹ️ 本章节无组织自身状态变化")
                    
            except Exception as org_state_error:
                # 组织状态更新失败不应影响整个分析流程
                logger.error(f"⚠️ 更新组织自身状态失败: {str(org_state_error)}", exc_info=True)
        
        # 🔮 自动更新伏笔状态（根据分析结果）
        if analysis_result.get('foreshadows'):
            try:
                logger.info(f"🔮 开始根据分析结果自动更新伏笔状态...")
                async with write_lock:
                    foreshadow_stats = await foreshadow_service.auto_update_from_analysis(
                        db=db_session,
                        project_id=project_id,
                        chapter_id=chapter_id,
                        chapter_number=chapter.chapter_number,
                        analysis_foreshadows=analysis_result.get('foreshadows', [])
                    )
                
                if foreshadow_stats['planted_count'] > 0 or foreshadow_stats['resolved_count'] > 0:
                    logger.info(
                        f"✅ 伏笔自动更新: 埋入{foreshadow_stats['planted_count']}个, "
                        f"回收{foreshadow_stats['resolved_count']}个"
                    )
                else:
                    logger.info("ℹ️ 本章节无新的伏笔状态变化")
                    
            except Exception as foreshadow_error:
                # 伏笔更新失败不应影响整个分析流程
                logger.error(f"⚠️ 自动更新伏笔失败: {str(foreshadow_error)}", exc_info=True)
        else:
            logger.debug("📋 分析结果中无伏笔信息，跳过伏笔自动更新")
        
        update_success = await _set_analysis_task_terminal_state(
            user_id, task_id, "completed"
        )
        if update_success:
            await report_progress(100)
            logger.info(f"✅ 章节分析完成: {chapter_id}, 提取{len(memories)}条记忆")
        else:
            logger.error(f"❌ 章节分析完成但无法更新任务终态: {chapter_id}")
        return update_success
        
    except Exception as e:
        logger.error(f"❌ 后台分析异常: {str(e)}", exc_info=True)
        if db_session:
            try:
                await db_session.rollback()
            except Exception as rollback_error:
                logger.warning(f"回滚分析事务失败: {rollback_error}")
        await _set_analysis_task_terminal_state(
            user_id, task_id, "failed", str(e)[:500]
        )
        return False
        
    finally:
        if db_session:
            await db_session.close()


async def _resolve_project_style_content(
    db: AsyncSession,
    project: Optional[Project],
    chapter_number: Optional[int],
    style_id: Optional[int],
    user_id: str,
    *,
    use_project_default: bool = False,
) -> str:
    """解析生成用的写作风格内容（计划 §13-3：按目标章节自动选卷）。

    优先级：
    1. ``style_id`` 指向本项目的卷画像 → 直接用该卷内容（用户显式选择）；
    2. ``style_id`` 为空或指向本项目全书画像（``profile:<project_id>``）→ 按章号
       匹配卷画像，命中则用卷画像；
    3. 其余保留原有语义：显式普通风格直接用，``use_project_default`` 时回退项目默认风格。

    显式选择普通风格时不会被卷画像覆盖。
    """
    style: Optional[WritingStyle] = None
    if style_id:
        style = (
            await db.execute(select(WritingStyle).where(WritingStyle.id == style_id))
        ).scalar_one_or_none()
        # 全局预设（user_id 为 NULL）或本人风格可用；其他用户的风格忽略
        if style is not None and style.user_id is not None and style.user_id != user_id:
            logger.warning(f"风格 {style_id} 不属于当前用户，跳过")
            style = None
        if style is None:
            logger.warning(f"未找到风格 {style_id}")

    if style is not None:
        volume_ref = parse_volume_preset_id(style.preset_id)
        if volume_ref is not None:
            if project is None or volume_ref[0] != project.id:
                logger.warning(f"风格 {style_id} 属于其他项目的卷画像，跳过")
                return ""
            logger.info(f"使用指定卷画像: {style.name}（第 {volume_ref[1]} 卷）")
            return style.prompt_content or ""
        # 非画像风格（用户显式选择）：尊重用户选择，不参与选卷
        if project is None or style.preset_id != profile_preset_id(project.id):
            return style.prompt_content or ""

    # 未指定风格 / 指向本项目全书画像：按目标章节匹配卷画像
    if project is not None and chapter_number:
        volume = await resolve_volume_for_chapter(db, project.id, chapter_number)
        if volume is not None:
            volume_ref = parse_volume_preset_id(volume.preset_id)
            index_label = volume_ref[1] if volume_ref else "?"
            logger.info(
                f"按第 {chapter_number} 章匹配到卷画像（第 {index_label} 卷）: {volume.name}"
            )
            return volume.prompt_content or ""

    if style is not None:
        # 全书画像：无卷画像命中时沿用原逻辑
        return style.prompt_content or ""

    if use_project_default and project is not None:
        from app.models.project_default_style import ProjectDefaultStyle

        default_style_id = (
            await db.execute(
                select(ProjectDefaultStyle.style_id).where(
                    ProjectDefaultStyle.project_id == project.id
                )
            )
        ).scalar_one_or_none()
        if default_style_id:
            default_style = (
                await db.execute(
                    select(WritingStyle).where(WritingStyle.id == default_style_id)
                )
            ).scalar_one_or_none()
            if default_style is not None and (
                default_style.user_id is None or default_style.user_id == user_id
            ):
                logger.info(f"使用项目默认写作风格: {default_style_id}")
                return default_style.prompt_content or ""

    return ""


@router.post("/{chapter_id}/generate-stream", summary="AI创作章节内容（流式）")
async def generate_chapter_content_stream(
    chapter_id: str,
    request: Request,
    background_tasks: BackgroundTasks,
    generate_request: ChapterGenerateRequest = ChapterGenerateRequest(),
    user_ai_service: AIService = Depends(get_user_ai_service)
):
    """
    根据大纲、前置章节内容和项目信息AI创作章节完整内容（流式返回）
    要求：必须按顺序生成，确保前置章节都已完成
    
    请求体参数：
    - style_id: 可选，指定使用的写作风格ID。不提供则不使用任何风格
    - target_word_count: 可选，目标字数，默认3000字，范围500-10000字
    - enable_mcp: 可选，是否启用MCP工具增强，默认True
    
    注意：此函数不使用依赖注入的db，而是在生成器内部创建独立的数据库会话
    以避免流式响应期间的连接泄漏问题
    """
    style_id = generate_request.style_id
    target_word_count = generate_request.target_word_count or 3000
    custom_model = generate_request.model if hasattr(generate_request, 'model') else None
    temp_narrative_perspective = generate_request.narrative_perspective if hasattr(generate_request, 'narrative_perspective') else None
    skill_key = generate_request.skill_key if hasattr(generate_request, 'skill_key') else None
    # 预先验证章节存在性（使用临时会话）
    async for temp_db in get_db(request):
        try:
            result = await temp_db.execute(
                select(Chapter).where(Chapter.id == chapter_id)
            )
            chapter = result.scalar_one_or_none()
            if not chapter:
                raise ApiError(code="not_found.chapter")
            
            # 检查前置条件
            can_generate, error_msg, previous_chapters = await check_prerequisites(temp_db, chapter)
            if not can_generate:
                raise ApiError(code=DYNAMIC_DETAIL_CODE, detail=error_msg, status=400, raw=error_msg)
            analysis_ready, analysis_msg = await check_previous_analysis_ready(temp_db, chapter)
            if not analysis_ready:
                raise ApiError(code=DYNAMIC_DETAIL_CODE, detail=analysis_msg, status=409, raw=analysis_msg)
            
            # 保存前置章节数据供生成器使用
            previous_chapters_data = [
                {
                    'id': ch.id,
                    'chapter_number': ch.chapter_number,
                    'title': ch.title,
                    'content': ch.content
                }
                for ch in previous_chapters
            ]
        finally:
            await temp_db.close()
        break
    
    async def event_generator():
        # 在生成器内部创建独立的数据库会话
        db_session = None
        db_committed = False
        # 获取当前用户ID（在生成器外部就需要）
        current_user_id = getattr(request.state, "user_id", "system")
        
        # 初始化标准进度追踪器
        from app.utils.sse_response import WizardProgressTracker
        tracker = WizardProgressTracker("章节")
        
        try:
            yield await tracker.start()
            
            # 创建新的数据库会话
            async for db_session in get_db(request):
                # === 加载阶段 ===
                yield await tracker.loading("加载章节信息...", 0.2)
                
                # 重新获取章节信息
                chapter_result = await db_session.execute(
                    select(Chapter).where(Chapter.id == chapter_id)
                )
                current_chapter = chapter_result.scalar_one_or_none()
                if not current_chapter:
                    yield await tracker.error("章节不存在", 404, error_code="not_found.chapter")
                    return
            
                yield await tracker.loading("加载项目信息...", 0.4)
                
                # 获取项目信息
                project_result = await db_session.execute(
                    select(Project).where(Project.id == current_chapter.project_id)
                )
                project = project_result.scalar_one_or_none()
                if not project:
                    yield await tracker.error("项目不存在", 404, error_code="not_found.project")
                    return
                
                # 获取项目的大纲模式
                outline_mode = project.outline_mode if project else 'one-to-many'
                logger.info(f"📋 项目大纲模式: {outline_mode}")
                
                # 获取对应的大纲（优先使用 chapter.outline_id 直接关联）
                if current_chapter.outline_id:
                    outline_result = await db_session.execute(
                        select(Outline)
                        .where(Outline.id == current_chapter.outline_id)
                        .execution_options(populate_existing=True)
                    )
                else:
                    # 回退到按序号查找
                    outline_result = await db_session.execute(
                        select(Outline)
                        .where(Outline.project_id == current_chapter.project_id)
                        .where(Outline.order_index == current_chapter.chapter_number)
                        .execution_options(populate_existing=True)
                    )
                outline = outline_result.scalar_one_or_none()
                
                # 获取写作风格（§13-3：未显式选择普通风格时按目标章节选卷）
                style_content = await _resolve_project_style_content(
                    db_session,
                    project,
                    current_chapter.chapter_number,
                    style_id,
                    current_user_id,
                )
                if not style_content:
                    logger.info("未指定写作风格，使用原始提示词")

                # issue #254 P1：生成路径注入本书原文语感锚点（局部重写不注入）
                style_content = await append_project_style_anchors(
                    db_session,
                    project=project,
                    style_content=style_content,
                    before_chapter_number=current_chapter.chapter_number,
                    override=_build_style_anchor_override(
                        enabled=generate_request.style_anchor_enabled,
                        chars=generate_request.style_anchor_chars,
                        count=generate_request.style_anchor_count,
                    ),
                )
                
                # 🚀 根据大纲模式选择独立的上下文构建器
                if outline_mode == 'one-to-one':
                    # ========== 1-1模式：使用独立的简化构建器 ==========
                    logger.info(f"🔧 [1-1模式] 使用 OneToOneContextBuilder")
                    context_builder = OneToOneContextBuilder(
                        memory_service=memory_service,
                        foreshadow_service=foreshadow_service,
                        full_book_budget_chars=await _resolve_full_book_budget(
                            user_ai_service,
                            custom_model or getattr(user_ai_service, "default_model", None),
                        ),
                    )
                    chapter_context = await context_builder.build(
                        chapter=current_chapter,
                        project=project,
                        outline=outline,
                        user_id=current_user_id,
                        db=db_session,
                        target_word_count=target_word_count
                    )
                    
                    # 日志输出统计信息
                    logger.info(f"📊 [1-1模式] 上下文统计:")
                    logger.info(f"  - 章节序号: {current_chapter.chapter_number}")
                    logger.info(f"  - 大纲长度: {chapter_context.context_stats.get('outline_length', 0)} 字符")
                    logger.info(f"  - 上一章内容: {chapter_context.context_stats.get('previous_content_length', 0)} 字符")
                    logger.info(f"  - 角色信息: {chapter_context.context_stats.get('characters_length', 0)} 字符")
                    logger.info(f"  - 伏笔提醒: {chapter_context.context_stats.get('foreshadow_length', 0)} 字符")
                    logger.info(f"  - 相关记忆: {chapter_context.context_stats.get('memories_length', 0)} 字符")
                    logger.info(f"  - 总长度: {chapter_context.context_stats.get('total_length', 0)} 字符")
                else:
                    # ========== 1-N模式：使用独立的完整构建器 ==========
                    logger.info(f"🔧 [1-N模式] 使用 OneToManyContextBuilder")
                    context_builder = OneToManyContextBuilder(
                        memory_service=memory_service,
                        foreshadow_service=foreshadow_service,
                        full_book_budget_chars=await _resolve_full_book_budget(
                            user_ai_service,
                            custom_model or getattr(user_ai_service, "default_model", None),
                        ),
                    )
                    chapter_context = await context_builder.build(
                        chapter=current_chapter,
                        project=project,
                        outline=outline,
                        user_id=current_user_id,
                        db=db_session,
                        style_content=style_content,
                        target_word_count=target_word_count,
                        temp_narrative_perspective=temp_narrative_perspective
                    )
                    
                    # 日志输出统计信息
                    logger.info(f"📊 [1-N模式] 上下文统计:")
                    logger.info(f"  - 章节序号: {current_chapter.chapter_number}")
                    logger.info(f"  - 衔接锚点: {chapter_context.context_stats.get('continuation_length', 0)} 字符")
                    logger.info(f"  - 角色信息: {chapter_context.context_stats.get('characters_length', 0)} 字符")
                    logger.info(f"  - 相关记忆: {chapter_context.context_stats.get('memories_length', 0)} 字符")
                    logger.info(f"  - 故事骨架: {chapter_context.context_stats.get('skeleton_length', 0)} 字符")
                    logger.info(f"  - 伏笔提醒: {chapter_context.context_stats.get('foreshadow_length', 0)} 字符")
                    logger.info(f"  - 总长度: {chapter_context.context_stats.get('total_length', 0)} 字符")
            
                yield await tracker.loading("上下文构建完成", 0.8)
                
                # 🎭 确定使用的叙事人称（临时指定 > 项目默认 > 系统默认）
                chapter_perspective = (
                    temp_narrative_perspective or
                    project.narrative_perspective or
                    '第三人称'
                )
                logger.info(f"📝 使用叙事人称: {chapter_perspective}")

                # 解析最终生成语言：per-gen override > 用户偏好 > UI 语言 > zh（todo 17）
                generation_language = await resolve_user_generation_language(
                    db_session, current_user_id, generate_request.content_language
                )

                # 🚀 根据大纲模式选择提示词模板和参数
                if outline_mode == 'one-to-one':
                    # 1-1模式
                    if chapter_context.continuation_point:
                        # 有上一章内容
                        template = await PromptService.get_template("CHAPTER_GENERATION_ONE_TO_ONE_NEXT", current_user_id, db_session)
                        base_prompt = PromptService.format_prompt(
                            template,
                            project_title=project.title,
                            chapter_number=current_chapter.chapter_number,
                            chapter_title=current_chapter.title,
                            chapter_outline=chapter_context.chapter_outline,
                            target_word_count=target_word_count,
                            genre=project.genre or '未设定',
                            narrative_perspective=chapter_perspective,
                            previous_chapter_content=chapter_context.continuation_point,
                            previous_chapter_summary=chapter_context.previous_chapter_summary or '（无上一章摘要）',
                            recent_chapters_context=chapter_context.recent_chapters_context or '暂无最近章节摘要',
                            characters_info=chapter_context.chapter_characters or '暂无角色信息',
                            chapter_careers=chapter_context.chapter_careers or '暂无职业信息',
                            foreshadow_reminders=chapter_context.foreshadow_reminders or '暂无需要关注的伏笔',
                            relevant_memories=chapter_context.relevant_memories or '暂无相关记忆',
                            content_language=generation_language
                        )
                        logger.debug(f"创建第{current_chapter.chapter_number}章提示词完成: prompt_length={len(base_prompt)}")
                    else:
                        # 第一章
                        template = await PromptService.get_template("CHAPTER_GENERATION_ONE_TO_ONE", current_user_id, db_session)
                        base_prompt = PromptService.format_prompt(
                            template,
                            project_title=project.title,
                            chapter_number=current_chapter.chapter_number,
                            chapter_title=current_chapter.title,
                            chapter_outline=chapter_context.chapter_outline,
                            target_word_count=target_word_count,
                            genre=project.genre or '未设定',
                            narrative_perspective=chapter_perspective,
                            characters_info=chapter_context.chapter_characters or '暂无角色信息',
                            chapter_careers=chapter_context.chapter_careers or '暂无职业信息',
                            foreshadow_reminders=chapter_context.foreshadow_reminders or '暂无需要关注的伏笔',
                            relevant_memories=chapter_context.relevant_memories or '暂无相关记忆',
                            content_language=generation_language
                        )
                        logger.debug(f"创建第一章提示词完成: prompt_length={len(base_prompt)}")
                else:
                    # ========== 1-n模式：使用完整模板 ==========
                    if chapter_context.continuation_point:
                        # 有前置内容，使用 WITH_CONTEXT 模板
                        logger.info(f"📝 [1-n模式] 使用带上下文的模板（第{current_chapter.chapter_number}章）")
                        
                        # 提取上一章摘要
                        previous_summary = "（无上一章摘要，请根据锚点续写）"
                        if chapter_context.previous_chapter_summary:
                            previous_summary = chapter_context.previous_chapter_summary
                        
                        template = await PromptService.get_template("CHAPTER_GENERATION_ONE_TO_MANY_NEXT", current_user_id, db_session)
                        base_prompt = PromptService.format_prompt(
                            template,
                            project_title=project.title,
                            chapter_number=current_chapter.chapter_number,
                            chapter_title=current_chapter.title,
                            chapter_outline=chapter_context.chapter_outline,
                            target_word_count=target_word_count,
                            continuation_point=chapter_context.continuation_point,
                            genre=project.genre or '未设定',
                            narrative_perspective=chapter_perspective,
                            characters_info=chapter_context.chapter_characters or '暂无角色信息',
                            chapter_careers=chapter_context.chapter_careers or '暂无职业信息',
                            foreshadow_reminders=chapter_context.foreshadow_reminders or '暂无需要关注的伏笔',
                            previous_chapter_summary=previous_summary,
                            recent_chapters_context=chapter_context.recent_chapters_context or '',
                            relevant_memories=chapter_context.relevant_memories or '',
                            content_language=generation_language
                        )
                        logger.debug(f"创建第{current_chapter.chapter_number}章提示词完成: prompt_length={len(base_prompt)}")
                    else:
                        # 第1章，使用无前置内容模板
                        logger.info(f"📝 [1-n模式] 使用第一章模板")
                        template = await PromptService.get_template("CHAPTER_GENERATION_ONE_TO_MANY", current_user_id, db_session)
                        base_prompt = PromptService.format_prompt(
                            template,
                            project_title=project.title,
                            chapter_number=current_chapter.chapter_number,
                            chapter_title=current_chapter.title,
                            chapter_outline=chapter_context.chapter_outline,
                            target_word_count=target_word_count,
                            genre=project.genre or '未设定',
                            narrative_perspective=chapter_perspective,
                            characters_info=chapter_context.chapter_characters or '暂无角色信息',
                            chapter_careers=chapter_context.chapter_careers or '暂无职业信息',
                            foreshadow_reminders=chapter_context.foreshadow_reminders or '暂无需要关注的伏笔',
                            relevant_memories=chapter_context.relevant_memories or '暂无相关记忆',
                            content_language=generation_language
                        )
                        logger.debug(f"创建第一章提示词完成: prompt_length={len(base_prompt)}")
                
                # 应用写作风格
                if style_content:
                    prompt = WritingStyleManager.apply_style_to_prompt(base_prompt, style_content)
                else:
                    prompt = base_prompt
                prompt = _append_full_book_context(prompt, chapter_context.full_book_context)
                
                # === 准备阶段 ===
                yield await tracker.preparing("准备AI提示词...")
                
                logger.info(f"开始AI流式创作章节 {chapter_id}")
                
                # 🎨 方案一：将写作风格注入到系统提示词（最高优先级）
                system_prompt_with_style = None
                
                # ⚡ Skill 支持：当指定 skill_key 时，将 Skill 工作流注入系统提示词
                if skill_key:
                    try:
                        from app.services.skill_loader import get_all_skills_cached
                        skills = get_all_skills_cached()
                        skill = next((s for s in skills if s["template_key"] == skill_key), None)
                        if skill:
                            skill_content = skill["content"]
                            skill_name = skill["template_name"]
                            system_prompt_with_style = f"""【⚡ Skill 工作流：{skill_name}】

{skill_content}

⚠️ 请严格遵循上述 Skill 工作流指令进行创作！"""
                            if style_content:
                                system_prompt_with_style += f"""

【🎨 写作风格要求 - 补充】

{style_content}"""
                            logger.info(f"⚡ 已将 Skill '{skill_name}' 注入系统提示词（{len(skill_content)}字符）")
                        else:
                            logger.warning(f"⚠️ 未找到 Skill: {skill_key}")
                    except Exception as skill_err:
                        logger.warning(f"⚠️ 加载 Skill 失败: {skill_err}")
                
                if not system_prompt_with_style and style_content:
                    system_prompt_with_style = f"""【🎨 写作风格要求 - 最高优先级】

{style_content}

⚠️ 请严格遵循上述写作风格要求进行创作，这是最重要的指令！
确保在整个章节创作过程中始终保持风格的一致性。"""
                    logger.info(f"✅ 已将写作风格注入系统提示词（{len(style_content)}字符）")
                
                # 🔢 输出预算：单一来源（按**实际探测到的**上下文窗口剩余给足上限），
                # 不再按模型名/目标字数预测，也不在本地硬算后显式传 max_tokens。
                generated_model = custom_model or getattr(user_ai_service, "default_model", None)
                input_tokens_est = int(
                    (len(prompt) + len(system_prompt_with_style or "")) / CHARS_PER_TOKEN
                )
                window_tokens = None
                window_note: Optional[str] = None
                try:
                    window_tokens = await user_ai_service.resolve_effective_window_tokens(
                        generated_model
                    )
                except ApiError as exc:
                    window_note = exc.code
                    if exc.code == CAPABILITY_INCOMPLETE_CODE:
                        # 缓存结论缺正数窗口：门禁在派发处会放行，这里若不报出来就没人知道
                        # 本次预算退化了。必须 ERROR 级。
                        logger.error(f"❌ 上下文窗口结论缺失有效数值，预算退化: model={generated_model}, code={exc.code}")
                    else:
                        # 门禁类（below_minimum / not_configured 等）：派发处会按同一三元组重抛真实错误，
                        # 这里退回保守上限不会掩盖失败。
                        logger.warning(f"⚠️ 上下文窗口不可得，预算退回保守上限: code={exc.code}")
                except Exception as exc:
                    # 非门禁类异常（DB 抖动、解析 bug 等）在派发处**不会**重抛，必须显式暴露。
                    window_note = type(exc).__name__
                    logger.error(f"❌ 读取上下文窗口异常，预算退化: model={generated_model}, {type(exc).__name__}: {exc}")
                calculated_max_tokens, budget_breakdown = plan_output_budget(
                    target_chars=target_word_count,
                    model=generated_model,
                    base_url=getattr(user_ai_service, "base_url", None),
                    input_tokens=input_tokens_est,
                    context_window=window_tokens,
                )
                logger.info(
                    f"📊 输出预算: 目标 {target_word_count} 字 → "
                    f"content={budget_breakdown.get('content')} "
                    f"cap={budget_breakdown.get('cap')} "
                    f"measured_cap={budget_breakdown.get('measured_cap')} "
                    f"context_room={budget_breakdown.get('context_room')} "
                    f"window={budget_breakdown.get('window')} "
                    f"input_tokens={budget_breakdown.get('input_tokens')} "
                    f"window_note={window_note or 'ok'} "
                    f"→ max_tokens={calculated_max_tokens}"
                )
                
                # 准备生成参数
                finish_sink: Dict[str, Any] = {}
                prompt = _append_style_tail(prompt, style_content)
                generate_kwargs = {
                    "prompt": prompt,
                    "system_prompt": system_prompt_with_style,
                    "tool_choice": "required",
                    "max_tokens": calculated_max_tokens,
                    "auto_mcp": bool(generate_request.enable_mcp),
                    "finish_sink": finish_sink,
                }
                if custom_model:
                    logger.info(f"  使用自定义模型: {custom_model}")
                    generate_kwargs["model"] = custom_model
                    # 注意：这里使用用户配置的AI服务，模型参数会覆盖默认模型
                    # 如果需要切换provider，需要在前端传递provider参数
                
                # === 生成阶段 ===
                full_content = ""
                chunk_count = 0
                
                yield await tracker.generating(
                    current_chars=0,
                    estimated_total=target_word_count
                )
                
                async for chunk in user_ai_service.generate_text_stream(**generate_kwargs):
                    full_content += chunk
                    chunk_count += 1
                    
                    # 发送内容块
                    yield await tracker.generating_chunk(chunk)
                    
                    # 每5个chunk发送一次进度更新
                    if chunk_count % 5 == 0:
                        yield await tracker.generating(
                            current_chars=len(full_content),
                            estimated_total=target_word_count,
                            message=f'正在创作中... 已生成 {len(full_content)} 字'
                        )
                    
                    # 每20个chunk发送心跳
                    if chunk_count % 20 == 0:
                        yield await tracker.heartbeat()
                    
                    await asyncio.sleep(0)  # 让出控制权

                # 截断判定：空正文/预算耗尽不得冒充成功（流已发给客户端，无法原地重生成，
                # 因此这里明确报错而不是写一张 0 字章节）
                finish_reason = finish_sink.get("finish_reason")
                if _is_truncated_chapter_content(
                    full_content,
                    target_word_count,
                    finish_reason,
                    f"第{current_chapter.chapter_number}章",
                ):
                    usage = finish_sink.get("usage")
                    budget_error = _truncation_error_message(
                        chapter_label=f"第{current_chapter.chapter_number}章",
                        content_chars=len(full_content),
                        target_word_count=target_word_count,
                        finish_reason=finish_reason,
                        reasoning_tokens=getattr(usage, "reasoning_tokens", None),
                        reasoning_estimated=getattr(usage, "reasoning_estimated", False),
                        reasoning_chars=_reasoning_chars_from_usage(
                            usage, len(full_content)
                        ),
                        breakdown=budget_breakdown,
                        budget_attempts=[calculated_max_tokens],
                    )
                    logger.error(f"❌ {budget_error}")
                    yield await tracker.error(budget_error, error_code="task.failed")
                    return
                
                # === 保存阶段 ===
                yield await tracker.saving("正在保存章节...", 0.3)
                
                # 更新章节内容到数据库
                old_word_count = current_chapter.word_count or 0
                current_chapter.content = full_content
                new_word_count = len(full_content)
                current_chapter.word_count = new_word_count
                current_chapter.status = "completed"
                current_chapter.summary = _build_lightweight_chapter_summary(full_content)
                
                # 更新项目字数
                project.current_words = project.current_words - old_word_count + new_word_count
                
                # 记录生成历史
                history = GenerationHistory(
                    project_id=current_chapter.project_id,
                    chapter_id=current_chapter.id,
                    prompt=f"创作章节: 第{current_chapter.chapter_number}章 {current_chapter.title}",
                    generated_content=full_content[:500] if len(full_content) > 500 else full_content,
                    model="default"
                )
                db_session.add(history)
                
                await db_session.commit()
                db_committed = True
                await db_session.refresh(current_chapter)
                
                logger.info(f"成功创作章节 {chapter_id}，共 {new_word_count} 字")
                
                # 🔮 章节生成后自动标记计划在本章埋入的伏笔
                try:
                    plant_result = await foreshadow_service.auto_plant_pending_foreshadows(
                        db=db_session,
                        project_id=project.id,
                        chapter_id=chapter_id,
                        chapter_number=current_chapter.chapter_number,
                        chapter_content=full_content
                    )
                    if plant_result.get('planted_count', 0) > 0:
                        logger.info(f"🔮 自动标记伏笔已埋入: {plant_result['planted_count']}个")
                except Exception as plant_error:
                    logger.warning(f"⚠️ 自动标记伏笔埋入失败: {str(plant_error)}")
                
                # 创建分析任务
                analysis_task = AnalysisTask(
                    chapter_id=chapter_id,
                    user_id=current_user_id,
                    project_id=project.id,
                    status='pending',
                    progress=0
                )
                db_session.add(analysis_task)
                await db_session.commit()
                await db_session.refresh(analysis_task)
                
                task_id = analysis_task.id
                logger.info(f"📋 已创建分析任务: {task_id}")
                
                # 短暂延迟确保SQLite WAL完成写入
                await asyncio.sleep(0.05)
                
                # 直接启动后台分析（并发执行）
                background_tasks.add_task(
                    analyze_chapter_background,
                    chapter_id=chapter_id,
                    user_id=current_user_id,
                    project_id=project.id,
                    task_id=task_id,
                    ai_service=user_ai_service
                )
                
                yield await tracker.saving("章节保存完成", 0.8)
                
                # issue #254 P2：生成后文笔一致性自检（只读，失败降级为 available=False）
                style_check = await compute_style_check_safely(
                    db_session, project, full_content
                )
                
                # === 完成阶段 ===
                yield await tracker.complete("创作完成！", code="progress.done")
                
                # 发送结果数据
                yield await tracker.result({
                    'word_count': new_word_count,
                    'analysis_task_id': task_id,
                    'style_check': style_check,
                })
                
                # 发送分析开始事件（使用自定义事件）
                yield await SSEResponse.send_event(
                    event='analysis_started',
                    data={
                        'task_id': task_id,
                        'message': '章节分析已开始'
                    }
                )
                
                # 发送完成信号
                yield await tracker.done()
                
                break  # 退出async for db_session循环
        
        except GeneratorExit:
            # SSE连接断开
            logger.warning("章节生成器被提前关闭（SSE断开）")
            if db_session and not db_committed:
                try:
                    if db_session.in_transaction():
                        await db_session.rollback()
                        logger.info("章节生成事务已回滚（GeneratorExit）")
                except Exception as e:
                    logger.error(f"GeneratorExit回滚失败: {str(e)}")
        except Exception as e:
            logger.error(f"流式创作章节失败: {str(e)}")
            if db_session and not db_committed:
                try:
                    if db_session.in_transaction():
                        await db_session.rollback()
                        logger.info("章节生成事务已回滚（异常）")
                except Exception as rollback_error:
                    logger.error(f"回滚失败: {str(rollback_error)}")
            yield await tracker.error_from_exception(e, str(e))
        finally:
            # 确保数据库会话被正确关闭
            if db_session:
                try:
                    # 最后检查：确保没有未提交的事务
                    if not db_committed and db_session.in_transaction():
                        await db_session.rollback()
                        logger.warning("在finally中发现未提交事务，已回滚")
                    
                    await db_session.close()
                    logger.info("数据库会话已关闭")
                except Exception as close_error:
                    logger.error(f"关闭数据库会话失败: {str(close_error)}")
                    # 强制关闭
                    try:
                        await db_session.close()
                    except Exception:
                        pass
    
    return create_sse_response(event_generator())


@router.post("/{chapter_id}/generate-background", summary="AI创作章节内容（后台任务）")
async def generate_chapter_content_background(
    chapter_id: str,
    request: Request,
    generate_request: ChapterGenerateRequest = ChapterGenerateRequest(),
    db: AsyncSession = Depends(get_db)
):
    """
    创建后台任务来生成章节内容。
    任务创建后立即返回task_id，前端通过 GET /api/tasks/{task_id} 轮询进度。
    关闭浏览器不影响生成，生成完成后内容自动保存到数据库。
    """
    user_id = getattr(request.state, 'user_id', None)
    if not user_id:
        raise ApiError(code="auth.unauthorized")

    # 验证章节存在
    result = await db.execute(
        select(Chapter).where(Chapter.id == chapter_id)
    )
    chapter = result.scalar_one_or_none()
    if not chapter:
        raise ApiError(code="not_found.chapter")

    # 验证项目权限
    project = await verify_project_access(chapter.project_id, user_id, db)

    # 检查前置条件
    can_generate, error_msg, _ = await check_prerequisites(db, chapter)
    if not can_generate:
        raise ApiError(code=DYNAMIC_DETAIL_CODE, detail=error_msg, status=400, raw=error_msg)
    analysis_ready, analysis_msg = await check_previous_analysis_ready(db, chapter)
    if not analysis_ready:
        raise ApiError(code=DYNAMIC_DETAIL_CODE, detail=analysis_msg, status=409, raw=analysis_msg)

    # 创建后台任务
    from app.services.background_task_service import background_task_service, TaskProgressTracker
    task = await background_task_service.create_task(
        user_id=user_id,
        project_id=chapter.project_id,
        task_type="chapter_generate",
        task_input={
            "chapter_id": chapter_id,
            "style_id": generate_request.style_id,
            "target_word_count": generate_request.target_word_count or 3000,
            "enable_mcp": generate_request.enable_mcp,
            "model": generate_request.model,
            "narrative_perspective": generate_request.narrative_perspective,
            "skill_key": generate_request.skill_key,
            "content_language": generate_request.content_language,
            "style_anchor_enabled": generate_request.style_anchor_enabled,
            "style_anchor_chars": generate_request.style_anchor_chars,
            "style_anchor_count": generate_request.style_anchor_count,
        },
        db=db
    )

    # 后台执行的函数
    async def _run_chapter_generation(task_id: str, bg_user_id: str):
        from app.database import get_engine
        from sqlalchemy.ext.asyncio import async_sessionmaker, AsyncSession as BgAsyncSession

        engine = await get_engine(bg_user_id)
        AsyncSessionLocal = async_sessionmaker(engine, class_=BgAsyncSession, expire_on_commit=False)

        async with AsyncSessionLocal() as bg_db:
            tracker = TaskProgressTracker(task_id, bg_user_id, "章节")
            try:
                await tracker.start()

                # 获取AI服务
                from app.api.settings import get_user_ai_service_from_db
                bg_ai_service = await get_user_ai_service_from_db(bg_user_id, bg_db)

                await _run_chapter_generation_bg(
                    task_input={
                        "chapter_id": chapter_id,
                        "style_id": generate_request.style_id,
                        "target_word_count": generate_request.target_word_count or 3000,
                        "enable_mcp": generate_request.enable_mcp,
                        "model": generate_request.model,
                        "narrative_perspective": generate_request.narrative_perspective,
                        "skill_key": generate_request.skill_key,
                        "content_language": generate_request.content_language,
                        "style_anchor_enabled": generate_request.style_anchor_enabled,
                        "style_anchor_chars": generate_request.style_anchor_chars,
                        "style_anchor_count": generate_request.style_anchor_count,
                    },
                    db=bg_db,
                    ai_service=bg_ai_service,
                    tracker=tracker,
                    user_id=bg_user_id,
                    task_id=task_id,
                )

            except Exception as e:
                logger.error(f"❌ 后台章节生成失败: {e}", exc_info=True)
                await tracker.error_from_exception(e)

    await background_task_service.spawn_background_task(
        task.id, user_id, _run_chapter_generation
    )

    return {
        "task_id": task.id,
        "task_type": "chapter_generate",
        "status": "pending",
        "message": "任务已创建，请通过 GET /api/tasks/{task_id} 查询进度"
    }


def _build_analysis_task_status_payload(
    chapter_id: str,
    task: Optional[AnalysisTask],
    auto_recovered: bool = False
) -> dict:
    """统一构建分析任务状态响应"""
    if not task:
        return {
            "has_task": False,
            "chapter_id": chapter_id,
            "status": "none",
            "progress": 0,
            "error_message": None,
            "auto_recovered": False,
            "task_id": None,
            "created_at": None,
            "started_at": None,
            "completed_at": None
        }

    return {
        "has_task": True,
        "task_id": task.id,
        "chapter_id": task.chapter_id,
        "status": task.status,
        "progress": task.progress,
        "error_message": task.error_message,
        "auto_recovered": auto_recovered,
        "created_at": task.created_at.isoformat() if task.created_at else None,
        "started_at": task.started_at.isoformat() if task.started_at else None,
        "completed_at": task.completed_at.isoformat() if task.completed_at else None
    }


async def _recover_stale_analysis_tasks(
    db: AsyncSession,
    tasks: list[AnalysisTask],
) -> set[str]:
    """将失去执行协程的陈旧任务原子地恢复为失败状态。"""
    now = datetime.now()
    recovered_ids: set[str] = set()

    for task in tasks:
        error_message: Optional[str] = None
        if task.status == "running":
            reference_time = task.started_at or task.created_at
            if reference_time and (now - reference_time) > timedelta(seconds=ANALYSIS_TASK_STALE_SECONDS):
                error_message = "任务执行超时，已自动恢复"
        if not error_message:
            continue

        result = await db.execute(
            update(AnalysisTask)
            .where(
                AnalysisTask.id == task.id,
                AnalysisTask.status == task.status,
            )
            .values(
                status="failed",
                error_message=error_message,
                completed_at=now,
                progress=0,
            )
            .execution_options(synchronize_session="fetch")
        )
        if result.rowcount:
            recovered_ids.add(task.id)

    if recovered_ids:
        await db.commit()
        for task in tasks:
            if task.id in recovered_ids:
                await db.refresh(task)
        logger.warning(f"自动恢复陈旧章节分析任务: {len(recovered_ids)} 个")

    return recovered_ids


async def _recover_stale_batch_tasks(
    db: AsyncSession,
    tasks: list[BatchGenerationTask],
    *,
    stale_minutes: Optional[int] = None,
) -> set[str]:
    """将心跳超时的批量生成任务原子地恢复为失败状态。

    worker 被重启（如 uvicorn --reload 重载）后，正在执行的批量协程不会留下
    任何终态；读取接口据此把「假 running」恢复为 failed，避免任务永久卡住。
    """
    minutes = BATCH_STALE_MINUTES if stale_minutes is None else stale_minutes
    now = datetime.now()
    cutoff = now - timedelta(minutes=minutes)
    recovered_ids: set[str] = set()

    for task in tasks:
        if task.status != "running":
            continue
        # 回退链不含 created_at：created_at 存 UTC，其余是本地 naive，混用会早杀 8 小时；
        # 进入 running 时必然写 started_at。
        reference_time = task.heartbeat_at or task.started_at
        if not reference_time or reference_time >= cutoff:
            continue

        result = await db.execute(
            update(BatchGenerationTask)
            .where(
                BatchGenerationTask.id == task.id,
                # 与 done-callback 共用同一幂等条件：只有仍 running 才翻转。
                # 两条路径的 UPDATE 各自原子，先到者成功，后到者 rowcount=0。
                BatchGenerationTask.status == "running",
            )
            .values(
                status="failed",
                completed_at=now,
                error_message="批量生成中断：worker 心跳超时（可能是服务重启），请重新发起",
            )
            .execution_options(synchronize_session="fetch")
        )
        if result.rowcount:
            recovered_ids.add(task.id)

    if recovered_ids:
        await db.commit()
        for task in tasks:
            if task.id in recovered_ids:
                await db.refresh(task)
        logger.warning(f"自动恢复陈旧批量生成任务: {len(recovered_ids)} 个")

    return recovered_ids


@router.post("/{chapter_id}/generate-background-legacy", summary="AI创作章节内容（后台任务，遗留重复实现）")
async def generate_chapter_content_background_legacy(
    chapter_id: str,
    request: Request,
    generate_request: ChapterGenerateRequest = ChapterGenerateRequest(),
    db: AsyncSession = Depends(get_db)
):
    """
    创建后台任务来生成章节内容。
    任务创建后立即返回task_id，前端通过 GET /api/tasks/{task_id} 轮询进度。
    关闭浏览器不影响生成，生成完成后内容自动保存到数据库。
    """
    user_id = getattr(request.state, 'user_id', None)
    if not user_id:
        raise ApiError(code="auth.unauthorized")

    # 验证章节存在
    result = await db.execute(
        select(Chapter).where(Chapter.id == chapter_id)
    )
    chapter = result.scalar_one_or_none()
    if not chapter:
        raise ApiError(code="not_found.chapter")

    # 验证项目权限
    project = await verify_project_access(chapter.project_id, user_id, db)

    # 检查前置条件
    can_generate, error_msg, _ = await check_prerequisites(db, chapter)
    if not can_generate:
        raise ApiError(code=DYNAMIC_DETAIL_CODE, detail=error_msg, status=400, raw=error_msg)

    # 创建后台任务
    from app.services.background_task_service import background_task_service, TaskProgressTracker
    task = await background_task_service.create_task(
        user_id=user_id,
        project_id=chapter.project_id,
        task_type="chapter_generate",
        task_input={
            "chapter_id": chapter_id,
            "style_id": generate_request.style_id,
            "target_word_count": generate_request.target_word_count or 3000,
            "enable_mcp": generate_request.enable_mcp,
            "model": generate_request.model,
            "narrative_perspective": generate_request.narrative_perspective,
            "content_language": generate_request.content_language,
            "style_anchor_enabled": generate_request.style_anchor_enabled,
            "style_anchor_chars": generate_request.style_anchor_chars,
            "style_anchor_count": generate_request.style_anchor_count,
        },
        db=db
    )

    # 后台执行的函数
    async def _run_chapter_generation(task_id: str, bg_user_id: str):
        from app.database import get_engine
        from sqlalchemy.ext.asyncio import async_sessionmaker, AsyncSession as BgAsyncSession

        engine = await get_engine(bg_user_id)
        AsyncSessionLocal = async_sessionmaker(engine, class_=BgAsyncSession, expire_on_commit=False)

        async with AsyncSessionLocal() as bg_db:
            tracker = TaskProgressTracker(task_id, bg_user_id, "章节")
            try:
                await tracker.start()

                # 获取AI服务
                from app.api.settings import get_user_ai_service_from_db
                bg_ai_service = await get_user_ai_service_from_db(bg_user_id, bg_db)

                await _run_chapter_generation_bg(
                    task_input={
                        "chapter_id": chapter_id,
                        "style_id": generate_request.style_id,
                        "target_word_count": generate_request.target_word_count or 3000,
                        "enable_mcp": generate_request.enable_mcp,
                        "model": generate_request.model,
                        "narrative_perspective": generate_request.narrative_perspective,
                        "content_language": generate_request.content_language,
                        "style_anchor_enabled": generate_request.style_anchor_enabled,
                        "style_anchor_chars": generate_request.style_anchor_chars,
                        "style_anchor_count": generate_request.style_anchor_count,
                    },
                    db=bg_db,
                    ai_service=bg_ai_service,
                    tracker=tracker,
                    user_id=bg_user_id,
                    task_id=task_id,
                )

            except Exception as e:
                logger.error(f"❌ 后台章节生成失败: {e}", exc_info=True)
                await tracker.error_from_exception(e)

    await background_task_service.spawn_background_task(
        task.id, user_id, _run_chapter_generation
    )

    return {
        "task_id": task.id,
        "task_type": "chapter_generate",
        "status": "pending",
        "message": "任务已创建，请通过 GET /api/tasks/{task_id} 查询进度"
    }


async def _run_chapter_generation_bg(
    task_input: dict,
    db: AsyncSession,
    ai_service: AIService,
    tracker,
    user_id: str,
    task_id: str,
):
    """后台执行章节生成（不使用SSE，直接生成并保存）"""
    from app.services.chapter_context_service import (
        OneToManyContextBuilder,
        OneToOneContextBuilder
    )

    chapter_id = task_input["chapter_id"]
    style_id = task_input.get("style_id")
    target_word_count = task_input.get("target_word_count", 3000)
    custom_model = task_input.get("model")
    temp_narrative_perspective = task_input.get("narrative_perspective")
    enable_mcp = task_input.get("enable_mcp", True)
    skill_key = task_input.get("skill_key")
    write_lock = await get_db_write_lock(user_id)

    # === 加载阶段 ===
    await tracker.loading("加载章节信息...", 0.2)

    chapter_result = await db.execute(
        select(Chapter).where(Chapter.id == chapter_id)
    )
    current_chapter = chapter_result.scalar_one_or_none()
    if not current_chapter:
        await tracker.error("章节不存在", error_code="not_found.chapter")
        return

    await tracker.loading("加载项目信息...", 0.4)

    project_result = await db.execute(
        select(Project).where(Project.id == current_chapter.project_id)
    )
    project = project_result.scalar_one_or_none()
    if not project:
        await tracker.error("项目不存在", error_code="not_found.project")
        return

    outline_mode = project.outline_mode if project else 'one-to-many'

    # 获取大纲
    if current_chapter.outline_id:
        outline_result = await db.execute(
            select(Outline).where(Outline.id == current_chapter.outline_id)
        )
    else:
        outline_result = await db.execute(
            select(Outline)
            .where(Outline.project_id == current_chapter.project_id)
            .where(Outline.order_index == current_chapter.chapter_number)
        )
    outline = outline_result.scalar_one_or_none()

    # 获取写作风格（§13-3：未显式选择普通风格时按目标章节选卷）
    style_content = await _resolve_project_style_content(
        db,
        project,
        current_chapter.chapter_number,
        style_id,
        user_id,
    )

    # issue #254 P1：生成路径注入本书原文语感锚点（局部重写不注入）
    style_content = await append_project_style_anchors(
        db,
        project=project,
        style_content=style_content,
        before_chapter_number=current_chapter.chapter_number,
        override=_build_style_anchor_override(
            enabled=task_input.get("style_anchor_enabled"),
            chars=task_input.get("style_anchor_chars"),
            count=task_input.get("style_anchor_count"),
        ),
    )

    # === 构建上下文 ===
    if outline_mode == 'one-to-one':
        context_builder = OneToOneContextBuilder(
            memory_service=memory_service,
            foreshadow_service=foreshadow_service,
            full_book_budget_chars=await _resolve_full_book_budget(
                ai_service,
                custom_model or getattr(ai_service, "default_model", None),
            ),
        )
        chapter_context = await context_builder.build(
            chapter=current_chapter,
            project=project,
            outline=outline,
            user_id=user_id,
            db=db,
            target_word_count=target_word_count
        )
    else:
        context_builder = OneToManyContextBuilder(
            memory_service=memory_service,
            foreshadow_service=foreshadow_service,
            full_book_budget_chars=await _resolve_full_book_budget(
                ai_service,
                custom_model or getattr(ai_service, "default_model", None),
            ),
        )
        chapter_context = await context_builder.build(
            chapter=current_chapter,
            project=project,
            outline=outline,
            user_id=user_id,
            db=db,
            style_content=style_content,
            target_word_count=target_word_count,
            temp_narrative_perspective=temp_narrative_perspective
        )

    await tracker.loading("上下文构建完成", 0.8)

    # 确定叙事人称
    chapter_perspective = (
        temp_narrative_perspective or
        project.narrative_perspective or
        '第三人称'
    )

    # 解析最终生成语言：per-gen override > 用户偏好 > UI 语言 > zh（todo 17）
    generation_language = await resolve_user_generation_language(
        db, user_id, task_input.get("content_language")
    )

    # === 准备提示词 ===
    if outline_mode == 'one-to-one':
        if chapter_context.continuation_point:
            template = await PromptService.get_template("CHAPTER_GENERATION_ONE_TO_ONE_NEXT", user_id, db)
            base_prompt = PromptService.format_prompt(
                template,
                project_title=project.title,
                chapter_number=current_chapter.chapter_number,
                chapter_title=current_chapter.title,
                chapter_outline=chapter_context.chapter_outline,
                target_word_count=target_word_count,
                genre=project.genre or '未设定',
                narrative_perspective=chapter_perspective,
                previous_chapter_content=chapter_context.continuation_point,
                previous_chapter_summary=chapter_context.previous_chapter_summary or '（无上一章摘要）',
                recent_chapters_context=chapter_context.recent_chapters_context or '暂无最近章节摘要',
                characters_info=chapter_context.chapter_characters or '暂无角色信息',
                chapter_careers=chapter_context.chapter_careers or '暂无职业信息',
                foreshadow_reminders=chapter_context.foreshadow_reminders or '暂无需要关注的伏笔',
                relevant_memories=chapter_context.relevant_memories or '暂无相关记忆',
                content_language=generation_language
            )
        else:
            template = await PromptService.get_template("CHAPTER_GENERATION_ONE_TO_ONE", user_id, db)
            base_prompt = PromptService.format_prompt(
                template,
                project_title=project.title,
                chapter_number=current_chapter.chapter_number,
                chapter_title=current_chapter.title,
                chapter_outline=chapter_context.chapter_outline,
                target_word_count=target_word_count,
                genre=project.genre or '未设定',
                narrative_perspective=chapter_perspective,
                characters_info=chapter_context.chapter_characters or '暂无角色信息',
                chapter_careers=chapter_context.chapter_careers or '暂无职业信息',
                foreshadow_reminders=chapter_context.foreshadow_reminders or '暂无需要关注的伏笔',
                relevant_memories=chapter_context.relevant_memories or '暂无相关记忆',
                content_language=generation_language
            )
    else:
        if chapter_context.continuation_point:
            previous_summary = chapter_context.previous_chapter_summary or "（无上一章摘要，请根据锚点续写）"
            template = await PromptService.get_template("CHAPTER_GENERATION_ONE_TO_MANY_NEXT", user_id, db)
            base_prompt = PromptService.format_prompt(
                template,
                project_title=project.title,
                chapter_number=current_chapter.chapter_number,
                chapter_title=current_chapter.title,
                chapter_outline=chapter_context.chapter_outline,
                target_word_count=target_word_count,
                continuation_point=chapter_context.continuation_point,
                genre=project.genre or '未设定',
                narrative_perspective=chapter_perspective,
                characters_info=chapter_context.chapter_characters or '暂无角色信息',
                chapter_careers=chapter_context.chapter_careers or '暂无职业信息',
                foreshadow_reminders=chapter_context.foreshadow_reminders or '暂无需要关注的伏笔',
                previous_chapter_summary=previous_summary,
                recent_chapters_context=chapter_context.recent_chapters_context or '',
                relevant_memories=chapter_context.relevant_memories or '',
                content_language=generation_language
            )
        else:
            template = await PromptService.get_template("CHAPTER_GENERATION_ONE_TO_MANY", user_id, db)
            base_prompt = PromptService.format_prompt(
                template,
                project_title=project.title,
                chapter_number=current_chapter.chapter_number,
                chapter_title=current_chapter.title,
                chapter_outline=chapter_context.chapter_outline,
                target_word_count=target_word_count,
                genre=project.genre or '未设定',
                narrative_perspective=chapter_perspective,
                characters_info=chapter_context.chapter_characters or '暂无角色信息',
                chapter_careers=chapter_context.chapter_careers or '暂无职业信息',
                foreshadow_reminders=chapter_context.foreshadow_reminders or '暂无需要关注的伏笔',
                relevant_memories=chapter_context.relevant_memories or '暂无相关记忆',
                content_language=generation_language
            )

    # 应用写作风格
    if style_content:
        prompt = WritingStyleManager.apply_style_to_prompt(base_prompt, style_content)
    else:
        prompt = base_prompt
    prompt = _append_full_book_context(prompt, chapter_context.full_book_context)

    # === 准备阶段 ===
    await tracker.preparing("准备AI提示词...")

    system_prompt_with_style = None
    if skill_key:
        try:
            from app.services.skill_loader import get_all_skills_cached
            skills = get_all_skills_cached()
            skill = next((s for s in skills if s["template_key"] == skill_key), None)
            if skill:
                skill_content = skill["content"]
                skill_name = skill["template_name"]
                system_prompt_with_style = f"""【⚡ Skill 工作流：{skill_name}】

{skill_content}

⚠️ 请严格遵循上述 Skill 工作流指令进行创作！"""
                if style_content:
                    system_prompt_with_style += f"""

【🎨 写作风格要求 - 补充】

{style_content}"""
                logger.info(f"⚡ 后台生成 - 已将 Skill '{skill_name}' 注入系统提示词（{len(skill_content)}字符）")
            else:
                logger.warning(f"⚠️ 后台生成 - 未找到 Skill: {skill_key}")
        except Exception as skill_err:
            logger.warning(f"⚠️ 后台生成 - 加载 Skill 失败: {skill_err}")

    if not system_prompt_with_style and style_content:
        system_prompt_with_style = f"""【🎨 写作风格要求 - 最高优先级】

{style_content}

⚠️ 请严格遵循上述写作风格要求进行创作，这是最重要的指令！
确保在整个章节创作过程中始终保持风格的一致性。"""

    # 输出预算单一来源：按**实际探测到的**上下文窗口剩余给足上限（见 plan_output_budget）
    generated_model = custom_model or getattr(ai_service, "default_model", None)
    input_tokens_est = int(
        (len(prompt) + len(system_prompt_with_style or "")) / CHARS_PER_TOKEN
    )
    window_tokens = None
    window_note: Optional[str] = None
    try:
        window_tokens = await ai_service.resolve_effective_window_tokens(generated_model)
    except ApiError as exc:
        window_note = exc.code
        if exc.code == CAPABILITY_INCOMPLETE_CODE:
            # 缓存结论缺正数窗口：门禁在派发处会放行，这里若不报出来就没人知道
            # 本次预算退化了。必须 ERROR 级。
            logger.error(f"❌ 上下文窗口结论缺失有效数值，预算退化: model={generated_model}, code={exc.code}")
        else:
            # 门禁类（below_minimum / not_configured 等）：派发处会按同一三元组重抛真实错误，
            # 这里退回保守上限不会掩盖失败。
            logger.warning(f"⚠️ 上下文窗口不可得，预算退回保守上限: code={exc.code}")
    except Exception as exc:
        # 非门禁类异常（DB 抖动、解析 bug 等）在派发处**不会**重抛，必须显式暴露。
        window_note = type(exc).__name__
        logger.error(f"❌ 读取上下文窗口异常，预算退化: model={generated_model}, {type(exc).__name__}: {exc}")
    calculated_max_tokens, budget_breakdown = plan_output_budget(
        target_chars=target_word_count,
        model=generated_model,
        base_url=getattr(ai_service, "base_url", None),
        input_tokens=input_tokens_est,
        context_window=window_tokens,
    )
    logger.info(
        f"📊 输出预算: 目标 {target_word_count} 字 → "
        f"content={budget_breakdown.get('content')} "
        f"cap={budget_breakdown.get('cap')} "
        f"measured_cap={budget_breakdown.get('measured_cap')} "
        f"context_room={budget_breakdown.get('context_room')} "
        f"window={budget_breakdown.get('window')} "
        f"input_tokens={budget_breakdown.get('input_tokens')} "
        f"window_note={window_note or 'ok'} "
        f"→ max_tokens={calculated_max_tokens}"
    )

    finish_sink: Dict[str, Any] = {}
    prompt = _append_style_tail(prompt, style_content)
    generate_kwargs = {
        "prompt": prompt,
        "system_prompt": system_prompt_with_style,
        "tool_choice": "required",
        "max_tokens": calculated_max_tokens,
        "auto_mcp": bool(enable_mcp),
        "finish_sink": finish_sink,
    }
    if custom_model:
        generate_kwargs["model"] = custom_model

    # === 生成阶段 ===
    full_content = ""
    chunk_count = 0

    await tracker.generating(
        current_chars=0,
        estimated_total=target_word_count
    )

    async for chunk in ai_service.generate_text_stream(**generate_kwargs):
        # 检查是否被取消
        if chunk_count % 10 == 0 and await tracker.check_cancelled():
            logger.info(f"🚫 后台章节生成被取消: {chapter_id}")
            return

        full_content += chunk
        chunk_count += 1

        # 每10个chunk更新一次进度
        if chunk_count % 10 == 0:
            await tracker.generating(
                current_chars=len(full_content),
                estimated_total=target_word_count,
                message=f'正在创作中... 已生成 {len(full_content)} 字'
            )

        await asyncio.sleep(0)

    finish_reason = finish_sink.get("finish_reason")
    if _is_truncated_chapter_content(
        full_content,
        target_word_count,
        finish_reason,
        f"第{current_chapter.chapter_number}章",
    ):
        # 截断即失败：预算已按窗口剩余给足，不再抬预算重试。
        usage = finish_sink.get("usage")
        budget_error = _truncation_error_message(
            chapter_label=f"第{current_chapter.chapter_number}章",
            content_chars=len(full_content),
            target_word_count=target_word_count,
            finish_reason=finish_reason,
            reasoning_tokens=getattr(usage, "reasoning_tokens", None),
            reasoning_estimated=getattr(usage, "reasoning_estimated", False),
            reasoning_chars=_reasoning_chars_from_usage(
                usage, len(full_content)
            ),
            breakdown=budget_breakdown,
            budget_attempts=[calculated_max_tokens],
        )
        logger.error(f"❌ {budget_error}")
        await tracker.error(budget_error, error_code="task.failed")
        return

    # === 保存阶段 ===
    await tracker.saving("正在保存章节...", 0.3)

    async with write_lock:
        # 重新获取章节（确保最新状态）
        chapter_result = await db.execute(
            select(Chapter).where(Chapter.id == chapter_id)
        )
        current_chapter = chapter_result.scalar_one_or_none()
        if not current_chapter:
            await tracker.error("保存时章节不存在", error_code="not_found.chapter")
            return

        old_word_count = current_chapter.word_count or 0
        current_chapter.content = full_content
        new_word_count = len(full_content)
        current_chapter.word_count = new_word_count
        current_chapter.status = "completed"
        current_chapter.summary = _build_lightweight_chapter_summary(full_content)

        # 更新项目字数
        project_result = await db.execute(
            select(Project).where(Project.id == current_chapter.project_id)
        )
        project_obj = project_result.scalar_one_or_none()
        if project_obj:
            project_obj.current_words = (project_obj.current_words or 0) - old_word_count + new_word_count

        # 记录生成历史
        history = GenerationHistory(
            project_id=current_chapter.project_id,
            chapter_id=current_chapter.id,
            prompt=f"创作章节: 第{current_chapter.chapter_number}章 {current_chapter.title}",
            generated_content=full_content[:500] if len(full_content) > 500 else full_content,
            model="default"
        )
        db.add(history)

        await db.commit()

    logger.info(f"✅ 后台创作章节 {chapter_id} 完成，共 {new_word_count} 字")

    # 🔮 自动标记伏笔
    try:
        plant_result = await foreshadow_service.auto_plant_pending_foreshadows(
            db=db,
            project_id=current_chapter.project_id,
            chapter_id=chapter_id,
            chapter_number=current_chapter.chapter_number,
            chapter_content=full_content
        )
        if plant_result.get('planted_count', 0) > 0:
            logger.info(f"🔮 自动标记伏笔已埋入: {plant_result['planted_count']}个")
    except Exception as plant_error:
        logger.warning(f"⚠️ 自动标记伏笔埋入失败: {str(plant_error)}")

    # 创建分析任务
    analysis_task = AnalysisTask(
        chapter_id=chapter_id,
        user_id=user_id,
        project_id=current_chapter.project_id,
        status='pending',
        progress=0
    )
    db.add(analysis_task)
    await db.commit()
    await db.refresh(analysis_task)

    logger.info(f"📋 后台生成：已创建分析任务: {analysis_task.id}")

    # issue #254 P2：生成后文笔一致性自检（只读，失败降级为 available=False）
    style_check = await compute_style_check_safely(db, project, full_content)

    await tracker.set_result({
        "chapter_id": chapter_id,
        "word_count": new_word_count,
        "analysis_task_id": analysis_task.id,
        "style_check": style_check,
    })
    await tracker.analyzing(0, "章节创作完成，准备分析...")

    analysis_success = await analyze_chapter_background(
        chapter_id=chapter_id,
        user_id=user_id,
        project_id=current_chapter.project_id,
        task_id=analysis_task.id,
        ai_service=ai_service,
        progress_callback=tracker.analyzing,
    )
    if not analysis_success:
        raise RuntimeError("章节内容已生成，但章节分析失败")

    # === 完成 ===
    await tracker.complete(
        f"创作和分析完成！共 {new_word_count} 字",
        code="progress.creation_done_words", params={"word_count": new_word_count},
    )


def _build_analysis_task_status_payload(
    chapter_id: str,
    task: Optional[AnalysisTask],
    auto_recovered: bool = False
) -> dict:
    """统一构建分析任务状态响应"""
    if not task:
        return {
            "has_task": False,
            "chapter_id": chapter_id,
            "status": "none",
            "progress": 0,
            "error_message": None,
            "auto_recovered": False,
            "task_id": None,
            "created_at": None,
            "started_at": None,
            "completed_at": None
        }

    return {
        "has_task": True,
        "task_id": task.id,
        "chapter_id": task.chapter_id,
        "status": task.status,
        "progress": task.progress,
        "error_message": task.error_message,
        "auto_recovered": auto_recovered,
        "created_at": task.created_at.isoformat() if task.created_at else None,
        "started_at": task.started_at.isoformat() if task.started_at else None,
        "completed_at": task.completed_at.isoformat() if task.completed_at else None
    }


@router.get("/{chapter_id}/analysis/status", summary="查询章节分析任务状态", response_model=AnalysisTaskStatusResponse)
async def get_analysis_task_status(
    chapter_id: str,
    request: Request,
    db: AsyncSession = Depends(get_db)
):
    """
    查询指定章节的最新分析任务状态
    
    自动恢复机制：
    - 如果任务状态为running且超过1分钟未更新，自动标记为failed
    - 如果任务状态为pending且超过2分钟未启动，自动标记为failed
    
    返回:
    - has_task: 是否存在分析任务
    - task_id: 任务ID（如果存在）
    - status: pending/running/completed/failed/none（如果不存在则为none）
    - progress: 0-100
    - error_message: 错误信息(如果失败)
    - auto_recovered: 是否被自动恢复
    - created_at: 创建时间
    - completed_at: 完成时间
    
    注意：当章节不存在或无权访问时返回404，当没有分析任务时返回has_task=false
    """
    from datetime import timedelta
    
    # 先获取章节以验证存在性和权限
    chapter_result = await db.execute(
        select(Chapter).where(Chapter.id == chapter_id)
    )
    chapter = chapter_result.scalar_one_or_none()
    
    if not chapter:
        raise ApiError(code="not_found.chapter")
    
    # 验证用户权限
    user_id = getattr(request.state, 'user_id', None)
    await verify_project_access(chapter.project_id, user_id, db)
    
    # 获取该章节最新的分析任务
    result = await db.execute(
        select(AnalysisTask)
        .where(AnalysisTask.chapter_id == chapter_id)
        .order_by(AnalysisTask.created_at.desc())
        .limit(1)
    )
    task = result.scalar_one_or_none()
    
    if not task:
        # 返回无任务状态，而不是抛出404错误
        return _build_analysis_task_status_payload(chapter_id, None)
    
    recovered_ids = await _recover_stale_analysis_tasks(db, [task])
    auto_recovered = task.id in recovered_ids
    
    return _build_analysis_task_status_payload(chapter_id, task, auto_recovered)


@router.post("/project/{project_id}/analysis/statuses", summary="批量查询章节分析任务状态", response_model=BatchAnalysisStatusResponse)
async def get_project_analysis_task_statuses(
    project_id: str,
    payload: BatchAnalysisStatusRequest,
    request: Request,
    db: AsyncSession = Depends(get_db)
):
    """批量查询项目章节分析状态，避免前端逐章节请求造成请求风暴"""
    # 验证用户权限
    user_id = getattr(request.state, 'user_id', None)
    await verify_project_access(project_id, user_id, db)

    # 先取项目章节列表
    chapter_query = select(Chapter.id).where(Chapter.project_id == project_id)
    if payload.chapter_ids and len(payload.chapter_ids) > 0:
        chapter_query = chapter_query.where(Chapter.id.in_(payload.chapter_ids))

    chapter_result = await db.execute(chapter_query)
    chapter_ids = [row[0] for row in chapter_result.all()]

    if not chapter_ids:
        return {
            "project_id": project_id,
            "total": 0,
            "items": {}
        }

    # 批量查询这些章节对应的所有分析任务，随后在内存中取最新一条
    tasks_result = await db.execute(
        select(AnalysisTask)
        .where(AnalysisTask.chapter_id.in_(chapter_ids))
        .order_by(AnalysisTask.chapter_id, AnalysisTask.created_at.desc())
    )
    all_tasks = tasks_result.scalars().all()

    latest_task_map: dict[str, AnalysisTask] = {}
    for task in all_tasks:
        if task.chapter_id not in latest_task_map:
            latest_task_map[task.chapter_id] = task

    recovered_ids = await _recover_stale_analysis_tasks(
        db, list(latest_task_map.values())
    )

    items: dict[str, dict] = {}
    for chapter_id in chapter_ids:
        task = latest_task_map.get(chapter_id)
        items[chapter_id] = _build_analysis_task_status_payload(
            chapter_id,
            task,
            task.id in recovered_ids if task else False,
        )

    return {
        "project_id": project_id,
        "total": len(chapter_ids),
        "items": items
    }


async def _run_batch_analysis_in_sequence(
    tasks_queue: list[dict[str, int | str]],
    user_id: str,
    project_id: str,
    ai_service: Optional[AIService] = None
) -> None:
    """按章节顺序逐个执行分析任务。"""
    for index, task_item in enumerate(tasks_queue, start=1):
        chapter_id = str(task_item["chapter_id"])
        chapter_number = int(task_item["chapter_number"])
        task_id = str(task_item["task_id"])

        logger.info(f"🔁 一键分析顺序执行中 [{index}/{len(tasks_queue)}]：第{chapter_number}章")
        try:
            success = await analyze_chapter_background(
                chapter_id=chapter_id,
                user_id=user_id,
                project_id=project_id,
                task_id=task_id,
                ai_service=ai_service
            )
            if not success:
                logger.warning(f"⚠️ 一键顺序分析返回失败: chapter_id={chapter_id}, task_id={task_id}")
        except Exception as e:
            # analyze_chapter_background 内部已处理任务失败状态，这里仅保护顺序队列不中断
            logger.error(
                f"❌ 一键顺序分析异常（已继续后续章节） chapter_id={chapter_id}, task_id={task_id}: {str(e)}",
                exc_info=True
            )


@router.post(
    "/project/{project_id}/analysis/analyze-unanalyzed",
    summary="一键按章节顺序分析未分析章节",
    response_model=BatchAnalyzeUnanalyzedResponse
)
async def batch_analyze_unanalyzed_chapters(
    project_id: str,
    payload: BatchAnalyzeUnanalyzedRequest,
    request: Request,
    db: AsyncSession = Depends(get_db)
):
    """自动识别项目中未完成分析的章节，并按章节顺序逐个启动分析。"""
    user_id = getattr(request.state, "user_id", None)
    if not user_id:
        raise ApiError(code="auth.unauthorized")

    # 验证项目权限
    await verify_project_access(project_id, user_id, db)

    # 查询目标章节（可选限制 chapter_ids）
    chapter_query = select(Chapter).where(Chapter.project_id == project_id).order_by(Chapter.chapter_number)
    if payload.chapter_ids and len(payload.chapter_ids) > 0:
        chapter_query = chapter_query.where(Chapter.id.in_(payload.chapter_ids))

    chapter_result = await db.execute(chapter_query)
    chapters = chapter_result.scalars().all()

    if not chapters:
        return {
            "project_id": project_id,
            "total_candidates": 0,
            "total_started": 0,
            "total_skipped_no_content": 0,
            "total_skipped_running": 0,
            "total_already_completed": 0,
            "started_tasks": {}
        }

    chapter_ids = [chapter.id for chapter in chapters]

    # 查询每个章节最新分析任务
    tasks_result = await db.execute(
        select(AnalysisTask)
        .where(AnalysisTask.chapter_id.in_(chapter_ids))
        .order_by(AnalysisTask.chapter_id, AnalysisTask.created_at.desc())
    )
    all_tasks = tasks_result.scalars().all()

    latest_task_map: dict[str, AnalysisTask] = {}
    for task in all_tasks:
        if task.chapter_id not in latest_task_map:
            latest_task_map[task.chapter_id] = task

    total_candidates = 0
    total_skipped_no_content = 0
    total_skipped_running = 0
    total_already_completed = 0
    started_tasks: dict[str, dict] = {}
    tasks_to_start: list[tuple[Chapter, AnalysisTask]] = []

    for chapter in chapters:
        # 无内容章节直接跳过
        if not chapter.content or chapter.content.strip() == "":
            total_skipped_no_content += 1
            continue

        total_candidates += 1
        latest_task = latest_task_map.get(chapter.id)

        # 已在队列/分析中，跳过
        if latest_task and latest_task.status in ("pending", "running"):
            total_skipped_running += 1
            continue

        # 已分析完成，跳过
        if latest_task and latest_task.status == "completed":
            total_already_completed += 1
            continue

        # 无任务/失败/未知状态，重新发起分析
        analysis_task = AnalysisTask(
            chapter_id=chapter.id,
            user_id=user_id,
            project_id=project_id,
            status='pending',
            progress=0
        )
        db.add(analysis_task)
        tasks_to_start.append((chapter, analysis_task))

    if tasks_to_start:
        try:
            await db.flush()

            for chapter, analysis_task in tasks_to_start:
                started_tasks[chapter.id] = _build_analysis_task_status_payload(chapter.id, analysis_task)

            await db.commit()
        except Exception as e:
            await db.rollback()
            logger.error(f"❌ 一键分析创建任务失败: {str(e)}", exc_info=True)
            detail = f"一键分析创建任务失败: {str(e)}"
            raise ApiError(code=DYNAMIC_DETAIL_CODE, detail=detail, status=500, raw=detail)

        # 提交后立即按章节顺序调度后台分析（逐章执行）
        tasks_queue = [
            {
                "chapter_id": chapter.id,
                "chapter_number": chapter.chapter_number,
                "task_id": analysis_task.id
            }
            for chapter, analysis_task in tasks_to_start
        ]
        _schedule_analysis_background(
            _run_batch_analysis_in_sequence(
                tasks_queue=tasks_queue,
                user_id=user_id,
                project_id=project_id
            )
        )

    return {
        "project_id": project_id,
        "total_candidates": total_candidates,
        "total_started": len(tasks_to_start),
        "total_skipped_no_content": total_skipped_no_content,
        "total_skipped_running": total_skipped_running,
        "total_already_completed": total_already_completed,
        "started_tasks": started_tasks
    }


@router.get("/{chapter_id}/analysis", summary="获取章节分析结果")
async def get_chapter_analysis(
    chapter_id: str,
    request: Request,
    db: AsyncSession = Depends(get_db)
):
    """
    获取章节的完整分析结果
    
    返回:
    - analysis_data: 完整的分析数据(JSON)
    - summary: 分析摘要文本
    - memories: 提取的记忆列表
    - created_at: 分析时间
    """
    # 先获取章节以验证权限
    chapter_result_check = await db.execute(
        select(Chapter).where(Chapter.id == chapter_id)
    )
    chapter_check = chapter_result_check.scalar_one_or_none()
    if chapter_check:
        # 验证用户权限
        user_id = getattr(request.state, 'user_id', None)
        await verify_project_access(chapter_check.project_id, user_id, db)
    
    # 获取分析结果
    analysis_result = await db.execute(
        select(PlotAnalysis)
        .where(PlotAnalysis.chapter_id == chapter_id)
        .order_by(PlotAnalysis.created_at.desc())
        .limit(1)
    )
    analysis = analysis_result.scalar_one_or_none()
    
    if not analysis:
        raise ApiError(code="not_found.chapter_analysis")
    
    # 获取相关记忆
    memories_result = await db.execute(
        select(StoryMemory)
        .where(StoryMemory.chapter_id == chapter_id)
        .order_by(StoryMemory.importance_score.desc())
    )
    memories = memories_result.scalars().all()
    
    return {
        "chapter_id": chapter_id,
        "analysis": analysis.to_dict(),  # 使用to_dict()方法
        "memories": [
            {
                "id": mem.id,
                "type": mem.memory_type,
                "title": mem.title,
                "content": mem.content,
                "importance": mem.importance_score,
                "tags": mem.tags,
                "is_foreshadow": mem.is_foreshadow,
                "position": mem.chapter_position,
                "related_characters": mem.related_characters
            }
            for mem in memories
        ],
        "created_at": analysis.created_at.isoformat() if analysis.created_at else None
    }


@router.get("/{chapter_id}/annotations", summary="获取章节标注数据")
async def get_chapter_annotations(
    chapter_id: str,
    request: Request,
    db: AsyncSession = Depends(get_db)
):
    """
    获取章节的标注数据（用于前端展示标注）
    
    返回格式化的标注列表，包含精确位置信息
    适用于章节内容的可视化标注展示
    """
    # 验证用户权限
    user_id = getattr(request.state, 'user_id', None)
    
    # 获取章节
    chapter_result = await db.execute(
        select(Chapter).where(Chapter.id == chapter_id)
    )
    chapter = chapter_result.scalar_one_or_none()
    
    if not chapter:
        raise ApiError(code="not_found.chapter")
    
    # 验证项目访问权限
    await verify_project_access(chapter.project_id, user_id, db)
    
    # 获取分析结果
    analysis_result = await db.execute(
        select(PlotAnalysis)
        .where(PlotAnalysis.chapter_id == chapter_id)
        .order_by(PlotAnalysis.created_at.desc())
        .limit(1)
    )
    analysis = analysis_result.scalar_one_or_none()
    
    # 获取记忆
    memories_result = await db.execute(
        select(StoryMemory)
        .where(StoryMemory.chapter_id == chapter_id)
        .order_by(StoryMemory.importance_score.desc())
    )
    memories = memories_result.scalars().all()
    
    # 构建标注数据
    annotations = []
    
    for mem in memories:
        # 优先从数据库读取位置信息
        position = mem.chapter_position if mem.chapter_position is not None else -1
        length = mem.text_length if hasattr(mem, 'text_length') and mem.text_length is not None else 0
        metadata_extra = {}
        
        # 如果数据库中没有位置信息，尝试从分析数据中重新计算
        if position == -1 and analysis and chapter.content:
            # 根据记忆类型从分析数据中查找对应项
            if mem.memory_type == 'hook' and analysis.hooks:
                for hook in analysis.hooks:
                    # 通过标题或内容匹配
                    if mem.title and hook.get('type') in mem.title:
                        keyword = hook.get('keyword', '')
                        if keyword:
                            pos = chapter.content.find(keyword)
                            if pos != -1:
                                position = pos
                                length = len(keyword)
                        metadata_extra["strength"] = hook.get('strength', 5)
                        metadata_extra["position_desc"] = hook.get('position', '')
                        break
            
            elif mem.memory_type == 'foreshadow' and analysis.foreshadows:
                for foreshadow in analysis.foreshadows:
                    if foreshadow.get('content') in mem.content:
                        keyword = foreshadow.get('keyword', '')
                        if keyword:
                            pos = chapter.content.find(keyword)
                            if pos != -1:
                                position = pos
                                length = len(keyword)
                        metadata_extra["foreshadow_type"] = foreshadow.get('type', 'planted')
                        metadata_extra["strength"] = foreshadow.get('strength', 5)
                        break
            
            elif mem.memory_type == 'plot_point' and analysis.plot_points:
                for plot_point in analysis.plot_points:
                    if plot_point.get('content') in mem.content:
                        keyword = plot_point.get('keyword', '')
                        if keyword:
                            pos = chapter.content.find(keyword)
                            if pos != -1:
                                position = pos
                                length = len(keyword)
                        break
        else:
            # 如果数据库有位置，也从分析数据中提取额外的元数据
            if analysis:
                if mem.memory_type == 'hook' and analysis.hooks:
                    for hook in analysis.hooks:
                        if mem.title and hook.get('type') in mem.title:
                            metadata_extra["strength"] = hook.get('strength', 5)
                            metadata_extra["position_desc"] = hook.get('position', '')
                            break
                
                elif mem.memory_type == 'foreshadow' and analysis.foreshadows:
                    for foreshadow in analysis.foreshadows:
                        if foreshadow.get('content') in mem.content:
                            metadata_extra["foreshadow_type"] = foreshadow.get('type', 'planted')
                            metadata_extra["strength"] = foreshadow.get('strength', 5)
                            break
        
        annotation = {
            "id": mem.id,
            "type": mem.memory_type,
            "title": mem.title,
            "content": mem.content,
            "importance": mem.importance_score or 0.5,
            "position": position,
            "length": length,
            "tags": mem.tags or [],
            "metadata": {
                "is_foreshadow": mem.is_foreshadow,
                "related_characters": mem.related_characters or [],
                "related_locations": mem.related_locations or [],
                **metadata_extra
            }
        }
        
        annotations.append(annotation)
    
    return {
        "chapter_id": chapter_id,
        "chapter_number": chapter.chapter_number,
        "title": chapter.title,
        "word_count": chapter.word_count or 0,
        "annotations": annotations,
        "has_analysis": analysis is not None,
        "summary": {
            "total_annotations": len(annotations),
            "hooks": len([a for a in annotations if a["type"] == "hook"]),
            "foreshadows": len([a for a in annotations if a["type"] == "foreshadow"]),
            "plot_points": len([a for a in annotations if a["type"] == "plot_point"]),
            "character_events": len([a for a in annotations if a["type"] == "character_event"])
        }
    }


@router.post("/{chapter_id}/analyze", summary="手动触发章节分析")
async def trigger_chapter_analysis(
    chapter_id: str,
    request: Request,
    background_tasks: BackgroundTasks,
    db: AsyncSession = Depends(get_db)
):
    """
    手动触发章节分析(用于重新分析或分析旧章节)
    """
    # 从请求中获取用户ID
    user_id = getattr(request.state, "user_id", None)
    if not user_id:
        raise ApiError(code="auth.unauthorized")
    
    # 验证章节存在
    chapter_result = await db.execute(
        select(Chapter).where(Chapter.id == chapter_id)
    )
    chapter = chapter_result.scalar_one_or_none()
    
    if not chapter:
        raise ApiError(code="not_found.chapter")
    
    if not chapter.content or chapter.content.strip() == "":
        raise ApiError(code="validation.chapter_content_empty_for_analysis")
    
    # 获取项目信息
    project_result = await db.execute(
        select(Project).where(Project.id == chapter.project_id)
    )
    project = project_result.scalar_one_or_none()
    
    if not project:
        raise ApiError(code="not_found.project")

    # 避免重复点击或状态轮询误判后创建并发分析任务。
    existing_task_result = await db.execute(
        select(AnalysisTask)
        .where(AnalysisTask.chapter_id == chapter_id)
        .order_by(AnalysisTask.created_at.desc())
        .limit(1)
    )
    existing_task = existing_task_result.scalar_one_or_none()
    if existing_task and existing_task.status in ("pending", "running"):
        return {
            "task_id": existing_task.id,
            "chapter_id": chapter_id,
            "status": existing_task.status,
            "message": "已有分析任务正在执行"
        }
    
    # 创建分析任务
    analysis_task = AnalysisTask(
        chapter_id=chapter_id,
        user_id=user_id,
        project_id=project.id,
        status='pending',
        progress=0
    )
    db.add(analysis_task)
    await db.commit()
    
    task_id = analysis_task.id
    logger.info(f"📋 创建分析任务: {task_id}, 章节: {chapter_id}")
    
    # 刷新数据库会话，确保其他会话可以看到新任务
    await db.refresh(analysis_task)
    
    # 短暂延迟确保SQLite WAL完成写入（让其他会话可见）
    await asyncio.sleep(3)
    
    # 直接启动后台分析（并发执行）
    background_tasks.add_task(
        analyze_chapter_background,
        chapter_id=chapter_id,
        user_id=user_id,
        project_id=project.id,
        task_id=task_id
    )
    
    return {
        "task_id": task_id,
        "chapter_id": chapter_id,
        "status": "pending",
        "message": "分析任务已创建并开始执行"
    }



def calculate_estimated_time(
    chapter_count: int,
    target_word_count: int,
    enable_analysis: bool
) -> int:
    """
    计算预估耗时（分钟）
    
    基准：
    - 生成3000字约需2分钟
    - 分析约需1分钟
    """
    generation_time_per_chapter = (target_word_count / 3000) * 2
    analysis_time_per_chapter = 1 if enable_analysis else 0
    
    total_time = chapter_count * (generation_time_per_chapter + analysis_time_per_chapter)
    
    return max(1, int(total_time))


@router.post("/project/{project_id}/batch-generate", response_model=BatchGenerateResponse, summary="批量顺序生成章节内容")
async def batch_generate_chapters_in_order(
    project_id: str,
    batch_request: BatchGenerateRequest,
    request: Request,
    db: AsyncSession = Depends(get_db),
    user_ai_service: AIService = Depends(get_user_ai_service)
):
    """
    从指定章节开始，按顺序批量生成指定数量的章节
    
    特性：
    1. 严格按章节序号顺序生成（不可跳过）
    2. 自动检测起始章节是否可生成
    3. 可选同步分析（影响耗时和质量）
    4. 单章失败不中断整批（失败明细进 failed_chapters，结束汇总）；
       但如果后续章节的阻塞根因是「本批已失败的章节」（前置内容或前置分析），
       则记一条级联记录后结束整批，不再逐章退避重试
    """
    user_id = getattr(request.state, "user_id", None)
    if not user_id:
        raise ApiError(code="auth.unauthorized")
    
    # 验证项目存在和用户权限
    project = await verify_project_access(project_id, user_id, db)
    
    # 获取项目的所有章节，按序号排序
    result = await db.execute(
        select(Chapter)
        .where(Chapter.project_id == project_id)
        .order_by(Chapter.chapter_number)
    )
    all_chapters = result.scalars().all()
    
    if not all_chapters:
        raise ApiError(code="not_found.project_chapters", detail="项目没有章节")
    
    # 计算要生成的章节范围
    start_number = batch_request.start_chapter_number
    end_number = start_number + batch_request.count - 1
    
    # 筛选出要生成的章节
    chapters_to_generate = [
        ch for ch in all_chapters
        if start_number <= ch.chapter_number <= end_number
    ]
    
    if not chapters_to_generate:
        raise ApiError(code="not_found.project_chapters", detail="指定范围内没有章节")
    
    # 验证起始章节的前置条件
    first_chapter = chapters_to_generate[0]
    can_generate, error_msg, _ = await check_prerequisites(db, first_chapter)
    if not can_generate:
        detail = f"起始章节无法生成：{error_msg}"
        raise ApiError(code=DYNAMIC_DETAIL_CODE, detail=detail, status=400, raw=detail)

    # 批量生成必须同步分析，否则下一章无法获得最新角色状态、记忆和伏笔上下文。
    enable_analysis = True
    
    # 创建批量生成任务
    batch_task = BatchGenerationTask(
        project_id=project_id,
        user_id=user_id,
        start_chapter_number=start_number,
        chapter_count=len(chapters_to_generate),
        chapter_ids=[ch.id for ch in chapters_to_generate],
        style_id=batch_request.style_id,
        target_word_count=batch_request.target_word_count,
        enable_analysis=enable_analysis,
        max_retries=batch_request.max_retries,
        status='pending',
        total_chapters=len(chapters_to_generate),
        completed_chapters=0,
        failed_chapters=[],
        current_retry_count=0,
        anchor_settings={
            key: value
            for key, value in {
                "enabled": batch_request.style_anchor_enabled,
                "chars": batch_request.style_anchor_chars,
                "count": batch_request.style_anchor_count,
            }.items()
            if value is not None
        } or None,
    )
    db.add(batch_task)
    await db.commit()
    await db.refresh(batch_task)
    
    batch_id = batch_task.id
    
    # 计算预估耗时
    estimated_time = calculate_estimated_time(
        chapter_count=len(chapters_to_generate),
        target_word_count=batch_request.target_word_count,
        enable_analysis=enable_analysis
    )
    
    logger.info(f"📦 创建批量生成任务: {batch_id}, 章节: 第{start_number}-{end_number}章, 预估耗时: {estimated_time}分钟")
    
    # 启动后台批量生成任务，传递model参数和skill_key
    # 用持有引用的 create_task 替代 BackgroundTasks：后者在进程被杀时会连同
    # 正在执行的批量协程一起丢失且无任何清理（done-callback 负责兜底置 failed）
    _spawn_batch_generation(
        execute_batch_generation_in_order(
            batch_id=batch_id,
            user_id=user_id,
            ai_service=user_ai_service,
            custom_model=batch_request.model,
            skill_key=batch_request.skill_key,
            enable_mcp=batch_request.enable_mcp,
            narrative_perspective=batch_request.narrative_perspective,
            content_language=batch_request.content_language
        ),
        batch_id=batch_id,
        user_id=user_id,
    )
    
    return BatchGenerateResponse(
        batch_id=batch_id,
        message=f"批量生成任务已创建，将生成 {len(chapters_to_generate)} 个章节",
        chapters_to_generate=[
            {
                "id": ch.id,
                "chapter_number": ch.chapter_number,
                "title": ch.title
            }
            for ch in chapters_to_generate
        ],
        estimated_time_minutes=estimated_time
    )


@router.get("/batch-generate/{batch_id}/status", response_model=BatchGenerateStatusResponse, summary="查询批量生成任务状态")
async def get_batch_generation_status(
    batch_id: str,
    request: Request,
    db: AsyncSession = Depends(get_db)
):
    """查询批量生成任务的状态和进度"""
    user_id = getattr(request.state, 'user_id', None)
    if not user_id:
        raise ApiError(code="auth.unauthorized")

    result = await db.execute(
        select(BatchGenerationTask).where(
            BatchGenerationTask.id == batch_id,
            BatchGenerationTask.user_id == user_id
        )
    )
    task = result.scalar_one_or_none()
    
    if not task:
        raise ApiError(code="not_found.batch_task")

    # worker 被重启后协程已消失：按心跳把「假 running」恢复为 failed 再返回
    await _recover_stale_batch_tasks(db, [task])

    return BatchGenerateStatusResponse(
        batch_id=task.id,
        status=task.status,
        total=task.total_chapters,
        completed=task.completed_chapters,
        current_chapter_id=task.current_chapter_id,
        current_chapter_number=task.current_chapter_number,
        current_retry_count=task.current_retry_count,
        max_retries=task.max_retries,
        failed_chapters=task.failed_chapters or [],
        style_check=task.style_check,
        created_at=task.created_at.isoformat() if task.created_at else None,
        started_at=task.started_at.isoformat() if task.started_at else None,
        completed_at=task.completed_at.isoformat() if task.completed_at else None,
        error_message=task.error_message
    )


@router.get("/project/{project_id}/batch-generate/active", summary="获取项目当前运行中的批量生成任务")
async def get_active_batch_generation(
    project_id: str,
    request: Request,
    db: AsyncSession = Depends(get_db)
):
    """
    获取项目当前运行中的批量生成任务
    用于页面刷新后恢复任务状态
    """
    # 验证用户权限
    user_id = getattr(request.state, 'user_id', None)
    if not user_id:
        raise ApiError(code="auth.unauthorized")
    await verify_project_access(project_id, user_id, db)
    
    result = await db.execute(
        select(BatchGenerationTask)
        .where(BatchGenerationTask.project_id == project_id)
        .where(BatchGenerationTask.user_id == user_id)
        .where(BatchGenerationTask.status.in_(['pending', 'running']))
        .order_by(BatchGenerationTask.created_at.desc())
    )
    tasks = list(result.scalars().all())

    # 先按心跳回收陈旧任务，再挑仍活跃的那条（否则会返回假 running）
    await _recover_stale_batch_tasks(db, tasks)
    task = next(
        (item for item in tasks if item.status in ('pending', 'running')),
        None,
    )
    
    if not task:
        return {
            "has_active_task": False,
            "task": None
        }
    
    return {
        "has_active_task": True,
        "task": {
            "batch_id": task.id,
            "status": task.status,
            "total": task.total_chapters,
            "completed": task.completed_chapters,
            "current_chapter_id": task.current_chapter_id,
            "current_chapter_number": task.current_chapter_number,
            "created_at": task.created_at.isoformat() if task.created_at else None,
            "started_at": task.started_at.isoformat() if task.started_at else None
        }
    }


@router.post("/batch-generate/{batch_id}/cancel", summary="取消批量生成任务")
async def cancel_batch_generation(
    batch_id: str,
    request: Request,
    db: AsyncSession = Depends(get_db)
):
    """取消正在进行的批量生成任务"""
    user_id = getattr(request.state, 'user_id', None)
    if not user_id:
        raise ApiError(code="auth.unauthorized")

    result = await db.execute(
        select(BatchGenerationTask).where(
            BatchGenerationTask.id == batch_id,
            BatchGenerationTask.user_id == user_id
        )
    )
    task = result.scalar_one_or_none()
    
    if not task:
        raise ApiError(code="not_found.batch_task")

    # 先回收心跳超时的「假 running」：否则会 400 invalid status 且取消不到
    await _recover_stale_batch_tasks(db, [task])

    if task.status in ['completed', 'failed', 'cancelled']:
        raise ApiError(
            code="task.cancel_invalid_status",
            detail=f"任务已处于 {task.status} 状态，无法取消",
            params={"status": task.status},
        )
    
    task.status = 'cancelled'
    task.completed_at = datetime.now()
    await db.commit()
    
    logger.info(f"🛑 批量生成任务已取消: {batch_id}")
    
    return {
        "message": "批量生成任务已取消",
        "batch_id": batch_id,
        "completed_chapters": task.completed_chapters,
        "total_chapters": task.total_chapters
    }


async def _touch_batch_heartbeat(db_session: AsyncSession, batch_id: str) -> None:
    """即时刷新心跳（每章开始 / 每次预算重试前）；仅任务仍 running 时生效。"""
    await db_session.execute(
        update(BatchGenerationTask)
        .where(
            BatchGenerationTask.id == batch_id,
            BatchGenerationTask.status == "running",
        )
        .values(heartbeat_at=datetime.now())
        .execution_options(synchronize_session=False)
    )
    await db_session.commit()


async def _update_batch_status_if_running(
    db_session: AsyncSession,
    batch_id: str,
    *,
    status: str,
    error_message: Optional[str] = None,
    clear_current: bool = False,
) -> int:
    """条件落终态：仅当任务仍 running 时更新，返回 rowcount。

    与陈旧回收 / done-callback 共用同一 WHERE status='running' 幂等条件：
    外部已抢先落终态时 rowcount=0，调用方据此跳过，不覆盖他人的结果。
    """
    values: Dict[str, Any] = {
        "status": status,
        "completed_at": datetime.now(),
    }
    if error_message is not None:
        values["error_message"] = error_message
    if clear_current:
        values["current_chapter_id"] = None
        values["current_chapter_number"] = None

    result = await db_session.execute(
        update(BatchGenerationTask)
        .where(
            BatchGenerationTask.id == batch_id,
            BatchGenerationTask.status == "running",
        )
        .values(**values)
        .execution_options(synchronize_session=False)
    )
    await db_session.commit()
    return result.rowcount


async def execute_batch_generation_in_order(
    batch_id: str,
    user_id: str,
    ai_service: AIService,
    custom_model: Optional[str] = None,
    skill_key: Optional[str] = None,
    enable_mcp: bool = True,
    narrative_perspective: Optional[str] = None,
    content_language: Optional[str] = None
):
    """
    按顺序执行批量生成任务（后台任务）
    - 严格按章节序号顺序
    - 单章失败**不中断**整批：失败明细写入 `failed_chapters`，继续下一章，
      结束时若存在失败章节则任务状态为 failed 并在 error_message 汇总
    - 截断类错误外层最多重试 1 次；升级预算后正文仍为 0 的失败不可重试
    - 可选同步分析
    """
    db_session = None
    task = None
    write_lock = await get_db_write_lock(user_id)
    
    try:
        logger.info(f"📦 开始执行顺序批量生成任务: {batch_id}")
        
        # 创建独立数据库会话
        from app.database import get_engine
        from sqlalchemy.ext.asyncio import async_sessionmaker, AsyncSession
        
        engine = await get_engine(user_id)
        AsyncSessionLocal = async_sessionmaker(
            engine,
            class_=AsyncSession,
            expire_on_commit=False
        )
        db_session = AsyncSessionLocal()
        
        # 获取任务
        task_result = await db_session.execute(
            select(BatchGenerationTask).where(BatchGenerationTask.id == batch_id)
        )
        task = task_result.scalar_one_or_none()
        
        if not task:
            logger.error(f"❌ 批量生成任务不存在: {batch_id}")
            return

        if task.status in ('cancelled', 'failed'):
            logger.warning(f"批量任务已被外部标记为 {task.status}，协程退出: {batch_id}")
            return
        
        # 更新任务状态为运行中
        async with write_lock:
            await db_session.refresh(task)
            if task.status in ('cancelled', 'failed'):
                logger.warning(f"批量任务已被外部标记为 {task.status}，协程退出: {batch_id}")
                return
            task.status = 'running'
            task.started_at = datetime.now()
            await db_session.commit()
        
        # 维护上一章的摘要，用于传递给下一章（防重复上下文）
        last_generated_summary = None

        # 按顺序生成每个章节
        for idx, chapter_id in enumerate(task.chapter_ids, 1):
            # 存活检查：cancelled 是用户取消；failed 可能是陈旧回收/兜底先落了终态
            await db_session.refresh(task)
            if task.status in ('cancelled', 'failed'):
                logger.warning(f"批量任务已被外部标记为 {task.status}，协程退出: {batch_id}")
                return

            # 每章开始写一次心跳：worker 被杀后由陈旧回收据此判定中断
            async with write_lock:
                await _touch_batch_heartbeat(db_session, batch_id)
            
            # 更新当前章节
            async with write_lock:
                task.current_chapter_id = chapter_id
                task.current_retry_count = 0  # 重置重试计数
                await db_session.commit()
            
            # 重试循环
            retry_count = 0
            chapter_success = False
            chapter = None
            last_error = None
            cascade_stop = False
            
            while retry_count <= task.max_retries and not chapter_success:
                try:
                    await db_session.refresh(task)
                    if task.status in ('cancelled', 'failed'):
                        logger.warning(f"批量任务已被外部标记为 {task.status}，协程退出: {batch_id}")
                        return

                    # 获取章节信息
                    chapter_result = await db_session.execute(
                        select(Chapter).where(Chapter.id == chapter_id)
                    )
                    chapter = chapter_result.scalar_one_or_none()
                    
                    if not chapter:
                        raise Exception(f"章节 {chapter_id} 不存在")
                    
                    # 更新当前章节序号和重试次数
                    async with write_lock:
                        task.current_chapter_number = chapter.chapter_number
                        task.current_retry_count = retry_count
                        await db_session.commit()
                    
                    if retry_count > 0:
                        logger.info(f"🔄 [{idx}/{task.total_chapters}] 重试生成章节 (第{retry_count}次): 第{chapter.chapter_number}章 《{chapter.title}》")
                    else:
                        logger.info(f"📝 [{idx}/{task.total_chapters}] 开始生成章节: 第{chapter.chapter_number}章 《{chapter.title}》")
                    
                    # 检查前置条件（每次都检查，确保顺序性）
                    can_generate, error_msg, _ = await check_prerequisites(db_session, chapter)
                    if not can_generate:
                        # 顺序依赖下，根因章在本批已失败时余章不可能成功：
                        # 不再逐章烧满退避重试，记一条级联记录后结束整批。
                        missing_numbers = _extract_chapter_numbers(error_msg)
                        failed_numbers = {
                            int(item.get("chapter_number"))
                            for item in (task.failed_chapters or [])
                            if isinstance(item.get("chapter_number"), int)
                        }
                        blocking = sorted(missing_numbers & failed_numbers)
                        if blocking:
                            async with write_lock:
                                task.failed_chapters = list(task.failed_chapters or []) + [
                                    {
                                        "chapter_id": chapter_id,
                                        "chapter_number": chapter.chapter_number,
                                        "title": chapter.title,
                                        "error": (
                                            f"前置章节第{blocking[0]}章失败，"
                                            "后续章节批量跳过"
                                        ),
                                        "retry_count": 0,
                                    }
                                ]
                                task.current_retry_count = 0
                                await db_session.commit()
                            logger.error(
                                f"🛑 第{chapter.chapter_number}章前置章节"
                                f"（第{blocking[0]}章）已失败，结束本批（不再逐章重试）"
                            )
                            cascade_stop = True
                            break
                        raise Exception(f"前置条件不满足: {error_msg}")
                    analysis_ready, analysis_msg = await check_previous_analysis_ready(db_session, chapter)
                    if not analysis_ready:
                        # 与前置内容阻塞同构：根因章已在本批失败时不再逐章退避
                        missing_numbers = _extract_chapter_numbers(analysis_msg)
                        failed_numbers = {
                            int(item.get("chapter_number"))
                            for item in (task.failed_chapters or [])
                            if isinstance(item.get("chapter_number"), int)
                        }
                        blocking = sorted(missing_numbers & failed_numbers)
                        if blocking:
                            async with write_lock:
                                task.failed_chapters = list(task.failed_chapters or []) + [
                                    {
                                        "chapter_id": chapter_id,
                                        "chapter_number": chapter.chapter_number,
                                        "title": chapter.title,
                                        "error": (
                                            f"前置章节第{blocking[0]}章失败，"
                                            "后续章节批量跳过"
                                        ),
                                        "retry_count": 0,
                                    }
                                ]
                                task.current_retry_count = 0
                                await db_session.commit()
                            logger.error(
                                f"🛑 第{chapter.chapter_number}章前置章节"
                                f"（第{blocking[0]}章）未完成，结束本批（不再逐章重试）"
                            )
                            cascade_stop = True
                            break
                        raise Exception(analysis_msg)
                    
                    # 生成章节内容（复用现有流式生成逻辑的核心部分），传递model参数
                    # 并获取生成后的摘要（如果生成函数支持返回）
                    chapter_generation = await generate_single_chapter_for_batch(
                        db_session=db_session,
                        chapter=chapter,
                        user_id=user_id,
                        style_id=task.style_id,
                        target_word_count=task.target_word_count,
                        ai_service=ai_service,
                        write_lock=write_lock,
                        custom_model=custom_model,
                        previous_summary_context=last_generated_summary,
                        skill_key=skill_key,
                        batch_id=batch_id,
                        enable_mcp=enable_mcp,
                        temp_narrative_perspective=narrative_perspective,
                        content_language=content_language,
                        anchor_override=task.anchor_settings,
                    ) or {}

                    # issue #254 P2：批量生成后的文笔一致性自检结果
                    generated_summary = chapter_generation.get("summary")
                    batch_style_check = chapter_generation.get("style_check") or {}
                    if batch_style_check.get("alerts"):
                        logger.warning(
                            f"🎨 批量生成 - 第{chapter.chapter_number}章文笔一致性告警 "
                            f"{len(batch_style_check['alerts'])} 项"
                        )
                    await db_session.refresh(task)
                    if batch_style_check:
                        # 必须在 refresh 之后赋值：refresh 会用 DB 值覆盖未提交的
                        # in-memory 变更（issue #254 审核 BLOCKER）。
                        task.style_check = batch_style_check
                    if task.status in ('cancelled', 'failed'):
                        logger.warning(f"批量任务已被外部标记为 {task.status}，协程退出: {batch_id}")
                        return
                    
                    # 更新上一章摘要，供下一章使用
                    if generated_summary:
                        last_generated_summary = f"第{chapter.chapter_number}章《{chapter.title}》：{generated_summary}"
                        logger.info(f"📝 已更新上一章摘要上下文: {last_generated_summary[:50]}...")
                    
                    logger.info(f"✅ 章节生成完成: 第{chapter.chapter_number}章")
                    
                    # 如果启用同步分析
                    if task.enable_analysis:
                        await db_session.refresh(task)
                        if task.status in ('cancelled', 'failed'):
                            logger.warning(f"批量任务已被外部标记为 {task.status}，协程退出（跳过同步分析）: {batch_id}")
                            return
                        logger.info(f"🔍 开始同步分析章节: 第{chapter.chapter_number}章")

                        async with write_lock:
                            analysis_task = AnalysisTask(
                                chapter_id=chapter_id,
                                user_id=user_id,
                                project_id=task.project_id,
                                status='pending',
                                progress=0
                            )
                            db_session.add(analysis_task)
                            await db_session.commit()
                            await db_session.refresh(analysis_task)

                        analysis_result = await analyze_chapter_background(
                            chapter_id=chapter_id,
                            user_id=user_id,
                            project_id=task.project_id,
                            task_id=analysis_task.id
                        )

                        if not analysis_result:
                            error_message = "分析函数返回失败"
                            logger.error(
                                f"❌ 章节分析失败，记录后继续下一章: 第{chapter.chapter_number}章"
                            )

                            failed_info = {
                                'chapter_id': chapter_id,
                                'chapter_number': chapter.chapter_number,
                                'title': chapter.title,
                                'error': f"分析失败: {error_message}",
                                'retry_count': 1
                            }

                            async with write_lock:
                                # 整表赋值而不是原地 append：JSON 列没有 MutableList，
                                # 原地修改不会被 ORM 记为脏数据（失败明细会静默丢失）。
                                task.failed_chapters = list(task.failed_chapters or []) + [failed_info]
                                task.current_retry_count = 0
                                await db_session.commit()

                            # 单章分析失败不再中断整批（批量任务按章独立结算），
                            # 失败明细进 failed_chapters，最终汇总时统一报告；
                            # 若后续章被该失败章阻塞，外层会级联快停。
                            break

                        logger.info(f"✅ 章节分析成功: 第{chapter.chapter_number}章")
                    
                    # 标记成功
                    chapter_success = True
                    
                    # 更新完成数
                    async with write_lock:
                        task.completed_chapters += 1
                        task.current_retry_count = 0  # 重置重试计数
                        await db_session.commit()
                    
                    logger.info(f"✅ 进度: {task.completed_chapters}/{task.total_chapters}")
                    
                except Exception as e:
                    last_error = str(e)
                    await db_session.refresh(task)
                    if task.status in ('cancelled', 'failed'):
                        logger.warning(f"批量任务已被外部标记为 {task.status}，协程退出: {batch_id}")
                        return
                    error_msg = f"第{chapter.chapter_number if chapter else '?'}章出错: {last_error}"
                    logger.error(f"❌ {error_msg}")
                    
                    retry_count += 1
                    # 重试额度按异常类型收敛：不可重试 → 0；截断类 → 1；其它 → max_retries。
                    retry_allowance = _retry_allowance(e, task.max_retries)
                    
                    # 如果还有重试机会，等待一小段时间后重试
                    if retry_count <= retry_allowance:
                        wait_time = min(2 ** retry_count, 10)  # 指数退避，最多等待10秒
                        logger.info(f"⏳ 等待 {wait_time} 秒后重试...")
                        await asyncio.sleep(wait_time)
                    else:
                        # 达到该异常类型的重试上限，记录失败信息
                        logger.error(f"❌ 章节生成失败，已达重试上限({retry_allowance}): 第{chapter.chapter_number if chapter else '?'}章")
                        
                        failed_info = {
                            'chapter_id': chapter_id,
                            'chapter_number': chapter.chapter_number if chapter else -1,
                            'title': chapter.title if chapter else '未知',
                            'error': last_error,
                            'retry_count': retry_count - 1
                        }
                        
                        async with write_lock:
                            # JSON 列无 MutableList，必须整表赋值才会被持久化
                            task.failed_chapters = list(task.failed_chapters or []) + [failed_info]
                            task.current_retry_count = 0
                            await db_session.commit()

                        # 单章失败只记账，不中断整批：后续章节继续生成，最终汇总统一报告。
                        logger.error(
                            f"🛑 第{chapter.chapter_number if chapter else '?'}章失败，"
                            f"记录后继续下一章（原因: {last_error}）"
                        )
                        # 必须显式退出「单章重试 while」：否则控制流回到
                        # `while retry_count <= task.max_retries and not chapter_success`
                        # （两者都仍成立）→ 该章被反复重试、failed_chapters 重复记账，
                        # allowance=0 的 NonRetryable 也就形同虚设。
                        break

            # 前置章节已失败导致本批无法继续：结束整批，不再逐章重试
            if cascade_stop:
                break
        
        # 全部完成（单章失败不中断整批；被失败根因阻塞时级联快停；
        # 最终状态必须如实反映失败章节）
        async with write_lock:
            await db_session.refresh(task)
            if task.status in ('cancelled', 'failed'):
                # 终态已被用户取消 / 陈旧回收抢先写入：绝不覆盖
                logger.warning(f"批量任务已被外部标记为 {task.status}，跳过终态写入: {batch_id}")
                return
            failures = task.failed_chapters or []
            terminal_status = 'completed'
            error_message: Optional[str] = None
            if failures:
                detail = "；".join(
                    f"第{f.get('chapter_number')}章《{f.get('title')}》{f.get('error')}"
                    for f in failures
                )
                terminal_status = 'failed'
                error_message = (
                    f"批量生成结束：成功 {task.completed_chapters}/{task.total_chapters} 章，"
                    f"失败 {len(failures)} 章。{detail}"
                )[:500]
            # 条件更新（rowcount=0 ⇒ 别的路径已抢先落终态），避免无条件覆写
            updated = await _update_batch_status_if_running(
                db_session,
                batch_id,
                status=terminal_status,
                error_message=error_message,
                clear_current=True,
            )
            if not updated:
                logger.warning(f"⚠️ 批量生成终态已被其他路径写入，跳过覆盖: {batch_id}")
                return

        await db_session.refresh(task)
        if terminal_status == 'failed':
            logger.error(f"⚠️ 批量生成结束但有失败章节: {task.error_message}")

        logger.info(
            f"✅ 批量生成任务结束: {batch_id}, 成功生成 {task.completed_chapters} 章, "
            f"失败 {len(task.failed_chapters or [])} 章"
        )
        
    except Exception as e:
        logger.error(f"❌ 批量生成任务异常: {str(e)}", exc_info=True)
        if db_session and task:
            try:
                async with write_lock:
                    updated = await _update_batch_status_if_running(
                        db_session,
                        batch_id,
                        status='failed',
                        error_message=str(e)[:500],
                        clear_current=True,
                    )
                    if not updated:
                        logger.warning(
                            f"⚠️ 批量任务终态已被其他路径写入，跳过异常态覆盖: {batch_id}"
                        )
            except Exception as commit_error:
                logger.error(f"❌ 更新任务失败状态失败: {str(commit_error)}")
    finally:
        if db_session:
            await db_session.close()


async def generate_single_chapter_for_batch(
    db_session: AsyncSession,
    chapter: Chapter,
    user_id: str,
    style_id: Optional[int],
    target_word_count: int,
    ai_service: AIService,
    write_lock: Lock,
    custom_model: Optional[str] = None,
    previous_summary_context: Optional[str] = None,
    skill_key: Optional[str] = None,
    batch_id: Optional[str] = None,
    enable_mcp: bool = True,
    temp_narrative_perspective: Optional[str] = None,
    content_language: Optional[str] = None,
    anchor_override: Optional[dict] = None,
) -> dict:
    """
    为批量生成执行单个章节的生成（非流式）
    复用现有生成逻辑的核心部分
    
    Returns:
        {"summary": 生成章节的摘要（前200字）, "style_check": 文笔一致性自检结果}
    """
    # 获取项目信息
    project_result = await db_session.execute(
        select(Project).where(Project.id == chapter.project_id)
    )
    project = project_result.scalar_one_or_none()
    if not project:
        raise Exception("项目不存在")
    
    # 获取项目的大纲模式
    outline_mode = project.outline_mode if project else 'one-to-many'
    logger.info(f"📋 批量生成 - 项目大纲模式: {outline_mode}")
    
    # 获取对应的大纲（优先使用 chapter.outline_id 直接关联）
    if chapter.outline_id:
        outline_result = await db_session.execute(
            select(Outline).where(Outline.id == chapter.outline_id)
        )
    else:
        # 回退到按序号查找
        outline_result = await db_session.execute(
            select(Outline)
            .where(Outline.project_id == chapter.project_id)
            .where(Outline.order_index == chapter.chapter_number)
        )
    outline = outline_result.scalar_one_or_none()
    
    # 获取写作风格（§13-3：未显式选择普通风格时按目标章节选卷）
    style_content = await _resolve_project_style_content(
        db_session,
        project,
        chapter.chapter_number,
        style_id,
        user_id,
    )

    # issue #254 P1：批量生成路径注入本书原文语感锚点（批量任务无 per-gen 覆盖，沿用项目设置）
    style_content = await append_project_style_anchors(
        db_session,
        project=project,
        style_content=style_content,
        before_chapter_number=chapter.chapter_number,
        override=anchor_override,
    )
    
    # 🚀 根据大纲模式选择独立的上下文构建器（批量生成）
    if outline_mode == 'one-to-one':
        # 1-1模式
        logger.info(f"🔧 批量生成 - [1-1模式] 使用 OneToOneContextBuilder")
        context_builder = OneToOneContextBuilder(
            memory_service=memory_service,
            foreshadow_service=foreshadow_service,
            full_book_budget_chars=await _resolve_full_book_budget(
                ai_service,
                custom_model or getattr(ai_service, "default_model", None),
            ),
        )
        chapter_context = await context_builder.build(
            chapter=chapter,
            project=project,
            outline=outline,
            user_id=user_id,
            db=db_session,
            target_word_count=target_word_count
        )
    else:
        # 1-N模式：使用独立的完整构建器
        logger.info(f"🔧 批量生成 - [1-N模式] 使用 OneToManyContextBuilder")
        context_builder = OneToManyContextBuilder(
            memory_service=memory_service,
            foreshadow_service=foreshadow_service,
            full_book_budget_chars=await _resolve_full_book_budget(
                ai_service,
                custom_model or getattr(ai_service, "default_model", None),
            ),
        )
        chapter_context = await context_builder.build(
            chapter=chapter,
            project=project,
            outline=outline,
            user_id=user_id,
            db=db_session,
            style_content=style_content,
            target_word_count=target_word_count,
            temp_narrative_perspective=temp_narrative_perspective
        )
    
    # 日志输出统计信息
    logger.info(f"📊 批量生成 - 优化上下文统计:")
    logger.info(f"  - 章节序号: {chapter.chapter_number}")
    logger.info(f"  - 衔接锚点长度: {len(chapter_context.continuation_point or '')} 字符")
    logger.info(f"  - 相关记忆: {chapter_context.context_stats.get('memory_count', 0)} 条")
    logger.info(f"  - 总上下文长度: {chapter_context.context_stats.get('total_length', 0)} 字符")
    
    # 🚀 根据大纲模式选择提示词模板（批量生成）
    # 统一使用 context_builder 构建的 chapter_context 结果，与单章生成保持一致
    # 解析最终生成语言：per-gen override > 用户偏好 > UI 语言 > zh（todo 17）
    generation_language = await resolve_user_generation_language(
        db_session, user_id, content_language
    )
    if outline_mode == 'one-to-one':
        # 1-1模式
        if chapter_context.continuation_point:
            # 有上一章内容
            template = await PromptService.get_template("CHAPTER_GENERATION_ONE_TO_ONE_NEXT", user_id, db_session)
            base_prompt = PromptService.format_prompt(
                template,
                project_title=project.title,
                chapter_number=chapter.chapter_number,
                chapter_title=chapter.title,
                chapter_outline=chapter_context.chapter_outline,
                target_word_count=target_word_count,
                genre=project.genre or '未设定',
                narrative_perspective=temp_narrative_perspective or project.narrative_perspective or '第三人称',
                previous_chapter_content=chapter_context.continuation_point,
                characters_info=chapter_context.chapter_characters or '暂无角色信息',
                chapter_careers=chapter_context.chapter_careers or '暂无职业信息',
                foreshadow_reminders=chapter_context.foreshadow_reminders or '暂无需要关注的伏笔',
                relevant_memories=chapter_context.relevant_memories or '暂无相关记忆',
                previous_chapter_summary=chapter_context.previous_chapter_summary or '',
                recent_chapters_context=chapter_context.recent_chapters_context or '暂无最近章节摘要',
                content_language=generation_language
            )
        else:
            # 第一章
            template = await PromptService.get_template("CHAPTER_GENERATION_ONE_TO_ONE", user_id, db_session)
            base_prompt = PromptService.format_prompt(
                template,
                project_title=project.title,
                chapter_number=chapter.chapter_number,
                chapter_title=chapter.title,
                chapter_outline=chapter_context.chapter_outline,
                target_word_count=target_word_count,
                genre=project.genre or '未设定',
                narrative_perspective=temp_narrative_perspective or project.narrative_perspective or '第三人称',
                characters_info=chapter_context.chapter_characters or '暂无角色信息',
                chapter_careers=chapter_context.chapter_careers or '暂无职业信息',
                foreshadow_reminders=chapter_context.foreshadow_reminders or '暂无需要关注的伏笔',
                relevant_memories=chapter_context.relevant_memories or '暂无相关记忆',
                content_language=generation_language
            )
    else:
        # 1-n模式：使用 context_builder 构建的结果，与单章生成保持一致
        if chapter_context.continuation_point:
            # 有前置内容，使用 WITH_CONTEXT 模板
            # 优先使用 context_builder 的摘要，其次使用传入的 previous_summary_context
            final_prev_summary = "（无上一章摘要，请根据锚点续写）"
            
            if chapter_context.previous_chapter_summary:
                final_prev_summary = chapter_context.previous_chapter_summary
            elif previous_summary_context:
                final_prev_summary = previous_summary_context
                    
            template = await PromptService.get_template("CHAPTER_GENERATION_ONE_TO_MANY_NEXT", user_id, db_session)
            base_prompt = PromptService.format_prompt(
                template,
                project_title=project.title,
                chapter_number=chapter.chapter_number,
                chapter_title=chapter.title,
                chapter_outline=chapter_context.chapter_outline,
                target_word_count=target_word_count,
                continuation_point=chapter_context.continuation_point,
                genre=project.genre or '未设定',
                narrative_perspective=temp_narrative_perspective or project.narrative_perspective or '第三人称',
                characters_info=chapter_context.chapter_characters or '暂无角色信息',
                chapter_careers=chapter_context.chapter_careers or '暂无职业信息',
                foreshadow_reminders=chapter_context.foreshadow_reminders or '暂无需要关注的伏笔',
                previous_chapter_summary=final_prev_summary,
                recent_chapters_context=chapter_context.recent_chapters_context or '',
                relevant_memories=chapter_context.relevant_memories or '',
                content_language=generation_language
            )
        else:
            # 第一章，使用无前置内容模板
            template = await PromptService.get_template("CHAPTER_GENERATION_ONE_TO_MANY", user_id, db_session)
            base_prompt = PromptService.format_prompt(
                template,
                project_title=project.title,
                chapter_number=chapter.chapter_number,
                chapter_title=chapter.title,
                chapter_outline=chapter_context.chapter_outline,
                target_word_count=target_word_count,
                genre=project.genre or '未设定',
                narrative_perspective=temp_narrative_perspective or project.narrative_perspective or '第三人称',
                characters_info=chapter_context.chapter_characters or '暂无角色信息',
                chapter_careers=chapter_context.chapter_careers or '暂无职业信息',
                foreshadow_reminders=chapter_context.foreshadow_reminders or '暂无需要关注的伏笔',
                relevant_memories=chapter_context.relevant_memories or '暂无相关记忆',
                content_language=generation_language
            )
    
    # 应用写作风格
    if style_content:
        prompt = WritingStyleManager.apply_style_to_prompt(base_prompt, style_content)
    else:
        prompt = base_prompt
    prompt = _append_full_book_context(prompt, chapter_context.full_book_context)
    
    # 🎨 将 Skill / 写作风格注入到系统提示词（批量生成）
    system_prompt_with_style = None

    # ⚡ Skill 支持
    if skill_key:
        try:
            from app.services.skill_loader import get_all_skills_cached
            skills = get_all_skills_cached()
            skill = next((s for s in skills if s["template_key"] == skill_key), None)
            if skill:
                skill_content = skill["content"]
                skill_name = skill["template_name"]
                system_prompt_with_style = f"""【⚡ Skill 工作流：{skill_name}】

{skill_content}

⚠️ 请严格遵循上述 Skill 工作流指令进行创作！"""
                if style_content:
                    system_prompt_with_style += f"""

【🎨 写作风格要求 - 补充】

{style_content}"""
                logger.info(f"⚡ 批量生成 - 已将 Skill '{skill_name}' 注入系统提示词（{len(skill_content)}字符）")
            else:
                logger.warning(f"⚠️ 批量生成 - 未找到 Skill: {skill_key}")
        except Exception as skill_err:
            logger.warning(f"⚠️ 批量生成 - 加载 Skill 失败: {skill_err}")

    if not system_prompt_with_style and style_content:
        system_prompt_with_style = f"""【🎨 写作风格要求 - 最高优先级】

{style_content}

⚠️ 请严格遵循上述写作风格要求进行创作，这是最重要的指令！
确保在整个章节创作过程中始终保持风格的一致性。"""
        logger.info(f"✅ 批量生成 - 已将写作风格注入系统提示词（{len(style_content)}字符）")
    
    # 🔢 输出预算：单一来源（按**实际探测到的**上下文窗口剩余给足上限），
    # 不再按模型名/目标字数预测，也不在本地硬算后显式传 max_tokens
    # （2026-11-08 批量第 58 章：6000 字目标被钉在 16000，推理吃光预算后正文 0 字）。
    generated_model = custom_model or getattr(ai_service, "default_model", None)
    input_tokens_est = int(
        (len(prompt) + len(system_prompt_with_style or "")) / CHARS_PER_TOKEN
    )
    window_tokens = None
    window_note: Optional[str] = None
    try:
        window_tokens = await ai_service.resolve_effective_window_tokens(generated_model)
    except ApiError as exc:
        window_note = exc.code
        if exc.code == CAPABILITY_INCOMPLETE_CODE:
            # 缓存结论缺正数窗口：门禁在派发处会放行，这里若不报出来就没人知道
            # 本次预算退化了。必须 ERROR 级。
            logger.error(f"❌ 上下文窗口结论缺失有效数值，预算退化: model={generated_model}, code={exc.code}")
        else:
            # 门禁类（below_minimum / not_configured 等）：派发处会按同一三元组重抛真实错误，
            # 这里退回保守上限不会掩盖失败。
            logger.warning(f"⚠️ 上下文窗口不可得，预算退回保守上限: code={exc.code}")
    except Exception as exc:
        # 非门禁类异常（DB 抖动、解析 bug 等）在派发处**不会**重抛，必须显式暴露。
        window_note = type(exc).__name__
        logger.error(f"❌ 读取上下文窗口异常，预算退化: model={generated_model}, {type(exc).__name__}: {exc}")
    calculated_max_tokens, budget_breakdown = plan_output_budget(
        target_chars=target_word_count,
        model=generated_model,
        base_url=getattr(ai_service, "base_url", None),
        input_tokens=input_tokens_est,
        context_window=window_tokens,
    )
    logger.info(
        f"📊 输出预算: 目标 {target_word_count} 字 → "
        f"content={budget_breakdown.get('content')} "
        f"cap={budget_breakdown.get('cap')} "
        f"measured_cap={budget_breakdown.get('measured_cap')} "
        f"context_room={budget_breakdown.get('context_room')} "
        f"window={budget_breakdown.get('window')} "
        f"input_tokens={budget_breakdown.get('input_tokens')} "
        f"window_note={window_note or 'ok'} "
        f"→ max_tokens={calculated_max_tokens}"
    )

    # 非流式生成内容
    full_content = ""
    finish_sink: Dict[str, Any] = {}
    # 准备生成参数
    prompt = _append_style_tail(prompt, style_content)
    generate_kwargs = {
        "prompt": prompt,
        "system_prompt": system_prompt_with_style,
        "tool_choice": "required",
        "max_tokens": calculated_max_tokens,
        "auto_mcp": bool(enable_mcp),
        "finish_sink": finish_sink,
    }
    # 如果传入了自定义模型，使用指定的模型
    if custom_model:
        generate_kwargs["model"] = custom_model
        logger.info(f"  批量生成使用自定义模型: {custom_model}")
    
    async def _is_batch_cancelled() -> bool:
        """流式过程中快速止损：用户取消，或任务已被外部回收为 failed。"""
        if not batch_id:
            return False
        result = await db_session.execute(
            select(BatchGenerationTask.status).where(BatchGenerationTask.id == batch_id)
        )
        return result.scalar_one_or_none() in ('cancelled', 'failed')

    # 批量生成中的流式生成（非SSE，不需要修改进度显示）
    chunk_count = 0
    async for chunk in ai_service.generate_text_stream(**generate_kwargs):
        if chunk_count % 10 == 0 and await _is_batch_cancelled():
            logger.info(f"🛑 批量生成单章流式生成被取消: batch={batch_id}, chapter={chapter.id}")
            raise Exception("批量生成任务已取消")
        full_content += chunk
        chunk_count += 1

    finish_reason = finish_sink.get("finish_reason")
    if _is_truncated_chapter_content(
        full_content,
        target_word_count,
        finish_reason,
        f"第{chapter.chapter_number}章",
    ):
        # 截断即失败：预算已按窗口剩余给足，不再抬预算重试；外层按 max_retries 处理。
        usage = finish_sink.get("usage")
        budget_error = _truncation_error_message(
            chapter_label=f"第{chapter.chapter_number}章",
            content_chars=len(full_content),
            target_word_count=target_word_count,
            finish_reason=finish_reason,
            reasoning_tokens=getattr(usage, "reasoning_tokens", None),
            reasoning_estimated=getattr(usage, "reasoning_estimated", False),
            reasoning_chars=_reasoning_chars_from_usage(
                usage, len(full_content)
            ),
            breakdown=budget_breakdown,
            budget_attempts=[calculated_max_tokens],
        )
        logger.error(f"❌ {budget_error}")
        raise ChapterGenerationTruncatedError(budget_error)

    if await _is_batch_cancelled():
        logger.info(f"🛑 批量生成保存前被取消: batch={batch_id}, chapter={chapter.id}")
        raise Exception("批量生成任务已取消")
    
    # 更新章节内容到数据库（使用锁保护）
    async with write_lock:
        old_word_count = chapter.word_count or 0
        chapter.content = full_content
        new_word_count = len(full_content)
        chapter.word_count = new_word_count
        chapter.status = "completed"
        chapter.summary = _build_lightweight_chapter_summary(full_content)
        
        # 更新项目字数
        project.current_words = project.current_words - old_word_count + new_word_count
        
        # 记录生成历史
        history = GenerationHistory(
            project_id=chapter.project_id,
            chapter_id=chapter.id,
            prompt=f"批量生成: 第{chapter.chapter_number}章 {chapter.title}",
            generated_content=full_content[:500] if len(full_content) > 500 else full_content,
            model="default"
        )
        db_session.add(history)
        
        await db_session.commit()
        await db_session.refresh(chapter)
    
    logger.info(f"✅ 单章节生成完成: 第{chapter.chapter_number}章，共 {new_word_count} 字")
    
    # 生成简短摘要返回
    summary_preview = _build_lightweight_chapter_summary(full_content)

    # issue #254 P2：生成后文笔一致性自检（只读，失败降级为 available=False）
    style_check = await compute_style_check_safely(db_session, project, full_content)
    
    # 🔮 批量生成后自动标记计划在本章埋入的伏笔
    try:
        async with write_lock:
            plant_result = await foreshadow_service.auto_plant_pending_foreshadows(
                db=db_session,
                project_id=chapter.project_id,
                chapter_id=chapter.id,
                chapter_number=chapter.chapter_number,
                chapter_content=full_content
            )
        if plant_result.get('planted_count', 0) > 0:
            logger.info(f"🔮 批量生成 - 自动标记伏笔已埋入: {plant_result['planted_count']}个")
    except Exception as plant_error:
        logger.warning(f"⚠️ 批量生成 - 自动标记伏笔埋入失败: {str(plant_error)}")
        
    return {"summary": summary_preview, "style_check": style_check}




# ==================== 章节重新生成相关API ====================

@router.post("/{chapter_id}/regenerate-stream", summary="流式重新生成章节内容")
async def regenerate_chapter_stream(
    chapter_id: str,
    request: Request,
    regenerate_request: ChapterRegenerateRequest,
    background_tasks: BackgroundTasks,
    db: AsyncSession = Depends(get_db),
    user_ai_service: AIService = Depends(get_user_ai_service)
):
    """
    根据分析建议或自定义指令重新生成章节内容（流式返回）
    
    工作流程：
    1. 验证章节和分析结果
    2. 创建重新生成任务
    3. 构建修改指令
    4. 流式生成新内容
    5. 保存为版本历史
    6. 可选自动应用
    """
    user_id = getattr(request.state, 'user_id', None)
    if not user_id:
        raise ApiError(code="auth.unauthorized")
    
    # 验证章节存在
    chapter_result = await db.execute(
        select(Chapter).where(Chapter.id == chapter_id)
    )
    chapter = chapter_result.scalar_one_or_none()
    
    if not chapter:
        raise ApiError(code="not_found.chapter")
    
    if not chapter.content or chapter.content.strip() == "":
        raise ApiError(code="validation.chapter_content_empty_for_regenerate")
    
    # 验证用户权限
    await verify_project_access(chapter.project_id, user_id, db)
    
    # 获取分析结果（如果使用分析建议）
    analysis = None
    if regenerate_request.modification_source in ['analysis_suggestions', 'mixed']:
        analysis_result = await db.execute(
            select(PlotAnalysis)
            .where(PlotAnalysis.chapter_id == chapter_id)
            .order_by(PlotAnalysis.created_at.desc())
            .limit(1)
        )
        analysis = analysis_result.scalar_one_or_none()
        
        if not analysis:
            raise ApiError(code="not_found.chapter_analysis")
    
    # 预先获取项目上下文数据和写作风格
    async for temp_db in get_db(request):
        try:
            # 获取项目信息
            project_result = await temp_db.execute(
                select(Project).where(Project.id == chapter.project_id)
            )
            project = project_result.scalar_one_or_none()
            
            # 获取角色信息（包含职业信息）
            characters_result = await temp_db.execute(
                select(Character).where(Character.project_id == chapter.project_id)
            )
            characters = characters_result.scalars().all()
            
            # 📝 根据大纲模式智能筛选相关角色（重新生成）
            outline_mode_result = await temp_db.execute(
                select(Project.outline_mode).where(Project.id == chapter.project_id)
            )
            outline_mode = outline_mode_result.scalar_one_or_none() or 'one-to-many'
            
            filter_character_names = None
            if outline_mode == 'one-to-one':
                # 1-1模式：从outline.structure中提取characters字段（优先使用 outline_id）
                if chapter.outline_id:
                    outline_result_temp = await temp_db.execute(
                        select(Outline.structure)
                        .where(Outline.id == chapter.outline_id)
                    )
                else:
                    outline_result_temp = await temp_db.execute(
                        select(Outline.structure)
                        .where(Outline.project_id == chapter.project_id)
                        .where(Outline.order_index == chapter.chapter_number)
                    )
                outline_structure = outline_result_temp.scalar_one_or_none()
                if outline_structure:
                    try:
                        structure = json.loads(outline_structure)
                        filter_character_names = structure.get('characters', [])
                        if filter_character_names:
                            logger.info(f"📋 重新生成 - 1-1模式：从structure提取角色列表 {filter_character_names}")
                    except json.JSONDecodeError:
                        logger.warning(f"⚠️ 重新生成 - outline.structure解析失败，使用全部角色")
            else:
                # 1-n模式：从chapter.expansion_plan中提取character_focus字段
                if chapter.expansion_plan:
                    try:
                        plan = json.loads(chapter.expansion_plan)
                        filter_character_names = plan.get('character_focus', [])
                        if filter_character_names:
                            logger.info(f"📋 重新生成 - 1-n模式：从expansion_plan提取角色焦点 {filter_character_names}")
                    except json.JSONDecodeError:
                        logger.warning(f"⚠️ 重新生成 - expansion_plan解析失败，使用全部角色")
            
            characters_info_with_careers = await build_characters_info_with_careers(
                db=temp_db,
                project_id=chapter.project_id,
                characters=characters,
                filter_character_names=filter_character_names
            )
            
            # 获取章节大纲（优先使用 chapter.outline_id 直接关联）
            if chapter.outline_id:
                outline_result = await temp_db.execute(
                    select(Outline).where(Outline.id == chapter.outline_id)
                )
            else:
                # 回退到按序号查找
                outline_result = await temp_db.execute(
                    select(Outline)
                    .where(Outline.project_id == chapter.project_id)
                    .where(Outline.order_index == chapter.chapter_number)
                )
            outline = outline_result.scalar_one_or_none()
            
            # 获取写作风格（§13-3：未指定/全书画像时按目标章节选卷，无卷命中再回退默认风格）
            style_content = await _resolve_project_style_content(
                temp_db,
                project,
                chapter.chapter_number,
                regenerate_request.style_id,
                user_id,
                use_project_default=True,
            )
            
            # 构建项目上下文
            project_context = {
                'project_title': project.title if project else '未知',
                'genre': project.genre if project else '未设定',
                'theme': project.theme if project else '未设定',
                'narrative_perspective': project.narrative_perspective if project else '第三人称',
                'time_period': project.world_time_period if project else '未设定',
                'location': project.world_location if project else '未设定',
                'atmosphere': project.world_atmosphere if project else '未设定',
                'characters_info': characters_info_with_careers,
                'chapter_outline': outline.content if outline else chapter.summary or '暂无大纲',
                'previous_context': ''  # 可以后续扩展添加前置章节上下文
            }
        finally:
            await temp_db.close()
        break
    
    async def event_generator():
        """流式生成事件生成器"""
        db_session = None
        db_committed = False
        
        # 初始化标准进度追踪器
        from app.utils.sse_response import WizardProgressTracker
        tracker = WizardProgressTracker("章节重新生成")
        
        try:
            yield await tracker.start()
            
            # 创建独立数据库会话
            async for db_session in get_db(request):
                yield await tracker.loading("加载章节信息...", 0.5)
                
                # 创建重新生成任务
                regen_task = RegenerationTask(
                    chapter_id=chapter_id,
                    analysis_id=analysis.id if analysis else None,
                    user_id=user_id,
                    project_id=chapter.project_id,
                    modification_instructions="",  # 稍后填充
                    original_suggestions=analysis.suggestions if analysis else None,
                    selected_suggestion_indices=regenerate_request.selected_suggestion_indices,
                    custom_instructions=regenerate_request.custom_instructions,
                    style_id=regenerate_request.style_id,
                    target_word_count=regenerate_request.target_word_count,
                    focus_areas=regenerate_request.focus_areas,
                    preserve_elements=regenerate_request.preserve_elements.model_dump() if regenerate_request.preserve_elements else None,
                    status='running',
                    original_content=chapter.content,
                    original_word_count=chapter.word_count or len(chapter.content),
                    version_note=regenerate_request.version_note,
                    started_at=datetime.now()
                )
                db_session.add(regen_task)
                await db_session.commit()
                await db_session.refresh(regen_task)
                
                task_id = regen_task.id
                logger.info(f"📝 创建重新生成任务: {task_id}")
                
                yield await tracker.preparing("准备重新生成...")
                
                yield await SSEResponse.send_event(
                    event='task_created',
                    data={'task_id': task_id}
                )
                
                # 初始化重新生成器
                regenerator = ChapterRegenerator(user_ai_service)
                
                # === 生成阶段 ===
                full_content = ""
                estimated_total = regenerate_request.target_word_count or len(chapter.content)
                
                yield await tracker.generating(
                    current_chars=0,
                    estimated_total=estimated_total
                )
                
                # issue #254 P2：重生成后的文笔一致性自检（由 regenerator 计算，失败为 available=False）
                style_check = dict(STYLE_CHECK_UNAVAILABLE)

                async for event in regenerator.regenerate_with_feedback(
                    chapter=chapter,
                    analysis=analysis,
                    regenerate_request=regenerate_request,
                    project_context=project_context,
                    style_content=style_content,
                    user_id=user_id,
                    db=db_session
                ):
                    # 处理不同类型的事件
                    if event['type'] == 'chunk':
                        # 内容块
                        chunk = event['content']
                        full_content += chunk
                        yield await tracker.generating_chunk(chunk)
                        
                        # 定期更新进度
                        if len(full_content) % 500 == 0:
                            yield await tracker.generating(
                                current_chars=len(full_content),
                                estimated_total=estimated_total,
                                message=f'重新生成中... 已生成 {len(full_content)} 字'
                            )
                    elif event['type'] == 'progress':
                        # 进度更新 - 映射到对应阶段
                        if event.get('style_check') is not None:
                            style_check = event['style_check']
                        progress = event.get('progress', 0)
                        message = event.get('message', '')
                        if progress < 20:
                            yield await tracker.preparing(message)
                        elif progress < 85:
                            yield await tracker.generating(
                                current_chars=len(full_content),
                                estimated_total=estimated_total,
                                message=message
                            )
                        else:
                            yield await tracker.parsing(message)
                    
                    await asyncio.sleep(0)
                
                # === 保存阶段 ===
                yield await tracker.saving("保存重新生成的内容...", 0.5)
                
                # 更新任务状态
                regen_task.status = 'completed'
                regen_task.regenerated_content = full_content
                regen_task.regenerated_word_count = len(full_content)
                regen_task.completed_at = datetime.now()
                
                # 计算差异统计
                diff_stats = regenerator.calculate_content_diff(chapter.content, full_content)
                
                await db_session.commit()
                db_committed = True
                
                yield await tracker.saving("保存完成", 0.9)
                
                # === 完成阶段 ===
                yield await tracker.complete("重新生成完成！", code="progress.done")
                
                # 发送结果数据
                yield await tracker.result({
                    'task_id': task_id,
                    'word_count': len(full_content),
                    'version_number': regen_task.version_number,
                    'auto_applied': regenerate_request.auto_apply,
                    'diff_stats': diff_stats,
                    'style_check': style_check,
                })
                
                # 发送完成信号
                yield await tracker.done()
                
                logger.info(f"✅ 章节重新生成完成: {chapter_id}, 任务: {task_id}")
                
                break
        
        except Exception as e:
            logger.error(f"❌ 重新生成失败: {str(e)}", exc_info=True)
            
            # 更新任务状态为失败
            if db_session and not db_committed:
                try:
                    task_result = await db_session.execute(
                        select(RegenerationTask).where(RegenerationTask.chapter_id == chapter_id)
                        .order_by(RegenerationTask.created_at.desc()).limit(1)
                    )
                    task = task_result.scalar_one_or_none()
                    if task:
                        task.status = 'failed'
                        task.error_message = str(e)[:500]
                        task.completed_at = datetime.now()
                        await db_session.commit()
                except Exception as update_error:
                    logger.error(f"更新任务失败状态失败: {str(update_error)}")
            
            yield await tracker.error_from_exception(e, str(e))
        
        finally:
            if db_session:
                try:
                    if not db_committed and db_session.in_transaction():
                        await db_session.rollback()
                    await db_session.close()
                except Exception as close_error:
                    logger.error(f"关闭数据库会话失败: {str(close_error)}")
    
    return create_sse_response(event_generator())


@router.get("/{chapter_id}/regeneration/tasks", summary="获取章节的重新生成任务列表")
async def get_regeneration_tasks(
    chapter_id: str,
    request: Request,
    limit: int = Query(10, ge=1, le=50),
    db: AsyncSession = Depends(get_db)
):
    """获取指定章节的重新生成任务历史"""
    user_id = getattr(request.state, 'user_id', None)
    
    # 验证章节存在和权限
    chapter_result = await db.execute(
        select(Chapter).where(Chapter.id == chapter_id)
    )
    chapter = chapter_result.scalar_one_or_none()
    if not chapter:
        raise ApiError(code="not_found.chapter")
    
    await verify_project_access(chapter.project_id, user_id, db)
    
    # 获取任务列表
    result = await db.execute(
        select(RegenerationTask)
        .where(RegenerationTask.chapter_id == chapter_id)
        .order_by(RegenerationTask.created_at.desc())
        .limit(limit)
    )
    tasks = result.scalars().all()
    
    return {
        "chapter_id": chapter_id,
        "total": len(tasks),
        "tasks": [
            {
                "task_id": task.id,
                "status": task.status,
                "version_number": task.version_number,
                "version_note": task.version_note,
                "original_word_count": task.original_word_count,
                "regenerated_word_count": task.regenerated_word_count,
                "created_at": task.created_at.isoformat() if task.created_at else None,
                "completed_at": task.completed_at.isoformat() if task.completed_at else None
            }
            for task in tasks
        ]
    }


@router.put("/{chapter_id}/expansion-plan", response_model=dict, summary="更新章节规划信息")
async def update_chapter_expansion_plan(
    chapter_id: str,
    expansion_plan: ExpansionPlanUpdate,
    request: Request,
    db: AsyncSession = Depends(get_db)
):
    """
    更新章节的展开规划信息和情节概要
    
    Args:
        chapter_id: 章节ID
        expansion_plan: 规划信息更新数据(包含summary和expansion_plan字段)
    
    Returns:
        更新后的章节规划信息
    """
    # 获取章节
    result = await db.execute(
        select(Chapter).where(Chapter.id == chapter_id)
    )
    chapter = result.scalar_one_or_none()
    
    if not chapter:
        raise ApiError(code="not_found.chapter")
    
    # 验证用户权限
    user_id = getattr(request.state, 'user_id', None)
    await verify_project_access(chapter.project_id, user_id, db)
    
    # 准备更新数据(排除None值)
    plan_data = expansion_plan.model_dump(exclude_unset=True, exclude_none=True)
    
    # 分离summary和expansion_plan数据
    summary_value = plan_data.pop('summary', None)
    
    # 更新summary字段(如果提供)
    if summary_value is not None:
        chapter.summary = summary_value
        logger.info(f"更新章节概要: {chapter_id}")
    
    # 更新expansion_plan字段(如果有其他字段)
    if plan_data:
        if chapter.expansion_plan:
            try:
                existing_plan = json.loads(chapter.expansion_plan)
                # 合并更新
                existing_plan.update(plan_data)
                chapter.expansion_plan = json.dumps(
                    to_storage_plan(existing_plan),
                    ensure_ascii=False,
                )
            except json.JSONDecodeError:
                logger.warning(f"章节 {chapter_id} 的expansion_plan格式错误,将覆盖")
                chapter.expansion_plan = json.dumps(
                    to_storage_plan(plan_data),
                    ensure_ascii=False,
                )
        else:
            chapter.expansion_plan = json.dumps(
                to_storage_plan(plan_data),
                ensure_ascii=False,
            )
    
    await db.commit()
    await db.refresh(chapter)
    
    logger.info(f"章节规划更新成功: {chapter_id}")
    
    # 返回更新后的规划数据
    updated_plan = json.loads(chapter.expansion_plan) if chapter.expansion_plan else None
    
    return {
        "id": chapter.id,
        "summary": chapter.summary,
        "expansion_plan": updated_plan,
        "message": "规划信息更新成功"
    }


# ==================== 局部重写相关API ====================

CONTINUE_PREFIXES_TO_REMOVE = [
    "重写后：", "重写后:", "改写后：", "改写后:",
    "以下是重写后的内容：", "以下是重写后的内容:",
    "重写内容：", "重写内容:"
]


def _clean_partial_generated_text(text: str) -> str:
    """清理生成输出：strip → 常见AI前缀 → 首尾引号（rewrite/continue 共用）。"""
    text = text.strip()
    for prefix in CONTINUE_PREFIXES_TO_REMOVE:
        if text.startswith(prefix):
            text = text[len(prefix):].strip()
            break
    if (text.startswith('"') and text.endswith('"')) or \
       (text.startswith("'") and text.endswith("'")):
        text = text[1:-1]
    if (text.startswith('「') and text.endswith('」')) or \
       (text.startswith('『') and text.endswith('』')):
        text = text[1:-1]
    return text


def _strip_anchor_echo(text: str, anchor_text: str) -> str:
    """D7 回声守卫：输出以锚点尾部（探针=锚点后≤40字，须≥8字）开头时剥除该重复前缀。"""
    probe = anchor_text[-40:]
    if len(probe) >= 8 and text.startswith(probe):
        return text[len(probe):].strip()
    return text


async def _stream_partial_generate(
    tracker,
    ai_stream,
    *,
    estimated_total: int,
    progress_message: Callable[[int], str],
):
    """单次流式生成 pass：包心跳（HEARTBEAT→tracker.heartbeat()）；yield (SSE事件, 累计文本)。"""
    full_content = ""
    chunk_count = 0
    async for item in wrap_stream_with_heartbeat(ai_stream, heartbeat_interval=15.0):
        if item is HEARTBEAT:
            yield await tracker.heartbeat(), full_content
            continue
        full_content += item
        chunk_count += 1
        yield await tracker.generating_chunk(item), full_content
        if chunk_count % 5 == 0:
            yield await tracker.generating(
                current_chars=len(full_content),
                estimated_total=estimated_total,
                message=progress_message(len(full_content)),
            ), full_content
        await asyncio.sleep(0)


@router.post("/{chapter_id}/partial-regenerate-stream", summary="流式局部重写选中内容")
async def partial_regenerate_stream(
    chapter_id: str,
    request: Request,
    partial_request: PartialRegenerateRequest,
    db: AsyncSession = Depends(get_db),
    user_ai_service: AIService = Depends(get_user_ai_service)
):
    """
    对章节中选中的部分内容进行流式重写
    
    工作流程：
    1. 验证章节和选中内容的有效性
    2. 截取上下文（前后文）
    3. 根据用户要求构建提示词
    4. 流式生成重写内容
    5. 返回重写结果（不自动保存，由前端决定是否应用）
    """
    user_id = getattr(request.state, 'user_id', None)
    if not user_id:
        raise ApiError(code="auth.unauthorized")
    
    # 验证章节存在
    chapter_result = await db.execute(
        select(Chapter).where(Chapter.id == chapter_id)
    )
    chapter = chapter_result.scalar_one_or_none()
    
    if not chapter:
        raise ApiError(code="not_found.chapter")
    
    if not chapter.content or chapter.content.strip() == "":
        raise ApiError(code="validation.chapter_content_empty")
    
    # 验证用户权限
    await verify_project_access(chapter.project_id, user_id, db)
    
    mode = partial_request.mode
    content_length = len(chapter.content)

    if mode == "continue":
        # ---- 章内 AI 续写：位置与锚点校验（精确匹配，无 ±50 模糊校正；start==end 允许=追加）----
        if partial_request.start_position > content_length:
            raise ApiError(
                code="validation.polish_range_out_of_bounds",
                detail="起始位置超出内容范围",
                params={"bound": "start"},
            )
        if partial_request.end_position > content_length:
            raise ApiError(
                code="validation.polish_range_out_of_bounds",
                detail="结束位置超出内容范围",
                params={"bound": "end"},
            )
        if partial_request.start_position > partial_request.end_position:
            raise ApiError(code="validation.polish_start_before_end")

        actual_selected = chapter.content[partial_request.start_position:partial_request.end_position]
        if partial_request.selected_text and actual_selected != partial_request.selected_text:
            raise ApiError(code="validation.polish_selection_mismatch")

        target_chars = partial_request.target_word_count or 1000
        if target_chars > CONTINUE_MAX_TARGET_CHARS:
            raise ApiError(code="validation.continue_target_too_large")

        # ---- 能力推导与服务端分段（无服务端循环：单请求单段，客户端驱动 0..N-1）----
        continue_model = getattr(user_ai_service, "default_model", None)
        # prompt_chars 口径：此刻 prompt 还没拼（分段必须先于 prompt 决定，本函数不为此
        # 调整顺序），所以按该请求**真正会拼出的**上下文规模的已知组成估算（见下方
        # continue_event_generator）：首段前窗 3000 + 后窗 1200 字，后续段改用滚动上下文
        # rolling_context（取两者较大值），再叠加锚点文本与用户额外要求。风格正文与模板
        # 在此刻尚未读取，按 0 计 —— 输入估算只用于「窗口剩余」判断，少算几千字符在实测
        # 窗口（≥1M）面前不改变结论。
        continue_prompt_chars = (
            max(3000 + 1200, len((partial_request.rolling_context or "").strip()))
            + len(partial_request.selected_text or "")
            + len(partial_request.user_instructions or "")
        )
        # 注：rolling_context 在模板里会被拼**两次**（前文 + 锚点文本，见
        # continue_event_generator 的 k>0 分支），故再叠一份；少算会让窗口剩余偏大。
        continue_prompt_chars += len((partial_request.rolling_context or "").strip())
        # 输出上限来自**实测能力**（窗口剩余 / 网关报过的输出上限），不按模型名登记表猜。
        # 同一请求内只算一次（分段数必须跨段稳定），失败才退回保守上限。
        try:
            segment_budget, _ = await user_ai_service.resolve_output_budget(
                target_chars=target_chars,
                prompt_chars=continue_prompt_chars,
                model=continue_model,
            )
            # 用**已被窗口剩余钳过**的预算当上限，而不是裸的 cap：网关报过很大的输出
            # 上限（如 200000）而 prompt 几乎占满窗口时，裸 cap 会让分段按
            # SEGMENT_TARGET_CHARS 规划出一段塞不进窗口的请求（上游 400 兜底）。
            output_limit = max(1, int(segment_budget))
        except Exception as exc:
            _log_budget_capability_failure(exc, model=continue_model, scope="续写")
            output_limit = THINKING_MODEL_DEFAULT_MAX_TOKENS
        safety_factor = 2.0
        effective_segment_chars = min(SEGMENT_TARGET_CHARS, output_limit // 2)
        if effective_segment_chars < CONTINUE_MIN_SEGMENT_CHARS:
            raise ApiError(code="internal.continue_capability_insufficient")
        # N 按 FULL target_chars 推导（跨段稳定），不按剩余量
        segment_count = max(1, ceil(target_chars / effective_segment_chars))
        if segment_count > MAX_CONTINUE_SEGMENTS:
            raise ApiError(code="validation.continue_segment_limit_exceeded")
        if partial_request.segment_index >= segment_count:
            raise ApiError(code="validation.continue_segment_index_invalid")

        remaining = max(0, target_chars - partial_request.already_generated_chars)
        seg_chars = max(1, min(effective_segment_chars, remaining))
        # 上限来自能力：max_tokens 是上限不是花费，给足由模型在推理与正文间自行分配
        budget = min(ceil(seg_chars * safety_factor), output_limit)
    else:
        # ---- 局部重写：既有位置校验（行为保持）----
        if partial_request.start_position >= content_length:
            raise ApiError(
                code="validation.polish_range_out_of_bounds",
                detail="起始位置超出内容范围",
                params={"bound": "start"},
            )
        if partial_request.end_position > content_length:
            raise ApiError(
                code="validation.polish_range_out_of_bounds",
                detail="结束位置超出内容范围",
                params={"bound": "end"},
            )
        if partial_request.start_position >= partial_request.end_position:
            raise ApiError(code="validation.polish_start_before_end")
    
    # 验证选中的文本是否匹配
    actual_selected = chapter.content[partial_request.start_position:partial_request.end_position]
    if mode != "continue" and actual_selected != partial_request.selected_text:
        # 位置可能有偏差，尝试在附近查找
        search_start = max(0, partial_request.start_position - 50)
        search_end = min(content_length, partial_request.end_position + 50)
        search_area = chapter.content[search_start:search_end]
        
        if partial_request.selected_text in search_area:
            # 找到了，更新位置
            offset = search_area.find(partial_request.selected_text)
            partial_request.start_position = search_start + offset
            partial_request.end_position = partial_request.start_position + len(partial_request.selected_text)
            logger.info(f"⚠️ 选中文本位置校正: {partial_request.start_position}-{partial_request.end_position}")
        else:
            raise ApiError(
                code="validation.polish_selection_mismatch",
            )
    
    # 预先获取项目信息和写作风格
    project_result = await db.execute(
        select(Project).where(Project.id == chapter.project_id)
    )
    project = project_result.scalar_one_or_none()
    
    # 获取写作风格
    style_content = ""
    style_id = partial_request.style_id
    
    # 如果没有指定风格，尝试使用项目的默认风格
    if not style_id:
        from app.models.project_default_style import ProjectDefaultStyle
        default_style_result = await db.execute(
            select(ProjectDefaultStyle.style_id)
            .where(ProjectDefaultStyle.project_id == chapter.project_id)
        )
        default_style_id = default_style_result.scalar_one_or_none()
        if default_style_id:
            style_id = default_style_id
            logger.info(f"📝 局部重写 - 使用项目默认写作风格: {style_id}")
    
    # 获取风格内容
    if style_id:
        style_result = await db.execute(
            select(WritingStyle).where(WritingStyle.id == style_id)
        )
        style = style_result.scalar_one_or_none()
        if style:
            if style.user_id is None or style.user_id == user_id:
                style_content = style.prompt_content or ""
                style_type = "全局预设" if style.user_id is None else "用户自定义"
                logger.info(f"✅ 局部重写 - 使用写作风格: {style.name} ({style_type})")
            else:
                logger.warning(f"⚠️ 风格 {style_id} 不属于当前用户，跳过")
    
    async def continue_event_generator():
        """续写流式事件生成器：单请求单段（客户端驱动 0..N-1，无服务端循环）"""
        from app.utils.sse_response import WizardProgressTracker
        tracker = WizardProgressTracker("AI续写")
        seg_index = partial_request.segment_index

        try:
            yield await tracker.start()
            yield await tracker.loading("准备续写上下文...", 0.3)

            pre_window, post_window = 3000, 1200
            rolling = (partial_request.rolling_context or "").strip()
            if seg_index > 0 and rolling:
                context_before = rolling
                anchor_text = rolling
                context_after = "（续写中，暂无后文）"
            else:
                start_pos = partial_request.start_position
                end_pos = partial_request.end_position
                context_before = chapter.content[max(0, start_pos - pre_window):start_pos] or "（这是章节开头）"
                anchor_text = partial_request.selected_text or "（续写起点）"
                context_after = chapter.content[end_pos:min(content_length, end_pos + post_window)] or "（这是章节结尾）"

            logger.info(f"🤖 AI续写 第{seg_index + 1}/{segment_count}段: 锚点{len(anchor_text)}字, "
                        f"前文{len(context_before)}字, 后文{len(context_after)}字, 本段预算{seg_chars}字")

            yield await tracker.loading("构建提示词...", 0.5)

            template = await PromptService.get_template("PARTIAL_CONTINUE", user_id, db)
            if not template:
                template = PromptService.PARTIAL_CONTINUE

            generation_language = await resolve_user_generation_language(db, user_id)

            prompt = PromptService.format_prompt(
                template,
                context_before=context_before,
                anchor_text=anchor_text,
                context_after=context_after,
                target_chars=seg_chars,
                user_instructions=partial_request.user_instructions,
                style_content=style_content or "保持与原文一致的叙事风格",
                content_language=generation_language,
            )
            prompt = _append_style_tail(prompt, style_content)

            yield await tracker.preparing("开始生成...")

            def progress_message(chars: int) -> str:
                return f"正在续写中... 第{seg_index + 1}/{segment_count}段, 已生成 {chars} 字"

            yield await tracker.generating(
                current_chars=0,
                estimated_total=seg_chars,
                message=f"AI续写 第{seg_index + 1}/{segment_count}段...",
            )

            full_content = ""
            async for ev, full_content in _stream_partial_generate(
                tracker,
                user_ai_service.generate_text_stream(prompt=prompt, max_tokens=budget),
                estimated_total=seg_chars,
                progress_message=progress_message,
            ):
                yield ev

            full_content = _strip_anchor_echo(_clean_partial_generated_text(full_content), anchor_text)

            if len(full_content) < max(50, seg_chars * 0.1):
                full_content = ""
                async for ev, full_content in _stream_partial_generate(
                    tracker,
                    user_ai_service.generate_text_stream(prompt=prompt, max_tokens=budget),
                    estimated_total=seg_chars,
                    progress_message=progress_message,
                ):
                    yield ev
                full_content = _strip_anchor_echo(_clean_partial_generated_text(full_content), anchor_text)

            if len(full_content) == 0:
                logger.warning(f"⚠️ AI续写返回空正文: 本段目标{seg_chars}字, model={continue_model}")
                yield await tracker.error(
                    "AI服务返回空响应",
                    error_code="internal.ai_empty_response",
                )
                return

            content_hash = hashlib.sha256(chapter.content.encode("utf-8")).hexdigest()
            logger.info(f"✅ AI续写完成: 第{seg_index + 1}/{segment_count}段生成{len(full_content)}字")

            yield await tracker.complete("续写完成！", code="progress.done")

            yield await tracker.result({
                'new_text': full_content,
                'word_count': len(full_content),
                'original_word_count': len(anchor_text),
                'start_position': partial_request.end_position,
                'end_position': partial_request.end_position,
                'mode': 'continue',
                'content_hash': content_hash,
                'segment_index': seg_index,
                'segment_count': segment_count,
                'requested_chars': seg_chars,
                'generated_chars': len(full_content),
                'complete': seg_index + 1 >= segment_count,
            })

            yield await tracker.done()

        except Exception as e:
            logger.error(f"❌ AI续写失败: {str(e)}", exc_info=True)
            yield await tracker.error_from_exception(e, str(e))

    if mode == "continue":
        return create_sse_response(continue_event_generator())

    async def event_generator():
        """流式生成事件生成器"""
        from app.utils.sse_response import WizardProgressTracker
        tracker = WizardProgressTracker("局部重写")
        
        try:
            yield await tracker.start()
            yield await tracker.loading("准备重写上下文...", 0.3)
            
            # 截取上下文
            context_chars = partial_request.context_chars
            start_pos = partial_request.start_position
            end_pos = partial_request.end_position
            
            # 前文：从start_pos往前截取context_chars个字符
            context_before_start = max(0, start_pos - context_chars)
            context_before = chapter.content[context_before_start:start_pos]
            
            # 后文：从end_pos往后截取context_chars个字符
            context_after_end = min(content_length, end_pos + context_chars)
            context_after = chapter.content[end_pos:context_after_end]
            
            # 原文
            original_text = partial_request.selected_text
            original_word_count = len(original_text)
            
            logger.info(f"📝 局部重写 - 原文: {original_word_count}字, 前文: {len(context_before)}字, 后文: {len(context_after)}字")
            
            yield await tracker.loading("构建提示词...", 0.5)
            
            # 构建字数要求
            length_requirement = ""
            if partial_request.length_mode == "similar":
                min_words = int(original_word_count * 0.8)
                max_words = int(original_word_count * 1.2)
                length_requirement = f"保持与原文相近的字数（约{original_word_count}字，允许{min_words}-{max_words}字浮动）"
            elif partial_request.length_mode == "expand":
                min_words = int(original_word_count * 1.2)
                max_words = int(original_word_count * 2.0)
                length_requirement = f"适当扩展内容（目标{min_words}-{max_words}字）"
            elif partial_request.length_mode == "condense":
                min_words = int(original_word_count * 0.5)
                max_words = int(original_word_count * 0.8)
                length_requirement = f"精简压缩内容（目标{min_words}-{max_words}字）"
            elif partial_request.length_mode == "custom" and partial_request.target_word_count:
                length_requirement = f"目标字数：约{partial_request.target_word_count}字（允许±20%浮动）"
            else:
                length_requirement = f"保持与原文相近的字数（约{original_word_count}字）"
            
            # 获取提示词模板
            template = await PromptService.get_template("PARTIAL_REGENERATE", user_id, db)
            if not template:
                template = PromptService.PARTIAL_REGENERATE

            # 解析最终生成语言：用户偏好 > UI 语言 > zh（局部重写无 per-gen 参数，todo 17）
            generation_language = await resolve_user_generation_language(db, user_id)

            # 构建提示词
            prompt = PromptService.format_prompt(
                template,
                context_before=context_before if context_before else "（这是章节开头）",
                original_word_count=original_word_count,
                selected_text=original_text,
                context_after=context_after if context_after else "（这是章节结尾）",
                user_instructions=partial_request.user_instructions,
                length_requirement=length_requirement,
                style_content=style_content if style_content else "保持与原文一致的叙事风格",
                content_language=generation_language
            )
            prompt = _append_style_tail(prompt, style_content)
            
            yield await tracker.preparing("开始生成...")
            
            # 计算 max_tokens
            if partial_request.length_mode == "expand":
                target_words = int(original_word_count * 2.0)
            elif partial_request.length_mode == "custom" and partial_request.target_word_count:
                target_words = partial_request.target_word_count
            else:
                target_words = int(original_word_count * 1.5)
            
            # 计算 max_tokens：单一来源 = 实测能力（窗口剩余 / 网关报过的输出上限），
            # 不按选区字数硬算、也不按模型名抬底。失败时才退回 plan_output_budget 的
            # 保守上限（无实测上限、无窗口时它内部按 THINKING_MODEL_DEFAULT_MAX_TOKENS 封顶）。
            rewrite_model = getattr(user_ai_service, "default_model", None)
            try:
                calculated_max_tokens, budget_breakdown = await user_ai_service.resolve_output_budget(
                    target_chars=target_words,
                    prompt_chars=len(prompt),
                    model=rewrite_model,
                )
            except Exception as exc:
                _log_budget_capability_failure(exc, model=rewrite_model, scope="局部重写")
                calculated_max_tokens, budget_breakdown = plan_output_budget(
                    target_chars=target_words,
                    model=rewrite_model,
                    base_url=getattr(user_ai_service, "base_url", None),
                )
            logger.info(
                f"📊 局部重写输出预算: 目标 {target_words} 字 → "
                f"cap={budget_breakdown.get('cap')} "
                f"measured_cap={budget_breakdown.get('measured_cap')} "
                f"context_room={budget_breakdown.get('context_room')} "
                f"window={budget_breakdown.get('window')} "
                f"input_tokens={budget_breakdown.get('input_tokens')} "
                f"→ max_tokens={calculated_max_tokens}"
            )
            
            # 流式生成
            full_content = ""
            
            yield await tracker.generating(
                current_chars=0,
                estimated_total=target_words
            )
            
            async for ev, full_content in _stream_partial_generate(
                tracker,
                user_ai_service.generate_text_stream(
                    prompt=prompt,
                    max_tokens=calculated_max_tokens
                ),
                estimated_total=target_words,
                progress_message=lambda chars: f'正在重写中... 已生成 {chars} 字',
            ):
                yield ev
            
            # 清理输出（移除可能的前后缀）
            full_content = _clean_partial_generated_text(full_content)
            
            new_word_count = len(full_content)
            
            if new_word_count == 0:
                logger.warning(
                    f"⚠️ 局部重写返回空正文: 原文{original_word_count}字, "
                    f"model={getattr(user_ai_service, 'default_model', None)}"
                )
                yield await tracker.error(
                    "AI服务返回空响应",
                    error_code="internal.ai_empty_response",
                )
                return
            
            logger.info(f"✅ 局部重写完成: 原文{original_word_count}字 -> 新文{new_word_count}字")

            content_hash = hashlib.sha256(chapter.content.encode("utf-8")).hexdigest()

            # 完成
            yield await tracker.complete("重写完成！", code="progress.done")

            # 发送结果数据
            yield await tracker.result({
                'new_text': full_content,
                'word_count': new_word_count,
                'original_word_count': original_word_count,
                'start_position': partial_request.start_position,
                'end_position': partial_request.end_position,
                'mode': 'rewrite',
                'content_hash': content_hash,
            })
            
            yield await tracker.done()
            
        except Exception as e:
            logger.error(f"❌ 局部重写失败: {str(e)}", exc_info=True)
            yield await tracker.error_from_exception(e, str(e))
    
    return create_sse_response(event_generator())


@router.post("/{chapter_id}/apply-partial-regenerate", summary="应用局部重写结果")
async def apply_partial_regenerate(
    chapter_id: str,
    request: Request,
    apply_request: dict,
    db: AsyncSession = Depends(get_db)
):
    """
    将局部重写的结果应用到章节内容中
    
    请求体：
    - new_text: 重写后的新内容
    - start_position: 原文起始位置
    - end_position: 原文结束位置
    """
    user_id = getattr(request.state, 'user_id', None)
    if not user_id:
        raise ApiError(code="auth.unauthorized")
    
    # 验证章节存在
    chapter_result = await db.execute(
        select(Chapter).where(Chapter.id == chapter_id)
    )
    chapter = chapter_result.scalar_one_or_none()
    
    if not chapter:
        raise ApiError(code="not_found.chapter")
    
    # 验证用户权限
    await verify_project_access(chapter.project_id, user_id, db)
    
    # 获取参数
    new_text = apply_request.get('new_text', '')
    start_position = apply_request.get('start_position', 0)
    end_position = apply_request.get('end_position', 0)
    mode = apply_request.get('mode', 'rewrite')
    content_hash = apply_request.get('content_hash')
    
    if not new_text:
        raise ApiError(code="validation.polish_new_content_empty")
    
    # D6 乐观锁：客户端带 content_hash 时比对当前章节正文，不一致则拒绝写入（旧客户端不带则跳过）
    if content_hash is not None:
        actual_hash = hashlib.sha256(chapter.content.encode("utf-8")).hexdigest()
        if actual_hash != content_hash:
            logger.warning(f"⚠️ content_hash 不匹配，拒绝应用: 章节{chapter_id}（内容已被其他路径修改）")
            raise ApiError(code="validation.content_hash_mismatch")
    
    # 验证位置有效性（续写插入允许 start==end；重写保持严格小于）
    content_length = len(chapter.content)
    bad_order = start_position > end_position if mode == "continue" else start_position >= end_position
    if start_position < 0 or end_position > content_length or bad_order:
        raise ApiError(code="validation.polish_position_invalid")
    
    # 构建新内容
    old_word_count = chapter.word_count or 0
    new_content = chapter.content[:start_position] + new_text + chapter.content[end_position:]
    new_word_count = len(new_content)
    
    # 更新章节
    chapter.content = new_content
    chapter.word_count = new_word_count
    
    # 更新项目字数
    project_result = await db.execute(
        select(Project).where(Project.id == chapter.project_id)
    )
    project = project_result.scalar_one_or_none()
    if project:
        project.current_words = project.current_words - old_word_count + new_word_count
    
    await db.commit()
    await db.refresh(chapter)
    
    action_label = "AI续写" if mode == "continue" else "局部重写"
    logger.info(f"✅ {action_label}已应用: 章节{chapter_id}, {old_word_count}字 -> {new_word_count}字")
    
    return {
        "success": True,
        "chapter_id": chapter_id,
        "word_count": new_word_count,
        "old_word_count": old_word_count,
        "message": f"{action_label}已应用"
    }
