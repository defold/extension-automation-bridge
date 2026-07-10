# Automation Bridge Native API

The native extension registers a curl-friendly HTTP API on Defold's engine service web server:

```text
http://127.0.0.1:<engine_service_port>/automation-bridge/v1
```

Find `<engine_service_port>` in the editor console line:

```text
INFO:ENGINE: Engine service started on port <port>
```

No Lua setup is required for v1. The endpoint is available in debug builds where Defold's engine service web server is running.

Simple Automation Bridge endpoints take query parameters. Mutating `/input/*` endpoints also accept a flat `application/json` object (string, number, and boolean values) up to 32768 bytes so correlation ids, text, and waypoint payloads are not constrained by Defold's request-resource length. The Python wrapper uses JSON for all mutations. Other request bodies are rejected with `body_not_supported`; do not mix query parameters and a JSON body.

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

Common error codes include `bad_request`, `not_found`, `method_not_allowed`, `body_not_supported`, `input_queue_full`, `input_too_large`, `input_controller_busy`, `input_device_unsupported`, `input_not_found`, `input_not_owned`, `pointer_closed`, `stale_scene`, `screen_resize_failed`, `screenshot_unsupported`, `screenshot_path_failed`, and `screenshot_pending`.

## Coordinates

Input coordinates use top-left screen pixels:

```json
{
  "coordinates": { "origin": "top-left", "units": "screen_pixels" }
}
```

Game object bounds are projected from configured `display.width` and `display.height` space to the current window size. GUI bounds are reported in screen pixels.

`GET /screen`, `PUT /screen`, and `GET /health` return:

```json
{
  "window": { "width": 1280, "height": 720 },
  "backbuffer": { "width": 1280, "height": 720 },
  "display": { "width": 960, "height": 640 },
  "viewport": { "x": 0, "y": 0, "width": 1280, "height": 720 },
  "coordinates": { "origin": "top-left", "units": "screen_pixels" }
}
```

## Nodes

Node responses always include the basic fields:

```json
{
  "id": "n:0123456789abcdef",
  "name": "play",
  "type": "gui_node_box",
  "kind": "gui_node",
  "path": "/main[0]/hud[0]/play[0]",
  "parent": "n:0011223344556677",
  "visible": true,
  "enabled": true
}
```

Optional node fields include `text`, `url`, `resource`, `bounds`, `properties`, and `children`.

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

Node ids are stable for the current scene shape. Clients should discover nodes with `/nodes`, then use `id` for `/node`, `/input/click`, and `/input/drag`. Re-discover nodes after scene changes.

## Endpoint Reference

### `GET /automation-bridge/v1/health`

Checks that the extension is running.

Response `data` includes:

- `version`: API version string.
- `debug`: `true`.
- `platform`: `macos`, `windows`, `linux`, `android`, `ios`, or `unknown`.
- `capabilities`: supported capability names such as `scene`, `nodes`, `node`, `screen.resize`, `input.click`, `input.drag`, `input.drag_path`, `input.pointer`, `input.receipts`, `input.queue`, `input.controller`, device capabilities, and, when supported, `screenshot`.
- `screen`: current screen metadata.
- `scene_sequence`: snapshot sequence number.
- `engine_frame` and `engine_instance_id`: native temporal and process-instance correlation.
- `input`: default device/visualization and supported input devices.

```sh
curl -fsS "$BASE/health" | python3 -m json.tool
```

### `GET /automation-bridge/v1/screen`

Returns window, backbuffer, configured display, viewport, and coordinate convention data.

```sh
curl -fsS "$BASE/screen" | python3 -m json.tool
```

### `PUT /automation-bridge/v1/screen`

Sets the native Defold window size and returns updated screen metadata. This endpoint is available when `GET /health` reports the `screen.resize` capability.

Query parameters:

- `width`, `height`: positive integer window size in screen pixels.

```sh
curl -fsS -X PUT "$BASE/screen?width=1280&height=720" | python3 -m json.tool
```

### `GET /automation-bridge/v1/scene`

Returns the current runtime scene tree.

Query parameters:

- `visible`: optional boolean. Use `1`, `true`, or `yes` for true; any other present value is false.
- `include`: optional comma-separated include list.

Response `data` includes `count`, `visible_filter`, `screen`, and `root`.

```sh
curl -fsS "$BASE/scene?visible=1&include=bounds,properties" | python3 -m json.tool
```

### `GET /automation-bridge/v1/nodes`

Searches nodes with simple filters. This is the main discovery endpoint for automation clients.

Query parameters:

- `id`: exact node id.
- `type`: case-insensitive substring match.
- `name`: case-insensitive substring match.
- `text`: case-insensitive substring match.
- `url`: case-insensitive substring match.
- `visible`: optional boolean filter.
- `include`: optional comma-separated include list.
- `limit`: number of returned nodes, clamped to `0..500`. Default is `50`. Use `limit=0` to get counts without node payloads.

Response `data` includes:

- `nodes`: returned node list.
- `count`: returned node count after `limit`.
- `matched`: total matching nodes before `limit`.
- `total`: total nodes in the snapshot.

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

Use `PUT /input/configure?client_id=...&session_id=...&lease=5&device=auto&visualize=1` to acquire or renew control and set defaults. Devices are exclusive per gesture: `auto`, `mouse`, or `touch`. `GET /health` reports `input.device.mouse` and, on platforms where native touch injection is supported, `input.device.touch`. The public Defold HID API has no reliable connected-touch-device predicate, so explicit touch is conservatively enabled on iOS, Android, and Switch and rejected elsewhere; one gesture never injects both mouse and touch. The proposed engine-side fix is specified in [`docs/defold-hid-device-capability-query.md`](../docs/defold-hid-device-capability-query.md).

Accepted input returns HTTP 202 and a lifecycle receipt:

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

Schedules a post-render screenshot and returns the PNG file path. The file is written asynchronously after the next render; wait for the path to exist and have nonzero size before reading it.

```sh
curl -fsS "$BASE/screenshot" | python3 -m json.tool
```

Response `data`:

```json
{ "path": "/tmp/automation-bridge/screenshot-123-1.png", "pending": true }
```
