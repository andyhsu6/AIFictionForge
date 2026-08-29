"""Security helpers for sessions and outbound URL validation."""
import base64
import hashlib
import hmac
import ipaddress
import json
import secrets
import socket
import time
from typing import Iterable
from urllib.parse import urlparse

from fastapi import HTTPException

from app.config import settings


def _session_secret() -> bytes:
    secret = (
        getattr(settings, "SESSION_SECRET_KEY", None)
        or getattr(settings, "session_secret_key", None)
        or settings.LINUXDO_CLIENT_SECRET
        or settings.LOCAL_AUTH_PASSWORD
        or settings.openai_api_key
    )
    if not secret:
        secret = "aistoryforge-development-session-secret"
    return str(secret).encode("utf-8")


def create_session_token(user_id: str, max_age_seconds: int) -> str:
    payload = {
        "uid": user_id,
        "exp": int(time.time()) + max_age_seconds,
        "nonce": secrets.token_urlsafe(16),
    }
    payload_bytes = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    payload_b64 = base64.urlsafe_b64encode(payload_bytes).rstrip(b"=").decode("ascii")
    signature = hmac.new(_session_secret(), payload_b64.encode("ascii"), hashlib.sha256).digest()
    signature_b64 = base64.urlsafe_b64encode(signature).rstrip(b"=").decode("ascii")
    return f"{payload_b64}.{signature_b64}"


def verify_session_token(token: str) -> str | None:
    if not token or "." not in token:
        return None
    payload_b64, signature_b64 = token.split(".", 1)
    expected = hmac.new(_session_secret(), payload_b64.encode("ascii"), hashlib.sha256).digest()
    try:
        provided = base64.urlsafe_b64decode(signature_b64 + "=" * (-len(signature_b64) % 4))
    except Exception:
        return None
    if not hmac.compare_digest(expected, provided):
        return None
    try:
        payload_raw = base64.urlsafe_b64decode(payload_b64 + "=" * (-len(payload_b64) % 4))
        payload = json.loads(payload_raw.decode("utf-8"))
    except Exception:
        return None
    if int(payload.get("exp", 0)) < int(time.time()):
        return None
    user_id = payload.get("uid")
    return user_id if isinstance(user_id, str) and user_id else None


def _is_forbidden_ip(ip: ipaddress._BaseAddress) -> bool:
    return any([
        ip.is_private,
        ip.is_loopback,
        ip.is_link_local,
        ip.is_multicast,
        ip.is_reserved,
        ip.is_unspecified,
    ])


def _is_always_blocked_ip(ip: ipaddress._BaseAddress) -> bool:
    """Even local-LLM allowlists must not reach metadata / multicast endpoints."""
    return any([
        ip.is_link_local,
        ip.is_multicast,
        ip.is_unspecified,
    ])


def _normalize_hostname(host: str) -> str:
    return host.lower().strip().rstrip(".")


def _parse_allowed_hosts(raw: Iterable[str] | str | None) -> set[str]:
    if raw is None:
        return set()
    if isinstance(raw, str):
        items = raw.split(",")
    else:
        items = list(raw)
    return {_normalize_hostname(item) for item in items if str(item).strip()}


def validate_public_http_url(
    raw_url: str,
    *,
    allowed_schemes: Iterable[str] = ("https", "http"),
    allow_private: bool = False,
    allowed_hosts: Iterable[str] | str | None = None,
) -> str:
    """Validate an outbound URL to reduce SSRF risk.

    By default only public HTTP(S) hosts are accepted. Local/private targets can
    be opted in for self-hosted LLM endpoints without disabling SSRF checks for
    MCP plugins or other callers.
    """
    if not raw_url or not isinstance(raw_url, str):
        raise HTTPException(status_code=400, detail="URL不能为空")

    parsed = urlparse(raw_url.strip())
    if parsed.scheme not in set(allowed_schemes):
        raise HTTPException(status_code=400, detail="仅支持 HTTP/HTTPS URL")
    if not parsed.hostname:
        raise HTTPException(status_code=400, detail="URL缺少主机名")
    if parsed.username or parsed.password:
        raise HTTPException(status_code=400, detail="URL不允许包含认证信息")

    host = _normalize_hostname(parsed.hostname)
    host_allowed = allow_private or host in _parse_allowed_hosts(allowed_hosts)

    if host in {"localhost", "localhost.localdomain"} and not host_allowed:
        raise HTTPException(status_code=400, detail="URL不允许指向本机地址")

    try:
        ip = ipaddress.ip_address(host)
        if _is_always_blocked_ip(ip):
            raise HTTPException(status_code=400, detail="URL不允许指向链路本地、组播或未指定地址")
        if _is_forbidden_ip(ip) and not host_allowed:
            raise HTTPException(status_code=400, detail="URL不允许指向内网或保留地址")
    except ValueError:
        try:
            infos = socket.getaddrinfo(
                host,
                parsed.port or (443 if parsed.scheme == "https" else 80),
                type=socket.SOCK_STREAM,
            )
        except socket.gaierror:
            raise HTTPException(status_code=400, detail="URL主机名无法解析")
        for info in infos:
            resolved_ip = ipaddress.ip_address(info[4][0])
            if _is_always_blocked_ip(resolved_ip):
                raise HTTPException(status_code=400, detail="URL解析到链路本地、组播或未指定地址")
            if _is_forbidden_ip(resolved_ip) and not host_allowed:
                raise HTTPException(status_code=400, detail="URL解析到内网或保留地址")

    return raw_url.strip().rstrip("/")


def validate_ai_http_url(raw_url: str) -> str:
    """Validate an AI provider base URL.

    Public endpoints stay subject to SSRF checks. Local Ollama / Docker
    `host.docker.internal` targets are allowed only when explicitly configured.
    """
    allow_private = bool(getattr(settings, "allow_private_ai_endpoints", False))
    allowed_hosts = getattr(settings, "allowed_ai_hosts", "") or ""
    try:
        return validate_public_http_url(
            raw_url,
            allow_private=allow_private,
            allowed_hosts=allowed_hosts,
        )
    except HTTPException as exc:
        detail = str(exc.detail or "")
        if exc.status_code == 400 and any(
            token in detail for token in ("本机", "内网", "链路本地")
        ):
            raise HTTPException(
                status_code=400,
                detail=(
                    f"{detail}。如需连接本地或 Docker 内网 LLM，"
                    "请设置 ALLOW_PRIVATE_AI_ENDPOINTS=true，"
                    "或把主机名加入 ALLOWED_AI_HOSTS（例如 host.docker.internal,127.0.0.1）。"
                ),
            ) from exc
        raise
