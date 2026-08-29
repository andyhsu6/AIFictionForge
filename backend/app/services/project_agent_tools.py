"""项目智能体内部工具注册表与执行器。"""
from dataclasses import dataclass
from datetime import datetime
import json
from typing import Any, Awaitable, Callable

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.chapter import Chapter
from app.models.character import Character
from app.models.foreshadow import Foreshadow
from app.models.outline import Outline
from app.models.project import Project
from app.models.relationship import CharacterRelationship, Organization
from app.services.relationship_service import relationship_display_names
from app.services.project_agent_extended_tools import (
    EXTENDED_TOOL_SPECS,
    READ_TOOL_NAMES,
    WRITE_TOOL_NAMES,
    ProjectAgentExtendedTools,
)
from app.services.project_agent_operational_tools import (
    OPERATIONAL_READ_TOOL_NAMES,
    OPERATIONAL_TOOL_SPECS,
    OPERATIONAL_WRITE_TOOL_NAMES,
    ProjectAgentOperationalTools,
)
from app.services.project_agent_selectors import (
    clean_identifier,
    find_chapter,
    find_character,
    find_outline,
    normalize_tool_arguments,
)


ToolHandler = Callable[[dict[str, Any]], Awaitable[dict[str, Any]]]


@dataclass(frozen=True)
class ProjectAgentTool:
    name: str
    description: str
    parameters: dict[str, Any]
    risk_level: int = 0
    resources: tuple[str, ...] = ()

    @property
    def requires_confirmation(self) -> bool:
        return self.risk_level >= 2

    def as_model_tool(self) -> dict[str, Any]:
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.parameters,
            },
        }


def _object_schema(properties: dict[str, Any], required: list[str] | None = None) -> dict[str, Any]:
    schema: dict[str, Any] = {
        "type": "object",
        "properties": properties,
        "additionalProperties": False,
    }
    if required:
        schema["required"] = required
    return schema


def _selector_schema(properties: dict[str, Any], *selectors: str) -> dict[str, Any]:
    schema = _object_schema(properties)
    schema["anyOf"] = [{"required": [selector]} for selector in selectors]
    return schema


def _json_value(value: Any) -> Any:
    if isinstance(value, datetime):
        return value.isoformat()
    return value


def normalize_tool_preview(
    preview: dict[str, Any] | None,
    arguments: dict[str, Any],
) -> dict[str, Any] | None:
    """移除旧预览中由未提供可选参数的 null 产生的伪修改。"""
    if not preview or not isinstance(preview.get("changes"), dict):
        return preview
    null_fields = {key for key, value in arguments.items() if value is None}
    if not null_fields:
        return preview
    changes = {
        key: value
        for key, value in preview["changes"].items()
        if key not in null_fields
    }
    return {**preview, "changes": changes}


