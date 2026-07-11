# Automation Bridge Improvement Backlog

This backlog is based on practical use of Automation Bridge to build, run,
inspect, automate, time, and record Defold applications. The workloads included
long gesture-driven sessions, transient UI, asynchronous state changes,
localization, window resizing, application-audio capture, and repeated editor
build/attach cycles.

## Assessment

The original recommendations are directionally correct. The highest-value
areas are continuous gestures, race-free application synchronization, stale
scene protection, and better input lifecycle diagnostics.

Some capabilities need clearer ownership:

- The native bridge can report that HID input was queued and injected. It
  cannot generically know which Lua script consumed the action or why
  application logic rejected it. That requires an opt-in application
  acknowledgement.
- The scene graph exposes rendered text, not localization keys or arbitrary
  application semantics. Stable automation ids and localization metadata must be
  supplied by the application.
- A generic bridge cannot reliably detect the end of every application-defined
  animation. It can provide frame/stability primitives; exact semantic waits
  should use application events or state.
- Application-audio recording is platform-specific and should be an optional
  Python companion capability, not a mandatory native runtime feature.

The core bridge should remain useful without application changes. Structured
events, semantic metadata, state, commands, and acknowledgements should be
optional capabilities layered on top of the zero-configuration inspection and
input API. No domain-specific workflow should be built into the library.

## Implementation status on `improvements`

All 31 recommendations have been implemented to the boundary of Defold's
current public APIs. The Python API, native HTTP/Lua contracts, capability
rules, examples, failure modes, and optional dependencies are documented in
[`automation_bridge/README.md`](automation_bridge/README.md) and
[`automation_bridge/automation-bridge-python/README.md`](automation_bridge/automation-bridge-python/README.md).

| Backlog items | Delivered result |
| --- | --- |
| 1-6 | FIFO input ownership, ids and native receipts, continuous paths, leased pointer sessions, cancellation/flush/status, and explicit device selection |
| 7-10 | Bounded cursor-based events plus opt-in Lua state, commands, acknowledgements, and semantic annotations |
| 11-13 | Scene/frame correlation, stale-snapshot guards, named coordinate conversion, and bounded JSON request bodies |
| 14-18 | Snapshot and generational identity, server-side semantic/exact selectors, pagination, transient observation, and diagnostic selector failures |
| 19-21 | Atomic screenshot receipts, native frame waits, dependency-free pixel stability/change helpers, and scene/state-first assertion layering |
| 22-23 | Optional platform recording companion with explicit capability failures and native timeline markers independent of recording |
| 24-27 | Interruption-safe cleanup, deterministic gesture generation, trace bundles with best-effort replay, and observation-rich wait errors |
| 28-31 | Precise resize outcomes, engine identity/lifecycle data, versioned capability negotiation, stale-engine rejection, and reduced-backend capability handling |

Where Defold does not expose enough stable public API to implement a truthful,
portable contract, the bridge reports the reduced capability instead of
guessing or relying on private engine layout. The missing engine contracts and
their proposed implementations are specified in:

- [`docs/defold-hid-device-capability-query.md`](docs/defold-hid-device-capability-query.md)
- [`docs/input-action-correlation.md`](docs/input-action-correlation.md)
- [`docs/DEFOLD_PUBLIC_AUTOMATION_GEOMETRY.md`](docs/DEFOLD_PUBLIC_AUTOMATION_GEOMETRY.md)
- [`docs/defold-headless-lifecycle-api.md`](docs/defold-headless-lifecycle-api.md)
- [`docs/recording-platform-contract.md`](docs/recording-platform-contract.md)

## Findings in the current implementation

These details materially affect priority and API design:

1. Input requests are accepted into an array, but `UpdateInput()` advances all
   queued events during the same engine update. Two quickly queued gestures can
   overlap and overwrite the same mouse state instead of running FIFO.
2. `drag()` waits by sleeping for `duration + 0.1` in Python. It does not receive
   a native completion acknowledgement.
3. Native drag interpolation is linear and starts at full velocity. It has no
   easing, initial press hold, waypoint, or curved-path support.
4. `/health` already exposes `scene_sequence`, but `/scene`, `/nodes`, `/node`,
   and input requests do not consistently return or enforce it.
5. Node ids are hashes of traversal paths that contain sibling indices. They
   are useful within one scene shape, but insertion/removal can change ids or
   make an old id refer to a different logical node.
