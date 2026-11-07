"""计划契约一致性守卫。

本次会话连续踩到同一类问题：运行期有必填要求、提案期没有对照物；或者
`data` schema 是裸对象，模型只能靠猜字段名（实测编出过根本不存在的字段）。

这里守住最容易回归、又能静态检查的那一半：**所有写工具的 `data` 必须可读**——
至少有 `description` 或 `properties`，不能是 `{"type": "object"}` 了事。
它不能替代运行期/提案期的动态对照，但能挡住"新工具复制粘贴裸 schema"这一路径。
"""
from __future__ import annotations

from app.services.project_agent_extended_tools import EXTENDED_TOOL_SPECS
from app.services.project_agent_operational_tools import OPERATIONAL_TOOL_SPECS


def _write_specs() -> list[dict]:
    return [
        spec
        for spec in [*EXTENDED_TOOL_SPECS, *OPERATIONAL_TOOL_SPECS]
        if spec.get("risk_level")
    ]


def test_write_tool_data_schemas_are_documented():
    undocumented: list[str] = []
    for spec in _write_specs():
        data = (spec.get("parameters") or {}).get("properties", {}).get("data")
        if data is None:
            continue
        if data.get("description") or data.get("properties"):
            continue
        undocumented.append(spec["name"])

    assert not undocumented, (
        "这些写工具的 data schema 既没有 description 也没有 properties，"
        f"模型只能靠猜字段名：{sorted(undocumented)}"
    )


def test_write_tool_names_are_unique():
    """同名工具分别注册在两张表里时，参数契约会各自漂移。"""
    names = [spec["name"] for spec in _write_specs()]
    duplicates = sorted({name for name in names if names.count(name) > 1})
    assert not duplicates, f"写工具名重复注册：{duplicates}"
