# Repository guidance

- Use the dependency-free Python wrapper for automation scripts and tests. Add
  `automation_bridge/automation-bridge-python` to `PYTHONPATH`, then import
  `editor` and `engine` from `automation_bridge`.
- Bootstrap with `game = editor.open_project(".").build_and_run()`.
- To avoid an unnecessary editor launch, probe with
  `editor.open_project(".", start_if_needed=False)`.
- macOS: run a bootstrap that may launch Defold unsandboxed. A running editor may
  be reused from the sandbox.
- Windows: after `NotRunningError` caused by the JDK loopback sandbox, start
  Defold manually outside the agent process tree, then reuse it with the probe.
- Public docstrings are the Python API reference. Start with
  `automation_bridge/automation-bridge-python/best_practices.py`; see the nearby
  `AGENTS.md` and `README.md` for wrapper rules and details.
- Follow `DEVELOPMENT.md` for cross-layer, protocol, validation, and test changes.
- Use the native API only for endpoint implementation or transport debugging.
  See `automation_bridge/AGENTS.md` and `automation_bridge/README.md`.
- Run tests from the repository root:
  `PYTHONPATH=automation_bridge/automation-bridge-python python3 -m unittest tests.test_automation_bridge_api tests.test_tooling`.
