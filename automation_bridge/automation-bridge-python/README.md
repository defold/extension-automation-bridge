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

png = bridge.screenshot(wait=True)

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

## Public API Surface

`AutomationBridgeClient` is the main entry point.

- Bootstrap: `AutomationBridgeClient(port)`, `from_project(...)`, `from_editor(...)`, and `wait_ready(...)`.
- Raw API passthrough: `get(path, params=None)`, `post(path, params=None)`, and `put(path, params=None)`.
- Runtime state: `health()`, `screen()`, `scene(...)`, `remotery_url`, and `last_window_size`.
- Node queries: `nodes(...)`, `node(...)`, `maybe_node(...)`, `by_id(...)`, `parent(...)`, and `count(...)`.
- Input: `click(...)`, `drag(...)`, `type_text(...)`, and `key(...)`.
- Waits and screenshots: client methods `wait_for_node(...)`, `wait_for_count(...)`, and `screenshot(...)`, plus the module-level `wait_until(...)`.
- Engine diagnostics: `engine_info()`, `engine_log_port()`, `log_stream(...)`, `read_logs(...)`, `format_nodes(...)`, and `dump_scene(...)`.
- Engine control: `resize(...)`, `set_portrait(...)`, `set_landscape(...)`, `reboot(...)`, and `close_engine(...)`.
- Profiling: `bridge.profiler.resources()`, `bridge.profiler.remotery(...)`, `bridge.profiler.capture(...)`, and `bridge.profiler.start_recording(...)`.

`EditorClient` exposes editor-oriented helpers for advanced bootstrap flows: `from_project(...)`, `build(...)`, `console_lines()`, `engine_service_port()`, `engine_service_ports()`, `latest_registration_engine_service_ports()`, `latest_registration_has_engine_service_port()`, `last_build_had_engine_service_port()`, `endpoint_registered_count()`, `remotery_url()`, `remotery_urls()`, `latest_registration_remotery_urls()`, `remember_engine_service_port(...)`, and `remember_remotery_url(...)`.

Typed response wrappers include `Node`, `Bounds`, `ResourceProfileEntry`, `RemoteryFrame`, `RemoterySample`, `RemoterySampleAggregate`, `RemoteryCapture`, `RemoteryScopeStats`, `RemoteryCounterStats`, `RemoteryProperty`, `RemoteryPropertyFrame`, `RemoteryPropertyEntry`, `RemoteryTimingStats`, `RemoteryValueStats`, and `RemoteryRecording`. `EngineLogStream`, `ProfilerClient`, and `RemoteryClient` are also exported for direct use. These are lightweight snapshots around native engine data; re-query after clicks, drags, or scene changes.

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

Raw `get()`, `post()`, and `put()` are available for curl-level debugging or endpoints that do not have named wrapper methods yet.

## Selectors

`nodes()` returns a list of snapshot `Node` objects. `node()` expects exactly one match. `maybe_node()` allows zero or one.

```python
restart = bridge.node(name_exact="restart", enabled=True)
labels = bridge.nodes(type="labelc", text="L", limit=100)
count = bridge.count(type="labelc", text_exact="L2")
detail = bridge.by_id(restart.id)
item = bridge.parent(labels[0])
```

Server-side filters are passed to the extension: `id`, `type`, `name`, `text`, `url`, `visible`, `limit`, and `include`.

Python adds stricter client-side filters: `enabled`, `kind`, `path`, `name_exact`, `text_exact`, `has_bounds`, and `visible_and_enabled`.

`include` may be either a comma-separated string or an iterable:

```python
node = bridge.node(name="spawner", include=["bounds", "properties"])
```

Native `type`, `name`, `text`, and `url` filters are case-insensitive substring matches. Use `name_exact` and `text_exact` when exact matching matters.

`Node` exposes convenience properties for the native node payload:

```python
node.id
node.name
node.type
node.kind
node.path
node.parent_id
node.text
node.url
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
bridge.click(node)
bridge.click(node.id)
bridge.click(480, 320)
bridge.click((480, 320))
bridge.click({"x": 480, "y": 320})
bridge.click({"center": {"x": 480, "y": 320}})
bridge.click(node, visualize=False)

bridge.drag(first_node, second_node, duration=0.16)
bridge.drag(first_node.center, second_node.center, duration=0.16)
bridge.drag(first_node, second_node, duration=0.16, visualize=False)

bridge.type_text("hello")
bridge.key("KEY_ENTER")
```

Mouse/touch input visualization is on by default in the native endpoint: clicks draw a growing circle and drags draw a line for one second. Pass `visualize=False` to `click()` or `drag()` to disable it for that input. Drag calls block until the requested `duration` has completed and the engine has had a short extra moment to process the release. Pass `wait=0` to only queue the input, or pass a larger wait if the game needs additional settle time after input.

`Node` objects are snapshots. If the scene may have changed, fetch a fresh node with `bridge.by_id(node.id)` or query again.

Component nodes often expose useful text or properties, while their parent game object is the actionable target. Use `bridge.parent(component_node)` before clicking or dragging when needed:

```python
label = bridge.node(type="labelc", text_exact="L1")
item = bridge.parent(label)
bridge.drag(item, (480, 320))
```

## Waits and Screenshots

```python
from automation_bridge import wait_until

wait_until(lambda: bridge.count(type="labelc", text_exact="L2") >= 1)

node = bridge.wait_for_node(name_exact="restart", enabled=True)
bridge.wait_for_count(2, type="labelc", text_exact="L1")

png = bridge.screenshot(wait=True, timeout=5)
```

`wait_until(fn, timeout=..., interval=..., message=...)` polls until `fn()` returns a truthy value. Exceptions raised while polling are retried until timeout, then reported in the final `AssertionError`.

## Engine Control

The helper can post a subset of Defold's built-in engine service protobuf messages to the same engine service port used by the Automation Bridge API:

```python
bridge.resize(1280, 720)
bridge.set_portrait()
bridge.set_landscape()

bridge.reboot(
    "--config=bootstrap.main_collection=/main/main.collectionc",
    "build/default/game.projectc",
)
```

`bridge.resize(width, height)` calls Automation Bridge's `PUT /screen` endpoint, which sets the native Defold window size, then remembers the returned `(width, height)` as `bridge.last_window_size`. `bridge.screen()` and `bridge.health()` also update the remembered size from the engine response. `bridge.set_portrait()` and `bridge.set_landscape()` use that remembered size, fetching `/screen` first if needed, and swap width/height only when the current orientation does not already match.

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
recording = bridge.profiler.start_recording(thread="Main")
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

`start_recording()` reads Remotery on a background thread until `recording.stop()` returns a `RemoteryCapture`. Use `recording.snapshot()` to inspect frames collected so far without stopping, and `recording.frame_count`, `recording.property_frame_count`, and `recording.running` for progress checks. Do not call `get_frame()`, `get_properties()`, or `capture()` on the same `RemoteryClient` while a recording is running.

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
- `SelectorError`: `node()` or `maybe_node()` found the wrong number of nodes.
- `ProfilerDataError`: a Defold profiler endpoint returned malformed or unexpected binary data.
- `RemoteryError`: base class for Remotery websocket connection, timeout, and protocol errors.
- `RemoteryProtocolError`: malformed or unexpected Remotery websocket data.
- `RemoteryTimeoutError`: Remotery did not produce requested data before timeout.
