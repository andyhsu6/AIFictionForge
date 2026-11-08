"""代理环境变量归一化：兜住 httpx 对 NO_PROXY 中 IPv6 方括号的解析 bug。

现象（2026-11-08 实测）：进程环境里有
`NO_PROXY=127.0.0.1,localhost,::1,[::1]` 且任意 `*_PROXY` 变量存在时，
`httpx.AsyncClient()` 在构造阶段就抛
`httpx.InvalidURL: Invalid port: ':1]'`——因为 httpx 0.28.x 的
`get_environment_proxies()` 对已经是方括号形式的 IPv6 主机再拼一层，生成
`all://*[::1]` 这种非法挂载。于是任何走 httpx 的功能（AI 对话、模型探测、
封面生成……）在「创建客户端」这一步直接 500，错误信息与业务完全无关。

NO_PROXY 规范里 IPv6 写不写方括号语义相同，因此启动时去掉方括号即可，行为不变。
放在 `app/__init__` 里执行：无论 uvicorn 从哪个 shell（带不带代理环境）启动，
也无论测试如何导入 `app.*`，都会先归一化一次。
"""
from __future__ import annotations

import os

_NO_PROXY_KEYS = ("NO_PROXY", "no_proxy")


def normalize_no_proxy(value: str) -> str:
    """把 NO_PROXY 列表里形如 `[::1]` / `[::1]:8080` 的 IPv6 条目去掉方括号。"""
    hosts: list[str] = []
    for raw in value.split(","):
        host = raw.strip()
        if not host:
            continue
        if host.startswith("["):
            end = host.find("]")
            if end != -1:
                host = host[1:end] + host[end + 1:]
        hosts.append(host)
    return ",".join(hosts)


def sanitize_no_proxy_env() -> None:
    """原地归一化进程环境里的 NO_PROXY（幂等）。"""
    for key in _NO_PROXY_KEYS:
        value = os.environ.get(key)
        if not value:
            continue
        cleaned = normalize_no_proxy(value)
        if cleaned != value:
            os.environ[key] = cleaned