6. Python-only exact and semantic selectors fetch at most 500 server matches
   before filtering. Large scenes can therefore produce incomplete results.
7. Screenshot completion is inferred by polling for a non-empty file. There is
   no capture id, completion endpoint, render-frame receipt, or atomic
   completion signal.
8. The log stream temporarily changes socket timeouts around each read. Cleanup
   and timeout restoration need to remain safe under `KeyboardInterrupt`,
   socket closure, and nested exceptions.

## Terminology

Use precise lifecycle terms in APIs and documentation:

- **accepted**: the HTTP request was valid and entered the native queue;
- **started**: the first press/down event was injected;
- **released**: the final up/release event was injected;
- **acknowledged**: the application explicitly reported the semantic result;
- **stable**: a defined visual region remained within a pixel-difference
  threshold for a defined number of rendered frames.

Avoid calling input “processed” or “consumed” unless the response says which
of these stages it means.

## P0: Input correctness and deterministic completion

### 1. Make the input queue FIFO and assign input ids

Sequential input should be the default. Only one mouse gesture should own the
mouse button and position at a time. Multi-touch or deliberate parallel input
should require separate pointer ids and an explicit API.

Every accepted action should receive an id:

```json
{
  "input_id": 42,
  "kind": "drag",
  "state": "accepted",
  "queue_position": 1
}
```

This is a correctness prerequisite for paths, cancellation, status queries,
and reliable completion waits.

Requests should also carry a client/session id. If multiple clients connect,
the bridge should define controller ownership explicitly: one client may hold
an input lease while other clients remain read-only observers. Do not let two
uncoordinated clients silently drive the same HID state.

### 2. Add native completion receipts

Replace Python wall-clock guessing with a native status/completion mechanism:

```python
result = game.drag(start, target, duration=0.4, wait="released")
print(result.input_id, result.start_frame, result.release_frame)
```

The result should include:

- input id and final state;
- accepted, start, and release frame numbers;
- monotonic native timestamps;
- requested and actual duration;
- cancellation or failure reason;
- client/session and request correlation ids;
- engine instance id and scene sequence used to resolve node targets.

`wait="released"` means native release injection, not application acceptance.
Application acceptance belongs to the acknowledgement API below.

### 3. Add one continuous multi-point gesture

The server must own the whole gesture so the pointer is pressed once, follows
all segments, and is released once:

```python
game.drag_path(
    [start, probe_left, probe_right, target],
    durations=[0.15, 0.22, 0.18],
    easing=["ease_in", "ease_in_out", "ease_out"],
    hold_before=0.08,
    hold_after=0.04,
    visualize=False,
    wait="released",
)
```

Required features:

- per-segment duration and easing;
- optional press and release holds;
- linear, quadratic/cubic Bezier, or sampled waypoint paths;
- exactly one down and one up event;
- validation of point count, total duration, and payload size;
- visualization of the actual sampled path rather than a start/end line.

This directly supports curved movement, sliders, drawing, and realistic
pointer timing without a series of independent drags.

### 4. Add low-level pointer sessions with leases

Expose an advanced API on top of the same native gesture state machine:

```python
with game.pointer(start, lease=2.0) as pointer:
    pointer.move(left, duration=0.2, easing="ease_in_out")
    pointer.hold(0.1)
    pointer.move(target, duration=0.3, easing="ease_out")
```

The session needs `up()`, `cancel()`, and a lease timeout that always releases
the pointer. A stateless HTTP server cannot detect that a Python process died
after an earlier request, so automatic cleanup must use either a lease or a
persistent connection with disconnect handling.

### 5. Expose queue inspection, cancellation, and flush semantics

```python
game.input.pending()
game.input.status(input_id)
game.input.cancel(input_id, release=True)
game.input.flush(release=True)
```

Cancellation must define what happens to active mouse buttons, touch contacts,
partially typed keys, visualization, and later queued events.

### 6. Support input device selection

Report and select the injected device explicitly:

```python
game.input.configure(device="mouse", visualize=False)
game.drag(..., device="touch", pointer_id=0)
```

`auto`, `mouse`, and `touch` should be capability-gated. Avoid injecting both
mouse and touch for one gesture unless explicitly requested, because an
application may bind both actions and observe duplicates.

## P0: Race-free synchronization

### 7. Add application-defined structured events

Provide a small, debug-only Lua API:

```lua
automation_bridge.emit("operation_complete", {
    operation_id = "op-42",
    success = true
})
```

