"""`propose_plan` 的 schema、白名单与产出校验（架构计划 §1「规划阶段」）。

纯函数层：不认识 registry、不碰数据库。规划期**不执行任何步骤**，
所以这里只做结构校验 + 工具白名单过滤；目标实体是否存在留给执行期 preview
（registry.execute 无条件先调 preview_handler，见 project_agent_operational_tools.py）。
"""
from __future__ import annotations

from typing import Any

from app.services.task_resources import AGENT_TASK_ACTION_TYPES

PROPOSE_PLAN_TOOL_NAME = "propose_plan"
MAX_PLAN_STEPS = 12

PROPOSE_PLAN_TOOL_DESCRIPTION = (
    "提交一份多步执行计划并结束本轮回答。这是终止型规划工具：调用它不会执行任何步骤，"
    "计划会在用户批准后由后台执行器逐步运行。只能调用一次，且必须在你已经用只读工具"
    "查清必要信息之后才调用。每一步只能是本回合可用工具白名单内的工具。"
    "跨步引用只允许执行期可解析的既有标识（chapter_number、outline.order_index、批次内序号），"
    "禁止引用前序步骤返回体里才会出现的新 ID。"
    "tool 为 start_project_task 的步骤必须给出 action，且 action 只能是以下值之一："
    f"{', '.join(sorted(AGENT_TASK_ACTION_TYPES))}。"
)

# 架构计划 §1：这两个工具要求模型自己给出 JSON 正文（SYSTEM_PROMPT 规则 8 禁止臆造），
# 放进计划等于让模型编数据 ⇒ 从 schema 排除。
EXCLUDED_PLAN_TOOLS = frozenset({"import_outlines_json", "import_characters_json"})

# 需要 action 才能定位落库任务表的工具（PR-2b 用 AGENT_TASK_ACTION_TYPES 反查 task_type）。
ACTION_BEARING_TOOLS = frozenset({"start_project_task"})

# action 的枚举与 validate_plan 同源（AGENT_TASK_ACTION_TYPES）：新增 action 只改
# task_resources.py，模型看到的 schema 与服务端校验器不可能各自漂移。
_STEP_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "id": {"type": "string", "minLength": 1, "pattern": r".*\S.*"},
        "tool": {
            "type": "string",
            "minLength": 1,
            "pattern": r".*\S.*",
            "description": "步骤要使用的工具名，必须是本回合工具白名单内的工具。",
        },
        "action": {
            "type": "string",
            "enum": sorted(AGENT_TASK_ACTION_TYPES),
            "description": (
                "步骤动作：仅当 tool 为 start_project_task 时必须提供，"
                "且只能取枚举值之一；其他工具的步骤请省略本字段。"
            ),
        },
        "arguments": {"type": "object"},
        "note": {"type": ["string", "null"]},
    },
    "required": ["id", "tool"],
    "additionalProperties": False,
}

PLAN_TOOL_PARAMETERS: dict[str, Any] = {
    "type": "object",
    "properties": {
        "objective": {"type": "string", "minLength": 1, "pattern": r".*\S.*"},
        "steps": {
            "type": "array",
            "minItems": 1,
            "maxItems": MAX_PLAN_STEPS,
            "items": _STEP_SCHEMA,
        },
    },
    "required": ["objective", "steps"],
    "additionalProperties": False,
}


class PlanValidationError(ValueError):
    """产出不是合法计划。消息面向模型可读，会作为服务端纠正消息回喂。"""


# 只有「把 tool_choice 真的写进请求 payload」的 provider 才进这个白名单。
# 逐个客户端实测（2026-09-13 对码）：
#   openai_client._build_payload: payload["tool_choice"] = tool_choice  ⇒ 进 payload
#   anthropic_client.chat_completion: required ⇒ kwargs["tool_choice"] = {"type": "any"}
#   gemini_client: 收下 tool_choice 形参，构造 payload 时**从不使用** ⇒ 静默空转
# （normalize_provider 把 mumu/commandcode 等渠道别名归一到 "openai"，所以
#   AIService.api_provider 实际只有 openai / anthropic / gemini 三种取值。）
# Gemini 故意排除：它靠 ① 收窄工具 + ③ 产出校验有界重试兜住强制产出（架构计划 §1 定案）。
REQUIRED_TOOL_CHOICE_PROVIDERS: frozenset[str] = frozenset({"openai", "anthropic"})


