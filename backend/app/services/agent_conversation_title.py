"""创作助手会话标题的自动生成（issue #262）。

与 #260 的规则标题分工：
- 规则标题（首行截 40 字）在会话创建/首条消息时立刻可用，作为占位；
- 本模块在首轮结束后异步用模型生成一句 <= 20 字的摘要标题并覆盖占位；
- 用户显式重命名（title_user_edited=True）后不再覆盖，失败静默保留规则标题。

设计约束：
- 不阻塞/不失败对话主路径：fire-and-forget + 独立 session（同会话摘要调度模式）；
- 标题只由「用户第一句话」生成，不读取助手正文/工具结果，避免把正文专名带进标题；
- 日志只记长度与状态，不记标题原文。
"""
from __future__ import annotations

import asyncio

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.logger import get_logger
from app.models.project_agent import AgentConversation, AgentMessage

logger = get_logger(__name__)

TITLE_MAX_CHARS = 20

TITLE_SYSTEM_PROMPT = """你在为 AI 小说创作助手的一次对话拟标题。

规则：
1. 用一句短语概括这次对话要解决的问题，不超过 20 个字。
2. 不要引号、句号结尾、书名号、emoji 或换行；不要写「对话」「提问」「用户」这类词。
3. 只输出标题本身，不要解释、不要代码块。"""

TITLE_USER_PROMPT = "用户的第一句话：\n{message}"


def clean_generated_title(raw: str | None) -> str:
    """清理模型输出：取首行、去包裹符号、空白归一化、截断上限。"""
    text = (raw or "").strip()
    if not text:
        return ""
    text = text.splitlines()[0].strip()
    text = text.strip('"\'“”‘’《》「」【】[]()（） ')
    text = " ".join(text.split())
    if not text:
        return ""
    return text[:TITLE_MAX_CHARS]


async def _first_user_message(session: AsyncSession, conversation_id: str) -> str:
    content = await session.scalar(
        select(AgentMessage.content)
        .where(
            AgentMessage.conversation_id == conversation_id,
            AgentMessage.role == "user",
        )
        .order_by(AgentMessage.created_at.asc(), AgentMessage.id.asc())
        .limit(1)
    )
    return content or ""


async def generate_title_with(
    session: AsyncSession,
    ai_service,
    conversation_id: str,
    *,
    first_message: str = "",
) -> str:
    """用调用方的 session/AI 服务生成并写入标题；返回写入的标题（失败/跳过为空串）。"""
    conversation = (
        await session.execute(
            select(AgentConversation).where(AgentConversation.id == conversation_id)
        )
    ).scalar_one_or_none()
    if conversation is None or conversation.title_user_edited:
        return ""

    source = " ".join((first_message or "").split())
    if not source:
        source = " ".join((await _first_user_message(session, conversation_id)).split())
    if not source:
        return ""

    response = await ai_service.generate_text(
        prompt=TITLE_USER_PROMPT.format(message=source[:500]),
        system_prompt=TITLE_SYSTEM_PROMPT,
        temperature=0.3,
        max_tokens=64,
        auto_mcp=False,
        handle_tool_calls=False,
    )
    title = clean_generated_title((response or {}).get("content") or "")
    if not title:
        return ""

    # 生成期间用户可能已经重命名：刷新后再判一次，用户输入优先。
    await session.refresh(conversation)
    if conversation.title_user_edited:
        return ""
    conversation.title = title
    await session.commit()
    logger.info(
        f"会话标题已生成: conversation={conversation_id[:8]} chars={len(title)}"
    )
    return title


async def generate_conversation_title(
    conversation_id: str, user_id: str, *, first_message: str = ""
) -> str:
    """独立 session 生成标题（供 fire-and-forget 调度）；失败返回空串。"""
    if not conversation_id or not user_id:
        return ""
    try:
        from app.api.settings import get_user_ai_service_from_db
        from app.database import get_engine

        engine = await get_engine(user_id)
        factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
        async with factory() as session:
            owned = await session.scalar(
                select(AgentConversation.id).where(
                    AgentConversation.id == conversation_id,
                    AgentConversation.user_id == user_id,
                )
            )
            if owned is None:
                return ""
            ai_service = await get_user_ai_service_from_db(user_id, session)
            return await generate_title_with(
                session,
                ai_service,
                conversation_id,
                first_message=first_message,
            )
    except Exception:  # noqa: BLE001 —— 标题生成失败不得影响对话
        logger.warning(
            f"会话标题生成失败: conversation={conversation_id[:8]}", exc_info=True
        )
        return ""


#: 持有运行中的任务引用，避免被垃圾回收；按会话去重，重复调度直接跳过。
_PENDING_TASKS: dict[str, asyncio.Task] = {}


def schedule_conversation_title(
    conversation_id: str, user_id: str, *, first_message: str = ""
) -> None:
    """异步调度一次标题生成；调用方不需要 await，也不应依赖它完成。"""
    if not conversation_id or not user_id:
        return
    existing = _PENDING_TASKS.get(conversation_id)
    if existing is not None and not existing.done():
        return
    task = asyncio.create_task(
        generate_conversation_title(conversation_id, user_id, first_message=first_message)
    )
    _PENDING_TASKS[conversation_id] = task
    task.add_done_callback(lambda _task: _PENDING_TASKS.pop(conversation_id, None))