Python should consume typed JSON without parsing console text:

```python
event = events.wait(
    "operation_complete",
    where={"operation_id": "op-42"},
    timeout=20,
)
```

Events should carry an event sequence, engine frame, monotonic timestamp,
engine instance id, and scene sequence.

### 8. Make event subscriptions race-free

The server should retain a bounded event ring buffer and support a cursor:

```python
with game.events(from_cursor="now") as events:
    game.drag(start, target, wait="released")
    events.wait("operation_complete")
```

A long-poll endpoint is sufficient; WebSocket support is optional. The API must
report buffer overflow instead of silently skipping old events.

### 9. Add opt-in published state and commands

Applications should be able to publish structured debug state and register
explicit commands:

```lua
automation_bridge.publish("ui", {
    busy = busy,
    workflow_step = workflow_step,
    modal = modal_id
})

automation_bridge.command("load_test_fixture", function(data)
    load_test_fixture(data.name)
end)
```

```python
game.command("load_test_fixture", {"name": "standard"})
game.wait_for_state("ui.busy", False)
```

Requirements:

- debug builds only and opt-in per project;
- namespaced command/state names;
- strict JSON payload and response size limits;
- no arbitrary Lua evaluation;
- command ids, completion results, timeouts, and cancellation semantics;
- state revisions so waits cannot confuse old and new values.

### 10. Separate native delivery from application acknowledgement

The native bridge can truthfully report that down/move/up events were injected.
The application may optionally acknowledge the semantic outcome:

```lua
automation_bridge.acknowledge_input(input_id, {
    accepted = false,
    reason = "input_locked"
})
```

Useful application reasons include:

- input disabled or application busy;
- modal UI intercepted input;
- target not interactive;
- target outside the active region;
- validation or workflow restriction;
- no valid drop target;
- application constraint rejected the action.

Do not promise a generic `consumer="/main#controller"` field unless the Defold
engine exposes reliable dispatch-consumption metadata.

## P0: Snapshot and coordinate safety

### 11. Promote the existing scene sequence to every relevant response

Return `scene_sequence` from `/scene`, `/nodes`, `/node`, screenshots, and input
receipts. Allow stale-target protection:

```python
scene = game.scene()
game.click(element, expected_scene_sequence=scene["scene_sequence"])
```

If the sequence changed before node-id resolution, return a structured
`stale_scene` error instead of potentially clicking a different logical node.

Also return the native engine frame number. Scene sequence identifies snapshot
rebuilds; frame number identifies time.

### 12. Define coordinate spaces and provide conversions

Current screen-pixel coordinates are not enough for letterboxed applications,
high-DPI windows, backbuffer recording, or scripts that know logical content
coordinates.

Expose named rectangles and transforms for spaces the bridge can know:

- window/client coordinates;
- viewport coordinates;
- backbuffer pixels;
- display pixels and display scale;
- normalized viewport coordinates.

```python
point = game.convert_point(
    (540, 960),
    from_space="backbuffer",
    to_space="window",
)
```

World and GUI-layout coordinates depend on application cameras, projections,
and layout rules. Offer those only when the engine exposes the required
transform or the application publishes one; do not silently guess.

### 13. Add JSON request bodies for structured operations

Query parameters are convenient for curl-level v1 endpoints but are a poor fit
for waypoint arrays, structured commands, selectors, and event filters. New
endpoints should accept `application/json`, enforce content length and nesting
limits, and retain simple query forms where useful.

## P1: Scene identity and selectors

### 14. Distinguish snapshot ids from logical identities

Document current node ids as snapshot/path identities. Where Defold exposes a
stable instance handle, return it separately. Useful fields include:

- `snapshot_id` or current `id`;
- `instance_id` when available;
- `created_scene_sequence`;
- current `scene_sequence`;
- application-provided `automation_id`.

Dynamic instances with the same component name need a creation generation so
tests can distinguish a new modal/status node from a stale snapshot of an old
one.

### 15. Add application-supplied semantic metadata

Localization-aware selectors are useful, but localization keys are not
generically recoverable from rendered labels. Provide an annotation API and
query stable semantics instead:

```lua
automation_bridge.annotate(node_url, {
    automation_id = "operation_status",
    localization_key = "status_ready",
    role = "status"
})
```

```python
game.element(automation_id="operation_status")
```

Rendered text and current locale can still be exposed when the application
publishes them.

