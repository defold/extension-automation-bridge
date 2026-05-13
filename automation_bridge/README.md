# Automation Bridge Native API

The native extension registers a curl-friendly API on Defold's engine service web server:

```text
http://127.0.0.1:<engine_service_port>/automation-bridge/v1
```

Find `<engine_service_port>` in the editor console line:

```text
INFO:ENGINE: Engine service started on port <port>
```

No Lua setup is required for v1.

## Endpoints

```text
GET /automation-bridge/v1/health
```

Checks that the extension is running. Returns API version, platform, screen data, and available capabilities.

```text
GET /automation-bridge/v1/screen
```

Returns window, backbuffer, viewport, and coordinate convention data. Input coordinates use top-left screen pixels.

```text
GET /automation-bridge/v1/scene?visible=1&include=basic,bounds,properties
```

Returns the current runtime scene tree. Use this when an automation client needs broad context before selecting a target.

```text
GET /automation-bridge/v1/nodes?type=gui_node_box&name=play&text=Play&visible=1&limit=20
```

Searches nodes with simple filters. This is the main discovery endpoint for automation clients.

```text
GET /automation-bridge/v1/node?id=<node_id>&include=basic,bounds,properties,children
```

Returns one node in detail. Use this after `/nodes` when exact bounds or properties are needed.

```text
POST /automation-bridge/v1/input/click?id=<node_id>
```

Clicks the center of a known node.

```text
POST /automation-bridge/v1/input/click?x=480&y=320
```

Clicks an absolute screen position in top-left pixel coordinates.

```text
POST /automation-bridge/v1/input/drag?from_id=<node_id>&to_id=<node_id>&duration=0.35
```

Drags from the center of one node to the center of another node.

```text
POST /automation-bridge/v1/input/drag?x1=100&y1=100&x2=500&y2=300&duration=0.35
```

Drags between absolute screen positions.

```text
POST /automation-bridge/v1/input/key?text=hello
```

Types plain text through Defold HID keyboard input.

```text
POST /automation-bridge/v1/input/key?keys=%7BKEY_ENTER%7D
```

Sends special key events using the Poco-style `{KEY_NAME}` format. URL-encode braces when calling from shells.

```text
GET /automation-bridge/v1/screenshot
```

Schedules a post-render screenshot and returns the PNG file path.

## Response Shape

Success:

```json
{ "ok": true, "data": {} }
```

Error:

```json
{ "ok": false, "error": { "code": "not_found", "message": "..." } }
```

Node responses include stable opaque `id` values. Clients should discover with `/nodes`, then use `id` for follow-up `/node` and input calls.
