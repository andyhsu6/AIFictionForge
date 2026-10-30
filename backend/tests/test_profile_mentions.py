"""``profile_mentions`` 纯函数 + 离线脚本别名口径（issue #190 后续）。

全部用例不连 DB、不调模型：实体一律用 ``SimpleNamespace`` 构造，
脚本模块用 importlib 直接按文件加载（``backend/scripts`` 没有 ``__init__.py``）。
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import SimpleNamespace

from app.services.profile_mentions import (
    MIN_TOKEN_LEN,
    character_tokens,
    count_mentions,
    mention_tokens,
    parse_aliases,
    unique_tokens_by_entity,
)

SCRIPTS_DIR = Path(__file__).resolve().parents[1] / "scripts"


def _load_sweep():
    """按文件路径加载 ``sweep_profiles_readonly``（脚本目录不是包）。"""
    cached = sys.modules.get("sweep_profiles_readonly")
    if cached is not None:
        return cached
    spec = importlib.util.spec_from_file_location(
        "sweep_profiles_readonly", SCRIPTS_DIR / "sweep_profiles_readonly.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["sweep_profiles_readonly"] = module
    spec.loader.exec_module(module)
    return module


def _entity(entity_id: str, name: str, aliases=None) -> SimpleNamespace:
    return SimpleNamespace(
        id=entity_id,
        name=name,
        aliases=aliases,
        is_organization=False,
        personality=None,
        background=None,
        appearance=None,
    )


# --------------------------------------------------------------------------- #
# parse_aliases
# --------------------------------------------------------------------------- #
def test_parse_aliases_none_and_empty():
    assert parse_aliases(None) == []
    assert parse_aliases("") == []
    assert parse_aliases("   ") == []


def test_parse_aliases_json_array_string():
    assert parse_aliases('["三哥", "老张"]') == ["三哥", "老张"]


def test_parse_aliases_accepts_list():
    assert parse_aliases(["三哥", " 老张 "]) == ["三哥", "老张"]


def test_parse_aliases_comma_separated_string():
    assert parse_aliases("三哥,老张") == ["三哥", "老张"]
    assert parse_aliases('"三哥,老张"') == ["三哥", "老张"]


def test_parse_aliases_bad_values():
    # 半截 JSON 不能当逗号分隔串解析，否则会漏出 "[张三" 这种脏 token
    assert parse_aliases("[张三,李四") == []
    assert parse_aliases("{bad") == []
    assert parse_aliases(123) == []
    assert parse_aliases({"name": "张三"}) == []


def test_parse_aliases_drops_empty_and_non_string_elements():
    assert parse_aliases('["张三", "", "  ", 5, null, "李四"]') == ["张三", "李四"]


# --------------------------------------------------------------------------- #
# character_tokens
# --------------------------------------------------------------------------- #
def test_character_tokens_name_first_then_aliases():
    entity = _entity("e1", "张三", '["三哥", "老张"]')
    assert character_tokens(entity) == ["张三", "三哥", "老张"]


def test_character_tokens_dedupes_and_filters_short_tokens():
    entity = _entity("e1", " 张三 ", '["张三", "三", "哥", "三哥"]')
    tokens = character_tokens(entity)
    assert tokens == ["张三", "三哥"]
    assert all(len(token) >= MIN_TOKEN_LEN for token in tokens)


def test_character_tokens_works_without_aliases_attribute():
    assert character_tokens(SimpleNamespace(name="李四")) == ["李四"]


# --------------------------------------------------------------------------- #
# unique_tokens_by_entity
# --------------------------------------------------------------------------- #
def test_unique_tokens_keeps_unambiguous_tokens():
    entities = [_entity("a", "张三", '["三哥"]'), _entity("b", "李四", '["四爷"]')]
    unique = unique_tokens_by_entity(entities)
    assert unique["a"] == ["张三", "三哥"]
    assert unique["b"] == ["李四", "四爷"]


def test_unique_tokens_excludes_shared_token_from_both_entities():
    entities = [_entity("a", "张三", '["老大"]'), _entity("b", "李四", '["老大"]')]
    unique = unique_tokens_by_entity(entities)
    assert "老大" not in unique["a"]
    assert "老大" not in unique["b"]
    assert unique["a"] == ["张三"]
    assert unique["b"] == ["李四"]


def test_unique_tokens_excludes_substring_conflicts():
    # 单字 token 先被 MIN_TOKEN_LEN 过滤；「沈浪」/「沈浪风」互为子串，双方都排除
    entities = [_entity("a", "沈", '["沈"]'), _entity("b", "沈浪"), _entity("c", "沈浪风")]
    unique = unique_tokens_by_entity(entities)
    assert unique["a"] == []
    assert unique["b"] == []
    assert unique["c"] == []


def test_unique_tokens_allows_same_entity_name_alias_overlap():
    # 同实体内 name 与 alias 互相包含不算冲突（两者都描述同一个实体）
    entities = [_entity("a", "大乾朝廷", '["朝廷"]')]
    unique = unique_tokens_by_entity(entities)
    assert unique["a"] == ["大乾朝廷", "朝廷"]


# --------------------------------------------------------------------------- #
# count_mentions / mention_tokens
# --------------------------------------------------------------------------- #
def test_count_mentions_sums_each_token():
    assert count_mentions("三哥来了，三哥走了", ["三哥"]) == 2
    assert count_mentions("三哥来了", ["三哥", "张三"]) == 1
    assert count_mentions("", ["三哥"]) == 0
    assert count_mentions("正文", []) == 0


def test_mention_tokens_falls_back_to_name_when_map_missing():
    entity = _entity("a", "张三", '["三哥"]')
    assert mention_tokens(entity, {}) == ["张三"]
    assert mention_tokens(entity, {"a": []}) == ["张三"]


def test_mention_tokens_uses_unique_map_when_available():
    entity = _entity("a", "张三", '["三哥"]')
    assert mention_tokens(entity, {"a": ["张三", "三哥"]}) == ["张三", "三哥"]


def test_mention_tokens_filters_blank_name():
    assert mention_tokens(_entity("a", ""), {}) == []


# --------------------------------------------------------------------------- #
# sweep._entry_map
# --------------------------------------------------------------------------- #
def test_entry_map_resolves_unique_alias_to_primary_name():
    sweep = _load_sweep()
    batch = [_entity("a", "张三", '["三哥"]'), _entity("b", "李四")]
    entries = [{"name": "三哥", "facts": []}, {"name": "李四", "facts": []}]
    matched, unmatched = sweep._entry_map(entries, batch)
    assert set(matched) == {"张三", "李四"}
    assert matched["张三"]["name"] == "三哥"
    assert unmatched == []


def test_entry_map_leaves_ambiguous_alias_unmatched():
    sweep = _load_sweep()
    batch = [_entity("a", "张三", '["老大"]'), _entity("b", "李四", '["老大"]')]
    entries = [{"name": "老大", "facts": []}]
    matched, unmatched = sweep._entry_map(entries, batch)
    assert matched == {}
    assert unmatched == ["老大"]


# --------------------------------------------------------------------------- #
# sweep._assign_entities
# --------------------------------------------------------------------------- #
def test_assign_entities_uses_unique_alias_when_name_absent():
    sweep = _load_sweep()
    entity = _entity("a", "张三", '["三哥"]')
    windows = [
        [SimpleNamespace(content="李四出门了。")],
        [SimpleNamespace(content="三哥回来了。")],
    ]
    assigned, unmentioned = sweep._assign_entities([entity], windows)
    assert unmentioned == []
    assert list(assigned) == [1]
    assert assigned[1] == [entity]


def test_assign_entities_keeps_name_only_behaviour_for_ambiguous_alias():
    sweep = _load_sweep()
    a = _entity("a", "张三", '["老大"]')
    b = _entity("b", "李四", '["老大"]')
    windows = [[SimpleNamespace(content="老大回来了。张三不在。")]]
    assigned, unmentioned = sweep._assign_entities([a, b], windows)
    assert assigned[0] == [a]
    assert unmentioned == [b]


def test_assign_entities_reports_entity_with_no_mention():
    sweep = _load_sweep()
    entity = _entity("a", "张三")
    assigned, unmentioned = sweep._assign_entities(
        [entity], [[SimpleNamespace(content="无关正文。")]]
    )
    assert assigned == {}
    assert unmentioned == [entity]


def test_assign_entities_prefers_earliest_window_on_tie():
    sweep = _load_sweep()
    entity = _entity("a", "张三")
    windows = [
        [SimpleNamespace(content="张三")],
        [SimpleNamespace(content="张三")],
    ]
    assigned, unmentioned = sweep._assign_entities([entity], windows)
    assert unmentioned == []
    assert assigned[0] == [entity]
