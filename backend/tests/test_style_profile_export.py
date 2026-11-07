"""issue #254 P2 回归：1.4.0 可选项目画像块导出/导入 + 回填脚本。

覆盖：
1. `include_style_profile=False`（默认）与 P0 隔离完全一致——画像行不进导出包，
   默认风格是画像时 `project_default_style=None`；
2. `include_style_profile=True` 时导出画像行，`profile` 块带解析后的 profile_json
   与 profile_version；
3. 1.4.0 包导入到另一项目：画像 preset_id 用**目标项目 id**重建；目标项目已有
   `profile_edited` 画像时不覆盖；
4. 1.3.0 旧包（无 profile 块）导入不创建画像、不报错；
5. 回填脚本 dry-run 不写库、apply 只填空缺、`--force` 覆盖、幂等。

用临时 SQLite + `asyncio.run`（与 tests/ 既有风格一致）；不调用真实模型。
"""
import asyncio
import json
import os
import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.database import Base
from app.models.chapter import Chapter
from app.models.project import Project
from app.models.project_default_style import ProjectDefaultStyle
from app.models.writing_style import WritingStyle
from app.services.book_import_service import BookImportService
from app.services.import_export_service import ImportExportService
from app.services.style_profile import profile_preset_id
from scripts.backfill_style_profiles import backfill_projects

USER_ID = "user-1"

PROFILE_DATA = {
    "version": 1,
    "confidence": 0.8,
    "metrics": {
        "avg_sentence_chars": 16.0,
        "median_sentence_chars": 15,
        "short_sentence_ratio": 0.5,
        "dialogue_ratio": 0.25,
        "paragraph_chars_p50": 60,
    },
    "narrative": {"pov": "第三人称限制视角", "tense": "过去时", "distance": "贴近"},
    "diction": {"register": "白话", "concreteness": "具体", "idiom_density": "低"},
    "dialogue": {"attribution": "动作提示", "tag_density": "每4-6句一次"},
    "rhetoric": ["比喻密度低"],
    "do": ["平均句长 13–19 字"],
    "dont": ["避免长句堆叠"],
    "evidence_chapters": [1, 3],
}


def _db_path() -> str:
    return f"/tmp/test_style_profile_export_{uuid.uuid4().hex}.db"


def _run(scenario) -> None:
    """每个测试独立临时 SQLite，跑完删除。"""

    async def _main() -> None:
        db_path = _db_path()
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


def _open_session(db_path: str):
    return create_async_engine(f"sqlite+aiosqlite:///{db_path}")


async def _seed_source(db, *, project_id: str = "project-a", with_profile: bool = True):
    """种一个项目：预设风格 + 自定义风格 + 画像行（可选）+ 默认风格指向画像。"""
    project = Project(id=project_id, user_id=USER_ID, title="占位项目", current_words=0)
    preset = WritingStyle(
        id=1,
        user_id=None,
        name="预设一",
        style_type="preset",
        preset_id="natural",
        prompt_content="预设内容",
        order_index=1,
    )
    custom = WritingStyle(
        id=2,
        user_id=USER_ID,
        name="自定义风格",
        style_type="custom",
        prompt_content="自定义内容",
        order_index=2,
    )
    db.add_all([project, preset, custom])
    if with_profile:
        profile_row = WritingStyle(
            id=3,
            user_id=USER_ID,
            name="文笔画像·占位项目",
            style_type="profile",
            preset_id=profile_preset_id(project_id),
            description="由拆书导入自动抽取；样本：第 1–3 章",
            prompt_content="画像提示词内容",
            profile_json=json.dumps(PROFILE_DATA, ensure_ascii=False),
            profile_version=1,
            order_index=0,
        )
        db.add(profile_row)
        db.add(ProjectDefaultStyle(project_id=project_id, style_id=3))
    await db.commit()
    return project


def _profile_styles(exported: dict) -> list[dict]:
    return [
        style
        for style in exported["writing_styles"]
        if str(style.get("preset_id") or "").startswith("profile:")
    ]


# ---------------------------------------------------------------- 导出 1.4.0


