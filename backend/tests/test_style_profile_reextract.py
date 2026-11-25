"""issue #254 P1：项目文笔画像重新抽取端点回归。

覆盖 `POST /writing-styles/project/{project_id}/profile/reextract`：
- dry_run=True 只返回 diff，不写库、不动默认风格；
- dry_run=False 建画像行（含 profile_json）并重建锚点；
- 用户手改（profile_edited）默认保留，显式授权才覆盖；
- set_default 控制是否把画像设为项目默认；
- 样本不足直接 skipped，不写库。

抽取方法整体被 mock（不跑真实 AI / embedding），DB 用临时 SQLite。
"""
import asyncio
import json
import os
import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from starlette.requests import Request

from app.api.writing_styles import reextract_project_profile
from app.database import Base
from app.models.chapter import Chapter
from app.models.project import Project
from app.models.project_default_style import ProjectDefaultStyle
from app.models.project_style_anchor import ProjectStyleAnchor
from app.models.writing_style import WritingStyle
from app.schemas.writing_style import ProfileReextractRequest
from app.services.book_import_service import BookImportService
from app.services.style_profile import profile_preset_id

USER_ID = "user-1"
# 无引号的连贯正文：既可做抽样样本，也能通过锚点的对话占比过滤
CHAPTER_TEXT = "夜风吹过窗棂，灯影在墙上轻轻晃动。" * 20

PROFILE_RESULT = {
    "skipped": False,
    "prompt_content": "【文笔画像】平均句长 16 字，对话占比约 28%",
    "description": "由拆书导入自动抽取，可编辑；样本：第 1 章（共 360 字，全章采样）",
    "degraded": False,
    "confidence": 0.8,
    "sample_chars": len(CHAPTER_TEXT),
    "profile_data": {
        "version": 1,
        "confidence": 0.8,
        "source": "model",
        "do": ["平均句长控制在 13–19 字"],
    },
    "profile_version": 1,
    "ranges": [{"chapter": 1, "start": 0, "end": 300}],
}


def _run(scenario) -> None:
    """为每个测试建立独立临时 SQLite，跑异步场景后清理。"""

    async def _main() -> None:
        db_path = f"/tmp/test_style_profile_reextract_{uuid.uuid4().hex}.db"
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


def patch_extract(monkeypatch, payload: dict) -> None:
    """替换实例方法：返回固定抽取结果，避免真实 AI 调用。"""

    async def _fake_extract(
        self,
        *,
        db,
        user_id,
        project,
        chapters,
        ai_service=None,
        content_language=None,
        extract_mode="full",
    ):
        return dict(payload)

    monkeypatch.setattr(BookImportService, "_extract_style_profile", _fake_extract)


async def seed_project(db, *, default_style_id=1):
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
    chapter = Chapter(
        id="chapter-1",
        project_id=project.id,
        chapter_number=1,
        title="第1章",
        content=CHAPTER_TEXT,
    )
    db.add_all([project, preset, chapter])
    if default_style_id is not None:
        db.add(ProjectDefaultStyle(project_id=project.id, style_id=default_style_id))
    await db.commit()
    return project


async def profile_rows(db):
    return (
        await db.execute(
            select(WritingStyle).where(
                WritingStyle.preset_id == profile_preset_id("project-a")
            )
        )
    ).scalars().all()


async def anchor_count(db) -> int:
    return len(
        (await db.execute(select(ProjectStyleAnchor))).scalars().all()
    )


async def default_style_id(db):
    return (
        await db.execute(
            select(ProjectDefaultStyle.style_id).where(
                ProjectDefaultStyle.project_id == "project-a"
            )
        )
    ).scalar_one_or_none()


def test_dry_run_returns_diff_without_writing(monkeypatch):
    patch_extract(monkeypatch, PROFILE_RESULT)

    async def scenario(db):
        project = await seed_project(db)

        resp = await reextract_project_profile(project.id, make_request(), db)

        assert resp["skipped"] is False
        assert resp["changed"] is True
        assert resp["old_prompt_content"] is None
        assert resp["new_prompt_content"] == PROFILE_RESULT["prompt_content"]
        assert resp["old_profile"] is None
        assert resp["new_profile"] == PROFILE_RESULT["profile_data"]
        assert resp["confidence"] == 0.8
        assert resp["degraded"] is False
        # 不写库：无画像行、无锚点、默认不变
        assert await profile_rows(db) == []
        assert await anchor_count(db) == 0
        assert await default_style_id(db) == 1

    _run(scenario)


