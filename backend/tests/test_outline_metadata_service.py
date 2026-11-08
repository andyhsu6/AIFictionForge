"""大纲元数据补全服务（outline_metadata_service）单元测试。

覆盖：
- missing_metadata_fields：空结构、占位值、完整结构、characters 空 name
- normalize_structure_metadata：type 非法归 character、去重、占位值清理
- fill_missing_metadata：只补缺失键、已有值不被覆盖、失败计数、写回 JSON 可解析
"""
import asyncio
import json
from types import SimpleNamespace

from app.services.outline_metadata_service import (
    METADATA_FIELDS,
    fill_missing_metadata,
    missing_metadata_fields,
    normalize_structure_metadata,
)


class _Result:
    """最小化 SQLAlchemy Result 替身。"""

    def __init__(self, scalar=None, items=None):
        self._scalar = scalar
        self._items = items or []

    def scalar_one_or_none(self):
        return self._scalar

    def scalars(self):
        items = list(self._items)
        return SimpleNamespace(all=lambda: items)


class _FakeAIService:
    """按顺序吐出预设响应的假 AI 服务。"""

    def __init__(self, responses):
        self._responses = list(responses)
        self.prompts = []

    async def call_with_json_retry(self, **kwargs):
        self.prompts.append(kwargs.get("prompt", ""))
        response = self._responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response


def _structure_json(structure):
    return None if structure is None else json.dumps(structure, ensure_ascii=False)


def _outline(order_index, structure=None, title=None):
    return SimpleNamespace(
        id=f"outline-{order_index}",
        order_index=order_index,
        title=title or f"第{order_index}章大纲",
        content=f"第{order_index}章概要",
        structure=_structure_json(structure),
    )


# ---------------------------------------------------------------- missing_metadata_fields


def test_missing_all_fields_for_empty_structure():
    assert missing_metadata_fields({}) == list(METADATA_FIELDS)


def test_missing_fields_for_placeholder_values():
    structure = {
        "scenes": ["", "未设定"],
        "characters": [{"name": "", "type": "character"}],
        "key_points": ["未知"],
        "emotion": "待定",
        "goal": "无",
    }
    assert missing_metadata_fields(structure) == list(METADATA_FIELDS)


def test_complete_structure_has_no_missing_fields():
    structure = {
        "scenes": ["城门", "地牢"],
        "characters": [{"name": "甲", "type": "character"}],
        "key_points": ["甲夺取令牌"],
        "emotion": "戒备→信任",
        "goal": "主角获得线索",
    }
    assert missing_metadata_fields(structure) == []


def test_characters_with_empty_name_counts_as_missing():
    structure = {
        "scenes": ["城门"],
        "characters": [{"name": "   ", "type": "character"}, {"type": "organization"}],
        "key_points": ["甲登场"],
        "emotion": "紧张",
        "goal": "获得线索",
    }
    assert missing_metadata_fields(structure) == ["characters"]


def test_missing_fields_keep_fixed_order():
    structure = {"scenes": ["城门"], "characters": [{"name": "甲", "type": "character"}]}
    assert missing_metadata_fields(structure) == ["key_points", "emotion", "goal"]


def test_key_events_only_counts_as_having_key_points():
    """key_points / key_events 是同一内容的两套键名：只写 key_events 不算缺失。"""
    structure = {
        "scenes": ["城门"],
        "characters": [{"name": "甲", "type": "character"}],
        "key_events": ["甲登场"],
        "emotion": "紧张",
        "goal": "获得线索",
    }
    assert missing_metadata_fields(structure) == []


def test_key_points_placeholder_with_key_events_is_not_missing():
    structure = {
        "scenes": ["城门"],
        "characters": [{"name": "甲", "type": "character"}],
        "key_points": ["未设定"],
        "key_events": ["甲登场"],
        "emotion": "紧张",
        "goal": "获得线索",
    }
    assert missing_metadata_fields(structure) == []


def test_key_points_missing_when_both_keys_empty():
    structure = {
        "scenes": ["城门"],
        "characters": [{"name": "甲", "type": "character"}],
        "key_events": ["未知"],
        "emotion": "紧张",
        "goal": "获得线索",
    }
    assert missing_metadata_fields(structure) == ["key_points"]


# ------------------------------------------------------------- normalize_structure_metadata


def test_normalize_invalid_type_falls_back_to_character():
    normalized = normalize_structure_metadata(
        {"characters": [{"name": "张三", "type": "person"}, {"name": "青云宗", "type": "organization"}]}
    )
    assert normalized["characters"] == [
        {"name": "张三", "type": "character"},
        {"name": "青云宗", "type": "organization"},
    ]


