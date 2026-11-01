"""档案同步：内容审核二分降级、超时看门狗、超大单章切分与水位连续覆盖（issue #193）。

钉死契约：
(a) 多章批次命中 `ai.content_filtered` → 对半拆批重试，不丢整批；
(b) 单章命中拦截 → 正文按段落边界切两片重试，提示词标注「第 x/y 段」；
(c) 降到最小粒度仍失败 → 记 skipped，且水位停在该章之前（下一轮会被重新选中）；
(d) 未命中拦截时不增加任何 AI 调用；
(e) 水位只按「该档案提及章节序列里从起点连续成功的最长前缀」推进，
    提及章节不连续时也按该档案自己的序列算；
(f) `ai.output_truncated` 语义不变：不重试、不二分、不推进水位；
(g) 世界设定同样二分降级；
(h) 单次调用超时（asyncio 看门狗 / httpx 超时）重试 1 次；两次都超时记
    `ai.timeout` skipped 且不二分、不上抛，其余批继续，水位停在该章之前；
    非超时异常仍然上抛、不重试；
(i) 单章正文超过 `_MAX_SINGLE_CALL_CHARS` 时先按段落边界切到上限内，切分后的批数
    与 `estimate_refresh()["calls_min"]` 一致，同一章多切片必须全部成功才推进水位。

夹具与断言文本全部为中性占位（AGENTS.md 脱敏硬约束）。
"""
from __future__ import annotations

import asyncio
import json
import os
import uuid

import httpx
import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.core.errors import ApiError
from app.database import Base
from app.models.chapter import Chapter
from app.models.character import Character
from app.models.project import Project
from app.services import character_profile_sync_service as profile_sync_module
from app.services.character_profile_sync_service import (
    CharacterProfileSyncService,
    character_profile_sync_service,
)

PROJECT_ID = "proj-bisect-1"
USER_ID = "user-bisect-1"

pytestmark = pytest.mark.anyio

_CHAPTER_PARAGRAPH = "占位正文段落，角色甲在占位场景里走动，四周是占位陈设与占位器物。"


class ScriptedAIService:
    """按调用顺序出结果的桩：元素是响应 dict，或要抛出的 ApiError。

    脚本用尽后重复最后一项，方便「同一类错误反复出现」的用例。
    """

    def __init__(self, script: list):
        self.script = list(script)
        self.calls: list[str] = []

    async def call_with_json_retry(self, *, prompt: str, **kwargs):
        self.calls.append(prompt)
        item = self.script[min(len(self.calls) - 1, len(self.script) - 1)]
        if isinstance(item, BaseException):
            raise item
        return item


def _payload(name: str, **overrides) -> dict:
    return {
        "characters": [
            {
                "name": name,
                "fields": {field: overrides.get(field) for field in (
                    "personality",
                    "background",
                    "appearance",
                    "current_state",
                )},
                "organizations": [],
            }
        ]
    }


def _world_payload(**fields) -> dict:
    return {
        "world": {
            field: fields.get(field)
            for field in (
                "world_time_period",
                "world_location",
                "world_atmosphere",
                "world_rules",
            )
        }
    }


def _long_chapter(paragraphs: int = 4) -> str:
    """超过切片下限（600 字符）且带段落边界的一章正文。"""
    return "\n\n".join(
        f"占位段落{index}：{_CHAPTER_PARAGRAPH * 5}" for index in range(paragraphs)
    )


