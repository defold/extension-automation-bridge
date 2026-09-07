# Development Guardrails

This file records stable, cross-cutting engineering rules. Endpoint behavior,
individual regressions, and feature-specific instructions belong in API docs,
tests, and comments next to the implementation.

## Keep policy in one authoritative layer

Each safety invariant and default should have one owner. The native runtime owns
execution state, completion, and fail-safe cleanup. Callers may choose documented
operation parameters, and wrappers may translate those choices, but they must
not invent or override native safety policy. Raw API clients and wrapper clients
must receive equivalent guarantees.

When responsibility is unclear, decide and document the owner before adding
another timer, lease calculation, retry loop, or cleanup path.

## Design for the embedded platform that actually ships

Do not assume a general-purpose library behaves like its desktop counterpart.
Verify supported behavior in the Defold version used by the extension and keep
platform constraints behind a shared adapter.

One current transport constraint is that Defold's embedded DLIB HTTP server has
reason phrases only for `200`, `302`, `404`, and `500`. All responses must pass
through `SendResponse()`, which maps unsupported success statuses to `200` and
unsupported error statuses to `500`, while the JSON error envelope preserves the
logical status in `error.status`. A compatibility adapter must never turn a
failure into a successful transport response. Do not call
`dmWebServer::SetStatusCode()` directly or remove the mapping until every
supported Defold target is verified and covered by integration tests.

## Evolve protocols explicitly

Never rely on an older endpoint rejecting an unknown field; it may ignore the
field and return a misleading success. Any additive change that alters behavior
must have a capability or protocol version. Clients should negotiate that
version before sending the new behavior and fail clearly when it is unavailable.

Preserve compatibility for operations whose semantics did not change.

## Reject malformed input instead of applying defaults

Treat an absent optional value differently from a supplied value that failed to
parse. Check typed-parser results whenever a field is present. Invalid values
must produce a structured error and must not silently become zero, false, an
empty value, or another default.

Apply the same rule in native endpoints and public wrappers so failures occur as
early and consistently as possible.

## Keep the default test suite fast and deterministic

Do not turn a supported maximum duration into an equivalent wall-clock wait in
the normal suite. Check numeric boundaries with unit tests, exercise lifecycle
behavior with short integration cases, and reserve slow real-time boundaries or
platform experiments for explicit release checks.

Tests should assert observable contracts rather than untouched initial state or
timing accidents that external input and asynchronous rendering can change.

Run the dependency-free suite from the repository root:

```sh
PYTHONPATH=automation_bridge/automation-bridge-python python3 -m unittest tests.test_automation_bridge_api tests.test_tooling
```

CI runs the Python and tooling tests explicitly on Linux, macOS, and Windows
with Python 3.10 and 3.14, independently of the native Bob build. For a local run
that never contacts an editor:

```sh
PYTHONPATH=automation_bridge/automation-bridge-python python3 -m unittest tests.test_automation_bridge_api.EngineClientUnitTest tests.test_automation_bridge_api.EditorDiscoveryUnitTest tests.test_automation_bridge_api.EditorCompatibilityUnitTest tests.test_tooling
```

Before merging changes to the shared protocol, open this sample project in
Defold and run the complete suite with a required runtime:

```sh
AUTOMATION_BRIDGE_REQUIRE_RUNTIME=1 PYTHONPATH=automation_bridge/automation-bridge-python python3 -m unittest tests.test_automation_bridge_api tests.test_tooling
```

The suite reuses the editor, builds the sample, and closes engines it built.
`AUTOMATION_BRIDGE_ENGINE_PORT` can select an already built sample engine; that
process is borrowed and remains running after teardown. Editor-specific tests
still need the editor workflow. The ordinary command skips runtime tests when
Defold is absent; `AUTOMATION_BRIDGE_REQUIRE_RUNTIME=1` turns unavailable runtime
setup into a failure. It never launches the editor implicitly.

Concurrent native validation projects need different available
`[profiler] remotery_port` settings in `game.project`. Start a new engine process
after changing that setting. An occupied Remotery port causes profiler startup
to fail and can freeze the tested 1.13.1 and 1.13.2 alpha engines on in-process
reboot. The wrapper reports missing profiler metadata instead of reading
another game's default port. See the
[Python profiler guidance](automation_bridge/automation-bridge-python/README.md#profiling)
for connection setup and the verified workaround, and `tests/MCP_VALIDATION.md`
for the investigation. Keep runtime tests enabled when validating this behavior.

Editor compatibility tests serve the versioned fixtures in `tests/fixtures`
through local HTTP servers. They cover 1.13.1 command enums and acknowledgements,
1.13.2 command paths and focus, compile/run, structured completion and errors,
Bob authentication, and target URLs with console fallback. Feature availability
comes from advertised capabilities, including when an early alpha lacks an API.

Runtime checks cover pagination metadata and malformed values, built/borrowed
clients, cancellation release, competing client/session identities, native lease
expiry, and application contracts. The sample enables `test_contracts = 1` to
load the bounded Lua contract fixtures in `tests/application_contracts.lua`;
dependent projects do not need this test setting. Use short leases or explicitly
cancel held input instead of waiting out a maximum-duration operation.

Merge/gesture tests use the fixture's `sample.arrange_items` command before
selecting targets. It separates and stops existing items outside the spawn
button's hit area; random placement can otherwise turn a drag into another spawn
or place targets on top of each other. Input delivery still uses native receipts
and the normal gameplay merge path.

## MCP plugin validation

Edit the canonical wrapper and MCP modules, then regenerate the bundled copy:

```sh
python3 plugins/automation-bridge/scripts/sync_python.py
python3 plugins/automation-bridge/scripts/validate_plugin.py --check-sync
PYTHONPATH=automation_bridge/automation-bridge-python python3 -m unittest tests.test_mcp_server tests.test_mcp_improvements
```

CI runs both MCP suites on the same Python/OS matrix as the shared wrapper.
Coverage includes operation inventory, JSON schemas, image content, independent
logical sessions, busy handles, cancellation effects, cleanup errors, and an
installed layout with spaces launched from an unrelated working directory.

For live independent-client coverage, install the official TypeScript MCP SDK in
a temporary directory and point `MCP_SDK_ROOT` at that package directory:

```sh
MCP_SDK_ROOT=/absolute/path/to/node_modules/@modelcontextprotocol/sdk \
  node tests/mcp_sdk_smoke.mjs /absolute/path/to/plugin /absolute/path/to/sample /tmp/mcp-screenshot.png
```

Run against open 1.13.1 and 1.13.2 editors separately. The test builds and owns a
sample engine, checks editor negotiation, structured output with the SDK's schema
validator, screenshot/preview image blocks, pagination, borrowed-client cleanup,
competing sessions, cancellation followed by an empty native input queue, and
reconnection after restarting the MCP server. It closes its engine.
The native lease remains authoritative; cleanup of an idle observer must not
acquire one. Native controller contention is distinct from MCP `handle_busy`.

Validate the installed Codex cache as well as the source launcher. Codex 0.153
only reads the first `tools/list` page, so return the complete focused-tool list;
use paginated summaries for the much larger Python operation catalog.

After installing the plugin, check discovery through Codex's actual MCP client:

```sh
PYTHONPATH=automation_bridge/automation-bridge-python python3 -m tests.mcp_codex_smoke
```

This starts a temporary Codex app-server without creating a task or invoking a
model. Visible rendering inside a host still needs a fresh task with the plugin
loaded. See `tests/MCP_VALIDATION.md` for recorded host/version coverage and
remaining release checks.
