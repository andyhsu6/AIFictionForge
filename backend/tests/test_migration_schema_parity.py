"""迁移链 ↔ ORM metadata 一致性测试（issue #208）。

在临时 SQLite 库上跑 `alembic upgrade head`，再用 autogenerate 把结果与
`Base.metadata` 比对，断言除「有意不处理」的两类漂移外没有任何残留差异。

不 import `app.main` / `memory_service`，因此不需要 AI key，也不会触发
embedding 模型下载（`ONNX_EMBEDDING_OFFLINE=1` 兜底）。
"""

import os
from pathlib import Path

import pytest
from sqlalchemy import create_engine

os.environ.setdefault("ONNX_EMBEDDING_OFFLINE", "1")

BACKEND_DIR = Path(__file__).resolve().parents[1]
ALEMBIC_INI = BACKEND_DIR / "alembic-sqlite.ini"
SCRIPT_LOCATION = BACKEND_DIR / "alembic" / "sqlite"

# 已知且有意不处理的漂移（理由见 mu_schema_parity 迁移文件顶部注释）：
# - add_fk: SQLite 无 ALTER TABLE ADD CONSTRAINT，且运行期未开 PRAGMA foreign_keys
# - modify_default: SQLite 改列默认值需整表重建，运行期由 ORM/服务层显式赋值
ALLOWED_DIFF_TARGETS = {
    ("add_fk", "relationship_types"),
    ("modify_default", "settings.smtp_from_name"),
}

STRUCTURAL_DIFF_TYPES = {"add_column", "remove_column", "add_table", "remove_table"}
INDEX_DIFF_TYPES = {"add_index", "remove_index"}


def _alembic_url(db_path: Path) -> str:
    return f"sqlite+aiosqlite:///{db_path}"


def _upgrade_head(db_path: Path) -> None:
    """用 alembic Python API 把临时库升到 head。"""
    from alembic import command
    from alembic.config import Config

    from app.config import settings

    url = _alembic_url(db_path)
    previous_env = os.environ.get("DATABASE_URL")
    previous_setting = settings.database_url
    os.environ["DATABASE_URL"] = url
    # env.py 读的是 settings 对象的 database_url 属性，直接改属性可避免
    # 模块导入顺序导致它指向真实库（绝对不允许在真实库上跑迁移）。
    settings.database_url = url
    try:
        cfg = Config(str(ALEMBIC_INI))
        cfg.set_main_option("script_location", str(SCRIPT_LOCATION))
        cfg.set_main_option("sqlalchemy.url", url)
        command.upgrade(cfg, "head")
    finally:
        settings.database_url = previous_setting
        if previous_env is None:
            os.environ.pop("DATABASE_URL", None)
        else:
            os.environ["DATABASE_URL"] = previous_env


def _compare(db_path: Path):
    from alembic.autogenerate import compare_metadata
    from alembic.migration import MigrationContext

    from app.database import Base

    engine = create_engine(f"sqlite:///{db_path}")
    try:
        with engine.connect() as conn:
            context = MigrationContext.configure(
                conn,
                opts={"compare_type": True, "compare_server_default": True},
            )
            return compare_metadata(context, Base.metadata)
    finally:
        engine.dispose()


def _flatten(diffs) -> list:
    flat = []
    for diff in diffs:
        if isinstance(diff, list):
            flat.extend(diff)
        else:
            flat.append(diff)
    return flat


def _target(diff):
    """把 diff 归一成 (类型, 目标名)，便于精确断言残留项。"""
    kind = diff[0]
    if kind == "add_fk":
        return (kind, diff[1].table.name)
    if kind == "modify_default":
        return (kind, f"{diff[2]}.{diff[3]}")
    return (kind, None)


@pytest.fixture()
def upgraded_db(tmp_path):
    db_path = tmp_path / "parity.db"
    _upgrade_head(db_path)
    return db_path


def test_migration_chain_matches_orm_metadata(upgraded_db):
    diffs = _flatten(_compare(upgraded_db))
    rendered = "\n".join(repr(diff) for diff in diffs)

    structural = [d for d in diffs if d[0] in STRUCTURAL_DIFF_TYPES]
    assert structural == [], f"迁移链与 ORM 的表/列存在漂移:\n{rendered}"

    index_diffs = [d for d in diffs if d[0] in INDEX_DIFF_TYPES]
    assert index_diffs == [], f"迁移链与 ORM 的索引存在漂移:\n{rendered}"

    residual = {_target(d) for d in diffs}
    assert residual == ALLOWED_DIFF_TARGETS, (
        f"残留漂移集合与预期不一致（预期仅外键与 server_default 两项）:\n{rendered}"
    )
