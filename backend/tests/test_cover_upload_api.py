"""自定义封面上传接口与服务测试"""
from __future__ import annotations

import asyncio
import uuid
from pathlib import Path

import pytest
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.api import project_covers
from app.core.errors import ERROR_REGISTRY, register_exception_handlers
from app.database import Base, get_db
from app.models.project import Project
from app.services import cover_generation_service as cover_service_module

PNG_BYTES = b"\x89PNG\r\n\x1a\n" + b"\x00\x00\x00\rIHDR" + b"\x00" * 17
JPEG_BYTES = b"\xff\xd8\xff\xe0" + b"\x00\x10JFIF\x00\x01" + b"\xff\xd9"
WEBP_BYTES = b"RIFF\x18\x00\x00\x00WEBPVP8 \x0c\x00\x00\x00" + b"\x00" * 8


def test_cover_upload_error_codes_registered():
    assert ERROR_REGISTRY["validation.cover_file_invalid"] == (
        "封面图片仅支持 PNG、JPG 或 WebP 格式",
        400,
    )
    assert "validation.file_too_large" in ERROR_REGISTRY


@pytest.fixture
def test_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    db_path = tmp_path / f"cover_upload_{uuid.uuid4().hex}.db"
    engine = create_async_engine(f"sqlite+aiosqlite:///{db_path}")
    session_factory = async_sessionmaker(bind=engine, expire_on_commit=False)

    async def _init():
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        async with session_factory() as session:
            session.add_all(
                [
                    Project(
                        id="proj-ready",
                        user_id="user-1",
                        title="Project One",
                        cover_status="none",
                        cover_prompt="old ai prompt",
                        cover_error="old error",
                    ),
                    Project(
                        id="proj-generating",
                        user_id="user-1",
                        title="Project Generating",
                        cover_status="generating",
                    ),
                    Project(
                        id="proj-other",
                        user_id="user-2",
                        title="Project Other",
                        cover_status="none",
                    ),
                ]
            )
            await session.commit()

    asyncio.run(_init())

    cover_dir = tmp_path / "generated_covers"
    monkeypatch.setattr(cover_service_module, "GENERATED_COVER_STORAGE_DIR", cover_dir)

    app = FastAPI()
    register_exception_handlers(app)
    current_user = {"id": "user-1"}

    @app.middleware("http")
    async def inject_user(request: Request, call_next):
        request.state.user_id = current_user["id"]
        return await call_next(request)

    async def override_get_db():
        async with session_factory() as session:
            yield session

    app.dependency_overrides[get_db] = override_get_db
    app.include_router(project_covers.router, prefix="/api")

    with TestClient(app) as client:
        yield {
            "client": client,
            "session_factory": session_factory,
            "cover_dir": cover_dir,
            "tmp_path": tmp_path,
            "current_user": current_user,
        }

    asyncio.run(engine.dispose())


