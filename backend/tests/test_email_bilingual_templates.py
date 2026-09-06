"""邮箱双语模板测试（i18n todo13 part 4 / issue #27）。

覆盖：
- zh byte-identity：lang='zh'（及缺省）时三场景 + 未知场景兜底的 (subject, text_body,
  html_body) 与转换前 f-string 拼装结果逐字节一致（legacy 组装逻辑以原样拷贝内嵌于
  本文件的 _legacy_zh_mail，源为转换前 auth.py:288-321）。
- en 变体：同结构同变量（code / ttl_minutes 均插值），断言显著英文子串且不含中文。
- 语言选择：仅 Settings.preferences.language == 'en' → en；用户缺失（register 场景
  发送时尚未注册）/ 无 Settings 行 / preferences 缺失或非法 JSON → 默认 zh。
- 端到端：直接调用 send_email_verification_code 协程，patch 掉 SMTP 发送捕获邮件内容。

测试直接调用 endpoint/内部协程（绕过路由），DB 用临时文件 SQLite。
"""
import json
import os
import uuid

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import app.api.auth as auth_module
from app.api.auth import (
    EmailSendCodeRequest,
    _build_verification_mail_content,
    _resolve_recipient_language,
    send_email_verification_code,
)
from app.database import Base
from app.models.settings import Settings
from app.models.user import User as UserModel

CODE = "123456"
TTL = 10

RUNTIME = {
    "email_auth_enabled": True,
    "email_register_enabled": True,
    "verification_code_ttl_minutes": TTL,
    "verification_resend_interval_seconds": 60,
    "smtp_host": "smtp.example.com",
    "smtp_port": 465,
    "smtp_username": "noreply@example.com",
    "smtp_password": "secret",
    "smtp_use_tls": False,
    "smtp_use_ssl": True,
    "smtp_from_email": "noreply@example.com",
    "smtp_from_name": "AIFictionForge",
}


def _legacy_zh_mail(scene_title: str, scene_desc: str, code: str, ttl_minutes: int) -> tuple[str, str, str]:
    """转换前 auth.py _build_verification_mail_content 的 zh 拼装逻辑原样拷贝（byte-identity 基准）。"""
    subject = f"AIFictionForge {scene_title}"
    text_body = (
        f"{scene_desc}\n\n"
        f"你的验证码是：{code}\n"
        f"有效期：{ttl_minutes} 分钟\n\n"
        f"如果这不是你的操作，请忽略本邮件。"
    )
    html_body = f"""
    <div style="font-family: Arial, PingFang SC, Microsoft YaHei, sans-serif; line-height: 1.8; color: #1f2937;">
      <h2 style="margin-bottom: 16px;">AIFictionForge {scene_title}</h2>
      <p>{scene_desc}</p>
      <p>你的验证码为：</p>
      <div style="display: inline-block; padding: 10px 18px; background: #eff6ff; border: 1px solid #bfdbfe; border-radius: 8px; font-size: 28px; font-weight: 700; letter-spacing: 4px; color: #2563eb;">
        {code}
      </div>
      <p style="margin-top: 16px;">有效期：{ttl_minutes} 分钟</p>
      <p>如果这不是你的操作，请忽略本邮件。</p>
    </div>
    """
    return subject, text_body, html_body


LEGACY_ZH_SCENES = {
    "register": ("邮箱注册验证码", "欢迎注册 AIFictionForge。"),
    "login": ("邮箱登录验证码", "你正在使用邮箱验证码登录 AIFictionForge。"),
    "reset_password": ("重置密码验证码", "你正在重置 AIFictionForge 账号密码。"),
}


# ---------------------------------------------------------------------------
# (a) zh byte-identity
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("scene", ["register", "login", "reset_password"])
def test_zh_byte_identity_per_scene(scene):
    title, desc = LEGACY_ZH_SCENES[scene]
    expected = _legacy_zh_mail(title, desc, CODE, TTL)
    assert _build_verification_mail_content(scene, CODE, TTL, lang="zh") == expected


def test_zh_byte_identity_unknown_scene_fallback():
    expected = _legacy_zh_mail("邮箱验证码", "你正在进行邮箱身份验证。", CODE, TTL)
    assert _build_verification_mail_content("unknown_scene", CODE, TTL, lang="zh") == expected


def test_zh_byte_identity_lang_omitted_is_zh():
    """不传 lang（旧调用形状）与 lang='zh' 完全一致 —— 向后兼容。"""
    assert _build_verification_mail_content("login", CODE, TTL) == \
        _build_verification_mail_content("login", CODE, TTL, lang="zh")


