# Native extension guidance

- This subtree implements the debug-only HTTP API. Prefer the Python wrapper in
  `automation-bridge-python/` for automation scripts; work at this level for
  native endpoints, wrapper internals, or curl-level diagnostics.
- The endpoint reference and source of truth for transport details, capability
  names, response fields, and error codes is `README.md`.
- The base URL is
  `http://127.0.0.1:<engine_service_port>/automation-bridge/v1`; obtain the port
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
- Nodes are snapshots. Prefer semantic selectors or node ids to coordinates, and
  re-query after input or scene changes. `/nodes` filters and paginates on the
  server; use `limit=0` when only a complete count is needed.
- Coordinates use named top-left spaces. Use `/coordinates/convert` instead of
  inferring viewport, GUI, display, or window transforms.
- Input is queued and receipt-based. Wait for the required input phase, and use
  application events/state/acknowledgements when semantic completion matters.
- Screenshot creation is asynchronous: poll `/screenshot/status` for its atomic
  `complete` receipt; file size is not completion evidence.
