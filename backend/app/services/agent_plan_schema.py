"""`propose_plan` 的 schema、白名单与产出校验（架构计划 §1「规划阶段」）。

纯函数层：不认识 registry、不碰数据库。规划期**不执行任何步骤**，
所以这里只做结构校验 + 工具白名单过滤；目标实体是否存在留给执行期 preview
（registry.execute 无条件先调 preview_handler，见 project_agent_operational_tools.py）。
"""
from __future__ import annotations

import re
from typing import Any

from app.services.project_agent_extended_tools import (
    FLAT_DATA_FIELDS as EXTENDED_FLAT_DATA_FIELDS,
)
from app.services.project_agent_operational_tools import (
    FLAT_DATA_FIELDS as OPERATIONAL_FLAT_DATA_FIELDS,
)
from app.services.project_agent_selectors import merge_flat_data_fields
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
    "写入类步骤要调用 manage_* 工具（manage_outline/manage_character/manage_chapter/"
    "manage_relationship/manage_organization/manage_foreshadow/manage_career/manage_writing_style），"
    "在 arguments 里给出 action，工具字段一律放在 arguments.data 对象内，不要把 data 字段摊在顶层。"
    '示例：{"id":"s1","tool":"manage_foreshadow","arguments":{"action":"update",'
    '"foreshadow_id":"<伏笔ID>","data":{"content":"新的伏笔内容"}}}。'
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


def tool_parameter_schemas(definitions: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """模型工具定义 -> {工具名: parameters}，步骤参数校验直接复用注册表 schema。"""
    schemas: dict[str, dict[str, Any]] = {}
    for item in definitions or []:
        function = item.get("function") if isinstance(item, dict) else None
        name = (function or {}).get("name")
        parameters = (function or {}).get("parameters")
        if isinstance(name, str) and name and isinstance(parameters, dict):
            schemas[name] = parameters
    return schemas


_PLAN_FLAT_DATA_FIELDS: dict[str, frozenset[str]] = {
    **EXTENDED_FLAT_DATA_FIELDS,
    **OPERATIONAL_FLAT_DATA_FIELDS,
}
_NO_FLAT_DATA_FIELDS: frozenset[str] = frozenset()


def _json_type_name(value: Any) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, int):
        return "integer"
    if isinstance(value, float):
        return "number"
    if isinstance(value, str):
        return "string"
    if isinstance(value, list):
        return "array"
    if isinstance(value, dict):
        return "object"
    return type(value).__name__


def _matches_json_type(value: Any, expected: str) -> bool:
    if expected == "object":
        return isinstance(value, dict)
    if expected == "array":
        return isinstance(value, list)
    if expected == "string":
        return isinstance(value, str)
    if expected == "boolean":
        return isinstance(value, bool)
    if expected == "integer":
        return isinstance(value, int) and not isinstance(value, bool)
    if expected == "number":
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    if expected == "null":
        return value is None
    return True


def _branch_error(branches: list[Any], path: str) -> str:
    required = [
        sub["required"][0]
        for sub in branches
        if isinstance(sub, dict)
        and set(sub) == {"required"}
        and isinstance(sub.get("required"), list)
        and len(sub["required"]) == 1
    ]
    label = path or "参数"
    if len(required) == len(branches):
        return f"{label} 必须提供 {', '.join(required)} 之一"
    return f"{label} 不满足 anyOf 条件"


def _scalar_error(value: Any, schema: dict[str, Any], label: str) -> str | None:
    if isinstance(value, str):
        if isinstance(schema.get("minLength"), int) and len(value) < schema["minLength"]:
            return f"{label} 长度不能少于 {schema['minLength']}"
        if isinstance(schema.get("maxLength"), int) and len(value) > schema["maxLength"]:
            return f"{label} 长度不能超过 {schema['maxLength']}"
        pattern = schema.get("pattern")
        if isinstance(pattern, str):
            try:
                if re.search(pattern, value) is None:
                    return f"{label} 不满足格式要求"
            except re.error:
                return None
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        minimum = schema.get("minimum")
        if isinstance(minimum, (int, float)) and value < minimum:
            return f"{label} 不能小于 {minimum}"
        maximum = schema.get("maximum")
        if isinstance(maximum, (int, float)) and value > maximum:
            return f"{label} 不能大于 {maximum}"
    return None


def _object_error(value: dict[str, Any], schema: dict[str, Any], path: str) -> str | None:
    properties = schema.get("properties")
    declared = properties if isinstance(properties, dict) else {}
    # 嵌套 data 的 required 是 action 作用域（RELATIONSHIP_DATA 的必填只在 create 成立），
    # 只校验顶层 required，避免把合法的 update/delete 步骤误判为不可执行。
    if not path:
        for field in schema.get("required") or []:
            if isinstance(field, str) and field not in value:
                return f"缺少必填字段 {field}"
    if schema.get("additionalProperties") is False:
        unknown = [key for key in value if key not in declared]
        if unknown:
            return f"不支持的字段 {f'{path}.{unknown[0]}' if path else unknown[0]}"
    for key, sub in declared.items():
        if key in value:
            problem = _schema_error(value[key], sub, f"{path}.{key}" if path else key)
            if problem:
                return problem
    return None


