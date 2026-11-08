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
    assert normalize_no_proxy("[::1]:8080, example.com") == "::1:8080,example.com"
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

    with pytest.raises(httpx.InvalidURL):
        # 前置自证：不归一化时 httpx 确实会炸，否则本用例可能空转。
        httpx.AsyncClient(trust_env=True)

    sanitize_no_proxy_env()
    client = httpx.AsyncClient(trust_env=True)
    assert client is not None