def _huge_chapter(paragraphs: int = 16, paragraph_chars: int = 2000) -> str:
    """超过单次调用上限（24000 字符）且带段落边界的一章正文。"""
    filler = "占位正文" * (paragraph_chars // 4)
    return "\n\n".join(
        f"占位段落{index}：角色甲登场。{filler}" for index in range(paragraphs)
    )


class _WatchdogAIService:
    """按调用顺序出结果的桩；float 项表示「这次调用挂起这么久」以触发看门狗。

    `asyncio.wait_for` 超时时会取消挂起调用，睡眠不会真的跑完——用例靠
    monkeypatch 看门狗常量（如 0.05s）秒级完成，绝不真等 180s。
    """

    def __init__(self, script: list):
        self.script = list(script)
        self.calls: list[str] = []

    async def call_with_json_retry(self, *, prompt: str, **kwargs):
        self.calls.append(prompt)
        item = self.script[min(len(self.calls) - 1, len(self.script) - 1)]
        if isinstance(item, (int, float)):
            await asyncio.sleep(item)
            raise AssertionError("看门狗应在挂起调用返回前取消它")
        if isinstance(item, BaseException):
            raise item
        return item


@pytest.fixture
async def db_session():
    db_path = f"/tmp/test_character_profile_sync_bisect_{uuid.uuid4().hex}.db"
    engine = create_async_engine(f"sqlite+aiosqlite:///{db_path}")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    Session = async_sessionmaker(bind=engine, expire_on_commit=False)
    async with Session() as session:
        yield session
    await engine.dispose()
    if os.path.exists(db_path):
        os.remove(db_path)


@pytest.fixture(autouse=True)
def _clear_reentry_guard():
    CharacterProfileSyncService._in_progress.clear()
    CharacterProfileSyncService._world_in_progress.clear()
    yield
    CharacterProfileSyncService._in_progress.clear()
    CharacterProfileSyncService._world_in_progress.clear()


async def _seed(db, *, chapters, markers=None):
    db.add(Project(id=PROJECT_ID, user_id=USER_ID, title="占位项目", genre="通用"))
    for name, marker in (markers or {}).items():
        db.add(
            Character(
                project_id=PROJECT_ID,
                name=name,
                personality="占位旧性格",
                ai_profile_snapshot=json.dumps({"personality": "占位旧性格"}, ensure_ascii=False),
                profile_synced_through_chapter=marker,
            )
        )
    for number, content in chapters:
        db.add(
            Chapter(
                project_id=PROJECT_ID,
                chapter_number=number,
                title=f"占位章节标题{number}",
                content=content,
                word_count=len(content),
            )
        )
    await db.commit()


async def _character(db, name: str) -> Character:
    character = (
        await db.execute(select(Character).where(Character.name == name))
    ).scalar_one()
    await db.refresh(character)
    return character


async def _project(db) -> Project:
    project = (await db.execute(select(Project).where(Project.id == PROJECT_ID))).scalar_one()
    await db.refresh(project)
    return project


async def test_two_chapter_batch_filtered_is_split_into_two_calls(db_session):
    await _seed(
        db_session,
        chapters=[(1, "第1章占位正文。角色甲登场。"), (2, "第2章占位正文。角色甲再次登场。")],
        markers={"角色甲": 0},
    )
    ai = ScriptedAIService(
        [
            ApiError(code="ai.content_filtered"),
            _payload("角色甲", personality="占位新性格"),
            _payload("角色甲", personality="占位新性格"),
        ]
    )

    stats = await character_profile_sync_service.sync(
        db=db_session, user_id=USER_ID, project_id=PROJECT_ID, through_chapter=2, ai_service=ai
    )

    character = await _character(db_session, "角色甲")
    assert len(ai.calls) == 3, "两章批次被拦后应各自成批，共 3 次调用"
    assert "【第1章" in ai.calls[0] and "【第2章" in ai.calls[0]
    assert character.personality == "占位新性格"
    assert character.profile_synced_through_chapter == 2
    assert stats["fields_updated"] == 1
    assert stats["skipped"] == []


async def test_single_chapter_filtered_splits_content_into_two_parts(db_session):
    await _seed(db_session, chapters=[(3, _long_chapter())], markers={"角色甲": 0})
    ai = ScriptedAIService(
        [
            ApiError(code="ai.content_filtered"),
            _payload("角色甲", personality="占位新性格"),
            _payload("角色甲", personality="占位新性格"),
        ]
    )

    stats = await character_profile_sync_service.sync(
        db=db_session, user_id=USER_ID, project_id=PROJECT_ID, through_chapter=3, ai_service=ai
    )

    character = await _character(db_session, "角色甲")
    assert len(ai.calls) == 3, "单章被拦后应按正文切两片：1 次被拦 + 2 次切片"
    assert "（第 1/2 段）" in ai.calls[1]
    assert "（第 2/2 段）" in ai.calls[2]
    assert character.personality == "占位新性格"
    assert character.profile_synced_through_chapter == 3, "两片都成功则该章算 covered"
    assert stats["skipped"] == []


async def test_minimum_granularity_failure_stops_watermark_before_chapter(db_session):
    await _seed(
        db_session,
        chapters=[(1, "第1章占位正文。角色甲登场。"), (2, "第2章占位正文。角色甲再次登场。")],
        markers={"角色甲": 0},
    )
    ai = ScriptedAIService(
        [
            ApiError(code="ai.content_filtered"),
            _payload("角色甲", personality="占位新性格"),
            ApiError(code="ai.content_filtered"),
        ]
    )

    stats = await character_profile_sync_service.sync(
        db=db_session, user_id=USER_ID, project_id=PROJECT_ID, through_chapter=2, ai_service=ai
    )

    character = await _character(db_session, "角色甲")
    assert len(ai.calls) == 3
    assert character.profile_synced_through_chapter == 1, "水位不得越过被拦章节"
    assert len(stats["skipped"]) == 1
    assert stats["skipped"][0]["chapter_numbers"] == [2]
    assert stats["skipped"][0]["code"] == "ai.content_filtered"

    # 再来一轮：第 2 章仍应被选中（start = 2），说明被拦章节没被水位漏掉
    retry = ScriptedAIService([ApiError(code="ai.content_filtered")])
    retry_stats = await character_profile_sync_service.sync(
        db=db_session, user_id=USER_ID, project_id=PROJECT_ID, through_chapter=2, ai_service=retry
    )
    assert retry_stats["characters_considered"] == 1
    assert len(retry.calls) == 1
    assert "【第2章" in retry.calls[0]
    assert (await _character(db_session, "角色甲")).profile_synced_through_chapter == 1


async def test_no_filter_does_not_add_calls(db_session):
    await _seed(db_session, chapters=[(1, "第1章占位正文。角色甲登场。")], markers={"角色甲": 0})
    ai = ScriptedAIService([_payload("角色甲", personality="占位新性格")])

    stats = await character_profile_sync_service.sync(
        db=db_session, user_id=USER_ID, project_id=PROJECT_ID, through_chapter=1, ai_service=ai
    )

    character = await _character(db_session, "角色甲")
    assert len(ai.calls) == 1, "未被拦截时不得增加调用"
    assert character.personality == "占位新性格"
    assert character.profile_synced_through_chapter == 1
    assert stats["skipped"] == []


async def test_non_contiguous_mentions_stop_at_failed_chapter(db_session):
    await _seed(
        db_session,
        chapters=[(1, "第1章占位正文。角色甲登场。"), (5, "第5章占位正文。角色甲再次登场。")],
        markers={"角色甲": 0},
    )
    ai = ScriptedAIService(
        [
            ApiError(code="ai.content_filtered"),
            _payload("角色甲", personality="占位新性格"),
            ApiError(code="ai.content_filtered"),
        ]
    )

    stats = await character_profile_sync_service.sync(
        db=db_session, user_id=USER_ID, project_id=PROJECT_ID, through_chapter=5, ai_service=ai
    )

    character = await _character(db_session, "角色甲")
    assert len(ai.calls) == 3
    assert character.profile_synced_through_chapter == 1, "按该角色自己的提及序列算连续前缀"
    assert stats["skipped"][0]["chapter_numbers"] == [5]


async def test_non_contiguous_mentions_all_success_advance_to_last(db_session):
    await _seed(
        db_session,
        chapters=[(1, "第1章占位正文。角色甲登场。"), (5, "第5章占位正文。角色甲再次登场。")],
        markers={"角色甲": 0},
    )
    ai = ScriptedAIService([_payload("角色甲", personality="占位新性格")])

    await character_profile_sync_service.sync(
        db=db_session, user_id=USER_ID, project_id=PROJECT_ID, through_chapter=5, ai_service=ai
    )

    character = await _character(db_session, "角色甲")
    assert len(ai.calls) == 1
    assert character.profile_synced_through_chapter == 5


async def test_output_truncated_is_not_bisected(db_session):
    await _seed(
        db_session,
        chapters=[(1, "第1章占位正文。角色甲登场。"), (2, "第2章占位正文。角色甲再次登场。")],
        markers={"角色甲": 0},
    )
    ai = ScriptedAIService([ApiError(code="ai.output_truncated")])

    stats = await character_profile_sync_service.sync(
        db=db_session, user_id=USER_ID, project_id=PROJECT_ID, through_chapter=2, ai_service=ai
    )

    character = await _character(db_session, "角色甲")
    assert len(ai.calls) == 1, "output_truncated 不重试、不二分"
    assert character.profile_synced_through_chapter == 0
    assert len(stats["skipped"]) == 1
    assert stats["skipped"][0]["code"] == "ai.output_truncated"
    assert stats["skipped"][0]["chapter_numbers"] == [1, 2]


async def test_world_batch_filtered_is_bisected(db_session):
    await _seed(
        db_session,
        chapters=[(1, "第1章占位正文。占位城邦登场。"), (2, "第2章占位正文。占位城邦再现。")],
    )
    ai = ScriptedAIService(
        [
            ApiError(code="ai.content_filtered"),
            _world_payload(
                world_time_period="占位纪元第七年",
                world_location="占位大陆东侧的占位城邦",
                world_atmosphere="占领与抵抗并存的占位气氛",
                world_rules="占位晶石可换取通行权",
            ),
            _world_payload(
                world_time_period="占位纪元第七年",
                world_location="占位大陆东侧的占位城邦",
                world_atmosphere="占领与抵抗并存的占位气氛",
                world_rules="占位晶石可换取通行权",
            ),
        ]
    )

    stats = await character_profile_sync_service.sync_world(
        db=db_session, user_id=USER_ID, project_id=PROJECT_ID, through_chapter=2, ai_service=ai
    )

    project = await _project(db_session)
    assert len(ai.calls) == 3, "世界设定同样拆批重试"
    assert project.world_location == "占位大陆东侧的占位城邦"
    assert project.world_profile_synced_through_chapter == 2, "两章都成功 → 水位推进到 2"
    assert stats["fields_updated"] >= 4
    assert stats["skipped"] == []


# --- ⑧ 超时看门狗：重试一次后降级为 skipped ---------------------------------


async def test_timeout_twice_is_skipped_after_one_retry(db_session, monkeypatch):
    """某批两次都超时 → 重试 1 次后记 skipped，其余批继续，水位停在该章之前。"""
    monkeypatch.setattr(profile_sync_module, "_PROFILE_CALL_TIMEOUT_SECONDS", 0.05)
    monkeypatch.setattr(profile_sync_module, "_PROMPT_CHAR_BUDGET", 10)  # 每章单独成批
    await _seed(
        db_session,
        chapters=[
            (1, "第1章占位正文。角色甲登场。"),
            (2, "第2章占位正文。角色甲再次登场。"),
        ],
        markers={"角色甲": 0},
    )
    ai = _WatchdogAIService(
        [
            0.5,  # 第 1 章首次尝试：看门狗超时
            0.5,  # 第 1 章重试：再次超时 → 记 skipped（不二分、不上抛）
            _payload("角色甲", personality="占位新性格"),
        ]
    )

    stats = await character_profile_sync_service.sync(
        db=db_session, user_id=USER_ID, project_id=PROJECT_ID, through_chapter=2, ai_service=ai
    )

    character = await _character(db_session, "角色甲")
    assert len(ai.calls) == 3, "超时批重试 1 次后记 skipped，另一批照常调用"
    assert len(stats["skipped"]) == 1
    assert stats["skipped"][0]["code"] == "ai.timeout"
    assert stats["skipped"][0]["chapter_numbers"] == [1]
    assert stats["skipped"][0]["characters"] == ["角色甲"]
    assert character.personality == "占位新性格", "其余批继续写回"
    assert character.profile_synced_through_chapter == 0, "水位停在超时章节之前"


async def test_httpx_timeout_then_retry_succeeds(db_session):
    """首次尝试抛 httpx 超时、重试成功 → 不 skip，调用次数=2，正常写回。"""
    await _seed(db_session, chapters=[(1, "第1章占位正文。角色甲登场。")], markers={"角色甲": 0})
    ai = _WatchdogAIService(
        [
            httpx.ReadTimeout("占位上游读超时"),
            _payload("角色甲", personality="占位新性格"),
        ]
    )

    stats = await character_profile_sync_service.sync(
        db=db_session, user_id=USER_ID, project_id=PROJECT_ID, through_chapter=1, ai_service=ai
    )

    character = await _character(db_session, "角色甲")
    assert len(ai.calls) == 2, "首发超时后重试一次"
    assert character.personality == "占位新性格"
    assert character.profile_synced_through_chapter == 1
    assert stats["skipped"] == []


async def test_non_timeout_exception_is_reraised_without_retry(db_session):
    """非超时类异常照旧上抛：不重试、不吞、不记 skipped。"""
    await _seed(db_session, chapters=[(1, "第1章占位正文。角色甲登场。")], markers={"角色甲": 0})
    ai = ScriptedAIService([RuntimeError("占位上游客户端崩溃")])

    with pytest.raises(RuntimeError):
        await character_profile_sync_service.sync(
            db=db_session,
            user_id=USER_ID,
            project_id=PROJECT_ID,
            through_chapter=1,
            ai_service=ai,
        )

    assert len(ai.calls) == 1, "非超时异常不得重试"


# --- ⑨ 超大单章切分 ---------------------------------------------------------


async def test_oversized_chapter_is_split_to_limit_and_counted_in_estimate(db_session):
    """单章 > 24000 字符 → 按段落边界切到上限内；调用次数 == estimate.calls_min。"""
    content = _huge_chapter()
    assert len(content) > profile_sync_module._MAX_SINGLE_CALL_CHARS
    await _seed(db_session, chapters=[(7, content)], markers={"角色甲": 0})
    ai = ScriptedAIService([_payload("角色甲", personality="占位新性格")])

    estimate = await character_profile_sync_service.estimate_refresh(
        db=db_session, project_id=PROJECT_ID, mode="incremental"
    )
    stats = await character_profile_sync_service.sync(
        db=db_session, user_id=USER_ID, project_id=PROJECT_ID, through_chapter=7, ai_service=ai
    )

    character = await _character(db_session, "角色甲")
    assert estimate["calls_min"] == 2, "32200+ 字符对半切成 2 片 → 2 批"
    assert len(ai.calls) == estimate["calls_min"], "预估与实际调用次数必须一致"
    assert "（第 1/2 段）" in ai.calls[0]
    assert "（第 2/2 段）" in ai.calls[1]
    assert character.personality == "占位新性格"
    assert character.profile_synced_through_chapter == 7, "全部切片成功 → 水位正常推进"
    assert stats["skipped"] == []


async def test_oversized_chapter_partial_failure_stops_watermark(db_session):
    """同一章多切片：任一片失败则 covered=False，水位不得越过该章。"""
    await _seed(db_session, chapters=[(7, _huge_chapter())], markers={"角色甲": 0})
    ai = ScriptedAIService(
        [
            _payload("角色甲", personality="占位新性格"),
            ApiError(code="ai.output_truncated"),
        ]
    )

    stats = await character_profile_sync_service.sync(
        db=db_session, user_id=USER_ID, project_id=PROJECT_ID, through_chapter=7, ai_service=ai
    )

    character = await _character(db_session, "角色甲")
    assert len(ai.calls) == 2
    assert character.profile_synced_through_chapter == 0, "多切片必须全部成功才推进水位"
    assert stats["skipped"][0]["chapter_numbers"] == [7]

    # 下一轮 start=1 仍会重新覆盖该章（水位没被误推进）
    retry = ScriptedAIService([_payload("角色甲", personality="占位新性格")])
    await character_profile_sync_service.sync(
        db=db_session, user_id=USER_ID, project_id=PROJECT_ID, through_chapter=7, ai_service=retry
    )
    assert len(retry.calls) == 2, "整章重新切两片重跑"
    assert (await _character(db_session, "角色甲")).profile_synced_through_chapter == 7
