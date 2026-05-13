# Native API Automation Bridge Notes

- Use this raw HTTP API when you need curl-level access or are implementing a wrapper; otherwise prefer `automation_bridge/python`.
- Discover the engine service port from the editor console line `Engine service started on port <port>`.
- Base URL: `http://127.0.0.1:<engine_service_port>/automation-bridge/v1`.
- Call `/health` first and require `ok: true` before scene queries or input.
- Discover targets with `/nodes`, then use returned stable `id` values for `/node`, `/input/click`, and `/input/drag`.
- Coordinates are top-left screen pixels. Prefer node ids over coordinates when possible.
- Requests use query parameters, not JSON bodies.
- `/screenshot` schedules capture and returns a file path; wait for the file to exist and have nonzero size.
