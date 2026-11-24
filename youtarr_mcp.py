"""Expose the Youtarr v1.95.0 session API through 10 portmanteau tools.

Routes and payloads were verified against upstream tag v1.95.0 at commit
88720288a074861b5e78fa278cf930ea36410637. Endpoint functions stay plain
callables; _GROUPS registers one operation-dispatched MCP tool per resource.

Authentication uses YOUTARR_TOKEN or a username/password pair. Credentials
are exchanged at POST /auth/login and the resulting session token is sent as
x-access-token. Limited x-api-key download and status keys are deliberately
not accepted as full API credentials.
"""

import asyncio
import inspect
import os
import sys
from typing import Any, Literal
from urllib.parse import quote

import httpx
from fastmcp import FastMCP
from fastmcp.exceptions import ToolError
from fastmcp.tools import Tool
from mcp.types import ToolAnnotations

READONLY = ToolAnnotations(read_only_hint=True)

JSONObj = dict[str, Any]
JSONVal = JSONObj | list[Any] | str | None

mcp = FastMCP("youtarr-mcp")


def _error_message(response: httpx.Response) -> str:
    try:
        payload = response.json()
    except ValueError:
        return response.text[:1000] or response.reason_phrase
    if isinstance(payload, dict):
        message = payload.get("error") or payload.get("message") or payload
        return str(message)[:1000]
    return str(payload)[:1000]


def _check_response(response: httpx.Response) -> None:
    if response.status_code >= 400:
        raise ToolError(f"Youtarr API {response.status_code}: {_error_message(response)}")


