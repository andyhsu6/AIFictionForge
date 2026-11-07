"""写作风格 Schema"""
from pydantic import BaseModel, Field, ConfigDict
from typing import Any, Dict, Optional
from datetime import datetime


class WritingStyleBase(BaseModel):
    """写作风格基础模型"""
    name: str = Field(..., description="风格名称")
    style_type: str = Field(..., description="风格类型：preset/custom")
    preset_id: Optional[str] = Field(None, description="预设风格ID")
    description: Optional[str] = Field(None, description="风格描述")
    prompt_content: str = Field(..., description="风格提示词内容")


class WritingStyleCreate(BaseModel):
    """创建写作风格（仅用于创建用户自定义风格）"""
    name: str = Field(..., description="风格名称")
    style_type: Optional[str] = Field(None, description="风格类型：preset/custom")
    preset_id: Optional[str] = Field(None, description="预设风格ID")
    description: Optional[str] = Field(None, description="风格描述")
    prompt_content: str = Field(..., description="风格提示词内容")


class WritingStyleUpdate(BaseModel):
    """更新写作风格"""
    name: Optional[str] = None
    description: Optional[str] = None
    prompt_content: Optional[str] = None


class SetDefaultStyleRequest(BaseModel):
    """设置默认风格请求"""
    project_id: str = Field(..., description="项目ID")


class WritingStyleResponse(BaseModel):
    """写作风格响应"""
    id: int
    user_id: Optional[str] = None  # NULL 表示全局预设风格
    name: str
    style_type: str
    preset_id: Optional[str] = None
    description: Optional[str] = None
    prompt_content: str
    profile_json: Optional[str] = None
    profile_version: Optional[int] = None
    is_default: bool
    order_index: int
    created_at: datetime
    updated_at: datetime
    
    model_config = ConfigDict(from_attributes=True)


class WritingStyleListResponse(BaseModel):
    """写作风格列表响应"""
    total: int
    styles: list[WritingStyleResponse]


class ProfileReextractRequest(BaseModel):
    """重新抽取项目文笔画像（issue #254 P1）"""

    dry_run: bool = Field(default=True, description="True 只返回 diff 预览，不写库")
    overwrite_edited: bool = Field(default=False, description="是否覆盖用户已编辑的画像")
    set_default: bool = Field(default=False, description="写库后是否将画像设为项目默认风格")


class ProfileReextractResponse(BaseModel):
    """重新抽取结果：dry_run 返回 diff 预览，写库返回更新结果"""

    skipped: bool = Field(default=False, description="样本不足，未抽取也未写库")
    changed: bool = Field(default=False, description="新画像与现有画像（prompt_content）是否不同")
    old_prompt_content: Optional[str] = None
    new_prompt_content: Optional[str] = None
    old_profile: Optional[Dict[str, Any]] = None
    new_profile: Optional[Dict[str, Any]] = None
    description: Optional[str] = None
    confidence: Optional[float] = None
    degraded: bool = False
    updated: bool = Field(default=False, description="是否已写库")
    style_id: Optional[int] = None
    kept_reason: Optional[str] = Field(
        default=None, description="未写库原因：user_edited=用户已手改且未授权覆盖"
    )