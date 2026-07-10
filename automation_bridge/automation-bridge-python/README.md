# Automation Bridge Python Helpers

Small dependency-free Python helpers for driving the Automation Bridge extension from tests and local automation scripts.

The package uses only the Python standard library and wraps the existing curl-friendly `/automation-bridge/v1` API.

## Quick Start

```python
from automation_bridge import AutomationBridgeClient

bridge = AutomationBridgeClient.from_project(".", build=True)

spawner = bridge.node(type="goc", name="spawner", visible=True)
bridge.click(spawner)

items = bridge.nodes(type="labelc", text_exact="L1", limit=100)
bridge.drag(bridge.parent(items[0]), bridge.parent(items[1]), duration=0.16)

shot = bridge.screenshot(wait=True)
print(shot.path, shot.frame, shot.scene_sequence, shot.sha256)

with bridge.log_stream(read_timeout=1.0) as logs:
    print(logs.readline(timeout=1.0))
```

Close the running engine when a script needs to clean up after itself:

```python
bridge.close_engine()
```

Resize the running engine window or swap the last known size between portrait and landscape:

```python
bridge.resize(1280, 720)
bridge.set_portrait()
bridge.set_landscape()
```

When running from this repository without installing anything, add `automation_bridge/automation-bridge-python` to `PYTHONPATH`:

```sh
PYTHONPATH=automation_bridge/automation-bridge-python python3 tests/test_automation_bridge_api.py
```

The repository also includes a runtime smoke test for game-object bounds:

```sh
PYTHONPATH=automation_bridge/automation-bridge-python python3 examples/gameobject_bounds.py
```

It uses a sample fixture whose sprite is offset from the parent game object origin, so it fails if parent game-object bounds collapse to the raw Defold world position.

An interruption-safe input/recording example is available as
`examples/interruption_safe.py`; it demonstrates session flushing, screenshot
and native input waits, structured timeout diagnostics, and profiler
finalization/abort hooks.

## Public API Surface

`AutomationBridgeClient` is the main entry point.

- Bootstrap: `AutomationBridgeClient(port)`, `from_project(...)`, `from_editor(...)`, and `wait_ready(...)`.
- Raw API passthrough: `get(...)`, `post(...)`, `put(...)`, `post_json(...)`, and `put_json(...)`.
- Runtime state: `health()`, `screen()`, `scene(...)`, `remotery_url`, and `last_window_size`.
- Node queries: `nodes(...)`, `node(...)`, `maybe_node(...)`, `by_id(...)`, `parent(...)`, and `count(...)`.
- Input: `click(...)`, `drag(...)`, `drag_path(...)`, `pointer(...)`, `type_text(...)`, `key(...)`, and `bridge.input` queue/status/cancel/flush/configure methods.
- Application synchronization: `events(...)`, `wait_for_ack(...)`, `states(...)`, `state(...)`, `wait_for_state(...)`, `start_command(...)`, `command_status(...)`, `cancel_command(...)`, `command(...)`, and `mark(...)`.
- Snapshot safety: `convert_point(...)`, `wait_frames(...)`, `wait_for_appearance(...)`, `wait_for_disappearance(...)`, `observe_node(...)`, and `assert_node(...)`.
- Screenshots/visual fallback: `screenshot(...)`, `wait_for_stable_frame(...)`, `wait_for_region_change(...)`, and the explicit `VisualClient`/`difference(...)` package.
- Engine diagnostics: `engine_info()`, `engine_log_port()`, `log_stream(...)`, `read_logs(...)`, `format_nodes(...)`, and `dump_scene(...)`.
- Engine control: `resize(...)`, `set_portrait(...)`, `set_landscape(...)`, `reboot(...)`, and `close_engine(...)`.
- Profiling: `bridge.profiler.resources()`, `bridge.profiler.remotery(...)`, `bridge.profiler.capture(...)`, and `bridge.profiler.start_recording(...)`.

`EditorClient` exposes editor-oriented helpers for advanced bootstrap flows: `from_project(...)`, `build(...)`, `console_lines()`, `engine_service_port()`, `engine_service_ports()`, `latest_registration_engine_service_ports()`, `latest_registration_has_engine_service_port()`, `last_build_had_engine_service_port()`, `endpoint_registered_count()`, `remotery_url()`, `remotery_urls()`, `latest_registration_remotery_urls()`, `remember_engine_service_port(...)`, and `remember_remotery_url(...)`.

