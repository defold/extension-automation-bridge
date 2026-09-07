# Automation Bridge Python Helpers

Dependency-free Python helpers for driving a Defold debug build through the
Automation Bridge HTTP API.

When updating an existing script from wrapper 2.x, follow the Python-only
[migration guide](MIGRATION.md).

Coding agents should start with the syntax-checked, copyable patterns in
[`best_practices.py`](best_practices.py), then use this README and public
docstrings for details.

## Install or update Automation Bridge

To copy or refresh the Python wrapper, run `install.py /absolute/path/to/project`
from this directory after the target project's Fetch Libraries completes. It
locates the archive for the configured dependency and atomically replaces the
wrapper without launching Defold or rewriting the dependency. From an existing
wrapper, the equivalent API is `editor.update_python_wrapper(project_path)`.
Restart Python after replacing a wrapper. No project code should be stored in
the managed directory.

Use `editor.doctor(project_path, required_capabilities=("elements",))` to inspect
project configuration, copied/loaded Python versions, editor installations,
connection errors, native API compatibility, and required capabilities. It never
launches or builds, acquires input, starts a log collector, or writes caches.
Its `timeout` bounds each network probe; total time depends on how many candidate
ports are inspected. Missing capabilities are failures; an unavailable launcher
registry is a warning when an editor is already running.

Once this helper directory is present, use the editor client to install or
update the matching extension dependency and refresh the complete copied Python
wrapper:

```python
from automation_bridge import editor

project = editor.open_project(".")
result = project.update_automation_bridge()
print(result.dependency_url)
print(result.wrapper_path)
```

With no version argument, the method resolves GitHub's latest stable release.
Pass an exact version to pin the project instead:

```python
project.update_automation_bridge("2.1.0")
```

Run this as a standalone maintenance step, then restart Python before importing
the updated wrapper for automation. The method:

- detects whether the Automation Bridge dependency is already present;
- adds it to `[project]` or updates its release URL without rewriting unrelated
  `game.project` settings;
- invokes Defold's Fetch Libraries command;
- validates the fetched extension archive; and
- atomically replaces the project-root `automation-bridge-python` directory.

The wrapper directory is managed as one unit. Do not store project scripts or
local customizations inside it. A failed fetch, invalid archive, or failed
replacement restores the previous dependency and wrapper.

## Quick start

After copying this directory into a Defold project root, add the copied
directory to `PYTHONPATH`:

```sh
PYTHONPATH=automation-bridge-python python3 your_script.py
```

When working in the extension source checkout instead, use
`PYTHONPATH=automation_bridge/automation-bridge-python`.

Build, connect, query the scene, and send input:

```python
from automation_bridge import editor

project = editor.open_project(".")
game = project.build_and_run()

spawner = game.element(type="goc", name_exact="/spawner", visible=True)
game.click(spawner)
label = game.wait_for_element(type="labelc", text_exact="L1")
game.drag(game.parent(label), (500, 300), duration=0.16)
shot = game.screenshot(wait=True, resolution_multiplier=0.5)
print(shot.path, shot.frame, shot.scene_sequence, shot.sha256)
```

Use `game.close_engine()` only when the script intentionally owns engine cleanup.

## Public API

Use `engine.ElementSelector` as a typed dictionary of supported query keywords.
Its docstring specifies substring versus exact matching, boolean filters, and
pagination limits. Selectors reject malformed supplied values before a request.

`game.elements()` still returns a list. Use `game.elements_page()` when the next
cursor or complete match count matters:

```python
from automation_bridge import engine

selector: engine.ElementSelector = {"type": "goc", "visible": True, "limit": 20}
page = game.elements_page(**selector)
print(page.count, page.matched, page.scene_sequence, page.engine_frame)
if page.next_cursor is not None:
    next_page = game.elements_page(**selector, cursor=page.next_cursor)
```

Pagination requires `scene.pagination`. Each page is a live snapshot; a cursor
does not freeze the scene. `element()` and `maybe_element()` reject ambiguous
selectors even when `limit=1` would hide additional matches.

The package root exposes only `editor` and `engine`.

- Bootstrap: `editor.open_project(...)`, `project.build_and_run()`,
  `project.clean_build_and_run()`, `project.connect_engine()`, and
  `engine.connect(port)`.
