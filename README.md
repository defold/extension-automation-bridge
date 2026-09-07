# Automation Bridge Extension

Debug-only native extension for inspecting and controlling a running Defold game
from local automation clients such as Codex. It provides scene and element queries,
FIFO input with receipts, screenshots, optional native desktop video recording,
and an opt-in Lua channel for application events, state, commands, semantic
annotations, and input acknowledgements.

## Installation

To install the matching Python wrapper without manually copying files, first
Fetch Libraries in the target project, then run the installer from an extension
checkout:

```sh
python3 automation_bridge/automation-bridge-python/install.py /absolute/path/to/game
```

The installer uses that game's fetched dependency archive, requires no editor
connection or `PYTHONPATH`, and leaves `game.project` unchanged. It replaces the
managed wrapper directory as a unit; keep your scripts outside that directory.

After Defold has fetched the extension into your project, copy the
`automation-bridge-python` directory from the extension into your project root.
When you update the extension, update the copied Python helper directory at the
same time so your automation scripts use the matching API. From the project
root, add the copied directory to `PYTHONPATH`:

```sh
PYTHONPATH=automation-bridge-python python3 your_script.py
```

Starting with extension 2.1.0, the copied wrapper provides
`project.update_automation_bridge()` to select the latest stable release, update
the dependency, invoke Defold's Fetch Libraries command, and atomically refresh
the project-root Python wrapper. Pass a version to pin a specific release. Run
it as a standalone maintenance step and restart Python afterward.

The HTTP bridge is available only in debug builds. Scene inspection, input,
screenshots, recording, and timeline markers require no Lua setup. Enable the
application-defined Lua channel separately in `game.project` when a script needs
events, published state, commands, annotations, or acknowledgements:

```ini
[automation_bridge]
application_api = 1
```

## Documentation

Diagnose setup without launching, building, or changing the project:

```python
from automation_bridge import editor

report = editor.doctor(".", required_capabilities=("elements", "input.click"))
for check in report.checks:
    print(check.name, check.status, check.message, check.action or "")
```

`report.ready` requires a compatible running engine. `report.as_dict()` provides
JSON-serializable evidence, including independently reported Python and native
versions and runtime capabilities.

- Native extension endpoint reference: [`automation_bridge/`](automation_bridge/README.md)
- Dependency-free Python helpers for editor bootstrap, element queries, input gestures, race-free events/state/commands, semantic annotations, timeline markers, waits, screenshots, Metal GPU traces, and diagnostics: [`automation_bridge/automation-bridge-python/`](automation_bridge/automation-bridge-python/README.md)
- Development constraints and regression-prevention rationale: [`DEVELOPMENT.md`](DEVELOPMENT.md)

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