Typed response wrappers include `Node`, `Bounds`, `InputReceipt`, `PointerSession`,
`ScreenshotReceipt`, `ObservationReceipt`, `VisualObservation`,
`ResourceProfileEntry`, and the Remotery types. `InputController`,
`InputInterruptionScope`, `EngineLogStream`, `ProfilerClient`, `VisualClient`,
`RemoteryClient`, `FinalizationHooks`, and `WaitTimeoutError` are also exported
for direct use. These are lightweight snapshots around native engine data;
re-query after clicks, drags, or scene changes.

## Bootstrap

Use the editor client when you want explicit control over the Defold editor server. `AutomationBridgeClient.from_editor(..., build=True)` closes known candidate engine ports before building so repeated runs start from a fresh engine process. `build()` waits until the runtime logs `Automation Bridge endpoint registered`, then waits another 0.2 seconds before returning.
When the editor reuses the same running engine process, it may not print a fresh engine service port before `Automation Bridge endpoint registered`; `EditorClient` reuses the last port it saw and stores it in `.internal/automation_bridge.engine.port`.
The same bootstrap reads Defold's `Initialized Remotery (ws://.../rmt)` log line when it is present and stores it in `.internal/automation_bridge.remotery.url`, so `bridge.profiler.remotery()` can connect without a hard-coded port when discovery succeeds. If no fresh Remotery URL is discovered, the helper falls back to Defold's default Remotery port; pass `url=` or `port=` to override it.
If the latest build logs `Automation Bridge endpoint registered` without a fresh engine service port before it, `AutomationBridgeClient.from_editor(..., build=True)` immediately tries to close candidate engine ports with `@system/exit`, rebuilds once, and retries. It uses the same recovery path if discovered ports do not serve `/automation-bridge/v1/health`.

```python
from automation_bridge import AutomationBridgeClient, EditorClient

editor = EditorClient.from_project(".")
editor.build()

bridge = AutomationBridgeClient.from_editor(editor, build=False)
bridge.wait_ready()
```

Use an already-started engine service port directly:

```python
from automation_bridge import AutomationBridgeClient

bridge = AutomationBridgeClient(51337)
bridge.wait_ready()
```

## Raw API and Scene Data

Use the named helpers for normal scripts:

```python
health = bridge.health()
screen = bridge.screen()
scene = bridge.scene(visible=True, include=["bounds", "properties"])

label_count = bridge.count(type="labelc")
bridge.click(480, 320)
bridge.resize(1280, 720)
```

`health()` and `screen()` update `bridge.last_window_size` from the engine response. `scene()` returns the full native scene-tree payload with `count`, `visible_filter`, `screen`, and `root`.

Raw `get()`, `post()`, and `put()` are available for curl-level debugging.
`post_json()` and `put_json()` send an `application/json` root object; the
native bridge enforces a 65,536-byte body and 16-level nesting limit.

Named geometry uses a top-left origin. Convert only spaces the engine can know:

```python
window_point = bridge.convert_point(
    (0.5, 0.5),
    from_space="normalized_viewport",
    to_space="window",
)
```

Supported spaces are `window`, `client`, `display_pixels`, `backbuffer`,
`viewport`, and `normalized_viewport`. `display_pixels` is scoped to the
drawable window; public Defold APIs do not expose global monitor or decorated
outer-window rectangles. World and GUI-layout conversion require an
application-published transform or the proposed DMSDK APIs in
[`docs/DEFOLD_PUBLIC_AUTOMATION_GEOMETRY.md`](../../docs/DEFOLD_PUBLIC_AUTOMATION_GEOMETRY.md).

## Selectors

`nodes()` returns a list of snapshot `Node` objects. `node()` expects exactly one match. `maybe_node()` allows zero or one.

```python
restart = bridge.node(name_exact="restart", enabled=True)
labels = bridge.nodes(type="labelc", text="L", limit=100)
count = bridge.count(type="labelc", text_exact="L2")
detail = bridge.by_id(restart.id)
item = bridge.parent(labels[0])
```