def test_write_creates_profile_row_and_anchors(monkeypatch):
    patch_extract(monkeypatch, PROFILE_RESULT)

    async def scenario(db):
        project = await seed_project(db)

        resp = await reextract_project_profile(
            project.id,
            make_request(),
            db,
            ProfileReextractRequest(dry_run=False),
        )

        assert resp["updated"] is True
        assert resp["kept_reason"] is None
        rows = await profile_rows(db)
        assert len(rows) == 1
        assert rows[0].id == resp["style_id"]
        assert rows[0].style_type == "profile"
        assert rows[0].prompt_content == PROFILE_RESULT["prompt_content"]
        # profile_json 落库且可解析回结构化画像
        assert json.loads(rows[0].profile_json) == PROFILE_RESULT["profile_data"]
        assert rows[0].profile_version == 1
        # 锚点已创建（只存定位与哈希）
        assert await anchor_count(db) > 0

        # 同内容再抽一次：diff 为未变化
        again = await reextract_project_profile(project.id, make_request(), db)
        assert again["changed"] is False
        assert again["old_prompt_content"] == PROFILE_RESULT["prompt_content"]

    _run(scenario)


def test_edited_profile_kept_unless_overwrite(monkeypatch):
    patch_extract(monkeypatch, PROFILE_RESULT)

    async def scenario(db):
        project = await seed_project(db)
        first = await reextract_project_profile(
            project.id,
            make_request(),
            db,
            ProfileReextractRequest(dry_run=False),
        )
        row = (
            await db.execute(select(WritingStyle).where(WritingStyle.id == first["style_id"]))
        ).scalar_one()
        row.style_type = "profile_edited"
        row.prompt_content = "用户手改内容"
        await db.commit()

        kept = await reextract_project_profile(
            project.id,
            make_request(),
            db,
            ProfileReextractRequest(dry_run=False, overwrite_edited=False),
        )
        assert kept["updated"] is False
        assert kept["style_id"] is None
        assert kept["kept_reason"] == "user_edited"
        assert len(await profile_rows(db)) == 1
        row = (
            await db.execute(select(WritingStyle).where(WritingStyle.id == first["style_id"]))
        ).scalar_one()
        assert row.prompt_content == "用户手改内容"
        assert row.style_type == "profile_edited"

        # 预览阶段也要暴露手改保护，前端据此提示并禁用"应用"
        preview = await reextract_project_profile(
            project.id,
            make_request(),
            db,
            ProfileReextractRequest(dry_run=True),
        )
        assert preview["kept_reason"] == "user_edited"
        assert preview["old_prompt_content"] == "用户手改内容"

        overwritten = await reextract_project_profile(
            project.id,
            make_request(),
            db,
            ProfileReextractRequest(dry_run=False, overwrite_edited=True),
        )
        assert overwritten["updated"] is True
        assert overwritten["style_id"] == first["style_id"]
        row = (
            await db.execute(select(WritingStyle).where(WritingStyle.id == first["style_id"]))
        ).scalar_one()
        assert row.style_type == "profile"
        assert row.prompt_content == PROFILE_RESULT["prompt_content"]
        assert len(await profile_rows(db)) == 1

    _run(scenario)


def test_set_default_controls_project_default(monkeypatch):
    patch_extract(monkeypatch, PROFILE_RESULT)

    async def scenario(db):
        project = await seed_project(db)

        no_default = await reextract_project_profile(
            project.id,
            make_request(),
            db,
            ProfileReextractRequest(dry_run=False, set_default=False),
        )
        assert no_default["updated"] is True
        assert await default_style_id(db) == 1

        as_default = await reextract_project_profile(
            project.id,
            make_request(),
            db,
            ProfileReextractRequest(dry_run=False, set_default=True),
        )
        assert as_default["style_id"] == no_default["style_id"]
        assert await default_style_id(db) == as_default["style_id"]

    _run(scenario)


def test_insufficient_sample_skips_without_writing(monkeypatch):
    patch_extract(monkeypatch, {"skipped": True, "sample_chars": 0})

    async def scenario(db):
        project = await seed_project(db)

        resp = await reextract_project_profile(
            project.id,
            make_request(),
            db,
            ProfileReextractRequest(dry_run=False, set_default=True),
        )

        assert resp["skipped"] is True
        assert await profile_rows(db) == []
        assert await anchor_count(db) == 0
        assert await default_style_id(db) == 1

    _run(scenario)