def test_export_default_excludes_project_profile():
    """默认（include_style_profile=False）保持 P0 隔离。"""

    async def scenario(db):
        await _seed_source(db)
        exported = (await ImportExportService.export_project("project-a", db)).model_dump()

        assert exported["version"] == ImportExportService.CURRENT_VERSION == "1.4.0"
        assert _profile_styles(exported) == []
        # 自定义风格仍然导出（隔离只针对画像）
        assert [s["name"] for s in exported["writing_styles"]] == ["自定义风格"]
        assert exported["project_default_style"] is None

    _run(scenario)


def test_export_with_style_profile_carries_profile_block():
    """显式开启时画像行随包导出，profile 块带解析结果与版本。"""

    async def scenario(db):
        await _seed_source(db)
        exported = (
            await ImportExportService.export_project(
                "project-a", db, include_style_profile=True
            )
        ).model_dump()

        profiles = _profile_styles(exported)
        assert len(profiles) == 1
        block = profiles[0]["profile"]
        assert block["profile_json"] == PROFILE_DATA
        assert block["profile_version"] == 1
        assert block["prompt_content"] == "画像提示词内容"
        assert block["is_project_profile"] is True
        # 默认风格（画像）此时写出名称，供导入端按 name 关联
        assert exported["project_default_style"]["style_name"] == "文笔画像·占位项目"
        # 锚点不导出
        assert "ranges" not in block

    _run(scenario)


# ---------------------------------------------------------------- 导入 1.4.0


def test_import_140_profile_rebuilds_with_target_project_id():
    """1.4.0 包导入到另一项目：preset_id 改用目标项目 id，默认风格指向画像。"""

    src_path = _db_path()
    dst_path = _db_path()

    async def _main() -> None:
        try:
            # 来源库：导出 1.4.0 包
            engine = create_async_engine(f"sqlite+aiosqlite:///{src_path}")
            try:
                async with engine.begin() as conn:
                    await conn.run_sync(Base.metadata.create_all)
                factory = async_sessionmaker(bind=engine, expire_on_commit=False)
                async with factory() as src:
                    await _seed_source(src)
                    data = (
                        await ImportExportService.export_project(
                            "project-a", src, include_style_profile=True
                        )
                    ).model_dump()
            finally:
                await engine.dispose()

            # 目标库：另一个用户导入，目标项目 id 由导入端生成
            engine = create_async_engine(f"sqlite+aiosqlite:///{dst_path}")
            try:
                async with engine.begin() as conn:
                    await conn.run_sync(Base.metadata.create_all)
                factory = async_sessionmaker(bind=engine, expire_on_commit=False)
                async with factory() as dst:
                    result = await ImportExportService.import_project(data, dst, "user-2")
                    assert result.success, result.message
                    assert result.project_id != "project-a"

                    rows = (
                        await dst.execute(
                            select(WritingStyle).where(
                                WritingStyle.preset_id
                                == profile_preset_id(result.project_id)
                            )
                        )
                    ).scalars().all()
                    assert len(rows) == 1
                    rebuilt = rows[0]
                    assert rebuilt.style_type == "profile"
                    assert rebuilt.user_id == "user-2"
                    assert json.loads(rebuilt.profile_json) == PROFILE_DATA
                    assert rebuilt.profile_version == 1

                    # 来源项目 id 不残留
                    leaked = (
                        await dst.execute(
                            select(WritingStyle).where(
                                WritingStyle.preset_id == profile_preset_id("project-a")
                            )
                        )
                    ).scalars().all()
                    assert leaked == []

                    default = (
                        await dst.execute(
                            select(ProjectDefaultStyle).where(
                                ProjectDefaultStyle.project_id == result.project_id
                            )
                        )
                    ).scalar_one()
                    assert default.style_id == rebuilt.id

                    # 用户编辑后再次导入同一包：保留编辑、不新增行
                    rebuilt.style_type = "profile_edited"
                    rebuilt.prompt_content = "用户编辑后的内容"
                    await dst.commit()

                    await ImportExportService._import_writing_styles(
                        result.project_id, data["writing_styles"], dst
                    )
                    await dst.commit()

                    rows_after = (
                        await dst.execute(
                            select(WritingStyle).where(
                                WritingStyle.preset_id
                                == profile_preset_id(result.project_id)
                            )
                        )
                    ).scalars().all()
                    assert len(rows_after) == 1
                    assert rows_after[0].style_type == "profile_edited"
                    assert rows_after[0].prompt_content == "用户编辑后的内容"
            finally:
                await engine.dispose()
        finally:
            for path in (src_path, dst_path):
                if os.path.exists(path):
                    os.remove(path)

    asyncio.run(_main())


