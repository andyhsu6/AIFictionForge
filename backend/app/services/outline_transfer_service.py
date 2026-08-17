"""大纲导入导出服务。"""
from dataclasses import dataclass, field
from datetime import datetime, timezone
import json
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.chapter import Chapter
from app.models.outline import Outline
from app.models.project import Project
from app.schemas.outline_transfer import (
    OutlineExportDocument,
    OutlineImportDetail,
    OutlineImportMode,
    OutlineImportPreviewResponse,
    OutlineImportResult,
    OutlineImportStatistics,
    OutlineSourceProject,
    OutlineTransferItem,
)


@dataclass
class ParsedOutlineDocument:
    version: str = ""
    source_type: str = "unknown"
    source_project: OutlineSourceProject | None = None
    items: list[OutlineTransferItem] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


class OutlineTransferService:
    """提供大纲文件解析、预览以及原子导入。"""

    OUTLINE_FORMAT_VERSION = "1.0.0"
    PROJECT_FORMAT_VERSIONS = {"1.0.0", "1.1.0"}
    MAX_ITEMS = 5000

    @classmethod
    async def export_outlines(
        cls,
        project: Project,
        outline_ids: list[str] | None,
        db: AsyncSession,
    ) -> OutlineExportDocument:
        query = select(Outline).where(Outline.project_id == project.id)

        requested_ids: list[str] | None = None
        if outline_ids is not None:
            requested_ids = list(dict.fromkeys(outline_ids))
            if not requested_ids:
                raise ValueError("请至少选择一个大纲")
            query = query.where(Outline.id.in_(requested_ids))

        result = await db.execute(query.order_by(Outline.order_index, Outline.created_at))
        outlines = result.scalars().all()

        if requested_ids is not None and len(outlines) != len(requested_ids):
            raise ValueError("部分大纲不存在或不属于当前项目")

        items = [
            OutlineTransferItem(
                order_index=outline.order_index,
                title=outline.title,
                content=outline.content or "",
                structure=outline.structure,
            )
            for outline in outlines
        ]

        return OutlineExportDocument(
            version=cls.OUTLINE_FORMAT_VERSION,
            export_time=datetime.now(timezone.utc),
            source_project=OutlineSourceProject(
                title=project.title,
                outline_mode=project.outline_mode,
            ),
            count=len(items),
            data=items,
        )

    @classmethod
    def parse_file(cls, content: bytes) -> ParsedOutlineDocument:
        parsed = ParsedOutlineDocument()
        try:
            raw = json.loads(content.decode("utf-8-sig"))
        except UnicodeDecodeError:
            parsed.errors.append("文件必须使用 UTF-8 编码")
            return parsed
        except json.JSONDecodeError as exc:
            parsed.errors.append(f"JSON 格式错误：第 {exc.lineno} 行第 {exc.colno} 列")
            return parsed

        if not isinstance(raw, dict):
            parsed.errors.append("文件根节点必须是 JSON 对象")
            return parsed

        raw_items: Any = None
        declared_count: Any = None
        export_type = raw.get("export_type")

        if export_type == "outlines":
            parsed.source_type = "outlines"
            parsed.version = str(raw.get("version") or "")
            raw_items = raw.get("data")
            declared_count = raw.get("count")
            parsed.source_project = cls._parse_source_project(raw.get("source_project"))
            if parsed.version != cls.OUTLINE_FORMAT_VERSION:
                parsed.errors.append(
                    f"不支持的大纲文件版本：{parsed.version or '缺失'}，"
                    f"当前支持 {cls.OUTLINE_FORMAT_VERSION}"
                )
        elif isinstance(raw.get("outlines"), list) and isinstance(raw.get("project"), dict):
            parsed.source_type = "project"
            parsed.version = str(raw.get("version") or "")
            raw_items = raw.get("outlines")
            declared_count = len(raw_items)
            project_data = raw["project"]
            parsed.source_project = cls._parse_source_project(
                {
                    "title": project_data.get("title", "未知项目"),
                    "outline_mode": project_data.get("outline_mode", "one-to-many"),
                }
            )
            parsed.warnings.append("检测到完整项目导出文件，本次仅导入其中的大纲数据")
            if parsed.version not in cls.PROJECT_FORMAT_VERSIONS:
                parsed.errors.append(
                    f"不支持的项目文件版本：{parsed.version or '缺失'}"
                )
        else:
            parsed.errors.append("不是有效的大纲导出文件或完整项目导出文件")
            return parsed

        if not isinstance(raw_items, list):
            parsed.errors.append("data/outlines 字段必须是数组")
            return parsed
        if len(raw_items) > cls.MAX_ITEMS:
            parsed.errors.append(f"大纲数量不能超过 {cls.MAX_ITEMS} 条")
            return parsed
        if declared_count is not None and declared_count != len(raw_items):
            parsed.warnings.append(
                f"文件声明数量为 {declared_count}，实际包含 {len(raw_items)} 条"
            )

        seen_orders: set[int] = set()
        for index, raw_item in enumerate(raw_items, start=1):
            item, item_errors = cls._parse_item(raw_item, index)
            parsed.errors.extend(item_errors)
            if item is None:
                continue
            if item.order_index in seen_orders:
                parsed.errors.append(f"第 {index} 条大纲的序号 {item.order_index} 重复")
                continue
            seen_orders.add(item.order_index)
            parsed.items.append(item)

        parsed.items.sort(key=lambda item: item.order_index)
        if not parsed.items and not parsed.errors:
            parsed.errors.append("文件中没有可导入的大纲")
        return parsed

    @staticmethod
    def _parse_source_project(raw: Any) -> OutlineSourceProject | None:
        if not isinstance(raw, dict):
            return None
        mode = raw.get("outline_mode")
        if mode not in {"one-to-one", "one-to-many"}:
            return None
        return OutlineSourceProject(
            title=str(raw.get("title") or "未知项目"),
            outline_mode=mode,
        )

    @staticmethod
    def _parse_item(raw: Any, index: int) -> tuple[OutlineTransferItem | None, list[str]]:
        errors: list[str] = []
        if not isinstance(raw, dict):
            return None, [f"第 {index} 条大纲必须是对象"]

        order_index = raw.get("order_index")
        if isinstance(order_index, bool) or not isinstance(order_index, int) or order_index < 1:
            errors.append(f"第 {index} 条大纲的 order_index 必须是正整数")

        title = raw.get("title")
        if not isinstance(title, str) or not title.strip():
            errors.append(f"第 {index} 条大纲缺少有效标题")
        elif len(title.strip()) > 200:
            errors.append(f"第 {index} 条大纲标题不能超过 200 个字符")

        content = raw.get("content", "")
        if content is None:
            content = ""
        elif not isinstance(content, str):
            errors.append(f"第 {index} 条大纲的 content 必须是字符串")

        structure = raw.get("structure")
        if isinstance(structure, (dict, list)):
            structure = json.dumps(structure, ensure_ascii=False)
        elif structure is not None and not isinstance(structure, str):
            errors.append(f"第 {index} 条大纲的 structure 必须是字符串、对象或 null")

        if errors:
            return None, errors
        return (
            OutlineTransferItem(
                order_index=order_index,
                title=title.strip(),
                content=content,
                structure=structure,
            ),
            [],
        )

    @staticmethod
    def _synchronize_structure(
        structure: str | None,
        title: str,
        content: str,
    ) -> str | None:
        """同步 structure 中用于展示和生成上下文的重复字段。"""
        if not structure:
            structure_data: Any = {}
        else:
            try:
                structure_data = json.loads(structure)
            except json.JSONDecodeError:
                # 保留非 JSON 的历史数据，列表接口仍会使用顶层字段正确展示。
                return structure

        if not isinstance(structure_data, dict):
            return structure

        structure_data["title"] = title
        structure_data["summary"] = content
        structure_data["content"] = content
        return json.dumps(structure_data, ensure_ascii=False)

    @classmethod
    async def preview_import(
        cls,
        parsed: ParsedOutlineDocument,
        project: Project,
        mode: OutlineImportMode,
        db: AsyncSession,
    ) -> OutlineImportPreviewResponse:
        errors = list(parsed.errors)
        warnings = list(parsed.warnings)

        if parsed.source_project and parsed.source_project.outline_mode != project.outline_mode:
            warnings.append(
                "源项目大纲模式与目标项目不同，导入时将按目标项目的模式处理章节联动"
            )

        result = await db.execute(
            select(Outline).where(Outline.project_id == project.id)
        )
        existing_outlines = result.scalars().all()
        existing_by_order = {outline.order_index: outline for outline in existing_outlines}

        will_create = len(parsed.items)
        will_update = 0
        will_create_chapters = 0

        if mode == "merge":
            will_update = sum(1 for item in parsed.items if item.order_index in existing_by_order)
            will_create = len(parsed.items) - will_update

        if project.outline_mode == "one-to-one" and parsed.items:
            chapter_result = await db.execute(
                select(Chapter).where(Chapter.project_id == project.id)
            )
            chapters = chapter_result.scalars().all()
            chapters_by_number = {chapter.chapter_number: chapter for chapter in chapters}

            if mode == "append":
                will_create_chapters = len(parsed.items)
            else:
                for item in parsed.items:
                    existing_outline = existing_by_order.get(item.order_index)
                    chapter = chapters_by_number.get(item.order_index)
                    if existing_outline is None and chapter is not None:
                        errors.append(
                            f"序号 {item.order_index} 已存在章节但没有对应大纲，无法按序号合并"
                        )
                    elif chapter is None:
                        will_create_chapters += 1

        if mode == "append" and parsed.items:
            warnings.append("追加模式会保留文件内相对顺序，并从当前末尾重新编号")

        return OutlineImportPreviewResponse(
            valid=not errors,
            version=parsed.version,
            source_type=parsed.source_type,
            source_project=parsed.source_project,
            target_outline_mode=project.outline_mode,
            mode=mode,
            statistics=OutlineImportStatistics(
                total=len(parsed.items),
                will_create=will_create,
                will_update=will_update,
                will_create_chapters=will_create_chapters,
            ),
            errors=errors,
            warnings=warnings,
        )

    @classmethod
    async def import_outlines(
        cls,
        parsed: ParsedOutlineDocument,
        project: Project,
        mode: OutlineImportMode,
        db: AsyncSession,
    ) -> OutlineImportResult:
        preview = await cls.preview_import(parsed, project, mode, db)
        if not preview.valid:
            raise ValueError("；".join(preview.errors))

        details: list[OutlineImportDetail] = []
        imported = 0
        updated = 0
        created_chapters = 0

        try:
            outline_result = await db.execute(
                select(Outline).where(Outline.project_id == project.id)
            )
            existing_outlines = outline_result.scalars().all()
            existing_by_order = {outline.order_index: outline for outline in existing_outlines}

            chapter_result = await db.execute(
                select(Chapter).where(Chapter.project_id == project.id)
            )
            chapters = chapter_result.scalars().all()
            chapters_by_number = {chapter.chapter_number: chapter for chapter in chapters}

            if mode == "append":
                max_outline_order = max(
                    (order for order in existing_by_order if isinstance(order, int)),
                    default=0,
                )
                max_chapter_order = max(chapters_by_number, default=0) if project.outline_mode == "one-to-one" else 0
                next_order = max(max_outline_order, max_chapter_order) + 1

                for offset, item in enumerate(parsed.items):
                    target_order = next_order + offset
                    synchronized_structure = cls._synchronize_structure(
                        item.structure,
                        item.title,
                        item.content,
                    )
                    outline = Outline(
                        project_id=project.id,
                        title=item.title,
                        content=item.content,
                        structure=synchronized_structure,
                        order_index=target_order,
                    )
                    db.add(outline)
                    await db.flush()

                    if project.outline_mode == "one-to-one":
                        db.add(cls._new_chapter(project.id, outline, target_order))
                        created_chapters += 1

                    imported += 1
                    details.append(
                        OutlineImportDetail(
                            source_order_index=item.order_index,
                            target_order_index=target_order,
                            title=item.title,
                            action="created",
                        )
                    )
            else:
                for item in parsed.items:
                    outline = existing_by_order.get(item.order_index)
                    action = "updated"
                    synchronized_structure = cls._synchronize_structure(
                        item.structure,
                        item.title,
                        item.content,
                    )
                    if outline is None:
                        outline = Outline(
                            project_id=project.id,
                            title=item.title,
                            content=item.content,
                            structure=synchronized_structure,
                            order_index=item.order_index,
                        )
                        db.add(outline)
                        await db.flush()
                        existing_by_order[item.order_index] = outline
                        imported += 1
                        action = "created"
                    else:
                        outline.title = item.title
                        outline.content = item.content
                        outline.structure = synchronized_structure
                        updated += 1

                    if project.outline_mode == "one-to-one":
                        chapter = chapters_by_number.get(item.order_index)
                        if chapter is None:
                            chapter = cls._new_chapter(project.id, outline, item.order_index)
                            db.add(chapter)
                            chapters_by_number[item.order_index] = chapter
                            created_chapters += 1
                        else:
                            chapter.outline_id = outline.id
                            chapter.title = item.title
                            chapter.summary = item.content

                    details.append(
                        OutlineImportDetail(
                            source_order_index=item.order_index,
                            target_order_index=item.order_index,
                            title=item.title,
                            action=action,
                        )
                    )

            await db.commit()
        except Exception:
            await db.rollback()
            raise

        return OutlineImportResult(
            success=True,
            message=f"导入完成：新增 {imported} 条，更新 {updated} 条",
            mode=mode,
            imported=imported,
            updated=updated,
            created_chapters=created_chapters,
            details=details,
            warnings=preview.warnings,
        )

    @staticmethod
    def _new_chapter(project_id: str, outline: Outline, chapter_number: int) -> Chapter:
        return Chapter(
            project_id=project_id,
            chapter_number=chapter_number,
            title=outline.title,
            content="",
            summary=outline.content,
            word_count=0,
            status="pending",
            outline_id=outline.id,
            sub_index=1,
        )
