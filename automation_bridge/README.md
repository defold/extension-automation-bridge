# Automation Bridge Native API

The native extension registers a curl-friendly HTTP API on Defold's engine service web server:

```text
http://127.0.0.1:<engine_service_port>/automation-bridge/v1
```

Find `<engine_service_port>` in the editor console line:

```text
INFO:ENGINE: Engine service started on port <port>
```

No Lua setup is required for scene inspection, input, screenshots, native
desktop video recording, the event cursor transport, or timeline markers.
Application-defined events, state, commands, annotations, and acknowledgements
use a debug-only Lua API that must be enabled explicitly:

```ini
[automation_bridge]
application_api = 1
event_capacity = 256
```

`event_capacity` is clamped to `16..4096` and defaults to `256`. Release builds expose neither the HTTP bridge nor the `automation_bridge` Lua module.

Query parameters remain available for curl-friendly requests. `POST` and `PUT`
also accept a root `application/json` object. JSON bodies are limited to 65,536
bytes and 16 nesting levels. Structured endpoints consume documented nested
fields (for example `point.x`/`point.y`); unsupported nested fields are validated
and ignored by flat v1 operations. Query values win if the same key is present
in both forms.

```sh
ENGINE_PORT=51337
BASE="http://127.0.0.1:${ENGINE_PORT}/automation-bridge/v1"

curl -fsS "$BASE/health" | python3 -m json.tool
```

## Response Shape

Success responses use this envelope:

```json
{ "ok": true, "data": {} }
```

Error responses use this envelope and an appropriate HTTP status:

```json
{ "ok": false, "error": { "code": "not_found", "message": "..." } }
```

Common error codes include `bad_request`, `invalid_json`, `json_body_too_large`,
`unsupported_media_type`, `not_found`, `method_not_allowed`, `body_not_supported`,
`stale_scene`, `input_queue_full`, `input_too_large`, `input_controller_busy`,
`input_device_unsupported`, `input_not_found`, `input_not_owned`, `pointer_closed`,
`screen_resize_unsupported`, `screenshot_unsupported`, `screenshot_not_found`,
`screenshot_pending`, `recording_unsupported`, `recording_active`,
`recording_inactive`, `recording_start_failed`, `recording_stop_failed`,
`metal_capture_unsupported`, `metal_capture_active`, `metal_capture_inactive`, and
`unsupported_capability`. The latter names a feature
omitted by the current platform, graphics, game-object, window, or HID backend.

## Coordinates

Input coordinates use top-left `window` pixels. All advertised spaces use a
top-left origin:

```json
{
  "coordinates": {
    "origin": "top-left",
    "units": "pixels",
    "input_space": "window",
    "spaces": ["window", "client", "backbuffer", "viewport", "normalized_viewport"]
  }
}
```

Game object bounds are projected from configured `display.width` and `display.height` space to the current window size. GUI bounds are reported in screen pixels.

`GET /screen`, `PUT /screen`, and `GET /health` return:

```json
{
  "window": { "x": 0, "y": 0, "width": 1280, "height": 720 },
  "client": { "x": 0, "y": 0, "width": 1280, "height": 720 },
  "backbuffer": { "x": 0, "y": 0, "width": 1280, "height": 720 },
  "display": { "width": 960, "height": 640, "scale": 2 },
  "project_content": { "x": 0, "y": 0, "width": 960, "height": 640 },
  "display_pixels": { "x": 0, "y": 0, "width": 1280, "height": 720, "scope": "window_drawable" },
  "viewport": { "x": 0, "y": 0, "width": 1280, "height": 720 },
  "coordinates": { "origin": "top-left", "units": "screen_pixels" }
}
```

`display` is retained for compatibility and describes configured project
content, not global monitor bounds. `display_pixels` is limited to the drawable
window because public Defold APIs do not expose monitor or decorated outer-window
rectangles.

## Nodes

Node responses always include the basic fields:

