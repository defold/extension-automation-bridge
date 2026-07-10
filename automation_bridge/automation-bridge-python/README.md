# Automation Bridge Python Helpers

Dependency-free Python helpers for driving a Defold debug build through the
Automation Bridge HTTP API.

## Quick start

When running from this repository, add the wrapper directory to `PYTHONPATH`:

```sh
PYTHONPATH=automation_bridge/automation-bridge-python python3 your_script.py
```

Build, connect, query the scene, and send input:

```python
from automation_bridge import AutomationBridgeClient

bridge = AutomationBridgeClient.from_project(".", build=True)

spawner = bridge.node(type="goc", name_exact="/spawner", visible=True)
bridge.click(spawner)

label = bridge.wait_for_node(
    type="labelc",
    text_exact="L1",
    after_scene_sequence=spawner.scene_sequence,
)
bridge.drag(bridge.parent(label), (500, 300), duration=0.16)

shot = bridge.screenshot(wait=True, resolution_multiplier=0.5)
print(shot.path, shot.frame, shot.scene_sequence, shot.sha256)
```

Use `close_engine()` only when the script intentionally owns engine cleanup:

```python
bridge.close_engine()
```

## Public API

`AutomationBridgeClient` is the main entry point.

- Bootstrap: `AutomationBridgeClient(port)`, `from_project(...)`,
  `from_editor(...)`, and `wait_ready(...)`.
- Capabilities: `health()`, `lifecycle()`, `require(...)`, `supports(...)`, and
  `trace_metadata()`.
- Runtime state: `screen()`, `scene(...)`, `engine_instance_id`,
  `profiler_url`, and `last_window_size`.
- Nodes: `nodes(...)`, `node(...)`, `maybe_node(...)`, `by_id(...)`,
  `parent(...)`, and `count(...)`.
- Input: `click(...)`, `drag(...)`, `drag_path(...)`, `pointer(...)`,
  `type_text(...)`, `key(...)`, and `bridge.input`.
- Application synchronization: `events(...)`, `wait_for_ack(...)`,
  `states(...)`, `state(...)`, `wait_for_state(...)`, `start_command(...)`,
  `command_status(...)`, `cancel_command(...)`, `command(...)`, and `mark(...)`.
- Observation: `wait_for_node(...)`, `wait_for_count(...)`, `wait_frames(...)`,
  `observe_node(...)`, and `wait_for_disappearance(...)`.
- Screenshots and geometry: `screenshot(...)`, `convert_point(...)`,
  `resize(...)`, `set_portrait()`, and `set_landscape()`.
- Diagnostics: `engine_info()`, `engine_log_port()`, `log_stream(...)`,
  `read_logs(...)`, `format_nodes(...)`, and `dump_scene(...)`.
- Lifecycle: `reboot(...)` and `close_engine(...)`.
- Optional namespaces: `bridge.profiler`, `bridge.visual`,
  `bridge.gestures`, `bridge.recording`, and `bridge.trace(...)`.
- Raw escape hatch: `request(method, path, params=..., json=...)`.

The package root intentionally exposes only the main client, editor client,
common response types, and primary exceptions. Advanced backend and protocol
objects live in their respective modules.

## Bootstrap

The usual bootstrap is:

```python
bridge = AutomationBridgeClient.from_project(".", build=True)
```

It reuses the editor recorded in `.internal/editor.port`. If the port file is
missing or stale, it discovers the most recently launched Defold installation,
starts that editor with the project's `game.project`, then builds, discovers
the engine service port, rejects a stale cached engine port belonging to
another engine or project, and waits for `/automation-bridge/v1/health`.

For explicit editor control:

```python
from automation_bridge import AutomationBridgeClient, EditorClient

editor = EditorClient.from_project(".")
editor.build()
bridge = AutomationBridgeClient.from_editor(editor, build=False)
```

`EditorClient` deliberately exposes only `from_project()`, `build()`,
`is_running()`, `installations()`, `latest_installation()`, `preview(...)`,
`console_lines()`, `engine_service_port()`, and `lifecycle_events`. Port-cache,
registration-window, and identity-validation operations are implementation
details of bootstrap.