### 16. Move exact filters server-side and add pagination

Add native exact/case-sensitive filters for name, text, path, kind, enabled,
bounds availability, and semantic metadata. Add `cursor`/`offset` pagination
and return whether results were truncated.

This removes the Python wrapper’s current 500-candidate ceiling for client-side
filters and makes `count()` correct for large scenes.

### 17. Add transient-node observation primitives

```python
game.wait_for_element(..., after_scene_sequence=sequence)
game.wait_for_disappearance(element_id=old.id)
game.observe_element(..., minimum_frames=3)
```

Define whether identity is snapshot id, instance id, or semantic automation
id. Return first/last observed frames rather than only a final sampled node.

### 18. Improve selector errors

When a selector returns zero or multiple nodes, include:

- total server matches and whether candidate results were truncated;
- nearest name/text/path values;
- matching nodes excluded for visibility, enabled state, or missing bounds;
- scene sequence and engine frame;
- active top-level collections and modal layers where available;
- suggested exact or semantic selectors.

## P1: Screenshot and visual verification

### 19. Return screenshot completion receipts

Treat screenshot capture as an asynchronous operation with an id:

```python
shot = game.screenshot(after_frames=2, wait=True)
print(shot.path, shot.frame, shot.width, shot.height, shot.sha256)
```

The native side should write to a temporary file and atomically rename on
completion, or provide a status/data endpoint. Return capture frame, scene
sequence, viewport, dimensions, format, and failure reason. The wrapper should
not infer completion from `exists() && size > 0` alone.

### 20. Add frame and stability waits

```python
game.wait_frames(2)
game.visual.wait_for_stable_frame(
    region=content_region,
    consecutive_frames=3,
    tolerance=0.01,
)
game.visual.wait_for_region_change(before, region=content_region)
```

Specify the pixel metric, alpha handling, scaling, consecutive-frame count,
and timeout. Image-diff helpers can be an optional Python extra if the core
wrapper must remain dependency-free.

Exact animation completion and semantic visibility should prefer application
events/state. Visual stability is the generic fallback.

### 21. Keep visual assertions layered

Core assertions should use scene/state data:

```python
game.element(automation_id="operation_status", visible=True)
game.wait_for_state("ui.workflow_step", "complete")
```

Pixel/template/OCR assertions belong in an optional visual package so the base
client can remain dependency-free.

## P2: Recording and capture tooling

### 22. Provide native macOS recording

The desired API is still valuable:

```python
recording = game.video_recording.start(
    "session.mp4",
    size=(1080, 1920),
    fps=30,
    audio=True,
)
```

The macOS implementation lives in the native extension and uses
ScreenCaptureKit to select the current Defold process window and record H.264
MP4 with optional application audio. Python is only a thin controller; it does
not launch FFmpeg or another recorder process. Windows uses Windows Graphics
Capture plus Media Foundation for video-only H.264 MP4; application audio
remains capability-gated until its native implementation is added.

It should provide:

- automatic current-process window selection and title-bar exclusion;
- optional application audio rather than microphone audio;
- explicit output size and frame rate;
- native H.264 MP4 output;
- explicit permission/start failures;
- reliable stop/finalization under exceptions;
- duration, dimensions, codec, and native timestamp metadata.

Backbuffer-only recording could be a separate native capability, but it
does not solve application-audio capture by itself.

### 23. Add timeline markers independent of the recorder

```python
game.mark("workflow_started")
game.mark("result_visible")
game.mark("workflow_complete")
```

Markers should enter the trace/event timeline with native and recording-clock
timestamps. A recorder may export them as a JSON sidecar, but marker collection
should remain useful without recording.

## P1: Diagnostics, interruption, and reproducibility

### 24. Make interruption safe

`KeyboardInterrupt` and cancellation should:

- preserve the original exception;
- close log/event streams;
- restore socket configuration without masking the interrupt;
- release active pointer/touch/key state;
- optionally flush later queued inputs;
- finalize or explicitly abort active recordings and traces.

Add tests that interrupt during a log read, active drag, held pointer, event
wait, screenshot wait, and recorder finalization.

### 25. Add deterministic generated gesture profiles in Python

Build optional path generators on top of the native path/easing API:

```python
gesture = game.gestures.generate_drag(
    start,
    target,
    seed=42,
    duration=(0.7, 0.8),
    easing="ease_in_out",
    lateral_offset=(-35, 35),
    control_points=2,
)
game.drag_path(**gesture)
```