class YoutarrClient:
    """HTTP client with lazy login and one credential-based retry after a 401."""

    def __init__(
        self,
        base_url: str,
        username: str | None = None,
        password: str | None = None,
        token: str | None = None,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        if not token and not (username and password):
            raise ValueError("Set YOUTARR_TOKEN or both YOUTARR_USERNAME and YOUTARR_PASSWORD")
        self.username = username
        self.password = password
        self._token = token or None
        self._login_lock = asyncio.Lock()
        headers = {"Accept": "application/json"}
        if self._token:
            headers["x-access-token"] = self._token
        self._http = httpx.AsyncClient(
            base_url=base_url.rstrip("/"),
            headers=headers,
            timeout=httpx.Timeout(120.0, connect=10.0),
            transport=transport,
        )

    async def aclose(self) -> None:
        await self._http.aclose()

    async def _login_locked(self) -> None:
        self._token = None
        self._http.headers.pop("x-access-token", None)
        try:
            response = await self._http.post(
                "/auth/login",
                json={"username": self.username, "password": self.password},
            )
        except httpx.HTTPError as exc:
            raise ToolError(f"Youtarr login failed: {exc}") from exc
        _check_response(response)
        try:
            payload = response.json()
        except ValueError as exc:
            raise ToolError("Youtarr login returned a non-JSON response") from exc
        token = payload.get("token") if isinstance(payload, dict) else None
        if not isinstance(token, str) or not token:
            raise ToolError("Youtarr login response did not include a session token")
        self._token = token
        self._http.headers["x-access-token"] = token

    async def authenticate(self) -> None:
        if self._token:
            return
        async with self._login_lock:
            if not self._token:
                await self._login_locked()

    async def _relogin(self, failed_token: str | None) -> None:
        async with self._login_lock:
            if self._token and self._token != failed_token:
                return
            await self._login_locked()

    async def request(
        self,
        method: str,
        path: str,
        params: dict[str, Any] | None = None,
        json_body: Any = None,
        authenticated: bool = True,
    ) -> httpx.Response:
        if not authenticated:
            return await self._http.request(method, path, params=params, json=json_body)
        await self.authenticate()
        response = await self._http.request(method, path, params=params, json=json_body)
        if response.status_code == 401 and self.username and self.password:
            await self._relogin(self._token)
            response = await self._http.request(method, path, params=params, json=json_body)
        return response


_client: YoutarrClient | None = None


def build_client(
    base_url: str,
    username: str | None = None,
    password: str | None = None,
    token: str | None = None,
    transport: httpx.BaseTransport | None = None,
) -> YoutarrClient:
    return YoutarrClient(base_url, username, password, token, transport)


async def _req(
    method: str,
    path: str,
    params: dict[str, Any] | None = None,
    json_body: Any = None,
    authenticated: bool = True,
) -> JSONVal:
    assert _client is not None, "client not configured"
    try:
        response = await _client.request(method, path, params, json_body, authenticated)
    except httpx.HTTPError as exc:
        raise ToolError(f"Youtarr request failed: {exc}") from exc
    _check_response(response)
    if response.status_code == 204 or not response.content:
        return None
    try:
        return response.json()
    except ValueError:
        return response.text


async def _req_text(path: str) -> str:
    assert _client is not None, "client not configured"
    try:
        response = await _client.request("GET", path)
    except httpx.HTTPError as exc:
        raise ToolError(f"Youtarr request failed: {exc}") from exc
    _check_response(response)
    return response.text


def _segment(value: str | int) -> str:
    return quote(str(value), safe="")


def _omit(values: dict[str, Any]) -> dict[str, Any]:
    """Omit unset values while preserving false and zero."""
    return {key: value for key, value in values.items() if value is not None and value != ""}


# System, health, status, and storage -----------------------------------------


async def youtarr_health() -> JSONVal:
    """Read the unauthenticated server health check."""
    return await _req("GET", "/api/health", authenticated=False)


async def youtarr_db_status() -> JSONVal:
    """Read unauthenticated database connection and schema health."""
    return await _req("GET", "/api/db-status", authenticated=False)


async def youtarr_release_version() -> JSONVal:
    """Read the latest stable Youtarr release and installed yt-dlp version."""
    return await _req("GET", "/getCurrentReleaseVersion", authenticated=False)


async def youtarr_status_summary() -> JSONVal:
    """Read the dashboard snapshot of library, queue, last run, and recent downloads."""
    return await _req("GET", "/api/status/summary")


async def youtarr_storage_status() -> JSONVal:
    """Read total, used, and free bytes for the downloads filesystem."""
    return await _req("GET", "/storage-status")


# Authentication and sessions -------------------------------------------------


async def youtarr_validate_token() -> JSONVal:
    """Validate the current session token."""
    return await _req("GET", "/validateToken")


async def youtarr_list_sessions() -> JSONVal:
    """List active sessions for the current user."""
    return await _req("GET", "/auth/sessions")


async def youtarr_revoke_session(session_id: int) -> JSONVal:
    """Invalidate one session belonging to the current user."""
    return await _req("DELETE", f"/auth/sessions/{_segment(session_id)}")


async def youtarr_logout() -> JSONVal:
    """Invalidate the current session; later calls log in again when credentials are configured."""
    return await _req("POST", "/auth/logout")


async def youtarr_change_password(current_password: str, new_password: str) -> JSONVal:
    """Change the current user's password; newPassword must be 8 to 64 characters."""
    return await _req(
        "POST",
        "/auth/change-password",
        json_body={"currentPassword": current_password, "newPassword": new_password},
    )


# Configuration ---------------------------------------------------------------


async def youtarr_get_config() -> JSONVal:
    """Read application configuration with upstream-filtered secret fields removed."""
    return await _req("GET", "/getconfig")


async def youtarr_update_config(config: JSONObj) -> JSONVal:
    """WRITE: Replace application configuration using the fields returned by get_config."""
    return await _req("POST", "/updateconfig", json_body=config)


async def youtarr_preview_filename(prefix: str) -> JSONVal:
    """Render a videoFilenamePrefix against Youtarr's canned yt-dlp sample."""
    return await _req("POST", "/api/config/filename-preview", json_body={"prefix": prefix})


async def youtarr_get_cookie_status() -> JSONVal:
    """Read uploaded or external YouTube cookie status without exposing cookie values."""
    return await _req("GET", "/api/cookies/status")


async def youtarr_test_cookies() -> JSONVal:
    """Ask YouTube whether the active cookie file still belongs to a signed-in session."""
    return await _req("POST", "/api/cookies/test")


async def youtarr_delete_cookies() -> JSONVal:
    """DESTRUCTIVE: Remove the custom YouTube cookie file when cookies are not externally managed."""
    return await _req("DELETE", "/api/cookies")


async def youtarr_test_notification() -> JSONVal:
    """Send a test notification through the saved notification configuration."""
    return await _req("POST", "/api/notifications/test")


async def youtarr_test_notification_single(
    url: str,
    name: str | None = None,
    rich_formatting: bool | None = None,
) -> JSONVal:
    """Send a test notification to one Apprise-compatible webhook URL."""
    return await _req(
        "POST",
        "/api/notifications/test-single",
        json_body=_omit({"url": url, "name": name, "richFormatting": rich_formatting}),
    )


async def youtarr_test_youtube_api_key(api_key: str) -> JSONVal:
    """WRITE: Validate a YouTube Data API key and save it when validation succeeds."""
    return await _req("POST", "/testYoutubeApiKey", json_body={"apiKey": api_key})


async def youtarr_validate_ytdlp_args(args: str) -> JSONVal:
    """Check custom yt-dlp arguments against the allowlist and dry-run their parser."""
    return await _req("POST", "/api/ytdlp/validate-args", json_body={"args": args})


async def youtarr_list_subfolders() -> JSONVal:
    """List registered subfolders and where each one is used."""
    return await _req("GET", "/api/subfolders")


async def youtarr_create_subfolder(name: str) -> JSONVal:
    """WRITE: Register a subfolder name for use by channels, playlists, and downloads."""
    return await _req("POST", "/api/subfolders", json_body={"name": name})


async def youtarr_delete_subfolder(name: str) -> JSONVal:
    """DESTRUCTIVE: Delete an unused, empty subfolder from the registry."""
    return await _req("DELETE", f"/api/subfolders/{_segment(name)}")


# Channels and subscription imports -------------------------------------------


async def youtarr_list_channels(
    page: int | None = None,
    page_size: int | None = None,
    search: str | None = None,
    sort_by: str | None = None,
    sort_order: str | None = None,
    sub_folder: str | None = None,
) -> JSONVal:
    """List subscribed YouTube channels with pagination, search, sorting, and subfolder filtering."""
    return await _req(
        "GET",
        "/getchannels",
        _omit(
            {
                "page": page,
                "pageSize": page_size,
                "search": search,
                "sortBy": sort_by,
                "sortOrder": sort_order,
                "subFolder": sub_folder,
            }
        ),
    )


async def youtarr_update_channels(payload: JSONObj | list[Any]) -> JSONVal:
    """WRITE: Replace channels with an array or apply a delta object containing add and remove arrays."""
    return await _req("POST", "/updatechannels", json_body=payload)


async def youtarr_get_channel_info(url: str) -> JSONVal:
    """Fetch YouTube channel metadata by URL before subscribing."""
    return await _req("POST", "/addchannelinfo", json_body={"url": url})


async def youtarr_get_channel(channel_id: str) -> JSONVal:
    """Read one subscribed channel by YouTube channel ID."""
    return await _req("GET", f"/getchannelinfo/{_segment(channel_id)}")


async def youtarr_search_channels(query: str, count: int | None = None) -> JSONVal:
    """Search YouTube for channels; results are ephemeral and include local subscription state."""
    return await _req(
        "POST",
        "/api/channels/search",
        json_body=_omit({"query": query, "count": count}),
    )


async def youtarr_get_channel_tabs(channel_id: str) -> JSONVal:
    """Read available videos, shorts, and streams tabs for a channel."""
    return await _req("GET", f"/api/channels/{_segment(channel_id)}/tabs")


async def youtarr_get_channel_tab_stats(channel_id: str) -> JSONVal:
    """Read per-tab public, downloaded, ignored, and loaded video counts."""
    return await _req("GET", f"/api/channels/{_segment(channel_id)}/tab-stats")


async def youtarr_set_channel_tab_auto_download(channel_id: str, tab_type: str, enabled: bool) -> JSONVal:
    """WRITE: Enable or disable automatic downloads for one channel tab."""
    return await _req(
        "PATCH",
        f"/api/channels/{_segment(channel_id)}/tabs/{_segment(tab_type)}/auto-download",
        json_body={"enabled": enabled},
    )


async def youtarr_redetect_channel_tabs(channel_id: str) -> JSONVal:
    """WRITE: Re-probe a channel's tabs with yt-dlp and update effective tab settings."""
    return await _req("POST", f"/api/channels/{_segment(channel_id)}/tabs/redetect")


async def youtarr_get_channel_settings(channel_id: str) -> JSONVal:
    """Read per-channel download, filter, playlist, and auto-removal settings."""
    return await _req("GET", f"/api/channels/{_segment(channel_id)}/settings")


async def youtarr_update_channel_settings(channel_id: str, settings: JSONObj) -> JSONVal:
    """WRITE: Update per-channel settings such as subfolder, title_filter_regex, and protection."""
    return await _req(
        "PUT",
        f"/api/channels/{_segment(channel_id)}/settings",
        json_body=settings,
    )


async def youtarr_list_channel_subfolders() -> JSONVal:
    """List subfolders currently used by channels."""
    return await _req("GET", "/api/channels/subfolders")


async def youtarr_preview_channel_filter(channel_id: str, title_filter_regex: str | None = None) -> JSONVal:
    """Preview which stored channel videos match a title-filter regular expression."""
    return await _req(
        "GET",
        f"/api/channels/{_segment(channel_id)}/filter-preview",
        _omit({"title_filter_regex": title_filter_regex}),
    )


async def youtarr_list_channel_videos(
    channel_id: str,
    page: int | None = None,
    page_size: int | None = None,
    search_query: str | None = None,
    sort_by: str | None = None,
    sort_order: str | None = None,
    tab_type: str | None = None,
    min_duration: int | None = None,
    max_duration: int | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
    downloaded_filter: str | None = None,
    protected_filter: str | None = None,
    missing_filter: str | None = None,
    ignored_filter: str | None = None,
    watched_filter: str | None = None,
    deleted_filter: str | None = None,
    max_rating: str | None = None,
) -> JSONVal:
    """List stored videos for a channel tab with download, watch, missing, and rating filters."""
    return await _req(
        "GET",
        f"/getchannelvideos/{_segment(channel_id)}",
        _omit(
            {
                "page": page,
                "pageSize": page_size,
                "searchQuery": search_query,
                "sortBy": sort_by,
                "sortOrder": sort_order,
                "tabType": tab_type,
                "minDuration": min_duration,
                "maxDuration": max_duration,
                "dateFrom": date_from,
                "dateTo": date_to,
                "downloadedFilter": downloaded_filter,
                "protectedFilter": protected_filter,
                "missingFilter": missing_filter,
                "ignoredFilter": ignored_filter,
                "watchedFilter": watched_filter,
                "deletedFilter": deleted_filter,
                "maxRating": max_rating,
            }
        ),
    )


async def youtarr_fetch_channel_videos(
    channel_id: str,
    page: int | None = None,
    page_size: int | None = None,
    downloaded_filter: str | None = None,
    tab_type: str | None = None,
) -> JSONVal:
    """WRITE: Fetch a page of videos from YouTube into a channel tab."""
    return await _req(
        "POST",
        f"/fetchallchannelvideos/{_segment(channel_id)}",
        _omit(
            {
                "page": page,
                "pageSize": page_size,
                "downloadedFilter": downloaded_filter,
                "tabType": tab_type,
            }
        ),
    )


async def youtarr_get_channel_fetch_status(channel_id: str, tab_type: str | None = None) -> JSONVal:
    """Read channel fetch progress and the latest background refresh result."""
    return await _req(
        "GET",
        f"/api/channels/{_segment(channel_id)}/fetch-status",
        _omit({"tabType": tab_type}),
    )


async def youtarr_preview_channel_download_all(channel_id: str, tab_type: str | None = None) -> JSONVal:
    """Count known never-downloaded videos that a channel download-all run would queue."""
    return await _req(
        "GET",
        f"/api/channels/{_segment(channel_id)}/download-all/preview",
        _omit({"tabType": tab_type}),
    )


async def youtarr_download_channel_all(
    channel_id: str,
    tab_type: str | None = None,
    override_settings: JSONObj | None = None,
) -> JSONVal:
    """WRITE: Queue every known never-downloaded video on a channel tab; this can be long-running."""
    return await _req(
        "POST",
        f"/api/channels/{_segment(channel_id)}/download-all",
        json_body=_omit({"tabType": tab_type, "overrideSettings": override_settings}),
    )


async def youtarr_ignore_channel_video(channel_id: str, youtube_id: str) -> JSONVal:
    """WRITE: Ignore a channel video so automatic downloads skip it."""
    return await _req(
        "POST",
        f"/api/channels/{_segment(channel_id)}/videos/{_segment(youtube_id)}/ignore",
    )


async def youtarr_unignore_channel_video(channel_id: str, youtube_id: str) -> JSONVal:
    """WRITE: Remove a channel video's ignored status."""
    return await _req(
        "POST",
        f"/api/channels/{_segment(channel_id)}/videos/{_segment(youtube_id)}/unignore",
    )


async def youtarr_bulk_ignore_channel_videos(channel_id: str, youtube_ids: list[str]) -> JSONVal:
    """WRITE: Ignore several videos belonging to one channel."""
    return await _req(
        "POST",
        f"/api/channels/{_segment(channel_id)}/videos/bulk-ignore",
        json_body={"youtubeIds": youtube_ids},
    )


async def youtarr_list_subscription_imports() -> JSONVal:
    """List recent Google Takeout or cookie-based subscription import jobs."""
    return await _req("GET", "/api/subscriptions/imports")


async def youtarr_get_active_subscription_import() -> JSONVal:
    """Read the active subscription import, or None when no import is running."""
    return await _req("GET", "/api/subscriptions/imports/active")


async def youtarr_get_subscription_import(job_id: str) -> JSONVal:
    """Read one active or historical subscription import job."""
    return await _req("GET", f"/api/subscriptions/imports/{_segment(job_id)}")


async def youtarr_start_subscription_import(channels: list[JSONObj]) -> JSONVal:
    """WRITE: Start importing selected channel objects in the background."""
    return await _req("POST", "/api/subscriptions/imports", json_body={"channels": channels})


async def youtarr_cancel_subscription_import(job_id: str) -> JSONVal:
    """WRITE: Request cancellation of an active subscription import."""
    return await _req("POST", f"/api/subscriptions/imports/{_segment(job_id)}/cancel")


# Videos, downloads, and trash -------------------------------------------------


async def youtarr_list_videos(
    page: int | None = None,
    limit: int | None = None,
    search: str | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
    sort_by: str | None = None,
    sort_order: str | None = None,
    channel_filter: str | None = None,
    protected_filter: str | None = None,
    missing_filter: str | None = None,
    deleted_filter: str | None = None,
    watched_filter: str | None = None,
    max_rating: str | None = None,
) -> JSONVal:
    """List downloaded videos with pagination, search, date, state, and rating filters."""
    return await _req(
        "GET",
        "/getVideos",
        _omit(
            {
                "page": page,
                "limit": limit,
                "search": search,
                "dateFrom": date_from,
                "dateTo": date_to,
                "sortBy": sort_by,
                "sortOrder": sort_order,
                "channelFilter": channel_filter,
                "protectedFilter": protected_filter,
                "missingFilter": missing_filter,
                "deletedFilter": deleted_filter,
                "watchedFilter": watched_filter,
                "maxRating": max_rating,
            }
        ),
    )


async def youtarr_get_video(youtube_id: str) -> JSONVal:
    """Read one video row by YouTube ID, including deleted or missing records until purged."""
    return await _req("GET", f"/api/videos/{_segment(youtube_id)}")


async def youtarr_get_video_metadata(youtube_id: str) -> JSONVal:
    """Read extended metadata from the cached info JSON or fetch it with yt-dlp."""
    return await _req("GET", f"/api/videos/{_segment(youtube_id)}/metadata")


async def youtarr_get_video_watch_status(youtube_id: str) -> JSONVal:
    """Read synced per-media-server and per-user watch status for a video."""
    return await _req("GET", f"/api/videos/{_segment(youtube_id)}/watch-status")


async def youtarr_check_video_members_access(youtube_id: str) -> JSONVal:
    """WRITE: Ask YouTube whether configured cookies can open a members-only video and cache the answer."""
    return await _req("POST", f"/api/videos/{_segment(youtube_id)}/members-access/check")


async def youtarr_get_local_video_status(youtube_ids: list[str]) -> JSONVal:
    """Read local download state and file metadata for up to 500 YouTube video IDs without YouTube requests."""
    return await _req("POST", "/api/videos/local-status", json_body={"youtubeIds": youtube_ids})


async def youtarr_search_videos(query: str, count: int | None = None) -> JSONVal:
    """Search YouTube for videos with local download and channel-subscription state."""
    return await _req(
        "POST",
        "/api/videos/search",
        json_body=_omit({"query": query, "count": count}),
    )


async def youtarr_validate_video_url(url: str) -> JSONVal:
    """Validate one YouTube video URL and fetch its metadata without queuing a download."""
    return await _req("POST", "/api/checkYoutubeVideoURL", json_body={"url": url})


async def youtarr_enrich_videos(ids: list[str]) -> JSONVal:
    """Fetch lightweight title and channel metadata for YouTube video IDs through oEmbed."""
    return await _req("POST", "/api/bulkEnrichVideos", json_body={"ids": ids})


async def youtarr_download_video(
    url: str,
    resolution: str | None = None,
    subfolder: str | None = None,
) -> JSONVal:
    """WRITE: Queue one YouTube video; accepts only individual video URLs, not playlists or channels."""
    return await _req(
        "POST",
        "/api/videos/download",
        json_body=_omit({"url": url, "resolution": resolution, "subfolder": subfolder}),
    )


async def youtarr_download_videos(
    urls: list[str],
    override_settings: JSONObj | None = None,
    video_channel_map: JSONObj | None = None,
) -> JSONVal:
    """WRITE: Queue a batch of individual YouTube video URLs with optional per-run overrides."""
    return await _req(
        "POST",
        "/triggerspecificdownloads",
        json_body=_omit(
            {
                "urls": urls,
                "overrideSettings": override_settings,
                "videoChannelMap": video_channel_map,
            }
        ),
    )


async def youtarr_download_new_channel_videos(override_settings: JSONObj | None = None) -> JSONVal:
    """WRITE: Start the manual download-new run for enabled channels and playlists."""
    return await _req(
        "POST",
        "/triggerchanneldownloads",
        json_body=_omit({"overrideSettings": override_settings}),
    )


async def youtarr_update_video_ratings(video_ids: list[int], rating: str | None) -> JSONVal:
    """WRITE: Set or clear content ratings for several database video IDs."""
    return await _req("POST", "/api/videos/rating", json_body={"videoIds": video_ids, "rating": rating})


async def youtarr_set_video_protected(video_id: int, protected: bool) -> JSONVal:
    """WRITE: Set whether a database video is protected from automatic removal."""
    return await _req(
        "PATCH",
        f"/api/videos/{_segment(video_id)}/protected",
        json_body={"protected": protected},
    )


async def youtarr_delete_videos(video_ids: list[int] | None = None, youtube_ids: list[str] | None = None) -> JSONVal:
    """DESTRUCTIVE: Delete downloaded files and records selected by database IDs or YouTube IDs."""
    return await _req(
        "DELETE",
        "/api/videos",
        json_body=_omit({"videoIds": video_ids, "youtubeIds": youtube_ids}),
    )


async def youtarr_preview_auto_removal(settings: JSONObj | None = None) -> JSONVal:
    """Preview automatic cleanup with optional autoRemoval* overrides without deleting files."""
    return await _req("POST", "/api/auto-removal/dry-run", json_body=settings)


async def youtarr_get_removed_video_summary() -> JSONVal:
    """Read deleted, missing, present, and library-check counts for the video trash."""
    return await _req("GET", "/api/videos/removed-summary")


async def youtarr_mark_videos_deleted(
    video_ids: list[int] | None = None,
    mark_all: bool | None = None,
    ignore_library_check: bool | None = None,
) -> JSONVal:
    """WRITE: Mark selected missing videos, or all missing videos, as deleted."""
    return await _req(
        "POST",
        "/api/videos/mark-deleted",
        json_body=_omit(
            {
                "videoIds": video_ids,
                "all": mark_all,
                "ignoreLibraryCheck": ignore_library_check,
            }
        ),
    )


async def youtarr_purge_removed_videos(states: list[str], remove_from_archive: bool) -> JSONVal:
    """DESTRUCTIVE: Permanently forget deleted and/or missing video records and cached metadata."""
    return await _req(
        "POST",
        "/api/videos/purge-removed",
        json_body={"states": states, "removeFromArchive": remove_from_archive},
    )


# Jobs ------------------------------------------------------------------------


async def youtarr_list_running_jobs() -> JSONVal:
    """List currently running download jobs."""
    return await _req("GET", "/runningjobs")


async def youtarr_get_job(job_id: str) -> JSONVal:
    """Read one download job by job ID."""
    return await _req("GET", f"/jobstatus/{_segment(job_id)}")


async def youtarr_get_video_activity() -> JSONVal:
    """Read the transient queued and downloading video activity snapshot."""
    return await _req("GET", "/api/jobs/video-activity")


async def youtarr_get_current_activity() -> JSONVal:
    """Read current or most recent download progress and final activity."""
    return await _req("GET", "/api/jobs/current-activity")


async def youtarr_get_download_pause() -> JSONVal:
    """Read storage or library pause state, reasons, and current measurements."""
    return await _req("GET", "/api/jobs/download-pause")


async def youtarr_terminate_current_job() -> JSONVal:
    """DESTRUCTIVE: Terminate the currently running download job."""
    return await _req("POST", "/api/jobs/terminate")


# Playlists -------------------------------------------------------------------


async def youtarr_list_playlists(page: int | None = None, page_size: int | None = None) -> JSONVal:
    """List subscribed YouTube playlists with current downloaded counts."""
    return await _req("GET", "/api/playlists", _omit({"page": page, "pageSize": page_size}))


async def youtarr_get_playlist(playlist_id: str) -> JSONVal:
    """Read one playlist with download counts and media-server sync status."""
    return await _req("GET", f"/api/playlists/{_segment(playlist_id)}")


async def youtarr_get_playlist_info(url: str) -> JSONVal:
    """Fetch YouTube playlist metadata and any existing local subscription before adding it."""
    return await _req("POST", "/api/playlists/addplaylistinfo", json_body={"url": url})


async def youtarr_subscribe_playlist(url: str, settings: JSONObj | None = None) -> JSONVal:
    """WRITE: Subscribe to a playlist, fetch its videos, and start background sync and M3U generation."""
    return await _req(
        "POST",
        "/api/playlists",
        json_body=_omit({"url": url, "settings": settings}),
    )


async def youtarr_unsubscribe_playlist(playlist_id: str) -> JSONVal:
    """DESTRUCTIVE: Soft-delete a playlist subscription while retaining its saved settings."""
    return await _req("DELETE", f"/api/playlists/{_segment(playlist_id)}")


async def youtarr_update_playlist(playlist_id: str, updates: JSONObj) -> JSONVal:
    """WRITE: Update playlist flags such as auto_download and sync_to_jellyfin."""
    return await _req("PATCH", f"/api/playlists/{_segment(playlist_id)}", json_body=updates)


async def youtarr_get_playlist_settings(playlist_id: str) -> JSONVal:
    """Read per-playlist download, filter, rating, audio, and sort settings."""
    return await _req("GET", f"/api/playlists/{_segment(playlist_id)}/settings")


async def youtarr_update_playlist_settings(playlist_id: str, settings: JSONObj) -> JSONVal:
    """WRITE: Update per-playlist download settings using the keys returned by get_playlist_settings."""
    return await _req(
        "PUT",
        f"/api/playlists/{_segment(playlist_id)}/settings",
        json_body=settings,
    )


async def youtarr_list_playlist_videos(
    playlist_id: str,
    page: int | None = None,
    page_size: int | None = None,
    sort_order: str | None = None,
    download_state: str | None = None,
    watched_state: str | None = None,
) -> JSONVal:
    """List playlist videos with local download, watch, and members-access overlays."""
    return await _req(
        "GET",
        f"/api/playlists/{_segment(playlist_id)}/videos",
        _omit(
            {
                "page": page,
                "pageSize": page_size,
                "sortOrder": sort_order,
                "downloadState": download_state,
                "watchedState": watched_state,
            }
        ),
    )


async def youtarr_refresh_playlist(playlist_id: str) -> JSONVal:
    """WRITE: Fetch the full playlist listing from YouTube and trigger background sync and M3U generation."""
    return await _req("POST", f"/api/playlists/{_segment(playlist_id)}/refresh")


async def youtarr_sync_playlist(playlist_id: str) -> JSONVal:
    """WRITE: Start a background playlist sync to configured media servers."""
    return await _req("POST", f"/api/playlists/{_segment(playlist_id)}/sync")


async def youtarr_preview_playlist_downloads(
    playlist_id: str,
    order: str | None = None,
    count: int | None = None,
) -> JSONVal:
    """Preview eligible tracked playlist videos and a suggested explicit selection."""
    return await _req(
        "GET",
        f"/api/playlists/{_segment(playlist_id)}/download-preview",
        _omit({"order": order, "count": count}),
    )


async def youtarr_start_playlist_following(
    playlist_id: str,
    restart: bool | None = None,
    video_ids: list[str] | None = None,
) -> JSONVal:
    """WRITE: Start or resume following new playlist entries, optionally restarting the baseline."""
    return await _req(
        "POST",
        f"/api/playlists/{_segment(playlist_id)}/following",
        json_body=_omit({"restart": restart, "videoIds": video_ids}),
    )


async def youtarr_download_playlist_batch(playlist_id: str, video_ids: list[str]) -> JSONVal:
    """WRITE: Queue selected eligible existing playlist videos without resetting following."""
    return await _req(
        "POST",
        f"/api/playlists/{_segment(playlist_id)}/download-batch",
        json_body={"videoIds": video_ids},
    )


async def youtarr_download_playlist(
    playlist_id: str,
    video_ids: list[str] | None = None,
    override_settings: JSONObj | None = None,
) -> JSONVal:
    """WRITE: Queue all eligible playlist videos, or only the supplied YouTube video IDs."""
    return await _req(
        "POST",
        f"/api/playlists/{_segment(playlist_id)}/download",
        json_body=_omit({"videoIds": video_ids, "overrideSettings": override_settings}),
    )


async def youtarr_regenerate_playlist_m3u(playlist_id: str) -> JSONVal:
    """WRITE: Regenerate the playlist M3U file."""
    return await _req("POST", f"/api/playlists/{_segment(playlist_id)}/regenerate-m3u")


async def youtarr_ignore_playlist_video(playlist_id: str, youtube_id: str) -> JSONVal:
    """WRITE: Ignore a playlist video for automatic and bulk downloads."""
    return await _req(
        "POST",
        f"/api/playlists/{_segment(playlist_id)}/videos/{_segment(youtube_id)}/ignore",
    )


async def youtarr_unignore_playlist_video(playlist_id: str, youtube_id: str) -> JSONVal:
    """WRITE: Remove a playlist video's ignored status."""
    return await _req(
        "POST",
        f"/api/playlists/{_segment(playlist_id)}/videos/{_segment(youtube_id)}/unignore",
    )


# Jellyfin, Emby, and Plex integration -----------------------------------------


async def youtarr_get_media_server_status() -> JSONVal:
    """Read which Plex, Jellyfin, and Emby integrations are enabled and configured."""
    return await _req("GET", "/api/mediaservers/status")


async def youtarr_test_jellyfin(config: JSONObj) -> JSONVal:
    """Test a Jellyfin connection using jellyfinUrl and jellyfinApiKey, with optional jellyfinUserId."""
    return await _req("POST", "/api/mediaservers/jellyfin/test", json_body=config)


async def youtarr_list_jellyfin_users(config: JSONObj) -> JSONVal:
    """List Jellyfin users using jellyfinUrl and jellyfinApiKey."""
    return await _req("POST", "/api/mediaservers/jellyfin/users", json_body=config)


async def youtarr_test_emby(config: JSONObj) -> JSONVal:
    """Test an Emby connection using embyUrl and embyApiKey, with optional embyUserId."""
    return await _req("POST", "/api/mediaservers/emby/test", json_body=config)


async def youtarr_list_emby_users(config: JSONObj) -> JSONVal:
    """List Emby users using embyUrl and embyApiKey."""
    return await _req("POST", "/api/mediaservers/emby/users", json_body=config)


async def youtarr_get_watch_status_sync() -> JSONVal:
    """Read media-server watch-status sync state and the latest run summary."""
    return await _req("GET", "/api/mediaservers/watch-status")


async def youtarr_sync_watch_status() -> JSONVal:
    """WRITE: Start a media-server watch-status sync in the background."""
    return await _req("POST", "/api/mediaservers/watch-status/sync")


async def youtarr_list_plex_libraries(
    test_ip: str | None = None,
    test_api_key: str | None = None,
    test_port: str | None = None,
    test_use_https: bool | None = None,
) -> JSONVal:
    """Read Plex libraries; WRITE: successful test credentials are saved automatically by upstream."""
    return await _req(
        "GET",
        "/getplexlibraries",
        _omit(
            {
                "testIP": test_ip,
                "testApiKey": test_api_key,
                "testPort": test_port,
                "testUseHttps": test_use_https,
            }
        ),
    )


async def youtarr_get_plex_server_identity(
    test_ip: str | None = None,
    test_api_key: str | None = None,
    test_port: str | None = None,
    test_use_https: bool | None = None,
) -> JSONVal:
    """Read Plex server identity using saved or optional test connection parameters."""
    return await _req(
        "GET",
        "/plex/server-identity",
        _omit(
            {
                "testIP": test_ip,
                "testApiKey": test_api_key,
                "testPort": test_port,
                "testUseHttps": test_use_https,
            }
        ),
    )


async def youtarr_refresh_plex_library() -> JSONVal:
    """WRITE: Trigger a refresh of the configured Plex library."""
    return await _req("GET", "/refreshlibrary")


async def youtarr_get_plex_auth_url() -> JSONVal:
    """Create a Plex OAuth PIN and return its authorization URL."""
    return await _req("GET", "/plex/auth-url")


async def youtarr_check_plex_pin(pin_id: str) -> JSONVal:
    """Check whether a Plex OAuth PIN has been authorized."""
    return await _req("GET", f"/plex/check-pin/{_segment(pin_id)}")


# Scheduling, maintenance, and logs -------------------------------------------


async def youtarr_list_schedules() -> JSONVal:
    """Read live state, next run, blockers, and history for every scheduled task."""
    return await _req("GET", "/api/schedules")


async def youtarr_run_schedule(key: str) -> JSONVal:
    """WRITE: Start one scheduled task now using its saved settings."""
    return await _req("POST", f"/api/schedules/{_segment(key)}/run")


async def youtarr_start_rescan(ignore_library_check: bool | None = None) -> JSONVal:
    """WRITE: Start a filesystem rescan, optionally bypassing empty and not-found library checks."""
    return await _req(
        "POST",
        "/api/maintenance/rescan-files",
        json_body=_omit({"ignoreLibraryCheck": ignore_library_check}),
    )


async def youtarr_get_rescan_status() -> JSONVal:
    """Read rescan running state, progress, and the latest run summary."""
    return await _req("GET", "/api/maintenance/rescan-status")


async def youtarr_get_ytdlp_version() -> JSONVal:
    """Read installed and latest yt-dlp versions plus update history."""
    return await _req("GET", "/api/ytdlp/latest-version")


async def youtarr_update_ytdlp() -> JSONVal:
    """WRITE: Update yt-dlp to the latest release on the configured stable or nightly channel."""
    return await _req("POST", "/api/ytdlp/update")


async def youtarr_download_logs() -> str:
    """Read all rolling log files as one plain-text response with upstream secret redaction."""
    return await _req_text("/api/logs/download")


# API key management ----------------------------------------------------------


async def youtarr_list_api_keys() -> JSONVal:
    """List API key metadata without raw key values; requires a full session."""
    return await _req("GET", "/api/keys")


async def youtarr_create_api_key(
    name: str,
    role: str | None = None,
    policy: JSONObj | None = None,
    channel_ids: list[int] | None = None,
) -> JSONVal:
    """WRITE: Create a download, status_read, or constrained external key; the raw key is shown once."""
    return await _req(
        "POST",
        "/api/keys",
        json_body=_omit({"name": name, "role": role, "policy": policy, "channelIds": channel_ids}),
    )


async def youtarr_update_api_key_policy(key_id: int, policy: JSONObj) -> JSONVal:
    """WRITE: Replace a constrained external API key's policy."""
    return await _req("PATCH", f"/api/keys/{_segment(key_id)}", json_body={"policy": policy})


async def youtarr_get_api_key_channels(key_id: int) -> JSONVal:
    """Read the exact channel grants for an active constrained external API key."""
    return await _req("GET", f"/api/keys/{_segment(key_id)}/channels")


async def youtarr_replace_api_key_channels(key_id: int, channel_ids: list[int]) -> JSONVal:
    """WRITE: Replace an external API key's exact channel grant set."""
    return await _req(
        "PUT",
        f"/api/keys/{_segment(key_id)}/channels",
        json_body={"channelIds": channel_ids},
    )


async def youtarr_replace_api_key_external_access(key_id: int, policy: JSONObj, channel_ids: list[int]) -> JSONVal:
    """WRITE: Atomically replace a constrained key's policy and channel grants."""
    return await _req(
        "PUT",
        f"/api/keys/{_segment(key_id)}/external-access",
        json_body={"policy": policy, "channelIds": channel_ids},
    )


async def youtarr_regenerate_api_key(key_id: int) -> JSONVal:
    """WRITE: Regenerate an API key; the new raw key is shown once."""
    return await _req("POST", f"/api/keys/{_segment(key_id)}/regenerate")


async def youtarr_revoke_api_key(key_id: int) -> JSONVal:
    """DESTRUCTIVE: Revoke an API key while retaining its audit record."""
    return await _req("DELETE", f"/api/keys/{_segment(key_id)}")


_GROUPS: dict[str, tuple[str, ...]] = {
    "youtarr_system": (
        "youtarr_health",
        "youtarr_db_status",
        "youtarr_release_version",
        "youtarr_status_summary",
        "youtarr_storage_status",
    ),
    "youtarr_auth": (
        "youtarr_validate_token",
        "youtarr_list_sessions",
        "youtarr_revoke_session",
        "youtarr_logout",
        "youtarr_change_password",
    ),
    "youtarr_config": (
        "youtarr_get_config",
        "youtarr_update_config",
        "youtarr_preview_filename",
        "youtarr_get_cookie_status",
        "youtarr_test_cookies",
        "youtarr_delete_cookies",
        "youtarr_test_notification",
        "youtarr_test_notification_single",
        "youtarr_test_youtube_api_key",
        "youtarr_validate_ytdlp_args",
        "youtarr_list_subfolders",
        "youtarr_create_subfolder",
        "youtarr_delete_subfolder",
    ),
    "youtarr_subscriptions": (
        "youtarr_list_channels",
        "youtarr_update_channels",
        "youtarr_get_channel_info",
        "youtarr_get_channel",
        "youtarr_search_channels",
        "youtarr_get_channel_tabs",
        "youtarr_get_channel_tab_stats",
        "youtarr_set_channel_tab_auto_download",
        "youtarr_redetect_channel_tabs",
        "youtarr_get_channel_settings",
        "youtarr_update_channel_settings",
        "youtarr_list_channel_subfolders",
        "youtarr_preview_channel_filter",
        "youtarr_list_channel_videos",
        "youtarr_fetch_channel_videos",
        "youtarr_get_channel_fetch_status",
        "youtarr_preview_channel_download_all",
        "youtarr_download_channel_all",
        "youtarr_ignore_channel_video",
        "youtarr_unignore_channel_video",
        "youtarr_bulk_ignore_channel_videos",
        "youtarr_list_subscription_imports",
        "youtarr_get_active_subscription_import",
        "youtarr_get_subscription_import",
        "youtarr_start_subscription_import",
        "youtarr_cancel_subscription_import",
    ),
    "youtarr_videos": (
        "youtarr_list_videos",
        "youtarr_get_video",
        "youtarr_get_video_metadata",
        "youtarr_get_video_watch_status",
        "youtarr_check_video_members_access",
        "youtarr_get_local_video_status",
        "youtarr_search_videos",
        "youtarr_validate_video_url",
        "youtarr_enrich_videos",
        "youtarr_download_video",
        "youtarr_download_videos",
        "youtarr_download_new_channel_videos",
        "youtarr_update_video_ratings",
        "youtarr_set_video_protected",
        "youtarr_delete_videos",
        "youtarr_preview_auto_removal",
        "youtarr_get_removed_video_summary",
        "youtarr_mark_videos_deleted",
        "youtarr_purge_removed_videos",
    ),
    "youtarr_jobs": (
        "youtarr_list_running_jobs",
        "youtarr_get_job",
        "youtarr_get_video_activity",
        "youtarr_get_current_activity",
        "youtarr_get_download_pause",
        "youtarr_terminate_current_job",
    ),
    "youtarr_playlists": (
        "youtarr_list_playlists",
        "youtarr_get_playlist",
        "youtarr_get_playlist_info",
        "youtarr_subscribe_playlist",
        "youtarr_unsubscribe_playlist",
        "youtarr_update_playlist",
        "youtarr_get_playlist_settings",
        "youtarr_update_playlist_settings",
        "youtarr_list_playlist_videos",
        "youtarr_refresh_playlist",
        "youtarr_sync_playlist",
        "youtarr_preview_playlist_downloads",
        "youtarr_start_playlist_following",
        "youtarr_download_playlist_batch",
        "youtarr_download_playlist",
        "youtarr_regenerate_playlist_m3u",
        "youtarr_ignore_playlist_video",
        "youtarr_unignore_playlist_video",
    ),
    "youtarr_media_servers": (
        "youtarr_get_media_server_status",
        "youtarr_test_jellyfin",
        "youtarr_list_jellyfin_users",
        "youtarr_test_emby",
        "youtarr_list_emby_users",
        "youtarr_get_watch_status_sync",
        "youtarr_sync_watch_status",
        "youtarr_list_plex_libraries",
        "youtarr_get_plex_server_identity",
        "youtarr_refresh_plex_library",
        "youtarr_get_plex_auth_url",
        "youtarr_check_plex_pin",
    ),
    "youtarr_maintenance": (
        "youtarr_list_schedules",
        "youtarr_run_schedule",
        "youtarr_start_rescan",
        "youtarr_get_rescan_status",
        "youtarr_get_ytdlp_version",
        "youtarr_update_ytdlp",
        "youtarr_download_logs",
    ),
    "youtarr_api_keys": (
        "youtarr_list_api_keys",
        "youtarr_create_api_key",
        "youtarr_update_api_key_policy",
        "youtarr_get_api_key_channels",
        "youtarr_replace_api_key_channels",
        "youtarr_replace_api_key_external_access",
        "youtarr_regenerate_api_key",
        "youtarr_revoke_api_key",
    ),
}


def _op_line(name: str, fn: Any) -> str:
    signature = ", ".join(
        parameter.name if parameter.default is inspect.Parameter.empty else f"{parameter.name}={parameter.default!r}"
        for parameter in inspect.signature(fn).parameters.values()
    )
    return f"- {name}({signature}): {' '.join((fn.__doc__ or '').split())}"


def _register_group(group: str, names: tuple[str, ...]) -> None:
    functions = {name: globals()[name] for name in names}

    async def dispatch(operation: str, arguments: JSONObj | None = None) -> JSONVal:
        function = functions.get(operation)
        if function is None:
            raise ToolError(f"Unknown operation {operation!r} for {group}. Valid: {', '.join(functions)}")
        return await function(**(arguments or {}))

    dispatch.__annotations__["operation"] = Literal[names]
    mcp.add_tool(
        Tool.from_function(
            dispatch,
            name=group,
            description=(
                f"{group.removeprefix('youtarr_').replace('_', ' ')} operations on Youtarr. "
                "Pass operation and an arguments dict matching that operation's parameters.\n\n"
                + "\n".join(_op_line(name, function) for name, function in functions.items())
            ),
            annotations=READONLY if group == "youtarr_system" else None,
        )
    )


for _group, _names in _GROUPS.items():
    _register_group(_group, _names)


def main() -> None:
    global _client
    url = os.environ.get("YOUTARR_URL")
    if not url:
        print("YOUTARR_URL environment variable is required (e.g. http://youtarr.example.com:3011)", file=sys.stderr)
        raise SystemExit(1)
    try:
        _client = build_client(
            url,
            os.environ.get("YOUTARR_USERNAME"),
            os.environ.get("YOUTARR_PASSWORD"),
            os.environ.get("YOUTARR_TOKEN"),
        )
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        raise SystemExit(1) from exc
    mcp.run()


if __name__ == "__main__":
    main()
