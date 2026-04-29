"""伏笔管理服务 - 处理伏笔的CRUD和业务逻辑"""
from typing import List, Dict, Any, Optional
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, and_, or_, desc, func, delete, update
from datetime import datetime
import uuid
import hashlib

from app.models.foreshadow import Foreshadow
from app.models.chapter import Chapter
from app.models.memory import PlotAnalysis, StoryMemory
from app.models.project import Project
from app.services.memory_service import memory_service
from app.schemas.foreshadow import (
    ForeshadowCreate, ForeshadowUpdate,
    PlantForeshadowRequest, ResolveForeshadowRequest,
    SyncFromAnalysisRequest
)
from app.logger import get_logger

logger = get_logger(__name__)


def generate_stable_foreshadow_id(chapter_id: str, content: str, foreshadow_type: str = "planted") -> str:
    """
    生成稳定的伏笔唯一标识符
    
    使用 chapter_id + content_hash 的方式，确保：
    1. 同一章节、相同内容的伏笔只有一个唯一ID
    2. 重新分析同一章节不会产生新ID
    3. 标识符足够短且可读
    
    Args:
        chapter_id: 章节ID
        content: 伏笔内容
        foreshadow_type: 伏笔类型（planted/resolved）
    
    Returns:
        稳定的唯一标识符，格式：{type}_{chapter_id_hash}_{content_hash}
    """
    # 生成内容哈希（取前12位，足够区分）
    content_normalized = content.strip().lower()
    content_hash = hashlib.md5(content_normalized.encode('utf-8')).hexdigest()[:12]
    
    # 生成章节ID哈希（取前8位）
    chapter_hash = hashlib.md5(chapter_id.encode('utf-8')).hexdigest()[:8]
    
    return f"{foreshadow_type}_{chapter_hash}_{content_hash}"