```json
{
  "id": "n:0123456789abcdef",
  "snapshot_id": "n:0123456789abcdef",
  "scene_sequence": 42,
  "engine_frame": 810,
  "instance_id": "/player",
  "instance_generation": 7,
  "logical_id": "instance:0123456789abcdef:g7",
  "created_scene_sequence": 12,
  "name": "play",
  "type": "gui_node_box",
  "kind": "gui_node",
  "path": "/main[0]/hud[0]/play[0]",
  "parent": "n:0011223344556677",
  "visible": true,
  "enabled": true
}
```

Optional node fields include `text`, `url`, `resource`, `automation_id`, `localization_key`, `role`, `bounds`, `properties`, and `children`.

`bounds` has screen, center, and normalized center coordinates:

```json
{
  "bounds": {
    "screen": { "x": 440, "y": 300, "w": 160, "h": 48 },
    "center": { "x": 520, "y": 324 },
    "normalized": { "x": 0.40625, "y": 0.45 }
  }
}
```

Use `include` to choose optional blocks:

```text
include=bounds
include=properties
include=children
include=all
include=bounds,properties,children
```

Basic fields are always returned. Unknown include tokens are ignored. `bounds` are included by default when `include` is omitted. `/node` also includes `properties` by default. `/scene` always returns the recursive tree under `root`.

Node `type` examples include `collectionc`, `goc`, component resource types such as `spritec` or `labelc`, and GUI node types such as `gui_node_box` or `gui_node_text`. `kind` is a broader category: `collection`, `game_object`, `component`, `subcomponent`, `gui_node`, or `node`.

`id`/`snapshot_id` hash the traversal path and are stable only for the current
scene shape. Nodes backed by a public Defold `HInstance` also expose the engine
identifier, allocation generation, a composed `logical_id`, and the first bridge
scene sequence that observed that generation. Collection-global qualification is
not available in public DMSDK yet; use application automation ids when project
semantics are required. Re-discover snapshot ids after scene changes.

## Endpoint Reference

### `GET /automation-bridge/v1/health`

Checks that the extension is running.

Response `data` includes:

- `version`: API version string.
- `debug`: `true`.
- `platform`: `macos`, `windows`, `linux`, `android`, `ios`, or `unknown`.
- `capabilities`: supported capability names for runtime health/lifecycle,
  diagnostics, scene inspection, screen/coordinate operations, input and input
  devices, events, markers, frame waits, application synchronization, screenshots,
  native video recording, and Metal GPU trace capture. Backend-dependent names are
  omitted when unavailable.
- `capability_versions`: capability-to-version map for feature negotiation.
- `native_version`, `api_version_min`, and `api_version_max`: native package and API compatibility information.
- `identity`: opaque engine instance, process id where portable, wall/monotonic start timestamps, hashed project identity, and hashed build-configuration identity.
- `backend`: graphics, HID, and headless availability.
- `lifecycle`: runtime registration, health, and initial-scene stages.
- `screen`: current screen metadata.
- `scene_sequence`: snapshot sequence number.
- `engine_frame` and `engine_instance_id`: native temporal and process-instance correlation.
- `input`: default device/visualization and supported input devices.

```sh
curl -fsS "$BASE/health" | python3 -m json.tool
```

The raw title, bootstrap path, executable path, and user paths are not returned.
`build_identity` is currently a non-secret configuration fingerprint rather than a
resource-content digest.

### `GET /automation-bridge/v1/lifecycle`

Returns ordered runtime stages with native monotonic timestamps. The
`initial_scene_ready` stage appears once a scene root is available.

```sh
curl -fsS "$BASE/lifecycle" | python3 -m json.tool
```

## Capability-Driven Backends

Health and lifecycle are independent of scene, graphics, screenshots, windows, and
HID. Capabilities are advertised only when their backing context exists. Calling an
omitted route returns `501 unsupported_capability`, so CI can distinguish an unsupported
backend from a transient runtime failure. True `DM_HEADLESS` registration still needs
the public Defold debug transport proposed in the specification linked above.

### `GET /automation-bridge/v1/screen`

Returns named rectangles, display scale, scene sequence, engine frame, and the
coordinate convention.

