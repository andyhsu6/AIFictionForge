"""计划 §13-3：分卷/多阶段文笔画像（MVP）。

覆盖：
- 卷画像行创建/更新（``preset_id=profile:<project_id>:v<index>``，不设默认）；
- ``list_project_volumes`` 的区间与 confidence；
- ``resolve_volume_for_chapter`` 的边界/重叠/无匹配；
- GET/POST/DELETE 端点（dry_run 不写库、profile_edited 保护、force 删除）；
- 生成选卷优先级：``_resolve_project_style_content`` 的显式选择/卷命中/全书画像回退。

全部用临时 SQLite + ``asyncio.run``，mock 掉 ``_extract_style_profile``，不触达
真实 AI / embedding / 网络。
"""
import asyncio
import inspect
import json
import os
import uuid

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from starlette.requests import Request

import app.api.chapters as chapters_module
from app.api.chapters import (
    _resolve_project_style_content,
    _run_chapter_generation_bg,
    generate_chapter_content_stream,
    generate_single_chapter_for_batch,
    regenerate_chapter_stream,
)
from app.api.writing_styles import (
    delete_project_profile_volume,
    generate_project_profile_volumes,
    get_project_profile_volumes,
)
from app.core.errors import ApiError
from app.database import Base
from app.models.chapter import Chapter
from app.models.project import Project
from app.models.project_default_style import ProjectDefaultStyle
from app.models.writing_style import WritingStyle
from app.schemas.writing_style import VolumeProfileGenerateRequest
from app.services.book_import_service import BookImportService
from app.services.style_profile import profile_preset_id
from app.services.style_volumes import (
    list_project_volumes,
    parse_volume_preset_id,
    resolve_volume_for_chapter,
    upsert_volume_profile,
    volume_preset_id,
)

USER_ID = "user-volumes"
PROJECT_ID = "project-volumes"
FULL_PROFILE_ID = 100
PRESET_STYLE_ID = 1


def _run(scenario) -> None:
    async def _main() -> None:
        db_path = f"/tmp/test_style_volumes_{uuid.uuid4().hex}.db"
        engine = create_async_engine(f"sqlite+aiosqlite:///{db_path}")
        try:
            async with engine.begin() as conn:
                await conn.run_sync(Base.metadata.create_all)
            session_factory = async_sessionmaker(bind=engine, expire_on_commit=False)
            async with session_factory() as session:
                await scenario(session)
        finally:
            await engine.dispose()
            if os.path.exists(db_path):
                os.remove(db_path)

    asyncio.run(_main())


def make_request(user_id: str = USER_ID) -> Request:
    req = Request({"type": "http", "method": "POST", "path": "/", "headers": []})
    req.state.user_id = user_id
    return req


async def seed_project(db, *, chapter_count: int = 30) -> Project:
    """项目 + 1..N 章 + 全局预设 + 全书画像（设为默认）。"""
    project = Project(id=PROJECT_ID, user_id=USER_ID, title="占位项目", current_words=0)
    chapters = [
        Chapter(
            project_id=PROJECT_ID,
            chapter_number=number,
            title=f"第{number}章",
            content="占位正文段落。" * 40,
            word_count=280,
        )
        for number in range(1, chapter_count + 1)
    ]
    preset = WritingStyle(
        id=PRESET_STYLE_ID,
        user_id=None,
        name="预设一",
        style_type="preset",
        preset_id="natural",
        prompt_content="预设风格内容",
        order_index=1,
    )
    full_profile = WritingStyle(
        id=FULL_PROFILE_ID,
        user_id=USER_ID,
        name="全书画像",
        style_type="profile",
        preset_id=profile_preset_id(PROJECT_ID),
        prompt_content="全书画像内容",
        profile_json=json.dumps({"version": 1, "confidence": 0.9}, ensure_ascii=False),
        profile_version=1,
    )
    db.add_all([project, *chapters, preset, full_profile])
    db.add(ProjectDefaultStyle(project_id=PROJECT_ID, style_id=FULL_PROFILE_ID))
    await db.commit()
    return project


async def volume_rows(db):
    return (
        await db.execute(
            select(WritingStyle).where(
                WritingStyle.preset_id.like(f"{profile_preset_id(PROJECT_ID)}:v%")
            )
        )
    ).scalars().all()


async def default_style_id(db):
    return (
        await db.execute(
            select(ProjectDefaultStyle.style_id).where(
                ProjectDefaultStyle.project_id == PROJECT_ID
            )
        )
    ).scalar_one_or_none()


# ========== 1. 卷画像创建/更新 ==========