def test_zh_text_body_exact_bytes():
    """独立字面量锚点：login 场景 text_body 逐字节断言（不经过 legacy helper 推导）。"""
    _, text_body, _ = _build_verification_mail_content("login", CODE, TTL, lang="zh")
    assert text_body == (
        "你正在使用邮箱验证码登录 AIFictionForge。\n\n"
        "你的验证码是：123456\n"
        "有效期：10 分钟\n\n"
        "如果这不是你的操作，请忽略本邮件。"
    )


# ---------------------------------------------------------------------------
# (b) en 变体：同结构同变量
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("scene,fragments", [
    ("register", ["Welcome to AIFictionForge.", "Email registration verification code"]),
    ("login", ["You are logging in to AIFictionForge with an email verification code.",
               "Email login verification code"]),
    ("reset_password", ["You are resetting the password of your AIFictionForge account.",
                        "Password reset verification code"]),
])
def test_en_variant_fragments(scene, fragments):
    subject, text_body, html_body = _build_verification_mail_content(scene, CODE, TTL, lang="en")
    assert subject == f"AIFictionForge {fragments[1]}"
    # 标题出现在 subject 与 HTML h2；描述出现在 text_body 与 HTML 正文
    assert fragments[1] in html_body
    assert fragments[0] in text_body
    assert fragments[0] in html_body
    assert f"Your verification code is: {CODE}" in text_body
    assert f"Valid for: {TTL} minutes" in text_body
    assert f"        {CODE}" in html_body
    assert "If this was not your operation, please ignore this email." in text_body
    assert "If this was not your operation, please ignore this email." in html_body
    # 不残留中文文案
    for text in (subject, text_body, html_body):
        assert not any("\u4e00" <= ch <= "\u9fff" for ch in text), text


def test_en_variables_interpolated_same_as_zh():
    """en 与 zh 使用同样的变量插值（code / ttl_minutes 同值出现）。"""
    zh_text = _build_verification_mail_content("login", CODE, TTL, lang="zh")[1]
    en_text = _build_verification_mail_content("login", CODE, TTL, lang="en")[1]
    for token in (CODE, str(TTL)):
        assert token in zh_text and token in en_text
    # 结构对应：en 每行位置与 zh 一致（desc / code 行 / ttl 行 / ignore 行）
    assert len(zh_text.split("\n")) == len(en_text.split("\n"))


# ---------------------------------------------------------------------------
# 语言选择 _resolve_recipient_language
# ---------------------------------------------------------------------------

@pytest.fixture
async def db_engine():
    """临时文件 SQLite（避免 in-memory 多连接问题），测试后清理。"""
    db_path = f"/tmp/test_email_i18n_{uuid.uuid4().hex}.db"
    engine = create_async_engine(f"sqlite+aiosqlite:///{db_path}")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield engine
    await engine.dispose()
    if os.path.exists(db_path):
        os.remove(db_path)


@pytest.fixture
async def patched_session(monkeypatch, db_engine):
    Session = async_sessionmaker(bind=db_engine, expire_on_commit=False)

    async def fake_global_session():
        return Session()

    monkeypatch.setattr(auth_module, "_get_global_session", fake_global_session)
    return Session


async def seed_user(Session, user_id: str, email: str,
                    preferences: str = None, with_settings: bool = True):
    async with Session() as session:
        session.add(UserModel(
            user_id=user_id,
            username=email,
            display_name=email.split("@")[0],
            linuxdo_id=user_id,
        ))
        if with_settings:
            session.add(Settings(user_id=user_id, preferences=preferences))
        await session.commit()


class _User:
    def __init__(self, user_id: str):
        self.user_id = user_id


@pytest.mark.anyio
async def test_resolver_no_user_defaults_zh():
    """register 场景发送时收件人尚未注册（无用户对象）→ 默认 zh。"""
    assert await _resolve_recipient_language(None) == "zh"


@pytest.mark.anyio
async def test_resolver_missing_settings_row_defaults_zh(patched_session):
    """用户存在但无 Settings 行 → 默认 zh。"""
    await seed_user(patched_session, "u-1", "nosettings@example.com", with_settings=False)
    assert await _resolve_recipient_language(_User("u-1")) == "zh"


@pytest.mark.parametrize("preferences", [None, "{}", "not-json", '[]', '{"language": "zh"}', '{"other": 1}'])
@pytest.mark.anyio
async def test_resolver_missing_or_invalid_language_defaults_zh(patched_session, preferences):
    """preferences 缺失/非法/无 language/language=zh → 默认 zh。"""
    email = f"u{uuid.uuid4().hex[:8]}@example.com"
    user_id = f"u-{uuid.uuid4().hex[:8]}"
    await seed_user(patched_session, user_id, email, preferences=preferences)
    assert await _resolve_recipient_language(_User(user_id)) == "zh"