Defold installations are read from the platform registry introduced for IDE
integration. Entries include launcher path, installation path, and the last
launch timestamp:

```python
for installation in EditorClient.installations():
    print(installation.launcher_path, installation.last_launched_at)

editor = EditorClient.from_project(".")  # reuses or launches automatically
```

Pass `start_if_needed=False` when missing editor state should remain an error,
or `launcher=...` to use an explicit Defold executable.

Render a scene resource without building or running the game:

```python
from pathlib import Path

preview_png = editor.preview(
    "main/main.collection",
    resolution_multiplier=0.5,
)
Path("preview.png").write_bytes(preview_png)
```

Previewable resources are resources with a scene view, including collections,
game objects, GUI scenes, particle effects, and tile maps. Omitted dimensions
use the project display dimensions; explicit dimensions must be from 1 through
4096. `resolution_multiplier` accepts `0.01` through `1.0`, preserves the
project display aspect ratio, and is mutually exclusive with explicit
dimensions. Agents should normally start with `0.5` to reduce transfer and
image-processing cost, then request a larger preview only when fine visual
detail matters.

Connect to an already-running engine service directly:

```python
bridge = AutomationBridgeClient(51337)
bridge.wait_ready()
```

## Capabilities

Declare mandatory capabilities during bootstrap or later with `require()`:

```python
bridge = AutomationBridgeClient.from_project(
    ".",
    required_capabilities=["runtime.lifecycle", "application.events>=1"],
)

bridge.require("scene", "input.drag")
```

Check nonessential functionality without mutating the client:

```python
available = {
    name: bridge.supports(name)
    for name in ("scene", "screenshot", "input.drag")
}
```

Capability declarations may use `name>=N`. Incompatible API versions raise
`IncompatibleApiVersionError`; missing required features raise
`UnsupportedCapabilityError`.

## Raw requests

Named helpers are preferred. For endpoint-level debugging, use the single raw
escape hatch:

```python
health = bridge.request("GET", "/health")
result = bridge.request("POST", "/coordinates/convert", json={
    "point": {"x": 0.5, "y": 0.5},
    "from_space": "normalized_viewport",
    "to_space": "window",
})
```

Raw native endpoint documentation is in [`../README.md`](../README.md).

## Nodes and selectors

`nodes()` returns snapshots. Re-query after input, collection changes, or UI
updates.

```python
labels = bridge.nodes(type="labelc", text="L", limit=100)
restart = bridge.node(name_exact="restart", enabled=True)
maybe_popup = bridge.maybe_node(automation_id="popup")
parent = bridge.parent(labels[0])
total = bridge.count(type="labelc")
```

Substring filters are `type`, `name`, `text`, and `url`. Exact filters include
`type_exact`, `name_exact`, `text_exact`, `url_exact`, `path`, `kind`,
`instance_id`, `logical_id`, `automation_id`, `localization_key`, and `role`.
Boolean filters include `visible`, `enabled`, `has_bounds`, and
`visible_and_enabled`.

`Node` exposes `id`, `snapshot_id`, `instance_id`, `instance_generation`,
`logical_id`, `created_scene_sequence`, `scene_sequence`, `engine_frame`,
`name`, `type`, `kind`, `path`, `parent_id`, `text`, `url`, semantic metadata,
visibility, bounds, children, and `raw`.

## Input

Input targets can be nodes, node ids, point mappings, `(x, y)` pairs, or raw
coordinates:

```python
bridge.click(node)
bridge.click(480, 320)
bridge.drag(first, second, duration=0.2, easing="ease_in_out")
bridge.type_text("Hello")
bridge.key("SPACE")
```

`click()`, `drag()`, `drag_path()`, `type_text()`, and `key()` wait for native
release by default. Use `wait="accepted"`, `wait="started"`, or `wait=False`
when needed.

Low-level queue and interruption control lives under `bridge.input`:

