"""Skill 聊天 API

提供 Skill 列表查询和基于 Skill 的流式聊天功能。
用户选择一个 Skill 后，以该 Skill 的工作流指令作为系统提示词进行对话。
"""
from fastapi import APIRouter, Request, Depends
from sqlalchemy.ext.asyncio import AsyncSession
from pydantic import BaseModel
from typing import Optional, List, Dict

from app.database import get_db
from app.user_manager import User
from app.api.settings import require_login
from app.services.skill_loader import get_all_skills_cached, get_skill_by_trigger, get_skill_detail, create_skill_files, update_skill_files, delete_skill_files, refresh_skills_cache
from app.services.ai_service import AIService, create_user_ai_service
from app.utils.sse_response import SSEResponse, create_sse_response, wrap_stream_with_heartbeat, HEARTBEAT
from app.logger import get_logger

logger = get_logger(__name__)
router = APIRouter(prefix="/api/skills", tags=["Skills"])


class SkillChatRequest(BaseModel):
    """Skill 聊天请求"""
    skill_key: str  # SKILL_STORY_LONG_WRITE 等
    message: str    # 用户消息
    history: Optional[List[dict]] = None  # 历史对话 [{"role": "user/assistant", "content": "..."}]


class SkillCreateRequest(BaseModel):
    """创建 Skill 请求"""
    name: str           # Skill 名称（英文，如 my-new-skill）
    description: str    # Skill 描述
    body: str           # 工作流指令（Markdown 正文）
    references: Optional[Dict[str, str]] = None  # 参考知识库 {"文件名": "内容"}


class SkillUpdateRequest(BaseModel):
    """更新 Skill 请求"""
    description: Optional[str] = None
    body: Optional[str] = None
    references: Optional[Dict[str, str]] = None


@router.get("/list")
async def list_skills(user: User = Depends(require_login)):
    """获取所有可用 Skill 列表"""
    skills = get_all_skills_cached()
    return [
        {
            "template_key": s["template_key"],
            "template_name": s["template_name"],
            "category": s["category"],
            "description": s["description"],
            "triggers": s.get("triggers", []),
        }
        for s in skills
    ]


@router.post("/match")
async def match_skill(request: Request, user: User = Depends(require_login)):
    """根据用户输入匹配最合适的 Skill"""
    body = await request.json()
    user_input = body.get("user_input", "")

    if not user_input:
        return {"matched": False}

    skill = get_skill_by_trigger(user_input)
    if skill:
        return {
            "matched": True,
            "skill": {
                "template_key": skill["template_key"],
                "template_name": skill["template_name"],
                "category": skill["category"],
                "description": skill["description"],
            }
        }
    return {"matched": False}


@router.post("/chat")
async def skill_chat(
    request: SkillChatRequest,
    user: User = Depends(require_login),
    db: AsyncSession = Depends(get_db),
):
    """
    基于 Skill 的流式聊天

    接收用户消息和 Skill 标识，以 Skill 内容作为系统提示词，
    通过用户的 AI 配置进行流式回复。
    """
    # 查找 Skill
    skills = get_all_skills_cached()
    skill = None
    for s in skills:
        if s["template_key"] == request.skill_key:
            skill = s
            break

    if not skill:
        async def error_gen():
            yield await SSEResponse.send_error(f"未找到 Skill: {request.skill_key}")
        return create_sse_response(error_gen())

    # 获取系统提示词（Skill 内容）
    system_prompt = skill["content"]

    # 构建完整提示词（将历史消息拼接到提示词中）
    history_text = ""
    if request.history:
        for msg in request.history[-20:]:
            role_label = "用户" if msg.get("role") == "user" else "助手"
            history_text += f"\n{role_label}: {msg.get('content', '')}"

    full_prompt = request.message
    if history_text:
        full_prompt = f"以下是之前的对话历史：{history_text}\n\n用户最新消息: {request.message}"

    # 获取用户 AI 配置
    from app.api.settings import get_user_ai_service
    try:
        ai_service = await get_user_ai_service(user=user, db=db)
        # 覆盖系统提示词为 Skill 内容
        ai_service.default_system_prompt = system_prompt
    except Exception as e:
        logger.error(f"创建 AI 服务失败: {e}")
        async def error_gen():
            yield await SSEResponse.send_error(f"AI 服务配置错误: {str(e)}")
        return create_sse_response(error_gen())

    # 流式生成
    async def generate():
        try:
            yield await SSEResponse.send_progress(f"正在使用 {skill['template_name']}...", 10)

            stream = ai_service.generate_text_stream(
                prompt=full_prompt,
                system_prompt=system_prompt,
                auto_mcp=False,  # Skill 聊天不使用 MCP 工具
            )

            async for item in wrap_stream_with_heartbeat(stream, heartbeat_interval=15.0):
                if item is HEARTBEAT:
                    yield await SSEResponse.send_heartbeat()
                    continue
                yield await SSEResponse.send_chunk(item)

            yield await SSEResponse.send_progress("回复完成", 100, "success")
            yield await SSEResponse.send_done()

        except Exception as e:
            logger.error(f"Skill 聊天生成失败: {e}")
            yield await SSEResponse.send_error(f"生成失败: {str(e)}")

    return create_sse_response(generate())


