# Automation Bridge Python Wrapper Notes

- Purpose: dependency-free Python helpers for driving a Defold debug build
  through the Automation Bridge HTTP API.
- Add `automation_bridge/automation-bridge-python` to `PYTHONPATH`, then import
  the focused package-root API from `automation_bridge`.
- Normal bootstrap: `editor.open_project(".").build_and_run()`.
- Declare mandatory features with `required_capabilities=[...]` or
  `game.require(...)`; check optional features with `game.supports(...)`.
- Use `game.request(method, path, params=..., json=...)` only as the raw engine API
  escape hatch. Prefer named helpers in automation scripts.
- Exact, boolean, identity, and pagination selectors are native. `count()` is
  complete regardless of response page limits.
- `Element` objects are snapshots. Re-query after input or scene changes.
- Component elements often expose labels while their parent game object receives
  input; use `game.parent(component_element)`.
- Input accepts elements, ids, point mappings, tuples, or raw coordinates.
- `screenshot(wait=True)` returns an atomic completion receipt. Prefer
  `resolution_multiplier=0.5` for initial agent inspection; the returned
  receipt describes the downscaled PNG and retains the native path in
  `raw["source_path"]`.
- Observation uses `wait_for_element(...)`, optionally with
  `after_scene_sequence`, `observe_element(...)`, and
  `wait_for_disappearance(...)`.
- Optional tools are namespaced: `game.visual`, `game.gestures`,
  `game.video_recording`, `game.profiler`, and `game.trace(...)`.
- `game.video_recording` uses a native recorder embedded in the engine: macOS 15+
  uses ScreenCaptureKit with application audio; Windows 10 version 1903+ uses
  Windows Graphics Capture and Media Foundation for video only. Omit `audio`
  for the platform default, or pass `audio=False` portably. No FFmpeg is used.
- Profiling is publicly named only as profiling. The internal stream protocol
  implementation lives in `remotery.py`; callers use `game.profiler` and
  profiler-named types from `automation_bridge.profiler`.
- `editor.open_project(...)` reuses a healthy editor from
  `.internal/editor.port`; if absent or stale, it uses the newest launcher from
  Defold's platform installation registry and starts the editor for the project.
  `installations()` and `latest_installation()` expose that discovery data.
- `project.preview.render(...)` renders scene resources to PNG without running the
  game. Prefer `resolution_multiplier=0.5` for initial agent inspection; use a
  larger multiplier or explicit dimensions only when fine detail matters.
  Registration parsing, port caching, and identity validation are private
  bootstrap mechanics.
- Full user/API documentation lives in `README.md`; raw endpoint documentation
  lives in `../README.md`.