```sh
curl -fsS "$BASE/screen" | python3 -m json.tool
```

### `PUT /automation-bridge/v1/screen`

Sets the native Defold window size and returns `requested`, `outcome`,
`window_matches`, `backbuffer_matches`, all named rectangles, scene sequence,
and engine frame. Outcomes are `already_correct`, `resized`, or
`requested_not_observed`. The latter uses HTTP 202 and lets a client poll
`GET /screen` to impose its own timeout.

Query parameters:

- `width`, `height`: positive integer window size in screen pixels.

```sh
curl -fsS -X PUT "$BASE/screen?width=1280&height=720" | python3 -m json.tool
curl -fsS -X PUT -H 'Content-Type: application/json' \
  -d '{"width":1280,"height":720}' "$BASE/screen" | python3 -m json.tool
```

### `POST /automation-bridge/v1/coordinates/convert`

Converts `x`/`y` from `from_space` to `to_space`. Supported spaces are `window`,
`client`, `display_pixels`, `backbuffer`, `viewport`, and
`normalized_viewport`. World and GUI-layout transforms are intentionally absent
until the application publishes them or Defold exposes public transforms.

```sh
curl -fsS -X POST -H 'Content-Type: application/json' \
  -d '{"point":{"x":0.5,"y":0.5},"from_space":"normalized_viewport","to_space":"window"}' \
  "$BASE/coordinates/convert" | python3 -m json.tool
```

### `GET /automation-bridge/v1/frame`

Returns the current `engine_frame` and `scene_sequence`. Poll this endpoint for
frame-relative waits; semantic completion should use application state/events.

### `GET /automation-bridge/v1/scene`

Returns the current runtime scene tree.

Query parameters:

- `visible`: optional boolean. Use `1`, `true`, or `yes` for true; any other present value is false.
- `include`: optional comma-separated include list.

Response `data` includes `count`, `visible_filter`, `screen`, `scene_sequence`,
`engine_frame`, and `root`.

```sh
curl -fsS "$BASE/scene?visible=1&include=bounds,properties" | python3 -m json.tool
```

### `GET /automation-bridge/v1/nodes`

Searches nodes with simple filters. This is the main discovery endpoint for automation clients.

Query parameters:

- `id`: exact node id.
- `type`, `name`, `text`, `url`: substring match, case-insensitive by default.
- `type_exact`, `name_exact`, `text_exact`, `url_exact`: case-sensitive exact match.
- `path`, `kind`, `instance_id`, `logical_id`: case-sensitive exact match.
- `automation_id`: exact application-supplied automation id.
- `localization_key`: exact application-supplied localization key.
- `role`: exact application-supplied semantic role.
- `visible`, `enabled`, `has_bounds`, `visible_and_enabled`: optional boolean filters.
- `case_sensitive`: make substring filters case-sensitive.
- `include`: optional comma-separated include list.
- `limit`: number of returned nodes, clamped to `0..500`. Default is `50`. Use `limit=0` to get counts without node payloads.
- `offset` or numeric `cursor`: zero-based result offset.

Response `data` includes:

- `nodes`: returned node list.
- `count`: returned node count after `limit`.
- `matched`: total matching nodes before `limit`.
- `total`: total nodes in the snapshot.
- `truncated`, `next_cursor`, and `offset`: explicit pagination state.
- `scene_sequence`, `engine_frame`, and `active_collections`: selector diagnostics.

```sh
curl -fsS "$BASE/nodes?type=labelc&text=Play&visible=1&limit=20" | python3 -m json.tool
```

### `GET /automation-bridge/v1/node`

Returns one node in detail.

Query parameters:

- `id`: required node id.
- `include`: optional comma-separated include list. Defaults to bounds and properties.

```sh
curl -fsS "$BASE/node?id=n:0123456789abcdef&include=bounds,properties,children" | python3 -m json.tool
```

### Input ownership, FIFO execution, and receipts

