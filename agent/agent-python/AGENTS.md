# Agent Python Wrapper Notes

- Add `agent/agent-python` to `PYTHONPATH` or `sys.path` before importing `defold_agent`.
- Public APIs have docstrings. Use `help(AgentClient)`, `help(AgentClient.drag)`, or `.__doc__` for quick reference.
- Prefer `AgentClient.from_project(".", build=True)` for normal scripts; it builds, waits for `Defold Agent endpoint registered`, discovers the engine service port, and waits for `/agent/v1/health`.
- The editor may reuse the same engine process and omit a fresh service-port log line before `Defold Agent endpoint registered`; `EditorClient` caches the last seen engine service port in `.internal/agent.engine.port`.
- If the latest registration has no fresh port, or candidate ports do not answer `/agent/v1/health`, `AgentClient.from_editor(..., build=True)` closes candidate engine ports, rebuilds once, and retries.
- Use `AGENT_ENGINE_PORT` only when a running engine port is already known.
- Query nodes through `agent.node(...)`, `agent.maybe_node(...)`, `agent.nodes(...)`, and `agent.by_id(...)`; use `name_exact`/`text_exact` when exact matching matters.
- Use `agent.parent(component_node)` when a matched component, such as a label or sprite, belongs to a parent game object that should receive input.
- `Node` objects are snapshots. Re-query after clicks, drags, or scene changes.
- Use `agent.click(...)`, `agent.drag(..., duration=...)`, `agent.type_text(...)`, and `agent.key("KEY_ENTER")`; drag blocks for its duration by default.
- Use `agent.screenshot(wait=True)` and `agent.format_nodes(...)` for diagnostics.
- Call `agent.close_engine()` only when the script intentionally shuts down the running engine.
