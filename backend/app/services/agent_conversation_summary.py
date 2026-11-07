"""会话结论摘要（跨会话记忆的「结论层」）。

只做一件事：把一个会话谈定的事压成一段短文本，写入
`AgentConversation.summary`。不建表、不抽结构化断言，刻意保持轻量。

为什么必须有这一层：实测一个 53 条消息的会话里，用户 13 条发言合计 1361 字符，
全部是提问、纠正与确认；真正的决策汇总（约 500 字符）在助手的一条回复里。
只注入对话尾部时，结论能否进入注入预算取决于尾部噪声的长度 —— 独立审核已给出
反例：结论之后只要跟一条足够长的执行叙述，结论就被挤出预算。结论层把「结论必
在场」从数据巧合变成机制保证。

设计约束：
- 结论必须显式抽取并持久化，不能靠注入原文碰运气；
- 抽取是「总结这次谈定了什么」，属模型擅长的归纳，不是逐句判定「算不算约定」；
- 失败一律静默降级（只记日志），绝不阻塞对话主路径。
"""
from __future__ import annotations

import asyncio

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.logger import get_logger
from app.models.project_agent import AgentConversation, AgentMessage

logger = get_logger(__name__)

#: 单次摘要最多读多少条 user/assistant 消息（tool 消息一律排除）。
#: 20 条会漏掉长会话前半的约定（实测存在 53 条消息的会话），放宽到 60。
SUMMARY_MAX_MESSAGES = 60
#: 送入模型的最大字符数；从最新往回累加。60 条约合 1.2 万字符，单次调用仍可控。
SUMMARY_INPUT_MAX_CHARS = 12000
#: 写入 summary 的硬上限，防止模型越写越长吃掉后续注入预算。
SUMMARY_MAX_OUTPUT_CHARS = 600
#: 消息太少时没有可总结的内容，直接跳过，省一次模型调用。
SUMMARY_MIN_MESSAGES = 4

SUMMARY_PROMPT = """你在为一个 AI 小说创作助手整理「会话结论」。

请阅读下面的对话记录，输出这次会话已经谈定的内容，供之后的新会话参考。

规则：
1. 只写已经明确达成一致的内容；被否决的提议、仍在讨论的问题不要写成结论。
2. 每条一行，以「- 」开头，单条不超过 60 字。
3. 最多 8 条。信息不足就少写，不要凑数。
4. 不写过程、不写客套、不写「用户表示」这类转述。
5. 如果这次会话没有谈定任何事，只输出一个空字符串。
6. 只输出结论列表本身，不要标题、不要解释、不要代码块。"""


async def _load_transcript(session: AsyncSession, conversation_id: str) -> str:
    """取会话尾部消息，返回带角色标注的单行转录；不足最小条数时返回空串。"""
    messages = (
        await session.execute(
            select(AgentMessage)
            .where(
                AgentMessage.conversation_id == conversation_id,
                AgentMessage.role.in_(("user", "assistant")),
            )
            # created_at 在 SQLite 只有秒级精度，id 只作稳定 tie-break。
            .order_by(AgentMessage.created_at.desc(), AgentMessage.id.desc())
            .limit(SUMMARY_MAX_MESSAGES)
        )
    ).scalars().all()
    if len(messages) < SUMMARY_MIN_MESSAGES:
        return ""

    lines: list[str] = []
    used = 0
    for message in messages:  # 已按时间倒序
        content = " ".join((message.content or "").split())
        if len(content) < 8:
            continue
        label = "用户" if message.role == "user" else "助手"
        line = f"[{label}] {content}"
        if used + len(line) + 1 > SUMMARY_INPUT_MAX_CHARS and lines:
            break
        lines.append(line)
        used += len(line) + 1
    if not lines:
        return ""
    return "\n".join(reversed(lines))


