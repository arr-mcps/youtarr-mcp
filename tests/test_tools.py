"""Offline tests for authentication, grouped dispatch, and Youtarr HTTP contracts."""

import asyncio
import inspect
import json
from collections.abc import Callable

import httpx
import pytest
import pytest_asyncio
from fastmcp import Client
from fastmcp.exceptions import ToolError

import youtarr_mcp


class Recorder:
    def __init__(self) -> None:
        self.requests: list[httpx.Request] = []
        self.responder: Callable[[httpx.Request], httpx.Response] = self._default_response

    @staticmethod
    def _default_response(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/auth/login":
            return httpx.Response(200, json={"token": "session-token", "expires": "2026-11-30T00:00:00Z"})
        return httpx.Response(200, json={"ok": True})

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        return self.responder(request)

    @property
    def last(self) -> httpx.Request:
        return self.requests[-1]

    def body(self, index: int = -1) -> dict:
        return json.loads(self.requests[index].content)


@pytest.fixture
def recorder() -> Recorder:
    return Recorder()


@pytest_asyncio.fixture
async def server(recorder: Recorder, monkeypatch: pytest.MonkeyPatch):
    client = youtarr_mcp.build_client(
        "https://youtarr.example.com",
        username="admin",
        password="test-password",
        transport=httpx.MockTransport(recorder.handler),
    )
    monkeypatch.setattr(youtarr_mcp, "_client", client)
    yield youtarr_mcp.mcp
    await client.aclose()


async def call(name: str, **kwargs):
    async with Client(youtarr_mcp.mcp) as client:
        return await client.call_tool(name, kwargs)


def endpoint_names() -> set[str]:
    return {
        name
        for name, value in vars(youtarr_mcp).items()
        if inspect.iscoroutinefunction(value) and name.startswith("youtarr_") and name not in youtarr_mcp._GROUPS
    }


# Registration and grouping ---------------------------------------------------


async def test_all_endpoint_functions_grouped_exactly_once():
    grouped = [name for names in youtarr_mcp._GROUPS.values() for name in names]
    assert len(grouped) == len(set(grouped))
    assert set(grouped) == endpoint_names()


async def test_registers_only_resource_scoped_tools_with_operation_literals():
    async with Client(youtarr_mcp.mcp) as client:
        tools = {tool.name: tool for tool in await client.list_tools()}
    assert 5 <= len(tools) <= 15
    assert set(tools) == set(youtarr_mcp._GROUPS)
    for group, names in youtarr_mcp._GROUPS.items():
        tool = tools[group]
        assert tool.input_schema["properties"]["operation"]["enum"] == list(names)
        assert "arguments" in tool.input_schema["properties"]
        assert all(name in tool.description for name in names)


async def test_only_all_read_only_group_has_read_only_hint():
    async with Client(youtarr_mcp.mcp) as client:
        tools = {tool.name: tool for tool in await client.list_tools()}
    assert tools["youtarr_system"].annotations.read_only_hint is True
    assert tools["youtarr_videos"].annotations is None
    assert tools["youtarr_media_servers"].annotations is None


async def test_invalid_operation_is_rejected_before_http(server, recorder):
    with pytest.raises(ToolError):
        await call("youtarr_system", operation="youtarr_list_videos", arguments={})
    assert recorder.requests == []


async def test_operation_from_another_group_is_rejected(server, recorder):
    with pytest.raises(ToolError):
        await call("youtarr_jobs", operation="youtarr_list_videos", arguments={})
    assert recorder.requests == []


# Authentication --------------------------------------------------------------


async def test_existing_token_is_sent_without_login(recorder, monkeypatch):
    client = youtarr_mcp.build_client(
        "https://youtarr.example.com",
        token="existing-token",
        transport=httpx.MockTransport(recorder.handler),
    )
    monkeypatch.setattr(youtarr_mcp, "_client", client)
    result = await call("youtarr_system", operation="youtarr_status_summary")
    assert result.data == {"ok": True}
    assert len(recorder.requests) == 1
    assert recorder.last.url.path == "/api/status/summary"
    assert recorder.last.headers["x-access-token"] == "existing-token"
    assert "x-api-key" not in recorder.last.headers
    await client.aclose()


async def test_username_password_login_then_session_header(server, recorder):
    await call("youtarr_system", operation="youtarr_status_summary")
    assert len(recorder.requests) == 2
    login, request = recorder.requests
    assert login.method == "POST"
    assert login.url.path == "/auth/login"
    assert recorder.body(0) == {"username": "admin", "password": "test-password"}
    assert "x-access-token" not in login.headers
    assert request.headers["x-access-token"] == "session-token"
    assert "x-api-key" not in request.headers


async def test_public_health_does_not_require_login(server, recorder):
    result = await call("youtarr_system", operation="youtarr_health")
    assert result.data == {"ok": True}
    assert len(recorder.requests) == 1
    assert recorder.last.url.path == "/api/health"


async def test_expired_token_relogins_once_when_credentials_exist(recorder, monkeypatch):
    def responder(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/auth/login":
            return httpx.Response(200, json={"token": "fresh-token"})
        if request.headers.get("x-access-token") == "stale-token":
            return httpx.Response(401, json={"error": "Session expired"})
        return httpx.Response(200, json={"ok": True})

    recorder.responder = responder
    client = youtarr_mcp.build_client(
        "https://youtarr.example.com",
        username="admin",
        password="test-password",
        token="stale-token",
        transport=httpx.MockTransport(recorder.handler),
    )
    monkeypatch.setattr(youtarr_mcp, "_client", client)
    await call("youtarr_system", operation="youtarr_status_summary")
    assert [request.url.path for request in recorder.requests] == [
        "/api/status/summary",
        "/auth/login",
        "/api/status/summary",
    ]
    assert recorder.requests[-1].headers["x-access-token"] == "fresh-token"
    await client.aclose()


async def test_concurrent_expired_token_relogins_once(recorder):
    def responder(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/auth/login":
            return httpx.Response(200, json={"token": "fresh-token"})
        if request.headers.get("x-access-token") == "stale-token":
            return httpx.Response(401, json={"error": "Session expired"})
        return httpx.Response(200, json={"ok": True})

    recorder.responder = responder
    client = youtarr_mcp.build_client(
        "https://youtarr.example.com",
        username="admin",
        password="test-password",
        token="stale-token",
        transport=httpx.MockTransport(recorder.handler),
    )
    await asyncio.gather(client.request("GET", "/first"), client.request("GET", "/second"))
    assert sum(request.url.path == "/auth/login" for request in recorder.requests) == 1
    assert sum(request.headers.get("x-access-token") == "fresh-token" for request in recorder.requests) == 2
    await client.aclose()


async def test_token_only_401_does_not_attempt_login(recorder, monkeypatch):
    recorder.responder = lambda request: httpx.Response(401, json={"error": "Invalid session"})
    client = youtarr_mcp.build_client(
        "https://youtarr.example.com",
        token="invalid-token",
        transport=httpx.MockTransport(recorder.handler),
    )
    monkeypatch.setattr(youtarr_mcp, "_client", client)
    with pytest.raises(ToolError, match="401.*Invalid session"):
        await call("youtarr_system", operation="youtarr_status_summary")
    assert len(recorder.requests) == 1
    await client.aclose()


async def test_concurrent_first_use_performs_one_login(recorder):
    client = youtarr_mcp.build_client(
        "https://youtarr.example.com",
        username="admin",
        password="test-password",
        transport=httpx.MockTransport(recorder.handler),
    )
    await asyncio.gather(client.authenticate(), client.authenticate(), client.authenticate())
    assert [request.url.path for request in recorder.requests] == ["/auth/login"]
    await client.aclose()


def test_client_requires_token_or_complete_credentials():
    with pytest.raises(ValueError, match="YOUTARR_TOKEN"):
        youtarr_mcp.build_client("https://youtarr.example.com")
    with pytest.raises(ValueError, match="YOUTARR_TOKEN"):
        youtarr_mcp.build_client("https://youtarr.example.com", username="admin")


async def test_login_error_surfaces_status_without_password(recorder):
    recorder.responder = lambda request: httpx.Response(401, json={"error": "Invalid credentials"})
    client = youtarr_mcp.build_client(
        "https://youtarr.example.com",
        username="admin",
        password="test-password",
        transport=httpx.MockTransport(recorder.handler),
    )
    with pytest.raises(ToolError, match="401.*Invalid credentials") as error:
        await client.authenticate()
    assert "test-password" not in str(error.value)
    await client.aclose()


# One dispatch through every group --------------------------------------------


@pytest.mark.parametrize(
    ("group", "operation", "method", "path"),
    [
        ("youtarr_system", "youtarr_storage_status", "GET", "/storage-status"),
        ("youtarr_auth", "youtarr_list_sessions", "GET", "/auth/sessions"),
        ("youtarr_config", "youtarr_get_config", "GET", "/getconfig"),
        ("youtarr_subscriptions", "youtarr_list_channels", "GET", "/getchannels"),
        ("youtarr_videos", "youtarr_list_videos", "GET", "/getVideos"),
        ("youtarr_jobs", "youtarr_list_running_jobs", "GET", "/runningjobs"),
        ("youtarr_playlists", "youtarr_list_playlists", "GET", "/api/playlists"),
        ("youtarr_media_servers", "youtarr_get_media_server_status", "GET", "/api/mediaservers/status"),
        ("youtarr_maintenance", "youtarr_list_schedules", "GET", "/api/schedules"),
        ("youtarr_api_keys", "youtarr_list_api_keys", "GET", "/api/keys"),
    ],
)
async def test_each_group_dispatches_a_representative_operation(server, recorder, group, operation, method, path):
    await call(group, operation=operation)
    assert recorder.last.method == method
    assert recorder.last.url.path == path


# Representative reads and writes --------------------------------------------


async def test_channel_query_uses_upstream_names_and_preserves_zero(server, recorder):
    await call(
        "youtarr_subscriptions",
        operation="youtarr_list_channels",
        arguments={"page": 0, "page_size": 50, "search": "space", "sort_order": "asc", "sub_folder": "TV"},
    )
    assert recorder.last.url.params["page"] == "0"
    assert recorder.last.url.params["pageSize"] == "50"
    assert recorder.last.url.params["search"] == "space"
    assert recorder.last.url.params["sortOrder"] == "asc"
    assert recorder.last.url.params["subFolder"] == "TV"


async def test_config_update_sends_json_body(server, recorder):
    config = {"downloadResolution": "1080", "videosToDownload": 5}
    await call("youtarr_config", operation="youtarr_update_config", arguments={"config": config})
    assert recorder.last.method == "POST"
    assert recorder.last.url.path == "/updateconfig"
    assert recorder.body() == config


async def test_channel_settings_update_uses_put_and_encoded_path(server, recorder):
    settings = {"subfolder": "TV & Film", "m3u_enabled": True}
    await call(
        "youtarr_subscriptions",
        operation="youtarr_update_channel_settings",
        arguments={"channel_id": "UC/test", "settings": settings},
    )
    assert recorder.last.method == "PUT"
    assert recorder.last.url.raw_path == b"/api/channels/UC%2Ftest/settings"
    assert recorder.body() == settings


async def test_subscription_import_start_maps_channels(server, recorder):
    channels = [{"channel_id": "UC123", "title": "Example"}]
    await call(
        "youtarr_subscriptions",
        operation="youtarr_start_subscription_import",
        arguments={"channels": channels},
    )
    assert recorder.last.url.path == "/api/subscriptions/imports"
    assert recorder.body() == {"channels": channels}


async def test_single_video_download_maps_optional_fields(server, recorder):
    await call(
        "youtarr_videos",
        operation="youtarr_download_video",
        arguments={"url": "https://youtu.be/abcdefghijk", "resolution": "1080", "subfolder": "TV"},
    )
    assert recorder.last.method == "POST"
    assert recorder.last.url.path == "/api/videos/download"
    assert recorder.body() == {
        "url": "https://youtu.be/abcdefghijk",
        "resolution": "1080",
        "subfolder": "TV",
    }


async def test_batch_download_maps_override_settings(server, recorder):
    await call(
        "youtarr_videos",
        operation="youtarr_download_videos",
        arguments={
            "urls": ["https://youtu.be/abcdefghijk"],
            "override_settings": {"resolution": "720", "protect": True},
            "video_channel_map": {"abcdefghijk": "UC123"},
        },
    )
    assert recorder.body() == {
        "urls": ["https://youtu.be/abcdefghijk"],
        "overrideSettings": {"resolution": "720", "protect": True},
        "videoChannelMap": {"abcdefghijk": "UC123"},
    }


async def test_delete_videos_sends_destructive_selection(server, recorder):
    await call(
        "youtarr_videos",
        operation="youtarr_delete_videos",
        arguments={"video_ids": [7, 8]},
    )
    assert recorder.last.method == "DELETE"
    assert recorder.last.url.path == "/api/videos"
    assert recorder.body() == {"videoIds": [7, 8]}


async def test_terminate_job_has_no_body(server, recorder):
    await call("youtarr_jobs", operation="youtarr_terminate_current_job")
    assert recorder.last.method == "POST"
    assert recorder.last.url.path == "/api/jobs/terminate"
    assert recorder.last.content == b""


async def test_playlist_subscribe_sends_url_and_settings(server, recorder):
    settings = {"auto_download": True, "sync_to_jellyfin": True}
    await call(
        "youtarr_playlists",
        operation="youtarr_subscribe_playlist",
        arguments={"url": "https://www.youtube.com/playlist?list=PL123", "settings": settings},
    )
    assert recorder.last.url.path == "/api/playlists"
    assert recorder.body() == {"url": "https://www.youtube.com/playlist?list=PL123", "settings": settings}


async def test_jellyfin_connection_test_sends_adapter_config(server, recorder):
    config = {"jellyfinUrl": "http://jellyfin:8096", "jellyfinApiKey": "test-key", "jellyfinUserId": "user"}
    await call(
        "youtarr_media_servers",
        operation="youtarr_test_jellyfin",
        arguments={"config": config},
    )
    assert recorder.last.url.path == "/api/mediaservers/jellyfin/test"
    assert recorder.body() == config


async def test_rescan_maps_ignore_library_check(server, recorder):
    await call(
        "youtarr_maintenance",
        operation="youtarr_start_rescan",
        arguments={"ignore_library_check": True},
    )
    assert recorder.last.url.path == "/api/maintenance/rescan-files"
    assert recorder.body() == {"ignoreLibraryCheck": True}


async def test_create_api_key_maps_role_and_channel_ids(server, recorder):
    await call(
        "youtarr_api_keys",
        operation="youtarr_create_api_key",
        arguments={"name": "reader", "role": "status_read"},
    )
    assert recorder.last.url.path == "/api/keys"
    assert recorder.body() == {"name": "reader", "role": "status_read"}


async def test_update_api_key_policy_wraps_policy(server, recorder):
    policy = {"role": "view", "maxRatingLevel": 2}
    await call(
        "youtarr_api_keys",
        operation="youtarr_update_api_key_policy",
        arguments={"key_id": 3, "policy": policy},
    )
    assert recorder.last.method == "PATCH"
    assert recorder.last.url.path == "/api/keys/3"
    assert recorder.body() == {"policy": policy}


async def test_subfolder_name_is_url_encoded(server, recorder):
    await call(
        "youtarr_config",
        operation="youtarr_delete_subfolder",
        arguments={"name": "TV & Film"},
    )
    assert recorder.last.url.raw_path == b"/api/subfolders/TV%20%26%20Film"


# Response and error handling -------------------------------------------------


async def test_204_active_import_returns_none(server, recorder):
    def responder(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/auth/login":
            return recorder._default_response(request)
        return httpx.Response(204)

    recorder.responder = responder
    result = await call("youtarr_subscriptions", operation="youtarr_get_active_subscription_import")
    assert result.data is None


async def test_plain_text_logs_are_supported(server, recorder):
    def responder(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/auth/login":
            return recorder._default_response(request)
        return httpx.Response(200, text="first line\nsecond line\n")

    recorder.responder = responder
    result = await call("youtarr_maintenance", operation="youtarr_download_logs")
    assert result.data == "first line\nsecond line\n"


async def test_array_response_has_structured_data(server, recorder):
    def responder(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/auth/login":
            return recorder._default_response(request)
        return httpx.Response(200, json=[{"jobId": "job-1"}])

    recorder.responder = responder
    result = await call("youtarr_jobs", operation="youtarr_list_running_jobs")
    assert result.data == [{"jobId": "job-1"}]


async def test_non_json_error_body_surfaces_status(server, recorder):
    recorder.responder = lambda request: httpx.Response(502, text="<html>Bad Gateway</html>")
    with pytest.raises(ToolError, match="502.*Bad Gateway"):
        await call("youtarr_system", operation="youtarr_status_summary")


async def test_login_response_without_token_is_an_error(recorder):
    recorder.responder = lambda request: httpx.Response(200, json={"message": "ok"})
    client = youtarr_mcp.build_client(
        "https://youtarr.example.com",
        username="admin",
        password="test-password",
        transport=httpx.MockTransport(recorder.handler),
    )
    with pytest.raises(ToolError, match="did not include a session token"):
        await client.authenticate()
    await client.aclose()


def test_omit_preserves_false_and_zero():
    assert youtarr_mcp._omit({"false": False, "zero": 0, "none": None, "empty": ""}) == {
        "false": False,
        "zero": 0,
    }


# Startup ---------------------------------------------------------------------


def test_main_requires_url(monkeypatch):
    monkeypatch.delenv("YOUTARR_URL", raising=False)
    with pytest.raises(SystemExit):
        youtarr_mcp.main()


def test_main_requires_auth(monkeypatch):
    monkeypatch.setenv("YOUTARR_URL", "https://youtarr.example.com")
    monkeypatch.delenv("YOUTARR_TOKEN", raising=False)
    monkeypatch.delenv("YOUTARR_USERNAME", raising=False)
    monkeypatch.delenv("YOUTARR_PASSWORD", raising=False)
    with pytest.raises(SystemExit):
        youtarr_mcp.main()