class ForeshadowService:
    """伏笔管理服务"""
    
    async def get_project_foreshadows(
        self,
        db: AsyncSession,
        project_id: str,
        status: Optional[str] = None,
        category: Optional[str] = None,
        source_type: Optional[str] = None,
        is_long_term: Optional[bool] = None,
        page: int = 1,
        limit: int = 50
    ) -> Dict[str, Any]:
        """
        获取项目伏笔列表
        
        Args:
            db: 数据库会话
            project_id: 项目ID
            status: 状态筛选
            category: 分类筛选
            source_type: 来源筛选
            is_long_term: 是否长线伏笔
            page: 页码
            limit: 每页数量
        
        Returns:
            包含列表和统计的字典
        """
        try:
            # 构建查询条件
            conditions = [Foreshadow.project_id == project_id]
            
            if status:
                conditions.append(Foreshadow.status == status)
            if category:
                conditions.append(Foreshadow.category == category)
            if source_type:
                conditions.append(Foreshadow.source_type == source_type)
            if is_long_term is not None:
                conditions.append(Foreshadow.is_long_term == is_long_term)
            
            # 查询总数
            count_query = select(func.count(Foreshadow.id)).where(and_(*conditions))
            total_result = await db.execute(count_query)
            total = total_result.scalar() or 0
            
            # 查询列表
            query = (
                select(Foreshadow)
                .where(and_(*conditions))
                .order_by(
                    Foreshadow.plant_chapter_number.asc().nulls_last(),
                    desc(Foreshadow.importance),
                    desc(Foreshadow.created_at)
                )
                .offset((page - 1) * limit)
                .limit(limit)
            )
            
            result = await db.execute(query)
            foreshadows = result.scalars().all()
            
            # 获取统计
            stats = await self.get_stats(db, project_id)
            
            return {
                "total": total,
                "items": [f.to_dict() for f in foreshadows],
                "stats": stats
            }
            
        except Exception as e:
            logger.error(f"❌ 获取伏笔列表失败: {str(e)}")
            raise
    
    async def get_foreshadow(
        self,
        db: AsyncSession,
        foreshadow_id: str
    ) -> Optional[Foreshadow]:
        """获取单个伏笔"""
        result = await db.execute(
            select(Foreshadow).where(Foreshadow.id == foreshadow_id)
        )
        return result.scalar_one_or_none()
    
    async def create_foreshadow(
        self,
        db: AsyncSession,
        data: ForeshadowCreate
    ) -> Foreshadow:
        """
        创建伏笔
        
        Args:
            db: 数据库会话
            data: 创建数据
        
        Returns:
            创建的伏笔对象
        """
        try:
            foreshadow = Foreshadow(
                id=str(uuid.uuid4()),
                project_id=data.project_id,
                title=data.title,
                content=data.content,
                hint_text=data.hint_text,
                resolution_text=data.resolution_text,
                source_type="manual",
                plant_chapter_number=data.plant_chapter_number,
                target_resolve_chapter_number=data.target_resolve_chapter_number,
                status="pending",
                is_long_term=data.is_long_term,
                importance=data.importance,
                strength=data.strength,
                subtlety=data.subtlety,
                urgency=0,
                related_characters=data.related_characters,
                tags=data.tags,
                category=data.category,
                notes=data.notes,
                resolution_notes=data.resolution_notes,
                auto_remind=data.auto_remind,
                remind_before_chapters=data.remind_before_chapters,
                include_in_context=data.include_in_context
            )
            
            db.add(foreshadow)
            await db.commit()
            await db.refresh(foreshadow)
            
            logger.info(f"✅ 创建伏笔成功: {foreshadow.title}")
            return foreshadow
            
        except Exception as e:
            await db.rollback()
            logger.error(f"❌ 创建伏笔失败: {str(e)}")
            raise
    
    async def update_foreshadow(
        self,
        db: AsyncSession,
        foreshadow_id: str,
        data: ForeshadowUpdate
    ) -> Optional[Foreshadow]:
        """
        更新伏笔
        
        Args:
            db: 数据库会话
            foreshadow_id: 伏笔ID
            data: 更新数据
        
        Returns:
            更新后的伏笔对象
        """
        try:
            foreshadow = await self.get_foreshadow(db, foreshadow_id)
            if not foreshadow:
                return None
            
            # 更新字段
            update_data = data.model_dump(exclude_unset=True)
            for key, value in update_data.items():
                if hasattr(foreshadow, key):
                    setattr(foreshadow, key, value)
            
            await db.commit()
            await db.refresh(foreshadow)
            
            logger.info(f"✅ 更新伏笔成功: {foreshadow.title}")
            return foreshadow
            
        except Exception as e:
            await db.rollback()
            logger.error(f"❌ 更新伏笔失败: {str(e)}")
            raise
    
    async def delete_foreshadow(
        self,
        db: AsyncSession,
        foreshadow_id: str
    ) -> bool:
        """删除伏笔，并同步清理关联记忆数据和历史分析引用"""
        try:
            foreshadow = await self.get_foreshadow(db, foreshadow_id)
            if not foreshadow:
                return False

            project_result = await db.execute(
                select(Project).where(Project.id == foreshadow.project_id)
            )
            project = project_result.scalar_one_or_none()

            deleted_memory_rows = 0
            deleted_vector_memories = 0
            cleaned_analysis_refs = 0
            cleaned_analysis_rows = 0
            foreshadow_keywords = []
            content_snippet = (foreshadow.content or "")[:50].strip()

            if foreshadow.title and foreshadow.title.strip():
                foreshadow_keywords.append(foreshadow.title.strip())

            if content_snippet:
                foreshadow_keywords.append(content_snippet)

            memory_conditions = [
                StoryMemory.project_id == foreshadow.project_id,
                StoryMemory.memory_type == "foreshadow"
            ]
            keyword_conditions = []
            for keyword in foreshadow_keywords:
                keyword_conditions.append(StoryMemory.content.contains(keyword))
                keyword_conditions.append(StoryMemory.title.contains(keyword))

            if keyword_conditions:
                delete_memory_query = delete(StoryMemory).where(
                    and_(*memory_conditions, or_(*keyword_conditions))
                )
                delete_memory_result = await db.execute(delete_memory_query)
                deleted_memory_rows = delete_memory_result.rowcount or 0

            if project and project.user_id and foreshadow_keywords:
                deleted_vector_memories = await memory_service.delete_foreshadow_memories(
                    user_id=project.user_id,
                    project_id=foreshadow.project_id,
                    foreshadow_keywords=foreshadow_keywords
                )

            analysis_result = await db.execute(
                select(PlotAnalysis).where(PlotAnalysis.project_id == foreshadow.project_id)
            )
            project_analyses = analysis_result.scalars().all()

            for analysis in project_analyses:
                analysis_foreshadows = analysis.foreshadows or []
                if not analysis_foreshadows:
                    continue

                original_count = len(analysis_foreshadows)
                filtered_foreshadows = []
                removed_count = 0

                for item in analysis_foreshadows:
                    if not isinstance(item, dict):
                        filtered_foreshadows.append(item)
                        continue

                    should_remove = False

                    # 1. 清理历史回收引用
                    if item.get("reference_foreshadow_id") == foreshadow_id:
                        should_remove = True

                    # 2. 清理历史埋入记录，避免“手动同步分析伏笔”再次从 PlotAnalysis 重建已删除伏笔
                    if not should_remove and foreshadow.source_type == "analysis":
                        item_type = item.get("type")
                        item_content = (item.get("content") or "").strip()
                        item_title = (item.get("title") or "").strip()
                        item_source_memory_id = None

                        if item_type == "planted" and item_content and analysis.chapter_id == foreshadow.plant_chapter_id:
                            item_source_memory_id = generate_stable_foreshadow_id(
                                analysis.chapter_id,
                                item_content,
                                item_type
                            )

                            if foreshadow.source_memory_id and item_source_memory_id == foreshadow.source_memory_id:
                                should_remove = True
                            elif (
                                item_title
                                and foreshadow.title
                                and item_title == foreshadow.title.strip()
                                and content_snippet
                                and content_snippet in item_content
                            ):
                                should_remove = True

                    if should_remove:
                        removed_count += 1
                        continue

                    filtered_foreshadows.append(item)

                if removed_count > 0:
                    analysis.foreshadows = filtered_foreshadows
                    analysis.foreshadows_planted = sum(
                        1 for f in filtered_foreshadows
                        if isinstance(f, dict) and f.get('type') == 'planted'
                    )
                    analysis.foreshadows_resolved = sum(
                        1 for f in filtered_foreshadows
                        if isinstance(f, dict) and f.get('type') == 'resolved'
                    )
                    cleaned_analysis_refs += removed_count
                    cleaned_analysis_rows += 1
                    logger.info(
                        f"🧹 已清理章节分析 {analysis.chapter_id[:8]} 中 {removed_count} 条已删除伏笔历史记录 "
                        f"(原数量: {original_count}, 现数量: {len(filtered_foreshadows)})"
                    )

            await db.delete(foreshadow)
            await db.commit()

            logger.info(
                f"✅ 删除伏笔成功: {foreshadow.title} "
                f"(关系记忆清理: {deleted_memory_rows}条, 向量记忆清理: {deleted_vector_memories}条, "
                f"历史分析引用清理: {cleaned_analysis_refs}条/{cleaned_analysis_rows}个分析)"
            )
            return True

        except Exception as e:
            await db.rollback()
            logger.error(f"❌ 删除伏笔失败: {str(e)}")
            raise
    
    async def mark_as_planted(
        self,
        db: AsyncSession,
        foreshadow_id: str,
        data: PlantForeshadowRequest
    ) -> Optional[Foreshadow]:
        """
        标记伏笔为已埋入
        
        Args:
            db: 数据库会话
            foreshadow_id: 伏笔ID
            data: 埋入信息
        
        Returns:
            更新后的伏笔对象
        """
        try:
            foreshadow = await self.get_foreshadow(db, foreshadow_id)
            if not foreshadow:
                return None
            
            foreshadow.status = "planted"
            foreshadow.plant_chapter_id = data.chapter_id
            foreshadow.plant_chapter_number = data.chapter_number
            foreshadow.planted_at = datetime.now()
            
            if data.hint_text:
                foreshadow.hint_text = data.hint_text
            
            await db.commit()
            await db.refresh(foreshadow)
            
            logger.info(f"✅ 伏笔已标记为埋入: {foreshadow.title} (第{data.chapter_number}章)")
            return foreshadow
            
        except Exception as e:
            await db.rollback()
            logger.error(f"❌ 标记伏笔埋入失败: {str(e)}")
            raise
    
    async def mark_as_resolved(
        self,
        db: AsyncSession,
        foreshadow_id: str,
        data: ResolveForeshadowRequest
    ) -> Optional[Foreshadow]:
        """
        标记伏笔为已回收
        
        Args:
            db: 数据库会话
            foreshadow_id: 伏笔ID
            data: 回收信息
        
        Returns:
            更新后的伏笔对象
        """
        try:
            foreshadow = await self.get_foreshadow(db, foreshadow_id)
            if not foreshadow:
                return None
            
            if data.is_partial:
                foreshadow.status = "partially_resolved"
            else:
                foreshadow.status = "resolved"
            
            foreshadow.actual_resolve_chapter_id = data.chapter_id
            foreshadow.actual_resolve_chapter_number = data.chapter_number
            foreshadow.resolved_at = datetime.now()
            
            if data.resolution_text:
                foreshadow.resolution_text = data.resolution_text
            
            await db.commit()
            await db.refresh(foreshadow)
            
            logger.info(f"✅ 伏笔已标记为回收: {foreshadow.title} (第{data.chapter_number}章)")
            return foreshadow
            
        except Exception as e:
            await db.rollback()
            logger.error(f"❌ 标记伏笔回收失败: {str(e)}")
            raise
    
    async def mark_as_abandoned(
        self,
        db: AsyncSession,
        foreshadow_id: str,
        reason: Optional[str] = None
    ) -> Optional[Foreshadow]:
        """标记伏笔为已废弃"""
        try:
            foreshadow = await self.get_foreshadow(db, foreshadow_id)
            if not foreshadow:
                return None
            
            foreshadow.status = "abandoned"
            if reason:
                foreshadow.notes = f"{foreshadow.notes or ''}\n[废弃原因] {reason}".strip()
            
            await db.commit()
            await db.refresh(foreshadow)
            
            logger.info(f"✅ 伏笔已标记为废弃: {foreshadow.title}")
            return foreshadow
            
        except Exception as e:
            await db.rollback()
            logger.error(f"❌ 标记伏笔废弃失败: {str(e)}")
            raise
    
    async def sync_from_analysis(
        self,
        db: AsyncSession,
        project_id: str,
        data: SyncFromAnalysisRequest
    ) -> Dict[str, Any]:
        """
        从章节分析结果同步伏笔（重构版）
        
        统一复用 auto_update_from_analysis 的核心逻辑，避免重复代码。
        本方法仅负责从 PlotAnalysis 表读取数据，然后委托处理。
        
        Args:
            db: 数据库会话
            project_id: 项目ID
            data: 同步请求数据
        
        Returns:
            同步结果
        """
        try:
            total_stats = {
                "synced_count": 0,
                "skipped_count": 0,
                "resolved_count": 0,
                "new_foreshadows": [],
                "skipped_reasons": []
            }
            
            # 获取分析结果
            query = select(PlotAnalysis).where(PlotAnalysis.project_id == project_id)
            if data.chapter_ids:
                query = query.where(PlotAnalysis.chapter_id.in_(data.chapter_ids))
            
            result = await db.execute(query)
            analyses = result.scalars().all()
            
            for analysis in analyses:
                if not analysis.foreshadows:
                    continue
                
                # 获取章节信息
                chapter_result = await db.execute(
                    select(Chapter).where(Chapter.id == analysis.chapter_id)
                )
                chapter = chapter_result.scalar_one_or_none()
                if not chapter:
                    continue
                
                # 委托给统一的处理方法
                chapter_stats = await self.auto_update_from_analysis(
                    db=db,
                    project_id=project_id,
                    chapter_id=chapter.id,
                    chapter_number=chapter.chapter_number,
                    analysis_foreshadows=analysis.foreshadows
                )
                
                # 汇总统计
                total_stats["synced_count"] += chapter_stats.get("planted_count", 0) + chapter_stats.get("resolved_count", 0)
                total_stats["resolved_count"] += chapter_stats.get("resolved_count", 0)
                total_stats["skipped_count"] += chapter_stats.get("skipped_resolve_count", 0)
            
            logger.info(f"✅ 伏笔同步完成: 同步{total_stats['synced_count']}个（其中回收{total_stats['resolved_count']}个）, 跳过{total_stats['skipped_count']}个")
            
            return total_stats
            
        except Exception as e:
            await db.rollback()
            logger.error(f"❌ 同步伏笔失败: {str(e)}")
            raise
    
    async def get_pending_resolve_foreshadows(
        self,
        db: AsyncSession,
        project_id: str,
        current_chapter: int,
        lookahead: int = 5
    ) -> List[Foreshadow]:
        """
        获取即将需要回收的伏笔
        
        Args:
            db: 数据库会话
            project_id: 项目ID
            current_chapter: 当前章节号
            lookahead: 向前看几章
        
        Returns:
            待回收伏笔列表
        """
        try:
            # 查询已埋入且计划在接下来几章回收的伏笔
            query = (
                select(Foreshadow)
                .where(
                    and_(
                        Foreshadow.project_id == project_id,
                        Foreshadow.status == "planted",
                        Foreshadow.target_resolve_chapter_number != None,
                        Foreshadow.target_resolve_chapter_number <= current_chapter + lookahead,
                        Foreshadow.auto_remind == True
                    )
                )
                .order_by(Foreshadow.target_resolve_chapter_number)
            )
            
            result = await db.execute(query)
            return list(result.scalars().all())
            
        except Exception as e:
            logger.error(f"❌ 获取待回收伏笔失败: {str(e)}")
            return []
    
    async def get_overdue_foreshadows(
        self,
        db: AsyncSession,
        project_id: str,
        current_chapter: int
    ) -> List[Foreshadow]:
        """
        获取超期未回收的伏笔
        
        Args:
            db: 数据库会话
            project_id: 项目ID
            current_chapter: 当前章节号
        
        Returns:
            超期伏笔列表
        """
        try:
            query = (
                select(Foreshadow)
                .where(
                    and_(
                        Foreshadow.project_id == project_id,
                        Foreshadow.status == "planted",
                        Foreshadow.target_resolve_chapter_number != None,
                        Foreshadow.target_resolve_chapter_number < current_chapter
                    )
                )
                .order_by(Foreshadow.target_resolve_chapter_number)
            )
            
            result = await db.execute(query)
            return list(result.scalars().all())
            
        except Exception as e:
            logger.error(f"❌ 获取超期伏笔失败: {str(e)}")
            return []
    
    async def get_must_resolve_foreshadows(
        self,
        db: AsyncSession,
        project_id: str,
        chapter_number: int
    ) -> List[Foreshadow]:
        """
        获取本章必须回收的伏笔（target_resolve_chapter_number == chapter_number）
        
        这些伏笔是用户明确指定在本章回收的，必须在本章完成回收
        
        Args:
            db: 数据库会话
            project_id: 项目ID
            chapter_number: 当前章节号
        
        Returns:
            必须回收的伏笔列表
        """
        try:
            query = (
                select(Foreshadow)
                .where(
                    and_(
                        Foreshadow.project_id == project_id,
                        Foreshadow.status == "planted",
                        Foreshadow.target_resolve_chapter_number == chapter_number
                    )
                )
                .order_by(desc(Foreshadow.importance))
            )
            
            result = await db.execute(query)
            return list(result.scalars().all())
            
        except Exception as e:
            logger.error(f"❌ 获取本章必须回收伏笔失败: {str(e)}")
            return []
    
    async def get_foreshadows_to_plant(
        self,
        db: AsyncSession,
        project_id: str,
        chapter_number: int
    ) -> List[Foreshadow]:
        """
        获取计划在本章埋入的伏笔
        
        Args:
            db: 数据库会话
            project_id: 项目ID
            chapter_number: 章节号
        
        Returns:
            待埋入伏笔列表
        """
        try:
            query = (
                select(Foreshadow)
                .where(
                    and_(
                        Foreshadow.project_id == project_id,
                        Foreshadow.status == "pending",
                        Foreshadow.plant_chapter_number == chapter_number
                    )
                )
                .order_by(desc(Foreshadow.importance))
            )
            
            result = await db.execute(query)
            return list(result.scalars().all())
            
        except Exception as e:
            logger.error(f"❌ 获取待埋入伏笔失败: {str(e)}")
            return []
    
    async def build_chapter_context(
        self,
        db: AsyncSession,
        project_id: str,
        chapter_number: int,
        include_pending: bool = True,
        include_overdue: bool = True,
        lookahead: int = 5
    ) -> Dict[str, Any]:
        """
        构建章节生成的伏笔上下文（智能分层提醒策略）
        
        核心策略：
        1. 本章必须回收的伏笔 → 明确要求回收
        2. 超期伏笔 → 强调需要尽快回收
        3. 即将回收的伏笔 → 仅作为背景信息，明确禁止提前回收
        4. 远期伏笔 → 不发送，防止干扰
        
        Args:
            db: 数据库会话
            project_id: 项目ID
            chapter_number: 章节号
            include_pending: 包含待埋入伏笔
            include_overdue: 包含超期伏笔
            lookahead: 向前看几章（用于背景提醒，非强制回收）
        
        Returns:
            伏笔上下文信息
        """
        try:
            lines = []
            to_plant = []
            must_resolve = []  # 本章必须回收
            overdue = []       # 超期待回收
            upcoming = []      # 即将回收（仅参考）
            
            # 1. 获取本章必须回收的伏笔（target_resolve_chapter_number == chapter_number）
            must_resolve = await self.get_must_resolve_foreshadows(db, project_id, chapter_number)
            if must_resolve:
                lines.append("【🎯 本章必须回收的伏笔 - 请务必在本章完成回收】")
                for f in must_resolve:
                    lines.append(f"- ID:{f.id[:8]} | {f.title}")
                    lines.append(f"  埋入章节：第{f.plant_chapter_number}章")
                    lines.append(f"  伏笔内容：{f.content[:100]}{'...' if len(f.content) > 100 else ''}")
                    if f.resolution_notes:
                        lines.append(f"  回收提示：{f.resolution_notes}")
                    lines.append("")
            
            # 2. 超期伏笔（已过目标回收章节但未回收）
            if include_overdue:
                overdue = await self.get_overdue_foreshadows(db, project_id, chapter_number)
                if overdue:
                    lines.append("【⚠️ 超期待回收伏笔 - 请尽快回收】")
                    for f in overdue[:3]:
                        overdue_chapters = chapter_number - (f.target_resolve_chapter_number or 0)
                        lines.append(f"- ID:{f.id[:8]} | {f.title} [已超期{overdue_chapters}章]")
                        lines.append(f"  埋入章节：第{f.plant_chapter_number}章，原计划第{f.target_resolve_chapter_number}章回收")
                        lines.append(f"  伏笔内容：{f.content[:80]}...")
                    lines.append("")
            
            # 3. 即将需要回收的伏笔（仅作为背景参考，明确禁止提前回收）
            upcoming_raw = await self.get_pending_resolve_foreshadows(
                db, project_id, chapter_number, lookahead
            )
            # 过滤：排除本章必须回收的和超期的，只保留未来章节的
            upcoming = [f for f in upcoming_raw
                       if (f.target_resolve_chapter_number or 0) > chapter_number]
            
            if upcoming:
                lines.append("【📋 近期待回收伏笔（仅供参考，请勿在本章回收）】")
                lines.append("⚠️ 以下伏笔尚未到回收时机，本章请勿提前回收，仅作为剧情背景了解")
                for f in upcoming[:5]:
                    remaining = (f.target_resolve_chapter_number or 0) - chapter_number
                    lines.append(f"- {f.title}（计划第{f.target_resolve_chapter_number}章回收，还有{remaining}章）")
                lines.append("")
            
            # 4. 本章待埋入伏笔
            if include_pending:
                to_plant = await self.get_foreshadows_to_plant(db, project_id, chapter_number)
                if to_plant:
                    lines.append("【✨ 本章计划埋入伏笔】")
                    for f in to_plant:
                        content_preview = f.content[:80] if len(f.content) > 80 else f.content
                        lines.append(f"- {f.title}")
                        lines.append(f"  伏笔内容：{content_preview}")
                        if f.hint_text:
                            lines.append(f"  埋入提示：{f.hint_text}")
                    lines.append("")
            
            context_text = "\n".join(lines) if lines else ""
            
            return {
                "chapter_number": chapter_number,
                "context_text": context_text,
                "pending_plant": [f.to_dict() for f in to_plant],
                "must_resolve": [f.to_dict() for f in must_resolve],
                "pending_resolve": [f.to_dict() for f in upcoming],
                "overdue": [f.to_dict() for f in overdue],
                "recently_planted": []  # 可扩展
            }
            
        except Exception as e:
            logger.error(f"❌ 构建伏笔上下文失败: {str(e)}")
            return {
                "chapter_number": chapter_number,
                "context_text": "",
                "pending_plant": [],
                "must_resolve": [],
                "pending_resolve": [],
                "overdue": [],
                "recently_planted": []
            }
    
    async def get_stats(
        self,
        db: AsyncSession,
        project_id: str,
        current_chapter: Optional[int] = None
    ) -> Dict[str, int]:
        """
        获取伏笔统计
        
        Args:
            db: 数据库会话
            project_id: 项目ID
            current_chapter: 当前章节号(用于计算超期)
        
        Returns:
            统计信息字典
        """
        try:
            # 各状态统计
            stats_query = (
                select(
                    Foreshadow.status,
                    func.count(Foreshadow.id).label('count')
                )
                .where(Foreshadow.project_id == project_id)
                .group_by(Foreshadow.status)
            )
            
            result = await db.execute(stats_query)
            status_counts = {row.status: row.count for row in result}
            
            # 总数
            total = sum(status_counts.values())
            
            # 长线伏笔数量
            long_term_query = (
                select(func.count(Foreshadow.id))
                .where(
                    and_(
                        Foreshadow.project_id == project_id,
                        Foreshadow.is_long_term == True
                    )
                )
            )
            long_term_result = await db.execute(long_term_query)
            long_term_count = long_term_result.scalar() or 0
            
            # 超期数量
            overdue_count = 0
            if current_chapter:
                overdue = await self.get_overdue_foreshadows(db, project_id, current_chapter)
                overdue_count = len(overdue)
            
            return {
                "total": total,
                "pending": status_counts.get("pending", 0),
                "planted": status_counts.get("planted", 0),
                "resolved": status_counts.get("resolved", 0),
                "partially_resolved": status_counts.get("partially_resolved", 0),
                "abandoned": status_counts.get("abandoned", 0),
                "long_term_count": long_term_count,
                "overdue_count": overdue_count
            }
            
        except Exception as e:
            logger.error(f"❌ 获取伏笔统计失败: {str(e)}")
            return {
                "total": 0,
                "pending": 0,
                "planted": 0,
                "resolved": 0,
                "partially_resolved": 0,
                "abandoned": 0,
                "long_term_count": 0,
                "overdue_count": 0
            }
    
    async def get_planted_foreshadows_for_analysis(
        self,
        db: AsyncSession,
        project_id: str,
        current_chapter_number: Optional[int] = None
    ) -> List[Dict[str, Any]]:
        """
        获取用于分析时注入的已埋入伏笔列表（智能过滤版）
        
        策略：
        1. 只返回 status='planted' 的伏笔
        2. 如果指定了当前章节号，会标记哪些伏笔应该在本章回收
        3. 区分"可回收"和"不可回收"伏笔，帮助AI正确识别
        
        Args:
            db: 数据库会话
            project_id: 项目ID
            current_chapter_number: 当前章节号（可选，用于智能标记）
        
        Returns:
            伏笔列表（带回收标记）
        """
        try:
            query = (
                select(Foreshadow)
                .where(
                    and_(
                        Foreshadow.project_id == project_id,
                        Foreshadow.status == "planted"
                    )
                )
                .order_by(Foreshadow.plant_chapter_number)
            )
            
            result = await db.execute(query)
            foreshadows = result.scalars().all()
            
            formatted_list = []
            for f in foreshadows:
                item = {
                    "id": f.id,
                    "title": f.title,
                    "content": f.content,
                    "hint_text": f.hint_text, 
                    "plant_chapter_number": f.plant_chapter_number,
                    "target_resolve_chapter_number": f.target_resolve_chapter_number,
                    "category": f.category,
                    "related_characters": f.related_characters or [],
                    "is_long_term": f.is_long_term
                }
                
                # 智能标记回收状态
                if current_chapter_number and f.target_resolve_chapter_number:
                    if f.target_resolve_chapter_number == current_chapter_number:
                        item["resolve_status"] = "must_resolve_now"  # 本章必须回收
                        item["resolve_hint"] = "本章必须回收此伏笔"
                    elif f.target_resolve_chapter_number < current_chapter_number:
                        item["resolve_status"] = "overdue"  # 已超期
                        item["resolve_hint"] = f"已超期{current_chapter_number - f.target_resolve_chapter_number}章，应尽快回收"
                    else:
                        item["resolve_status"] = "not_yet"  # 尚未到期
                        item["resolve_hint"] = f"计划第{f.target_resolve_chapter_number}章回收，请勿提前回收"
                else:
                    item["resolve_status"] = "no_plan"  # 无明确计划
                    item["resolve_hint"] = "无明确回收计划，根据剧情自然回收"
                
                formatted_list.append(item)
            
            return formatted_list
            
        except Exception as e:
            logger.error(f"❌ 获取已埋入伏笔失败: {str(e)}")
            return []
    
    async def delete_chapter_foreshadows(
        self,
        db: AsyncSession,
        project_id: str,
        chapter_id: str,
        only_analysis_source: bool = True
    ) -> Dict[str, Any]:
        """
        删除与指定章节相关的伏笔
        
        当章节内容被清空或重新生成时调用，清理残留数据
        
        Args:
            db: 数据库会话
            project_id: 项目ID
            chapter_id: 章节ID
            only_analysis_source: 是否只删除来源为 analysis 的伏笔（默认True）
                                  如果为False，则删除所有与该章节相关的伏笔
        
        Returns:
            删除统计信息
        """
        try:
            # 1. 先通过 PlotAnalysis 查找该章节的分析ID
            # 这是关键：sync_from_analysis 创建的伏笔使用 source_analysis_id 关联
            analysis_query = select(PlotAnalysis.id).where(PlotAnalysis.chapter_id == chapter_id)
            analysis_result = await db.execute(analysis_query)
            analysis_ids = [row[0] for row in analysis_result.fetchall()]
            
            logger.debug(f"🔍 找到章节 {chapter_id[:8]} 的分析ID: {len(analysis_ids)} 个")
            
            # 2. 构建查询条件：查找与该章节相关的伏笔
            # 匹配方式：
            # 1. 埋入章节是该章节 (plant_chapter_id)
            # 2. 回收章节是该章节 (actual_resolve_chapter_id)
            # 3. 来源分析ID对应该章节的分析 (source_analysis_id)
            or_conditions = [
                Foreshadow.plant_chapter_id == chapter_id,
                Foreshadow.actual_resolve_chapter_id == chapter_id,
            ]
            
            # 如果找到了分析ID，添加 source_analysis_id 匹配条件
            if analysis_ids:
                or_conditions.append(Foreshadow.source_analysis_id.in_(analysis_ids))
            
            conditions = [
                Foreshadow.project_id == project_id,
                or_(*or_conditions)
            ]
            
            # 如果只删除分析来源的伏笔
            if only_analysis_source:
                conditions.append(Foreshadow.source_type == "analysis")
            
            # 查询要删除的伏笔
            query = select(Foreshadow).where(and_(*conditions))
            result = await db.execute(query)
            foreshadows_to_delete = result.scalars().all()
            
            deleted_count = len(foreshadows_to_delete)
            deleted_ids = [f.id for f in foreshadows_to_delete]
            deleted_titles = [f.title for f in foreshadows_to_delete]
            
            # 执行删除
            for foreshadow in foreshadows_to_delete:
                await db.delete(foreshadow)
            
            await db.commit()
            
            if deleted_count > 0:
                logger.info(f"🗑️ 已删除章节 {chapter_id[:8]} 相关的 {deleted_count} 个伏笔")
                for title in deleted_titles[:5]:  # 只打印前5个
                    logger.debug(f"  - {title}")
                if deleted_count > 5:
                    logger.debug(f"  ... 还有 {deleted_count - 5} 个")
            
            return {
                "deleted_count": deleted_count,
                "deleted_ids": deleted_ids,
                "deleted_titles": deleted_titles
            }
            
        except Exception as e:
            await db.rollback()
            logger.error(f"❌ 删除章节伏笔失败: {str(e)}")
            raise
    
    async def clean_chapter_analysis_foreshadows(
        self,
        db: AsyncSession,
        project_id: str,
        chapter_id: str
    ) -> Dict[str, Any]:
        """
        清理章节分析产生的伏笔（用于重新分析前的清理）
        
        两步操作：
        1. 删除 source_type='analysis' 且 plant_chapter_id == chapter_id 的伏笔
        2. 回退在本章被回收的伏笔（将其从 resolved 恢复为 planted）
        
        保留手动创建的伏笔
        
        Args:
            db: 数据库会话
            project_id: 项目ID
            chapter_id: 章节ID
        
        Returns:
            清理统计信息
        """
        try:
            # 步骤1: 删除在本章埋入的分析伏笔
            query = select(Foreshadow).where(
                and_(
                    Foreshadow.project_id == project_id,
                    Foreshadow.source_type == "analysis",
                    Foreshadow.plant_chapter_id == chapter_id
                )
            )
            
            result = await db.execute(query)
            foreshadows_to_clean = result.scalars().all()
            
            cleaned_count = len(foreshadows_to_clean)
            cleaned_ids = [f.id for f in foreshadows_to_clean]
            
            for foreshadow in foreshadows_to_clean:
                await db.delete(foreshadow)
            
            # 步骤2: 回退在本章被回收的伏笔（恢复为 planted 状态）
            reverted_count = await self._revert_chapter_resolutions(db, project_id, chapter_id)
            
            await db.commit()
            
            if cleaned_count > 0 or reverted_count > 0:
                logger.info(f"🧹 已清理章节 {chapter_id[:8]}: 删除{cleaned_count}个分析伏笔, 回退{reverted_count}个回收状态")
            
            return {
                "cleaned_count": cleaned_count,
                "cleaned_ids": cleaned_ids,
                "reverted_count": reverted_count
            }
            
        except Exception as e:
            await db.rollback()
            logger.error(f"❌ 清理章节分析伏笔失败: {str(e)}")
            raise
    
    async def _revert_chapter_resolutions(
        self,
        db: AsyncSession,
        project_id: str,
        chapter_id: str
    ) -> int:
        """
        回退在指定章节中被回收的伏笔
        
        将 actual_resolve_chapter_id == chapter_id 且 status 为 resolved/partially_resolved 的伏笔
        恢复为 planted 状态，以便重新分析时可以重新匹配回收
        
        Args:
            db: 数据库会话
            project_id: 项目ID
            chapter_id: 章节ID
        
        Returns:
            回退的伏笔数量
        """
        try:
            update_query = (
                update(Foreshadow)
                .where(
                    and_(
                        Foreshadow.project_id == project_id,
                        Foreshadow.actual_resolve_chapter_id == chapter_id,
                        Foreshadow.status.in_(["resolved", "partially_resolved"])
                    )
                )
                .values(
                    status="planted",
                    actual_resolve_chapter_id=None,
                    actual_resolve_chapter_number=None,
                    resolved_at=None,
                    resolution_text=None
                )
            )
            result = await db.execute(update_query)
            reverted_count = result.rowcount
            
            if reverted_count > 0:
                logger.info(f"↩️ 回退了 {reverted_count} 个在章节 {chapter_id[:8]} 中被回收的伏笔")
            
            return reverted_count
            
        except Exception as e:
            logger.error(f"❌ 回退章节回收失败: {str(e)}")
            return 0

    async def clear_project_foreshadows_for_reset(
        self,
        db: AsyncSession,
        project_id: str
    ) -> Dict[str, int]:
        """
        全新生成时清理项目伏笔
        
        1. 删除所有 source_type='analysis' 的伏笔
        2. 重置所有 source_type='manual' 的伏笔为 pending 状态
        
        Args:
            db: 数据库会话
            project_id: 项目ID
            
        Returns:
            清理统计
        """
        try:
            # 1. 删除分析产生的伏笔
            delete_query = delete(Foreshadow).where(
                and_(
                    Foreshadow.project_id == project_id,
                    Foreshadow.source_type == "analysis"
                )
            )
            delete_result = await db.execute(delete_query)
            deleted_count = delete_result.rowcount
            
            # 2. 重置手动创建的伏笔
            # 将 planted/resolved/partially_resolved 状态重置为 pending
            # 清空章节关联信息
            update_query = (
                update(Foreshadow)
                .where(
                    and_(
                        Foreshadow.project_id == project_id,
                        Foreshadow.source_type == "manual",
                        Foreshadow.status.in_(["planted", "resolved", "partially_resolved"])
                    )
                )
                .values(
                    status="pending",
                    plant_chapter_id=None,
                    plant_chapter_number=None,
                    actual_resolve_chapter_id=None,
                    actual_resolve_chapter_number=None,
                    planted_at=None,
                    resolved_at=None,
                    target_resolve_chapter_id=None,
                    target_resolve_chapter_number=None
                )
            )
            update_result = await db.execute(update_query)
            reset_count = update_result.rowcount
            
            await db.commit()
            
            logger.info(f"🧹 项目 {project_id} 伏笔清理完成: 删除 {deleted_count} 个分析伏笔, 重置 {reset_count} 个手动伏笔")
            
            return {
                "deleted_count": deleted_count,
                "reset_count": reset_count
            }
            
        except Exception as e:
            await db.rollback()
            logger.error(f"❌ 清理项目伏笔失败: {str(e)}")
            raise

    async def auto_update_from_analysis(
        self,
        db: AsyncSession,
        project_id: str,
        chapter_id: str,
        chapter_number: int,
        analysis_foreshadows: List[Dict[str, Any]]
    ) -> Dict[str, Any]:
        """
        根据章节分析结果自动更新伏笔状态
        
        功能：
        1. 自动标记新埋入的伏笔为 planted
        2. 根据 reference_foreshadow_id 自动回收已有伏笔
        3. 如果没有 reference_foreshadow_id，使用内容匹配备用机制
        4. 创建新发现的伏笔记录
        
        Args:
            db: 数据库会话
            project_id: 项目ID
            chapter_id: 章节ID
            chapter_number: 章节号
            analysis_foreshadows: 分析结果中的伏笔列表
        
        Returns:
            更新统计
        """
        try:
            stats = {
                "planted_count": 0,      # 新埋入的伏笔
                "resolved_count": 0,     # 回收的伏笔
                "created_count": 0,      # 新创建的伏笔记录
                "updated_ids": [],       # 更新的伏笔ID
                "created_ids": [],       # 创建的伏笔ID
                "matched_by_content": 0, # 通过内容匹配回收的数量
                "errors": []             # 错误信息
            }
            
            # 预先获取所有已埋入的伏笔，用于内容匹配
            planted_foreshadows = await self.get_planted_foreshadows_for_analysis(db, project_id)
            
            # 每章最多创建的新伏笔数量
            MAX_NEW_FORESHADOWS_PER_CHAPTER = 5
            new_foreshadow_count = 0

            for fs_data in analysis_foreshadows:
                try:
                    fs_type = fs_data.get("type", "planted")
                    reference_id = fs_data.get("reference_foreshadow_id")
                    
                    if fs_type == "resolved":
                        existing = None
                        matched_by_content = False
                        
                        # 策略1: 优先使用 reference_id 精确匹配
                        # 重要：如果分析结果里已经给出了 reference_foreshadow_id，
                        # 但该伏笔已被用户删除或已不属于当前项目，则直接跳过，
                        # 不再退回内容匹配，避免把“已删除伏笔”的旧分析结果错误同步到其他同名/近似伏笔上。
                        if reference_id:
                            existing = await self.get_foreshadow(db, reference_id)
                            if existing and existing.project_id == project_id:
                                logger.info(f"🎯 通过ID精确匹配伏笔: {existing.title}")
                            else:
                                existing = None
                                logger.warning(f"⚠️ 伏笔ID不存在或不属于该项目，跳过本次回收同步: {reference_id}")
                                stats["skipped_resolve_count"] = stats.get("skipped_resolve_count", 0) + 1
                                stats["errors"].append(f"reference_foreshadow_id 无效或已删除: {reference_id}")
                                continue
                        
                        # 策略2: 内容匹配备用机制（仅在 analysis 未提供 reference_id 时启用）
                        if not reference_id and not existing and planted_foreshadows:
                            matched = self._match_foreshadow_by_content(
                                fs_data, planted_foreshadows
                            )
                            if matched:
                                matched_by_content = True
                                logger.info(f"🔍 通过内容匹配找到伏笔: {matched.get('title')}")
                                # 重新获取完整的伏笔对象
                                existing = await self.get_foreshadow(db, matched.get('id'))
                        
                        # 检查伏笔是否已被回收（防止重复回收）
                        if existing:
                            if existing.status == "resolved" and existing.actual_resolve_chapter_number == chapter_number:
                                logger.info(f"ℹ️ 伏笔已在本章回收过，跳过: {existing.title}")
                                continue
                            elif existing.status == "resolved":
                                logger.warning(f"⚠️ 伏笔已在第{existing.actual_resolve_chapter_number}章回收，跳过: {existing.title}")
                                continue
                        
                        # 执行回收
                        if existing and existing.status == "planted":
                            # 更新为已回收状态
                            existing.status = "resolved"
                            existing.actual_resolve_chapter_id = chapter_id
                            existing.actual_resolve_chapter_number = chapter_number
                            existing.resolved_at = datetime.now()
                            
                            # 更新回收文本
                            if fs_data.get("content"):
                                existing.resolution_text = fs_data.get("content")
                            
                            await db.flush()
                            await db.refresh(existing)
                            
                            stats["resolved_count"] += 1
                            stats["updated_ids"].append(existing.id)
                            if matched_by_content:
                                stats["matched_by_content"] += 1
                            logger.info(f"✅ 自动回收伏笔: {existing.title} (ID: {existing.id}, status: {existing.status})")
                            
                            # 从待匹配列表中移除已回收的伏笔
                            planted_foreshadows = [f for f in planted_foreshadows if f['id'] != existing.id]
                        elif existing:
                            logger.warning(f"⚠️ 伏笔状态不是planted，跳过回收: {existing.title} (status: {existing.status})")
                        else:
                            # 找不到匹配的已埋入伏笔，跳过（不创建新记录！）
                            # 核心原则：只有"埋入"操作会创建伏笔记录，"回收"只是更新已有记录
                            # 如果没有埋入的伏笔，就不可能存在回收
                            fs_title = fs_data.get("title", fs_data.get("content", "")[:30])
                            logger.warning(f"⚠️ 未找到匹配的已埋入伏笔，跳过回收（不创建新记录）: {fs_title}")
                            logger.warning(f"   提示：AI可能误识别了回收伏笔，或者 reference_foreshadow_id 未正确填写")
                            stats["skipped_resolve_count"] = stats.get("skipped_resolve_count", 0) + 1
                            continue
                    
                    elif fs_type == "planted":
                        fs_content = fs_data.get("content", "")
                        if not fs_content:
                            logger.warning(f"⚠️ 伏笔内容为空，跳过")
                            continue
                        
                        fs_title = fs_data.get("title", "")
                        if not fs_title:
                            fs_title = fs_content[:50] + ("..." if len(fs_content) > 50 else "")
                        
                        # 使用稳定的唯一标识符（基于 chapter_id + content_hash）
                        source_memory_id = generate_stable_foreshadow_id(
                            chapter_id, fs_content, fs_type
                        )
                        
                        # 检查是否已存在（使用稳定ID去重，防止重复分析创建重复记录）
                        existing_check = await db.execute(
                            select(Foreshadow).where(
                                and_(
                                    Foreshadow.project_id == project_id,
                                    or_(
                                        # 方式1：通过稳定source_memory_id精确匹配
                                        Foreshadow.source_memory_id == source_memory_id,
                                        # 方式2：通过标题+章节号匹配（兼容旧数据）
                                        and_(
                                            Foreshadow.title == fs_title,
                                            Foreshadow.plant_chapter_id == chapter_id,
                                            Foreshadow.source_type == "analysis"
                                        )
                                    )
                                )
                            )
                        )
                        existing_fs = existing_check.scalar_one_or_none()
                        
                        if existing_fs:
                            # 更新已存在的伏笔，避免重复创建
                            existing_fs.title = fs_title
                            existing_fs.content = fs_content
                            existing_fs.strength = fs_data.get("strength", existing_fs.strength)
                            existing_fs.subtlety = fs_data.get("subtlety", existing_fs.subtlety)
                            existing_fs.hint_text = fs_data.get("keyword", existing_fs.hint_text)
                            existing_fs.category = fs_data.get("category", existing_fs.category)
                            existing_fs.is_long_term = fs_data.get("is_long_term", existing_fs.is_long_term)
                            existing_fs.related_characters = fs_data.get("related_characters", existing_fs.related_characters)
                            if fs_data.get("estimated_resolve_chapter"):
                                existing_fs.target_resolve_chapter_number = fs_data.get("estimated_resolve_chapter")
                            # 更新为稳定的source_memory_id
                            existing_fs.source_memory_id = source_memory_id
                            await db.flush()
                            stats["updated_ids"].append(existing_fs.id)
                            logger.info(f"📝 更新已存在伏笔（避免重复）: {fs_title} (ID: {existing_fs.id})")
                        else:
                            # 创建新伏笔
                            # 检查每章新伏笔数量上限
                            if new_foreshadow_count >= MAX_NEW_FORESHADOWS_PER_CHAPTER:
                                logger.info(f"🚫 已达每章新伏笔上限({MAX_NEW_FORESHADOWS_PER_CHAPTER}个)，跳过: {fs_title}")
                                continue
                            
                            # 不再为 estimated_resolve_chapter 设置默认值，避免误报"超期"
                            estimated_resolve = fs_data.get("estimated_resolve_chapter")
                            if estimated_resolve is None:
                                logger.info(f"ℹ️ AI未填写estimated_resolve_chapter，不设默认值，标记为无明确回收计划")
                            
                            new_foreshadow = Foreshadow(
                                id=str(uuid.uuid4()),
                                project_id=project_id,
                                title=fs_title,
                                content=fs_content,
                                hint_text=fs_data.get("keyword"),
                                source_type="analysis",
                                source_memory_id=source_memory_id,  # 使用稳定的唯一标识
                                plant_chapter_id=chapter_id,
                                plant_chapter_number=chapter_number,
                                planted_at=datetime.now(),
                                target_resolve_chapter_number=estimated_resolve,
                                status="planted",
                                is_long_term=fs_data.get("is_long_term", False),
                                importance=min(fs_data.get("strength", 5) / 10.0, 1.0),
                                strength=fs_data.get("strength", 5),
                                subtlety=fs_data.get("subtlety", 5),
                                category=fs_data.get("category"),
                                related_characters=fs_data.get("related_characters"),
                                auto_remind=True,
                                remind_before_chapters=5,
                                include_in_context=True
                            )
                            
                            db.add(new_foreshadow)
                            await db.flush()
                            
                            new_foreshadow_count += 1
                            stats["planted_count"] += 1
                            stats["created_count"] += 1
                            stats["created_ids"].append(new_foreshadow.id)
                            logger.info(f"✅ 自动创建伏笔: {fs_title} (ID: {new_foreshadow.id}) [{new_foreshadow_count}/{MAX_NEW_FORESHADOWS_PER_CHAPTER}]")
                    
                except Exception as item_error:
                    error_msg = f"处理伏笔时出错: {str(item_error)}"
                    stats["errors"].append(error_msg)
                    logger.error(f"❌ {error_msg}")
            
            await db.commit()
            
            logger.info(f"📊 伏笔自动更新完成: 埋入{stats['planted_count']}个, 回收{stats['resolved_count']}个, 创建{stats['created_count']}个")
            return stats
            
        except Exception as e:
            await db.rollback()
            logger.error(f"❌ 自动更新伏笔失败: {str(e)}")
            raise
    
    async def auto_plant_pending_foreshadows(
        self,
        db: AsyncSession,
        project_id: str,
        chapter_id: str,
        chapter_number: int,
        chapter_content: str
    ) -> Dict[str, Any]:
        """
        自动将计划在本章埋入的伏笔标记为已埋入
        
        检查 pending 状态且 plant_chapter_number == chapter_number 的伏笔，
        如果章节内容中包含相关关键词，则自动标记为 planted
        
        Args:
            db: 数据库会话
            project_id: 项目ID
            chapter_id: 章节ID
            chapter_number: 章节号
            chapter_content: 章节内容
        
        Returns:
            更新统计
        """
        try:
            stats = {
                "checked_count": 0,
                "planted_count": 0,
                "planted_ids": []
            }
            
            # 获取计划在本章埋入的伏笔
            pending_foreshadows = await self.get_foreshadows_to_plant(
                db, project_id, chapter_number
            )
            
            stats["checked_count"] = len(pending_foreshadows)
            
            for fs in pending_foreshadows:
                # 用户明确指定了本章埋入的伏笔，自动标记为已埋入
                # 注：只有 pending 状态且 plant_chapter_number == chapter_number 的伏笔
                # 才会被 get_foreshadows_to_plant 查出，所以这里直接标记即可
                should_plant = True
                
                if should_plant:
                    fs.status = "planted"
                    fs.plant_chapter_id = chapter_id
                    fs.planted_at = datetime.now()
                    await db.flush()
                    
                    stats["planted_count"] += 1
                    stats["planted_ids"].append(fs.id)
                    logger.info(f"✅ 自动标记伏笔已埋入: {fs.title} (第{chapter_number}章)")
            
            await db.commit()
            
            if stats["planted_count"] > 0:
                logger.info(f"📊 自动埋入伏笔: 检查{stats['checked_count']}个, 埋入{stats['planted_count']}个")
            
            return stats
            
        except Exception as e:
            await db.rollback()
            logger.error(f"❌ 自动埋入伏笔失败: {str(e)}")
            return {"checked_count": 0, "planted_count": 0, "planted_ids": [], "error": str(e)}


    def _match_foreshadow_by_content(
        self,
        resolved_fs_data: Dict[str, Any],
        planted_foreshadows: List[Dict[str, Any]],
        min_similarity: float = 0.5
    ) -> Optional[Dict[str, Any]]:
        """
        通过内容相似度匹配伏笔（备用机制）
        
        匹配策略（按优先级）：
        1. 标题完全匹配（权重最高）
        2. 标题部分匹配（包含关系）
        3. 标题关键词匹配（去除"回收"等后缀）
        4. 关键词匹配
        5. 内容关键词匹配
        6. 相关角色匹配 + 分类匹配
        
        Args:
            resolved_fs_data: 分析结果中的回收伏笔数据
            planted_foreshadows: 已埋入的伏笔列表
            min_similarity: 最低相似度阈值
        
        Returns:
            最匹配的伏笔对象或None
        """
        if not planted_foreshadows:
            return None
        
        resolved_title = resolved_fs_data.get("title", "").strip()
        resolved_content = resolved_fs_data.get("content", "").strip()
        resolved_keyword = resolved_fs_data.get("keyword", "").strip()
        resolved_category = resolved_fs_data.get("category")
        resolved_characters = set(resolved_fs_data.get("related_characters", []))
        reference_chapter = resolved_fs_data.get("reference_chapter")
        
        # 处理标题后缀（兜底机制）
        resolved_title_clean = resolved_title
        for suffix in ["回收", "揭示", "解答", "兑现"]:
            if resolved_title.endswith(suffix):
                resolved_title_clean = resolved_title[:-len(suffix)]
                logger.debug(f"🔍 去除标题后缀: '{resolved_title}' -> '{resolved_title_clean}'")
                break
        
        best_match = None
        best_score = 0.0
        
        for fs in planted_foreshadows:
            score = 0.0
            fs_title = fs.get("title", "").strip()
            fs_content = fs.get("content", "").strip()
            fs_category = fs.get("category")
            fs_characters = set(fs.get("related_characters", []))
            fs_plant_chapter = fs.get("plant_chapter_number")
            
            # 策略1: 标题匹配
            if resolved_title and fs_title:
                if resolved_title == fs_title:
                    score = 1.0
                    logger.debug(f"🎯 标题完全匹配: '{resolved_title}' == '{fs_title}'")
                elif resolved_title_clean and resolved_title_clean == fs_title:
                    score = 0.95
                    logger.debug(f"🎯 清理标题匹配: '{resolved_title_clean}' == '{fs_title}'")
                elif resolved_title in fs_title or fs_title in resolved_title:
                    score = max(score, 0.8)
                    logger.debug(f"🔍 标题包含匹配: '{resolved_title}' <-> '{fs_title}'")
                elif resolved_title_clean and (resolved_title_clean in fs_title or fs_title in resolved_title_clean):
                    score = max(score, 0.75)
                    logger.debug(f"🔍 清理标题包含匹配: '{resolved_title_clean}' <-> '{fs_title}'")
                else:
                    title_overlap = self._calculate_word_overlap(resolved_title, fs_title)
                    score = max(score, title_overlap * 0.7)
                    if title_overlap > 0.3:
                        logger.debug(f"📊 标题词重叠: overlap={title_overlap:.2f}")
            
            # 策略2: 关键词匹配
            if resolved_keyword and fs_content:
                if resolved_keyword in fs_content:
                    score = max(score, 0.75)
            
            # 策略3: 内容关键词匹配
            if resolved_content and fs_content:
                content_overlap = self._calculate_word_overlap(resolved_content, fs_content)
                score = max(score, content_overlap * 0.6)
            
            # 策略4: 引用章节号匹配（如果分析结果中有reference_chapter）
            if reference_chapter and fs_plant_chapter:
                if reference_chapter == fs_plant_chapter:
                    score += 0.15  # 加分
            
            # 策略5: 分类匹配
            if resolved_category and fs_category:
                if resolved_category == fs_category:
                    score += 0.1
            
            # 策略6: 相关角色匹配
            if resolved_characters and fs_characters:
                character_overlap = len(resolved_characters & fs_characters) / max(len(resolved_characters | fs_characters), 1)
                score += character_overlap * 0.1
            
            # 更新最佳匹配
            if score > best_score and score >= min_similarity:
                best_score = score
                best_match = fs
        
        if best_match:
            logger.info(f"🎯 内容匹配成功: '{resolved_title}' -> '{best_match.get('title')}' (相似度: {best_score:.2f})")
        
        return best_match
    
    def _calculate_word_overlap(self, text1: str, text2: str) -> float:
        """
        计算两个文本的词重叠度
        
        使用字符级别的 n-gram 相似度计算
        
        Args:
            text1: 文本1
            text2: 文本2
        
        Returns:
            0-1之间的相似度分数
        """
        if not text1 or not text2:
            return 0.0
        
        # 使用2-gram和3-gram
        def get_ngrams(text: str, n: int) -> set:
            text = text.lower().replace(" ", "").replace("\n", "")
            if len(text) < n:
                return {text}
            return {text[i:i+n] for i in range(len(text) - n + 1)}
        
        # 计算2-gram相似度
        ngrams1_2 = get_ngrams(text1, 2)
        ngrams2_2 = get_ngrams(text2, 2)
        overlap_2 = len(ngrams1_2 & ngrams2_2) / max(len(ngrams1_2 | ngrams2_2), 1)
        
        # 计算3-gram相似度
        ngrams1_3 = get_ngrams(text1, 3)
        ngrams2_3 = get_ngrams(text2, 3)
        overlap_3 = len(ngrams1_3 & ngrams2_3) / max(len(ngrams1_3 | ngrams2_3), 1)
        
        # 综合评分（3-gram权重更高，因为更精确）
        return overlap_2 * 0.4 + overlap_3 * 0.6


# 创建全局服务实例
foreshadow_service = ForeshadowService()