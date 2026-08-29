"""项目管理页智能体 API。"""
import asyncio
from datetime import datetime
import json
from typing import AsyncGenerator
from urllib.parse import quote

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import Response, StreamingResponse
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.api.common import verify_project_access
from app.api.settings import get_user_ai_service
from app.models.project_agent import (
    AgentConversation,
    AgentExecutionStep,
    AgentMessage,
    AgentToolCall,
)
from app.models.chapter import Chapter
from app.models.character import Character
from app.schemas.project_agent import (
    AgentChatRequest,
    AgentConversationCreate,
    AgentConversationDetail,
    AgentConversationResponse,
    AgentToolCallResponse,
    AgentToolDecisionResponse,
)
from app.services.ai_service import AIService
from app.services.project_agent_service import (
    ProjectAgentService,
    build_mcp_tool_preview,
    execute_mcp_tool_call,
)
from app.services.project_agent_tools import ProjectAgentToolRegistry, normalize_tool_preview
from app.services.import_export_service import ImportExportService
from app.services.outline_transfer_service import OutlineTransferService
from app.utils.sse_response import SSEResponse


router = APIRouter(prefix="/projects/{project_id}/agent", tags=["灵创创作助手"])


def _user_id(request: Request) -> str:
    user_id = getattr(request.state, "user_id", None)
    if not user_id:
        raise HTTPException(status_code=401, detail="未登录")
    return user_id