def provider_supports_required_tool_choice(api_provider: str | None) -> bool:
    """未知/未列 provider 一律 False（fail-closed），由 ①+③ 兜住强制产出。"""
    if not isinstance(api_provider, str):
        return False
    return api_provider.strip().lower() in REQUIRED_TOOL_CHOICE_PROVIDERS


def plannable_tool_names(definitions: list[dict[str, Any]]) -> set[str]:
    """从本轮 available_tools（模型工具定义）挑出可进计划的工具名。"""
    names: set[str] = set()
    for item in definitions or []:
        name = (item.get("function") or {}).get("name")
        if isinstance(name, str) and name:
            names.add(name)
    names.discard(PROPOSE_PLAN_TOOL_NAME)
    return names - set(EXCLUDED_PLAN_TOOLS)


def _clean_text(value: Any, *, field: str, required: bool) -> str:
    if value is None:
        if required:
            raise PlanValidationError(f"步骤缺少 {field}")
        return ""
    if not isinstance(value, str):
        raise PlanValidationError(f"{field} 必须是字符串")
    text = value.strip()
    if not text and required:
        raise PlanValidationError(f"{field} 不能为空")
    return text


def validate_plan(arguments: Any, *, allowed_tools: set[str]) -> dict[str, Any]:
    """校验并归一化 `{objective, steps:[{id, tool, action, arguments, note}]}`。"""
    if not isinstance(arguments, dict):
        raise PlanValidationError("计划必须是一个 JSON 对象")
    objective = _clean_text(arguments.get("objective"), field="objective", required=True)

    raw_steps = arguments.get("steps")
    if not isinstance(raw_steps, list) or not raw_steps:
        raise PlanValidationError("steps 必须是非空数组")
    if len(raw_steps) > MAX_PLAN_STEPS:
        raise PlanValidationError(f"steps 数量 {len(raw_steps)} 超过上限 {MAX_PLAN_STEPS}")

    seen: set[str] = set()
    steps: list[dict[str, Any]] = []
    for index, raw in enumerate(raw_steps, start=1):
        if not isinstance(raw, dict):
            raise PlanValidationError(f"第 {index} 步必须是 JSON 对象")
        step_id = _clean_text(raw.get("id"), field=f"第 {index} 步的 id", required=True)
        if step_id in seen:
            raise PlanValidationError(f"步骤 id 重复：{step_id}")
        seen.add(step_id)

        tool = _clean_text(raw.get("tool"), field=f"步骤 {step_id} 的 tool", required=True)
        if tool in EXCLUDED_PLAN_TOOLS:
            raise PlanValidationError(f"工具 {tool} 不允许出现在计划中：它需要模型自备 JSON 正文")
        if tool not in allowed_tools:
            raise PlanValidationError(
                f"步骤 {step_id} 引用了本回合未启用的工具：{tool}。"
                f"可用工具：{', '.join(sorted(allowed_tools)) or '（无）'}"
            )

        raw_arguments = raw.get("arguments")
        if raw_arguments is None:
            raw_arguments = {}
        if not isinstance(raw_arguments, dict):
            raise PlanValidationError(f"步骤 {step_id} 的 arguments 必须是 JSON 对象")

        action = _clean_text(raw.get("action"), field=f"步骤 {step_id} 的 action", required=False)
        if tool in ACTION_BEARING_TOOLS:
            if not action:
                raise PlanValidationError(f"步骤 {step_id} 的 {tool} 必须给出 action")
            if action not in AGENT_TASK_ACTION_TYPES:
                raise PlanValidationError(
                    f"步骤 {step_id} 使用了未知 action：{action}。"
                    f"允许：{', '.join(sorted(AGENT_TASK_ACTION_TYPES))}"
                )

        steps.append({
            "id": step_id,
            "tool": tool,
            "action": action or None,
            "arguments": raw_arguments,
            "note": _clean_text(raw.get("note"), field="note", required=False),
        })

    return {"objective": objective, "steps": steps}
