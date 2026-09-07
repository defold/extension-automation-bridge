# Automation Bridge agent plugin

This directory is a self-contained [Agent Plugins 1.0.0](https://agent-plugins.org/specification)
package for Automation Bridge. It provides an Agent Skill and a local stdio MCP
server that can build, run, inspect, control, test, and diagnose Defold projects.
The bundled Python wrapper has no third-party runtime dependencies.

The plugin directory is the installation unit:

```text
/absolute/path/to/extension-automation-bridge/plugins/automation-bridge
```

Do not install the repository root as the plugin. Likewise, do not use the
plugin directory as the target Defold project. Project operations must receive
an explicit absolute path such as:

```text
/absolute/path/to/my-defold-game
```

## Package layout

```text
automation-bridge/
├── plugin.json                 # portable Agent Plugins manifest
├── mcp.json                    # portable Agent Plugins MCP configuration
├── skills/defold-automation/   # portable Agent Skill
├── scripts/run_mcp.py          # stdio launcher
├── python/                     # bundled Automation Bridge Python API
├── .codex-plugin/plugin.json   # Codex-native compatibility manifest
└── .mcp.json                   # Codex-native MCP compatibility config
```

The portable files target the canonical 1.0.0 schemas. `${PLUGIN_ROOT}` is used
only in portable `mcp.json` arguments and working directory, where the
specification requires clients to expand it. The launcher independently locates
the plugin and prepends `python/` to `sys.path`; it does not depend on a caller's
`PYTHONPATH`.

## Target project prerequisites

- Install Defold 1.13.1 or newer and make Python 3.10+ available as `python3` on `PATH`.
- Use a Defold project whose root contains `game.project`.
- Add Automation Bridge as a project dependency and fetch libraries. The
  bundled wrapper uses native HTTP API v2 and requires extension 2.2.1 or newer.
  A compatible pinned dependency is:

```ini
[project]
dependencies#0 = https://github.com/defold/extension-automation-bridge/archive/refs/tags/2.2.1.zip
```

Choose the next free `dependencies#N` index when the project already has
dependencies. Releases are listed at
[defold/extension-automation-bridge](https://github.com/defold/extension-automation-bridge/releases),
and the source is at
[github.com/defold/extension-automation-bridge](https://github.com/defold/extension-automation-bridge).

Automation Bridge is available only in debug builds. Scene inspection, input,
screenshots, recording, and diagnostics need no Lua setup. Application-defined
events, state, commands, semantic annotations, and input acknowledgements also
require this project setting:

```ini
[automation_bridge]
application_api = 1
```

## Install in Codex

This repository contains a local marketplace at
`/absolute/path/to/extension-automation-bridge/.agents/plugins/marketplace.json`.
Register the repository root, install the plugin from its `personal` marketplace,
and then start a new Codex task so its skill and MCP tools are loaded:

```sh
codex plugin marketplace add /absolute/path/to/extension-automation-bridge
codex plugin add automation-bridge@personal
```

The `.codex-plugin/plugin.json` and `.mcp.json` files retain native Codex
compatibility while `plugin.json` and `mcp.json` provide the portable layout.

## Install in another Agent Plugins client

Point the client's **Install Plugin From Source** flow at this directory, not at
the repository root:

```text
/absolute/path/to/extension-automation-bridge/plugins/automation-bridge
```

Clients supporting Agent Plugins discover the portable `plugin.json`, skill,
and `mcp.json`. Use that client's source-install workflow. The direct MCP
configuration below also works with clients that implement standard stdio MCP.

For a client that does not yet load Agent Plugins packages, use the direct MCP
configuration below and load
`skills/defold-automation/SKILL.md` through that client's native skill mechanism.

On Windows, the portable manifest still requires a `python3` executable because
Agent Plugins 1.0.0 has no operating-system-specific command alternatives. If a
client cannot resolve `python3`, use a native direct-MCP entry with that machine's
Python executable while keeping the script path absolute.

## Use the MCP server directly

Clients with a native MCP configuration can launch the same bundled server
without installing the plugin. Replace both example paths with real absolute
paths; the `cwd` is the plugin directory, never the game project:

```json
{
  "mcpServers": {
    "automation-bridge": {
      "command": "python3",
      "args": [
        "/absolute/path/to/extension-automation-bridge/plugins/automation-bridge/scripts/run_mcp.py"
      ],
      "env": {
        "PYTHONIOENCODING": "utf-8",
        "PYTHONUNBUFFERED": "1"
      },
      "cwd": "/absolute/path/to/extension-automation-bridge/plugins/automation-bridge"
    }
  }
}
```

For an MCP Inspector smoke test, run this from the plugin directory:

```sh
npx @modelcontextprotocol/inspector --cli python3 scripts/run_mcp.py --method tools/list
```

The server writes MCP protocol messages only to stdout. Diagnostics are written
to stderr.

The dependency-free server supports the stateful MCP `initialize` flow used by
Codex and MCP Inspector for protocol versions `2025-11-25` and `2025-06-18`.
It also supports the stateless `server/discover` flow introduced by MCP
`2026-07-28`; responses use the wire shape negotiated for that protocol era
rather than mixing legacy and modern fields.

## Use the APIs

1. Call `defold_doctor` with the absolute game `project_path` to diagnose setup
   without starting an editor or changing files.
2. Probe `defold_open_project` with `start_if_needed=false`. If a launch is needed,
   use the task's existing authorization and the host's application permissions.
3. Call `defold_compile` for a compilation check or `defold_build_and_run`
   directly for runtime testing. Build-and-run returns `data.engine`,
   `data.build_result`, and `data.session`; retain the engine handle.
4. Use `defold_find_elements` or `defold_observe`, then interact and synchronize
   through focused input, event, state, and command tools.
5. Close local clients with `defold_close`, release individual handles, or close
   a logical MCP session. Terminate an engine only when intended and authorized.

| Editor feature | Defold 1.13.1 | Defold 1.13.2+ |
| --- | --- | --- |
| Build and run | Legacy `build` command | Negotiated `run` command; compilation included |
| Focus | Native editor behavior | Defaults to `false` when advertised; explicit `focus` supported |
| Compile only | Unsupported, with `minimum_version` | `defold_compile` |
| Bob build/bundle | Unsupported, with `minimum_version` | `defold_bob(options, commands)` using editor token authentication |
| Completion | Legacy acknowledgements are marked incomplete | Structured results with warnings/errors, ranges, and optional target URL |

Capabilities come from the connected editor's advertised API. A prerelease that
lacks a feature is handled like an older editor. Bob authentication is managed by
the wrapper; callers never supply the editor token. Build failures preserve
`result` and source locations. Build/Bob timeouts do not imply that work stopped,
and are not marked automatically retryable.

### Discovery and JSON adapters

`automation_bridge_catalog` returns searchable summary pages (20 by default,
maximum 100). Follow `next_cursor`; use `automation_bridge_describe` for one
operation's full docstring, signature, argument schema, and restrictions. The
catalog resource contains the first page. MCP `tools/list` returns the complete focused-tool inventory for hosts that do not follow tool cursors.
Use `defold_editor_capabilities` for the connected editor's commands and
`defold_application_catalog` for the game's commands, states, and events.

Generic `automation_bridge_call` and `automation_bridge_get` use allowlisted
operation IDs. `automation_bridge_destructive_call` covers engine shutdown, raw
native requests, and bridge updates. Its `confirm=true` flag records intended
execution within existing authorization; it does not impose a fresh user prompt.
Enter/exit, next, release, and declarative wait tools adapt Python contexts,
iterators, and predicates. Callback and exception-class parameters have explicit
restrictions. Python cancellation scopes are managed per MCP request and cannot
be retained across worker threads. Reads of password preferences and groups
containing them are rejected before contacting the editor; read non-secret leaf
preferences separately. Bare handle strings are resolved in handle arguments,
while ordinary application strings remain literal.

The `exists` wait predicate keeps polling while its selected path is absent or
null, including paths through arrays that have not populated yet.

`defold_find_elements` returns `elements`, match counts, `next_cursor`, and
frame/scene evidence. Preserve the complete Element wire values when passing
targets to click and drag. Cursors traverse live snapshots, so re-query after
scene changes. `defold_key` forwards holds and modifiers; click/drag support chords.

### Images and observations

Completed screenshots and editor previews include native MCP PNG image content
alongside structured metadata. Image bytes are not duplicated into JSON text.
Pending captures return their receipt without an image. The focused screenshot,
preview, and observation workflows default to half resolution. Images are limited
to 8 MiB; use a smaller resolution if needed. Missing or replaced capture files
produce an error retaining the original receipt.

Pass the complete screenshot receipt to `game.visual.difference()`,
`assert_matches()`, or `wait_for_region_change()` through the generic MCP tools.
Their argument schemas accept screenshot receipts, paths, and base64 byte
envelopes. Receipt metadata is reconstructed only in receipt-typed arguments.

`defold_observe` returns at most 50 elements (20 by default), up to 50 recent error
lines (10 by default, 2,000 characters each), and an optional screenshot. Element,
log, and screenshot samples are independent; inspect the supplied frame evidence
instead of assuming they were captured simultaneously.

### Sessions, cancellation, and cleanup

One logical session is available by default. Open another with
`automation_bridge_session(action="open")` and pass the returned `mcp_session`
on subsequent tools. Sessions isolate handles within this local server process;
they are coordination boundaries, not authentication for untrusted clients.

Engine handles report `owns_engine` and `session_info`. Connect/build forward
explicit `client_id` and `session_id`, or generate independent native identities.
A duplicate identity pair is rejected while retained or connecting. Overlapping
requests on the same Python client/resource return `handle_busy`; separate native
clients continue to follow the engine's lease and execution rules.

`defold_close` releases client input and child resources while leaving the engine
running. Session close and EOF cancel work and finalize resources. Cleanup waits
for active users of a handle; late results cannot allocate handles after shutdown.
Owned engines also remain running on session cleanup: use `defold_close_engine`
explicitly when termination is intended.

MCP cancellation stops cooperative polling and requests native input/command
cleanup. It cannot forcibly interrupt an HTTP request or a running Lua callback.
Cancelled requests emit no response. Inspect bounded cleanup diagnostics through
`automation_bridge_session(action="info")`; shutdown also reports them to stderr.
A cleanup failure is preserved rather than silently reported as success. Failed
pointer and input-scope exits retain their handles for inspection or retry and
appear in the session's cleanup diagnostics. Cancelling an idle observer does not
acquire a native input controller lease.

### Python scripts

For substantial scripts or test loops, use the same bundled wrapper directly:

```sh
PYTHONPATH="/absolute/path/to/plugin/python" python3 my_test.py
```

```python
from automation_bridge import editor, engine

project = editor.open_project("/absolute/path/to/my-defold-game", start_if_needed=False)
game = project.build_and_run()
try:
    print(game.elements_page(role="button", limit=10))
finally:
    if game.owns_engine:
        game.close_engine()
    game.close()
```

The public docstrings and MCP Python/best-practices resources describe the full
wrapper. Each Python process owns its own client handles.

## Maintain and validate the package

After changing the canonical wrapper or MCP implementation, refresh the vendored
copy and compare it with the source:

```sh
python3 plugins/automation-bridge/scripts/sync_python.py
python3 plugins/automation-bridge/scripts/validate_plugin.py --check-sync
PYTHONPATH=automation_bridge/automation-bridge-python \
  python3 -m unittest tests.test_mcp_server tests.test_mcp_improvements
```

When updating an installed local plugin, refresh the Codex manifest's cachebuster
and mirror that version into portable `plugin.json`, validate, then reinstall:

```sh
codex plugin add automation-bridge@personal
```

Use a new Codex task to load the updated tools and skill. The launcher works from
an installed cache, unrelated working directories, and paths containing spaces;
keep the script path absolute in direct MCP configurations.

The conformance suite checks every catalog owner/dispatcher, every physical MCP
tool envelope, protocol-era wire shapes, strict schemas, handle/context/
iterator lifecycles, stale Element round-trips, declarative adapters, structured
errors, and vendored-source parity. Platform-dependent calls still require a
running Defold Editor/engine and the corresponding host capability (for example
video recording, Metal capture, or Remotery profiling).

The dependency-free validator checks both manifest layouts, the Agent Plugins
1.0.0 closed shapes and placeholder rules, skill discovery, path containment,
marketplace wiring, launcher syntax, and bundled source parity. The canonical
JSON Schemas remain authoritative:

- [plugin.schema.json](https://agent-plugins.org/schemas/1.0.0/plugin.schema.json)
- [mcp.schema.json](https://agent-plugins.org/schemas/1.0.0/mcp.schema.json)

Agent Plugins does not currently publish an official whole-package validation
CLI, so schema validation, this semantic validation, and an MCP handshake are
separate checks.
