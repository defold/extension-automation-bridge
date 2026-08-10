---
name: defold-automation
description: Build, run, inspect, control, test, and diagnose Defold projects through the Automation Bridge MCP server. Use for Defold Editor operations, runtime scene and element queries, semantic input, synchronization, screenshots, profiling, recordings, traces, project maintenance, or access to any public Automation Bridge Python API.
---

# Defold Automation

Use the Automation Bridge MCP server as the primary interface. Keep the target
project separate from the installed plugin: pass an explicit absolute
`project_path` such as `/absolute/path/to/my-defold-game` whenever a project is
opened or a session is created. Never infer the project from the MCP server's
working directory, which is the plugin directory.

## Start a session

1. Probe an existing editor first when launching applications may be restricted.
   Open the project with `start_if_needed=false`, then ask for permission before
   launching Defold if the probe reports that it is not running.
2. Use the focused project and engine tools for the common path: open the
   project, build and run (or connect to an existing engine), then retain the
   returned session identifiers.
3. Declare required runtime capabilities before the interaction. Probe optional
   capabilities and branch around missing platform-specific features.
4. Close an engine only when this task built it and owns its lifecycle. Release
   MCP handles and context-managed operations even after failures.

On macOS, launching Defold may require an unsandboxed process; a healthy running
editor can be reused from a sandbox. On Windows, start Defold manually outside a
restricted agent process tree if the JDK loopback connection is blocked, then
probe and reuse it.

## Choose the API surface

Prefer focused tools for routine project bootstrap, health checks, element
queries, input, waits, commands, screenshots, and cleanup. For anything else:

1. Use `automation_bridge_catalog` to discover the public API, signatures,
   ownership rules, and return types.
2. Use the generic call/get operations for methods and properties. Use the
   separately annotated destructive call only for a confirmed engine shutdown,
   raw native request, or bridge update.
3. Use `automation_bridge_wait` for a declarative operation/path/predicate wait
   when no named wait fits the workflow.
4. Use enter/exit operations for context managers, next for iterators or live
   streams, and release for every stateful handle no longer needed.

The catalog and generic handle operations expose the complete public `editor`
and `engine` wrapper rather than a small curated subset. Do not guess an advanced
method name or serialize an opaque Python object yourself; inspect the catalog
and use the returned handle.

Never request or echo password preference values. Raw native engine requests
and engine shutdown require explicit confirmation; prefer named APIs whenever
one exists.

## Interact reliably

- Select elements semantically with exact names, roles, automation IDs, state,
  and visibility. Treat returned elements as snapshots and re-query after a
  scene or state change.
- Pass element results to click and drag operations so stale identity is
  detected. If an operation reports a stale element, re-query once instead of
  retrying the old snapshot.
- Use literal text input separately from validated special-key input.
- Synchronize with application events, published state, command completion,
  input acknowledgements, frame observations, element waits, or count waits.
  Avoid arbitrary sleeps.
- Subscribe before triggering the event being observed. Preserve revisions,
  scene sequences, receipts, hashes, and timestamps when evidence matters.
- Start screenshots at half resolution for inspection and request full
  resolution only when pixel detail is required.
- Prefer named APIs. Use the raw request escape hatch only when the catalog shows
  no public helper for the required native endpoint.

Consult the MCP resources for the API catalog, best-practice examples, complete
Python wrapper documentation, and plugin installation notes when a workflow
needs more detail.
