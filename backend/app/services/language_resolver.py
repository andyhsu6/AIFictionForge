"""AI 生成内容语言解析与指令注入（i18n plan todo 17）。

优先级链（硬性约定，与 plan C4 一致）：
    per-generation request override > 用户 preferences.content_language
    > UI 语言（preferences.language，服务端代理）> "zh"

任一层级的 None / "auto" / 非法值都视为"未指定"，向下回落；
resolve_generation_language 的返回值恒为 "zh" 或 "en"。

注入方式：语言指令作为独立尾部段追加在模板渲染结果之外（append_language_instruction），
不要求 DB 可编辑模板内含任何语言变量；旧模板（含 {{...}} 转义花括号/既有占位符）
格式化行为不变。resolve_generation_language 为纯函数；
resolve_user_generation_language 负责从数据库读取用户偏好后复用同一优先级链。
"""
import json
from typing import Any, Dict, Literal, Mapping, Optional, Union

from sqlalchemy.ext.asyncio import AsyncSession

from app.schemas.common import ContentLanguageLiteral  # noqa: F401 - 词表统一出口，供调用方按需引用

# 最终生成语言（解析链终点）
GenerationLanguage = Literal["zh", "en"]

# 各最终语言的生成指令文本（plan todo 17 指定文案，勿改字面量——测试与注入均引用）
LANGUAGE_INSTRUCTIONS: Dict[GenerationLanguage, str] = {
    "zh": "请使用简体中文回复。",
    "en": "Please respond in English.",
}

# 请求层/content_language 层的合法"显式指定"值（"auto" 与 None 一样表示回落）；
# 用 str→GenerationLanguage 映射表达词表，查表命中即返回，天然收窄字面量类型
_EXPLICIT_CONTENT_LANGUAGE_MAP: Dict[str, GenerationLanguage] = {"zh": "zh", "en": "en"}


def normalize_content_language(value: Any) -> Optional[GenerationLanguage]:
    """把 content_language 原始值归一化为 "zh"/"en"；None/"auto"/非法值返回 None（回落）。"""
    if isinstance(value, str):
        return _EXPLICIT_CONTENT_LANGUAGE_MAP.get(value.strip().lower())
    return None


def normalize_ui_language(value: Any) -> Optional[GenerationLanguage]:
    """把 UI 语言归一化为 "zh"/"en"；兼容历史区域变体（zh-CN→zh，en-US→en）。

    preferences.language 写入侧已校验 ^(zh|en)$，此处宽容处理仅针对
    历史数据/手工编辑的 preferences JSON；无法识别返回 None（回落到默认 zh）。
    """
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized.startswith("zh"):
            return "zh"
        if normalized.startswith("en"):
            return "en"
    return None


def _coerce_preferences(preferences: Union[None, str, Mapping[str, Any]]) -> Mapping[str, Any]:
    """接受 dict 或 preferences JSON 字符串，统一为 Mapping；损坏输入返回空 Mapping。"""
    if preferences is None:
        return {}
    if isinstance(preferences, Mapping):
        return preferences
    if isinstance(preferences, str):
        try:
            parsed = json.loads(preferences or "{}")
        except (json.JSONDecodeError, TypeError):
            return {}
        return parsed if isinstance(parsed, Mapping) else {}
    return {}


def resolve_generation_language(
    per_request: Any,
    preferences: Union[None, str, Mapping[str, Any]] = None,
) -> GenerationLanguage:
    """按优先级链解析最终生成语言。

    Args:
        per_request: 生成请求上的 content_language 原始值（None/"auto"/"zh"/"en"；
            非法值同样回落，不抛错——schema 层已先行 422 拦截明显非法输入）。
        preferences: 用户偏好（dict 或 Settings.preferences JSON 字符串）。
            依次读取 content_language、language 两级。

    Returns:
        "zh" 或 "en"（永不为 None）。
    """
    # 1. per-generation override
    resolved = normalize_content_language(per_request)
    if resolved:
        return resolved

    # 2/3. 用户偏好：content_language > UI language
    prefs = _coerce_preferences(preferences)
    resolved = normalize_content_language(prefs.get("content_language"))
    if resolved:
        return resolved
    resolved = normalize_ui_language(prefs.get("language"))
    if resolved:
        return resolved

    # 4. 最终默认
    return "zh"


def language_instruction(language: GenerationLanguage) -> str:
    """返回目标语言的生成指令文本。"""
    return LANGUAGE_INSTRUCTIONS[language]


def append_language_instruction(prompt: str, language: GenerationLanguage) -> str:
    """把语言指令作为独立尾部段追加到已渲染 prompt 之后（追加式注入）。

    - 追加发生在模板 format 之后，DB 可编辑模板无需包含任何语言变量；
    - 旧模板的 {{...}} 转义花括号与既有占位符行为不受影响；
    - resolved zh 同样追加（plan 接受 zh 提示词新增指令段）。
    """
    return f"{prompt.rstrip()}\n\n{LANGUAGE_INSTRUCTIONS[language]}"


async def resolve_user_generation_language(
    db: AsyncSession | None,
    user_id: Optional[str],
    per_request: Any = None,
) -> GenerationLanguage:
    """DB 版解析：读取用户 Settings.preferences 后走 resolve_generation_language 链。

    供 API/service 层在已有 db 会话的生成路径中调用；
    user_id 缺失或用户无 Settings 行时按"无偏好"处理（回落链仍生效）。
    """
    raw_preferences: Optional[str] = None
    if db is not None and user_id:
        try:
            from sqlalchemy import select

            from app.models.settings import Settings

            result = await db.execute(select(Settings).where(Settings.user_id == user_id))
            user_settings = result.scalar_one_or_none()
            raw_preferences = getattr(user_settings, "preferences", None)
        except Exception:
            # 偏好读取失败不阻断生成：按无偏好解析（链尾回落 zh）
            raw_preferences = None
    return resolve_generation_language(per_request, raw_preferences)
