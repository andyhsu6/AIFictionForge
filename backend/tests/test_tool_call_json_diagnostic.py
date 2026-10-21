"""issue #160：工具调用参数 JSON 解析失败必须留下可诊断证据。

背景：`propose_plan` 的参数解析失败时，落库的工具调用行 `arguments` 是 `{}`，
原始参数文本被丢弃，而那次 AI 调用的 `finish_reason=tool_calls`、输出 2.6k–5.4k
tokens。只有 `{}` 这一条痕迹时，无法区分「模型生成了畸形 JSON」与「网关/客户端
把 arguments 截断」——这是 #154/#156 家族排查的关键证据缺口。

这三条一起锁：合法输入不受影响（回归）、失败时日志存证（长度 + 结尾片段）、
原始文本不外泄到用户可见异常（异常会进 `工具参数需要修正：{exc}` 步骤内容）。
"""
import json
import logging

import pytest

from app.services import project_agent_service as service_module
from app.services.project_agent_service import ProjectAgentService

# 中性占位参数（禁止真实书名/人名）
VALID_ARGS = {"objective": "goal-placeholder", "steps": [{"tool": "get_project_stats"}]}
TRUNCATED_ARGS = '{"objective": "goal-placeholder", "steps": ['


def _warnings(caplog):
    return [
        record for record in caplog.records
        if record.name == service_module.logger.name and record.levelno == logging.WARNING
    ]


def test_valid_arguments_parse_without_warning(caplog):
    with caplog.at_level(logging.WARNING, logger=service_module.logger.name):
        name, arguments = ProjectAgentService._parse_tool_call({
            "function": {"name": "propose_plan", "arguments": json.dumps(VALID_ARGS)},
        })

    assert name == "propose_plan"
    assert arguments == VALID_ARGS
    assert _warnings(caplog) == [], "合法 JSON 不得产生 warning，否则日志失去信噪比"


def test_truncated_arguments_raise_value_error_without_raw_text(caplog):
    with caplog.at_level(logging.WARNING, logger=service_module.logger.name):
        with pytest.raises(ValueError) as excinfo:
            ProjectAgentService._parse_tool_call({
                "function": {"name": "propose_plan", "arguments": TRUNCATED_ARGS},
            })

    message = str(excinfo.value)
    assert "propose_plan" in message
    assert "参数长度" in message
    assert str(len(TRUNCATED_ARGS)) in message, "长度必须进消息，否则事后算不出原文规模"
    # 异常文案会拼进 `工具参数需要修正：{exc}` 这条用户可见步骤内容 ⇒ 不得含原文
    assert "objective" not in message
    assert "goal-placeholder" not in message, "原文里的值不得进用户可见错误"
    assert "steps" not in message, "原文里的键名同样算原文，不得外泄"
    # 前提自证：这段原文里确实带着上面断言"不出现"的那些片段
    assert "goal-placeholder" in TRUNCATED_ARGS and "objective" in TRUNCATED_ARGS


def test_truncated_arguments_logs_length_and_tail(caplog):
    with caplog.at_level(logging.WARNING, logger=service_module.logger.name):
        with pytest.raises(ValueError):
            ProjectAgentService._parse_tool_call({
                "function": {"name": "propose_plan", "arguments": TRUNCATED_ARGS},
            })

    warnings = _warnings(caplog)
    assert len(warnings) == 1, f"应恰好记一条 warning，实际 {[r.getMessage() for r in warnings]}"
    logged = warnings[0].getMessage()
    assert "propose_plan" in logged
    assert str(len(TRUNCATED_ARGS)) in logged
    # 结尾片段的 repr —— 能看出参数停在 `"steps": [` 这种半句处（截断）而非畸形
    assert repr(TRUNCATED_ARGS[-20:]) in logged or '"steps": [' in logged


def test_tail_is_bounded_to_last_160_chars(caplog):
    """结尾片段只取最后 160 字符：日志不该把整份参数（可能几千字符）灌进去。"""
    arguments = '{"objective": "' + "x" * 5000 + '", "steps": ['
    with caplog.at_level(logging.WARNING, logger=service_module.logger.name):
        with pytest.raises(ValueError):
            ProjectAgentService._parse_tool_call({
                "function": {"name": "propose_plan", "arguments": arguments},
            })

    logged = _warnings(caplog)[0].getMessage()
    # 结尾 160 字符 = 一长串 x + `", "steps": [`，故只能断言其中约 140 个 x
    assert ("x" * 140) in logged
    assert ("x" * 200) not in logged, "结尾片段未被截到 160 字符"
    assert str(len(arguments)) in logged


@pytest.mark.parametrize("arguments", ["{}", "", None])
def test_empty_arguments_still_parse_to_empty_dict(arguments):
    name, parsed = ProjectAgentService._parse_tool_call({
        "function": {"name": "get_project_stats", "arguments": arguments},
    })

    assert name == "get_project_stats"
    assert parsed == {}


def test_missing_arguments_key_still_parses_to_empty_dict():
    name, parsed = ProjectAgentService._parse_tool_call({
        "function": {"name": "get_project_stats"},
    })

    assert name == "get_project_stats"
    assert parsed == {}
