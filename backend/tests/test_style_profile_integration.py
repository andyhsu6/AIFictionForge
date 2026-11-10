"""issue #254 P0 集成回归：文笔画像抽取、落库、API/导出隔离与删除清理。

覆盖计划 §5.7 验收的服务层部分（不跑完整 apply_import_stream，避免嵌入模型
与真实 AI 依赖）：
- 合法 JSON → 画像行 `preset_id=profile:<project_id>` 且成为项目默认；
- 二次写入复用同一行；
- 用户编辑（profile_edited）默认不被覆盖，显式授权才覆盖；
- 模型失败降级本地统计（degraded=True），不产生 failed_step；
- 项目风格列表只返回本项目画像，用户风格库与导出包/导入端不含画像；
- 项目删除清理画像行。

用 `asyncio.run` 包裹异步场景（与本目录既有测试一致，避免 pytest async fixture
插件差异）。
"""
import asyncio
import os
import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from starlette.requests import Request

from app.api.writing_styles import (
    create_writing_style,
    get_project_styles,
    get_user_styles,
    update_writing_style,
)
from app.core.errors import ApiError
from app.database import Base
from app.models.project import Project
from app.models.project_default_style import ProjectDefaultStyle
from app.models.writing_style import WritingStyle
from app.schemas.writing_style import WritingStyleCreate, WritingStyleUpdate
from app.services.book_import_service import BookImportService
from app.services.cascade_cleanup import delete_project_children
from app.services.import_export_service import ImportExportService
from app.services.style_profile import (
    build_style_sample,
    local_stats_fallback,
    profile_preset_id,
)

USER_ID = "user-1"

VALID_PROFILE = {
    "version": 1,
    "confidence": 0.82,
    "metrics": {
        "avg_sentence_chars": 16.4,
        "median_sentence_chars": 15,
        "short_sentence_ratio": 0.55,
        "dialogue_ratio": 0.28,
        "paragraph_chars_p50": 62,
    },
    "narrative": {
        "pov": "第三人称限制视角",
        "tense": "过去时",
        "distance": "贴近人物内心",
    },
    "diction": {
        "register": "口语化白话",
        "concreteness": "以具体动作与物件为主",
        "idiom_density": "低",
    },
    "dialogue": {
        "attribution": "多用动作提示，少用说话动词",
        "tag_density": "每4-6句对话一次",
    },
    "rhetoric": ["比喻密度低"],
    "do": ["平均句长控制在 13–19 字", "对话占比约 28%", "每段不超过 120 字"],
    "dont": ["避免连续 3 句以上超过 40 字", "避免成语堆砌", "避免全知视角插入评论"],
    "evidence_chapters": [1, 5, 9],
}


def _run(scenario) -> None:
    """为每个测试建立独立临时 SQLite，跑异步场景后清理。"""

    async def _main() -> None:
        db_path = f"/tmp/test_style_profile_integration_{uuid.uuid4().hex}.db"
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
    req = Request({"type": "http", "method": "GET", "path": "/", "headers": []})
    req.state.user_id = user_id
    return req


def fake_ai(payload=None, error=None):
    ai = AsyncMock()
    if error is not None:
        ai.call_with_json_retry = AsyncMock(side_effect=error)
    else:
        ai.call_with_json_retry = AsyncMock(return_value=payload)
    return ai


async def seed_project(db, *, default_style_id=None):
    project = Project(id="project-a", user_id=USER_ID, title="占位项目", current_words=0)
    preset = WritingStyle(
        id=1,
        user_id=None,
        name="预设一",
        style_type="preset",
        preset_id="natural",
        prompt_content="预设内容",
        order_index=1,
    )
    other_project_profile = WritingStyle(
        id=2,
        user_id=USER_ID,
        name="他项目画像",
        style_type="profile",
        preset_id=profile_preset_id("project-b"),
        prompt_content="他项目内容",
    )
    db.add_all([project, preset, other_project_profile])
    if default_style_id is not None:
        db.add(ProjectDefaultStyle(project_id=project.id, style_id=default_style_id))
    await db.commit()
    return project, preset


def sample_chapters(count: int = 10) -> list[SimpleNamespace]:
    return [
        SimpleNamespace(
            chapter_number=index,
            title=f"第{index}章",
            content="占位正文段落。" * 60,
        )
        for index in range(1, count + 1)
    ]


