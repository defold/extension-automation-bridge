# Defold Agent Python Helpers

Small dependency-free Python helpers for driving the Defold Agent extension from tests and local automation scripts.

The package uses only the Python standard library and wraps the existing curl-friendly `/agent/v1` API.

## Quick Start

```python
from defold_agent import AgentClient

agent = AgentClient.from_project(".", build=True)

spawner = agent.node(type="goc", name="spawner", visible=True)
agent.click(spawner)

items = agent.nodes(type="labelc", text_exact="L1", limit=100)
agent.drag(agent.parent(items[0]), agent.parent(items[1]), duration=0.16)

png = agent.screenshot(wait=True)
```

Close the running engine when a script needs to clean up after itself:

```python
agent.close_engine()
```

When running from this repository without installing anything, add `agent/agent-python` to `PYTHONPATH`:

```sh
PYTHONPATH=agent/agent-python python3 tests/test_agent_api.py
```

## Bootstrap

Use the editor client when you want explicit control over the Defold editor server. `build()` waits until the runtime logs `Defold Agent endpoint registered`, then waits another 0.2 seconds before returning.
When the editor reuses the same running engine process, it may not print a fresh engine service port before `Defold Agent endpoint registered`; `EditorClient` reuses the last port it saw and stores it in `.internal/agent.engine.port`.
If the latest build logs `Defold Agent endpoint registered` without a fresh engine service port before it, `AgentClient.from_editor(..., build=True)` immediately tries to close candidate engine ports with `@system/exit`, rebuilds once, and retries. It uses the same recovery path if discovered ports do not serve `/agent/v1/health`.

```python
from defold_agent import AgentClient, EditorClient

editor = EditorClient.from_project(".")
editor.build()

agent = AgentClient.from_editor(editor, build=False)
agent.wait_ready()
```

Use an already-started engine service port directly:

```python
from defold_agent import AgentClient

agent = AgentClient(51337)
agent.wait_ready()
```

## Selectors

`nodes()` returns a list of snapshot `Node` objects. `node()` expects exactly one match. `maybe_node()` allows zero or one.

```python
restart = agent.node(name_exact="restart", enabled=True)
labels = agent.nodes(type="labelc", text="L", limit=100)
count = agent.count(type="labelc", text_exact="L2")
detail = agent.by_id(restart.id)
item = agent.parent(labels[0])
```

Server-side filters are passed to the extension: `id`, `type`, `name`, `text`, `url`, `visible`, `limit`, and `include`.

Python adds stricter client-side filters: `enabled`, `kind`, `path`, `name_exact`, `text_exact`, `has_bounds`, and `visible_and_enabled`.

`include` may be either a comma-separated string or an iterable:

```python
node = agent.node(name="spawner", include=["basic", "bounds", "properties"])
```

## Input

```python
agent.click(node)
agent.click(node.id)
agent.click((480, 320))
agent.click({"x": 480, "y": 320})

agent.drag(first_node, second_node, duration=0.16)
agent.drag(first_node.center, second_node.center, duration=0.16)

agent.type_text("hello")
agent.key("KEY_ENTER")
```

Drag calls block for the requested `duration` by default. Pass `wait=0` to only queue the input, or pass a larger wait if the game needs additional settle time after input.

`Node` objects are snapshots. If the scene may have changed, fetch a fresh node with `agent.by_id(node.id)` or query again.

Component nodes often expose useful text or properties, while their parent game object is the actionable target. Use `agent.parent(component_node)` before clicking or dragging when needed:

```python
label = agent.node(type="labelc", text_exact="L1")
item = agent.parent(label)
agent.drag(item, (480, 320))
```

## Waits and Screenshots

```python
from defold_agent import wait_until

wait_until(lambda: agent.count(type="labelc", text_exact="L2") >= 1)

node = agent.wait_for_node(name_exact="restart", enabled=True)
agent.wait_for_count(2, type="labelc", text_exact="L1")

png = agent.screenshot(wait=True, timeout=5)
```

## Engine Control

`agent.close_engine()` posts Defold's built-in `@system/exit` message to the same engine service port used by the agent API. It is equivalent to:

```sh
printf '\010\000' | curl -sS -X POST --data-binary @- \
  "http://127.0.0.1:<ENGINE_PORT>/post/@system/exit"
```

## Diagnostics

Selector errors include the selector and compact candidate node details. You can also format or dump scene state explicitly:

```python
print(agent.format_nodes(type="gui_node", limit=100))
print(agent.dump_scene(visible=True, include=["basic", "bounds"]))
agent.dump_scene("scene.json", visible=True)
```

## Errors

All custom errors inherit from `DefoldAgentError`.

- `HttpError`: transport failures or invalid JSON.
- `AgentApiError`: extension returned `{ "ok": false }`.
- `SelectorError`: `node()` or `maybe_node()` found the wrong number of nodes.