- Editor operations: `project.commands`, `project.debugger`, `project.console`,
  `project.preferences`, `project.reference`, `project.preview`, and
  `project.build_and_run_html5()`.
- Project maintenance: `project.update_automation_bridge()` for the latest stable
  release, or `project.update_automation_bridge(version)` to pin a release.
- Runtime state: `health()`, `screen()`, `scene(...)`, capabilities, and lifecycle.
- Elements: `elements(...)`, `element(...)`, `maybe_element(...)`,
  `element_by_id(...)`, `parent(...)`, `count(...)`, compact formatting, and
  scene dumps.
- Input: `click(...)`, `drag(...)`, `drag_path(...)`, `pointer(...)`,
  `type_text(...)`, `key(...)`, and `game.input`.
- Synchronization: events, states, application commands, full input
  acknowledgements, timeline markers, frame/count waits, and element
  observation.
- Runtime control and geometry: `resize(...)`, `set_portrait()`,
  `set_landscape()`, `convert_point(...)`, `reboot(...)`, and
  `close_engine()`.
- Capture and diagnostics: screenshots, historical `game.logs`, live engine
  logs, profiling, visual comparison, tracing, `game.video_recording`, and
  `game.metal_capture` on supported macOS/Metal runtimes.
- Raw engine escape hatch: `game.request(method, path, params=..., json_body=...)`.

## Bootstrap

Editor connection and build helpers accept optional `client_id` and `session_id`.
Reuse both only for the same logical automation session; independent agents must
use distinct identities. Builds return `game.owns_engine == True`; attachment
through the editor or a known port returns `False`. `game.session_info()` exposes
this information without another network request.

