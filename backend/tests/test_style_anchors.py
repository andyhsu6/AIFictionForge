"""issue #254 P1：原文语感锚点的注入行为与项目级选项校验。

覆盖契约模块 `app.services.style_anchors` 的读取侧（注入/开关/预算/哈希/章号过滤）
与 PUT /api/projects/{id} 的锚点选项夹取。用临时 SQLite + `Base.metadata.create_all`，
`asyncio.run` 包裹场景（与本目录既有测试一致），不触达 embedding 与真实 AI。
"""
import asyncio
import json
import os
import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from starlette.requests import Request

from app.api.projects import update_project
from app.database import Base
from app.models.chapter import Chapter
from app.models.project import Project
from app.models.project_default_style import ProjectDefaultStyle
from app.models.project_style_anchor import ProjectStyleAnchor
from app.models.writing_style import WritingStyle
from app.schemas.project import ProjectUpdate
from app.services.style_anchors import (
    append_project_style_anchors,
    create_project_style_anchors,
    load_style_anchor_fragments,
)
from app.services.style_profile import profile_preset_id

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


# ---------------------------------------------------------------------------
# 计划 §6.3：每请求覆盖 + 锚点选择策略（同视角 / 防复读 / 动态纯对话阈值）
# ---------------------------------------------------------------------------

BASE_NARRATIVE = (
    "雨落在青石板上，声音细密。他沿着巷子往里走，墙头爬着半枯的藤。"
    "第三家没有点灯，门缝里透出一线灰白。他停下，抬手敲了三下。"
)

FIRST_PERSON_TEXT = (
    "我推开窗，看见雨还没停。我把手伸出去，掌心很快湿了。"
    "我没有关窗，只是站在那里，听雨点打在石阶上。"
) * 3 + "我记得那天的风很冷。"

THIRD_PERSON_TEXT = (
    "他推开窗，看见雨还没停。他把手伸出去，掌心很快湿了。"
    "他没有关窗，只是站在那里，听雨点打在石阶上。"
) * 3 + "他记得那天的风很冷。"

DIALOGUE_HEAVY_TEXT = "“我们明天再谈这件事。”他站在门口，看着外面的雨。"


async def seed_project_with(
    db,
    contents: dict[int, str],
    *,
    narrative_perspective: str | None = None,
) -> Project:
    """按显式章节正文播种项目（与 `seed_project` 同理，允许自定义视角/章数）。"""

    project = Project(
        id="project-a",
        user_id=USER_ID,
        title="占位项目",
        current_words=0,
        narrative_perspective=narrative_perspective,
    )
    db.add(project)
    for number, text in contents.items():
        db.add(
            Chapter(
                id=f"chapter-{number}",
                project_id=project.id,
                chapter_number=number,
                title=f"第{number}章",
                content=text,
                word_count=len(text),
            )
        )
    await db.commit()
    return project


async def create_anchors_for(db, chapters: dict[int, str]) -> None:
    await create_project_style_anchors(
        db,
        project_id="project-a",
        ranges=[
            {"chapter": number, "start": 0, "end": len(text)}
            for number, text in chapters.items()
        ],
    )


def test_override_enabled_beats_disabled_project():
    """请求 enabled=True 覆盖项目关闭状态；不传时行为不变（仍读项目列）。"""

    async def scenario(db):
        project = await seed_project(db, chapters=1)
        await create_anchors_for(db, {1: TEXT_A})
        project.style_anchor_enabled = False

        assert (
            await append_project_style_anchors(
                db, project=project, style_content=STYLE_CONTENT
            )
            == STYLE_CONTENT
        )

        injected = await append_project_style_anchors(
            db,
            project=project,
            style_content=STYLE_CONTENT,
            override={"enabled": True},
        )
        assert injected.startswith(STYLE_CONTENT)
        assert MARKER_A in injected

    _run(scenario)


def test_override_chars_beats_project_budget():
    """项目 chars=800、请求 chars=300 → 注入总量 ≤ 300。"""

    async def scenario(db):
        project = await seed_project(db, chapters=2)
        await create_anchors_for(db, {1: TEXT_A, 2: TEXT_B})
        project.style_anchor_chars = 800

        fragments = await load_style_anchor_fragments(
            db, project=project, override={"chars": 300}
        )
        injected = await append_project_style_anchors(
            db,
            project=project,
            style_content=STYLE_CONTENT,
            override={"chars": 300},
        )

        assert fragments
        assert sum(len(fragment) for fragment in fragments) <= 300
        assert anchor_payload_length(injected) <= 300

        # 不传 override 时仍按项目预算
        project_fragments = await load_style_anchor_fragments(db, project=project)
        assert sum(len(fragment) for fragment in project_fragments) <= 800

    _run(scenario)


