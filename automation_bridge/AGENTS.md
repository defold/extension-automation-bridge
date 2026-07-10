# Automation Bridge Native API Notes

- Purpose: this Defold native extension exposes a debug-build HTTP API for runtime automation: health/screen checks, scene and node discovery, click/drag/key input, and screenshots.
- Prefer the Python wrapper in `automation_bridge/automation-bridge-python` for scripts and tests. Use this raw API only for curl-level debugging, wrapper work, or endpoints not yet wrapped.
- Find the engine service port in the editor console line `Engine service started on port <port>`.
- Base URL: `http://127.0.0.1:<engine_service_port>/automation-bridge/v1`.
- No Lua setup is required for v1; the API is available when Defold's engine service web server is running.
- Requests use query parameters only, not JSON bodies. Success shape is `{ "ok": true, "data": ... }`; errors use `{ "ok": false, "error": { "code": ..., "message": ... } }`.
- Call `GET /health` first. Require `ok: true`, validate `data.version`, inspect `data.capability_versions`, and retain `data.identity.engine_instance_id` when caching a port. Use `GET /lifecycle` for native registration/health/scene-ready stages. Use `GET /screen` only when the `screen` capability exists; when `screen.resize` is present, use `PUT /screen?width=...&height=...` to set the native Defold window size.
- Capabilities are backend-driven. Routes whose graphics, game-object, window, or HID context is absent return `501 unsupported_capability`; health/lifecycle remain the attachment contract.
- Coordinates are top-left screen pixels. Prefer node ids over raw coordinates whenever possible.
- Discover targets with `GET /nodes` or `GET /scene`; fetch details with `GET /node?id=...`.
- `/nodes` applies exact, substring, boolean, identity, and pagination filters
  server-side. Responses include `matched`, `truncated`, `next_cursor`,
  `scene_sequence`, and `engine_frame`; use `limit=0` for complete counts.
- Include data with `include=bounds`, `properties`, `children`, `all`, or a comma-separated combination. Basic node fields are always returned.
- Node ids are stable only for the current scene shape. Re-discover nodes after clicks, drags, collection changes, or UI updates.
- Input endpoints: `POST /input/click?id=...` or `x/y`; `POST /input/drag?from_id=...&to_id=...&duration=...` or `x1/y1/x2/y2`; `POST /input/key?text=...` or URL-encoded `keys=%7BKEY_ENTER%7D`.
- Mouse/touch input visualization is on by default. Use `visualize=0` on click/drag endpoints when the overlay would interfere with diagnostics.
- `GET /screenshot` returns a capture id. Poll `/screenshot/status` until its
  atomic receipt is `complete`; do not infer completion from file size.
- Node ids are snapshot/path ids. Use `logical_id`/instance generation when
  available, and `expected_scene_sequence` for stale node-target enforcement.
- Coordinates use named top-left spaces and `/coordinates/convert`; do not infer
  world, GUI-layout, monitor, or outer-window transforms.
- Full endpoint details live in `automation_bridge/README.md`.