def test_legacy_130_package_without_profile_block_does_not_create_profile():
    """1.3.0 旧包（无 profile 块）导入：不建画像、不报错。"""

    async def scenario(db):
        await _seed_source(db)
        exported = (
            await ImportExportService.export_project("project-a", db)
        ).model_dump()
        exported["version"] = "1.3.0"
        # 旧包/异常包可能带画像行但没有 profile 块 → 导入端继续跳过
        exported["writing_styles"].append(
            {
                "name": "他项目画像",
                "style_type": "profile",
                "preset_id": profile_preset_id("project-a"),
                "prompt_content": "旧包画像内容",
                "order_index": 0,
            }
        )

        validation = ImportExportService.validate_import_data(exported)
        assert validation.valid
        # 1.3.0 在支持列表内：不应出现版本不匹配告警
        assert not [w for w in validation.warnings if "版本不匹配" in w]

        result = await ImportExportService.import_project(exported, db, "user-2")
        assert result.success, result.message

        rows = (
            await db.execute(
                select(WritingStyle).where(
                    WritingStyle.preset_id == profile_preset_id(result.project_id)
                )
            )
        ).scalars().all()
        assert rows == []

    _run(scenario)


def test_validate_accepts_131_and_140_versions():
    """1.4.0 合法；旧版本仍兼容。"""

    base = {
        "version": "1.4.0",
        "project": {"title": "占位项目"},
        "chapters": [{"title": "第一章", "chapter_number": 1}],
        "characters": [{"name": "占位角色"}],
    }
    assert ImportExportService.validate_import_data(base).valid is True
    base_old = {**base, "version": "1.3.0"}
    result_old = ImportExportService.validate_import_data(base_old)
    assert result_old.valid is True
    assert result_old.warnings == []


# ---------------------------------------------------------------- 回填脚本


class _StubBookImportService(BookImportService):
    """只替换抽取方法，避免真实模型调用；upsert 用真实实现以覆盖落库行为。"""

    def __init__(self, prompt_content: str = "回填画像提示词") -> None:
        super().__init__()
        self.prompt_content = prompt_content
        self.calls: list[tuple[str, int]] = []

    async def _extract_style_profile(self, *, db, user_id, project, chapters, **kwargs):
        self.calls.append((project.id, len(chapters)))
        return {
            "skipped": False,
            "prompt_content": self.prompt_content,
            "description": "由脚本回填；样本：第 1 章（共 10 字，全章采样）",
            "degraded": False,
            "confidence": 0.7,
            "sample_chars": 10,
            "profile_data": {**PROFILE_DATA, "source": "model"},
            "profile_version": 1,
            "ranges": [],
        }


async def _seed_backfill_projects(db):
    """三个项目：a 缺画像、b 已有画像、c 无章节。"""
    db.add_all(
        [
            Project(id="project-a", user_id=USER_ID, title="占位项目A"),
            Project(id="project-b", user_id=USER_ID, title="占位项目B"),
            Project(id="project-c", user_id=USER_ID, title="占位项目C"),
        ]
    )
    db.add_all(
        [
            Chapter(
                project_id="project-a",
                title="第1章",
                chapter_number=1,
                content="占位正文。",
                word_count=5,
                status="draft",
            ),
            Chapter(
                project_id="project-b",
                title="第1章",
                chapter_number=1,
                content="占位正文。",
                word_count=5,
                status="draft",
            ),
        ]
    )
    db.add(
        WritingStyle(
            id=10,
            user_id=USER_ID,
            name="文笔画像·占位项目B",
            style_type="profile",
            preset_id=profile_preset_id("project-b"),
            prompt_content="已有画像内容",
            profile_json=json.dumps(PROFILE_DATA, ensure_ascii=False),
            profile_version=1,
        )
    )
    await db.commit()


