# Automation Bridge Extension

Debug-only native extension for inspecting and controlling a running Defold game
from local automation clients such as Codex. It provides scene and node queries,
FIFO input with receipts, screenshots, optional native desktop video recording,
and an opt-in Lua channel for application events, state, commands, semantic
annotations, and input acknowledgements.

## Installation

After Defold has fetched the extension into your project, copy the `automation-bridge-python` directory from the extension into your project. When you update the extension, update the copied Python helper directory at the same time so your automation scripts use the matching API.

The HTTP bridge is available only in debug builds. Scene inspection, input,
screenshots, recording, and timeline markers require no Lua setup. Enable the
application-defined Lua channel separately in `game.project` when a script needs
events, published state, commands, annotations, or acknowledgements:

```ini
[automation_bridge]
application_api = 1
```

## Documentation

- Native extension endpoint reference: [`automation_bridge/`](automation_bridge/README.md)
- Dependency-free Python helpers for editor bootstrap, element queries, input gestures, race-free events/state/commands, semantic annotations, timeline markers, waits, screenshots, Metal GPU traces, and diagnostics: [`automation_bridge/automation-bridge-python/`](automation_bridge/automation-bridge-python/README.md)

## Examples

Run the game-object bounds smoke test from this repository:

```sh
PYTHONPATH=automation_bridge/automation-bridge-python python3 examples/gameobject_bounds.py
```

The example builds the sample project, finds `/bounds_fixture`, checks that the parent game object bounds follow its offset sprite child, clicks the parent, and closes the engine.

Application synchronization examples are in [`examples/application_sync.script`](examples/application_sync.script) and [`examples/application_sync.py`](examples/application_sync.py). The Lua API is debug-only and must be enabled with `[automation_bridge] application_api = 1`.

Run the snapshot/observation example to exercise scene-sequence guards,
logical instance identity, frame observation, coordinate conversion, and atomic
screenshot receipts:

```sh
PYTHONPATH=automation_bridge/automation-bridge-python python3 examples/scene_observation.py
```
