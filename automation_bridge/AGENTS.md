# Native extension guidance

- Work here for native endpoints, wrapper internals, or transport diagnostics.
  Use `automation-bridge-python/` for automation scripts.
- `README.md` is the source of truth for the base URL, endpoints, capabilities,
  response fields, and errors.
- Find the engine service port in the editor console line
  `Engine service started on port <port>`; begin with `GET /health` and validate
  its API version, capabilities, and engine identity.
- Application state, commands, events, and acknowledgements require
  `[automation_bridge] application_api = 1`; other native features do not.
- Query parameters are supported. `POST` and `PUT` also accept a bounded root
  JSON object; query values take precedence. Preserve the documented success and
  error envelopes.
- Elements are snapshots. Prefer semantic selectors, re-query after scene
  changes, and use logical-identity guards for element-targeted input.
  `expected_scene_sequence` guards one exact snapshot; logical IDs guard against
  path reuse across snapshots.
- Use named coordinate spaces and `/coordinates/convert`; do not infer window,
  viewport, display, or GUI transforms.
- Input is FIFO and receipt-based. Wait for the required phase. `text` is literal
  UTF-8; `keys` contains one validated brace-wrapped special key, optionally kept
  pressed for `hold` seconds (`0..60`) before its release. `modifiers` holds up to
  four named keys as a chord across a click, drag, pointer session, or key press
  (pressed one update before the primary action, released one update after).
- Screenshot and Metal captures are asynchronous; poll their status receipts.
  Metal capture requires macOS, the Metal adapter, and
  `METAL_CAPTURE_ENABLED=1`; inspect completed traces with `gpudebug` when
  available.