All selectors are applied server-side: substring filters `type`, `name`,
`text`, and `url`; exact filters `type_exact`, `name_exact`, `text_exact`,
`url_exact`, `path`, `kind`, `instance_id`, and `logical_id`; boolean filters
`visible`, `enabled`, `has_bounds`, and `visible_and_enabled`; plus
semantic exact filters `automation_id`, `localization_key`, and `role`; and
`case_sensitive`, `limit`, `offset`/`cursor`, and `include`.

`include` may be either a comma-separated string or an iterable:

```python
node = bridge.node(name="spawner", include=["bounds", "properties"])
```

Native substring filters are case-insensitive unless `case_sensitive=True`.
Exact filters are always case-sensitive. `count()` requests `limit=0`, so its
result is complete even when more than 500 nodes match. Page responses report
`truncated` and `next_cursor`; selector errors include total server matches,
truncation, frame/sequence, active collections, candidates, and suggestions.

`Node` exposes convenience properties for the native node payload:

```python
node.id
node.snapshot_id
node.instance_id
node.instance_generation
node.logical_id
node.created_scene_sequence
node.scene_sequence
node.engine_frame
node.name
node.type
node.kind
node.path
node.parent_id
node.text
node.url
node.automation_id
node.localization_key
node.role
node.visible
node.enabled
node.raw

if node.bounds:
    print(node.bounds.screen["x"], node.bounds.screen["y"])
    print(node.bounds.x, node.bounds.y, node.bounds.w, node.bounds.h)
    print(node.center["x"], node.center["y"])
    print(node.bounds.normalized["x"], node.bounds.normalized["y"])

for child in node.children:
    print(child.compact())
```

## Input

```python
click = bridge.click(node)
print(click.input_id, click.state, click.start_frame, click.release_frame)

bridge.click(node.id)
bridge.click(480, 320)
bridge.click({"center": {"x": 480, "y": 320}})
bridge.click(node, expected_scene_sequence=node.scene_sequence)

bridge.drag(first_node, second_node, duration=0.16)
bridge.drag(
    first_node,
    second_node,
    duration=0.16,
    easing="ease_in_out",
    hold_before=0.04,
    hold_after=0.02,
    visualize=False,
)

path = bridge.drag_path(
    [(100, 200), (160, 140), (240, 220), (320, 180)],
    durations=[0.15, 0.22, 0.18],
    easing=["ease_in", "ease_in_out", "ease_out"],
    hold_before=0.08,
    hold_after=0.04,
)

bridge.type_text("hello")
bridge.key("KEY_ENTER")
```

Input is native FIFO: one gesture/key action owns HID state at a time. Each `AutomationBridgeClient` creates stable, compact `client_id` and `session_id` values and a new compact `request_id` per mutation; callers may pass explicit ids to the constructor for external correlation. Input mutations use `application/json` bodies so ids, text, and paths are not truncated by Defold's bounded request-resource string; raw query forms remain available for curl-level use. The first session holds a renewable controller lease while other clients remain read-only observers.

`click()`, `drag()`, and `drag_path()` default to `wait="released"`. This polls the native receipt instead of sleeping for the requested duration. `released` means the engine injected the final up; it does not claim application acceptance. Use `wait="accepted"`, `wait="started"`, or `wait=False` as needed. `InputReceipt` remains dictionary-compatible and exposes keys as attributes. It includes accepted/start/release frames and monotonic timestamps, requested/actual duration, failure/cancellation reason, all correlation ids, engine instance/frame, scene sequence, device, and pointer id.

Configure exclusive devices and inspect/control the queue through `bridge.input`:

```python
bridge.input.configure(device="mouse", visualize=False, lease=10)
pending = bridge.input.pending()
current = bridge.input.status(path.input_id)
bridge.input.cancel(current.input_id, release=True)
bridge.input.flush(release=True)
```

`auto`, `mouse`, and `touch` are capability-gated by the native endpoint. A gesture uses exactly one device and never produces duplicate mouse+touch actions. Touch is conservatively supported on native touch platforms because Defold's public HID API does not expose a reliable connected-touch-hardware query.

Continuous sampled, quadratic, and cubic paths contain exactly one down and one up. Sampled/linear paths take one duration/easing per segment; quadratic paths take three points and one duration/easing; cubic paths take four. Native validation limits paths to 2–128 points and total gesture time to 60 seconds. Visualization follows positions actually injected each engine update, including curves.

For low-level control, use a leased pointer context. Normal exit injects up; exceptions and `KeyboardInterrupt` request cancellation/release without masking the original exception; lease expiry also cleans up if the Python process disappears:

```python
with bridge.pointer((100, 200), lease=2.0) as pointer:
    pointer.move((180, 160), duration=0.2, easing="ease_in_out")
    pointer.hold(0.1)
    pointer.move((300, 220), duration=0.3, easing="ease_out")
```

Input waits cancel the active input on interruption by default. `click()`, `drag()`, `drag_path()`, `type_text()`, and `key()` expose `cancel_on_interrupt` and `flush_on_interrupt`; use the latter when interruption should also cancel later inputs from this session. Cleanup always requests `release=True`, so the native queue releases an active mouse button or key and cancels an active touch contact.

When an interruption may occur after a non-waiting input call—for example in an application event wait or screenshot wait—wrap the complete operation in a session cleanup scope:

```python
with bridge.input.interruption_scope():
    bridge.drag(item, target, wait=False)
    wait_for_application_event()
```

The scope flushes this client's active and later queued actions only when an exception leaves the block. Native controller and pointer leases remain the last-resort release mechanism if the Python process exits without running cleanup. Log reads close their socket and preserve `KeyboardInterrupt`; socket closure and timeout-restoration failures cannot mask the original exception.

`Node.id`/`snapshot_id` hashes the traversal path and is tied to a scene shape.
Nodes backed by a public Defold game object instance also expose
`instance_id`, `instance_generation`, `logical_id`, and
`created_scene_sequence`. Pass `expected_scene_sequence` when an old snapshot
must fail with `stale_scene` instead of resolving its id against a newer graph.

Pass `expected_scene_sequence=scene["scene_sequence"]` to click/drag/path/pointer/key calls when stale target protection matters. The native endpoint rejects a changed scene with `stale_scene` before resolving node ids, and every receipt records the scene sequence actually used.

Component nodes often expose useful text or properties, while their parent game object is the actionable target. Use `bridge.parent(component_node)` before clicking or dragging when needed:

```python
label = bridge.node(type="labelc", text_exact="L1")
item = bridge.parent(label)
bridge.drag(item, (480, 320))
```

## Waits and Screenshots

```python
from automation_bridge import AutomationBridgeError, WaitTimeoutError, wait_until

wait_until(lambda: bridge.count(type="labelc", text_exact="L2") >= 1)

node = bridge.wait_for_node(name_exact="restart", enabled=True)
bridge.wait_for_count(2, type="labelc", text_exact="L1")
bridge.wait_frames(2)

cursor = bridge.scene()["scene_sequence"]
appeared = bridge.wait_for_appearance(name_exact="result", after_scene_sequence=cursor)
observed = bridge.observe_node(name_exact="result", minimum_frames=3, identity="logical")
gone = bridge.wait_for_disappearance(appeared.node.snapshot_id)

shot = bridge.screenshot(after_frames=2, wait=True, timeout=5)
print(shot.capture_id, shot.path, shot.frame, shot.width, shot.height, shot.sha256)
```

Screenshot completion comes from native `/screenshot/status`, not file-size
polling. The native side writes a temporary PNG, hashes it, atomically renames
it, then publishes a `complete` receipt. `ScreenshotReceipt` is path-like and
also provides `exists()`, `stat()`, and `read_bytes()` for compatibility.

Scene/state assertions remain the default:

```python
bridge.assert_node(name_exact="operation_status", visible=True, enabled=True)
```

Pixel comparisons are an explicit fallback layer and never affect selectors:

```python
before = bridge.screenshot()
changed = bridge.wait_for_region_change(before, region=(100, 100, 300, 200), tolerance=0.01)
stable = bridge.wait_for_stable_frame(region=(100, 100, 300, 200), consecutive_frames=3)
```

The metric is normalized mean absolute RGB channel error in native screenshot
pixels. Alpha is ignored unless `include_alpha=True`; images are never scaled;
and every consecutive sample is a distinct native capture. Regions are
top-left viewport pixels.

`wait_until(fn, timeout=..., interval=..., message=...)` polls until `fn()` returns a truthy value. By default exceptions escape immediately, which keeps `TypeError`, `AttributeError`, and other programming mistakes from being hidden until timeout. Explicitly list transient failures that are safe to retry:

```python
result = wait_until(
    read_application_state,
    retry_exceptions=(AutomationBridgeError,),
    predicate=lambda state: state.get("ready") is True,
    timeout=10,
)
```