All click, drag, path, pointer, and key actions share one FIFO. Only the first action advances during an engine update, so independent gestures cannot overwrite the same HID state. Every mutating request carries `client_id`, `session_id`, and `request_id`; keep ids compact on query endpoints because Defold bounds the complete request resource. The first client/session acquires the controller lease and other clients receive `input_controller_busy` until that lease expires. Observer endpoints remain readable without the lease.

Use `PUT /input/configure?client_id=...&session_id=...&lease=5&device=auto&visualize=1` to acquire or renew control and set defaults. Devices are exclusive per gesture: `auto`, `mouse`, or `touch`. `GET /health` reports `input.device.mouse` and, on platforms where native touch injection is supported, `input.device.touch`. The public Defold HID API has no reliable connected-touch-device predicate, so explicit touch is conservatively enabled on iOS, Android, and Switch and rejected elsewhere; one gesture never injects both mouse and touch.

When resolving a node id, pass `expected_scene_sequence` to reject a changed
snapshot with HTTP 409 `stale_scene`. Input receipts include the scene sequence
and engine frame used for resolution.

```sh
curl -fsS -X POST "$BASE/input/click?id=n:0123456789abcdef"
curl -fsS -X POST "$BASE/input/click?x=480&y=320&visualize=0"
```

Accepted input returns HTTP 202 with this lifecycle receipt in `data`:

```json
{
  "input_id": 42,
  "kind": "drag",
  "state": "accepted",
  "queue_position": 1,
  "accepted_frame": 120,
  "start_frame": null,
  "release_frame": null,
  "accepted_time_us": 123456789,
  "start_time_us": null,
  "release_time_us": null,
  "requested_duration": 0.35,
  "actual_duration": 0,
  "reason": null,
  "client_id": "runner-1",
  "session_id": "test-7",
  "request_id": "request-19",
  "engine_instance_id": "engine-123456",
  "scene_sequence": 8,
  "engine_frame": 120,
  "device": "mouse",
  "pointer_id": 0
}
```

Lifecycle terms are exact: `accepted` means queued, `started` means the first down/key event was injected, and `released` means the final up was injected. `cancelled` and `failed` are terminal and include `reason`. They do not claim that application code consumed or accepted the action.

Use `GET /input/status?input_id=42` for current/bounded-history status and `GET /input/pending` for the FIFO. `POST /input/cancel?input_id=42&release=1&client_id=...&session_id=...` cancels one action. `POST /input/flush?release=1&client_id=...&session_id=...` cancels the owning session's active and later actions. `release=1` releases mouse/key state or emits a cancelled touch contact. A pointer or controller lease expiry performs the same safe cleanup.

Node-targeted input accepts `expected_scene_sequence`. A mismatch returns HTTP 409 with `stale_scene` before resolving a snapshot/path node id. Receipts retain the scene sequence used for resolution plus engine instance/frame metadata.

### `POST /automation-bridge/v1/input/click`

Use `id`, or finite `x`/`y`. Optional common fields are `device`, `pointer_id`, `visualize`, `expected_scene_sequence`, and the ownership/correlation fields above.

```sh
curl -fsS -X POST "$BASE/input/click?id=n:0123456789abcdef&client_id=runner&session_id=test&request_id=click-1"
```

### `POST /automation-bridge/v1/input/drag`

Use `from_id`/`to_id`, or `x1`/`y1`/`x2`/`y2`. `duration`, `hold_before`, and `hold_after` are seconds in `0..60`; their total must not exceed 60. `easing` is `linear`, `ease_in`, `ease_out`, or `ease_in_out`.

```sh
curl -fsS -X POST "$BASE/input/drag?x1=100&y1=100&x2=500&y2=300&duration=.35&easing=ease_in_out&client_id=runner&session_id=test"
```

### `POST /automation-bridge/v1/input/drag_path`

Runs exactly one down, a continuous native path, and one up. `points` contains `x,y` pairs separated by semicolons. For `path=sampled` or `linear`, `durations` and `easing` have one comma-separated value per segment. `path=quadratic` requires three control points and one duration/easing; `path=cubic` requires four. Paths contain 2–128 points, the encoded point list is limited to 8192 bytes, and total duration including holds is at most 60 seconds. Visualization records and draws positions actually injected on engine updates, including the sampled curve, rather than drawing only a start/end chord.