`game.close()` (also called by the engine client's context manager) releases the
background log collector and leaves Defold running. It is idempotent; reconnect
before making further requests. Explicit streams/captures keep their own context
managers. Flush this session's input before closing if cancellation is intended.
Engine termination remains the separate, explicit `game.close_engine()` operation.

```python
from automation_bridge import editor, engine

project = editor.open_project(".")
game = project.build_and_run()
clean_game = project.clean_build_and_run()
existing_game = project.connect_engine()
direct_game = engine.connect(51337)
```

Choose the operation by ownership and lifecycle intent:

| Operation | Editor required | Builds | Engine intent |
| --- | --- | --- | --- |
| `editor.open_project(".")` | No; starts or reuses it | No | Return an editor project client |
| `project.build_and_run()` | Yes | Incremental | Replace the previous engine and connect |
| `project.clean_build_and_run()` | Yes | Clean | Replace the previous engine and connect |
| `project.connect_engine()` | Yes | No | Connect to the healthy engine already registered by this project |
| `engine.connect(port)` | No | No | Connect directly to a known engine service port |
| `game.close_engine()` | No | No | Close the connected engine; use only when the script owns cleanup |

There is intentionally no `project.connect()`: a project is already the editor
client, while `connect_engine()` makes the second client and its lifecycle
explicit.

The editor bootstrap discovers `.internal/editor.port`, launches Defold when
needed, rejects stale engine ports, and waits for Automation Bridge health.
Pass `start_if_needed=False` to require an already-running editor.

Editor discovery retries for up to `timeout` seconds (30 by default), rereading
the port file between attempts. HTTP requests use the remaining discovery time,
so a slow response can still reuse the editor. With automatic startup enabled,
a missing/invalid port file or refused connection gets a five-second grace
period (bounded by `timeout`) before launch; a newly launched editor then has its
own `timeout` budget. Timeouts, permission failures, and HTTP/JSON errors raise
`NotRunningError` with the underlying failure if discovery cannot recover.
These failures do not trigger another editor launch.

Use `editor.is_running(".", timeout=1)` for a single Boolean probe. A `False`
result means the endpoint did not respond successfully; `open_project()` handles
the retries and startup decision.

## Reliable game test loop

Resize before coordinate-based input or visual assertions, then synchronize on
application-published state instead of estimating completion from elapsed time:

If a `.go`, `.gui`, or `.collection` resource was created or edited manually,
render it through the editor preview before building. This makes the editor
parse its references and produces a quick visual artifact, so malformed files,
missing resources, and obvious layout mistakes are reported close to the edit:

```python
from pathlib import Path
from automation_bridge import editor

project = editor.open_project(".")
preview_directory = Path("/tmp/defold-previews")
preview_directory.mkdir(parents=True, exist_ok=True)

for resource in (
    "main/player.go",
    "main/hud.gui",
    "main/main.collection",
):
    png = project.preview.render(resource, resolution_multiplier=0.5)
    output = preview_directory / f"{Path(resource).name}.png"
    output.write_bytes(png)
    print(f"Rendered {resource} to {output}")
```

Preview rendering validates editor loading and appearance; it does not replace
`build_and_run()` when runtime scripts, input, physics, or dynamically spawned
content must be tested.

After previewing manually authored resources, continue with the runtime loop:

```python
from automation_bridge import editor

project = editor.open_project(".")
game = project.build_and_run(required_capabilities=[
    "screen.resize",
    "input.pointer",
    "screenshot",
    "application.state",
])

game.resize(1080, 1920)
game.set_portrait()
game.wait_for_state("tutorial.phase", "ready")

before = game.screenshot(after_frames=1)
game.drag((540, 1500), (540, 500), duration=0.25)
game.wait_for_state("tutorial.phase", "waiting_for_pickup")
after = game.screenshot(after_frames=1)
```

The state names and values in this example belong to the game. Publish them
from Lua together with semantic scene annotations:

```lua
automation_bridge.publish("tutorial", {
    phase = "waiting_for_pickup",
    figure = figure_num,
})

automation_bridge.annotate("main:/ghost#sprite", {
    automation_id = "tutorial_target_ghost",
    role = "tutorial_target",
})
```

Tests can then use
`game.element(automation_id="tutorial_target_ghost")` instead of inferring a
runtime object from size, scale, depth, or changing instance ids.

## Sandboxed editor launch

An already-running healthy editor can be reused from a sandboxed agent without
escalation. Probe without starting a new editor when the environment is
uncertain:

```python
from automation_bridge import editor

project = editor.open_project(".", start_if_needed=False)
```

If this raises `NotRunningError`, inspect the included discovery failure first.
A timeout or denied connection can mean the editor is already running but could
not be reached. If the editor needs to be started, the launch procedure differs
by platform:

- **macOS:** Rerun the normal bootstrap with escalated/unsandboxed execution.
  Defold inherits the Python parent's sandbox and otherwise cannot register with
  WindowServer or LaunchServices. The wrapper detects the Codex sandbox marker
  and raises `LaunchError` instead of starting a known-broken editor process.
  Neither `start_new_session=True` nor `/usr/bin/open` escapes an inherited
  sandbox.
- **Windows:** Start Defold outside the agent's restricted process tree, for
  example manually from the Start menu or Explorer, then rerun the probe above.
  A descendant editor may abort during boot when the sandbox blocks the JDK's
  internal loopback socket. This happens before `.internal/editor.port` is
  written and may appear to the wrapper as a startup timeout. The editor log in
  `%LOCALAPPDATA%\Defold\editor2.<date>.log` identifies this case with the
  message `Unable to establish loopback connection`. Switching agent shells,
  marking only a descendant command unsandboxed, or using detached child-process
  flags does not reliably move the editor outside the restricted process tree.

Installation discovery and preview rendering are explicit:

```python
for installation in editor.installations():
    print(installation.launcher_path, installation.last_launched_at)

preview_png = project.preview.render(
    "main/main.collection",
    resolution_multiplier=0.5,
)
```

Built-in preferences are discoverable constants with metadata, while custom
editor-script paths remain strings:

```python
size = project.preferences.get(project.preferences.CODE_FONT_SIZE)
project.preferences.set(project.preferences.CODE_FONT_SIZE, 16)
project.preferences.set("my-extension/preview/quality", "high")
for preference in project.preferences.list(prefix="code"):
    print(preference.path, preference.description)
```

## Capabilities

Editor command discovery supports Defold 1.13.1's command enum and 1.13.2's
individual OpenAPI paths. Use `project.commands.catalog()` for descriptions and
parameter schemas, and `project.commands.supports("run", parameter="focus")`
to probe support. Pass `refresh=True` to `catalog()` after capabilities change.
New-feature `editor.UnsupportedOperationError` messages identify Defold 1.13.2
as the minimum version; their `minimum_version` attribute is available to hosts.

`project.last_command_result` retains typed `editor.BuildResult` diagnostics after
build/run, HTML5, hot reload, and debugger operations. It includes warnings,
zero-based source ranges, completion status, and an optional `target_url`.
`editor.BuildError.result` retains the same evidence on failure. Existing helpers
keep their return values. On 1.13.1, HTML5, hot reload, and debugger acknowledgements
have `completed=False` and `success=None`; 1.13.2 reports their build completion.

Use `project.compile()` on Defold 1.13.2 to validate resources and Lua without
launching or bundling. It returns a `BuildResult`; compilation failures raise
`BuildError`. When runtime testing is needed, call `project.build_and_run()`
directly: it compiles and launches through `run` on 1.13.2 or `build` on 1.13.1.
The default avoids taking focus where supported and preserves the legacy launch
on 1.13.1. Explicit `focus=False` requires advertised focus control (1.13.2);
`focus=True` works on either version. Unsupported requests fail before engine
cleanup. `clean_build_and_run()` still uses the native focused launch on both.

Defold 1.13.2 also supports Bob builds and bundles without launching:

```python
result = project.bob(
    options={"platform": "wasm-web", "archive": True},
    commands=("build", "bundle"),
)
```

Bob option keys omit `--`; arrays supply repeatable options. Use
`project.bob(options={"help": True})` and `project.console.read()` for Bob help
and output. The wrapper reads `.internal/editor.token` for each call and sends
it as a bearer token. Missing or rejected credentials raise `CommandError`.
Bob requests are never automatically retried after an uncertain transport failure.

When a 1.13.2 build result includes a target URL, bootstrap connects to that
target and validates native health, capabilities, and identity before caching it.
The current engine transport supports `http://127.0.0.1:PORT` and
`http://localhost:PORT`. Other targets raise `UnsupportedOperationError`; select
a local engine in Defold. A reported target never falls back to historical ports
or triggers an automatic rebuild. Results without a URL continue to use console
registration and existing recovery behavior, including on Defold 1.13.1.

Declare mandatory capabilities during bootstrap or later with `require()`:

```python
game = project.build_and_run(
    required_capabilities=["runtime.lifecycle", "application.events>=1"],
)

game.require("scene", "input.drag")
```

Check nonessential functionality without mutating the client:

```python
available = {
    name: game.supports(name)
    for name in ("scene", "screenshot", "input.drag")
}
```

Capability declarations may use `name>=N`. Incompatible API versions raise
`IncompatibleApiVersionError`; missing required features raise
`UnsupportedCapabilityError`.

## Raw requests

Named helpers are preferred. For endpoint-level debugging, use the single raw
escape hatch. `params` are URL query fields; use `json_body` for `POST` and
`PUT` payloads so Defold's request-resource limit does not constrain their
size. The former `json` spelling remains accepted as a compatibility alias:

```python
health = game.request("GET", "/health")
result = game.request("POST", "/coordinates/convert", json_body={
    "point": {"x": 0.5, "y": 0.5},
    "from_space": "normalized_viewport",
    "to_space": "window",
})
```

Raw native endpoint documentation is in
[`automation_bridge/README.md`](https://github.com/defold/extension-automation-bridge/blob/master/automation_bridge/README.md)
in the extension source repository. It is not included in the copied Python
helper directory.

## Elements and selectors

`elements()` returns snapshots. Re-query after input, collection changes, or UI
updates.

```python
labels = game.elements(type="labelc", text="L", limit=100)
restart = game.element(name_exact="restart", enabled=True)
maybe_popup = game.maybe_element(automation_id="popup")
parent = game.parent(labels[0])
total = game.count(type="labelc")
```

Substring filters are `type`, `name`, `text`, and `url`. Exact filters include
`type_exact`, `name_exact`, `text_exact`, `url_exact`, `path`, `kind`,
`instance_id`, `logical_id`, `automation_id`, `localization_key`, and `role`.
Boolean filters include `visible`, `enabled`, `has_bounds`, and
`visible_and_enabled`.

`Element` exposes `id`, `snapshot_id`, `instance_id`, `instance_generation`,
`logical_id`, `created_scene_sequence`, `scene_sequence`, `engine_frame`,
`name`, `type`, `kind`, `path`, `parent_id`, `text`, `url`, semantic metadata,
visibility, bounds, children, and `raw`.

When an `Element` is passed directly to `click()`, or `Element` objects are passed
as both endpoints of `drag()`, the wrapper also sends their logical runtime
identities. If a path-derived element id has since been reused by another
instance, input fails with `engine.StaleElementError`; re-query the selector and
retry deliberately.

## Input

Input targets can be elements, element ids, point mappings, `(x, y)` pairs, or raw
coordinates:

```python
game.click(element)
game.click(480, 320)
game.drag(first, second, duration=0.2, easing="ease_in_out")
game.type_text("Hello")
game.key("SPACE")
game.key("SPACE", hold=1.5, wait="released", timeout=3)
```

`key()` accepts case-insensitive letters, digits, and every named key in Defold's
`dmHID::Key` enum, with or without the `KEY_` prefix. This includes function,
punctuation, keypad, lock, modifier, navigation, and system keys such as
`EQUALS`, `KP_0`, and `CAPS_LOCK`. Unknown names are rejected before input is
queued. `hold` keeps the key pressed for `0..60` seconds; held input requires a
native endpoint advertising `input.key>=2`. When waiting for release, set
`timeout` above the requested hold. `type_text()` always treats braces and other
characters as literal UTF-8 text.

`click()`, `drag()`, and `drag_path()` wait for native release by default.
`type_text()` and `key()` return after the request is accepted unless a `wait`
state is supplied. These five helpers accept `wait="accepted"`,
`wait="started"`, `wait="released"`, or `wait=False` as appropriate.

Low-level queue and interruption control lives under `game.input`:

```python
with game.input.interruption_scope():
    receipt = game.click(element, wait=False)
    game.input.wait(receipt, "released")
```

For a continuous held pointer:

```python
with game.pointer((100, 100)) as pointer:
    pointer.move((200, 150), duration=0.2)
    pointer.hold(0.1)
```

The pointer object exposes its configured `lease`, `input_id`, and `closed`
state. `game.input.status(pointer.input_id)` returns its current native receipt.
Pointer and input-interruption contexts preserve the original cancellation
exception when cleanup fails. Inspect `OperationCancelled.cleanup_error` for
the first cleanup failure. A pointer whose cancellation was refused remains
open, so its native receipt can be inspected before retrying `pointer.cancel()`.

To capture a rendered pressed state, request a frame-relative screenshot while
the pointer context is still open and choose a lease that comfortably covers
the capture:

```python
with game.pointer((100, 100), lease=10) as pointer:
    pressed = game.screenshot(after_frames=1)
    assert game.input.status(pointer.input_id).state == "started"
```

## Synchronization and observation

Discover game-specific operations before calling them:

```python
page = game.application_catalog(kind="command")
for entry in page.entries:
    print(entry.name, entry.description, entry.input_schema, entry.output_schema)
if page.next_cursor is not None:
    following = game.application_catalog(kind="command", cursor=page.next_cursor)
    assert (following.engine_instance_id, following.revision) == (page.engine_instance_id, page.revision)
```

`engine.ApplicationCatalogPage` retains counts, cursor, revision and engine
identity. `engine.ApplicationEntry` exposes descriptions and schemas; use
`kind="state"` or `kind="event"` to discover value/data contracts via `.schema`.
Games declare metadata with Lua `automation_bridge.describe()`; see
`examples/application_sync.script` in the extension source. Old runtimes without
`application.catalog` fail with `UnsupportedCapabilityError` before querying the
endpoint. Schemas document application expectations; they do not validate payloads.

Use a shared cancellation token when a host or another thread may stop an
operation:

```python
token = engine.CancellationToken()
# The controlling thread calls token.cancel("request cancelled").
try:
    with game.cancellation_scope(token):
        game.key("SPACE", hold=5, wait=False)
        game.wait_for_state("sample.game.ready", True)
except engine.OperationCancelled as exc:
    print(exc.reason, exc.cleanup_error)
```

Create one token per operation and enter the scope in the thread doing the work.
Polling delays wake when cancelled; in-flight HTTP/profiler requests remain
bounded by their transport timeouts. `game.cancellation_scope()` requests release
of that client's queued input on cancellation. `engine.cancellation_scope()` also
works without a client, including editor bootstrap. It cancels waits, with input
receipt and Metal capture waits using their existing native cleanup paths.
Pending commands receive a cancellation request; native code decides whether it
can be honored. Running Lua callbacks cannot be preempted. Cleanup failures are
retained in `OperationCancelled.cleanup_error`. Cancellation does not undo
completed operations or terminate an engine. Use context managers for streams
and recordings so they close when a scope is interrupted.

Use application events and published state instead of sleeps:

```python
with game.events("now") as events:
    game.click(button)
    completed = events.wait("operation.complete", timeout=5)

revision = game.state("ui").revision
game.click(button)
state = game.wait_for_state("ui.busy", False, after_revision=revision)
```

`wait_frames(count)` is useful when rendered-frame evidence is specifically
required. Its default timeout is `max(5, count / 30)` seconds; an explicit
`timeout` overrides that calculation. Timeout errors include the initial and
last observed frame, whether frames advanced, the current lifecycle stage, and
a best-effort engine health result. Frame waits do not replace semantic state or
event waits.

Run application commands:

```python
result = game.command("reset_fixture", {"seed": 42})
```

Add a timeline marker. `recording_timestamp_us` is optional; when omitted, the
wrapper records the host monotonic clock in microseconds:

```python
marker = game.mark("workflow_started", {"fixture": "menu"})
```

Native input completion and application acknowledgement are distinct. An
application can call `automation_bridge.acknowledge_input(input_id, result)`
from Lua; Python waits for that semantic result explicitly:

```python
with game.events("now") as events:
    receipt = game.click(button)
    acknowledgement = game.wait_for_input_acknowledgement(
        receipt.input_id,
        events=events,
    )
```

Wait for scene evidence:

```python
element = game.wait_for_element(
    automation_id="result",
    after_scene_sequence=previous_sequence,
)
stable = game.observe_element(logical_id=element.logical_id, minimum_frames=3)
gone = game.wait_for_disappearance(element.id)
```

## Screenshots and visual checks

`screenshot(wait=True)` returns an atomic `ScreenshotReceipt` containing the
capture path, engine frame, scene sequence, dimensions, and SHA-256. Runtime
screenshots also accept `resolution_multiplier` from `0.01` through `1.0`:

```python
shot = game.screenshot(resolution_multiplier=0.5)
print(shot.path, shot.width, shot.height, shot.sha256)
```

Screenshot pixels and pointer input both use top-left window orientation.
Defold's graphics adapters already normalize readback rows to display order, so
the native capture preserves their order when publishing the PNG and pixel
coordinates in the receipt correspond to input coordinates. Defold world
coordinates are a different space: a generic `height - world_y` conversion is
not reliable with cameras, projections, viewports, scaling, or letterboxing.
`game.convert_point(...)` converts among the advertised top-left window,
client, display-pixel, backbuffer, viewport, and normalized-viewport spaces;
world transforms must be published by the application when needed.

Large black regions are not automatically classified as capture corruption
because they may be legitimate game content. When a capture looks suspicious,
retain its receipt and PNG, capture an adjacent frame with
`screenshot(after_frames=1)`, inspect live scene state, and compare the two with
`game.visual.difference(...)`. The receipt's frame, scene sequence, dimensions,
and SHA-256 make the evidence reproducible without silently replacing it.

The engine capture remains available at `shot.raw["source_path"]`; the returned
receipt points to a bilinear-downscaled PNG with updated dimensions and hash.
Downscaling requires `wait=True`. Agents should normally start with `0.5` and
use full resolution only when fine detail matters.

## Runtime logs

Public project and engine connection helpers start a bounded background
collector against Defold's engine log service as soon as the Automation Bridge
client is available. Recent output and errors can then be queried without
depending on the editor console:

```python
recent = game.logs.tail(100)
errors = game.logs.tail(100, contains="ERROR:")
for line in errors:
    print(line)
```

The filter is a case-sensitive substring match. Filtering happens before the
limit is applied, so the second example returns up to 100 matching errors, not
only errors among the last 100 unfiltered lines. The collector retains the most
recent 10,000 lines and is stopped by `game.close_engine()`.

Collection begins at the earliest successful bridge connection. Messages
emitted before the wrapper discovers the engine service port cannot be recovered
from Defold's forward-only log service; use `project.console` only when those
earliest build or startup messages are required.

`game.log_stream()` opens Defold's live TCP log stream. Start it before an
operation when every subsequent line matters; `game.read_logs()` is also
forward-only:

```python
with game.log_stream(read_timeout=0.1) as logs:
    game.click(button)
    game.wait_for_state("tutorial.phase", "waiting_for_pickup")
    while line := logs.readline(timeout=0.1):
        print(line)
```

`project.console` remains available when the full editor snapshot or structured
console regions are needed.

Pixel comparisons are explicitly namespaced:

```python
before = game.screenshot()
game.click(button)
changed = game.visual.wait_for_region_change(before, region=(0, 0, 300, 200))

stable = game.visual.wait_for_stable_frame(consecutive_frames=3)
error = game.visual.difference(before, stable.screenshot)
```

## Generated gestures

Gesture synthesis has one public entry path:

```python
gesture = game.gestures.generate_drag(
    (100, 100),
    (500, 300),
    seed=42,
    duration=(0.7, 0.9),
    control_points=4,
)
game.drag_path(**gesture)
```

## Metal capture, recording, and traces

On macOS with Defold's Metal adapter, capture complete rendered frames to an
Apple `.gputrace` document:

```python
capture = game.metal_capture.start("captures/frame.gputrace", frames=1)
print(capture.path, capture.frames_captured)
```

The engine must be launched with `METAL_CAPTURE_ENABLED=1`, and health must
advertise `metal.capture`. `start()` creates the parent directory, schedules the
capture, and waits for `complete` by default; pass `wait=False`, inspect
`status()`, or call `stop()` for explicit lifecycle control. Analyze the result
with Xcode or, when installed, `gpudebug`.

Video recording is native and namespaced. It records the running Defold
process's largest on-screen window to H.264 MP4 using ScreenCaptureKit on
macOS 15+ or Windows Graphics Capture and Media Foundation on Windows 10
version 1903+:

```python
capabilities = game.video_recording.capabilities()

with game.video_recording.start("capture.mp4", size=(960, 540), fps=30):
    game.click(button)
```

The recorder runs in the game process, automatically selects that process's
window, crops it to the undecorated game content area, and finalizes the file
when the context exits. Omitted `audio` uses the platform default: enabled on
macOS and disabled on Windows. The current Windows backend is video-only and
rejects `audio=True`. The first macOS capture may trigger Screen Recording
permission. Check `capabilities().available` before treating recording as an
optional feature. No FFmpeg installation or external recorder process is used.

Diagnostic traces remain a client-bound context because they intercept client
operations:

```python
with game.trace("session.trace.json", screenshots="on_error") as trace:
    game.click(button)
    trace.record("checkpoint", {"name": "after click"})
```

Replay is best effort and cannot restore application state, random seeds,
timing mode, or external services.

## Profiling

All profiling is presented through `game.profiler`; the underlying stream
protocol is an implementation detail.

```python
resources = game.profiler.resources()

capture = game.profiler.capture(frames=120, warmup_frames=10)
for scope in capture.scopes(contains="Update"):
    print(scope.path, scope.self.p95_ms)

for counter in capture.counters(contains="Texture"):
    print(counter.path, counter.values.last)
```

Record until the automation script decides the interesting interval is over:

```python
recording = game.profiler.start_recording(warmup_frames=10)
try:
    game.click(button)
finally:
    capture = recording.stop()
```

Advanced profiler-facing type names live in `automation_bridge.profiler`, such
as `ProfilerCapture`, `ProfilerRecording`, `ProfilerConnection`,
`ProfilerScopeStats`, and `ProfilerCounterStats`.

## Tests

The test modules are part of the extension source repository and are not
included in the copied Python helper directory. Extension contributors can run
them from the extension repository root with:

```sh
PYTHONPATH=automation_bridge/automation-bridge-python \
python3 -m unittest tests.test_automation_bridge_api tests.test_tooling
```

CI explicitly runs the Python/tooling unit classes on Linux, macOS and Windows
with Python 3.10 and 3.14. The complete suite also exercises a running Defold
sample project. Set `AUTOMATION_BRIDGE_REQUIRE_RUNTIME=1` for release checks so a
missing editor/engine causes a failure instead of skipping runtime coverage.
See the source repository's `DEVELOPMENT.md` for unit-only and runtime commands.