@pytest.mark.anyio
async def test_resolver_en_preference(patched_session):
    user_id = "u-en"
    await seed_user(patched_session, user_id, "en@example.com",
                    preferences=json.dumps({"language": "en", "other": 1}))
    assert await _resolve_recipient_language(_User(user_id)) == "en"


# ---------------------------------------------------------------------------
# 端到端：send_email_verification_code 捕获实际发送内容
# ---------------------------------------------------------------------------

@pytest.fixture
def capture_send_mail(monkeypatch):
    captured = {}

    async def fake_send_mail(**kwargs):
        captured.update(kwargs)

    monkeypatch.setattr(auth_module.email_service, "send_mail", fake_send_mail)
    return captured


@pytest.fixture
def patched_runtime(monkeypatch):
    async def fake_runtime():
        return dict(RUNTIME)

    monkeypatch.setattr(auth_module, "_get_auth_runtime_settings", fake_runtime)


def _sent_code(scene: str, email: str) -> str:
    return auth_module._email_verification_storage[f"{scene}:{email}"]["code"]


@pytest.mark.anyio
async def test_endpoint_zh_user_receives_legacy_identical_mail(
        patched_session, capture_send_mail, patched_runtime):
    """(a) zh 偏好用户收到的邮件与转换前拼装结果逐字节一致。"""
    await seed_user(patched_session, "u-zh", "zh-user@example.com",
                    preferences=json.dumps({"language": "zh"}))
    resp = await send_email_verification_code(
        EmailSendCodeRequest(email="zh-user@example.com", scene="login"))
    assert resp["success"] is True

    code = _sent_code("login", "zh-user@example.com")
    expected = _legacy_zh_mail(
        "邮箱登录验证码", "你正在使用邮箱验证码登录 AIFictionForge。", code, TTL)
    assert (capture_send_mail["subject"],
            capture_send_mail["text_body"],
            capture_send_mail["html_body"]) == expected


@pytest.mark.anyio
async def test_endpoint_en_user_receives_english_mail(
        patched_session, capture_send_mail, patched_runtime):
    """(b) en 偏好用户收到英文变体（显著子串 + 同变量插值）。"""
    await seed_user(patched_session, "u-en", "en-user@example.com",
                    preferences=json.dumps({"language": "en"}))
    resp = await send_email_verification_code(
        EmailSendCodeRequest(email="en-user@example.com", scene="login"))
    assert resp["success"] is True

    code = _sent_code("login", "en-user@example.com")
    assert capture_send_mail["subject"] == "AIFictionForge Email login verification code"
    assert "You are logging in to AIFictionForge with an email verification code." in capture_send_mail["text_body"]
    assert f"Your verification code is: {code}" in capture_send_mail["text_body"]
    assert f"Valid for: {TTL} minutes" in capture_send_mail["text_body"]
    assert code in capture_send_mail["html_body"]
    assert "Your verification code is:" in capture_send_mail["html_body"]
    assert "邮箱" not in capture_send_mail["subject"]


@pytest.mark.anyio
async def test_endpoint_missing_preference_defaults_zh(
        patched_session, capture_send_mail, patched_runtime):
    """(c) 用户无 Settings 行（缺偏好）→ 收到 zh（与历史行为一致）。"""
    await seed_user(patched_session, "u-none", "nopref@example.com", with_settings=False)
    resp = await send_email_verification_code(
        EmailSendCodeRequest(email="nopref@example.com", scene="reset_password"))
    assert resp["success"] is True

    code = _sent_code("reset_password", "nopref@example.com")
    expected = _legacy_zh_mail(
        "重置密码验证码", "你正在重置 AIFictionForge 账号密码。", code, TTL)
    assert (capture_send_mail["subject"],
            capture_send_mail["text_body"],
            capture_send_mail["html_body"]) == expected


@pytest.mark.anyio
async def test_endpoint_register_scene_no_user_defaults_zh(
        patched_session, capture_send_mail, patched_runtime):
    """register 场景发送时收件人尚未注册（无偏好可读）→ 默认 zh。"""
    resp = await send_email_verification_code(
        EmailSendCodeRequest(email="new-user@example.com", scene="register"))
    assert resp["success"] is True

    code = _sent_code("register", "new-user@example.com")
    expected = _legacy_zh_mail(
        "邮箱注册验证码", "欢迎注册 AIFictionForge。", code, TTL)
    assert (capture_send_mail["subject"],
            capture_send_mail["text_body"],
            capture_send_mail["html_body"]) == expected