def test_normalize_dedupes_and_cleans_placeholders():
    normalized = normalize_structure_metadata({
        "scenes": ["城门", "城门 ", "未设定", ""],
        "characters": [
            {"name": "张三", "type": "character"},
            {"name": "张三", "type": "character"},
            {"name": "", "type": "character"},
        ],
        "key_points": ["甲登场", "甲登场", "未知"],
        "emotion": "未知",
        "goal": "   ",
    })
    assert normalized["scenes"] == ["城门"]
    assert normalized["characters"] == [{"name": "张三", "type": "character"}]
    assert normalized["key_points"] == ["甲登场"]
    assert normalized["emotion"] == ""
    assert normalized["goal"] == ""


def test_normalize_accepts_key_events_fallback_and_non_dict_input():
    normalized = normalize_structure_metadata({"key_events": ["事件一"], "emotional_tone": "紧张"})
    assert normalized["key_points"] == ["事件一"]
    assert normalized["emotion"] == "紧张"
    # 非 dict 输入不抛错，全部按缺失处理
    assert normalize_structure_metadata(None)["scenes"] == []


# ------------------------------------------------------------------ fill_missing_metadata


def _build_db(project, outlines):
    """构造 4 次 execute 的替身：项目 → 大纲 → 用户偏好 → 角色。"""
    from unittest.mock import AsyncMock

    db = AsyncMock()
    db.execute = AsyncMock(side_effect=[
        _Result(scalar=project),
        _Result(items=outlines),
        _Result(scalar=None),  # resolve_user_generation_language 读 Settings，无偏好
        _Result(items=[]),
    ])
    db.commit = AsyncMock()
    db.rollback = AsyncMock()
    return db


def test_fill_missing_metadata_only_fills_missing_and_counts_failures():
    project = SimpleNamespace(id="p1", user_id="u1", title="测试书", theme=None,
                              genre=None, world_time_period=None, world_location=None,
                              world_atmosphere=None, world_rules=None)

    partial = {"emotion": "紧张", "key_points": ["既有要点"]}
    complete = {
        "scenes": ["城门"],
        "characters": [{"name": "甲", "type": "character"}],
        "key_points": ["甲登场"],
        "emotion": "戒备→信任",
        "goal": "获得线索",
    }
    # 9 条待补（1 条部分缺失 + 8 条全空），外加 1 条已完整（应计入 skipped）
    outlines = [_outline(1, partial)] + [_outline(i) for i in range(2, 10)] + [
        _outline(10, complete)
    ]

    batch_one = [
        {
            "order_index": index,
            "scenes": [f"场景{index}"],
            "characters": [{"name": f"角色{index}", "type": "character"}],
            "key_points": [f"事件{index}"],
            "emotion": "平静",  # 已存在的 emotion 不得被覆盖
            "goal": f"目标{index}",
        }
        for index in range(1, 8)  # 故意漏掉 order_index=8，用于失败计数
    ]
    ai_service = _FakeAIService([batch_one, RuntimeError("上游失败")])

    db = _build_db(project, outlines)
    progress_calls = []

    async def _progress(message, current, total):
        progress_calls.append((message, current, total))

    summary = asyncio.run(
        fill_missing_metadata(
            db,
            "p1",
            None,
            ai_service,
            provider=None,
            model="test-model",
            content_language="zh",
            progress=_progress,
        )
    )

    assert summary == {"total": 10, "filled": 7, "skipped": 1, "failed": 2}
    # 两批 → 两次 AI 调用，提示词带字段定义与占位值禁令
    assert len(ai_service.prompts) == 2
    assert "禁止使用" in ai_service.prompts[0]
    assert "order_index=1" in ai_service.prompts[0]
    # 进度回调按批触发（8 + 1）
    assert [call[1:] for call in progress_calls] == [(8, 9), (9, 9)]

    # 部分缺失的大纲：补了新键，原有 emotion/key_points 未被覆盖
    merged = json.loads(outlines[0].structure)
    assert merged["emotion"] == "紧张"
    assert merged["key_points"] == ["既有要点"]
    assert merged["scenes"] == ["场景1"]
    assert merged["goal"] == "目标1"
    assert merged["characters"] == [{"name": "角色1", "type": "character"}]

    # 批次失败的大纲保持原样（空 structure 不写回）
    assert outlines[8].structure is None  # order_index=9，所在第二批失败
    # 已完整的大纲一字不动
    assert json.loads(outlines[9].structure) == complete


def test_fill_missing_metadata_returns_counts_without_ai_when_nothing_missing():
    project = SimpleNamespace(id="p1", user_id="u1", title="测试书")
    complete = {
        "scenes": ["城门"],
        "characters": [{"name": "甲", "type": "character"}],
        "key_points": ["甲登场"],
        "emotion": "紧张",
        "goal": "获得线索",
    }
    db = _build_db(project, [_outline(1, complete)])
    ai_service = _FakeAIService([])

    summary = asyncio.run(
        fill_missing_metadata(
            db, "p1", ["outline-1"], ai_service,
            provider=None, model=None, content_language=None,
        )
    )

    assert summary == {"total": 1, "filled": 0, "skipped": 1, "failed": 0}
    assert ai_service.prompts == []
