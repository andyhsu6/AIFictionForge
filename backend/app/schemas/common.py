"""跨 schema 共享的通用词汇类型（避免 schema 模块互相依赖）。

i18n plan todo 17 prelude：content_language 词表从中立模块导出，
settings.py 的校验 Annotated 与语言解析器（services/language_resolver.py）
统一引用此处定义，避免字面量重复维护。
"""
from typing import Literal

# AI 生成内容语言原始词表：None（未指定）在字段层用 Optional 表达，
# "auto" 表示显式跟随界面语言；"zh"/"en" 为最终生成语言。
ContentLanguageLiteral = Literal["auto", "zh", "en"]
