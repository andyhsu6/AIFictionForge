"""issue #254 P1：原文语感锚点的注入行为与项目级选项校验。

覆盖契约模块 `app.services.style_anchors` 的读取侧（注入/开关/预算/哈希/章号过滤）
与 PUT /api/projects/{id} 的锚点选项夹取。用临时 SQLite + `Base.metadata.create_all`，
`asyncio.run` 包裹场景（与本目录既有测试一致），不触达 embedding 与真实 AI。
"""
import asyncio
import os
import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from starlette.requests import Request

from app.api.projects import update_project
from app.database import Base
from app.models.chapter import Chapter
from app.models.project import Project
from app.models.project_style_anchor import ProjectStyleAnchor
from app.schemas.project import ProjectUpdate
from app.services.style_anchors import (
    append_project_style_anchors,
    create_project_style_anchors,
    load_style_anchor_fragments,
)

USER_ID = "user-1"

MARKER_A = "雨落在青石板上"
MARKER_B = "海风从窗缝里钻进来"

TEXT_A = (
    "雨落在青石板上，声音细密。他沿着巷子往里走，墙头爬着半枯的藤。"
    "第三家没有点灯，门缝里透出一线灰白。他停下，抬手敲了三下。"
) * 4
TEXT_B = (
    "海风从窗缝里钻进来，带着咸味。桌上摊着一叠旧信，边角已经卷起。"
    "他没有点灯，只是把窗推开一条缝，看远处的船影在雾里慢慢淡去。"
) * 4

STYLE_CONTENT = "语感要求：句子偏短，多写动作与物件，少用形容词。"


def _run(scenario) -> None:
    """为每个测试建立独立临时 SQLite，跑异步场景后清理。"""

    async def _main() -> None:
        db_path = f"/tmp/test_style_anchors_{uuid.uuid4().hex}.db"
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
    req = Request({"type": "http", "method": "PUT", "path": "/", "headers": []})
    req.state.user_id = user_id
    return req


async def seed_project(db, *, chapters: int = 2) -> Project:
    project = Project(id="project-a", user_id=USER_ID, title="占位项目", current_words=0)
    db.add(project)
    contents = {1: TEXT_A, 2: TEXT_B}
    for number in range(1, chapters + 1):
        db.add(
            Chapter(
                id=f"chapter-{number}",
                project_id=project.id,
                chapter_number=number,
                title=f"第{number}章",
                content=contents[number],
                word_count=len(contents[number]),
            )
        )
    await db.commit()
    return project


def anchor_payload_length(injected: str) -> int:
    """统计注入块中锚点正文的长度（排除原风格内容、标题行与 [锚点 N] 标签行）。"""
    payload = injected[len(STYLE_CONTENT):]
    return sum(
        len(line)
        for line in payload.splitlines()
        if line and not line.startswith("【") and not line.startswith("[锚点")
    )


def test_injects_anchor_block():
    async def scenario(db):
        project = await seed_project(db, chapters=1)
        created = await create_project_style_anchors(
            db, project_id=project.id, ranges=[{"chapter": 1, "start": 0, "end": len(TEXT_A)}]
        )
        assert created > 0

        result = await append_project_style_anchors(
            db, project=project, style_content=STYLE_CONTENT
        )

        assert result.startswith(STYLE_CONTENT)
        assert "锚点" in result
        assert MARKER_A in result

    _run(scenario)


def test_disabled_returns_original():
    async def scenario(db):
        project = await seed_project(db, chapters=1)
        await create_project_style_anchors(
            db, project_id=project.id, ranges=[{"chapter": 1, "start": 0, "end": len(TEXT_A)}]
        )
        project.style_anchor_enabled = False

        result = await append_project_style_anchors(
            db, project=project, style_content=STYLE_CONTENT
        )

        assert result == STYLE_CONTENT
        assert "锚点" not in result

    _run(scenario)


def test_char_budget_is_respected():
    async def scenario(db):
        project = await seed_project(db, chapters=2)
        await create_project_style_anchors(
            db,
            project_id=project.id,
            ranges=[
                {"chapter": 1, "start": 0, "end": len(TEXT_A)},
                {"chapter": 2, "start": 0, "end": len(TEXT_B)},
            ],
        )
        project.style_anchor_chars = 300

        result = await append_project_style_anchors(
            db, project=project, style_content=STYLE_CONTENT
        )
        fragments = await load_style_anchor_fragments(db, project=project)

        assert fragments
        assert anchor_payload_length(result) <= 300
        assert sum(len(fragment) for fragment in fragments) <= 300

    _run(scenario)


def test_hash_mismatch_skips_anchor():
    async def scenario(db):
        project = await seed_project(db, chapters=1)
        await create_project_style_anchors(
            db, project_id=project.id, ranges=[{"chapter": 1, "start": 0, "end": len(TEXT_A)}]
        )
        chapter = await db.get(Chapter, "chapter-1")
        chapter.content = "改" + chapter.content
        await db.commit()

        result = await append_project_style_anchors(
            db, project=project, style_content=STYLE_CONTENT
        )

        assert result == STYLE_CONTENT
        assert "锚点" not in result

    _run(scenario)


def test_before_chapter_number_filters():
    async def scenario(db):
        project = await seed_project(db, chapters=2)
        await create_project_style_anchors(
            db,
            project_id=project.id,
            ranges=[
                {"chapter": 1, "start": 0, "end": len(TEXT_A)},
                {"chapter": 2, "start": 0, "end": len(TEXT_B)},
            ],
        )

        result = await append_project_style_anchors(
            db,
            project=project,
            style_content=STYLE_CONTENT,
            before_chapter_number=2,
        )

        assert MARKER_A in result
        assert MARKER_B not in result

    _run(scenario)


def test_update_project_clamps_anchor_settings():
    async def scenario(db):
        project = await seed_project(db, chapters=1)

        updated = await update_project(
            "project-a",
            ProjectUpdate(
                style_anchor_chars=5000,
                style_anchor_count=10,
                style_anchor_enabled=False,
            ),
            db=db,
            request=make_request(),
        )

        assert updated.style_anchor_chars == 2000
        assert updated.style_anchor_count == 4
        assert updated.style_anchor_enabled is False

        project = await db.get(Project, "project-a")
        assert project.style_anchor_chars == 2000
        assert project.style_anchor_count == 4

    _run(scenario)


def test_create_anchors_with_empty_ranges_clears_previous_rows():
    """重抽后样本区间为空时不能残留上一轮锚点（回归 MAJOR）。"""

    async def scenario(db):
        await seed_project(db, chapters=1)
        created = await create_project_style_anchors(
            db,
            project_id="project-a",
            ranges=[{"chapter": 1, "start": 0, "end": 160}],
        )
        assert created > 0

        cleared = await create_project_style_anchors(
            db, project_id="project-a", ranges=[]
        )
        assert cleared == 0
        rows = (
            await db.execute(
                select(ProjectStyleAnchor).where(
                    ProjectStyleAnchor.project_id == "project-a"
                )
            )
        ).scalars().all()
        assert rows == []

    _run(scenario)