def _valid_v2_profile(*, source: str = "model") -> dict:
    return {
        "version": 2,
        "confidence": 0.85,
        "source": source,
        "impression": "冷峻克制、动作连贯，多以环境映衬心境",
        "texture": "动词精准、名词偏向具象器物与声响",
        "rhythm": "短句起手蓄势，长短交错推进冲突",
        "tone": "冷静抽离中带有暗涌张力",
        "scene_shifts": ["冲突升级时转用短拍推进", "对话间隙以景物切面收束"],
        "metrics": {
            "avg_sentence_chars": 24.5,
            "median_sentence_chars": 22.0,
            "short_sentence_ratio": 0.28,
            "dialogue_ratio": 0.25,
            "paragraph_chars_p50": 42,
        },
        "narrative": {"pov": "第三人称有限", "tense": "过去时质感", "distance": "近景偏中"},
        "dialogue": {"attribution": "多省略提示词，动作承接话锋", "tag_density": "低"},
        "rhetoric": ["动静对照", "白描细节"],
        "do": [
            "开场先落一个可触可感的环境细节",
            "长短句交错推进，在冲突转折处用短句落锤",
            "以动作神态接续对话，避免密集使用说道等直白标签",
        ],
        "dont": [
            "避免直接点破人物情绪结论",
            "避免一段话拆成满屏单行短句",
            "避免在节奏紧张处插入长篇议论升华",
        ],
        "evidence_chapters": [1],
    }


def test_apply_preview_payload_skips_llm_reextraction(monkeypatch):
    """前端应用已预览结果时应直接校验落库，不再重复调用 _extract_style_profile。"""
    import pytest
    from fastapi import HTTPException
    from app.schemas.writing_style import ProfileReextractApplyPayload

    calls = 0

    async def _fail_if_called(*args, **kwargs):
        nonlocal calls
        calls += 1
        raise AssertionError("应用已预览结果时不应再次调用 _extract_style_profile")

    monkeypatch.setattr(BookImportService, "_extract_style_profile", _fail_if_called)

    v2_data = _valid_v2_profile(source="model")

    async def scenario(db):
        project = await seed_project(db)

        resp = await reextract_project_profile(
            project.id,
            make_request(),
            db,
            ProfileReextractRequest(
                dry_run=False,
                set_default=True,
                apply_profile=ProfileReextractApplyPayload(
                    new_prompt_content="【本书文笔画像 v2】冷峻克制",
                    new_profile=v2_data,
                    description="样本：第 1 章（全章采样）",
                    confidence=0.85,
                    degraded=False,
                    profile_version=2,
                    ranges=[{"chapter": 1, "start": 0, "end": 300}],
                ),
            ),
        )
        assert calls == 0
        assert resp["updated"] is True
        assert resp["degraded"] is False
        assert resp["profile_version"] == 2
        assert await default_style_id(db) == resp["style_id"]
        assert await anchor_count(db) > 0
        rows = await profile_rows(db)
        assert len(rows) == 1
        assert rows[0].prompt_content == "【本书文笔画像 v2】冷峻克制"
        assert rows[0].profile_version == 2
        assert json.loads(rows[0].profile_json) == v2_data

        # 畸形预览载荷应返回 422，且不得污染已存画像
        bad_profile = dict(v2_data)
        bad_profile.pop("metrics")
        with pytest.raises(HTTPException) as exc_info:
            await reextract_project_profile(
                project.id,
                make_request(),
                db,
                ProfileReextractRequest(
                    dry_run=False,
                    apply_profile=ProfileReextractApplyPayload(
                        new_prompt_content="被篡改的提示词",
                        new_profile=bad_profile,
                    ),
                ),
            )
        assert exc_info.value.status_code == 422
        rows_after = await profile_rows(db)
        assert rows_after[0].prompt_content == "【本书文笔画像 v2】冷峻克制"

    _run(scenario)


def test_degraded_fallback_does_not_overwrite_existing_model_profile(monkeypatch):
    """已有 AI 画像（source=model）时，降级本地统计结果不得覆盖原画像。"""
    model_profile = {
        "skipped": False,
        "prompt_content": "【本书文笔画像 v2】高质量 AI 画像",
        "description": "样本：第 1 章",
        "degraded": False,
        "confidence": 0.9,
        "profile_data": _valid_v2_profile(source="model"),
        "profile_version": 2,
        "ranges": [{"chapter": 1, "start": 0, "end": 300}],
    }
    degraded_profile = {
        "skipped": False,
        "prompt_content": "【本地降级统计】未识别（以样本统计为准）",
        "description": "样本：第 1 章",
        "degraded": True,
        "confidence": 0.3,
        "profile_data": {
            "version": 1,
            "confidence": 0.3,
            "source": "local",
            "metrics": {"avg_sentence_chars": 20.0},
        },
        "profile_version": 1,
        "ranges": [{"chapter": 1, "start": 0, "end": 300}],
    }

    async def scenario(db):
        project = await seed_project(db)
        patch_extract(monkeypatch, model_profile)
        first = await reextract_project_profile(
            project.id,
            make_request(),
            db,
            ProfileReextractRequest(dry_run=False),
        )
        assert first["updated"] is True
        assert first["ranges"] == [{"chapter": 1, "start": 0, "end": 300}]

        # 再次抽取退化为本地兜底：预览和直接落库均须拦截覆盖
        patch_extract(monkeypatch, degraded_profile)
        preview = await reextract_project_profile(
            project.id,
            make_request(),
            db,
            ProfileReextractRequest(dry_run=True),
        )
        assert preview["degraded"] is True
        assert preview["kept_reason"] == "degraded_fallback"

        blocked = await reextract_project_profile(
            project.id,
            make_request(),
            db,
            ProfileReextractRequest(dry_run=False),
        )
        assert blocked["updated"] is False
        assert blocked["style_id"] is None
        assert blocked["kept_reason"] == "degraded_fallback"
        rows = await profile_rows(db)
        assert rows[0].prompt_content == "【本书文笔画像 v2】高质量 AI 画像"
        assert rows[0].profile_version == 2

    _run(scenario)