class ProjectAgentToolRegistry:
    """工具执行上下文被固定绑定到当前用户已验证的项目。"""

    PROJECT_FIELDS = {
        "title", "description", "theme", "genre", "target_words", "status",
        "world_time_period", "world_location", "world_atmosphere", "world_rules",
        "chapter_count", "narrative_perspective", "character_count",
    }
    OUTLINE_FIELDS = {"title", "content"}
    CHARACTER_FIELDS = {
        "name", "age", "gender", "role_type", "personality", "background",
        "appearance", "status", "traits", "organization_type",
        "organization_purpose", "avatar_url", "status_changed_chapter",
        "current_state", "state_updated_chapter",
    }
    CHAPTER_FIELDS = {"title", "summary", "status"}

    def __init__(self, project: Project, db: AsyncSession):
        self.project = project
        self.db = db
        self.extended = ProjectAgentExtendedTools(project, db)
        self.operational = ProjectAgentOperationalTools(project, db)
        self._tools = {tool.name: tool for tool in self._build_tools()}

    def _build_tools(self) -> list[ProjectAgentTool]:
        text = {"type": ["string", "null"]}
        integer = {"type": ["integer", "null"]}
        required_text = {"type": "string", "minLength": 1, "pattern": r".*\S.*"}
        identifier = {"type": "string", "minLength": 1, "pattern": r".*\S.*"}
        tools = [
            ProjectAgentTool(
                "get_project_overview",
                "获取当前项目基本信息、世界设定和数据数量。",
                _object_schema({}),
            ),
            ProjectAgentTool(
                "list_outlines",
                "查询当前项目的大纲列表，可按标题或正文关键词筛选。",
                _object_schema({
                    "query": {"type": "string"},
                    "limit": {"type": "integer", "minimum": 1, "maximum": 100},
                }),
            ),
            ProjectAgentTool(
                "get_outline_detail",
                "按大纲ID或序号获取当前项目的一条完整大纲。",
                _selector_schema({
                    "outline_id": identifier,
                    "order_index": {"type": "integer", "minimum": 1},
                }, "outline_id", "order_index"),
            ),
            ProjectAgentTool(
                "list_characters",
                "查询当前项目的角色或组织，可按名称筛选。",
                _object_schema({
                    "query": {"type": "string"},
                    "entity_type": {"type": "string", "enum": ["all", "character", "organization"]},
                    "limit": {"type": "integer", "minimum": 1, "maximum": 100},
                }),
            ),
            ProjectAgentTool(
                "get_character_detail",
                "按角色ID或名称获取当前项目的角色/组织详情。",
                _selector_schema({
                    "character_id": identifier,
                    "name": {"type": "string"},
                }, "character_id", "name"),
            ),
            ProjectAgentTool(
                "list_chapters",
                "查询当前项目章节列表和摘要，不返回完整正文。",
                _object_schema({
                    "query": {"type": "string"},
                    "limit": {"type": "integer", "minimum": 1, "maximum": 100},
                }),
            ),
            ProjectAgentTool(
                "get_chapter_detail",
                "按章节ID或章节号获取章节详情，可返回完整正文。",
                _selector_schema({
                    "chapter_id": identifier,
                    "chapter_number": {"type": "integer", "minimum": 1},
                    "include_content": {"type": "boolean"},
                }, "chapter_id", "chapter_number"),
            ),
            ProjectAgentTool(
                "list_relationships",
                "查询当前项目的角色关系。",
                _object_schema({"limit": {"type": "integer", "minimum": 1, "maximum": 100}}),
            ),
            ProjectAgentTool(
                "list_foreshadows",
                "查询当前项目伏笔，可按状态筛选。",
                _object_schema({
                    "status": {"type": "string"},
                    "limit": {"type": "integer", "minimum": 1, "maximum": 100},
                }),
            ),
            ProjectAgentTool(
                "update_project",
                "修改当前项目的基本信息或世界设定。调用后必须等待用户确认。",
                _object_schema({
                    "title": required_text, "description": text, "theme": text, "genre": text,
                    "target_words": integer,
                    "status": {"type": "string", "enum": ["planning", "writing", "revising", "completed"]},
                    "world_time_period": text,
                    "world_location": text, "world_atmosphere": text, "world_rules": text,
                    "chapter_count": integer, "narrative_perspective": text,
                    "character_count": integer,
                }),
                risk_level=2,
                resources=("projects",),
            ),
            ProjectAgentTool(
                "update_outline",
                "按ID或序号修改当前项目的大纲标题和内容。调用后必须等待用户确认。",
                _selector_schema({
                    "outline_id": identifier,
                    "order_index": {"type": "integer", "minimum": 1},
                    "title": required_text,
                    "content": text,
                }, "outline_id", "order_index"),
                risk_level=2,
                resources=("outlines", "chapters"),
            ),
            ProjectAgentTool(
                "update_character",
                "按ID或名称修改当前项目角色信息。调用后必须等待用户确认。",
                _selector_schema({
                    "character_id": identifier, "character_name": {"type": "string", "pattern": r".*\S.*"},
                    "name": required_text, "age": text, "gender": text, "role_type": text,
                    "personality": text, "background": text, "appearance": text,
                    "organization_type": text, "organization_purpose": text,
                    "avatar_url": text, "status_changed_chapter": integer,
                    "current_state": text, "state_updated_chapter": integer,
                    "status": {"type": "string", "enum": ["active", "deceased", "missing", "retired", "destroyed"]},
                    "traits": text,
                }, "character_id", "character_name"),
                risk_level=2,
                resources=("characters",),
            ),
            ProjectAgentTool(
                "update_chapter",
                "按ID或章节号修改章节标题、摘要或状态，不修改正文；只传需要修改的字段，"
                "未修改的字段必须省略，清空摘要请显式传空字符串；一对一模式会同步对应大纲。"
                "调用后必须等待用户确认。",
                _selector_schema({
                    "chapter_id": identifier,
                    "chapter_number": {"type": "integer", "minimum": 1},
                    "title": required_text, "summary": text,
                    "status": {"type": "string", "enum": ["draft", "pending", "writing", "completed"]},
                }, "chapter_id", "chapter_number"),
                risk_level=2,
                resources=("chapters", "outlines"),
            ),
        ]
        tools.extend(ProjectAgentTool(**spec) for spec in EXTENDED_TOOL_SPECS)
        tools.extend(ProjectAgentTool(**spec) for spec in OPERATIONAL_TOOL_SPECS)
        return tools

    def definitions(self) -> list[dict[str, Any]]:
        return [tool.as_model_tool() for tool in self._tools.values()]

    def get(self, name: str) -> ProjectAgentTool:
        tool = self._tools.get(name)
        if tool is None:
            raise ValueError(f"未注册的项目工具：{name}")
        return tool

    async def preview(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        arguments = normalize_tool_arguments(arguments)
        tool = self.get(name)
        if not tool.requires_confirmation:
            raise ValueError("只读工具不需要修改预览")
        if name in WRITE_TOOL_NAMES:
            return await self.extended.preview(name, arguments)
        if name in OPERATIONAL_WRITE_TOOL_NAMES:
            return await self.operational.preview(name, arguments)
        entity, fields, label = await self._resolve_update(name, arguments)
        changes = {
            field: {"before": _json_value(getattr(entity, field)), "after": _json_value(value)}
            for field, value in fields.items()
            if getattr(entity, field) != value
        }
        if not changes:
            raise ValueError("没有检测到需要修改的字段")
        return {
            "entity_type": entity.__class__.__name__.lower(),
            "entity_id": entity.id,
            "label": label,
            "changes": changes,
            "resources": list(tool.resources),
        }

    async def execute(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        arguments = normalize_tool_arguments(arguments)
        tool = self.get(name)
        if tool.requires_confirmation:
            if name in WRITE_TOOL_NAMES:
                return await self.extended.execute(name, arguments)
            if name in OPERATIONAL_WRITE_TOOL_NAMES:
                return await self.operational.execute(name, arguments)
            entity, fields, label = await self._resolve_update(name, arguments)
            before = {field: _json_value(getattr(entity, field)) for field in fields}
            for field, value in fields.items():
                setattr(entity, field, value)

            if isinstance(entity, Outline):
                self._sync_outline_structure(entity)
                if self.project.outline_mode == "one-to-one":
                    chapter_result = await self.db.execute(
                        select(Chapter).where(
                            Chapter.project_id == self.project.id,
                            Chapter.chapter_number == entity.order_index,
                        )
                    )
                    chapter = chapter_result.scalar_one_or_none()
                    if chapter:
                        if "title" in fields:
                            chapter.title = entity.title
                        if "content" in fields:
                            chapter.summary = entity.content

            if isinstance(entity, Chapter) and self.project.outline_mode == "one-to-one":
                outline = await self._find_outline_for_one_to_one_chapter(entity)
                if outline:
                    if "title" in fields:
                        outline.title = entity.title
                    if "summary" in fields:
                        outline.content = entity.summary or ""
                    if "title" in fields or "summary" in fields:
                        self._sync_outline_structure(outline)

            await self.db.flush()
            after = {field: _json_value(getattr(entity, field)) for field in fields}
            return {
                "message": f"已更新{self._entity_label(entity)}",
                "entity_id": entity.id,
                "before": before,
                "after": after,
                "resources": list(tool.resources),
            }

        if name in READ_TOOL_NAMES:
            return await self.extended.read(name, arguments)
        if name in OPERATIONAL_READ_TOOL_NAMES:
            return await self.operational.read(name, arguments)
        handler: ToolHandler = getattr(self, f"_{name}", None)
        if handler is None:
            raise ValueError(f"工具尚未实现：{name}")
        return await handler(arguments)

    async def _resolve_update(
        self,
        name: str,
        arguments: dict[str, Any],
    ) -> tuple[Any, dict[str, Any], str]:
        if name == "update_project":
            fields = self._pick_fields(arguments, self.PROJECT_FIELDS)
            self._validate_update_fields(name, fields)
            return self.project, fields, self._entity_label(self.project)
        if name == "update_outline":
            entity = await self._find_outline(arguments)
            fields = self._pick_fields(arguments, self.OUTLINE_FIELDS)
            self._validate_update_fields(name, fields)
            return entity, fields, self._entity_label(entity)
        if name == "update_character":
            entity = await self._find_character(arguments)
            fields = self._pick_fields(arguments, self.CHARACTER_FIELDS)
            self._validate_update_fields(name, fields)
            return entity, fields, self._entity_label(entity)
        if name == "update_chapter":
            entity = await self._find_chapter(arguments)
            fields = self._pick_fields(arguments, self.CHAPTER_FIELDS)
            self._validate_update_fields(name, fields)
            return entity, fields, self._entity_label(entity)
        raise ValueError(f"不支持的写入工具：{name}")

    @staticmethod
    def _entity_label(entity: Any) -> str:
        if isinstance(entity, Project):
            return f"项目《{entity.title}》"
        if isinstance(entity, Outline):
            return f"大纲《{entity.title}》"
        if isinstance(entity, Character):
            return f"角色《{entity.name}》"
        if isinstance(entity, Chapter):
            return f"第{entity.chapter_number}章《{entity.title}》"
        return entity.__class__.__name__

    @staticmethod
    def _pick_fields(arguments: dict[str, Any], allowed: set[str]) -> dict[str, Any]:
        # 部分模型会为未使用的可选参数补 null。null 表示“未提供”，不能被
        # 当成清空操作，否则只改标题时会误删摘要等现有字段。文本清空使用
        # 显式空字符串，仍会保留在 fields 中并进入修改预览。
        fields = {
            key: value
            for key, value in arguments.items()
            if key in allowed and value is not None
        }
        if not fields:
            raise ValueError("缺少可修改字段")
        return fields

    @staticmethod
    def _validate_update_fields(name: str, fields: dict[str, Any]) -> None:
        required_text_fields = {
            "update_project": {"title": 200},
            "update_outline": {"title": 200},
            "update_character": {"name": 100},
            "update_chapter": {"title": 200},
        }[name]
        for field, max_length in required_text_fields.items():
            if field not in fields:
                continue
            value = fields[field]
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"{field} 不能为空")
            if len(value) > max_length:
                raise ValueError(f"{field} 不能超过 {max_length} 个字符")

        status_values = {
            "update_project": {"planning", "writing", "revising", "completed"},
            "update_character": {"active", "deceased", "missing", "retired", "destroyed"},
            "update_chapter": {"draft", "pending", "writing", "completed"},
        }
        if "status" in fields and name in status_values and fields["status"] not in status_values[name]:
            raise ValueError("status 不是支持的状态值")

        if name == "update_project":
            for field in ("target_words", "chapter_count", "character_count"):
                value = fields.get(field)
                if value is not None and (not isinstance(value, int) or isinstance(value, bool) or value < 0):
                    raise ValueError(f"{field} 必须是非负整数")
        if name == "update_character":
            for field in ("status_changed_chapter", "state_updated_chapter"):
                value = fields.get(field)
                if value is not None and (not isinstance(value, int) or isinstance(value, bool) or value < 1):
                    raise ValueError(f"{field} 必须是正整数或 null")

    async def _find_outline(self, arguments: dict[str, Any]) -> Outline:
        return await find_outline(self.db, self.project.id, arguments)

    async def _find_character(self, arguments: dict[str, Any]) -> Character:
        return await find_character(self.db, self.project.id, arguments)

    async def _find_chapter(self, arguments: dict[str, Any]) -> Chapter:
        return await find_chapter(self.db, self.project.id, arguments)

    async def _find_outline_for_one_to_one_chapter(
        self,
        chapter: Chapter,
    ) -> Outline | None:
        if chapter.outline_id:
            result = await self.db.execute(
                select(Outline).where(
                    Outline.id == chapter.outline_id,
                    Outline.project_id == self.project.id,
                )
            )
            outline = result.scalar_one_or_none()
            if outline:
                return outline
        result = await self.db.execute(
            select(Outline).where(
                Outline.project_id == self.project.id,
                Outline.order_index == chapter.chapter_number,
            )
        )
        return result.scalar_one_or_none()

    @staticmethod
    def _sync_outline_structure(outline: Outline) -> None:
        try:
            structure = json.loads(outline.structure) if outline.structure else {}
        except json.JSONDecodeError:
            structure = {}
        if not isinstance(structure, dict):
            structure = {}
        structure["title"] = outline.title
        structure["summary"] = outline.content
        structure["content"] = outline.content
        outline.structure = json.dumps(structure, ensure_ascii=False)

    async def _get_project_overview(self, _: dict[str, Any]) -> dict[str, Any]:
        counts: dict[str, int] = {}
        for key, model in (
            ("outlines", Outline), ("characters", Character), ("chapters", Chapter),
            ("organizations", Organization), ("relationships", CharacterRelationship),
            ("foreshadows", Foreshadow),
        ):
            result = await self.db.execute(select(model.id).where(model.project_id == self.project.id))
            counts[key] = len(result.scalars().all())
        return {
            "project": {
                "id": self.project.id, "title": self.project.title,
                "description": self.project.description, "theme": self.project.theme,
                "genre": self.project.genre, "status": self.project.status,
                "outline_mode": self.project.outline_mode,
                "target_words": self.project.target_words,
                "current_words": self.project.current_words,
                "world_time_period": self.project.world_time_period,
                "world_location": self.project.world_location,
                "world_atmosphere": self.project.world_atmosphere,
                "world_rules": self.project.world_rules,
            },
            "counts": counts,
        }

    async def _list_outlines(self, arguments: dict[str, Any]) -> dict[str, Any]:
        limit = min(max(int(arguments.get("limit", 50)), 1), 100)
        query = select(Outline).where(Outline.project_id == self.project.id)
        keyword = str(arguments.get("query") or "").strip()
        if keyword:
            pattern = f"%{keyword}%"
            query = query.where(or_(Outline.title.ilike(pattern), Outline.content.ilike(pattern)))
        rows = (await self.db.execute(query.order_by(Outline.order_index).limit(limit))).scalars().all()
        return {"total": len(rows), "items": [
            {"id": row.id, "order_index": row.order_index, "title": row.title, "content": row.content}
            for row in rows
        ]}

    async def _get_outline_detail(self, arguments: dict[str, Any]) -> dict[str, Any]:
        row = await self._find_outline(arguments)
        return {"id": row.id, "order_index": row.order_index, "title": row.title,
                "content": row.content, "structure": row.structure}

    async def _list_characters(self, arguments: dict[str, Any]) -> dict[str, Any]:
        limit = min(max(int(arguments.get("limit", 50)), 1), 100)
        query = select(Character).where(Character.project_id == self.project.id)
        keyword = str(arguments.get("query") or "").strip()
        if keyword:
            query = query.where(Character.name.ilike(f"%{keyword}%"))
        entity_type = arguments.get("entity_type", "all")
        if entity_type == "character":
            query = query.where(Character.is_organization.is_(False))
        elif entity_type == "organization":
            query = query.where(Character.is_organization.is_(True))
        rows = (await self.db.execute(query.order_by(Character.created_at).limit(limit))).scalars().all()
        return {"total": len(rows), "items": [self._character_data(row, compact=True) for row in rows]}

    async def _get_character_detail(self, arguments: dict[str, Any]) -> dict[str, Any]:
        lookup = dict(arguments)
        if lookup.get("name") and not lookup.get("character_name"):
            lookup["character_name"] = lookup["name"]
        return self._character_data(await self._find_character(lookup), compact=False)

    @staticmethod
    def _character_data(row: Character, compact: bool) -> dict[str, Any]:
        data = {"id": row.id, "name": row.name, "is_organization": row.is_organization,
                "role_type": row.role_type, "status": row.status, "personality": row.personality}
        if not compact:
            data.update({"age": row.age, "gender": row.gender, "background": row.background,
                         "appearance": row.appearance, "traits": row.traits,
                         "current_state": row.current_state})
        return data

    async def _list_chapters(self, arguments: dict[str, Any]) -> dict[str, Any]:
        limit = min(max(int(arguments.get("limit", 50)), 1), 100)
        query = select(Chapter).where(Chapter.project_id == self.project.id)
        keyword = str(arguments.get("query") or "").strip()
        if keyword:
            pattern = f"%{keyword}%"
            query = query.where(or_(Chapter.title.ilike(pattern), Chapter.summary.ilike(pattern)))
        rows = (await self.db.execute(query.order_by(Chapter.chapter_number).limit(limit))).scalars().all()
        return {"total": len(rows), "items": [
            {"id": row.id, "chapter_number": row.chapter_number, "title": row.title,
             "summary": row.summary, "status": row.status, "word_count": row.word_count}
            for row in rows
        ]}

    async def _get_chapter_detail(self, arguments: dict[str, Any]) -> dict[str, Any]:
        row = await self._find_chapter(arguments)
        data = {"id": row.id, "chapter_number": row.chapter_number, "title": row.title,
                "summary": row.summary, "status": row.status, "word_count": row.word_count}
        if arguments.get("include_content", True):
            content = row.content or ""
            data["content"] = content[:50000]
            data["content_truncated"] = len(content) > 50000
        return data

    async def _list_relationships(self, arguments: dict[str, Any]) -> dict[str, Any]:
        limit = min(max(int(arguments.get("limit", 50)), 1), 100)
        rows = (await self.db.execute(
            select(CharacterRelationship)
            .where(CharacterRelationship.project_id == self.project.id)
            .order_by(CharacterRelationship.created_at)
            .limit(limit)
        )).scalars().all()
        char_rows = (await self.db.execute(
            select(Character).where(Character.project_id == self.project.id)
        )).scalars().all()
        names = {row.id: row.name for row in char_rows}
        return {"total": len(rows), "items": [
            {"id": row.id, "from": names.get(row.character_from_id),
             "to": names.get(row.character_to_id), "relationship_name": row.relationship_name,
             "relationship_type_names": await relationship_display_names(self.db, row),
             "intimacy_level": row.intimacy_level, "status": row.status,
             "description": row.description}
            for row in rows
        ]}

    async def _list_foreshadows(self, arguments: dict[str, Any]) -> dict[str, Any]:
        limit = min(max(int(arguments.get("limit", 50)), 1), 100)
        query = select(Foreshadow).where(Foreshadow.project_id == self.project.id)
        if arguments.get("status"):
            query = query.where(Foreshadow.status == arguments["status"])
        rows = (await self.db.execute(query.order_by(Foreshadow.created_at).limit(limit))).scalars().all()
        return {"total": len(rows), "items": [
            {"id": row.id, "title": row.title, "content": row.content,
             "status": row.status, "importance": row.importance,
             "target_resolve_chapter_number": row.target_resolve_chapter_number}
            for row in rows
        ]}