# ==================== Skill 管理 CRUD API ====================

@router.get("/detail/{skill_key:path}")
async def get_skill_detail_api(skill_key: str, user: User = Depends(require_login)):
    """获取 Skill 详细信息（包括原始内容和 references）"""
    detail = get_skill_detail(skill_key)
    if not detail:
        from fastapi import HTTPException
        raise HTTPException(status_code=404, detail=f"未找到 Skill: {skill_key}")
    
    return {
        "template_key": detail["template_key"],
        "template_name": detail["template_name"],
        "category": detail["category"],
        "description": detail["description"],
        "triggers": detail.get("triggers", []),
        "raw_content": detail.get("raw_content", ""),
        "standalone_references": detail.get("standalone_references", {}),
    }


@router.post("/create")
async def create_skill(request: SkillCreateRequest, user: User = Depends(require_login)):
    """创建新的 Skill"""
    try:
        result = create_skill_files(
            name=request.name,
            description=request.description,
            body=request.body,
            references=request.references,
        )
        return {"success": True, "skill": result}
    except ValueError as e:
        from fastapi import HTTPException
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        logger.error(f"创建 Skill 失败: {e}")
        from fastapi import HTTPException
        raise HTTPException(status_code=500, detail=f"创建失败: {str(e)}")


@router.put("/update/{skill_key:path}")
async def update_skill(skill_key: str, request: SkillUpdateRequest, user: User = Depends(require_login)):
    """更新 Skill"""
    try:
        result = update_skill_files(
            skill_key=skill_key,
            description=request.description,
            body=request.body,
            references=request.references,
        )
        return {"success": True, "skill": result}
    except ValueError as e:
        from fastapi import HTTPException
        raise HTTPException(status_code=404, detail=str(e))
    except Exception as e:
        logger.error(f"更新 Skill 失败: {e}")
        from fastapi import HTTPException
        raise HTTPException(status_code=500, detail=f"更新失败: {str(e)}")


@router.delete("/delete/{skill_key:path}")
async def delete_skill(skill_key: str, user: User = Depends(require_login)):
    """删除 Skill"""
    try:
        delete_skill_files(skill_key)
        return {"success": True, "message": f"已删除 Skill: {skill_key}"}
    except ValueError as e:
        from fastapi import HTTPException
        raise HTTPException(status_code=404, detail=str(e))
    except Exception as e:
        logger.error(f"删除 Skill 失败: {e}")
        from fastapi import HTTPException
        raise HTTPException(status_code=500, detail=f"删除失败: {str(e)}")


@router.post("/refresh-cache")
async def refresh_cache(user: User = Depends(require_login)):
    """手动刷新 Skill 缓存"""
    skills = refresh_skills_cache()
    return {"success": True, "count": len(skills)}