async def _profile_rows(db, project_id: str):
    return (
        await db.execute(
            select(WritingStyle).where(
                WritingStyle.preset_id == profile_preset_id(project_id)
            )
        )
    ).scalars().all()


def test_backfill_dry_run_writes_nothing_and_apply_fills_only_gaps():
    async def scenario(db):
        await _seed_backfill_projects(db)
        service = _StubBookImportService()

        dry = await backfill_projects(db, dry_run=True, service=service, log=lambda _: None)
        assert dry["candidates"] == 1
        assert dry["planned"] == 1
        assert dry["written"] == 0
        assert dry["skipped_no_chapters"] == 1
        assert dry["skipped_existing"] == 1
        assert service.calls == [("project-a", 1)]
        # 已回滚：库里仍无 project-a 画像
        assert await _profile_rows(db, "project-a") == []

        applied = await backfill_projects(db, dry_run=False, service=service, log=lambda _: None)
        assert applied["written"] == 1
        rows = await _profile_rows(db, "project-a")
        assert len(rows) == 1
        assert rows[0].style_type == "profile"
        assert rows[0].prompt_content == "回填画像提示词"
        assert json.loads(rows[0].profile_json)["source"] == "model"
        # 默认不动（未传 --set-default）
        assert (
            await db.execute(
                select(ProjectDefaultStyle).where(
                    ProjectDefaultStyle.project_id == "project-a"
                )
            )
        ).scalar_one_or_none() is None

        # 幂等：二次运行无变化
        again = await backfill_projects(db, dry_run=False, service=service, log=lambda _: None)
        assert again["written"] == 0
        assert again["skipped_existing"] == 2
        assert len(await _profile_rows(db, "project-a")) == 1

    _run(scenario)


def test_backfill_force_overwrites_and_set_default():
    async def scenario(db):
        await _seed_backfill_projects(db)
        service = _StubBookImportService("第一版画像")
        await backfill_projects(db, dry_run=False, service=service, log=lambda _: None)

        row = (await _profile_rows(db, "project-a"))[0]
        # 模拟用户手改
        row.style_type = "profile_edited"
        row.prompt_content = "用户手改内容"
        await db.commit()

        # 不带 --force：保留用户编辑
        service2 = _StubBookImportService("第二版画像")
        skipped = await backfill_projects(
            db,
            dry_run=False,
            project_id="project-a",
            service=service2,
            log=lambda _: None,
        )
        assert skipped["written"] == 0
        assert skipped["skipped_existing"] == 1
        assert (await _profile_rows(db, "project-a"))[0].prompt_content == "用户手改内容"

        # --force + --set-default：覆盖并替换默认
        forced = await backfill_projects(
            db,
            dry_run=False,
            project_id="project-a",
            service=service2,
            set_default=True,
            force=True,
            log=lambda _: None,
        )
        assert forced["written"] == 1
        rows = await _profile_rows(db, "project-a")
        assert len(rows) == 1
        assert rows[0].style_type == "profile"
        assert rows[0].prompt_content == "第二版画像"
        default = (
            await db.execute(
                select(ProjectDefaultStyle).where(
                    ProjectDefaultStyle.project_id == "project-a"
                )
            )
        ).scalar_one()
        assert default.style_id == rows[0].id

    _run(scenario)


def test_backfill_no_candidates_exits_clean():
    async def scenario(db):
        db.add(Project(id="empty", user_id=USER_ID, title="占位项目"))
        await db.commit()
        report = await backfill_projects(db, dry_run=False, log=lambda _: None)
        assert report["scanned"] == 1
        assert report["candidates"] == 0
        assert report["written"] == 0

    _run(scenario)