The helper should return the generated points/timings for traceability. Bounds,
maximum velocity, and maximum acceleration should be constrained so seeded
variation cannot create discontinuous or physically implausible motion.

### 26. Add trace and diagnostic bundles

```python
with game.trace("session.trace.json", screenshots="on_error"):
    run_workflow()
```

Capture:

- engine instance id, API/capabilities, screen geometry, and initial scene;
- actions, generated paths, input receipts, and application acknowledgements;
- events/state revisions and scene sequences;
- monotonic timestamps and engine frames;
- optional profiler frame/counter correlation;
- selector errors and optional screenshots;
- interruption/cancellation cleanup.

Replay is deterministic only when application state, random seeds, timing mode,
and external services are controlled. The document should call it “best-effort
replay” unless those prerequisites are recorded and restored.

### 27. Improve wait diagnostics

Polling helpers should report the last observed value, elapsed time, attempt
count, scene sequence, and last retryable exception. Let callers choose which
exception types are retryable so programming errors are not hidden until a
timeout.

## P2: Window, engine, and API lifecycle

### 28. Return precise resize and rectangle results

`resize(width, height)` should distinguish:

- resized successfully;
- already correct;
- platform window cannot resize;
- backbuffer correct but outer/client window differs;
- resize requested but not observed before timeout.

Return window, client/content, viewport, backbuffer, display rectangles, and
display scale. These values feed coordinate conversion and recording crops.

### 29. Add engine instance identity and lifecycle events

Health should include an engine instance id, process id where available, start
timestamp, and build/project identity. The wrapper can then reject a cached
port that belongs to a previous engine instance.

Expose or derive explicit lifecycle stages:

- editor build started;
- build completed or failed;
- previous engine closing/closed;
- new engine registered;
- bridge healthy;
- initial scene ready.

This reduces stale-port and attach ambiguity without relying only on parsing
console history.

### 30. Strengthen API version and capability negotiation

The native API already returns version `1` and a capability list. Add wrapper
supported-version ranges, per-feature capability versions, and a clear startup
error when a required capability is unavailable.

Prefer capability checks over inferring behavior from a global version alone.
Trace files should record both native and Python package versions.

### 31. Support capability-driven headless and CI operation

Inspection, application events/state/commands, tracing, and lifecycle control
can remain useful when graphics, screenshots, native windows, or HID injection
are unavailable. Headless builds should expose the subset they support rather
than disabling the entire game.

The Python wrapper should accept required and optional capabilities:

```python
game.require("application.events", "application.commands")
{name: game.supports(name) for name in ("scene", "screenshot", "input.drag")}
```

CI diagnostics should clearly distinguish unsupported capabilities from
runtime failures. Platform/backend test coverage should include graphical and
headless configurations where Defold supports them.

## Recommended implementation order

### Slice 1: Correct deterministic input

1. FIFO input execution, input ids, status, cancellation, and release receipts.
2. `drag_path()` with easing and press/release holds.
3. Python waits on native release instead of sleeping.
4. Scene-sequence guards and engine/frame metadata on receipts.
5. Safe interruption tests.

### Slice 2: Application synchronization

1. Structured event ring buffer with race-free cursors.
2. Debug-only Lua events, state, commands, and acknowledgements.
3. Semantic automation metadata.

### Slice 3: Scene and visual reliability

1. Server-side exact selectors and pagination.
2. Stable/semantic identities and transient observation.
3. Screenshot completion receipts and frame/stability waits.
4. Coordinate-space transforms and precise window/content rectangles.

### Slice 4: Tooling

1. Trace bundles and best-effort replay.
2. Seeded gesture generation.
3. Optional platform recording and timeline-marker export.

## Acceptance criteria for the first slice

- Two queued drags execute sequentially and each emits exactly one down and one
  up event.
- A path gesture remains pressed across all waypoints and supports start/end
  holds and easing without discontinuities.
- Python can wait for native release without sleeping for a guessed settle
  duration.
- Cancelling or interrupting any active gesture leaves no mouse button, touch,
  or key held.
- Every input receipt records engine instance, engine frame, monotonic time, and
  scene sequence.
- A stale node target fails with `stale_scene` rather than being resolved
  against a newer snapshot.
- Native and Python unit/integration tests cover queue order, cancellation,
  interruption, stale scenes, duration reporting, and capability negotiation.
