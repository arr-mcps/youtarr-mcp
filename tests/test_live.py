"""Opt-in read-only integration tests against a real Youtarr v1.95.0 instance."""

import os

import pytest
from fastmcp import Client

import youtarr_mcp

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        not os.environ.get("YOUTARR_URL")
        or not (
            os.environ.get("YOUTARR_TOKEN")
            or (os.environ.get("YOUTARR_USERNAME") and os.environ.get("YOUTARR_PASSWORD"))
        ),
        reason="requires YOUTARR_URL and YOUTARR_TOKEN or YOUTARR_USERNAME/YOUTARR_PASSWORD",
    ),
]


@pytest.fixture(autouse=True)
async def configure_client():
    client = youtarr_mcp.build_client(
        os.environ["YOUTARR_URL"],
        os.environ.get("YOUTARR_USERNAME"),
        os.environ.get("YOUTARR_PASSWORD"),
        os.environ.get("YOUTARR_TOKEN"),
    )
    youtarr_mcp._client = client
    yield
    await client.aclose()
    youtarr_mcp._client = None


async def call(group: str, operation: str, arguments: dict | None = None):
    async with Client(youtarr_mcp.mcp) as client:
        return await client.call_tool(group, {"operation": operation, "arguments": arguments or {}})


async def test_health_and_status():
    health = await call("youtarr_system", "youtarr_health")
    assert health.data["status"] == "healthy"
    status = await call("youtarr_system", "youtarr_status_summary")
    assert isinstance(status.data["version"], str)


async def test_storage_and_config():
    storage = await call("youtarr_system", "youtarr_storage_status")
    assert storage.data["total"] >= storage.data["used"]
    config = await call("youtarr_config", "youtarr_get_config")
    assert "passwordHash" not in config.data


async def test_channels_and_videos():
    channels = await call("youtarr_subscriptions", "youtarr_list_channels", {"page_size": 1})
    assert isinstance(channels.data, dict)
    videos = await call("youtarr_videos", "youtarr_list_videos", {"limit": 1})
    assert isinstance(videos.data, dict)


async def test_jobs_schedules_and_media_status():
    jobs = await call("youtarr_jobs", "youtarr_list_running_jobs")
    assert isinstance(jobs.data, list)
    schedules = await call("youtarr_maintenance", "youtarr_list_schedules")
    assert isinstance(schedules.data["tasks"], list)
    media = await call("youtarr_media_servers", "youtarr_get_media_server_status")
    assert isinstance(media.data["jellyfin"], bool)