On timeout, `WaitTimeoutError` (an `AssertionError` subclass) reports and exposes `.last_value`, `.elapsed`, `.attempts`, `.scene_sequence`, and `.last_exception`. A result mapping containing `scene_sequence` is detected automatically; client wait helpers also report the most recent native scene sequence. `predicate=` separates success from truthiness, which is useful when waiting for zero or another false-valued observation. `KeyboardInterrupt` and cancellation exceptions are never retried unless a caller explicitly and inadvisably lists their types.

## Race-free application synchronization

Enable the Lua side in `game.project` before using application-defined events, state, commands, acknowledgements, or annotations:

```ini
[automation_bridge]
application_api = 1
```

Resolve an event cursor before sending the action that may emit the event:

```python
with bridge.events(from_cursor="now") as events:
    receipt = bridge.drag(start, target, wait="released")
    completed = events.wait(
        "my_game.operation_complete",
        where={"operation_id": "op-42"},
        timeout=20,
    )
```

`EventStream` preserves unmatched events for later waits. The native ring is bounded; `EventBufferOverflow` reports the requested, oldest retained, and latest cursors instead of silently skipping data. Interrupting a wait leaves no persistent socket or background thread to clean up.

Published state carries revisions:

```python
before = bridge.state("my_game.ui")
bridge.command("my_game.begin_operation", {"id": "op-42"})
ready = bridge.wait_for_state(
    "my_game.ui.busy",
    False,
    after_revision=before.revision,
    timeout=20,
)
```

Without `after_revision`, a currently matching value is returned immediately. Pass `state_name=` when the state name cannot be inferred as the longest already-published prefix of the requested path.

Commands are explicit registered functions, never arbitrary Lua source:

```python
accepted = bridge.start_command("my_game.load_fixture", {"name": "standard"})
status = bridge.wait_for_command(accepted["command_id"])

# Equivalent submit-and-wait form; raises if the terminal state is not completed.
status = bridge.command("my_game.load_fixture", {"name": "standard"}, timeout=30)
print(status["result"])
```

Command JSON and results are limited to 32 KiB and 16 nested levels. A client timeout attempts to cancel pending work and raises `CommandTimeout`. Running Lua callbacks cannot be preempted; the exception records a cancellation error in that case.

Application acknowledgement is deliberately separate from native release:

```python
with bridge.events("now") as events:
    receipt = bridge.click(button, wait="released")
    acknowledgement = bridge.wait_for_ack(receipt.input_id, events=events)
```

The application must explicitly call `automation_bridge.ack(input_id, result)`. See the native documentation for the current input-id correlation limitation.

Timeline markers carry both native and host recording-clock timestamps:

```python
bridge.mark("workflow_started")
bridge.mark("result_visible", {"operation_id": "op-42"})
```

By default `mark()` supplies Python's monotonic clock in microseconds as the recording clock. A recorder with its own timebase can pass `recording_timestamp_us=` explicitly.

## Engine Control

The helper combines Automation Bridge window control with a small subset of Defold's built-in engine service protobuf messages on the same engine service port:

```python
bridge.resize(1280, 720)
bridge.set_portrait()
bridge.set_landscape()

bridge.reboot(
    "--config=bootstrap.main_collection=/main/main.collectionc",
    "build/default/game.projectc",
)
```

`bridge.resize(width, height)` uses a JSON `PUT /screen`, polls for at most
`wait` seconds, and returns requested dimensions, observed window/client/
viewport/backbuffer rectangles, `window_matches`, `backbuffer_matches`, and an
outcome: `already_correct`, `resized`, or
`requested_not_observed_before_timeout`. A correct backbuffer with a different
client window is therefore not reported as a successful window resize.
`bridge.screen()` and `bridge.health()` also update `last_window_size`.

`bridge.reboot(*args)` posts `com.dynamo.system.proto.System$Reboot` to `/post/@system/reboot`. Defold accepts up to six string arguments; pass the same command-line style arguments you would pass to `sys.reboot(...)` or the editor reboot helper. For editor-launched projects, a bare `bridge.reboot()` may restart without the project or this extension, so pass explicit project/bootstrap args when you expect Automation Bridge to return. By default the wrapper waits for the Automation Bridge endpoint to become ready again; pass `wait=False` when rebooting into a target that will not load this extension.