```sh
curl -fsS -X POST -H 'Content-Type: application/json' \
  --data '{"points":"100,100;180,60;260,140","durations":".15,.22","easing":"ease_in,ease_out","hold_before":.08,"hold_after":.04,"client_id":"runner","session_id":"test"}' \
  "$BASE/input/drag_path"
```

### Low-level pointer sessions

`POST /input/pointer/open?x=...&y=...&pointer_lease=2...` creates a leased pointer receipt. Append continuous work with `/input/pointer/move?input_id=...&x=...&y=...&duration=...&easing=...` and `/input/pointer/hold?input_id=...&duration=...`; finish with `/input/pointer/up?input_id=...`. Every command includes the same client/session identity and renews `pointer_lease`. If the client disappears, expiry emits a safe release/cancel. `POST /input/cancel` closes a session immediately.

### `POST /automation-bridge/v1/input/key`

Use `text` for UTF-8 or `keys` for a brace-wrapped special key such as URL-encoded `%7BKEY_ENTER%7D`. Values are limited to 4096 bytes. Supported names include arrows, modifiers, navigation keys, `KEY_F1`–`KEY_F12`, `KEY_A`–`KEY_Z`, and `KEY_0`–`KEY_9`. Key presses share the FIFO, report the same receipts, and cancellation releases an active special key.

### `GET /automation-bridge/v1/screenshot`

Schedules an atomic post-render PNG capture. `after_frames` may defer capture by
up to 600 rendered callbacks. The accepted receipt includes a `capture_id` and
must be completed through the status endpoint; file size polling is not a
completion protocol. PNG rows use the same top-left window orientation as input,
scene bounds, and coordinate conversion responses.

```sh
curl -fsS "$BASE/screenshot" | python3 -m json.tool
```

Response `data`:

```json
{
  "capture_id": 7,
  "state": "pending",
  "path": "/tmp/automation-bridge/screenshot-123-7.png",
  "engine_frame": 810,
  "scene_sequence": 42,
  "width": 1280,
  "height": 720,
  "format": "png",
  "sha256": null,
  "failure_reason": null
}
```

### `GET /automation-bridge/v1/screenshot/status`

Takes `capture_id` (or `id`) and returns `pending`, `complete`, or `failed`.
Completed receipts include capture frame/sequence, dimensions, and SHA-256. The
PNG is first written to a temporary path and atomically renamed before `complete`
is visible.

## Native video recording (macOS and Windows)

The extension selects its own largest on-screen window, resolves the matching
`NSWindow` content frame, and records only that undecorated game area with
ScreenCaptureKit's native H.264 MP4 recorder on macOS 15 or newer. The explicit
source rectangle excludes the title bar and rounded window corners. It does not
launch FFmpeg or any other helper process. Screen Recording permission is
granted to the running game process; the first request may cause the system
permission prompt.

On Windows 10 version 1903 or newer, the extension selects the largest visible
top-level window owned by the current Defold process, captures it with Windows
Graphics Capture, and crops frames to its Win32 client area. Windows 11 corner
rounding is disabled for the recording session and restored afterward. Frames
are resized as requested and encoded to H.264 MP4 with Media Foundation. The
Windows backend is currently video-only; `audio=true` returns
`recording_audio_unsupported` instead of recording another audio source.

- `GET /recording/capabilities` reports runtime availability, output format,
  codec, resizing, frame-rate, and application-audio support.
- `GET /recording/status` reports the active or most recently finalized capture.
- `POST /recording/start` accepts `path`, optional paired `width` and `height`,
  integer `fps` from 1 through 60 (default 30), and boolean `audio` (default
  true on macOS and false on Windows). It automatically selects a window owned
  by the current process.
- `POST /recording/stop` stops capture and waits until the MP4 is finalized.

