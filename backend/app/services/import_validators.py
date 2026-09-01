"""拆书导入四个生成步骤的字段级 schema 校验器。

每个校验器签名均为 ``validator(data: Any) -> None``，与
``AIService.call_with_json_retry(validator=...)`` 钩子兼容：
- 校验通过 → 无返回（返回 None）
- 校验失败 → 抛 ``ValueError``，错误信息必须指明具体违规字段，
  以便注入重试提示后模型能针对性修正

四个步骤：
1. 世界观生成：``validate_world_building``
2. 职业体系生成：``validate_career_system``
3. 角色/组织生成（分批）：``validate_characters_batch``
4. 原文关系抽取：``validate_relationships``
"""
from typing import Any

# 角色 role_type 枚举（对应 CHARACTERS_BATCH_GENERATION 模板）
ROLE_TYPE_ENUM = {"protagonist", "supporting", "antagonist"}
# 关系 status 枚举（对应 RELATIONSHIP_EXTRACTION 模板 constraints）
RELATIONSHIP_STATUS_ENUM = {"active", "broken", "past", "complicated"}
# 主/副职业数量口径（对应 CAREER_SYSTEM_GENERATION 模板）
MAIN_CAREERS_RANGE = (1, 3)
SUB_CAREERS_RANGE = (0, 2)
# 关系类型数量：至少 1 个，最多 4 个（对应 RELATIONSHIP_EXTRACTION 模板）
RELATIONSHIP_TYPES_RANGE = (1, 4)
# 亲密度合法区间（-100..100，负值表示敌对，对应模板数值范围）
INTIMACY_LEVEL_RANGE = (-100, 100)
# 组织影响力合法区间（对应模板 power_level: 70-95，放宽为 0-100 兜底）
POWER_LEVEL_RANGE = (0, 100)

WORLD_BUILDING_FIELDS = ("time_period", "location", "atmosphere", "rules")


def _is_nonempty_str(value: Any) -> bool:
    return isinstance(value, str) and bool(value.strip())


# 角色字段质量检查：禁止把正文句子/对话/内心独白原样抄入 personality/background/appearance。
# 保守启发式，只拦截明确"疑似粘贴原文"的信号，避免误伤正常生成的角色设定。
PASTED_NARRATION_ELLIPSIS = ("……", "…")
PASTED_NARRATION_QUOTES = ("「", "」", "『", "』")
PASTED_NARRATION_FIRST_PERSON_MIN_LEN = 60
CHARACTER_FIELD_QUALITY_KEYS = ("personality", "background", "appearance")


def _looks_like_pasted_narration(text: str) -> bool:
    """判断字段内容是否疑似从正文抄录的叙述/对话片段（省略号/引号/第一人称长句三个信号）。"""
    if not isinstance(text, str) or not text.strip():
        return False
    if any(mark in text for mark in PASTED_NARRATION_ELLIPSIS):
        return True
    if any(mark in text for mark in PASTED_NARRATION_QUOTES):
        return True
    if (
        len(text) > PASTED_NARRATION_FIRST_PERSON_MIN_LEN
        and "，" in text
        and "我" in text
    ):
        return True
    return False


def validate_world_building(data: Any) -> None:
    """世界观：必须是 dict，且 time_period/location/atmosphere/rules 四字段均为非空字符串。"""
    if not isinstance(data, dict):
        raise ValueError("字段 world_data 必须是对象（JSON object），实际为 " + type(data).__name__)

    for field in WORLD_BUILDING_FIELDS:
        value = data.get(field)
        if not _is_nonempty_str(value):
            raise ValueError(
                f"字段 {field} 必须是非空字符串，实际为 {value!r}（类型 {type(value).__name__}）"
            )


def _validate_career_item(item: Any, career_type: str, idx: int) -> None:
    if not isinstance(item, dict):
        raise ValueError(f"字段 {career_type}_careers[{idx}] 必须是对象，实际为 {type(item).__name__}")

    name = item.get("name")
    if not _is_nonempty_str(name):
        raise ValueError(
            f"字段 {career_type}_careers[{idx}].name 必须是非空字符串，实际为 {name!r}"
        )

    stages = item.get("stages")
    if stages is not None and not isinstance(stages, list):
        raise ValueError(
            f"字段 {career_type}_careers[{idx}].stages 必须是数组，实际为 {type(stages).__name__}"
        )


def validate_career_system(data: Any) -> None:
    """职业体系：dict 且 main_careers 1-3 个、sub_careers 0-2 个；每个职业必须有非空 name。"""
    if not isinstance(data, dict):
        raise ValueError("字段 career_data 必须是对象（JSON object），实际为 " + type(data).__name__)

    for key, (min_count, max_count), label, singular in (
        ("main_careers", MAIN_CAREERS_RANGE, "主职业", "main"),
        ("sub_careers", SUB_CAREERS_RANGE, "副职业", "sub"),
    ):
        items = data.get(key)
        if not isinstance(items, list):
            raise ValueError(f"字段 {key} 必须是数组，实际为 {type(items).__name__}")
        if not (min_count <= len(items) <= max_count):
            raise ValueError(
                f"字段 {key} 数量必须为 {min_count}-{max_count} 个（{label}），实际为 {len(items)} 个"
            )
        for idx, item in enumerate(items):
            _validate_career_item(item, singular, idx)


