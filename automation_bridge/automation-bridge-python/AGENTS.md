# Automation Bridge Python Wrapper Notes

- Purpose: dependency-free Python helpers for driving a Defold debug build through the Automation Bridge HTTP API. Use it for editor/bootstrap automation, runtime scene inspection, node selection, input, screenshots, logs, window control, reboot/shutdown, resource data, and Remotery profiling.
- Setup from this repo: add `automation_bridge/automation-bridge-python` to `PYTHONPATH` or `sys.path`, then import `AutomationBridgeClient`, `EditorClient`, or `wait_until` from `automation_bridge` as needed.
- Public APIs have docstrings. Use `help(AutomationBridgeClient)`, `help(AutomationBridgeClient.drag)`, or `.__doc__` for quick in-code reference.
- Normal bootstrap: `bridge = AutomationBridgeClient.from_project(".", build=True)`. It builds through the editor, discovers/caches the engine service port and, when present in fresh logs, the Remotery URL, handles stale reused engine ports, and waits for `/automation-bridge/v1/health`. If Remotery discovery is missing, `bridge.profiler.remotery()` falls back to Defold's default port; pass `url=` or `port=` to override it.
- Use `AutomationBridgeClient.from_editor(editor, build=True)` for explicit editor control, `AutomationBridgeClient(port)` for an already known engine port, and `AUTOMATION_BRIDGE_ENGINE_PORT` only when a running port is known.
- Core API also includes `convert_point(...)`, `wait_frames(...)`, transient
  observation receipts, `assert_node(...)`, atomic screenshot receipts, and the
  explicit visual stability/change layer.
- Exact, boolean, identity, and pagination selectors are native. `count()` is
  complete regardless of the response page limit.
- `Node` objects are snapshots with fields like `id`, `name`, `type`, `kind`, `path`, `parent_id`, `text`, `url`, `visible`, `enabled`, `bounds`, `center`, `children`, and `raw`. Re-query after clicks, drags, collection changes, or UI updates.
- Component nodes often expose useful labels/properties while the parent game object receives input. Use `bridge.parent(component_node)` before clicking or dragging when needed.
- Input accepts nodes, node ids, point mappings, `(x, y)` tuples, or raw `x, y` coordinates. Coordinates are top-left screen pixels. `drag(..., duration=...)` waits for the queued drag to finish by default.
- Mouse/touch input visualization is enabled by default; pass `visualize=False` to `click()` or `drag()` when needed.
- `bridge.screenshot(wait=True)` waits for a native atomic completion receipt
  with frame/sequence/dimensions/SHA-256. Pixel assertions remain in the
  optional `automation_bridge.visual` layer.
- Engine logs: `bridge.log_stream()` and `bridge.read_logs(duration=..., limit=...)` read future Defold TCP log lines; `EditorClient.console_lines()` reads existing editor console history.
- Engine control: `resize(width, height)`, `set_portrait()`, `set_landscape()`, `reboot(*args, wait=True)`, and `close_engine()`. Only call `close_engine()` when intentionally shutting down the running engine. For editor-launched reboots that must return to Automation Bridge, pass explicit project/bootstrap args.
- Profiling: `bridge.profiler.resources()` reads Defold `/resources_data`; `bridge.profiler.capture(frames=..., warmup_frames=...)` captures Remotery frames; `bridge.profiler.start_recording(...)` records until your script stops it. Use `capture.scopes(...)` for timing stats and `capture.counters(...)` for Remotery property counters.
- Full user/API docs live in `README.md`; raw endpoint docs live in `../README.md`.
