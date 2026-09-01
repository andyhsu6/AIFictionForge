"""preferences.language 读写测试（i18n plan todo 3）。

覆盖：
- PUT /settings/preferences 写入 language 后经 GET 语义（settings.preferences 列）可回读（round-trip）
- 增量合并：写入 language 不破坏 preferences 中其他既有键（如 api_presets）
- 非法语言值（zh-CN / fr / 空串）被 Pydantic 校验拒绝 → 对应 HTTP 422
- language=None 显式传 null 时移除该键
测试直接调用 endpoint 协程（绕过 require_login 依赖），DB 用临时文件 SQLite。
"""
import json
import os
import uuid
from types import SimpleNamespace

import pytest
from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.api.settings import update_preferences
from app.database import Base
from app.models.settings import Settings
from app.schemas.settings import PreferencesUpdate


@pytest.fixture
async def db_session():
    """临时文件 SQLite（避免 in-memory 多连接问题），测试后清理。"""
    db_path = f"/tmp/test_prefs_lang_{uuid.uuid4().hex}.db"
    engine = create_async_engine(f"sqlite+aiosqlite:///{db_path}")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    Session = async_sessionmaker(bind=engine, expire_on_commit=False)
    async with Session() as session:
        yield session
    await engine.dispose()
    if os.path.exists(db_path):
        os.remove(db_path)


def make_user(user_id: str = "u-1") -> SimpleNamespace:
    return SimpleNamespace(user_id=user_id, is_admin=False)


@pytest.mark.anyio
async def test_language_round_trip(db_session):
    """写入 zh 后从数据库回读 preferences.language == 'zh'；再改 en 同样生效。"""
    user = make_user()
    resp = await update_preferences(PreferencesUpdate(language="zh"), user, db_session)
    prefs = json.loads(resp["preferences"])
    assert prefs["language"] == "zh"

    row = (await db_session.execute(
        select(Settings).where(Settings.user_id == user.user_id)
    )).scalar_one()
    assert json.loads(row.preferences)["language"] == "zh"

    resp = await update_preferences(PreferencesUpdate(language="en"), user, db_session)
    assert json.loads(resp["preferences"])["language"] == "en"


@pytest.mark.anyio
async def test_language_merge_preserves_other_keys(db_session):
    """增量合并：不破坏 preferences 内其他既有键。"""
    user = make_user()
    db_session.add(Settings(
        user_id=user.user_id,
        preferences=json.dumps({"api_presets": {"presets": [], "version": "1.0"}, "theme_seed": 7}),
    ))
    await db_session.commit()

    resp = await update_preferences(PreferencesUpdate(language="en"), user, db_session)
    prefs = json.loads(resp["preferences"])
    assert prefs["language"] == "en"
    assert prefs["api_presets"] == {"presets": [], "version": "1.0"}
    assert prefs["theme_seed"] == 7


@pytest.mark.anyio
async def test_language_none_removes_key(db_session):
    """显式 null 移除 language 键，其余保留。"""
    user = make_user()
    db_session.add(Settings(user_id=user.user_id, preferences=json.dumps({"language": "zh", "keep": 1})))
    await db_session.commit()

    resp = await update_preferences(PreferencesUpdate(language=None), user, db_session)
    prefs = json.loads(resp["preferences"])
    assert "language" not in prefs
    assert prefs["keep"] == 1


@pytest.mark.parametrize("bad", ["zh-CN", "en-US", "fr", "", "ZH"])
def test_invalid_language_rejected(bad):
    """非标准化短码（含大小写/区域变体）被 schema 拒绝 → FastAPI 层即 422。"""
    with pytest.raises(ValidationError):
        PreferencesUpdate(language=bad)


def test_valid_languages_accepted():
    """仅 zh / en 通过校验。"""
    assert PreferencesUpdate(language="zh").language == "zh"
    assert PreferencesUpdate(language="en").language == "en"
