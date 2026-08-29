"""关系多类型与项目级类型 helper 测试。"""
from app.services.relationship_service import (
    is_probably_proper_noun_type,
    normalize_relationship_type_name,
)


def test_normalize_type_name_trims_and_caps_length():
    assert normalize_relationship_type_name(" 同门 ") == "同门"
    assert normalize_relationship_type_name("") is None
    assert normalize_relationship_type_name(None) is None
    long_name = "长" * 60
    normalized = normalize_relationship_type_name(long_name)
    assert normalized is not None and len(normalized) == 50


def test_proper_noun_types_are_rejected_but_generic_accepted():
    assert is_probably_proper_noun_type("玄天宗记名弟子")
    assert is_probably_proper_noun_type("九尾狐契约")
    assert is_probably_proper_noun_type("林家三少爷")
    assert not is_probably_proper_noun_type("同门")
    assert not is_probably_proper_noun_type("宗门记名弟子")
    assert not is_probably_proper_noun_type("主仆")
    assert not is_probably_proper_noun_type("契约")
