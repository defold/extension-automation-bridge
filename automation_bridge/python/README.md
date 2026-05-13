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
```

Close the running engine when a script needs to clean up after itself:

```python
bridge.close_engine()
```

When running from this repository without installing anything, add `automation_bridge/python` to `PYTHONPATH`:

```sh
PYTHONPATH=automation_bridge/python python3 tests/test_automation_bridge_api.py
```

## Bootstrap

Use the editor client when you want explicit control over the Defold editor server. `AutomationBridgeClient.from_editor(..., build=True)` closes known candidate engine ports before building so repeated runs start from a fresh engine process. `build()` waits until the runtime logs `Automation Bridge endpoint registered`, then waits another 0.2 seconds before returning.
When the editor reuses the same running engine process, it may not print a fresh engine service port before `Automation Bridge endpoint registered`; `EditorClient` reuses the last port it saw and stores it in `.internal/automation_bridge.engine.port`.
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
node = bridge.node(name="spawner", include=["basic", "bounds", "properties"])
```

## Input

```python
bridge.click(node)
bridge.click(node.id)
bridge.click((480, 320))
bridge.click({"x": 480, "y": 320})

bridge.drag(first_node, second_node, duration=0.16)
bridge.drag(first_node.center, second_node.center, duration=0.16)

bridge.type_text("hello")
bridge.key("KEY_ENTER")
```

Drag calls block until the requested `duration` has completed and the engine has had a short extra moment to process the release. Pass `wait=0` to only queue the input, or pass a larger wait if the game needs additional settle time after input.

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

## Engine Control

`bridge.close_engine()` posts Defold's built-in `@system/exit` message to the same engine service port used by the Automation Bridge API. If the process stays alive on macOS/Linux, the helper falls back to terminating the local process listening on that port. The graceful request is equivalent to:

```sh
printf '\010\000' | curl -sS -X POST --data-binary @- \
  "http://127.0.0.1:<ENGINE_PORT>/post/@system/exit"
```

## Diagnostics

Selector errors include the selector and compact candidate node details. You can also format or dump scene state explicitly:

```python
print(bridge.format_nodes(type="gui_node", limit=100))
print(bridge.dump_scene(visible=True, include=["basic", "bounds"]))
bridge.dump_scene("scene.json", visible=True)
```

## Errors

All custom errors inherit from `AutomationBridgeError`.

- `HttpError`: transport failures or invalid JSON.
- `AutomationBridgeApiError`: extension returned `{ "ok": false }`.
- `SelectorError`: `node()` or `maybe_node()` found the wrong number of nodes.
