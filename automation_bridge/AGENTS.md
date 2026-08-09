# Native extension guidance

- This subtree implements the debug-only HTTP API. Prefer the Python wrapper in
  `automation-bridge-python/` for automation scripts; work at this level for
  native endpoints, wrapper internals, or curl-level diagnostics.
- The endpoint reference and source of truth for transport details, capability
  names, response fields, and error codes is `README.md`.
- The base URL is
  `http://127.0.0.1:<engine_service_port>/automation-bridge/v2`; obtain the port
  from the editor console's `Engine service started on port <port>` line.
- No Lua setup is needed for native inspection, input, capture, events, or
  markers. Application-defined state, commands, events, and acknowledgements
  require `[automation_bridge] application_api = 1` in `game.project`.
- Query parameters are supported. `POST` and `PUT` also accept a bounded root
  `application/json` object; query values win when both transports supply a key.
  Responses use `{ "ok": true, "data": ... }` or
  `{ "ok": false, "error": { "code": ..., "message": ... } }`.
- Start with `GET /health`: validate `version`, negotiate
  `capability_versions`, and retain `identity.engine_instance_id` when caching a
  port. Unsupported backend features return `501 unsupported_capability`.
- Elements are snapshots. Prefer semantic selectors or element ids to coordinates,
  and re-query after input or scene changes. `/elements` filters and paginates on the
  server; use `limit=0` when only a complete count is needed.
- For element-targeted input, use `expected_logical_id` on clicks and
  `expected_from_logical_id`/`expected_to_logical_id` on drags to reject reused
  path ids with `409 stale_element`. `expected_scene_sequence` remains the
  stricter exact-snapshot guard.
- Coordinates use named top-left spaces. Use `/coordinates/convert` instead of
  inferring viewport, GUI, display, or window transforms.
- Input is queued and receipt-based. Wait for the required input phase, and use
  application events/state/acknowledgements when semantic completion matters.
  The `/input/key` `text` field is literal UTF-8; `keys` uses validated
  brace-wrapped names such as `{KEY_ENTER}` and rejects invalid names with
  `unsupported_key`.
- Screenshot creation is asynchronous: poll `/screenshot/status` for its atomic
  `complete` receipt; file size is not completion evidence.
- On macOS with the Metal adapter,
  `POST /metal?path=/absolute/output.gputrace&frames=1` schedules a frame capture;
  poll `GET /metal` for completion or use `DELETE /metal` to stop early. Launch
  with `METAL_CAPTURE_ENABLED=1`.
- When available, analyze a completed trace with
  `gpudebug -t /absolute/output.gputrace`; use `list`, `go`, `info`, `find`, and
  `fetch` to inspect commands, pipelines, bindings, and attachments. Check
  installation with `xcrun --find gpudebug`.
