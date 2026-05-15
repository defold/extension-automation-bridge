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

All Automation Bridge endpoints take query parameters only. Request bodies are rejected with `body_not_supported`.

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

Common error codes include `bad_request`, `not_found`, `method_not_allowed`, `body_not_supported`, `input_queue_full`, `input_too_large`, `screen_resize_failed`, `screenshot_unsupported`, `screenshot_path_failed`, and `screenshot_pending`.

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
- `capabilities`: supported capability names such as `scene`, `nodes`, `node`, `screen.resize`, `input.click`, `input.drag`, `input.key`, and, when supported, `screenshot`.
- `screen`: current screen metadata.
- `scene_sequence`: snapshot sequence number.

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

### `POST /automation-bridge/v1/input/click`

Queues a left-click. Use either a node id or absolute screen coordinates.

By default, the last mouse/touch input is drawn for one second: clicks appear as a growing circle, and drags appear as a line. Pass `visualize=0` to disable this for a specific input.

```sh
curl -fsS -X POST "$BASE/input/click?id=n:0123456789abcdef"
curl -fsS -X POST "$BASE/input/click?x=480&y=320&visualize=0"
```

Response `data`:

```json
{ "queued": "click", "x": 480, "y": 320 }
```

### `POST /automation-bridge/v1/input/drag`

Queues a left-button/touch drag. Use either node ids or absolute screen coordinates.

Query parameters:

- `from_id` and `to_id`: drag from the center of one node to the center of another.
- `x1`, `y1`, `x2`, `y2`: drag between absolute screen positions.
- `duration`: optional duration in seconds. Default is `0.35`; negative values are clamped to `0`.
- `visualize`: optional input visualization flag. Defaults to `1`; use `0` to disable.

```sh
curl -fsS -X POST "$BASE/input/drag?from_id=n:1111111111111111&to_id=n:2222222222222222&duration=0.35"
curl -fsS -X POST "$BASE/input/drag?x1=100&y1=100&x2=500&y2=300&duration=0.35"
```

Response `data`:

```json
{
  "queued": "drag",
  "from": { "x": 100, "y": 100 },
  "to": { "x": 500, "y": 300 },
  "duration": 0.35
}
```

### `POST /automation-bridge/v1/input/key`

Queues keyboard input.

Use `text` for plain UTF-8 text:

```sh
curl -fsS -X POST "$BASE/input/key?text=hello"
```

Use `keys` for Poco-style special key events. URL-encode braces when calling from shells:

```sh
curl -fsS -X POST "$BASE/input/key?keys=%7BKEY_ENTER%7D"
```

Special key names include `KEY_SPACE`, `KEY_ESC`, `KEY_ESCAPE`, `KEY_UP`, `KEY_DOWN`, `KEY_LEFT`, `KEY_RIGHT`, `KEY_TAB`, `KEY_ENTER`, `KEY_BACKSPACE`, `KEY_INSERT`, `KEY_DEL`, `KEY_DELETE`, `KEY_PAGEUP`, `KEY_PAGEDOWN`, `KEY_HOME`, `KEY_END`, `KEY_LSHIFT`, `KEY_RSHIFT`, `KEY_LCTRL`, `KEY_RCTRL`, `KEY_LALT`, `KEY_RALT`, `KEY_F1` through `KEY_F12`, `KEY_A` through `KEY_Z`, and `KEY_0` through `KEY_9`.

The `text` or `keys` query value may be up to 4096 bytes.

Response `data`:

```json
{ "queued": "key", "length": 5 }
```

### `GET /automation-bridge/v1/screenshot`

Schedules a post-render screenshot and returns the PNG file path. The file is written asynchronously after the next render; wait for the path to exist and have nonzero size before reading it.

```sh
curl -fsS "$BASE/screenshot" | python3 -m json.tool
```

Response `data`:

```json
{ "path": "/tmp/automation-bridge/screenshot-123-1.png", "pending": true }
```
