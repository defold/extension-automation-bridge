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

- Install Defold and make `python3` available on `PATH`.
- Use a Defold project whose root contains `game.project`.
- Add Automation Bridge as a project dependency and fetch libraries. The
  current stable dependency URL is:

```ini
[project]
dependencies#0 = https://github.com/defold/extension-automation-bridge/archive/refs/tags/2.0.2.zip
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

Agent Plugins 1.0.0 clients discover `plugin.json`, the immediate skill under
`skills/`, and `mcp.json` at fixed locations. The current official
[compatible-client matrix](https://agent-plugins.org/compatible-clients) lists
VS Code, Cursor, GitHub Copilot, ChatGPT and Codex, Kiro, and Hermes Agent with
stdio support. Follow each client's source-install flow; for example, GitHub
Copilot CLI accepts:

```sh
copilot plugin install /absolute/path/to/extension-automation-bridge/plugins/automation-bridge
```

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

Always open a project with its explicit absolute `project_path`. A typical
session opens `/absolute/path/to/my-defold-game`, builds and runs it, operates on
the returned project or engine session, and releases all handles at the end.

Focused tools cover routine project bootstrap, engine connection and health,
element queries, clicks and drags, text and key input, synchronization, commands,
screenshots, and lifecycle cleanup. The advanced API surface makes every public
Automation Bridge Python API usable:

- `automation_bridge_catalog` describes modules, members, signatures, argument
  schemas, ownership, and return behavior.
- `automation_bridge_call` invokes non-destructive public methods and functions.
- `automation_bridge_destructive_call` isolates engine shutdown, raw native
  requests, and bridge updates behind explicit confirmation and a destructive
  tool annotation.
- `automation_bridge_get` reads public properties.
- `automation_bridge_enter` and `automation_bridge_exit` manage context objects.
- `automation_bridge_next` advances stateful iterators and streams.
- `automation_bridge_release` releases any retained object handle.

Opaque Python objects are represented by MCP handles, so editor clients, engine
clients, elements, receipts, subscriptions, pointer sessions, recordings,
traces, and other stateful APIs can be composed instead of flattened into an
incomplete subset. MCP resources expose the full API catalog, best-practice
examples, wrapper reference, and this installation guide.

The checked catalog currently contains 197 operations: the audited 195-member
editor/engine operational surface plus declarative adapters for
`engine.wait_until` and `InputController.interruption_scope`. Python callback
parameters become explicit recording stop/abort calls, exception-class retry
parameters use the wrapper's safe defaults, varargs and integer-key maps have
lossless JSON adapters, and complete `Element.raw` snapshots round-trip so
logical stale-element guards remain active. The raw engine request escape hatch
is present but requires explicit confirmation. Password preference values are
never returned over MCP; preference metadata and non-secret values remain
available.

## Maintain and validate the package

After changing the canonical wrapper or MCP implementation, refresh the vendored
copy and compare it with the source:

```sh
python3 plugins/automation-bridge/scripts/sync_python.py
python3 plugins/automation-bridge/scripts/validate_plugin.py --check-sync
PYTHONPATH=automation_bridge/automation-bridge-python \
  python3 -m unittest tests.test_mcp_server
```

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
