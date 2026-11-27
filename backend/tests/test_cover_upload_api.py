"""自定义封面上传接口与服务测试"""
from __future__ import annotations

from app.core.errors import ERROR_REGISTRY


def test_cover_upload_error_codes_registered():
    assert ERROR_REGISTRY["validation.cover_file_invalid"] == (
        "封面图片仅支持 PNG、JPG 或 WebP 格式",
        400,
    )
    assert "validation.file_too_large" in ERROR_REGISTRY
