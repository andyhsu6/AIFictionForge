"""schema parity: background task i18n columns + index name parity

Revision ID: mu_schema_parity
Revises: mu_profile_facts
Create Date: 2026-10-30 15:00:00.000000

issue #208：`alembic check` 在全新空库上 upgrade head 后仍报告 9 项漂移，其中
7 个 diff 项属「迁移链 vs ORM metadata」的真实差异（2 列 + 3 处索引命名，改名
会同时产生 remove/add），本 revision 全部收敛；剩余 2 项有意保留（见下）。

1. `background_tasks.status_code` / `status_params` 两列：ORM 已声明
   (`app/models/background_task.py:26-27`)，但迁移链从未创建过；真实库是靠
   `app/main.py` 启动期 ALTER 自愈加上的。
2. `agent_messages` 索引名漂移：`idx_agent_messages_tool_call_id` →
   `ix_agent_messages_tool_call_id`（同一列，仅命名约定不同）。
3. `character_relationship_type_links` 索引名漂移：
   `ix_relationship_type_links_relationship` → 拆成 ORM 声明的两个名字
   `ix_character_relationship_type_links_relationship_id` +
   `ix_character_relationship_type_links_relationship_type_id`。

**有意不处理的两项**（`alembic check` 会继续报告，属已知残留）：

- `add_fk relationship_types.project_id -> projects.id ON DELETE CASCADE`：
  SQLite 不支持 `ALTER TABLE ADD CONSTRAINT`，补外键只能整表重建（`batch_alter_table`）；
  且 `app/database.py` 未开启 `PRAGMA foreign_keys`，运行期外键本就不生效，重建表换来的
  只有数据搬迁风险，收益为零。
- `modify_default settings.smtp_from_name -> 'AIFictionForge'`：
  改列默认值在 SQLite 上同样需要整表重建，且该列在运行期总由 ORM/服务层显式写入，
  DDL 默认值不参与任何业务路径。

**幂等性**：本机真实库 `backend/data/mumuai_novel.db` 里这两列已由启动期自增存在，
但 revision 尚未应用；直接 `ADD COLUMN` 会 `duplicate column name` 失败。因此全部操作
先经 SQLAlchemy inspector 判定，已存在则跳过。同理 downgrade 也可重复执行。
本 revision 不使用 `batch_alter_table`（不重建表，避免丢数据）。
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "mu_schema_parity"
down_revision: Union[str, None] = "mu_profile_facts"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


_AGENT_MESSAGES = "agent_messages"
_AGENT_MSG_OLD_INDEX = "idx_agent_messages_tool_call_id"
_AGENT_MSG_NEW_INDEX = "ix_agent_messages_tool_call_id"

_TASK_TABLE = "background_tasks"

_LINK_TABLE = "character_relationship_type_links"
_LINK_OLD_INDEX = "ix_relationship_type_links_relationship"
_LINK_RELATIONSHIP_INDEX = "ix_character_relationship_type_links_relationship_id"
_LINK_TYPE_INDEX = "ix_character_relationship_type_links_relationship_type_id"


def _column_names(inspector: sa.engine.reflection.Inspector, table: str):
    return {col["name"] for col in inspector.get_columns(table)}


def _index_names(inspector: sa.engine.reflection.Inspector, table: str):
    return {idx["name"] for idx in inspector.get_indexes(table)}


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)

    # ---- 1. background_tasks i18n 结构化状态列 ----
    task_columns = _column_names(inspector, _TASK_TABLE)
    if "status_code" not in task_columns:
        op.add_column(
            _TASK_TABLE,
            sa.Column(
                "status_code",
                sa.String(length=100),
                nullable=True,
                comment="结构化状态消息码(NULL=旧raw文案)",
            ),
        )
    if "status_params" not in task_columns:
        op.add_column(
            _TASK_TABLE,
            sa.Column(
                "status_params",
                sa.JSON(),
                nullable=True,
                comment="结构化状态消息参数(配合status_code)",
            ),
        )

    # ---- 2. agent_messages 索引名对齐 ORM ----
    agent_indexes = _index_names(inspector, _AGENT_MESSAGES)
    if _AGENT_MSG_OLD_INDEX in agent_indexes:
        op.drop_index(_AGENT_MSG_OLD_INDEX, table_name=_AGENT_MESSAGES)
    if _AGENT_MSG_NEW_INDEX not in agent_indexes:
        op.create_index(_AGENT_MSG_NEW_INDEX, _AGENT_MESSAGES, ["tool_call_id"])

    # ---- 3. character_relationship_type_links 索引名对齐 ORM ----
    link_indexes = _index_names(inspector, _LINK_TABLE)
    if _LINK_OLD_INDEX in link_indexes:
        op.drop_index(_LINK_OLD_INDEX, table_name=_LINK_TABLE)
    if _LINK_RELATIONSHIP_INDEX not in link_indexes:
        op.create_index(_LINK_RELATIONSHIP_INDEX, _LINK_TABLE, ["relationship_id"])
    if _LINK_TYPE_INDEX not in link_indexes:
        op.create_index(_LINK_TYPE_INDEX, _LINK_TABLE, ["relationship_type_id"])


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)

    # ---- 3. 索引名反向回退 ----
    link_indexes = _index_names(inspector, _LINK_TABLE)
    if _LINK_RELATIONSHIP_INDEX in link_indexes:
        op.drop_index(_LINK_RELATIONSHIP_INDEX, table_name=_LINK_TABLE)
    if _LINK_TYPE_INDEX in link_indexes:
        op.drop_index(_LINK_TYPE_INDEX, table_name=_LINK_TABLE)
    if _LINK_OLD_INDEX not in link_indexes:
        op.create_index(_LINK_OLD_INDEX, _LINK_TABLE, ["relationship_id"])

    # ---- 2. agent_messages 索引名反向回退 ----
    agent_indexes = _index_names(inspector, _AGENT_MESSAGES)
    if _AGENT_MSG_NEW_INDEX in agent_indexes:
        op.drop_index(_AGENT_MSG_NEW_INDEX, table_name=_AGENT_MESSAGES)
    if _AGENT_MSG_OLD_INDEX not in agent_indexes:
        op.create_index(_AGENT_MSG_OLD_INDEX, _AGENT_MESSAGES, ["tool_call_id"])

    # ---- 1. 删除新增列（SQLite 3.35+ 原生 DROP COLUMN，不重建表）----
    task_columns = _column_names(inspector, _TASK_TABLE)
    if "status_params" in task_columns:
        op.drop_column(_TASK_TABLE, "status_params")
    if "status_code" in task_columns:
        op.drop_column(_TASK_TABLE, "status_code")