def test_apply_profile_normalizes_source_validates_version_ranges_and_degraded_priority(monkeypatch):
    """apply_profile 归一化 source、校验版本号与 ranges，且降级保护优先于手改保护。"""
    import pytest
    from fastapi import HTTPException
    from pydantic import ValidationError
    from app.schemas.writing_style import ProfileReextractApplyPayload

    # 1. StyleProfileRange 拒绝 bool / 负数
    with pytest.raises(ValidationError):
        ProfileReextractApplyPayload(
            new_prompt_content="提示词",
            new_profile=_valid_v2_profile(),
            ranges=[{"chapter": True, "start": 0, "end": 10}],
        )

    async def scenario(db):
        project = await seed_project(db)
        profile_no_source = _valid_v2_profile()
        profile_no_source.pop("source")

        # 2. profile_version 与 new_profile["version"] 不一致 -> 422
        with pytest.raises(HTTPException) as exc_info:
            await reextract_project_profile(
                project.id,
                make_request(),
                db,
                ProfileReextractRequest(
                    dry_run=False,
                    apply_profile=ProfileReextractApplyPayload(
                        new_prompt_content="提示词",
                        new_profile=profile_no_source,
                        profile_version=1,
                    ),
                ),
            )
        assert exc_info.value.status_code == 422

        # 3. 缺省 source 落库归一化为 "model"，从而后续降级抽取能命中保护
        saved = await reextract_project_profile(
            project.id,
            make_request(),
            db,
            ProfileReextractRequest(
                dry_run=False,
                apply_profile=ProfileReextractApplyPayload(
                    new_prompt_content="【AI 画像】无 source 字段测试",
                    new_profile=profile_no_source,
                    degraded=False,
                    ranges=[{"chapter": 1, "start": 0, "end": 300}],
                ),
            ),
        )
        assert saved["updated"] is True
        row = (await profile_rows(db))[0]
        assert json.loads(row.profile_json)["source"] == "model"

        # 标为手动编辑（仍含 source=model），再以 degraded=True 预览/应用：
        # 降级保护须优先于 user_edited，阻止 overwrite_edited 覆盖
        row.style_type = "profile_edited"
        await db.commit()

        patch_extract(
            monkeypatch,
            {
                "skipped": False,
                "prompt_content": "【降级】未识别",
                "description": "样本：第 1 章",
                "degraded": True,
                "confidence": 0.3,
                "profile_data": {
                    "version": 1,
                    "confidence": 0.3,
                    "source": "local",
                    "metrics": {"avg_sentence_chars": 20.0},
                },
                "profile_version": 1,
                "ranges": [{"chapter": 1, "start": 0, "end": 300}],
            },
        )
        preview = await reextract_project_profile(
            project.id,
            make_request(),
            db,
            ProfileReextractRequest(dry_run=True, overwrite_edited=False),
        )
        assert preview["kept_reason"] == "degraded_fallback"

        degraded_apply = await reextract_project_profile(
            project.id,
            make_request(),
            db,
            ProfileReextractRequest(
                dry_run=False,
                overwrite_edited=True,
                apply_profile=ProfileReextractApplyPayload(
                    new_prompt_content="【伪装 AI 的降级载荷】",
                    new_profile=_valid_v2_profile(source="model"),
                    degraded=True,
                ),
            ),
        )
        assert degraded_apply["updated"] is False
        assert degraded_apply["kept_reason"] == "degraded_fallback"
        assert (await profile_rows(db))[0].prompt_content == "【AI 画像】无 source 字段测试"

    _run(scenario)