`bridge.close_engine()` posts Defold's built-in `@system/exit` message to the same engine service port used by the Automation Bridge API. If the process stays alive on macOS/Linux, the helper falls back to terminating the local process listening on that port. The graceful request is equivalent to:

```sh
printf '\010\000' | curl -sS -X POST --data-binary @- \
  "http://127.0.0.1:<ENGINE_PORT>/post/@system/exit"
```

## Diagnostics

Selector errors include the selector and compact candidate node details. You can also format or dump scene state explicitly:

```python
print(bridge.format_nodes(type="gui_node", limit=100))
print(bridge.dump_scene(visible=True, include=["bounds"]))
bridge.dump_scene("scene.json", visible=True)
```

Read future engine logs from Defold's TCP log service:

```python
print(bridge.engine_info()["log_port"])
print(bridge.engine_log_port())

with bridge.log_stream(read_timeout=1.0) as logs:
    print(logs.readline(timeout=1.0))

recent = bridge.read_logs(duration=2.0, limit=20)
```

`bridge.log_stream()` discovers the log port from engine `/info`, performs Defold's `0 OK` log-service handshake, and then yields plain log lines without trailing newlines. The stream only receives logs emitted after it connects; use `EditorClient.console_lines()` when you need the editor's existing console history.

`EngineLogStream` is context-managed and iterable. It also exposes `readline(timeout=...)` and `close()`.

## Profiler

Read Defold's built-in resource profiler data from `/resources_data`:

```python
resources = bridge.profiler.resources()
for resource in resources:
    print(resource.name, resource.type, resource.size, resource.size_on_disc, resource.ref_count)
```

`resources()` returns `ResourceProfileEntry` objects. `size` is the in-memory size reported by Defold, falling back to size on disc when the engine does not report a separate memory size.
The returned list is sorted by `size` from largest to smallest.

`parse_resources_data(payload)` is exported for tests and fixtures that already have a raw `/resources_data` binary payload.

Capture one Remotery sample frame from Defold's runtime profiler websocket:

```python
with bridge.profiler.remotery() as remotery:
    frame = remotery.get_frame(timeout=2.0)
    for entry in frame.aggregate(include_root=False)[:10]:
        print(entry.label, entry.total_ms, entry.self_ms, entry.call_count)
```

When the bridge was created with `from_project()` or `from_editor()`, `remotery()` uses the websocket URL discovered from editor logs. Direct `AutomationBridgeClient(port)` instances fall back to `ws://127.0.0.1:17815/rmt`, matching Defold's default profiler viewer. Use `start()` and `stop()` directly when a script needs to keep the connection open across multiple frame captures.

Override the Remotery target when needed:

```python
from automation_bridge import RemoteryClient

with bridge.profiler.remotery(port=17816) as remotery:
    print(remotery.aggregate_frame(thread="Main")[:5])

with bridge.profiler.remotery(url="ws://127.0.0.1:17816/rmt") as remotery:
    print(remotery.sample_names)

with RemoteryClient.from_url("ws://127.0.0.1:17816/rmt") as remotery:
    print(remotery.get_frame(timeout=2.0).duration_ms)
```

Capture a window of frames for performance analysis:

```python
capture = bridge.profiler.capture(frames=300, warmup_frames=60, timeout=10.0, thread="Main")

for scope in capture.scopes(sort="self_p95_ms", include_root=False)[:20]:
    print(scope.path, scope.self.avg_ms, scope.self.median_ms, scope.self.p95_ms, scope.calls_avg)

update = capture.scope("Frame/Update")
print(update.total.avg_ms, update.total.median_ms, update.total.p99_ms)
```

`capture.scopes()` groups samples by thread and full scope path. It can filter by `name`, exact or wildcard `path`, `thread`, substring `contains`, or regular expression, and exposes total/self timing statistics: count, total, min, max, average, median, p90, p95, and p99. Sort keys include `total_ms`, `self_ms`, `total_avg_ms`, `self_avg_ms`, `total_median_ms`, `self_median_ms`, `total_p90_ms`, `self_p90_ms`, `total_p95_ms`, `self_p95_ms`, `total_p99_ms`, `self_p99_ms`, `max_ms`, `self_max_ms`, `calls`, `calls_avg`, `occurrences`, and `frames`.

When the script does not know the frame count in advance, start a background recording, drive gameplay, then stop it when the scenario reaches the point you care about:

```python
def recording_finished(capture):
    print("captured", len(capture.frames), "frames")

def recording_aborted(cause, partial_capture):
    print("recording interrupted:", cause, "salvaged", len(partial_capture.frames), "frames")

recording = bridge.profiler.start_recording(
    thread="Main",
    on_finalize=recording_finished,
    on_abort=recording_aborted,
)
try:
    start = bridge.node(name_exact="start", visible_and_enabled=True)
    bridge.click(start)
    bridge.drag(item, target)
    bridge.wait_for_count(1, name_exact="result", timeout=5.0)
finally:
    capture = recording.stop()

for scope in capture.scopes(contains="update", sort="self_p95_ms")[:10]:
    print(scope.path, scope.self.avg_ms, scope.self.p95_ms)
```

`start_recording()` reads Remotery on a background thread until `recording.stop()` returns a `RemoteryCapture`. `recording.abort(cause)` closes the stream and returns a partial capture without surfacing a background-reader error. As a context manager, normal exit finalizes while exceptional exit aborts; hook or socket-cleanup failures cannot replace the exception already leaving the block. Use `recording.snapshot()` to inspect frames collected so far without stopping, and `recording.frame_count`, `recording.property_frame_count`, and `recording.running` for progress checks. Do not call `get_frame()`, `get_properties()`, or `capture()` on the same `RemoteryClient` while a recording is running.

`FinalizationHooks` exposes the same `finalize(artifact)` / `abort(cause, artifact, suppress=...)` callback contract for trace and diagnostic-bundle implementations. Trace cleanup should pass `suppress=True` only when preserving an original interruption; explicit finalization failures should remain visible.

Custom scopes and counters must be emitted by the running Defold app before Python can read them. In Lua, wrap same-frame work with `profiler.scope_begin("my_scope")` and `profiler.scope_end()`. In C++ code that includes `<dmsdk/dlib/profile.h>`, use `DM_PROFILE("MyCppScope")` for scopes and `DM_PROPERTY_GROUP(MyMetrics, "...", 0)`, `DM_PROPERTY_U32(MyCounter, 0, PROFILE_PROPERTY_NONE, "...", MyMetrics)`, then `DM_PROPERTY_SET_U32(MyCounter, value)` or `DM_PROPERTY_ADD_U32(MyCounter, delta)` for counters exposed through `capture.counters(...)`.

Remotery property snapshots are exposed as counters:

```python
capture = bridge.profiler.capture(frames=120, include_properties=True)

for counter in capture.counters(path="Memory/*", sort="max"):
    print(counter.path, counter.type, counter.last_value, counter.values.avg, counter.values.p95)

with bridge.profiler.remotery() as remotery:
    properties = remotery.get_properties(timeout=2.0)
    for entry in properties.find("Sprite", include_groups=False):
        print(entry.path, entry.value)
```

`capture.counters()` can filter by `name`, exact or wildcard `path`, substring `contains`, or regex. Counter values expose count, total, min, max, average, median, p90, p95, and p99. Sort keys include `avg`, `median`, `p90`, `p95`, `p99`, `min`, `max`, `last`, and `frames`. Remotery group properties are skipped by default; pass `include_groups=True` to include them. Use `capture.counter("Memory/Texture")` when exactly one counter should match. For a single raw `PSNP` snapshot, `properties.entries()` returns all entries and `properties.find("Sprite")` returns matching entries with `path`, `name`, `type`, and `value` convenience fields.

## Errors

All custom errors inherit from `AutomationBridgeError`.

- `HttpError`: transport failures or invalid JSON.
- `AutomationBridgeApiError`: extension returned `{ "ok": false }`.
- `InputExecutionError`: accepted input ended as native `cancelled` or `failed`; `.receipt` has the reason and lifecycle metadata.
- `SelectorError`: `node()` or `maybe_node()` found the wrong number of nodes.
- `ProfilerDataError`: a Defold profiler endpoint returned malformed or unexpected binary data.
- `RemoteryError`: base class for Remotery websocket connection, timeout, and protocol errors.
- `RemoteryProtocolError`: malformed or unexpected Remotery websocket data.
- `RemoteryTimeoutError`: Remotery did not produce requested data before timeout.
- `WaitTimeoutError`: a polling condition timed out; structured observation fields are available on the exception.
