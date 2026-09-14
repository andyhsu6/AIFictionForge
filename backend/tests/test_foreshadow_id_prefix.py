"""find_foreshadow 的 ID 前缀解析回归测试（issue #92）。

背景：交接文档/转账备注里常只保留 8 位 ID 前缀；agent 用前缀调用
get_foreshadow_detail 时，旧实现直接判「未找到」，用户被迫重试整轮，
并在 agent_tool_calls 里留下 failed 记录。修复要求：

- 唯一前缀解析到对应行，payload 与整段 ID 完全一致；
- 前缀命中多行时必须响亮失败（可区分的文案），绝不静默取第一行；
- 完整 ID 与不存在 ID 的行为保持不变。
"""
import os
import uuid

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.database import Base
from app.models.foreshadow import Foreshadow
from app.models.project import Project
from app.services.project_agent_extended_tools import ProjectAgentExtendedTools
from app.services.project_agent_selectors import find_foreshadow

PROJECT_ID = "proj-prefix-1"
OTHER_PROJECT_ID = "proj-prefix-2"

# 中性占位 ID（非真实数据）
UNIQUE_FULL_ID = "a1b2c3d4-e5f6-4a7b-8c9d-000000000001"
UNIQUE_PREFIX = UNIQUE_FULL_ID[:8]
# 另一个项目里的行共享同一前缀：前缀解析必须仍按项目过滤。
OTHER_PROJECT_SHARED_PREFIX_ID = "a1b2c3d4-e5f6-4a7b-8c9d-000000000099"
AMBIGUOUS_PREFIX = "b2c3d4e5"
AMBIGUOUS_IDS = [
    "b2c3d4e5-1111-4a7b-8c9d-000000000002",
    "b2c3d4e5-2222-4a7b-8c9d-000000000003",
]
OTHER_ID = "c3d4e5f6-3333-4a7b-8c9d-000000000004"
MISSING_ID = "f0f0f0f0"


@pytest.fixture
async def db_session():
    """临时文件 SQLite（与 test_careers_delete.py 同模式）。"""
    db_path = f"/tmp/test_foreshadow_prefix_{uuid.uuid4().hex}.db"
    engine = create_async_engine(f"sqlite+aiosqlite:///{db_path}")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    Session = async_sessionmaker(bind=engine, expire_on_commit=False)
    async with Session() as session:
        yield session
    await engine.dispose()
    if os.path.exists(db_path):
        os.remove(db_path)


@pytest.fixture
async def seeded(db_session):
    """一个项目内：唯一前缀行 + 歧义前缀两行 + 无关行；另一项目共享同一前缀。"""
    project = Project(id=PROJECT_ID, user_id="user-1", title="placeholder project")
    other_project = Project(id=OTHER_PROJECT_ID, user_id="user-1", title="placeholder project 2")
    rows = [
        Foreshadow(id=UNIQUE_FULL_ID, project_id=PROJECT_ID, title="placeholder unique", content="placeholder content"),
        Foreshadow(id=AMBIGUOUS_IDS[0], project_id=PROJECT_ID, title="placeholder ambiguous a", content="placeholder content"),
        Foreshadow(id=AMBIGUOUS_IDS[1], project_id=PROJECT_ID, title="placeholder ambiguous b", content="placeholder content"),
        Foreshadow(id=OTHER_ID, project_id=PROJECT_ID, title="placeholder other", content="placeholder content"),
        Foreshadow(id=OTHER_PROJECT_SHARED_PREFIX_ID, project_id=OTHER_PROJECT_ID, title="placeholder foreign", content="placeholder content"),
    ]
    db_session.add_all([project, other_project, *rows])
    await db_session.commit()
    return project, db_session


@pytest.mark.anyio
async def test_unique_prefix_resolves_to_the_row(seeded):
    """(a) 8 位唯一前缀在 resolver 层解析到目标行。"""
    _, db = seeded
    row = await find_foreshadow(db, PROJECT_ID, {"foreshadow_id": UNIQUE_PREFIX})
    assert row.id == UNIQUE_FULL_ID


