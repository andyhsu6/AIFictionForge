"""大纲导入导出相关模型。"""
from datetime import datetime
from typing import Literal, Optional

from pydantic import BaseModel, Field


OutlineImportMode = Literal["append", "merge"]


class OutlineExportRequest(BaseModel):
    """导出大纲请求。outline_ids 为空时导出项目全部大纲。"""

    project_id: str = Field(..., min_length=1)
    outline_ids: Optional[list[str]] = None


class OutlineTransferItem(BaseModel):
    """可跨项目传输的大纲数据，不包含数据库标识。"""

    order_index: int = Field(..., ge=1)
    title: str = Field(..., min_length=1, max_length=200)
    content: str = ""
    structure: Optional[str] = None


class OutlineSourceProject(BaseModel):
    """导出源项目信息，仅用于导入提示。"""

    title: str
    outline_mode: Literal["one-to-one", "one-to-many"]


class OutlineExportDocument(BaseModel):
    """大纲专用导出文件。version 是文件格式版本，不是应用版本。"""

    version: str = "1.0.0"
    export_type: str = "outlines"
    export_time: datetime
    source_project: OutlineSourceProject
    count: int
    data: list[OutlineTransferItem]


class OutlineImportStatistics(BaseModel):
    total: int = 0
    will_create: int = 0
    will_update: int = 0
    will_create_chapters: int = 0


class OutlineImportPreviewResponse(BaseModel):
    valid: bool
    version: str = ""
    source_type: Literal["outlines", "project", "unknown"] = "unknown"
    source_project: Optional[OutlineSourceProject] = None
    target_outline_mode: Optional[Literal["one-to-one", "one-to-many"]] = None
    mode: OutlineImportMode
    statistics: OutlineImportStatistics = Field(default_factory=OutlineImportStatistics)
    errors: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)


class OutlineImportDetail(BaseModel):
    source_order_index: int
    target_order_index: int
    title: str
    action: Literal["created", "updated"]


class OutlineImportResult(BaseModel):
    success: bool
    message: str
    mode: OutlineImportMode
    imported: int
    updated: int
    created_chapters: int
    details: list[OutlineImportDetail] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
