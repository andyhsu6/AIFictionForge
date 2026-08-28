"""delete_career 回归测试：孤儿关联行（角色已删除）不应阻塞职业删除。

背景：character_careers 存在孤儿行（character 已删除，但 SQLite foreign_keys=OFF
导致 CASCADE 未生效）。delete_career 的引用检查若 count 全部关联行，会误报
"被N个角色使用" 而拒绝删除。修复后应只统计仍存在的角色（JOIN characters）。
"""
import os
import uuid

import pytest
from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from starlette.requests import Request

from app.database import Base
from app.models.career import Career, CharacterCareer
from app.models.character import Character
from app.models.project import Project
from app.api.careers import delete_career


@pytest.fixture
async def db_session():
    """临时文件 SQLite（避免 in-memory 多连接问题），测试后清理。"""
    db_path = f"/tmp/test_careers_{uuid.uuid4().hex}.db"
    engine = create_async_engine(f"sqlite+aiosqlite:///{db_path}")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    Session = async_sessionmaker(bind=engine, expire_on_commit=False)
    async with Session() as session:
        yield session
    await engine.dispose()
    if os.path.exists(db_path):
        os.remove(db_path)


def make_request(user_id: str) -> Request:
    """构造最小 Request，模拟已登录用户。"""
    req = Request({"type": "http", "method": "DELETE", "path": "/", "headers": []})
    req.state.user_id = user_id
    return req


@pytest.mark.anyio
async def test_delete_career_ignores_orphan_references(db_session):
    """孤儿关联行（character 已删除）不应阻塞 delete_career。"""
    user_id = "user-1"
    project = Project(id="proj-1", user_id=user_id, title="测试项目")
    career = Career(id="career-1", project_id="proj-1", name="剑士", type="main", stages="[]", max_stage=10)
    # 孤儿行：character_id 指向不存在的角色（模拟角色已删除但关联行残留）
    orphan = CharacterCareer(id="cc-1", character_id="ghost-char", career_id="career-1", career_type="main")
    db_session.add_all([project, career, orphan])
    await db_session.commit()

    result = await delete_career(career_id="career-1", request=make_request(user_id), db=db_session)

    assert result == {"message": "职业删除成功"}
    remaining = await db_session.execute(select(Career).where(Career.id == "career-1"))
    assert remaining.scalar_one_or_none() is None


@pytest.mark.anyio
async def test_delete_career_blocks_on_existing_character(db_session):
    """真实引用（character 仍存在）应阻塞 delete_career。"""
    user_id = "user-1"
    project = Project(id="proj-1", user_id=user_id, title="测试项目")
    career = Career(id="career-1", project_id="proj-1", name="剑士", type="main", stages="[]", max_stage=10)
    character = Character(id="char-1", project_id="proj-1", name="张三")
    link = CharacterCareer(id="cc-1", character_id="char-1", career_id="career-1", career_type="main")
    db_session.add_all([project, career, character, link])
    await db_session.commit()

    with pytest.raises(HTTPException) as exc_info:
        await delete_career(career_id="career-1", request=make_request(user_id), db=db_session)

    assert exc_info.value.status_code == 400
    assert "角色使用" in exc_info.value.detail