@pytest.mark.anyio
async def test_unique_prefix_tool_payload_equals_full_id_payload(seeded):
    """(a) 工具层：前缀调用与完整 ID 调用返回同一 payload。"""
    project, db = seeded
    tools = ProjectAgentExtendedTools(project, db)
    by_prefix = await tools.read("get_foreshadow_detail", {"foreshadow_id": UNIQUE_PREFIX})
    by_full = await tools.read("get_foreshadow_detail", {"foreshadow_id": UNIQUE_FULL_ID})
    assert by_prefix == by_full
    assert by_prefix["id"] == UNIQUE_FULL_ID
    assert by_prefix["title"] == "placeholder unique"


@pytest.mark.anyio
async def test_prefix_stays_project_scoped(seeded):
    """另一个项目的行共享前缀，不得影响本项目的唯一解析。"""
    _, db = seeded
    row = await find_foreshadow(db, PROJECT_ID, {"foreshadow_id": UNIQUE_PREFIX})
    assert row.id == UNIQUE_FULL_ID
    foreign = await find_foreshadow(db, OTHER_PROJECT_ID, {"foreshadow_id": UNIQUE_PREFIX})
    assert foreign.id == OTHER_PROJECT_SHARED_PREFIX_ID


@pytest.mark.anyio
async def test_ambiguous_prefix_raises_distinct_error(seeded):
    """(b) 歧义前缀：必须失败，且不是通用「未找到」文案。"""
    _, db = seeded
    with pytest.raises(ValueError) as exc_info:
        await find_foreshadow(db, PROJECT_ID, {"foreshadow_id": AMBIGUOUS_PREFIX})
    message = str(exc_info.value)
    assert "未找到" not in message, f"歧义不应报通用未找到: {message}"
    assert "前缀" in message
    assert "2" in message  # 报告命中数量
    assert "更完整" in message  # 可行动的下一步


@pytest.mark.anyio
async def test_ambiguous_prefix_tool_call_raises_distinct_error(seeded):
    """(b) 工具层同样失败关闭，绝不静默选一行。"""
    project, db = seeded
    tools = ProjectAgentExtendedTools(project, db)
    with pytest.raises(ValueError) as exc_info:
        await tools.read("get_foreshadow_detail", {"foreshadow_id": AMBIGUOUS_PREFIX})
    assert "未找到" not in str(exc_info.value)
    assert "前缀" in str(exc_info.value)


@pytest.mark.anyio
async def test_full_id_path_unchanged(seeded):
    """(c) 完整 UUID 路径行为不变。"""
    project, db = seeded
    row = await find_foreshadow(db, PROJECT_ID, {"foreshadow_id": UNIQUE_FULL_ID})
    assert row.id == UNIQUE_FULL_ID
    payload = await ProjectAgentExtendedTools(project, db).read(
        "get_foreshadow_detail", {"foreshadow_id": UNIQUE_FULL_ID}
    )
    assert payload["id"] == UNIQUE_FULL_ID


@pytest.mark.anyio
async def test_missing_id_still_raises_not_found(seeded):
    """(d) 不存在的 ID 保持原有「未找到」失败。"""
    _, db = seeded
    with pytest.raises(ValueError, match="当前项目中未找到指定伏笔 ID"):
        await find_foreshadow(db, PROJECT_ID, {"foreshadow_id": MISSING_ID})


@pytest.mark.anyio
async def test_manage_foreshadow_update_accepts_unique_prefix(seeded):
    """(e) 兄弟工具 manage_foreshadow(update) 同样接受唯一前缀。"""
    project, db = seeded
    tools = ProjectAgentExtendedTools(project, db)
    result = await tools.execute(
        "manage_foreshadow",
        {"action": "update", "foreshadow_id": UNIQUE_PREFIX, "data": {"importance": 0.9}},
    )
    assert result["entity_id"] == UNIQUE_FULL_ID
    row = await find_foreshadow(db, PROJECT_ID, {"foreshadow_id": UNIQUE_FULL_ID})
    assert row.importance == 0.9


@pytest.mark.anyio
async def test_manage_foreshadow_ambiguous_prefix_fails(seeded):
    """(e) 兄弟工具的歧义前缀同样失败关闭。"""
    project, db = seeded
    tools = ProjectAgentExtendedTools(project, db)
    with pytest.raises(ValueError) as exc_info:
        await tools.execute(
            "manage_foreshadow",
            {"action": "update", "foreshadow_id": AMBIGUOUS_PREFIX, "data": {"importance": 0.9}},
        )
    assert "未找到" not in str(exc_info.value)
    assert "前缀" in str(exc_info.value)