def test_override_count_is_clamped():
    """count 越界按 2–4 夹取：请求 1 → 2 条；请求 99 → 4 条。"""

    async def scenario(db):
        contents = {
            number: BASE_NARRATIVE * 2 + f"他记住了第{number}章的标记。"
            for number in range(1, 6)
        }
        project = await seed_project_with(db, contents)
        await create_anchors_for(db, contents)

        low = await load_style_anchor_fragments(
            db, project=project, override={"count": 1, "chars": 2000}
        )
        high = await load_style_anchor_fragments(
            db, project=project, override={"count": 99, "chars": 2000}
        )

        assert len(low) == 2
        assert len(high) == 4

    _run(scenario)


def test_anchor_overlapping_target_chapter_is_skipped():
    """目标章正文已包含锚点片段（20 字滑窗命中）→ 该锚点不注入。"""

    async def scenario(db):
        project = await seed_project(db, chapters=1)
        await create_anchors_for(db, {1: TEXT_A})
        assert MARKER_A in TEXT_A

        db.add(
            Chapter(
                id="chapter-2",
                project_id=project.id,
                chapter_number=2,
                title="第2章",
                content="开篇一句。\n" + TEXT_A[:120] + "\n续写段落尚未完成。",
                word_count=0,
            )
        )
        await db.commit()

        fragments = await load_style_anchor_fragments(
            db, project=project, before_chapter_number=2
        )

        # 第 1 章整段是单条锚点（≤ 400 字），与目标章已有正文重叠 → 全部跳过
        assert fragments == []

    _run(scenario)


def test_same_perspective_anchor_ranks_first():
    """目标章第一人称时，第一人称锚点排前（邻近但不匹配者降权而非排除）。"""

    async def scenario(db):
        project = await seed_project_with(
            db,
            {
                1: FIRST_PERSON_TEXT,
                2: THIRD_PERSON_TEXT,
                3: (
                    "我站在门口，看雨沿着屋檐往下滴。我把外套往上拉了拉，"
                    "脚下的水已经漫过鞋面。我想起出门时忘了带伞，只好等雨小一点。"
                ),
            },
        )
        await create_anchors_for(db, {1: FIRST_PERSON_TEXT, 2: THIRD_PERSON_TEXT})

        fragments = await load_style_anchor_fragments(
            db, project=project, before_chapter_number=3, override={"count": 2}
        )

        assert len(fragments) == 2
        assert "我推开窗" in fragments[0]
        assert "他推开窗" in fragments[1]

    _run(scenario)


def test_low_dialogue_profile_lowers_dialogue_threshold():
    """画像 dialogue_ratio < 0.3 → 纯对话阈值 0.4；画像缺失/解析失败保持 0.6。"""

    async def scenario(db):
        project = await seed_project_with(
            db, {1: DIALOGUE_HEAVY_TEXT, 2: TEXT_B + "他记住了这个标记。"}
        )
        await create_anchors_for(db, {1: DIALOGUE_HEAVY_TEXT, 2: TEXT_B + "他记住了这个标记。"})

        # 无画像：0.48 的对话占比低于 0.6，保留
        fragments = await load_style_anchor_fragments(db, project=project)
        assert any("我们明天再谈" in fragment for fragment in fragments)

        style = WritingStyle(
            id=1,
            user_id=USER_ID,
            name="占位画像",
            style_type="profile",
            preset_id=profile_preset_id(project.id),
            prompt_content="占位画像内容",
            profile_json=json.dumps({"metrics": {"dialogue_ratio": 0.2}}),
        )
        db.add(style)
        db.add(ProjectDefaultStyle(project_id=project.id, style_id=1))
        await db.commit()

        # 画像对话占比低：阈值降到 0.4，0.48 的纯对话段被跳过，叙述锚点保留
        fragments = await load_style_anchor_fragments(db, project=project)
        assert not any("我们明天再谈" in fragment for fragment in fragments)
        assert any(MARKER_B in fragment for fragment in fragments)

        style.profile_json = "not-json"
        await db.commit()

        # 画像解析失败按缺失处理：阈值回到 0.6
        fragments = await load_style_anchor_fragments(db, project=project)
        assert any("我们明天再谈" in fragment for fragment in fragments)

    _run(scenario)


def test_append_without_style_content_still_injects_anchors():
    """未选写作风格时锚点也应生效（本书级语感约束不依赖风格选择）。"""

    async def scenario(db):
        project = await seed_project(db, chapters=2)
        await create_project_style_anchors(
            db,
            project_id="project-a",
            ranges=[{"chapter": 1, "start": 0, "end": 160}],
        )
        result = await append_project_style_anchors(
            db,
            project=project,
            style_content="",
            before_chapter_number=2,
        )
        assert "【本书原文语感锚点" in result
        assert result.strip()

    _run(scenario)