def test_upsert_creates_and_updates_volume_without_touching_default():
    async def scenario(db):
        project = await seed_project(db)

        style_id = await upsert_volume_profile(
            db,
            project=project,
            index=1,
            start_chapter=1,
            end_chapter=10,
            prompt_content="卷一画像内容",
            description="卷一",
            profile_data={"version": 1, "confidence": 0.8},
            profile_version=1,
        )
        await db.commit()

        row = await db.get(WritingStyle, style_id)
        assert row.preset_id == f"profile:{PROJECT_ID}:v1"
        assert row.preset_id == volume_preset_id(PROJECT_ID, 1)
        assert row.style_type == "profile"
        assert row.user_id == USER_ID
        profile = json.loads(row.profile_json)
        assert profile["volume"] == {
            "index": 1,
            "start_chapter": 1,
            "end_chapter": 10,
        }
        # 卷画像不设为默认：默认仍是全书画像
        assert await default_style_id(db) == FULL_PROFILE_ID

        # 同 index 再写：复用同一行，区间/画像更新，未传 profile_data 时保留旧结构
        style_id_2 = await upsert_volume_profile(
            db,
            project=project,
            index=1,
            start_chapter=2,
            end_chapter=12,
            prompt_content="卷一画像内容 v2",
            description="卷一 v2",
            profile_data=None,
            profile_version=2,
        )
        await db.commit()

        assert style_id_2 == style_id
        await db.refresh(row)
        assert row.prompt_content == "卷一画像内容 v2"
        assert row.profile_version == 2
        profile = json.loads(row.profile_json)
        assert profile["confidence"] == 0.8  # 旧结构保留
        assert profile["volume"]["start_chapter"] == 2
        assert profile["volume"]["end_chapter"] == 12
        assert len(await volume_rows(db)) == 1
        assert await default_style_id(db) == FULL_PROFILE_ID

        # 解析器：只有卷画像返回 (project_id, index)
        assert parse_volume_preset_id(volume_preset_id(PROJECT_ID, 3)) == (PROJECT_ID, 3)
        assert parse_volume_preset_id(profile_preset_id(PROJECT_ID)) is None
        assert parse_volume_preset_id("natural") is None

    _run(scenario)


# ========== 2. list_project_volumes ==========


def test_list_project_volumes_returns_ranges_and_confidence():
    async def scenario(db):
        project = await seed_project(db)
        for index, (start, end, confidence) in enumerate(
            [(1, 10, 0.81), (11, 20, 0.72)], start=1
        ):
            await upsert_volume_profile(
                db,
                project=project,
                index=index,
                start_chapter=start,
                end_chapter=end,
                prompt_content=f"卷{index}内容",
                description="",
                profile_data={"confidence": confidence},
                profile_version=1,
            )
        await db.commit()

        volumes = await list_project_volumes(db, PROJECT_ID)
        assert [item["index"] for item in volumes] == [1, 2]
        assert volumes[0]["start_chapter"] == 1
        assert volumes[0]["end_chapter"] == 10
        assert volumes[0]["style_id"] == volume_id(volumes, 1)
        assert volumes[0]["confidence"] == pytest.approx(0.81)
        assert volumes[1]["confidence"] == pytest.approx(0.72)
        assert volumes[0]["updated_at"] is not None

    _run(scenario)


def volume_id(volumes, index):
    for item in volumes:
        if item["index"] == index:
            return item["style_id"]
    raise AssertionError(f"卷 {index} 不存在")


# ========== 3. resolve_volume_for_chapter ==========


def test_resolve_volume_for_chapter_boundaries_overlap_and_none():
    async def scenario(db):
        project = await seed_project(db)
        ranges = [(1, 1, 1, 10), (2, 2, 8, 20), (3, 3, 30, 40)]
        for index, _, start, end in ranges:
            await upsert_volume_profile(
                db,
                project=project,
                index=index,
                start_chapter=start,
                end_chapter=end,
                prompt_content=f"卷{index}内容",
                description="",
                profile_data={"confidence": 0.5},
                profile_version=1,
            )
        await db.commit()

        # 边界包含（1 与 10 都属于卷 1）
        first = await resolve_volume_for_chapter(db, PROJECT_ID, 1)
        assert first is not None and first.preset_id == volume_preset_id(PROJECT_ID, 1)
        last = await resolve_volume_for_chapter(db, PROJECT_ID, 10)
        assert last is not None and last.preset_id == volume_preset_id(PROJECT_ID, 1)

        # 重叠（9 同属卷 1、2）取 index 最小
        overlap = await resolve_volume_for_chapter(db, PROJECT_ID, 9)
        assert overlap is not None and overlap.preset_id == volume_preset_id(PROJECT_ID, 1)

        # 只被卷 2 覆盖
        second = await resolve_volume_for_chapter(db, PROJECT_ID, 11)
        assert second is not None and second.preset_id == volume_preset_id(PROJECT_ID, 2)

        # 卷 3
        third = await resolve_volume_for_chapter(db, PROJECT_ID, 35)
        assert third is not None and third.preset_id == volume_preset_id(PROJECT_ID, 3)

        # 无匹配
        assert await resolve_volume_for_chapter(db, PROJECT_ID, 50) is None
        assert await resolve_volume_for_chapter(db, PROJECT_ID, None) is None

    _run(scenario)


