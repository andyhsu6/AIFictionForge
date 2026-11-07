"""issue #244 回归：项目导出/导入必须携带世界设定同步凭证。

背景：导出只写四个世界字段文本，丢掉了 #175 引入的
`ai_world_profile_snapshot` 与 `world_profile_synced_through_chapter`；
导入后 snapshot 为空、水位为 0，自动世界设定同步会把现有值当"历史值/用户值"
永久保护，世界设定从此不再演进（见 issue #244）。

这里用服务层往返测试钉住修复，并覆盖旧版（1.1.0）导出缺少凭证键时的兼容行为。
"""
import asyncio
import json
import os
import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.database import Base
from app.models.project import Project
from app.services.import_export_service import ImportExportService


def _db_path() -> str:
    return f"/tmp/test_ie_world_sync_{uuid.uuid4().hex}.db"


async def _seed_and_export(
    db_path: str,
    snapshot: str | None = json.dumps({"world_rules": "占位规则"}, ensure_ascii=False),
    watermark: int = 7,
    facts: str | None = json.dumps(
        [
            {
                "field": "world_rules",
                "text": "占位规则",
                "evidence": [{"chapter": 1, "quote": "占位规则原文"}],
                "source": "original",
            }
        ],
        ensure_ascii=False,
    ),
) -> dict:
    engine = create_async_engine(f"sqlite+aiosqlite:///{db_path}")
    try:
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        session_factory = async_sessionmaker(bind=engine, expire_on_commit=False)
        async with session_factory() as db:
            db.add(
                Project(
                    id="proj-roundtrip",
                    user_id="user-1",
                    title="占位项目",
                    world_time_period="占位纪元第七年",
                    world_location="占位城",
                    world_atmosphere="占位氛围",
                    world_rules="占位规则",
                    ai_world_profile_snapshot=snapshot,
                    world_facts=facts,
                    world_profile_synced_through_chapter=watermark,
                )
            )
            await db.commit()
            exported = await ImportExportService.export_project("proj-roundtrip", db)
        return exported.model_dump()
    finally:
        await engine.dispose()


async def _import_into_fresh_db(data: dict, db_path: str):
    """把导出数据导入一个全新库，返回 (ImportResult, 恢复后的 project 值)。"""
    engine = create_async_engine(f"sqlite+aiosqlite:///{db_path}")
    try:
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        session_factory = async_sessionmaker(bind=engine, expire_on_commit=False)
        async with session_factory() as db:
            result = await ImportExportService.import_project(data, db, "user-2")
        if not result.success:
            return result, None
        async with session_factory() as db:
            project = (
                await db.execute(select(Project).where(Project.id == result.project_id))
            ).scalar_one()
            restored = {
                "ai_world_profile_snapshot": project.ai_world_profile_snapshot,
                "world_profile_synced_through_chapter": project.world_profile_synced_through_chapter,
                "world_facts": project.world_facts,
            }
        return result, restored
    finally:
        await engine.dispose()


def test_round_trip_preserves_world_sync_credentials():
    """导出 → 导入后，世界设定快照与同步水位必须一字不差地恢复。"""
    src_path = _db_path()
    dst_path = _db_path()
    try:
        asyncio.run(_run_round_trip(src_path, dst_path))
    finally:
        for path in (src_path, dst_path):
            if os.path.exists(path):
                os.remove(path)


async def _run_round_trip(src_path: str, dst_path: str) -> None:
    exported = await _seed_and_export(src_path)
    assert exported["version"] == ImportExportService.CURRENT_VERSION == "1.3.0"
    project_data = exported["project"]
    assert project_data["ai_world_profile_snapshot"] == json.dumps(
        {"world_rules": "占位规则"}, ensure_ascii=False
    )
    assert project_data["world_profile_synced_through_chapter"] == 7

    result, restored = await _import_into_fresh_db(exported, dst_path)
    assert result.success, result.message
    assert restored is not None
    assert restored["ai_world_profile_snapshot"] == project_data["ai_world_profile_snapshot"]
    assert restored["world_profile_synced_through_chapter"] == 7
    assert restored["world_facts"] == project_data["world_facts"]


def test_legacy_export_without_credentials_keeps_defaults():
    """旧版（1.1.0）导出没有凭证键：导入仍成功，snapshot=None、水位=0（行为不变）。"""
    src_path = _db_path()
    dst_path = _db_path()
    try:
        asyncio.run(_run_legacy_import(src_path, dst_path))
    finally:
        for path in (src_path, dst_path):
            if os.path.exists(path):
                os.remove(path)


async def _run_legacy_import(src_path: str, dst_path: str) -> None:
    exported = await _seed_and_export(src_path)
    exported["version"] = "1.1.0"
    exported["project"].pop("ai_world_profile_snapshot", None)
    exported["project"].pop("world_profile_synced_through_chapter", None)
    exported["project"].pop("world_facts", None)

    result, restored = await _import_into_fresh_db(exported, dst_path)
    assert result.success, result.message
    assert restored is not None
    assert restored["ai_world_profile_snapshot"] is None
    assert restored["world_profile_synced_through_chapter"] == 0
    assert restored["world_facts"] is None