@router.get("/exports/{export_type}", summary="下载灵创创作助手导出的项目数据")
async def download_agent_export(
    project_id: str,
    export_type: str,
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    """通过可点击 GET 地址导出数据，避免把大文件塞进模型工具上下文。"""
    user_id = _user_id(request)
    project = await verify_project_access(project_id, user_id, db)
    safe_title = "".join(
        char for char in project.title if char.isalnum() or char in (" ", "-", "_")
    ) or "project"

    if export_type == "project":
        document = await ImportExportService.export_project(
            project_id=project.id,
            db=db,
            include_generation_history=True,
            include_writing_styles=True,
            include_careers=True,
            include_memories=True,
            include_plot_analysis=True,
        )
        content = document.model_dump_json(indent=2, exclude_none=True, by_alias=True).encode("utf-8")
        filename = f"project_{safe_title}.json"
        media_type = "application/json; charset=utf-8"
    elif export_type == "outlines":
        document = await OutlineTransferService.export_outlines(project, None, db)
        content = document.model_dump_json(indent=2, exclude_none=True).encode("utf-8")
        filename = f"outlines_{safe_title}.json"
        media_type = "application/json; charset=utf-8"
    elif export_type == "chapters":
        chapters = (await db.execute(
            select(Chapter).where(Chapter.project_id == project.id).order_by(Chapter.chapter_number)
        )).scalars().all()
        text = "\n\n".join(
            f"第{chapter.chapter_number}章 {chapter.title}\n\n{chapter.content or ''}"
            for chapter in chapters
        )
        content = text.encode("utf-8")
        filename = f"chapters_{safe_title}.txt"
        media_type = "text/plain; charset=utf-8"
    elif export_type == "characters":
        character_ids = list((await db.execute(select(Character.id).where(
            Character.project_id == project.id
        ))).scalars().all())
        if character_ids:
            document = await ImportExportService.export_characters(character_ids, db)
        else:
            document = {
                "version": ImportExportService.CURRENT_VERSION,
                "export_type": "characters",
                "count": 0,
                "data": [],
            }
        content = json.dumps(document, ensure_ascii=False, indent=2).encode("utf-8")
        filename = f"characters_{safe_title}.json"
        media_type = "application/json; charset=utf-8"
    else:
        raise HTTPException(status_code=404, detail="不支持的导出类型")

    return Response(
        content=content,
        media_type=media_type,
        headers={"Content-Disposition": f"attachment; filename*=UTF-8''{quote(filename)}"},
    )


async def _get_conversation(
    db: AsyncSession,
    *,
    conversation_id: str,
    project_id: str,
    user_id: str,
) -> AgentConversation:
    result = await db.execute(
        select(AgentConversation).where(
            AgentConversation.id == conversation_id,
            AgentConversation.project_id == project_id,
            AgentConversation.user_id == user_id,
        )
    )
    conversation = result.scalar_one_or_none()
    if conversation is None:
        raise HTTPException(status_code=404, detail="对话不存在")
    return conversation


@router.get("/conversations", response_model=list[AgentConversationResponse])
async def list_conversations(
    project_id: str,
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    user_id = _user_id(request)
    await verify_project_access(project_id, user_id, db)
    result = await db.execute(
        select(AgentConversation)
        .where(
            AgentConversation.project_id == project_id,
            AgentConversation.user_id == user_id,
            AgentConversation.status == "active",
        )
        .order_by(AgentConversation.last_message_at.desc())
        .limit(100)
    )
    return result.scalars().all()


@router.post("/conversations", response_model=AgentConversationResponse)
async def create_conversation(
    project_id: str,
    payload: AgentConversationCreate,
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    user_id = _user_id(request)
    await verify_project_access(project_id, user_id, db)
    conversation = AgentConversation(
        user_id=user_id,
        project_id=project_id,
        title=(payload.title or "新对话").strip()[:200] or "新对话",
    )
    db.add(conversation)
    await db.commit()
    await db.refresh(conversation)
    return conversation


@router.get("/conversations/{conversation_id}", response_model=AgentConversationDetail)
async def get_conversation_detail(
    project_id: str,
    conversation_id: str,
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    user_id = _user_id(request)
    await verify_project_access(project_id, user_id, db)
    conversation = await _get_conversation(
        db, conversation_id=conversation_id, project_id=project_id, user_id=user_id
    )
    messages = (
        await db.execute(
            select(AgentMessage)
            .where(AgentMessage.conversation_id == conversation.id)
            .order_by(AgentMessage.created_at)
        )
    ).scalars().all()
    tool_calls = (
        await db.execute(
            select(AgentToolCall)
            .where(AgentToolCall.conversation_id == conversation.id)
            .order_by(AgentToolCall.created_at)
        )
    ).scalars().all()
    execution_steps = (
        await db.execute(
            select(AgentExecutionStep)
            .where(AgentExecutionStep.conversation_id == conversation.id)
            .order_by(AgentExecutionStep.created_at, AgentExecutionStep.sequence)
        )
    ).scalars().all()
    tool_call_responses = []
    for tool_call in tool_calls:
        response = AgentToolCallResponse.model_validate(tool_call).model_dump()
        response["preview"] = normalize_tool_preview(tool_call.preview, tool_call.arguments)
        tool_call_responses.append(response)
    return {
        **AgentConversationResponse.model_validate(conversation).model_dump(),
        "messages": messages,
        "tool_calls": tool_call_responses,
        "execution_steps": execution_steps,
    }


@router.delete("/conversations/{conversation_id}", status_code=204)
async def delete_conversation(
    project_id: str,
    conversation_id: str,
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    user_id = _user_id(request)
    await verify_project_access(project_id, user_id, db)
    conversation = await _get_conversation(
        db, conversation_id=conversation_id, project_id=project_id, user_id=user_id
    )
    await db.delete(conversation)
    await db.commit()


@router.post("/chat-stream")
async def chat_stream(
    project_id: str,
    payload: AgentChatRequest,
    request: Request,
    db: AsyncSession = Depends(get_db),
    ai_service: AIService = Depends(get_user_ai_service),
):
    user_id = _user_id(request)
    project = await verify_project_access(project_id, user_id, db)
    service = ProjectAgentService(
        db=db,
        ai_service=ai_service,
        project=project,
        user_id=user_id,
    )

    async def event_stream() -> AsyncGenerator[str, None]:
        try:
            async for event in service.stream_chat(
                conversation_id=payload.conversation_id,
                message=payload.message,
                page_context=payload.page_context,
                auto_approve=payload.auto_approve,
            ):
                yield SSEResponse.format_sse(event)
            yield await SSEResponse.send_done()
        except (asyncio.CancelledError, GeneratorExit):
            await service.finalize_interrupted_turn("连接已中断", cancelled=True)
            raise
        except ValueError as exc:
            await service.finalize_interrupted_turn(str(exc), cancelled=False)
            yield await SSEResponse.send_error(str(exc), 400)
            yield await SSEResponse.send_done()
        except Exception as exc:
            await service.finalize_interrupted_turn(str(exc), cancelled=False)
            yield await SSEResponse.send_error(f"灵创创作助手执行失败：{exc}", 500)
            yield await SSEResponse.send_done()

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


async def _claim_tool_call(
    db: AsyncSession,
    *,
    tool_call_id: str,
    project_id: str,
    user_id: str,
    claimed_status: str,
) -> AgentToolCall:
    """通过条件 UPDATE 原子抢占请求，兼容 SQLite 不支持行锁的情况。"""
    now = datetime.now()
    result = await db.execute(
        update(AgentToolCall)
        .where(
            AgentToolCall.id == tool_call_id,
            AgentToolCall.project_id == project_id,
            AgentToolCall.user_id == user_id,
            AgentToolCall.status == "waiting_confirmation",
        )
        .values(
            status=claimed_status,
            confirmed_at=now if claimed_status == "executing" else None,
            error_message=None,
        )
    )
    if result.rowcount != 1:
        await db.rollback()
        existing = (await db.execute(select(AgentToolCall.id).where(
            AgentToolCall.id == tool_call_id,
            AgentToolCall.project_id == project_id,
            AgentToolCall.user_id == user_id,
        ))).scalar_one_or_none()
        if existing is None:
            raise HTTPException(status_code=404, detail="工具调用不存在")
        raise HTTPException(status_code=409, detail="该修改已处理或正在执行")
    await db.commit()
    tool_call = (await db.execute(select(AgentToolCall).where(
        AgentToolCall.id == tool_call_id,
        AgentToolCall.project_id == project_id,
        AgentToolCall.user_id == user_id,
    ))).scalar_one()
    await _get_conversation(
        db, conversation_id=tool_call.conversation_id,
        project_id=project_id, user_id=user_id,
    )
    return tool_call


async def _restore_waiting_tool_call(
    db: AsyncSession, tool_call: AgentToolCall, *, error: str | None = None
) -> None:
    tool_call.status = "waiting_confirmation"
    tool_call.confirmed_at = None
    tool_call.error_message = error
    await db.commit()


@router.post("/tool-calls/{tool_call_id}/confirm", response_model=AgentToolDecisionResponse)
async def confirm_tool_call(
    project_id: str,
    tool_call_id: str,
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    user_id = _user_id(request)
    project = await verify_project_access(project_id, user_id, db)
    tool_call = await _claim_tool_call(
        db, tool_call_id=tool_call_id, project_id=project_id,
        user_id=user_id, claimed_status="executing",
    )
    registry = ProjectAgentToolRegistry(project, db)
    is_mcp = False
    try:
        registry.get(tool_call.tool_name)
    except ValueError:
        is_mcp = bool(
            tool_call.requires_confirmation
            and isinstance(tool_call.preview, dict)
            and tool_call.preview.get("entity_type") == "mcp_tool"
        )
        if not is_mcp:
            await _restore_waiting_tool_call(db, tool_call, error="工具已不再可用")
            raise HTTPException(status_code=409, detail="工具已不再可用")
        from app.services.mcp_tools_loader import mcp_tools_loader

        available_mcp_tools = await mcp_tools_loader.get_user_tools(
            user_id, db, use_cache=False, force_refresh=True
        )
        available_names = {
            item.get("function", {}).get("name") for item in (available_mcp_tools or [])
        }
        if tool_call.tool_name not in available_names:
            await _restore_waiting_tool_call(db, tool_call, error="MCP 工具已禁用或不可用")
            raise HTTPException(status_code=409, detail="MCP 工具已禁用或不可用")

    try:
        current_preview = (
            build_mcp_tool_preview(tool_call.tool_name, tool_call.arguments)
            if is_mcp
            else await registry.preview(tool_call.tool_name, tool_call.arguments)
        )
    except ValueError as exc:
        await _restore_waiting_tool_call(db, tool_call, error=str(exc))
        raise HTTPException(status_code=409, detail=f"修改目标已变化：{exc}") from exc

    stored_preview = normalize_tool_preview(tool_call.preview, tool_call.arguments)
    if current_preview != stored_preview:
        tool_call.preview = current_preview
        await _restore_waiting_tool_call(db, tool_call, error="数据已发生变化")
        raise HTTPException(status_code=409, detail="数据已发生变化，差异预览已刷新，请重新确认")
    if tool_call.preview != current_preview:
        tool_call.preview = current_preview

    try:
        if is_mcp:
            result = await execute_mcp_tool_call(
                user_id, tool_call.tool_name, tool_call.arguments, tool_call.id
            )
            if not result.get("success"):
                raise RuntimeError(result.get("error") or "MCP 工具调用失败")
        else:
            result = await registry.execute(tool_call.tool_name, tool_call.arguments)
        tool_call.status = "executed"
        tool_call.result = result
        tool_call.before_snapshot = result.get("before")
        tool_call.after_snapshot = result.get("after")
        tool_call.error_message = None
        tool_call.executed_at = datetime.now()
        await db.execute(
            update(AgentExecutionStep)
            .where(AgentExecutionStep.tool_call_id == tool_call.id)
            .values(
                status="completed",
                content="修改已由用户确认并执行。",
                updated_at=datetime.now(),
            )
        )
        await db.execute(
            update(AgentConversation)
            .where(AgentConversation.id == tool_call.conversation_id)
            .values(last_message_at=datetime.now())
        )
        result_message = result.get("message") or (
            "MCP 工具已确认并执行。" if is_mcp else "修改已确认并执行。"
        )
        message = AgentMessage(
            conversation_id=tool_call.conversation_id,
            role="assistant",
            content=result_message,
        )
        db.add(message)
        await db.commit()
        await db.refresh(tool_call)
        return {
            "success": True,
            "message": result_message,
            "tool_call": tool_call,
            "resources": result.get("resources") or [],
        }
    except HTTPException:
        raise
    except Exception as exc:
        await db.rollback()
        refreshed = (await db.execute(select(AgentToolCall).where(
            AgentToolCall.id == tool_call_id
        ))).scalar_one_or_none()
        if refreshed is not None and refreshed.status == "executing":
            await _restore_waiting_tool_call(db, refreshed, error=str(exc))
        raise HTTPException(status_code=400, detail=f"执行修改失败：{exc}") from exc


@router.post("/tool-calls/{tool_call_id}/reject", response_model=AgentToolDecisionResponse)
async def reject_tool_call(
    project_id: str,
    tool_call_id: str,
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    user_id = _user_id(request)
    await verify_project_access(project_id, user_id, db)
    tool_call = await _claim_tool_call(
        db, tool_call_id=tool_call_id, project_id=project_id,
        user_id=user_id, claimed_status="rejecting",
    )
    tool_call.status = "rejected"
    await db.execute(
        update(AgentExecutionStep)
        .where(AgentExecutionStep.tool_call_id == tool_call.id)
        .values(
            status="rejected",
            content="用户已取消本次修改，项目数据没有变化。",
            updated_at=datetime.now(),
        )
    )
    await db.execute(
        update(AgentConversation)
        .where(AgentConversation.id == tool_call.conversation_id)
        .values(last_message_at=datetime.now())
    )
    message = AgentMessage(
        conversation_id=tool_call.conversation_id,
        role="assistant",
        content="已取消这次修改，项目数据没有变化。",
    )
    db.add(message)
    await db.commit()
    await db.refresh(tool_call)
    return {
        "success": True,
        "message": "已取消修改",
        "tool_call": tool_call,
        "resources": [],
    }