async def extract_and_upsert(db, project, *, ai, replace_default=True, allow_overwrite_edited=False):
    svc = BookImportService()
    profile = await svc._extract_style_profile(
        db=db,
        user_id=USER_ID,
        project=project,
        chapters=sample_chapters(),
        ai_service=ai,
        content_language="zh",
        extract_mode="full",
    )
    style_id = await svc._upsert_project_profile_style(
        db=db,
        project=project,
        user_id=USER_ID,
        prompt_content=profile["prompt_content"],
        description=profile["description"],
        replace_default=replace_default,
        allow_overwrite_edited=allow_overwrite_edited,
    )
    await db.commit()
    return profile, style_id


async def profile_rows(db, project_id="project-a"):
    return (
        await db.execute(
            select(WritingStyle).where(
                WritingStyle.preset_id == profile_preset_id(project_id)
            )
        )
    ).scalars().all()


def test_extract_and_upsert_creates_profile_and_switches_default():
    async def scenario(db):
        project, _ = await seed_project(db, default_style_id=1)
        profile, style_id = await extract_and_upsert(
            db, project, ai=fake_ai(VALID_PROFILE)
        )

        assert profile["degraded"] is False
        rows = await profile_rows(db)
        assert len(rows) == 1
        assert rows[0].style_type == "profile"
        assert rows[0].id == style_id
        assert "平均句长" in rows[0].prompt_content
        # description 只含章号/字数，不含样本原文
        assert "占位正文" not in rows[0].description
        assert "第" in rows[0].description
        default = (
            await db.execute(
                select(ProjectDefaultStyle).where(
                    ProjectDefaultStyle.project_id == project.id
                )
            )
        ).scalar_one()
        assert default.style_id == style_id

    _run(scenario)


def test_second_upsert_reuses_same_row():
    async def scenario(db):
        project, _ = await seed_project(db, default_style_id=1)
        _, first_id = await extract_and_upsert(db, project, ai=fake_ai(VALID_PROFILE))
        _, second_id = await extract_and_upsert(db, project, ai=fake_ai(VALID_PROFILE))

        assert first_id == second_id
        assert len(await profile_rows(db)) == 1

    _run(scenario)


def test_user_edited_profile_is_not_overwritten_by_default():
    async def scenario(db):
        project, _ = await seed_project(db, default_style_id=1)
        _, style_id = await extract_and_upsert(db, project, ai=fake_ai(VALID_PROFILE))

        row = (
            await db.execute(select(WritingStyle).where(WritingStyle.id == style_id))
        ).scalar_one()
        row.style_type = "profile_edited"
        row.prompt_content = "用户手改内容"
        await db.commit()

        _, updated_id = await extract_and_upsert(
            db, project, ai=fake_ai(VALID_PROFILE), replace_default=False
        )
        assert updated_id is None
        row = (
            await db.execute(select(WritingStyle).where(WritingStyle.id == style_id))
        ).scalar_one()
        assert row.style_type == "profile_edited"
        assert row.prompt_content == "用户手改内容"

        # 显式授权后才覆盖
        _, overwritten_id = await extract_and_upsert(
            db,
            project,
            ai=fake_ai(VALID_PROFILE),
            replace_default=False,
            allow_overwrite_edited=True,
        )
        assert overwritten_id == style_id
        row = (
            await db.execute(select(WritingStyle).where(WritingStyle.id == style_id))
        ).scalar_one()
        assert row.style_type == "profile"

    _run(scenario)


def test_model_failure_degrades_to_local_stats():
    async def scenario(db):
        project, _ = await seed_project(db, default_style_id=1)
        profile, style_id = await extract_and_upsert(
            db, project, ai=fake_ai(error=RuntimeError("upstream down"))
        )

        assert profile["degraded"] is True
        assert style_id is not None
        rows = await profile_rows(db)
        assert len(rows) == 1
        assert rows[0].prompt_content  # 本地统计兜底仍产生可注入内容

    _run(scenario)


def test_project_style_list_isolated_from_other_projects():
    async def scenario(db):
        project, _ = await seed_project(db, default_style_id=1)
        _, style_id = await extract_and_upsert(db, project, ai=fake_ai(VALID_PROFILE))

        project_styles = await get_project_styles(project.id, make_request(), db)
        preset_ids = {style["preset_id"] for style in project_styles["styles"]}
        assert profile_preset_id(project.id) in preset_ids
        assert profile_preset_id("project-b") not in preset_ids
        assert any(
            style["id"] == style_id and style["is_default"]
            for style in project_styles["styles"]
        )

        user_styles = await get_user_styles(make_request(), db)
        assert all(
            not (style["preset_id"] or "").startswith("profile:")
            for style in user_styles["styles"]
        )

    _run(scenario)


