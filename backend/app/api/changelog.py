"""
更新日志API
从仓库根目录的 CHANGELOG.md 读取本地更新日志。
"""
from pathlib import Path
from fastapi import APIRouter

router = APIRouter()
CHANGELOG_PATH = Path(__file__).resolve().parents[3] / "CHANGELOG.md"


@router.get("/changelog")
async def get_changelog():
    if not CHANGELOG_PATH.exists():
        return {"entries": []}
    text = CHANGELOG_PATH.read_text(encoding="utf-8")
    return {
        "entries": [
            {
                "id": "local",
                "date": "2026-08-29",
                "message": text,
            }
        ]
    }
