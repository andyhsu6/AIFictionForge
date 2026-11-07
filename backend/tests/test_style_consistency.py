"""issue #254 P2：文笔画像一致性自检（本地统计，不调用模型）。

覆盖服务纯函数（指标口径、告警阈值、边界安全返回）与 API 契约（无画像
available=false、越权拒绝、文本上限）。用临时 SQLite + ``asyncio.run``，
与本目录既有测试一致。
"""
import asyncio
import json
import os
import uuid

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from starlette.requests import Request

from app.api.style_consistency import (
    MAX_TEXT_CHARS,
    StyleConsistencyRequest,
    check_style_consistency,
)
from app.core.errors import ApiError
from app.database import Base
from app.models.project import Project
from app.models.project_default_style import ProjectDefaultStyle
from app.models.writing_style import WritingStyle
from app.services.style_consistency import (
    METRIC_KEYS,
    check_text_against_profile,
    compare_with_profile,
    compute_text_metrics,
)
from app.services.style_profile import profile_preset_id

USER_ID = "user-1"

TEXT = "他走了。她笑了。\n\n「你来吗？」他问。"


def make_request(user_id: str = USER_ID) -> Request:
    req = Request({"type": "http", "method": "POST", "path": "/", "headers": []})
    req.state.user_id = user_id
    return req


def _run(scenario) -> None:
    """为每个测试建立独立临时 SQLite，跑异步场景后清理。"""

    async def _main() -> None:
        db_path = f"/tmp/test_style_consistency_{uuid.uuid4().hex}.db"
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


# ---------------------------------------------------------------------------
# 服务纯函数
# ---------------------------------------------------------------------------

def test_compute_text_metrics_known_text():
    metrics = compute_text_metrics(TEXT)

    assert set(metrics) == set(METRIC_KEYS)
    # 四句：4、4、6、3 字（去空白，含句末标点）→ 平均 4.25、中位 4.0
    assert metrics["avg_sentence_chars"] == pytest.approx(4.2)
    assert metrics["median_sentence_chars"] == pytest.approx(4.0)
    # 四句都 ≤ 15 字
    assert metrics["short_sentence_ratio"] == pytest.approx(1.0)
    # 对话「你来吗？」6 字 / 全文 17 字
    assert metrics["dialogue_ratio"] == pytest.approx(0.35)
    assert metrics["paragraph_chars_p50"] > 0


def test_identical_metrics_produce_no_alert():
    metrics = compute_text_metrics(TEXT)
    result = check_text_against_profile(TEXT, {"metrics": metrics})

    assert result["alerts"] == []
    assert result["ok"] is True
    assert result["score"] == pytest.approx(1.0)
    assert result["metrics"] == metrics


def test_doubled_sentence_length_triggers_warning():
    metrics = compute_text_metrics(TEXT)
    profile = {
        "metrics": {
            **metrics,
            "avg_sentence_chars": metrics["avg_sentence_chars"] * 2,
        }
    }

    result = check_text_against_profile(TEXT, profile)

    assert result["ok"] is False
    assert len(result["alerts"]) == 1
    alert = result["alerts"][0]
    assert alert["metric"] == "avg_sentence_chars"
    assert alert["level"] == "warning"
    assert alert["actual"] == metrics["avg_sentence_chars"]
    assert alert["expected"] == pytest.approx(metrics["avg_sentence_chars"] * 2)
    assert alert["deviation"] == pytest.approx(0.5, abs=1e-3)
    assert result["score"] < 1.0


def test_compare_respects_tolerance():
    metrics = {"avg_sentence_chars": 10.0}
    profile = {"metrics": {"avg_sentence_chars": 12.0}}

    assert compare_with_profile(metrics, profile, tolerance=0.25) == []
    assert compare_with_profile(metrics, profile, tolerance=0.1) != []


