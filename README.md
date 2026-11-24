# youtarr-mcp

Part of the [arr-mcps](https://github.com/arr-mcps/arr-mcps) collection.

FastMCP server for the [Youtarr](https://github.com/DialmasterOrg/Youtarr) v1.95.0 session API. It manages health and storage, configuration, channel subscriptions, videos and downloads, jobs, playlists, Jellyfin/Emby/Plex integration, schedules, maintenance, logs, and API keys through 10 resource-scoped tools.

## Requirements

- Youtarr v1.95.0
- Python 3.11 or newer
- A Youtarr administrator session, or the username and password for one

## Install

From a checkout:

```bash
uv sync
uv tool install .
```

This installs the `youtarr-mcp` command. Configure the environment variables below in the MCP client that launches it.

To run directly from the checkout:

```bash
export YOUTARR_URL=http://youtarr.example.com:3011
export YOUTARR_USERNAME=admin
export YOUTARR_PASSWORD=your-password
uv run youtarr-mcp
```

`.env.example` lists the supported variables without credentials.

## Config

| Env var | Required | Purpose |
|---|---|---|
| `YOUTARR_URL` | yes | Youtarr origin, including its port when needed |
| `YOUTARR_TOKEN` | alternative | Existing browser session token sent as `x-access-token` |
| `YOUTARR_USERNAME` | with password | Administrator username used by `POST /auth/login` |
| `YOUTARR_PASSWORD` | with username | Administrator password used by `POST /auth/login` |

Set either `YOUTARR_TOKEN` or both username and password. A supplied token takes precedence. When a token expires, the server logs in again once if username and password are also available.

Youtarr's `x-api-key` keys are not full API credentials. Legacy keys only queue single-video downloads, `status_read` keys only reach `GET /api/status/summary`, and constrained external keys belong to the separate opt-in `/external-api/v1` namespace. This server does not accept them through `YOUTARR_TOKEN`.

## Tools

Each tool takes an `operation` enum and an `arguments` dict. The tool description lists every operation with its exact Python-style signature. Endpoint functions are not registered as individual MCP tools.

| Tool | Coverage |
|---|---|
| `youtarr_system` | Health, database status, release and yt-dlp version, dashboard summary, storage |
| `youtarr_auth` | Token validation, active sessions, session revocation, logout, password changes |
| `youtarr_config` | Configuration, filename previews, cookies, notifications, YouTube key testing, yt-dlp args, subfolders |
| `youtarr_subscriptions` | Channels, tabs, settings, filters, channel videos, download-all, subscription imports |
| `youtarr_videos` | Video metadata and search, local status, manual downloads, ratings, protection, deletion, auto-removal preview, trash |
| `youtarr_jobs` | Running jobs, job status, live activity, download pause state, current-job termination |
| `youtarr_playlists` | Playlist subscriptions, settings, videos, refresh, media-server sync, following, batch downloads, M3U, ignore state |
| `youtarr_media_servers` | Jellyfin and Emby connection/user checks, watch-status sync, Plex libraries, identity, OAuth, refresh |
| `youtarr_maintenance` | Schedules, manual task runs, filesystem rescans, yt-dlp updates, plain-text log download |
| `youtarr_api_keys` | Key listing, creation, policies, channel grants, regeneration, revocation |

Example:

```json
{
  "name": "youtarr_videos",
  "arguments": {
    "operation": "youtarr_list_videos",
    "arguments": {
      "limit": 20,
      "sort_by": "added",
      "sort_order": "desc",
      "deleted_filter": "exclude"
    }
  }
}
```

Only `youtarr_system` is marked read-only. Every other group contains at least one operation that changes state. Operation docstrings mark writes and destructive actions. Some upstream GET routes also change state: Plex library refresh does so, and successful Plex library checks with test credentials save those credentials.

Responses preserve upstream JSON objects and arrays. The active subscription-import endpoint can return `None` for HTTP 204, and the log endpoint returns plain text.

## API boundaries

- Cookie-file uploads and Google Takeout/cookie subscription previews use multipart uploads and are not exposed.
- Binary video/audio streaming and feed downloads are not exposed.
- The opt-in external API, Sonarr/Radarr bridge, and external-request review APIs are not exposed.
- Session tokens expire after seven days. Username/password authentication allows automatic renewal after a 401.
- Creating or regenerating an API key returns its raw secret once. Store it securely.

## Development

```bash
make help
```

| Command | Action |
|---|---|
| `make sync` | Install or update dependencies with `uv` |
| `make format` | Format Python with Ruff |
| `make lint` | Lint Python with Ruff |
| `make check` | Lint and run the offline suite |
| `make test` | Run mocked-HTTP unit tests |
| `make test-integration` | Run opt-in read-only tests against a live instance |
| `make build` | Build wheel and sdist in `dist/` |
| `make bump-patch` / `bump-minor` / `bump-major` | Update package and lockfile versions |

The integration suite never starts downloads or changes configuration. It requires `YOUTARR_URL` plus either `YOUTARR_TOKEN` or `YOUTARR_USERNAME` and `YOUTARR_PASSWORD`.

`.github/workflows/release.yml` builds and publishes package artifacts when a `v*` tag is pushed.