```sh
curl -fsS -X POST -H 'Content-Type: application/json' \
  --data '{"path":"/tmp/game.mp4","width":960,"height":540,"fps":30,"audio":true}' \
  "$BASE/recording/start"

curl -fsS -X POST "$BASE/recording/stop"
```

## Application synchronization

The application channel is optional. Core scene inspection and native input work without application changes. All names are limited to 128 bytes and use letters, numbers, `_`, `-`, `:`, and dot-separated namespace segments. Project-prefixed names such as `my_game.operation_complete` are recommended to avoid collisions.

Application JSON is limited to 32 KiB and 16 nested array/object levels. Commands call registered functions only; there is no endpoint for arbitrary Lua evaluation.

### Structured events and cursors

Applications emit typed JSON from Lua:

```lua
automation_bridge.emit("my_game.operation_complete", {
    operation_id = "op-42",
    success = true,
})
```

`GET /events/cursor` returns the next cursor and the oldest retained cursor. `GET /events?cursor=N&timeout_ms=10000&limit=100` long-polls for entries at or after `N`. Each event includes `sequence`, `type`, `name`, JSON `data`, engine `frame`, `native_timestamp_us`, `engine_instance_id`, and `scene_sequence`. `recording_timestamp_us` is present for recorder-correlated markers.

The event ring is bounded. A cursor older than `oldest_cursor` returns `overflow: true`, the retained range, and no silent cursor advancement. Clients must explicitly decide whether to fail or resubscribe.

### Published state

```lua
automation_bridge.publish("my_game.ui", {
    busy = false,
    workflow_step = "ready",
})
```

`GET /state` returns all latest values plus the global `revision`. `GET /state?name=my_game.ui` selects an exact state name. `GET /state/wait?after_revision=12&timeout_ms=10000` long-polls until a newer publication. Every state includes its own revision, frame, and native timestamp, so a wait can require a publication newer than the state it already observed.

### Named commands

```lua
automation_bridge.command("my_game.load_fixture", function(data)
    load_fixture(data.name)
    return { loaded = data.name }
end)
```

Submit strict JSON with `POST /commands?name=my_game.load_fixture&data=<url-encoded-json>&timeout_ms=30000`. The `202` response contains a command id. Poll `GET /commands?id=<id>` for `pending`, `running`, `completed`, `failed`, `cancelled`, or `timed_out`, plus the JSON result or error. `DELETE /commands?id=<id>` cancels only pending work. Lua callbacks run on the engine update thread and cannot be safely preempted; a running cancellation returns `409 command_not_cancellable`.

### Native delivery and application acknowledgement

Native input receipts describe only bridge lifecycle stages such as accepted, started, and released. An application can separately report its semantic decision:

```lua
automation_bridge.acknowledge_input(input_id, {
    accepted = false,
    reason = "input_locked",
})
```

Acknowledgements are retained as `type="acknowledgement"`, `name="input.acknowledged"` events whose data contains `input_id` and `result`. They do not claim which script consumed an action. Automatic propagation of a native injection id into Defold's Lua input action is not currently available.

### Semantic annotations

Rendered text does not reveal a localization key. Applications can annotate a public scene-node URL instead:

```lua
automation_bridge.annotate("main:/ui#status", {
    automation_id = "operation_status",
    localization_key = "status_ready",
    role = "status",
})
```

Annotations are copied into matching scene snapshots and can be queried with the exact `automation_id`, `localization_key`, and `role` node filters. Metadata is application-owned and disappears when the engine instance exits.

### Timeline markers

`POST /markers?name=workflow_started&data=<url-encoded-json>&recording_timestamp_us=...`
inserts a `marker` event. Native monotonic time is always recorded. The optional
recording timestamp is caller-supplied, allowing a client to correlate markers
with its own media or trace clock. Native video recording does not add this
correlation automatically.

## Metal GPU trace capture (macOS)

### `/automation-bridge/v1/metal`

Captures complete rendered frames to a Metal `.gputrace` on macOS when Defold is using the Metal graphics adapter. Launch the engine with `METAL_CAPTURE_ENABLED=1`; the parent directory of the requested output path must already exist.