```python
with bridge.input.interruption_scope():
    receipt = bridge.click(node, wait=False)
    bridge.input.wait(receipt, "released")
```

For a continuous held pointer:

```python
with bridge.pointer((100, 100)) as pointer:
    pointer.move((200, 150), duration=0.2)
    pointer.hold(0.1)
```

## Synchronization and observation

Use application events and published state instead of sleeps:

```python
with bridge.events("now") as events:
    bridge.click(button)
    completed = events.wait("operation.complete", timeout=5)

revision = bridge.state("ui").revision
bridge.click(button)
state = bridge.wait_for_state("ui.busy", False, after_revision=revision)
```

Run application commands:

```python
result = bridge.command("reset_fixture", {"seed": 42})
```

Wait for scene evidence:

```python
node = bridge.wait_for_node(
    automation_id="result",
    after_scene_sequence=previous_sequence,
)
stable = bridge.observe_node(logical_id=node.logical_id, minimum_frames=3)
gone = bridge.wait_for_disappearance(node.id)
```

## Screenshots and visual checks

`screenshot(wait=True)` returns an atomic `ScreenshotReceipt` containing the
capture path, engine frame, scene sequence, dimensions, and SHA-256. Runtime
screenshots also accept `resolution_multiplier` from `0.01` through `1.0`:

```python
shot = bridge.screenshot(resolution_multiplier=0.5)
print(shot.path, shot.width, shot.height, shot.sha256)
```

The engine capture remains available at `shot.raw["source_path"]`; the returned
receipt points to a bilinear-downscaled PNG with updated dimensions and hash.
Downscaling requires `wait=True`. Agents should normally start with `0.5` and
use full resolution only when fine detail matters.

Pixel comparisons are explicitly namespaced:

```python
before = bridge.screenshot()
bridge.click(button)
changed = bridge.visual.wait_for_region_change(before, region=(0, 0, 300, 200))

stable = bridge.visual.wait_for_stable_frame(consecutive_frames=3)
error = bridge.visual.difference(before, stable.screenshot)
```

## Generated gestures

Gesture synthesis has one public entry path:

```python
gesture = bridge.gestures.generate_drag(
    (100, 100),
    (500, 300),
    seed=42,
    duration=(0.7, 0.9),
    control_points=4,
)
bridge.drag_path(**gesture)
```

## Recording and traces

Video recording is optional and namespaced:

```python
capabilities = bridge.recording.capabilities()
diagnostics = bridge.recording.permission_diagnostics()

with bridge.recording.start("capture.mp4", crop="content"):
    bridge.click(button)
```

FFmpeg backend classes and recording option types are available from
`automation_bridge.recording` for custom integrations; they are not package-root
API.

Diagnostic traces remain a client-bound context because they intercept client
operations:

```python
with bridge.trace("session.trace.json", screenshots="on_error") as trace:
    bridge.click(button)
    trace.record("checkpoint", {"name": "after click"})
```

Replay is best effort and cannot restore application state, random seeds,
timing mode, or external services.

## Profiling

All profiling is presented through `bridge.profiler`; the underlying stream
protocol is an implementation detail.

```python
resources = bridge.profiler.resources()

capture = bridge.profiler.capture(frames=120, warmup_frames=10)
for scope in capture.scopes(contains="Update"):
    print(scope.path, scope.self.p95_ms)

for counter in capture.counters(contains="Texture"):
    print(counter.path, counter.values.last)
```

Record until the automation script decides the interesting interval is over:

```python
recording = bridge.profiler.start_recording(warmup_frames=10)
try:
    bridge.click(button)
finally:
    capture = recording.stop()
```

Advanced profiler-facing type names live in `automation_bridge.profiler`, such
as `ProfilerCapture`, `ProfilerRecording`, `ProfilerConnection`,
`ProfilerScopeStats`, and `ProfilerCounterStats`.

## Tests

Run the wrapper tests with:

```sh
PYTHONPATH=automation_bridge/automation-bridge-python \
python3 -m unittest tests.test_automation_bridge_api tests.test_tooling
```