def test_export_excludes_profiles():
    async def scenario(db):
        project, _ = await seed_project(db, default_style_id=1)
        await extract_and_upsert(db, project, ai=fake_ai(VALID_PROFILE))

        exported = await ImportExportService._export_writing_styles(project.id, db)
        assert all(
            not (style.preset_id or "").startswith("profile:") for style in exported
        )
        # 默认风格是画像时，导出端不写 project_default_style
        assert (
            await ImportExportService._export_project_default_style(project.id, db)
            is None
        )

    _run(scenario)


def test_import_skips_profile_rows():
    async def scenario(db):
        project, _ = await seed_project(db, default_style_id=1)

        count = await ImportExportService._import_writing_styles(
            project.id,
            [
                {
                    "name": "外来画像",
                    "style_type": "profile",
                    "preset_id": profile_preset_id("project-b"),
                    "description": "",
                    "prompt_content": "x",
                    "order_index": 0,
                },
                {
                    "name": "普通自定义",
                    "style_type": "custom",
                    "preset_id": None,
                    "description": "",
                    "prompt_content": "y",
                    "order_index": 0,
                },
            ],
            db,
        )
        await db.commit()

        assert count == 1
        assert (
            await db.execute(
                select(WritingStyle).where(WritingStyle.name == "外来画像")
            )
        ).scalar_one_or_none() is None
        assert (
            await db.execute(
                select(WritingStyle).where(WritingStyle.name == "普通自定义")
            )
        ).scalar_one_or_none() is not None

    _run(scenario)


def test_delete_project_cleans_profile_row():
    async def scenario(db):
        project, _ = await seed_project(db, default_style_id=1)
        await extract_and_upsert(db, project, ai=fake_ai(VALID_PROFILE))
        assert len(await profile_rows(db)) == 1

        counts = await delete_project_children(db, project.id)

        assert counts["style_profiles"] == 1
        assert await profile_rows(db) == []

    _run(scenario)


def test_user_edit_marks_profile_as_profile_edited():
    async def scenario(db):
        project, _ = await seed_project(db, default_style_id=1)
        _, style_id = await extract_and_upsert(db, project, ai=fake_ai(VALID_PROFILE))

        response = await update_writing_style(
            style_id,
            WritingStyleUpdate(prompt_content="用户改写"),
            make_request(),
            db,
        )

        assert response["style_type"] == "profile_edited"

    _run(scenario)


def test_sample_build_failure_propagates_for_failed_step(monkeypatch):
    """抽样/渲染阶段的硬失败必须向上抛，主流程才能记 failed_steps 并保留首预设。

    （模型失败走本地统计兜底，不抛；只有兜底之外的异常才成为失败步骤。）
    """
    import app.services.book_import_service as module

    def boom(*args, **kwargs):
        raise RuntimeError("sample build failed")

    monkeypatch.setattr(module, "build_style_sample", boom)

    async def scenario(db):
        project, _ = await seed_project(db, default_style_id=1)
        svc = BookImportService()
        with pytest.raises(RuntimeError):
            await svc._extract_style_profile(
                db=db,
                user_id=USER_ID,
                project=project,
                chapters=sample_chapters(),
                ai_service=fake_ai(VALID_PROFILE),
            )
        # 抛错前没有写入画像行，默认仍是首个预设
        assert await profile_rows(db) == []
        default = (
            await db.execute(
                select(ProjectDefaultStyle).where(
                    ProjectDefaultStyle.project_id == project.id
                )
            )
        ).scalar_one()
        assert default.style_id == 1

    _run(scenario)


def test_insufficient_sample_skips_profile_without_touching_default():
    """样本不足时不写画像、不改默认（避免本地统计默认值顶掉项目默认风格）。"""

    async def scenario(db):
        project, _ = await seed_project(db, default_style_id=1)
        svc = BookImportService()
        profile = await svc._extract_style_profile(
            db=db,
            user_id=USER_ID,
            project=project,
            chapters=[SimpleNamespace(chapter_number=1, title="短章", content="太短")],
            ai_service=fake_ai(VALID_PROFILE),
        )

        assert profile["skipped"] is True
        assert await profile_rows(db) == []
        default = (
            await db.execute(
                select(ProjectDefaultStyle).where(
                    ProjectDefaultStyle.project_id == project.id
                )
            )
        ).scalar_one()
        assert default.style_id == 1

    _run(scenario)


