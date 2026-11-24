# AGENTS.md - youtarr-mcp

FastMCP server for the Youtarr v1.95.0 session API. Routes and payloads are verified against upstream tag `v1.95.0`, commit `88720288a074861b5e78fa278cf930ea36410637`.

## Tool design

- Expose 10 resource-scoped portmanteau tools through `_GROUPS`, not one MCP tool per endpoint.
- Every endpoint is a plain async function. `_register_group` creates one `dispatch(operation, arguments)` closure per group and types `operation` with `Literal[names]`.
- `tests/test_tools.py::test_all_endpoint_functions_grouped_exactly_once` is the registry safety net. Every module-level `youtarr_*` coroutine outside `_GROUPS` must appear in exactly one group.
- Keep the total at 5 to 15 tools. Add operations to an existing resource group unless a new group is necessary.
- `youtarr_system` is the only all-read-only group. Mixed groups have no read-only hint, and write/destructive behavior is stated in operation docstrings.
- Return annotations must include JSON objects, arrays, strings, and `None`: logs are plain text and the active import endpoint returns HTTP 204 when idle.

## Authentication

- `YOUTARR_URL` is required.
- `YOUTARR_TOKEN` uses an existing session token directly.
- Otherwise both `YOUTARR_USERNAME` and `YOUTARR_PASSWORD` are required. `YoutarrClient` logs in lazily through `POST /auth/login` and sends the returned token as `x-access-token`.
- A 401 triggers one fresh login and retry when credentials exist. Token-only clients surface the 401.
- Do not add an `x-api-key` fallback. Legacy download keys, status keys, and constrained external keys do not provide full session access.
- Never log or place credentials in source, tests, docs, or tool output. Tests use synthetic values only.

## API boundaries

- Youtarr v1.95.0 session routes are the compatibility target. Re-check upstream route source and Swagger annotations before adding or changing an operation.
- Multipart cookie/Takeout uploads, binary streams, feeds, the opt-in external API, the Sonarr/Radarr bridge, and external-request review are outside this server.
- `POST /api/keys` and key regeneration reveal raw secrets once.
- Some GET routes mutate state. `/refreshlibrary` refreshes Plex, and `/getplexlibraries` saves successful test credentials. Keep their groups mixed and their docstrings explicit.

## Tests

- `make test` runs the offline suite with `httpx.MockTransport` and FastMCP's in-memory client.
- `make test-integration` runs read-only live checks only when URL and auth variables are set.
- Do not add live tests that create downloads, alter configuration, revoke keys, or run maintenance.
- Use `make format`, `make lint`, and `make build` before handing off changes.

## Release workflow

The package starts at `0.0.0`. Use `make bump-patch`, `make bump-minor`, or `make bump-major` so `pyproject.toml` and `uv.lock` change together. A release commit message is only the version, and tags use `v<version>`. Do not edit the version by hand.