@pytest.mark.parametrize(
    ("filename", "content_type", "payload", "expected_ext"),
    [
        ("cover.png", "image/png", PNG_BYTES, ".png"),
        ("cover.jpg", "image/jpeg", JPEG_BYTES, ".jpg"),
        ("cover.webp", "image/webp", WEBP_BYTES, ".webp"),
        ("no-ext", "application/octet-stream", PNG_BYTES, ".png"),
    ],
)
def test_upload_cover_png_jpeg_webp_success_without_ai_settings(
    test_env, filename: str, content_type: str, payload: bytes, expected_ext: str
):
    client: TestClient = test_env["client"]
    resp = client.post(
        "/api/projects/proj-ready/cover/upload",
        files={"file": (filename, payload, content_type)},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["project_id"] == "proj-ready"
    assert body["cover_status"] == "ready"
    assert body["cover_image_url"].startswith("/generated-assets/covers/user-1/")
    assert body["cover_image_url"].endswith(expected_ext)
    assert body["cover_prompt"] is None

    async def _fetch():
        async with test_env["session_factory"]() as session:
            return await session.get(Project, "proj-ready")

    project = asyncio.run(_fetch())
    assert project is not None
    assert project.cover_status == "ready"
    assert project.cover_image_url == body["cover_image_url"]
    assert project.cover_prompt is None
    assert project.cover_error is None
    assert project.cover_updated_at is not None

    download_resp = client.get("/api/projects/proj-ready/cover/download")
    assert download_resp.status_code == 200
    assert download_resp.content == payload


def test_upload_cover_replaces_existing_and_removes_old_file(test_env):
    client: TestClient = test_env["client"]
    cover_dir: Path = test_env["cover_dir"]

    first_resp = client.post(
        "/api/projects/proj-ready/cover/upload",
        files={"file": ("first.png", PNG_BYTES, "image/png")},
    )
    assert first_resp.status_code == 200
    first_name = first_resp.json()["cover_image_url"].split("/")[-1]
    first_path = cover_dir / "user-1" / first_name
    assert first_path.exists()

    second_resp = client.post(
        "/api/projects/proj-ready/cover/upload",
        files={"file": ("second.jpg", JPEG_BYTES, "image/jpeg")},
    )
    assert second_resp.status_code == 200
    second_name = second_resp.json()["cover_image_url"].split("/")[-1]
    second_path = cover_dir / "user-1" / second_name
    assert second_path.exists()
    assert second_path != first_path
    assert not first_path.exists()


def test_upload_cover_ignores_missing_old_file_and_traversal_url(test_env):
    client: TestClient = test_env["client"]
    outside_secret = test_env["tmp_path"] / "outside.txt"
    outside_secret.write_text("keep-me", encoding="utf-8")

    async def _set_previous(url: str):
        async with test_env["session_factory"]() as session:
            project = await session.get(Project, "proj-ready")
            assert project is not None
            project.cover_status = "ready"
            project.cover_image_url = url
            await session.commit()

    asyncio.run(_set_previous("/generated-assets/covers/../outside.txt"))
    resp1 = client.post(
        "/api/projects/proj-ready/cover/upload",
        files={"file": ("cover.png", PNG_BYTES, "image/png")},
    )
    assert resp1.status_code == 200
    assert outside_secret.exists()

    asyncio.run(_set_previous("/generated-assets/covers/user-1/missing.png"))
    resp2 = client.post(
        "/api/projects/proj-ready/cover/upload",
        files={"file": ("cover.webp", WEBP_BYTES, "image/webp")},
    )
    assert resp2.status_code == 200


@pytest.mark.parametrize(
    "payload",
    [
        b"",
        b"not an image at all",
        b"RIFF\x10\x00\x00\x00WAVEfmt ",
        b"\x89PNG\r\n\x1a\n",
        b"\x89PNG\r\n\x1a\n\x00\x00\x00\rNOPE" + b"\x00" * 17,
        b"RIFF\x04\x00\x00\x00WEBP",
        b"\xff\xd8\xff",
    ],
)
def test_upload_cover_rejects_empty_and_invalid_or_truncated_bytes(test_env, payload: bytes):
    client: TestClient = test_env["client"]
    resp = client.post(
        "/api/projects/proj-ready/cover/upload",
        files={"file": ("fake.png", payload, "image/png")},
    )
    assert resp.status_code == 400
    assert resp.json()["code"] == "validation.cover_file_invalid"


def test_upload_cover_accepts_exact_10mb_boundary_and_rejects_oversized_file(test_env):
    client: TestClient = test_env["client"]
    exact_limit = PNG_BYTES + b"\x00" * (10 * 1024 * 1024 - len(PNG_BYTES))
    ok_resp = client.post(
        "/api/projects/proj-ready/cover/upload",
        files={"file": ("exact.png", exact_limit, "image/png")},
    )
    assert ok_resp.status_code == 200

    oversized = exact_limit + b"\x00"
    resp = client.post(
        "/api/projects/proj-ready/cover/upload",
        files={"file": ("huge.png", oversized, "image/png")},
    )
    assert resp.status_code == 413
    body = resp.json()
    assert body["code"] == "validation.file_too_large"
    assert body["params"] == {"max_mb": 10}


def test_upload_cover_rejects_when_generating(test_env):
    client: TestClient = test_env["client"]
    resp = client.post(
        "/api/projects/proj-generating/cover/upload",
        files={"file": ("cover.png", PNG_BYTES, "image/png")},
    )
    assert resp.status_code == 409
    assert resp.json()["code"] == "conflict.cover_generating"


def test_upload_cover_rejects_other_user_project(test_env):
    client: TestClient = test_env["client"]
    resp = client.post(
        "/api/projects/proj-other/cover/upload",
        files={"file": ("cover.png", PNG_BYTES, "image/png")},
    )
    assert resp.status_code == 404
    assert resp.json()["code"] == "not_found.project"
