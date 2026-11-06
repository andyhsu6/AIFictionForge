"""issue #223：steps 形状错误的报错必须可操作。

真实故障里模型把 steps 数组写成了 JSON 字符串，服务端只回「steps 必须是非空数组」，
重试轮拿不到可操作信息，模型把上一轮的畸形序列化原样复制了一遍。这里钉住两条：
- 字符串形态给出「数组本身」的具体纠正；
- 空数组/非数组仍走原有的非空数组报错（不改变既有语义）。
"""
import pytest

from app.services.agent_plan_schema import PlanValidationError, validate_plan

ALLOWED = {"manage_foreshadow"}


def test_steps_as_json_string_gets_actionable_error():
    with pytest.raises(PlanValidationError) as exc:
        validate_plan(
            {"objective": "x", "steps": '[{"id":"s1","tool":"manage_foreshadow"}]'},
            allowed_tools=ALLOWED,
        )
    message = str(exc.value)
    assert "JSON 数组本身" in message
    assert "不要再序列化一次" in message


def test_empty_steps_keeps_the_existing_message():
    with pytest.raises(PlanValidationError) as exc:
        validate_plan({"objective": "x", "steps": []}, allowed_tools=ALLOWED)
    assert str(exc.value) == "steps 必须是非空数组"


def test_missing_steps_keeps_the_existing_message():
    with pytest.raises(PlanValidationError) as exc:
        validate_plan({"objective": "x"}, allowed_tools=ALLOWED)
    assert str(exc.value) == "steps 必须是非空数组"


def test_list_steps_still_validates_normally():
    plan = validate_plan(
        {
            "objective": "x",
            "steps": [
                {
                    "id": "s1",
                    "tool": "manage_foreshadow",
                    "action": "update",
                    "arguments": {"foreshadow_id": "abc", "data": {"content": "c"}},
                }
            ],
        },
        allowed_tools=ALLOWED,
    )
    assert plan["steps"][0]["id"] == "s1"
