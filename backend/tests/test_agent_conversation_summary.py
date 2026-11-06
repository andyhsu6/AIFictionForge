"""会话结论摘要的纯函数与取数口径测试。

摘要生成本身需要真实/桩 AI 服务与独立引擎，这里只钉两件最容易回归的部分：
输出清洗（只保留 `- ` 条目）与转录截取（角色标注、最小条数、超长截断）。
"""
from datetime import datetime

import pytest

from app.services.agent_conversation_summary import (
    SUMMARY_INPUT_MAX_CHARS,
    SUMMARY_MIN_MESSAGES,
    _clean_summary,
    _load_transcript,
)

from tests.test_agent_user_history_context import (
    add_assistant_message,
    add_conversation,
    add_user_message,
    db_session,
    make_project,
)


def test_clean_summary_keeps_only_bullets_and_dedupes():
    raw = "结论如下：\n- 甲设定保持\n- 甲设定保持\n* 乙不应保留\n- 丙设定作废\n"
    assert _clean_summary(raw) == "- 甲设定保持\n- 丙设定作废"


def test_clean_summary_caps_at_eight_items():
    raw = "\n".join(f"- 第{i}条结论" for i in range(12))
    assert len(_clean_summary(raw).splitlines()) == 8


def test_clean_summary_returns_empty_for_prose_only():
    assert _clean_summary("这次没有谈定任何事情。") == ""


def test_clean_summary_strips_markdown_decorations():
    raw = "- **设定A保持**\n- ## 设定B作废\n- * 设定C待定"
    assert _clean_summary(raw) == "- 设定A保持\n- 设定B作废\n- 设定C待定"


def test_clean_summary_drops_whole_line_on_cap():
    """超长时整行丢弃，不把最后一条结论切成半句。"""
    from app.services.agent_conversation_summary import SUMMARY_MAX_OUTPUT_CHARS

    raw = "\n".join(f"- {'长' * 100}" for _ in range(8))
    out = _clean_summary(raw)
    assert len(out) <= SUMMARY_MAX_OUTPUT_CHARS
    assert out.splitlines()
    assert all(len(line) == 102 for line in out.splitlines())


@pytest.mark.anyio
async def test_load_transcript_returns_empty_below_min_messages(db_session):
    project = make_project()
    db_session.add(project)
    conv = add_conversation(db_session, project.id, "会话")
    await db_session.flush()
    for i in range(SUMMARY_MIN_MESSAGES - 1):
        add_user_message(db_session, conv.id, f"第{i}条足够长的内容", datetime(2026, 1, 1, 0, 0, i))
    await db_session.commit()

    assert await _load_transcript(db_session, conv.id) == ""


@pytest.mark.anyio
async def test_load_transcript_labels_roles_and_orders_chronologically(db_session):
    project = make_project()
    db_session.add(project)
    conv = add_conversation(db_session, project.id, "会话")
    await db_session.flush()
    add_user_message(db_session, conv.id, "先说的一条完整约定内容", datetime(2026, 1, 1, 0, 0, 1))
    add_assistant_message(db_session, conv.id, "后说的一条完整回复内容", datetime(2026, 1, 1, 0, 0, 2))
    add_user_message(db_session, conv.id, "再后来的一条完整内容", datetime(2026, 1, 1, 0, 0, 3))
    add_assistant_message(db_session, conv.id, "最后补充的一条回复内容", datetime(2026, 1, 1, 0, 0, 4))
    await db_session.commit()

    transcript = await _load_transcript(db_session, conv.id)

    assert transcript.index("[用户] 先说的一条完整约定内容") < transcript.index("[助手] 后说的一条完整回复内容")
    assert transcript.index("[助手] 后说的一条完整回复内容") < transcript.index("[用户] 再后来的一条完整内容")


@pytest.mark.anyio
async def test_load_transcript_drops_short_messages(db_session):
    project = make_project()
    db_session.add(project)
    conv = add_conversation(db_session, project.id, "会话")
    await db_session.flush()
    add_user_message(db_session, conv.id, "好的", datetime(2026, 1, 1, 0, 0, 1))
    add_user_message(db_session, conv.id, "嗯", datetime(2026, 1, 1, 0, 0, 2))
    add_user_message(db_session, conv.id, "这是一条足够长的讨论内容", datetime(2026, 1, 1, 0, 0, 3))
    add_assistant_message(db_session, conv.id, "这是一条足够长的回复内容", datetime(2026, 1, 1, 0, 0, 4))
    await db_session.commit()

    transcript = await _load_transcript(db_session, conv.id)

    assert "好的" not in transcript
    assert "这是一条足够长的讨论内容" in transcript


@pytest.mark.anyio
async def test_load_transcript_respects_input_cap(db_session):
    project = make_project()
    db_session.add(project)
    conv = add_conversation(db_session, project.id, "会话")
    await db_session.flush()
    big = "长" * (SUMMARY_INPUT_MAX_CHARS // 2)
    add_user_message(db_session, conv.id, big, datetime(2026, 1, 1, 0, 0, 1))
    add_assistant_message(db_session, conv.id, big, datetime(2026, 1, 1, 0, 0, 2))
    add_user_message(db_session, conv.id, big, datetime(2026, 1, 1, 0, 0, 3))
    add_assistant_message(db_session, conv.id, big, datetime(2026, 1, 1, 0, 0, 4))
    await db_session.commit()

    transcript = await _load_transcript(db_session, conv.id)

    assert len(transcript) <= SUMMARY_INPUT_MAX_CHARS + 64
