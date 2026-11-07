"""项目文笔画像锚点"""
from sqlalchemy import Column, String, Integer, DateTime, ForeignKey, Index
from sqlalchemy.sql import func
from app.database import Base


class ProjectStyleAnchor(Base):
    """本书原文语感锚点（issue #254 P1）。

    只存章节定位与哈希，不复制原文；生成时按偏移从 `Chapter.content`
    取片段，哈希不符（章节被编辑/删除）则跳过。
    """

    __tablename__ = "project_style_anchors"

    id = Column(Integer, primary_key=True, autoincrement=True)
    project_id = Column(
        String(36),
        ForeignKey("projects.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
        comment="项目ID",
    )
    chapter_id = Column(
        String(36),
        ForeignKey("chapters.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
        comment="章节ID",
    )
    start_offset = Column(Integer, nullable=False, comment="片段起止偏移（字符）")
    end_offset = Column(Integer, nullable=False, comment="片段结束偏移（字符）")
    text_hash = Column(String(64), nullable=False, comment="片段内容的 SHA-256")
    order_index = Column(Integer, default=0, server_default="0", nullable=False, comment="选择顺序")

    created_at = Column(DateTime, server_default=func.now(), comment="创建时间")
    updated_at = Column(DateTime, server_default=func.now(), onupdate=func.now(), comment="更新时间")

    __table_args__ = (
        Index("ix_project_style_anchors_project_order", "project_id", "order_index"),
    )

    def __repr__(self) -> str:
        return f"<ProjectStyleAnchor(project={self.project_id}, chapter={self.chapter_id})>"
