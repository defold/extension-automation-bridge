# Automation Bridge Native API Notes

- Purpose: this Defold native extension exposes a debug-build HTTP API for runtime automation: health/screen checks, scene and node discovery, click/drag/key input, and screenshots.
- Prefer the Python wrapper in `automation_bridge/automation-bridge-python` for scripts and tests. Use this raw API only for curl-level debugging, wrapper work, or endpoints not yet wrapped.
- Find the engine service port in the editor console line `Engine service started on port <port>`.
- Base URL: `http://127.0.0.1:<engine_service_port>/automation-bridge/v1`.
- No Lua setup is required for v1; the API is available when Defold's engine service web server is running.
- Requests use query parameters only, not JSON bodies. Success shape is `{ "ok": true, "data": ... }`; errors use `{ "ok": false, "error": { "code": ..., "message": ... } }`.
- Call `GET /health` first. Require `ok: true`, inspect `data.capabilities`, and use `GET /screen` when window/display/viewport metadata matters. When `screen.resize` is present, use `PUT /screen?width=...&height=...` to set the native Defold window size.
- Coordinates are top-left screen pixels. Prefer node ids over raw coordinates whenever possible.
- Discover targets with `GET /nodes` or `GET /scene`; fetch details with `GET /node?id=...`.
- `/nodes` filters: `id`, `type`, `name`, `text`, `url`, `visible`, `include`, and `limit`. `type`/`name`/`text`/`url` are case-insensitive substring matches. Use `limit=0` for counts.
- Include data with `include=bounds`, `properties`, `children`, `all`, or a comma-separated combination. Basic node fields are always returned.
- Node ids are stable only for the current scene shape. Re-discover nodes after clicks, drags, collection changes, or UI updates.
- Input endpoints: `POST /input/click?id=...` or `x/y`; `POST /input/drag?from_id=...&to_id=...&duration=...` or `x1/y1/x2/y2`; `POST /input/key?text=...` or URL-encoded `keys=%7BKEY_ENTER%7D`.
- Mouse/touch input visualization is on by default. Use `visualize=0` on click/drag endpoints when the overlay would interfere with diagnostics.
- `GET /screenshot` schedules capture and returns a file path; wait until that file exists and has nonzero size.
- Full endpoint details live in `automation_bridge/README.md`.
