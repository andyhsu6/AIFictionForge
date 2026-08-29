import pytest

from app.api.changelog import get_changelog


@pytest.mark.anyio
async def test_local_changelog_returns_markdown():
    result = await get_changelog()
    assert result["entries"]
    assert "AIFictionForge" in result["entries"][0]["message"]
