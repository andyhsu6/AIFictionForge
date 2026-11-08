"""代理环境变量归一化：兜住 httpx 对 NO_PROXY 中 IPv6 方括号的解析 bug。

现象（2026-11-08 实测）：进程环境里有
`NO_PROXY=127.0.0.1,localhost,::1,[::1]` 且任意 `*_PROXY` 变量存在时，
`httpx.AsyncClient()` 在构造阶段就抛
`httpx.InvalidURL: Invalid port: ':1]'`——因为 httpx 0.28.x 的
`get_environment_proxies()` 对已经是方括号形式的 IPv6 主机再拼一层，生成
`all://*[::1]` 这种非法挂载。于是任何走 httpx 的功能（AI 对话、模型探测、
封面生成……）在「创建客户端」这一步直接 500，错误信息与业务完全无关。

NO_PROXY 规范里 IPv6 写不写方括号语义相同，因此启动时归一化即可：无端口条目
去掉方括号（httpx 会重新加回），带端口条目改写成 httpx 接受且无歧义的
`all://[ipv6]:port` 形式（issue #259 复审）。
放在 `app/__init__` 里执行：无论 uvicorn 从哪个 shell（带不带代理环境）启动，
也无论测试如何导入 `app.*`，都会先归一化一次。
"""
from __future__ import annotations

import os

_NO_PROXY_KEYS = ("NO_PROXY", "no_proxy")


def _normalize_entry(host: str) -> str:
    """归一化单个 NO_PROXY 条目里的方括号 IPv6。

    - `[::1]` -> `::1`：httpx 会重新加方括号并生成合法挂载；
    - `[::1]:8080` -> `all://[::1]:8080`：带端口的方括号条目**不能**简单去括号，
      否则 `::1:8080` 会被当成另一个合法 IPv6 地址（端口被吞、目标也会变）。
      httpx 对含 `://` 的条目原样采用，这是唯一无歧义的表示。
    """
    if not host.startswith("["):
        return host
    end = host.find("]")
    if end == -1:
        return host
    inner = host[1:end]
    rest = host[end + 1:]
    if not rest:
        return inner
    if rest.startswith(":"):
        return f"all://[{inner}]{rest}"
    return inner + rest


def normalize_no_proxy(value: str) -> str:
    """把 NO_PROXY 列表里的方括号 IPv6 条目归一化为 httpx 可接受的写法。"""
    hosts: list[str] = []
    for raw in value.split(","):
        host = raw.strip()
        if host:
            hosts.append(_normalize_entry(host))
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
