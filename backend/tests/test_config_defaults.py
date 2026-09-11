"""回归测试：锁定端口默认值为 8008/5173，防止 8000 遗留默认值回潮。

与本地 .env 无关：显式 _env_file=None 并清除相关环境变量。
"""
from urllib.parse import urlparse

from app.config import Settings
from app.services import oauth_service


def _clean_settings(monkeypatch) -> Settings:
    for var in ("APP_PORT", "CORS_ORIGINS", "FRONTEND_URL", "LINUXDO_REDIRECT_URI"):
        monkeypatch.delenv(var, raising=False)
    return Settings(_env_file=None)


def test_app_port_default_is_8008(monkeypatch):
    settings = _clean_settings(monkeypatch)
    assert settings.app_port == 8008


def test_cors_origins_default_ports(monkeypatch):
    settings = _clean_settings(monkeypatch)
    assert settings.cors_origins, "cors_origins 默认值不应为空"
    for origin in settings.cors_origins:
        port = urlparse(origin).port
        assert port in (8008, 5173), f"cors origin 指向非法端口 {port}: {origin}"


def test_frontend_url_default_is_8008(monkeypatch):
    settings = _clean_settings(monkeypatch)
    assert settings.FRONTEND_URL == "http://localhost:8008"


def test_oauth_default_redirect_uri_uses_8008(monkeypatch):
    settings = _clean_settings(monkeypatch)
    assert settings.LINUXDO_REDIRECT_URI is None
    monkeypatch.setattr(oauth_service, "settings", settings)
    service = oauth_service.LinuxDOOAuthService()
    assert service.redirect_uri == "http://localhost:8008/api/auth/callback"
