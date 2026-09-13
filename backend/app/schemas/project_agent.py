"""灵创创作助手 API 模型。"""
from datetime import datetime
from typing import Any, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field


class AgentConversationCreate(BaseModel):
    title: Optional[str] = Field(None, max_length=200)


class AgentConversationResponse(BaseModel):
    id: str
    user_id: str
    project_id: str
    title: str
    summary: Optional[str] = None
    status: str
    last_message_at: datetime
    created_at: datetime
    updated_at: datetime

    model_config = ConfigDict(from_attributes=True)


class AgentMessageResponse(BaseModel):
    id: str
    conversation_id: str
    role: Literal["user", "assistant", "tool", "system"]
    content: str
    model: Optional[str] = None
    prompt_tokens: Optional[int] = None
    completion_tokens: Optional[int] = None
    created_at: datetime

    model_config = ConfigDict(from_attributes=True)


class AgentToolCallResponse(BaseModel):
    id: str
    conversation_id: str
    message_id: Optional[str] = None
    tool_name: str
    arguments: dict[str, Any]
    risk_level: int
    requires_confirmation: bool
    status: str
    preview: Optional[dict[str, Any]] = None
    result: Optional[dict[str, Any]] = None
    error_message: Optional[str] = None
    created_at: datetime

    model_config = ConfigDict(from_attributes=True)


class AgentExecutionStepResponse(BaseModel):
    id: str
    conversation_id: str
    user_message_id: Optional[str] = None
    assistant_message_id: Optional[str] = None
    tool_call_id: Optional[str] = None
    sequence: int
    step_type: str
    category: str
    title: str
    content: Optional[str] = None
    status: str
    detail: Optional[dict[str, Any]] = None
    created_at: datetime
    updated_at: datetime

    model_config = ConfigDict(from_attributes=True)


class AgentConversationDetail(AgentConversationResponse):
    messages: list[AgentMessageResponse] = Field(default_factory=list)
    tool_calls: list[AgentToolCallResponse] = Field(default_factory=list)
    execution_steps: list[AgentExecutionStepResponse] = Field(default_factory=list)


class AgentChatRequest(BaseModel):
    conversation_id: Optional[str] = None
    message: str = Field(..., min_length=1, max_length=20000)
    page_context: dict[str, Any] = Field(default_factory=dict)
    auto_approve: bool = False
    # PR-2a：规划回合开关。默认 False ⇒ 发给模型的工具集与逐轮行为与 PR-1 逐字一致；
    # True 时收口轮只提供终止型工具 propose_plan（只产计划、不执行）。
    plan_mode: bool = False


class AgentToolDecisionResponse(BaseModel):
    success: bool
    message: str
    tool_call: AgentToolCallResponse
    resources: list[str] = Field(default_factory=list)


class AgentPlanApprovalRequest(BaseModel):
    """计划卡的勾选结果：None = 全部步骤；给了 id 就必须全部命中。

    `extra="forbid"` 是硬要求而不是洁癖：字段名打错时 Pydantic 默认**静默丢弃**，
    selected_step_ids 落在默认值 None ⇒ 等价于"批准全部步骤"，用户以为只跑了勾的
    那一步。未知键直接 422。
    """

    model_config = ConfigDict(extra="forbid")

    selected_step_ids: Optional[list[str]] = None


class AgentPlanApprovalResponse(BaseModel):
    tool_call_id: str = Field(..., max_length=36)
    plan_task_id: str = Field(..., max_length=36)
    status: str = Field(..., max_length=30)
    steps_total: int
