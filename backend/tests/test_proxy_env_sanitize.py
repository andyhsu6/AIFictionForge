"""httpx NO_PROXY IPv6 方括号 bug 的回归（issue #259）。

环境里有 `NO_PROXY=...,[::1]` + 任意 `*_PROXY` 时，httpx 0.28.x 会把它拼成
`all://*[::1]` 并在 `AsyncClient()` 构造阶段抛 `InvalidURL: Invalid port ':1]'`，
表现为所有走 httpx 的功能（AI 对话等）直接 500。`app` 包初始化时去方括号归一化。
"""
import os

import pytest

from app.core.proxy_env import normalize_no_proxy, sanitize_no_proxy_env


def test_normalize_strips_ipv6_brackets():
    assert normalize_no_proxy("127.0.0.1,localhost,::1,[::1]") == "127.0.0.1,localhost,::1,::1"
    # 带端口的方括号 IPv6 不能简单去括号（::1:8080 是另一个地址）；
    # httpx 对含 :// 的条目原样采用，这是无歧义写法。
    assert normalize_no_proxy("[::1]:8080, example.com") == "all://[::1]:8080,example.com"
    assert normalize_no_proxy(" example.com , ") == "example.com"
    assert normalize_no_proxy("") == ""


def test_sanitize_env_is_idempotent(monkeypatch):
    monkeypatch.setenv("NO_PROXY", "127.0.0.1,[::1]")
    monkeypatch.setenv("no_proxy", "[::1]")
    sanitize_no_proxy_env()
    assert os.environ["NO_PROXY"] == "127.0.0.1,::1"
    assert os.environ["no_proxy"] == "::1"
    sanitize_no_proxy_env()
    assert os.environ["NO_PROXY"] == "127.0.0.1,::1"


def test_httpx_client_constructs_with_bracketed_no_proxy(monkeypatch):
    """真回归点：归一化后 httpx 客户端必须能构造（修复前这里抛 InvalidURL）。"""
    import httpx

    monkeypatch.setenv("NO_PROXY", "127.0.0.1,localhost,::1,[::1]")
    monkeypatch.setenv("no_proxy", "127.0.0.1,localhost,::1,[::1]")
    monkeypatch.setenv("HTTP_PROXY", "http://127.0.0.1:10808")
    monkeypatch.setenv("https_proxy", "http://127.0.0.1:10808")

    # 前置自证：不归一化时 httpx 确实会炸，否则本用例可能空转。
    # 注意：该断言依赖 httpx 0.28.x 的 NO_PROXY 解析实现；升级 httpx 后
    # 若上游修复了方括号处理，应改为「构造成功」而不是删掉前置自证。
    with pytest.raises(httpx.InvalidURL):
        httpx.AsyncClient(trust_env=True)

    sanitize_no_proxy_env()
    client = httpx.AsyncClient(trust_env=True)
    assert client is not None

def test_httpx_client_constructs_with_ipv6_port_entry(monkeypatch):
    """带端口的方括号 IPv6：归一化为 all://[ipv6]:port，httpx 可构造且挂载精确匹配。"""
    import httpx

    monkeypatch.setenv("NO_PROXY", "[::1]:8080")
    monkeypatch.setenv("no_proxy", "[::1]:8080")
    monkeypatch.setenv("HTTP_PROXY", "http://127.0.0.1:10808")

    sanitize_no_proxy_env()
    assert os.environ["NO_PROXY"] == "all://[::1]:8080"

    client = httpx.AsyncClient(trust_env=True)
    patterns = [getattr(pattern, "pattern", str(pattern)) for pattern in client._mounts]
    assert "all://[::1]:8080" in patterns
