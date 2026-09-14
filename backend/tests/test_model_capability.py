"""上下文窗口**登记表提示**测试（需求 #55 步骤 3/4 后的定位）。

步骤 3 起，`_KNOWN_CONTEXT_WINDOWS` 只是「探测从哪个刻度开始」的提示，
不再参与任何接受/拒绝判定；步骤 4 起它也不再决定注入预算：
- 原先的三档 `resolve_context_budget_chars`（1M→0.6 / 128K–1M→0.3 / 小窗口→0.1）
  已整体删除，预算唯一来源是实发模型实测/显式声明的窗口，
  见 `tests/test_no_fallback_degradation.py`；
- 未登记的模型不再回退成一个保守窗口值，而是 `None`（「无提示」≠「32K」）。

本文件因此只钉两件事：登记表的**键匹配特异性**，以及未知模型返回 None。

测试值一律中性占位（模型名与 "chapter one body text"），不含任何导入原文、
角色人名或书名（AGENTS.md 原文数据脱敏硬约束）。
"""
import inspect

from app.services import ai_service as ai_service_module
from app.services.ai_service import detect_context_window


def test_detect_1m_context_window():
    assert detect_context_window("deepseek-v4-flash") >= 1000000
    assert detect_context_window("deepseek-v3") >= 1000000


def test_detect_128k_context_window():
    assert detect_context_window("claude-3-5-sonnet") >= 128000
    assert detect_context_window("gpt-4o") >= 128000


def test_detect_small_context_window():
    assert detect_context_window("gpt-3.5-turbo") < 128000


def test_unregistered_model_has_no_hint():
    """未登记 ⇒ None：诚实的「不知道」，不是伪装成 32K 的保守猜测。"""
    assert detect_context_window("unknown-model-xyz") is None
    assert detect_context_window(None) is None
    assert detect_context_window("") is None


def test_tiered_budget_resolver_is_gone():
    """按模型名分三档推预算的函数必须消失（留着就是降级逻辑复活）。"""
    assert not hasattr(ai_service_module, "resolve_context_budget_chars")


def test_no_conservative_window_fallback_constant():
    """源码级守卫：`detect_context_window` 里不得再出现固定回退窗口值（验收 grep 同口径）。"""
    source = inspect.getsource(ai_service_module.detect_context_window)
    assert "32768" not in source, "detect_context_window 仍带保守回退常量"
    assert "return None" in source
