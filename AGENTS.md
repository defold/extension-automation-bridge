# Repository guidance

- Prefer the dependency-free Python wrapper for automation scripts and tests. Add
  `automation_bridge/automation-bridge-python` to `PYTHONPATH` (or `sys.path`),
  then import `editor` and `engine` from `automation_bridge`.
- Typical bootstrap:
  `game = editor.open_project(".").build_and_run()`.
- On macOS, a bootstrap that may launch Defold must run with
  escalated/unsandboxed execution. A GUI process inherits the agent command's
  sandbox and otherwise cannot register with WindowServer or LaunchServices.
  Reusing an already healthy editor is safe without escalation; probe with
  `editor.open_project(".", start_if_needed=False)` when uncertain, then rerun
  the normal bootstrap with escalation if no editor is running.
- Treat public docstrings as the exact callable API; for example, use
  `help(engine.Client)` or `help(engine.Client.drag)`. The wrapper overview and
  examples are in `automation_bridge/automation-bridge-python/README.md`.
- Wrapper-specific agent guidance is in
  `automation_bridge/automation-bridge-python/AGENTS.md`.
- Use the native HTTP API only for endpoint work, curl-level debugging, or a
  feature the wrapper does not expose. Its agent guidance and endpoint reference
  are `automation_bridge/AGENTS.md` and `automation_bridge/README.md`.
- Run the Python tests from the repository root with:
  `PYTHONPATH=automation_bridge/automation-bridge-python python3 -m unittest tests.test_automation_bridge_api tests.test_tooling`.
