# Automation Bridge Python Wrapper Notes

- Purpose: dependency-free Python helpers for driving a Defold debug build
  through the Automation Bridge HTTP API.
- Add `automation_bridge/automation-bridge-python` to `PYTHONPATH`, then import
  the focused package-root API from `automation_bridge`.
- Normal bootstrap: `AutomationBridgeClient.from_project(".", build=True)`.
- Declare mandatory features with `required_capabilities=[...]` or
  `bridge.require(...)`; check optional features with `bridge.supports(...)`.
- Use `bridge.request(method, path, params=..., json=...)` only as the raw API
  escape hatch. Prefer named helpers in automation scripts.
- Exact, boolean, identity, and pagination selectors are native. `count()` is
  complete regardless of response page limits.
- `Node` objects are snapshots. Re-query after input or scene changes.
- Component nodes often expose labels while their parent game object receives
  input; use `bridge.parent(component_node)`.
- Input accepts nodes, ids, point mappings, tuples, or raw coordinates.
- `screenshot(wait=True)` returns an atomic completion receipt.
- Observation uses `wait_for_node(...)`, optionally with
  `after_scene_sequence`, `observe_node(...)`, and
  `wait_for_disappearance(...)`.
- Optional tools are namespaced: `bridge.visual`, `bridge.gestures`,
  `bridge.recording`, `bridge.profiler`, and `bridge.trace(...)`.
- Profiling is publicly named only as profiling. The internal stream protocol
  implementation lives in `remotery.py`; callers use `bridge.profiler` and
  profiler-named types from `automation_bridge.profiler`.
- `EditorClient` exposes build, console, discovered primary port, and lifecycle
  diagnostics. Registration parsing, port caching, and identity validation are
  private bootstrap mechanics.
- Full user/API documentation lives in `README.md`; raw endpoint documentation
  lives in `../README.md`.
