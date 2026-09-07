---
name: defold-automation
description: Build, run, inspect, control, test, and diagnose Defold projects with Automation Bridge. Use for editor compilation and Bob builds, runtime elements and input, application commands and events, screenshots, profiling, recordings, or automation scripts.
---

# Defold Automation

Use focused MCP tools for interactive work. Use the bundled dependency-free
Python wrapper for substantial scripts, repeated checks, and test loops. Pass an
explicit absolute target `project_path`; the MCP working directory is the plugin
installation, not the game project.

## Diagnose and connect

1. Run `defold_doctor` for project setup, bridge version, editor, and capability
   diagnostics. The bundled wrapper requires native HTTP API v2; use extension
   2.2.1 or newer. Diagnostics do not launch applications or change files.
2. Probe with `defold_open_project(start_if_needed=false)`. When a launch is
   needed, proceed within the task's existing authorization and host permissions.
   Ask only for missing authorization or a host-required approval.
3. Use `defold_compile` to check edits without launching (Defold 1.13.2+).
   Use `defold_build_and_run` directly for runtime tests; it already compiles.
   On 1.13.2 it negotiates `run` and defaults to `focus=false`. On 1.13.1 it uses
   `build` and the editor's native focus behavior. Compile and Bob have no legacy
   fallback. Inspect `minimum_version` on unsupported-operation errors.
4. Retain `data.engine` from build-and-run plus `build_result` and session
   ownership. Attach to an existing engine with `defold_connect_engine` when
   appropriate. Declare `required_capabilities` for required runtime features.

On macOS, GUI launches may need an unsandboxed host process; existing editors
can be reused. On Windows, a JDK loopback sandbox failure requires starting
Defold outside the restricted agent process tree, then probing it again.

## Discover and act

- Search `automation_bridge_catalog` with a short query, then use
  `automation_bridge_describe(operation=...)` for the complete argument schema
  and docstring. Follow pagination cursors instead of requesting the whole API.
- Use `defold_editor_capabilities` for connected-editor command availability and
  `defold_application_catalog` for game-defined command, state, and event schemas.
- Use generic call/get tools for advanced APIs. Python callbacks and cancellation
  scopes have explicit restrictions or adapters; read the operation description.
- Select elements by exact names, roles, automation IDs, state, and visibility.
  `defold_find_elements` returns a page with `elements`, counts, continuation and
  frame evidence. Pass complete Element snapshots to click/drag for stale checks.
  Re-query after changes; pages are live snapshots. Re-query once on stale errors.
- Use `defold_key` for special keys, `hold` for sustained presses, and `modifiers`
  for chords. Use `defold_type_text` for literal text.
- Subscribe before triggering events. Synchronize with events, state revisions,
  command completion, input receipts, or frame/element waits instead of sleeps.
- Use `defold_observe` for a bounded view of elements, recent errors, and an image.
  Element and screenshot frames may differ. Screenshots and editor previews
  return visible MCP images and default to half resolution for inspection.

## Finish or interrupt

Use MCP request cancellation to stop cooperative waits. Running Lua callbacks
and in-flight editor HTTP work may continue; cancellation is not rollback. A
build or Bob timeout is not a reason to automatically repeat the operation.
Inspect completion evidence and `automation_bridge_session(action="info")`
cleanup errors before deciding the next action.

Use separate MCP sessions (`action="open"`, then pass `mcp_session` on tools) and
native client/session identities for independent work. Handles cannot cross MCP
sessions. `handle_busy` means another request is using that Python resource;
finish or cancel that request, or connect a separate client.

Exit contexts and release handles when done. `defold_close` closes the local
client and releases its input and child resources; it leaves the engine running.
Close a whole logical session with `automation_bridge_session(action="close")`.
Use `defold_close_engine` when termination is intended and authorized, normally
for an engine the task built. The `confirm=true` API flag records that intent;
it does not require asking again when the task already authorizes it. The same
principle applies to raw requests and bridge updates. Prefer named helpers.

For Python, add `<plugin-root>/python` to `PYTHONPATH` and import
`from automation_bridge import editor, engine`. Open the explicit game path;
use `try/finally` for client cleanup and close engines only when owned. Consult
`automation-bridge://docs/best-practices` and `automation-bridge://docs/python`
for longer workflows. Never request or echo password preference values.
