# Python wrapper guidance

- Add this directory to `PYTHONPATH` and use only the package-root namespaces:
  `from automation_bridge import editor, engine`.
- Bootstrap with `game = editor.open_project(".").build_and_run()`. Close the
  engine only when the script owns its lifecycle.
- For migrations, run `project.update_automation_bridge()`, restart Python, then
  apply the relevant steps from `MIGRATION.md`. Pass a version only when pinning.
  Never merge wrapper versions or store project code in the wrapper directory.
- Probe a running editor with
  `editor.open_project(".", start_if_needed=False)` before launching one.
- macOS: a launch must run unsandboxed; an existing editor may be reused.
  Detached processes and `/usr/bin/open` do not escape the inherited sandbox.
- Windows: if the sandbox blocks the JDK loopback connection, start Defold
  manually outside the agent process tree, then reuse it. Changing shells or
  detaching a child process does not escape the inherited sandbox.
- Declare required features with `required_capabilities` or `game.require()`;
  probe optional features with `game.supports()`.
- Prefer named helpers. Use `game.request(...)` only when no wrapper helper
  exposes the required native feature.
- `elements()` is paginated; use `count()` when the complete match count is
  required.
- `Element` objects are snapshots. Re-query after state or scene changes. Pass
  `Element` objects to `click()` and both ends of `drag()` for stale-identity
  protection; handle `engine.StaleElementError` by re-querying.
- Use `game.parent(component)` when a visible child exposes the selector but its
  parent receives input. Use `type_text()` for literal text and `key()` for one
  validated special key.
- Prefer events, published state, commands, acknowledgements, frame waits, and
  element waits over sleeps.
- Use atomic receipts for screenshots and recordings. Start visual inspection at
  `resolution_multiplier=0.5`; use `project.preview.render()` for editor-side
  validation without running the game. Optional diagnostics live under
  `game.visual`, `game.gestures`, `game.video_recording`, `game.metal_capture`,
  `game.profiler`, and `game.trace(...)`.
- Public docstrings are the API reference. See `README.md` for examples and
  `best_practices.py` for copyable agent patterns. See `MIGRATION.md` for
  upgrades and `README.md` for detailed workflows.
- In the extension source checkout, run tests from the repository root:
  `PYTHONPATH=automation_bridge/automation-bridge-python python3 -m unittest tests.test_automation_bridge_api tests.test_tooling`.
