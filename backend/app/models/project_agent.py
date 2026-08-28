"""项目智能体会话、消息与工具调用模型。"""
import uuid

from sqlalchemy import Boolean, Column, DateTime, ForeignKey, Integer, JSON, String, Text
from sqlalchemy.sql import func

from app.database import Base


class AgentConversation(Base):
    __tablename__ = "agent_conversations"

    id = Column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    user_id = Column(String(100), nullable=False, index=True)
    project_id = Column(
        String(36),
        ForeignKey("projects.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    title = Column(String(200), nullable=False, default="新对话")
    summary = Column(Text)
    status = Column(String(20), nullable=False, default="active")
    last_message_at = Column(DateTime, server_default=func.now(), nullable=False)
    created_at = Column(DateTime, server_default=func.now(), nullable=False)
    updated_at = Column(
        DateTime,
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )


class AgentMessage(Base):
    __tablename__ = "agent_messages"

    id = Column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    conversation_id = Column(
        String(36),
        ForeignKey("agent_conversations.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    role = Column(String(20), nullable=False)
    content = Column(Text, nullable=False, default="")
    model = Column(String(100))
    prompt_tokens = Column(Integer)
    completion_tokens = Column(Integer)
    tool_calls = Column(Text)
    tool_call_id = Column(String(36), index=True)
    created_at = Column(DateTime, server_default=func.now(), nullable=False)


class AgentToolCall(Base):
    __tablename__ = "agent_tool_calls"

    id = Column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    conversation_id = Column(
        String(36),
        ForeignKey("agent_conversations.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    message_id = Column(
        String(36),
        ForeignKey("agent_messages.id", ondelete="SET NULL"),
        nullable=True,
    )
    user_id = Column(String(100), nullable=False, index=True)
    project_id = Column(String(36), nullable=False, index=True)
    tool_name = Column(String(100), nullable=False)
    arguments = Column(JSON, nullable=False, default=dict)
    risk_level = Column(Integer, nullable=False, default=0)
    requires_confirmation = Column(Boolean, nullable=False, default=False)
    status = Column(String(30), nullable=False, default="proposed", index=True)
    preview = Column(JSON)
    result = Column(JSON)
    before_snapshot = Column(JSON)
    after_snapshot = Column(JSON)
    error_message = Column(Text)
    confirmed_at = Column(DateTime)
    executed_at = Column(DateTime)
    created_at = Column(DateTime, server_default=func.now(), nullable=False)


class AgentExecutionStep(Base):
    __tablename__ = "agent_execution_steps"

    id = Column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    conversation_id = Column(
        String(36),
        ForeignKey("agent_conversations.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    user_message_id = Column(
        String(36),
        ForeignKey("agent_messages.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    assistant_message_id = Column(
        String(36),
        ForeignKey("agent_messages.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    tool_call_id = Column(
        String(36),
        ForeignKey("agent_tool_calls.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    sequence = Column(Integer, nullable=False, default=0)
    step_type = Column(String(30), nullable=False)
    category = Column(String(30), nullable=False)
    title = Column(String(200), nullable=False)
    content = Column(Text)
    status = Column(String(30), nullable=False, default="running")
    detail = Column(JSON)
    created_at = Column(DateTime, server_default=func.now(), nullable=False)
    updated_at = Column(
        DateTime,
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )
