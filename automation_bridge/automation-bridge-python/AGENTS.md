# Automation Bridge Python Wrapper Notes

- Add `automation_bridge/automation-bridge-python` to `PYTHONPATH` or `sys.path` before importing `automation_bridge`.
- Public APIs have docstrings. Use `help(AutomationBridgeClient)`, `help(AutomationBridgeClient.drag)`, or `.__doc__` for quick reference.
- Prefer `AutomationBridgeClient.from_project(".", build=True)` for normal scripts; it closes known candidate engine ports, builds, waits for `Automation Bridge endpoint registered`, discovers the engine service port, and waits for `/automation-bridge/v1/health`.
- The editor may reuse the same engine process and omit a fresh service-port log line before `Automation Bridge endpoint registered`; `EditorClient` caches the last seen engine service port in `.internal/automation_bridge.engine.port`.
- If the latest registration has no fresh port, or candidate ports do not answer `/automation-bridge/v1/health`, `AutomationBridgeClient.from_editor(..., build=True)` closes candidate engine ports, rebuilds once, and retries.
- Use `AUTOMATION_BRIDGE_ENGINE_PORT` only when a running engine port is already known.
- Query nodes through `bridge.node(...)`, `bridge.maybe_node(...)`, `bridge.nodes(...)`, and `bridge.by_id(...)`; use `name_exact`/`text_exact` when exact matching matters.
- Use `bridge.parent(component_node)` when a matched component, such as a label or sprite, belongs to a parent game object that should receive input.
- `Node` objects are snapshots. Re-query after clicks, drags, or scene changes.
- Use `bridge.click(...)`, `bridge.drag(..., duration=...)`, `bridge.type_text(...)`, and `bridge.key("KEY_ENTER")`; drag blocks until the queued drag should be finished by default.
- Use `bridge.screenshot(wait=True)` and `bridge.format_nodes(...)` for diagnostics.
- Call `bridge.close_engine()` only when the script intentionally shuts down the running engine.