def _validate_age(value: Any, idx: int) -> None:
    """年龄：int / 数字字符串 / None 均可；其他类型报错。"""
    if value is None or isinstance(value, int):
        return
    if isinstance(value, str) and value.strip().isdigit():
        return
    raise ValueError(f"字段 characters[{idx}].age 必须是整数、数字字符串或 null，实际为 {value!r}")


def validate_characters_batch(data: Any) -> None:
    """角色/组织批量生成：必须是数组；每个对象必须有非空 name，role_type 枚举合法等。"""
    if not isinstance(data, list):
        raise ValueError("字段 characters_data 必须是数组（JSON array），实际为 " + type(data).__name__)

    for idx, item in enumerate(data):
        if not isinstance(item, dict):
            raise ValueError(f"字段 characters[{idx}] 必须是对象，实际为 {type(item).__name__}")

        name = item.get("name")
        if not _is_nonempty_str(name):
            raise ValueError(f"字段 characters[{idx}].name 必须是非空字符串，实际为 {name!r}")

        role_type = item.get("role_type")
        if role_type is not None and role_type not in ROLE_TYPE_ENUM:
            raise ValueError(
                f"字段 characters[{idx}].role_type 枚举值非法: {role_type!r}，"
                f"仅允许 {sorted(ROLE_TYPE_ENUM)}"
            )

        is_organization = item.get("is_organization")
        if is_organization is not None and not isinstance(is_organization, bool):
            raise ValueError(
                f"字段 characters[{idx}].is_organization 必须是布尔值，实际为 {type(is_organization).__name__}"
            )

        _validate_age(item.get("age"), idx)

        for field in CHARACTER_FIELD_QUALITY_KEYS:
            field_value = item.get(field)
            if field_value is not None and not isinstance(field_value, str):
                raise ValueError(
                    f"字段 characters[{idx}].{field} 必须是字符串，实际为 {type(field_value).__name__}"
                )
            if _looks_like_pasted_narration(field_value or ""):
                raise ValueError(
                    f"字段 characters[{idx}].{field} 疑似抄录原文叙述，"
                    f"请勿把正文句子/对话/内心独白原样抄入；无明确描写时请输出空字符串"
                )

        if is_organization:
            power_level = item.get("power_level")
            if power_level is not None:
                if not isinstance(power_level, int) or isinstance(power_level, bool):
                    raise ValueError(
                        f"字段 characters[{idx}].power_level 必须是整数，实际为 {power_level!r}"
                    )
                if not (POWER_LEVEL_RANGE[0] <= power_level <= POWER_LEVEL_RANGE[1]):
                    raise ValueError(
                        f"字段 characters[{idx}].power_level 必须为 {POWER_LEVEL_RANGE[0]}-{POWER_LEVEL_RANGE[1]} 整数，"
                        f"实际为 {power_level}"
                    )


def validate_relationships(data: Any) -> None:
    """原文关系抽取：必须是数组；character_a/character_b 非空且不相等，关系类型 1-4 个等。"""
    if not isinstance(data, list):
        raise ValueError("字段 relationships_data 必须是数组（JSON array），实际为 " + type(data).__name__)

    for idx, item in enumerate(data):
        if not isinstance(item, dict):
            raise ValueError(f"字段 relationships[{idx}] 必须是对象，实际为 {type(item).__name__}")

        char_a = item.get("character_a")
        char_b = item.get("character_b")
        if not _is_nonempty_str(char_a):
            raise ValueError(f"字段 relationships[{idx}].character_a 必须是非空字符串，实际为 {char_a!r}")
        if not _is_nonempty_str(char_b):
            raise ValueError(f"字段 relationships[{idx}].character_b 必须是非空字符串，实际为 {char_b!r}")
        if char_a.strip() == char_b.strip():
            raise ValueError(f"字段 relationships[{idx}].character_a 与 character_b 不能相同: {char_a!r}")

        types = item.get("relationship_types")
        if not isinstance(types, list):
            raise ValueError(
                f"字段 relationships[{idx}].relationship_types 必须是数组，实际为 {type(types).__name__}"
            )
        min_types, max_types = RELATIONSHIP_TYPES_RANGE
        if not (min_types <= len(types) <= max_types):
            raise ValueError(
                f"字段 relationships[{idx}].relationship_types 数量必须为 {min_types}-{max_types} 个，"
                f"实际为 {len(types)} 个"
            )
        for t_idx, rel_type in enumerate(types):
            if not _is_nonempty_str(rel_type):
                raise ValueError(
                    f"字段 relationships[{idx}].relationship_types[{t_idx}] 必须是非空字符串，"
                    f"实际为 {rel_type!r}"
                )

        status = item.get("status")
        if status is not None and status not in RELATIONSHIP_STATUS_ENUM:
            raise ValueError(
                f"字段 relationships[{idx}].status 枚举值非法: {status!r}，"
                f"仅允许 {sorted(RELATIONSHIP_STATUS_ENUM)}"
            )

        intimacy = item.get("intimacy_level")
        if intimacy is not None:
            if not isinstance(intimacy, int) or isinstance(intimacy, bool):
                raise ValueError(
                    f"字段 relationships[{idx}].intimacy_level 必须是整数，实际为 {intimacy!r}"
                )
            lo, hi = INTIMACY_LEVEL_RANGE
            if not (lo <= intimacy <= hi):
                raise ValueError(
                    f"字段 relationships[{idx}].intimacy_level 必须为 {lo}-{hi} 整数，实际为 {intimacy}"
                )