# ========== 4. API 端点 ==========


def _fake_extract(prompt_factory):
    async def fake_extract(self, **kwargs):
        numbers = [chapter.chapter_number for chapter in kwargs["chapters"]]
        return {
            "skipped": False,
            "prompt_content": prompt_factory(numbers),
            "description": "占位画像描述",
            "degraded": False,
            "confidence": 0.66,
            "sample_chars": len(numbers) * 100,
            "profile_data": {"version": 1, "confidence": 0.66},
            "profile_version": 1,
            "ranges": [],
        }

    return fake_extract


def test_volume_endpoints_dry_run_write_edited_protection_and_delete(monkeypatch):
    monkeypatch.setattr(
        BookImportService,
        "_extract_style_profile",
        _fake_extract(lambda numbers: f"卷画像内容·{numbers[0]}-{numbers[-1]}"),
    )

    async def scenario(db):
        await seed_project(db)
        payload = VolumeProfileGenerateRequest(
            volumes=[
                {"start_chapter": 1, "end_chapter": 10},
                {"start_chapter": 11, "end_chapter": 20},
            ],
        )

        # dry_run：只返回摘要，不写库
        preview = await generate_project_profile_volumes(
            PROJECT_ID, request=make_request(), payload=payload.model_copy(
                update={"dry_run": True}
            ), db=db
        )
        assert preview["total"] == 2
        assert preview["dry_run"] is True
        assert preview["anchors_reused"] is True
        assert [item.index for item in preview["volumes"]] == [1, 2]
        assert preview["volumes"][0].confidence == pytest.approx(0.66)
        assert preview["volumes"][0].skipped is False
        assert await volume_rows(db) == []
        # 全书画像与默认风格未受影响
        full = await db.get(WritingStyle, FULL_PROFILE_ID)
        assert full.prompt_content == "全书画像内容"
        assert await default_style_id(db) == FULL_PROFILE_ID

        # 写库
        result = await generate_project_profile_volumes(
            PROJECT_ID, request=make_request(), payload=payload, db=db
        )
        assert result["dry_run"] is False
        assert all(item.style_id for item in result["volumes"])
        rows = await volume_rows(db)
        assert len(rows) == 2
        assert {row.preset_id for row in rows} == {
            f"profile:{PROJECT_ID}:v1",
            f"profile:{PROJECT_ID}:v2",
        }
        assert await default_style_id(db) == FULL_PROFILE_ID

        listed = await get_project_profile_volumes(
            PROJECT_ID, request=make_request(), db=db
        )
        assert listed["total"] == 2
        assert listed["volumes"][0]["start_chapter"] == 1
        assert listed["volumes"][0]["end_chapter"] == 10
        assert listed["volumes"][0]["confidence"] == pytest.approx(0.66)

        # 手改保护：profile_edited 默认跳过，其他卷照常更新
        volume_one = next(row for row in rows if row.preset_id.endswith(":v1"))
        volume_one.style_type = "profile_edited"
        volume_one.prompt_content = "用户手改内容"
        await db.commit()

        guarded = await generate_project_profile_volumes(
            PROJECT_ID, request=make_request(), payload=payload, db=db
        )
        skipped = [item for item in guarded["volumes"] if item.index == 1][0]
        assert skipped.skipped is True
        assert skipped.skip_reason == "user_edited"
        await db.refresh(volume_one)
        assert volume_one.prompt_content == "用户手改内容"
        assert volume_one.style_type == "profile_edited"

        # overwrite_edited=True 才覆盖
        overridden = await generate_project_profile_volumes(
            PROJECT_ID,
            request=make_request(),
            payload=payload.model_copy(update={"overwrite_edited": True}),
            db=db,
        )
        assert overridden["volumes"][0].skipped is False
        await db.refresh(volume_one)
        assert volume_one.prompt_content == "卷画像内容·1-10"
        assert volume_one.style_type == "profile"

        # 区间校验：end < start / 区间内无章节
        with pytest.raises(ApiError) as invalid_range:
            await generate_project_profile_volumes(
                PROJECT_ID,
                request=make_request(),
                payload=VolumeProfileGenerateRequest(
                    volumes=[{"start_chapter": 10, "end_chapter": 5}]
                ),
                db=db,
            )
        assert invalid_range.value.status == 400

        with pytest.raises(ApiError) as empty_range:
            await generate_project_profile_volumes(
                PROJECT_ID,
                request=make_request(),
                payload=VolumeProfileGenerateRequest(
                    volumes=[{"start_chapter": 500, "end_chapter": 600}]
                ),
                db=db,
            )
        assert empty_range.value.status == 400

        # 权限：他人项目
        with pytest.raises(ApiError):
            await get_project_profile_volumes(
                PROJECT_ID, request=make_request("other-user"), db=db
            )

        # 删除：手改需 force
        volume_one.style_type = "profile_edited"
        await db.commit()
        with pytest.raises(ApiError) as guarded_delete:
            await delete_project_profile_volume(
                PROJECT_ID, 1, request=make_request(), db=db
            )
        assert guarded_delete.value.status == 409

        await delete_project_profile_volume(
            PROJECT_ID, 1, request=make_request(), db=db, force=True
        )
        assert {row.preset_id for row in await volume_rows(db)} == {
            f"profile:{PROJECT_ID}:v2"
        }
        # 卷画像删除不影响全书画像/默认风格
        assert await default_style_id(db) == FULL_PROFILE_ID

        with pytest.raises(ApiError):
            await delete_project_profile_volume(
                PROJECT_ID, 9, request=make_request(), db=db
            )

    _run(scenario)