def test_dirty_watermark_falls_back_to_zero():
    """水位字段是脏字符串时回退 0，而不是让整次导入失败。"""
    src_path = _db_path()
    dst_path = _db_path()
    try:
        asyncio.run(_run_dirty_watermark(src_path, dst_path))
    finally:
        for path in (src_path, dst_path):
            if os.path.exists(path):
                os.remove(path)


async def _run_dirty_watermark(src_path: str, dst_path: str) -> None:
    exported = await _seed_and_export(src_path)
    exported["project"]["world_profile_synced_through_chapter"] = "not-a-number"
    result, restored = await _import_into_fresh_db(exported, dst_path)
    assert result.success, result.message
    assert restored is not None
    assert restored["world_profile_synced_through_chapter"] == 0

def test_round_trip_preserves_null_snapshot_and_zero_watermark():
    """1.3.0 导出自身也要原样往返 None / 0，而不是只在旧版兼容用例里出现。"""
    src_path = _db_path()
    dst_path = _db_path()
    try:
        asyncio.run(_run_null_zero_round_trip(src_path, dst_path))
    finally:
        for path in (src_path, dst_path):
            if os.path.exists(path):
                os.remove(path)


async def _run_null_zero_round_trip(src_path: str, dst_path: str) -> None:
    exported = await _seed_and_export(src_path, snapshot=None, watermark=0, facts=None)
    project_data = exported["project"]
    assert project_data["ai_world_profile_snapshot"] is None
    assert project_data["world_profile_synced_through_chapter"] == 0
    assert project_data["world_facts"] is None

    result, restored = await _import_into_fresh_db(exported, dst_path)
    assert result.success, result.message
    assert restored is not None
    assert restored["ai_world_profile_snapshot"] is None
    assert restored["world_profile_synced_through_chapter"] == 0
    assert restored["world_facts"] is None


def test_negative_watermark_is_clamped_to_zero():
    """负数水位不是合法进度：导入时收敛到 0，避免把章节水位推到负数。"""
    src_path = _db_path()
    dst_path = _db_path()
    try:
        asyncio.run(_run_negative_watermark(src_path, dst_path))
    finally:
        for path in (src_path, dst_path):
            if os.path.exists(path):
                os.remove(path)


async def _run_negative_watermark(src_path: str, dst_path: str) -> None:
    exported = await _seed_and_export(src_path)
    exported["project"]["world_profile_synced_through_chapter"] = -3
    result, restored = await _import_into_fresh_db(exported, dst_path)
    assert result.success, result.message
    assert restored is not None
    assert restored["world_profile_synced_through_chapter"] == 0


def test_dirty_world_facts_are_dropped():
    """world_facts 不是合法 JSON 数组时降级为 None，并让导入继续成功。"""
    src_path = _db_path()
    dst_path = _db_path()
    try:
        asyncio.run(_run_dirty_facts(src_path, dst_path))
    finally:
        for path in (src_path, dst_path):
            if os.path.exists(path):
                os.remove(path)


async def _run_dirty_facts(src_path: str, dst_path: str) -> None:
    exported = await _seed_and_export(src_path)
    exported["project"]["world_facts"] = "not-json"
    result, restored = await _import_into_fresh_db(exported, dst_path)
    assert result.success, result.message
    assert restored is not None
    assert restored["world_facts"] is None


def test_world_facts_accepts_already_parsed_list_on_import():
    """手工编辑/第三方产出的 world_facts 可能已是数组：导入直接采纳。"""
    src_path = _db_path()
    dst_path = _db_path()
    try:
        asyncio.run(_run_parsed_facts(src_path, dst_path))
    finally:
        for path in (src_path, dst_path):
            if os.path.exists(path):
                os.remove(path)


async def _run_parsed_facts(src_path: str, dst_path: str) -> None:
    exported = await _seed_and_export(src_path)
    parsed = json.loads(exported["project"]["world_facts"])
    exported["project"]["world_facts"] = parsed

    result, restored = await _import_into_fresh_db(exported, dst_path)
    assert result.success, result.message
    assert restored is not None
    assert json.loads(restored["world_facts"]) == parsed


def test_outline_transfer_accepts_current_project_export_version():
    """完整项目导出升级后，大纲提取导入不得再拒绝新文件。"""
    from app.services.outline_transfer_service import OutlineTransferService

    assert ImportExportService.CURRENT_VERSION in OutlineTransferService.PROJECT_FORMAT_VERSIONS
    assert OutlineTransferService.PROJECT_FORMAT_VERSIONS == set(
        ImportExportService.SUPPORTED_VERSIONS
    )