def _clean_summary(raw: str) -> str:
    """只保留「- 」开头的结论行，剥掉 markdown 装饰，去重后按行截断。"""
    cleaned: list[str] = []
    seen: set[str] = set()
    for line in (raw or "").splitlines():
        line = line.strip()
        if not line.startswith("- "):
            continue
        claim = line[2:].strip().strip("*#").strip()
        if not claim or claim in seen:
            continue
        seen.add(claim)
        cleaned.append(f"- {claim}")
        if len(cleaned) >= 8:
            break
    # 按行累加到硬上限：宁可少一条结论，也不要把最后一条切成半句。
    result: list[str] = []
    used = 0
    for line in cleaned:
        if used + len(line) + 1 > SUMMARY_MAX_OUTPUT_CHARS:
            break
        result.append(line)
        used += len(line) + 1
    return "\n".join(result)


async def summarize_with(
    session: AsyncSession,
    ai_service,
    conversation_id: str,
) -> str:
    """用调用方提供的 session 与 AIService 生成并写入结论。

    供 `stream_chat` 在首回合同步兜底时复用：那里已经有请求态 session 与服务，
    再新建一套只会多一次连接，也看不到同一事务视图。
    """
    conversation = (
        await session.execute(
            select(AgentConversation).where(AgentConversation.id == conversation_id)
        )
    ).scalar_one_or_none()
    if conversation is None:
        return ""

    transcript = await _load_transcript(session, conversation_id)
    if not transcript:
        return ""

    response = await ai_service.generate_text(
        prompt=f"对话记录：\n{transcript}",
        system_prompt=SUMMARY_PROMPT,
        temperature=0.2,
        max_tokens=600,
        auto_mcp=False,
        handle_tool_calls=False,
    )
    summary = _clean_summary((response or {}).get("content") or "")
    if not summary:
        return summary

    conversation.summary = summary
    await session.commit()
    logger.info(
        f"会话结论已生成: conversation={conversation_id[:8]} chars={len(summary)}"
    )
    return summary


async def summarize_conversation(conversation_id: str, user_id: str) -> str:
    """生成并写入会话结论，返回写入的文本；失败返回空串。

    用独立 session：本函数由 `schedule_conversation_summary` 以 fire-and-forget
    方式调度，请求态 session 在 SSE 结束后已被关闭。
    """
    if not conversation_id or not user_id:
        return ""
    try:
        from app.api.settings import get_user_ai_service_from_db
        from app.database import get_engine

        engine = await get_engine(user_id)
        factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
        async with factory() as session:
            owned = (
                await session.execute(
                    select(AgentConversation.id).where(
                        AgentConversation.id == conversation_id,
                        AgentConversation.user_id == user_id,
                    )
                )
            ).scalar_one_or_none()
            if owned is None:
                return ""

            # 保留 db_session：`generate_text` 的模型门禁按 (user, provider, base_url,
            # model) 读结论缓存，db_session=None 会让实例被判为 unbound diagnostic
            # 而直通放行 —— 摘要路径反而绕开门禁。不要把这里改成 `service.db_session = None`。
            ai_service = await get_user_ai_service_from_db(user_id, session)
            return await summarize_with(session, ai_service, conversation_id)
    except Exception:  # noqa: BLE001 —— 摘要失败不得影响对话
        logger.warning(
            f"会话结论生成失败: conversation={conversation_id[:8]}", exc_info=True
        )
        return ""


#: 持有运行中的任务引用，避免被垃圾回收；按会话去重，重复调度直接跳过。
_PENDING_TASKS: dict[str, asyncio.Task] = {}


def schedule_conversation_summary(conversation_id: str, user_id: str) -> None:
    """异步调度一次摘要；调用方不需要 await，也不应依赖它完成。

    同一会话已有在途任务时直接跳过：两个新会话并发打开会各自判定上一会话
    「结论过期」，不去重就会重复调用模型。
    """
    if not conversation_id or not user_id:
        return
    existing = _PENDING_TASKS.get(conversation_id)
    if existing is not None and not existing.done():
        return
    task = asyncio.create_task(summarize_conversation(conversation_id, user_id))
    _PENDING_TASKS[conversation_id] = task
    task.add_done_callback(lambda _task: _PENDING_TASKS.pop(conversation_id, None))
