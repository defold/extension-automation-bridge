# Repository guidance

- Prefer the dependency-free Python wrapper for automation scripts and tests. Add
  `automation_bridge/automation-bridge-python` to `PYTHONPATH` (or `sys.path`),
  then import `editor` and `engine` from `automation_bridge`.
- Typical bootstrap:
  `game = editor.open_project(".").build_and_run()`.
- Reusing an already healthy editor is safe from a sandboxed agent shell. Probe
  with `editor.open_project(".", start_if_needed=False)` when uncertain.
- On macOS, a bootstrap that may launch Defold must run with
  escalated/unsandboxed execution. A GUI process inherits the agent command's
  sandbox and otherwise cannot register with WindowServer or LaunchServices.
  After `NotRunningError`, rerun the normal bootstrap with escalation.
- On Windows, an editor launched as a descendant of a sandboxed agent may abort
  before writing `.internal/editor.port` because the sandbox blocks the JDK's
  internal loopback socket. Running a command in another agent shell, or marking
  only that child command unsandboxed, may still leave it in the restricted
  process tree. After `NotRunningError`, start Defold outside the agent process
  tree, for example manually from the Start menu or Explorer, then rerun the
  probe to reuse it.
- Treat public docstrings as the exact callable API; for example, use
  `help(engine.Client)` or `help(engine.Client.drag)`. The wrapper overview and
  examples are in `automation_bridge/automation-bridge-python/README.md`.
- `game.key(...)` accepts normalized names such as `M`, `SPACE`, and
  `KEY_ENTER`; invalid names fail before queueing. Passing an `Element` directly
  to `click()`, or `Element` objects as both endpoints of `drag()`, adds a stable
  runtime-identity guard. Elements remain snapshots and should be re-queried
  after state or scene changes.
- Wrapper-specific agent guidance is in
  `automation_bridge/automation-bridge-python/AGENTS.md`.
- Use the native HTTP API only for endpoint work, curl-level debugging, or a
  feature the wrapper does not expose. Its agent guidance and endpoint reference
  are `automation_bridge/AGENTS.md` and `automation_bridge/README.md`.
- Run the Python tests from the repository root with:
  `PYTHONPATH=automation_bridge/automation-bridge-python python3 -m unittest tests.test_automation_bridge_api tests.test_tooling`.
