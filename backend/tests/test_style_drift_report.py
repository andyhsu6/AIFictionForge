"""计划 §15 风格漂移报表：本地统计口径与 CLI 契约。

覆盖：
1. 两样本 KS：分布不相交 → 1.0，相同分布 → 0，方向由 ``mean_shift`` 表达；
2. 画像 + 章节 → 均值差/相对偏差，超阈值（>25% 或 KS>0.2）产生 warning；
3. 无画像项目进 ``skipped``，``--last-n`` 只取最近 N 章；
4. 输出只含项目 id 与数值，不含章节标题/正文；
5. CLI ``--help`` 退出码 0，非法参数退出码 1。

用临时 SQLite + ``asyncio.run``（与本目录既有测试一致）；不调用真实 AI/embedding。
"""
import asyncio
import json
import os
import uuid

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.database import Base
from app.models.chapter import Chapter
from app.models.project import Project
from app.models.writing_style import WritingStyle
from app.services.style_profile import profile_preset_id
from scripts.style_drift_report import (
    build_project_report,
    build_report,
    main,
    render_text,
    sentence_lengths,
    two_sample_ks,
)

USER_ID = "user-1"

# 每句 4 字（含句末标点）
SHORT_TEXT = "他走了。她笑了。天黑了。"
# 每句 21 字：与 SHORT_TEXT 的句长分布完全不相交
LONG_SENTENCE = "他慢慢地走过那条很长很长的街道然后停下来。"
LONG_TEXT = LONG_SENTENCE * 3

SHORT_METRICS = {
    "avg_sentence_chars": 4.0,
    "dialogue_ratio": 0.0,
    "short_sentence_ratio": 1.0,
    "paragraph_chars_p50": 12,
}


def _run(scenario) -> None:
    """每个测试独立临时 SQLite，跑完删除。"""

    async def _main() -> None:
        db_path = f"/tmp/test_style_drift_report_{uuid.uuid4().hex}.db"
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


async def _seed_project(db, project_id: str) -> None:
    db.add(
        Project(id=project_id, user_id=USER_ID, title="占位项目", current_words=0)
    )
    await db.commit()


async def _attach_profile(db, project_id: str, metrics: dict) -> None:
    db.add(
        WritingStyle(
            user_id=USER_ID,
            name="文笔画像",
            style_type="profile",
            preset_id=profile_preset_id(project_id),
            prompt_content="占位画像",
            profile_json=json.dumps({"version": 1, "metrics": metrics}),
            order_index=0,
        )
    )
    await db.commit()


async def _seed_chapters(db, project_id: str, texts: list[str], *, title_prefix: str) -> None:
    db.add_all(
        [
            Chapter(
                id=f"{project_id}-chapter-{index}",
                project_id=project_id,
                chapter_number=index,
                title=f"{title_prefix}{index}",
                content=text,
            )
            for index, text in enumerate(texts, start=1)
        ]
    )
    await db.commit()


# ---------------------------------------------------------------------------
# 1. 句长切分与 KS
# ---------------------------------------------------------------------------

def test_sentence_lengths_matches_metric_split():
    assert sentence_lengths(SHORT_TEXT) == [4, 4, 4]
    assert sentence_lengths(LONG_TEXT) == [21, 21, 21]
    assert sentence_lengths("") == []


def test_two_sample_ks_extremes_and_overlap():
    short = [4, 4, 4]
    long = [21, 21, 21]

    assert two_sample_ks(short, long) == pytest.approx(1.0)
    assert two_sample_ks(long, short) == pytest.approx(1.0)
    assert two_sample_ks(short, short) == pytest.approx(0.0)
    # 部分重叠：0 < KS < 1
    assert 0.0 < two_sample_ks([4, 8, 12], [8, 12, 21]) < 1.0
    # 空样本不制造告警
    assert two_sample_ks([], long) == 0.0


def test_project_report_ks_direction():
    ascending = build_project_report(
        "project-a", [SHORT_TEXT, LONG_TEXT], SHORT_METRICS
    )
    descending = build_project_report(
        "project-a", [LONG_TEXT, SHORT_TEXT], SHORT_METRICS
    )
    steady = build_project_report("project-a", [SHORT_TEXT, SHORT_TEXT], SHORT_METRICS)

    assert ascending["ks"]["value"] == pytest.approx(1.0)
    assert ascending["ks"]["mean_shift"] > 0  # 后半更长
    assert descending["ks"]["value"] == pytest.approx(1.0)
    assert descending["ks"]["mean_shift"] < 0  # 后半更短
    assert steady["ks"]["value"] == pytest.approx(0.0)
    assert steady["ks"]["mean_shift"] == pytest.approx(0.0)


def test_single_chapter_has_no_ks():
    report = build_project_report("project-a", [SHORT_TEXT], SHORT_METRICS)
    assert report["ks"] is None