# ========== 5. 生成选卷 ==========


def test_resolve_style_content_prefers_volume_only_when_no_explicit_normal_style():
    async def scenario(db):
        project = await seed_project(db)
        await upsert_volume_profile(
            db,
            project=project,
            index=1,
            start_chapter=1,
            end_chapter=10,
            prompt_content="卷一画像内容",
            description="",
            profile_data={"confidence": 0.8},
            profile_version=1,
        )
        volume_two_id = await upsert_volume_profile(
            db,
            project=project,
            index=2,
            start_chapter=11,
            end_chapter=20,
            prompt_content="卷二画像内容",
            description="",
            profile_data={"confidence": 0.7},
            profile_version=1,
        )
        await db.commit()

        # style_id=None + 章号命中卷 → 用卷画像
        content = await _resolve_project_style_content(
            db, project, 5, None, USER_ID
        )
        assert content == "卷一画像内容"

        # style_id 指向本项目全书画像 → 仍先按章号选卷
        content = await _resolve_project_style_content(
            db, project, 15, FULL_PROFILE_ID, USER_ID
        )
        assert content == "卷二画像内容"

        # 全书画像 + 章号无卷命中 → 全书画像
        content = await _resolve_project_style_content(
            db, project, 50, FULL_PROFILE_ID, USER_ID
        )
        assert content == "全书画像内容"

        # 无风格 + 章号无卷命中 → 空（不凭空用全书画像）
        content = await _resolve_project_style_content(
            db, project, 50, None, USER_ID
        )
        assert content == ""

        # 用户显式选择普通风格 → 不被卷覆盖
        content = await _resolve_project_style_content(
            db, project, 5, PRESET_STYLE_ID, USER_ID
        )
        assert content == "预设风格内容"

        # 用户显式选择某个卷画像 → 用该卷（即使目标章属于别的卷）
        content = await _resolve_project_style_content(
            db, project, 5, volume_two_id, USER_ID
        )
        assert content == "卷二画像内容"

        # 卷画像存在但 style_id 指向其他项目的卷画像 → 拒绝
        other_volume = WritingStyle(
            user_id=USER_ID,
            name="他项目卷画像",
            style_type="profile",
            preset_id=volume_preset_id("other-project", 1),
            prompt_content="他项目卷内容",
            profile_json=json.dumps(
                {"volume": {"index": 1, "start_chapter": 1, "end_chapter": 10}}
            ),
        )
        db.add(other_volume)
        await db.commit()
        content = await _resolve_project_style_content(
            db, project, 5, other_volume.id, USER_ID
        )
        assert content == ""

        # 重生成路径：未指定风格且无卷命中 → 回退项目默认（全书画像）
        content = await _resolve_project_style_content(
            db, project, 50, None, USER_ID, use_project_default=True
        )
        assert content == "全书画像内容"

    _run(scenario)


def test_generation_paths_wire_volume_resolution():
    """静态护栏：4 条生成路径都走统一的选卷 helper（局部重写不接入）。"""
    for func in (
        generate_chapter_content_stream,
        _run_chapter_generation_bg,
        generate_single_chapter_for_batch,
        regenerate_chapter_stream,
    ):
        assert "_resolve_project_style_content(" in inspect.getsource(func), func.__name__

    partial = getattr(chapters_module, "partial_regenerate_stream", None)
    if partial is not None:
        assert "_resolve_project_style_content" not in inspect.getsource(partial)