def test_empty_inputs_are_safe():
    metrics = compute_text_metrics("")
    assert metrics == {
        "avg_sentence_chars": 0.0,
        "median_sentence_chars": 0.0,
        "short_sentence_ratio": 0.0,
        "dialogue_ratio": 0.0,
        "paragraph_chars_p50": 0,
    }

    empty_text = check_text_against_profile("", {"metrics": {"avg_sentence_chars": 16}})
    assert empty_text["ok"] is True
    assert empty_text["alerts"] == []
    assert empty_text["score"] == pytest.approx(1.0)

    empty_profile = check_text_against_profile(TEXT, {})
    assert empty_profile["ok"] is True
    assert empty_profile["alerts"] == []


# ---------------------------------------------------------------------------
# API
# ---------------------------------------------------------------------------

async def _seed_project(db, *, project_id: str, user_id: str = USER_ID):
    project = Project(id=project_id, user_id=user_id, title="占位项目", current_words=0)
    db.add(project)
    await db.commit()
    return project


async def _attach_profile(db, project_id: str, metrics: dict, *, user_id: str = USER_ID):
    style = WritingStyle(
        user_id=user_id,
        name="文笔画像",
        style_type="profile",
        preset_id=profile_preset_id(project_id),
        prompt_content="占位画像",
        profile_json=json.dumps({"version": 1, "metrics": metrics}),
        order_index=0,
    )
    db.add(style)
    await db.flush()
    db.add(ProjectDefaultStyle(project_id=project_id, style_id=style.id))
    await db.commit()
    return style


def test_api_without_profile_returns_unavailable():
    async def scenario(db):
        await _seed_project(db, project_id="project-a")

        result = await check_style_consistency(
            StyleConsistencyRequest(project_id="project-a", text=TEXT),
            make_request(),
            db,
        )
        assert result == {"available": False}

    _run(scenario)


def test_api_with_profile_returns_metrics_and_alerts():
    async def scenario(db):
        await _seed_project(db, project_id="project-a")
        metrics = compute_text_metrics(TEXT)
        await _attach_profile(
            db,
            "project-a",
            {**metrics, "avg_sentence_chars": metrics["avg_sentence_chars"] * 2},
        )

        result = await check_style_consistency(
            StyleConsistencyRequest(project_id="project-a", text=TEXT),
            make_request(),
            db,
        )

        assert result["available"] is True
        assert set(result["metrics"]) == set(METRIC_KEYS)
        assert result["ok"] is False
        assert [alert["metric"] for alert in result["alerts"]] == ["avg_sentence_chars"]
        assert 0.0 <= result["score"] <= 1.0

    _run(scenario)


def test_api_rejects_other_user_project():
    async def scenario(db):
        await _seed_project(db, project_id="project-b", user_id="user-2")

        with pytest.raises(ApiError) as exc:
            await check_style_consistency(
                StyleConsistencyRequest(project_id="project-b", text=TEXT),
                make_request(),
                db,
            )
        assert exc.value.code == "not_found.project_or_forbidden"

    _run(scenario)


def test_api_rejects_oversized_text():
    async def scenario(db):
        await _seed_project(db, project_id="project-a")

        with pytest.raises(ApiError) as exc:
            await check_style_consistency(
                StyleConsistencyRequest(
                    project_id="project-a", text="字" * (MAX_TEXT_CHARS + 1)
                ),
                make_request(),
                db,
            )
        assert exc.value.status == 422

    _run(scenario)


def test_compare_ignores_small_absolute_deviation():
    """expected 很小时，小绝对差不生成噪声告警。"""
    alerts = compare_with_profile(
        {"short_sentence_ratio": 0.08},
        {"metrics": {"short_sentence_ratio": 0.04}},
    )
    assert alerts == []


def test_compare_flags_large_absolute_deviation():
    alerts = compare_with_profile(
        {"short_sentence_ratio": 0.60},
        {"metrics": {"short_sentence_ratio": 0.10}},
    )
    assert len(alerts) == 1
    assert alerts[0]["metric"] == "short_sentence_ratio"


def test_check_reports_compared_metrics():
    result = check_text_against_profile(
        "短句。另一句。", {"metrics": {"avg_sentence_chars": 10}}
    )
    assert result["compared_metrics"] == 1
    assert check_text_against_profile("", {"metrics": {}})["compared_metrics"] == 0
