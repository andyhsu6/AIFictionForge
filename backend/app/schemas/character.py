"""角色相关的Pydantic模型"""
import json

from pydantic import BaseModel, Field, ConfigDict, field_validator
from typing import Optional, List, Dict, Any, Literal
from datetime import datetime


class CharacterBase(BaseModel):
    """角色基础模型"""
    name: str = Field(..., description="角色/组织姓名")
    age: Optional[str] = Field(None, description="年龄")
    gender: Optional[str] = Field(None, description="性别")
    is_organization: bool = Field(False, description="是否为组织")
    role_type: Optional[str] = Field(None, description="角色类型：protagonist/supporting/antagonist")
    personality: Optional[str] = Field(None, description="性格特点/组织特性")
    background: Optional[str] = Field(None, description="背景故事")
    appearance: Optional[str] = Field(None, description="外貌特征")
    relationships: Optional[str] = Field(None, description="人际关系(JSON)")
    organization_type: Optional[str] = Field(None, description="组织类型")
    organization_purpose: Optional[str] = Field(None, description="组织目的")
    organization_members: Optional[str] = Field(None, description="组织成员(JSON)")
    traits: Optional[str] = Field(None, description="特征标签(JSON)")


class CharacterCreate(BaseModel):
    """手动创建角色的请求模型"""
    project_id: str = Field(..., description="项目ID")
    name: str = Field(..., description="角色/组织姓名")
    age: Optional[str] = Field(None, description="年龄")
    gender: Optional[str] = Field(None, description="性别")
    is_organization: bool = Field(False, description="是否为组织")
    role_type: Optional[str] = Field("supporting", description="角色类型：protagonist/supporting/antagonist")
    personality: Optional[str] = Field(None, description="性格特点/组织特性")
    background: Optional[str] = Field(None, description="背景故事")
    appearance: Optional[str] = Field(None, description="外貌特征")
    organization_type: Optional[str] = Field(None, description="组织类型")
    organization_purpose: Optional[str] = Field(None, description="组织目的")
    organization_members: Optional[str] = Field(None, description="组织成员(JSON)")
    traits: Optional[str] = Field(None, description="特征标签(JSON)")
    avatar_url: Optional[str] = Field(None, description="头像URL")
    
    # 组织额外字段
    power_level: Optional[int] = Field(None, description="组织势力等级(0-100)")
    location: Optional[str] = Field(None, description="组织所在地")
    motto: Optional[str] = Field(None, description="组织格言/口号")
    color: Optional[str] = Field(None, description="组织代表颜色")
    
    # 职业字段
    main_career_id: Optional[str] = Field(None, description="主职业ID")
    main_career_stage: Optional[int] = Field(None, description="主职业阶段")
    sub_careers: Optional[str] = Field(None, description="副职业列表JSON字符串")


class CharacterUpdate(BaseModel):
    """更新角色的请求模型"""
    name: Optional[str] = None
    age: Optional[str] = None
    gender: Optional[str] = None
    is_organization: Optional[bool] = None
    role_type: Optional[str] = None
    personality: Optional[str] = None
    background: Optional[str] = None
    appearance: Optional[str] = None
    organization_type: Optional[str] = None
    organization_purpose: Optional[str] = None
    organization_members: Optional[str] = None
    traits: Optional[str] = None
    
    # 组织额外字段（会同步到Organization表）
    power_level: Optional[int] = Field(None, description="组织势力等级(0-100)")
    location: Optional[str] = Field(None, description="组织所在地")
    motto: Optional[str] = Field(None, description="组织格言/口号")
    color: Optional[str] = Field(None, description="组织代表颜色")
    
    # 职业字段（会同步到CharacterCareer表）
    main_career_id: Optional[str] = Field(None, description="主职业ID")
    main_career_stage: Optional[int] = Field(None, description="主职业阶段")
    sub_careers: Optional[str] = Field(None, description="副职业列表JSON字符串")


class CharacterResponse(CharacterBase):
    """角色响应模型"""
    id: str
    project_id: str
    avatar_url: Optional[str] = None
    created_at: datetime
    updated_at: datetime
    
    # 组织额外字段（从Organization表关联）
    power_level: Optional[int] = Field(None, description="组织势力等级(0-100)")
    location: Optional[str] = Field(None, description="组织所在地")
    motto: Optional[str] = Field(None, description="组织格言/口号")
    color: Optional[str] = Field(None, description="组织代表颜色")
    
    # 职业信息字段
    main_career_id: Optional[str] = Field(None, description="主职业ID")
    main_career_stage: Optional[int] = Field(None, description="主职业阶段")
    sub_careers: Optional[List[Dict[str, Any]]] = Field(None, description="副职业列表")
    
    # 角色/组织存活状态
    status: Optional[str] = Field("active", description="状态：active/deceased/missing/retired/destroyed")
    status_changed_chapter: Optional[int] = Field(None, description="状态变更的章节号")
    
    # 心理状态追踪字段
    current_state: Optional[str] = Field(None, description="角色当前心理状态")
    state_updated_chapter: Optional[int] = Field(None, description="心理状态最后更新的章节号")

    # 档案事实溯源（issue #190）：库里是 JSON 文本，对外输出解析后的数组
    profile_facts: Optional[List[Dict[str, Any]]] = Field(
        None, description="档案事实溯源：[{field, claim, quote, chapter_number, inferred}]"
    )
    profile_facts_updated_at: Optional[datetime] = Field(
        None, description="档案事实最后写入时间；晚于 updated_at 说明文本在依据之后被改过"
    )

    @field_validator("profile_facts", mode="before")
    @classmethod
    def _parse_profile_facts(cls, value: Any) -> Any:
        """把库里的 JSON 文本解析成数组；损坏时返回 None，不让整个响应挂掉。"""
        if isinstance(value, str):
            text = value.strip()
            if not text:
                return None
            try:
                parsed = json.loads(text)
            except ValueError:
                return None
            return parsed if isinstance(parsed, list) else None
        return value

    model_config = ConfigDict(from_attributes=True)


class CharacterGenerateRequest(BaseModel):
    """AI生成角色的请求模型"""
    project_id: str = Field(..., description="项目ID")
    name: Optional[str] = Field(None, description="角色名称")
    role_type: Optional[str] = Field(None, description="角色类型")
    background: Optional[str] = Field(None, description="角色背景")
    requirements: Optional[str] = Field(None, description="特殊要求")
    enable_mcp: bool = Field(True, description="是否启用MCP工具增强（搜索人物原型参考）")


class CharacterListResponse(BaseModel):
    """角色列表响应模型"""
    total: int
    items: List[CharacterResponse]


class CharacterProfileRefreshRequest(BaseModel):
    """档案同步请求（issue #174 / #175）"""
    through_chapter: Optional[int] = Field(
        None, description="同步到第几章（不传则取项目内已写完的最大章节号）"
    )
    include_world: bool = Field(True, description="是否同时刷新世界设定")
    force: bool = Field(
        False,
        description="强制覆盖：忽略历史值与快照对照，整段重写（默认保护用户手改/历史值）",
    )


class CharacterProfileRefreshPreviewRequest(BaseModel):
    """档案重算预览请求（dry-run，只产出差异计划，不写库）。"""
    mode: Literal["incremental", "full"] = Field(
        "incremental", description="重算模式：incremental（增量）或 full（全量）"
    )
    from_chapter: int = Field(1, ge=1, description="起始章节号")


class CharacterProfileRefreshApplyRequest(BaseModel):
    """档案重算 apply 请求：回放某个预览任务产出的差异计划。"""
    preview_task_id: str = Field(..., description="预览任务ID")