def test_create_rejects_reserved_profile_prefix():
    async def scenario(db):
        await seed_project(db)
        with pytest.raises(ApiError) as exc_info:
            await create_writing_style(
                WritingStyleCreate(
                    name="伪装画像",
                    preset_id=profile_preset_id("project-a"),
                    prompt_content="x",
                ),
                make_request(),
                db,
            )
        assert exc_info.value.code == "validation.profile_preset_reserved"

    _run(scenario)


# ---- issue #279：metrics 必须来自实测，不采信模型自报值 ----

# 每句 20 字、每段 40 字；与 MODEL_CLAIMED_PROFILE 声称的 130 字/段、0.45 对话占比明显不同
_CONTROLLED_SENTENCE = "占" * 19 + "。"
_CONTROLLED_PARAGRAPH = _CONTROLLED_SENTENCE * 2

MODEL_CLAIMED_PROFILE = {
    **VALID_PROFILE,
    "metrics": {
        "avg_sentence_chars": 12.5,
        "median_sentence_chars": 12,
        "short_sentence_ratio": 0.6,
        "dialogue_ratio": 0.45,
        "paragraph_chars_p50": 130,
    },
}


def measured_chapters(count: int = 3) -> list[SimpleNamespace]:
    content = "\n\n".join([_CONTROLLED_PARAGRAPH] * 10)
    return [
        SimpleNamespace(chapter_number=index, title=f"第{index}章", content=content)
        for index in range(1, count + 1)
    ]


def measured_metrics(chapters) -> dict:
    return local_stats_fallback(build_style_sample(chapters))["metrics"]


async def extract_profile(project, db, *, ai, chapters):
    return await BookImportService()._extract_style_profile(
        db=db,
        user_id=USER_ID,
        project=project,
        chapters=chapters,
        ai_service=ai,
        content_language="zh",
    )


def test_model_metrics_are_overridden_by_measured_values(caplog):
    chapters = measured_chapters()
    expected = measured_metrics(chapters)

    async def scenario(db):
        project, _ = await seed_project(db, default_style_id=1)
        with caplog.at_level("INFO", logger="app.services.book_import_service"):
            profile = await extract_profile(
                project, db, ai=fake_ai(MODEL_CLAIMED_PROFILE), chapters=chapters
            )

        metrics = profile["profile_data"]["metrics"]
        assert metrics == expected
        assert metrics["paragraph_chars_p50"] == 40
        assert metrics["avg_sentence_chars"] == pytest.approx(20.0)
        assert metrics["dialogue_ratio"] == 0  # 模型声称 0.45
        # 质性字段仍来自模型（未降级）
        assert profile["degraded"] is False
        assert profile["profile_data"]["source"] == "model"
        assert (
            profile["profile_data"]["narrative"]["pov"]
            == MODEL_CLAIMED_PROFILE["narrative"]["pov"]
        )

    _run(scenario)

    messages = [
        record.getMessage()
        for record in caplog.records
        if "以实测覆盖模型估计" in record.getMessage()
    ]
    assert len(messages) == 1
    # 只记偏差 >30% 的键，且日志不含任何原文
    assert "paragraph_chars_p50 130->40" in messages[0]
    assert "avg_sentence_chars 12.5->20.0" in messages[0]
    assert "dialogue_ratio 0.45->0.0" in messages[0]
    assert "占" not in messages[0]


def test_prompt_content_renders_measured_metrics():
    chapters = measured_chapters()
    expected = measured_metrics(chapters)
    assert expected["paragraph_chars_p50"] == 40
    assert round(expected["avg_sentence_chars"]) == 20

    async def scenario(db):
        project, _ = await seed_project(db, default_style_id=1)
        profile = await extract_profile(
            project, db, ai=fake_ai(MODEL_CLAIMED_PROFILE), chapters=chapters
        )

        content = profile["prompt_content"]
        assert "- 平均句长：20 字（中位 20 字）" in content
        assert "- 段落长度中位：40 字" in content
        assert "- 对话占比：0%" in content
        # 模型声称的数值不得出现在注入内容里
        assert "130" not in content
        assert "45%" not in content
        assert "12.5" not in content

    _run(scenario)


def test_model_failure_metrics_stay_measured():
    chapters = measured_chapters()
    expected = measured_metrics(chapters)

    async def scenario(db):
        project, _ = await seed_project(db, default_style_id=1)
        profile = await extract_profile(
            project,
            db,
            ai=fake_ai(error=RuntimeError("upstream down")),
            chapters=chapters,
        )

        assert profile["degraded"] is True
        assert profile["profile_data"]["source"] == "local"
        assert profile["profile_data"]["metrics"] == expected

    _run(scenario)