Schedule a one-frame capture:

```sh
curl -fsS -X POST --get \
  --data-urlencode "path=/tmp/fontgen.gputrace" \
  "$BASE/metal" | python3 -m json.tool
```

Use `frames` to capture more than one frame:

```sh
curl -fsS -X POST --get \
  --data-urlencode "path=/tmp/fontgen.gputrace" \
  --data-urlencode "frames=60" \
  "$BASE/metal" | python3 -m json.tool
```

`POST` returns `202` with state `pending`. Capture starts at the next pre-render callback and stops after the requested number of post-render callbacks. Poll status with:

```sh
curl -fsS "$BASE/metal" | python3 -m json.tool
```

Request an early stop with:

```sh
curl -fsS -X DELETE "$BASE/metal" | python3 -m json.tool
```

Status data includes `state` (`idle`, `pending`, `capturing`, `complete`, `canceled`, or `failed`), `path`, `frames`, `frames_captured`, `stop_requested`, and `error` when startup failed.

#### Analyze the capture with `gpudebug`

Apple's `gpudebug` command-line tool can replay and inspect the resulting trace without opening Xcode's graphical Metal debugger. It is part of the newer Apple GPU command-line tooling and requires an Xcode/macOS combination that provides it. Check availability first:

```sh
xcrun --find gpudebug
man gpudebug
```

If `xcrun` cannot find it, use a newer Xcode and macOS release that includes `gpudebug`, or open the `.gputrace` in Xcode instead.

Start an interactive session:

```sh
gpudebug -t /tmp/fontgen.gputrace
```

At the `gpudebug>` prompt, begin by exploring the trace:

```text
status
list
go commands
list --all
```

Navigate into a command buffer, render or compute encoder, and draw or dispatch shown by `list`. The exact names depend on the trace:

```text
go cb0/re0/draw0
info
info pipeline
go vertex
list --all
go ..
go fragment
list --all
```

Useful commands include:

- `list` or `list --all`: show children and the actions available for each node.
- `go <name-or-path>`: navigate through command buffers, encoders, draws, dispatches, bindings, API calls, and resources.
- `info` or `info <child>`: inspect draw/dispatch arguments, encoder state, pipeline state, resource metadata, and bindings.
- `find <text>`: search labels, names, and summaries throughout the trace.
- `next` and `prev`: step between neighboring draws, dispatches, or other sibling nodes.
- `fetch <attachment-or-resource>`: export a texture, buffer, attachment, or counter series for external inspection. For example, `fetch color0` writes the selected draw's color attachment to an image file.
- `profile`: inspect or collect GPU profiling data when supported by the replay device.
- `help` and `<command> ?`: discover commands and command-specific options.

A practical visual-debugging sequence is:

```text
go commands
list --all
go cb0/re0/draw0
info pipeline
fetch color0
next
fetch color0
```

Compare fetched attachments before and after suspicious draws, and inspect the pipeline plus vertex/fragment bindings where the output first changes incorrectly.

For scripted analysis, create one persistent session and reuse the session id printed by the first command:

```sh
gpudebug -t /tmp/fontgen.gputrace -c "list"
gpudebug -s 412 -c "go commands/cb0/re0/draw0" -c "info pipeline"
gpudebug -s 412 -c "fetch color0"
gpudebug --terminate 412
```

Replace `412` and the node path with values reported for your trace. Reusing a session avoids loading and preparing the trace for every command.

For a single isolated query:

```sh
gpudebug --oneshot \
  -t /tmp/fontgen.gputrace \
  -c "go commands/cb0/re0/draw0" \
  -c "info pipeline"
```

The root normally exposes `commands`, `performance`, `api_calls`, and `resources`. Prefer following the actions printed by `list` rather than assuming a particular command-buffer or draw path.

See Apple's [interactive `gpudebug` walkthrough](https://developer.apple.com/documentation/xcode/debugging-with-interactive-command-line-tools) and [scripted/agent workflow](https://developer.apple.com/documentation/xcode/investigating-gpu-issues-with-ai-agents) for further examples.