# ---------------------------------------------------------------------------
# 2. 均值差 / 相对偏差 / warning
# ---------------------------------------------------------------------------

def test_report_computes_deviations_and_warns():
    async def scenario(db):
        await _seed_project(db, "project-a")
        await _attach_profile(db, "project-a", SHORT_METRICS)
        await _seed_chapters(
            db, "project-a", [SHORT_TEXT, LONG_TEXT], title_prefix="占位章节"
        )

        report = await build_report(db, project_id="project-a")

        project = report["projects"][0]
        block = project["metrics"]["avg_sentence_chars"]
        # 章级均值：(4 + 21) / 2 = 12.5
        assert block["actual"] == pytest.approx(12.5)
        assert block["expected"] == pytest.approx(4.0)
        assert block["diff"] == pytest.approx(8.5)
        assert block["abs_diff"] == pytest.approx(8.5)
        assert block["rel_dev"] == pytest.approx(2.125)
        # 超 25% 相对偏差 → warning
        assert any(
            warning["kind"] == "metric"
            and warning["metric"] == "avg_sentence_chars"
            for warning in project["warnings"]
        )
        # KS 1.0 > 0.2 → 同时有 ks warning
        assert any(warning["kind"] == "ks" for warning in project["warnings"])

    _run(scenario)


def test_within_threshold_produces_no_warning():
    async def scenario(db):
        await _seed_project(db, "project-a")
        await _attach_profile(db, "project-a", SHORT_METRICS)
        await _seed_chapters(
            db, "project-a", [SHORT_TEXT, SHORT_TEXT], title_prefix="占位章节"
        )

        project = (await build_report(db, project_id="project-a"))["projects"][0]

        assert project["metrics"]["avg_sentence_chars"]["rel_dev"] == 0.0
        assert project["warnings"] == []

    _run(scenario)


# ---------------------------------------------------------------------------
# 3. 无画像跳过 / --last-n
# ---------------------------------------------------------------------------

def test_report_skips_projects_without_profile_and_last_n_applies():
    async def scenario(db):
        await _seed_project(db, "project-a")
        await _seed_project(db, "project-b")  # 无画像
        await _attach_profile(db, "project-a", SHORT_METRICS)
        await _seed_chapters(
            db,
            "project-a",
            [SHORT_TEXT, SHORT_TEXT, LONG_TEXT, LONG_TEXT],
            title_prefix="占位章节",
        )

        full = await build_report(db)
        assert [project["project_id"] for project in full["projects"]] == ["project-a"]
        assert {"project_id": "project-b", "reason": "no_profile"} in full["skipped"]

        recent = (
            await build_report(db, project_id="project-a", last_n=2)
        )["projects"][0]
        assert recent["chapter_count"] == 2
        # 最近两章都是长句；全量均值被前两章拉低
        assert recent["metrics"]["avg_sentence_chars"]["actual"] == pytest.approx(21.0)
        assert (
            recent["metrics"]["avg_sentence_chars"]["actual"]
            > full["projects"][0]["metrics"]["avg_sentence_chars"]["actual"]
        )

    _run(scenario)


def test_unknown_project_id_yields_empty_report():
    async def scenario(db):
        await _seed_project(db, "project-a")

        report = await build_report(db, project_id="does-not-exist")

        assert report["projects"] == []
        assert report["skipped"] == []

    _run(scenario)


# ---------------------------------------------------------------------------
# 4. 输出脱敏
# ---------------------------------------------------------------------------

def test_output_contains_no_titles_or_body_text():
    title = "占位标题ABC"
    body_mark = "占位正文标记DEF"

    async def scenario(db):
        await _seed_project(db, "project-a")
        await _attach_profile(db, "project-a", SHORT_METRICS)
        await _seed_chapters(
            db,
            "project-a",
            [f"{body_mark}他走了。{body_mark}她笑了。", LONG_TEXT],
            title_prefix=title,
        )

        report = await build_report(db, project_id="project-a")
        text_output = render_text(report)
        json_output = json.dumps(report, ensure_ascii=False)

        for output in (text_output, json_output):
            assert title not in output
            assert body_mark not in output
        # 报表只有 id、章数与数值
        assert "project-a" in text_output
        assert "avg_sentence_chars" in text_output

    _run(scenario)


# ---------------------------------------------------------------------------
# 5. CLI
# ---------------------------------------------------------------------------

def test_cli_help_exits_zero(capsys):
    with pytest.raises(SystemExit) as exc:
        main(["--help"])

    assert exc.value.code == 0
    assert "--project-id" in capsys.readouterr().out


def test_cli_rejects_invalid_last_n(capsys):
    with pytest.raises(SystemExit) as exc:
        main(["--last-n", "0"])

    # 约定：参数错误退出码 1（argparse 默认 2）
    assert exc.value.code == 1
