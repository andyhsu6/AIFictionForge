"""export_project_chapters 回归测试：ApiError 化后不得被 except 兜底重包成 500。

背景（i18n todo 12 后续）：raise HTTPException → raise ApiError 转换后，
若 try 块只有 except HTTPException: raise，转换出的 ApiError 会落入
except Exception 兜底，被重包为 500 {"detail": "导出失败: ..."}。
修复后 handler 加宽为 except (HTTPException, ApiError): raise，
客户端错误（404 not_found.project_chapters）应原样透传。
"""
import os
import uuid

import pytest
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.core.errors import register_exception_handlers
from app.database import Base, get_db
from app.models.project import Project
from app.api.projects import router as projects_router


@pytest.fixture
def client():
    """临时文件 SQLite + 真实 projects 路由 + 生产同款全局 handler。"""
    import asyncio

    db_path = f"/tmp/test_projects_export_{uuid.uuid4().hex}.db"
    engine = create_async_engine(f"sqlite+aiosqlite:///{db_path}")

    async def _seed():
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        Session = async_sessionmaker(bind=engine, expire_on_commit=False)
        async with Session() as session:
            session.add(Project(id="proj-1", user_id="user-1", title="测试项目"))
            await session.commit()

    asyncio.run(_seed())

    async def override_get_db():
        Session = async_sessionmaker(bind=engine, expire_on_commit=False)
        async with Session() as session:
            yield session

    app = FastAPI()
    register_exception_handlers(app)
    app.include_router(projects_router)
    app.dependency_overrides[get_db] = override_get_db

    @app.middleware("http")
    async def fake_auth(request: Request, call_next):
        """模拟 auth 中间件：注入已登录用户。"""
        request.state.user_id = "user-1"
        return await call_next(request)

    yield TestClient(app, raise_server_exceptions=False)

    app.dependency_overrides.clear()
    asyncio.run(engine.dispose())
    if os.path.exists(db_path):
        os.remove(db_path)


def test_export_project_without_chapters_returns_404_envelope(client):
    """零章节项目导出：应 404 + not_found.project_chapters envelope，而非 500 导出失败。"""
    resp = client.get("/projects/proj-1/export")

    assert resp.status_code == 404
    body = resp.json()
    assert body["code"] == "not_found.project_chapters"
    assert body["detail"] == "项目没有任何章节"
    assert "导出失败" not in resp.text