def _array_error(value: list[Any], schema: dict[str, Any], path: str) -> str | None:
    label = path or "参数"
    if isinstance(schema.get("minItems"), int) and len(value) < schema["minItems"]:
        return f"{label} 至少需要 {schema['minItems']} 项"
    if isinstance(schema.get("maxItems"), int) and len(value) > schema["maxItems"]:
        return f"{label} 最多允许 {schema['maxItems']} 项"
    items = schema.get("items")
    if isinstance(items, dict):
        for index, item in enumerate(value):
            problem = _schema_error(item, items, f"{path}[{index}]" if path else f"[{index}]")
            if problem:
                return problem
    return None


def _schema_error(value: Any, schema: Any, path: str = "") -> str | None:
    """返回第一个可读参数错误；schema 不支持的关键字一律跳过（fail-open）。"""
    if not isinstance(schema, dict):
        return None
    label = path or "参数"
    expected = schema.get("type")
    if isinstance(expected, str):
        if not _matches_json_type(value, expected):
            return f"{label} 类型必须是 {expected}，实际为 {_json_type_name(value)}"
    elif isinstance(expected, list) and expected:
        if not any(isinstance(item, str) and _matches_json_type(value, item)
                   for item in expected):
            allowed = "/".join(str(item) for item in expected)
            return f"{label} 类型必须是 {allowed}，实际为 {_json_type_name(value)}"
    if "enum" in schema and value not in schema["enum"]:
        return f"{label} 必须是 {', '.join(map(str, schema['enum']))} 之一"
    if "const" in schema and value != schema["const"]:
        return f"{label} 必须等于 {schema['const']!r}"
    problem = _scalar_error(value, schema, label)
    if problem:
        return problem
    if isinstance(value, dict):
        problem = _object_error(value, schema, path)
        if problem:
            return problem
    if isinstance(value, list):
        problem = _array_error(value, schema, path)
        if problem:
            return problem
    for sub in schema.get("allOf") or []:
        problem = _schema_error(value, sub, path)
        if problem:
            return problem
    branches = schema.get("anyOf")
    if isinstance(branches, list) and branches:
        if not any(_schema_error(value, branch, path) is None for branch in branches):
            return _branch_error(branches, path)
    alternatives = schema.get("oneOf")
    if isinstance(alternatives, list) and alternatives:
        if not any(_schema_error(value, branch, path) is None for branch in alternatives):
            return _branch_error(alternatives, path)
    condition = schema.get("if")
    if isinstance(condition, dict):
        branch = schema.get("then") if _schema_error(value, condition, path) is None else schema.get("else")
        if isinstance(branch, dict):
            problem = _schema_error(value, branch, path)
            if problem:
                return problem
    return None


def _step_arguments_error(
    step_id: str,
    tool: str,
    raw_arguments: dict[str, Any],
    schema: dict[str, Any],
    action: str,
) -> str | None:
    effective = dict(raw_arguments)
    if action and "action" not in effective:
        effective["action"] = action
    effective, moved = merge_flat_data_fields(
        effective, _PLAN_FLAT_DATA_FIELDS.get(tool, _NO_FLAT_DATA_FIELDS)
    )
    properties = schema.get("properties")
    declared = set(properties) if isinstance(properties, dict) else set()
    unknown = sorted(set(effective) - declared - moved)
    if unknown:
        return f"步骤 {step_id} 的 {tool} 参数无效：不支持的字段 {', '.join(unknown)}"
    canonical = {key: value for key, value in effective.items() if key in declared}
    problem = _schema_error(canonical, schema)
    if problem:
        return f"步骤 {step_id} 的 {tool} 参数无效：{problem}"
    return None


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


def validate_plan(
    arguments: Any,
    *,
    allowed_tools: set[str],
    tool_schemas: dict[str, dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """校验并归一化 `{objective, steps:[{id, tool, action, arguments, note}]}`。

    ``tool_schemas``（见 ``tool_parameter_schemas``）非空时，步骤 arguments 会先按
    工具的扁平字段白名单归一化（issue #94），再对照该工具声明的 parameters 校验；
    raw arguments 原样透传（runner 的 raw/validated 配对依赖这一点）。
    """
    if not isinstance(arguments, dict):
        raise PlanValidationError("计划必须是一个 JSON 对象")
    objective = _clean_text(arguments.get("objective"), field="objective", required=True)
    schemas = tool_schemas if isinstance(tool_schemas, dict) else {}

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

        schema = schemas.get(tool)
        if isinstance(schema, dict):
            problem = _step_arguments_error(step_id, tool, raw_arguments, schema, action)
            if problem:
                raise PlanValidationError(problem)

        steps.append({
            "id": step_id,
            "tool": tool,
            "action": action or None,
            "arguments": raw_arguments,
            "note": _clean_text(raw.get("note"), field="note", required=False),
        })

    return {"objective": objective, "steps": steps}
