# MCP validation — 2026-09-07

Local validation used macOS 26.6.2, Python 3.14.0, Codex 0.153.4, and the
official TypeScript MCP SDK 1.30.0. Defold installations were 1.13.1 stable
(`574678c7d44be490d874fbed2d0ae6211feec4d9`) and 1.13.2 alpha
(`dd22b1e40a8f4837578c14d635f6d91a98b6ec9f`). The alpha ran an isolated copy of
the sample project. Engines built by validation were closed afterward.

| Check | Result |
| --- | --- |
| Shared Python, tooling, and MCP deterministic suites | 291 tests passed |
| Full native sample contracts, 1.13.2 alpha | 16 tests passed |
| Native sample contracts, 1.13.1 stable | 13 passed, 2 failed, 1 expected skip; details below |
| Independent SDK, 1.13.1 and 1.13.2 | Complete MCP workflow passed on both |
| Independent SDK against the installed Codex cache, 1.13.1 | Complete MCP workflow passed |
| Codex app-server using its own MCP client | All 37 focused tools discovered from the installed plugin |
| On-demand Python catalog schemas | All 215 operations described and JSON-encoded successfully |
| Installed cache vs. plugin source | All 42 packaged files match, excluding generated Python caches |
| Repository plugin validator, Codex plugin validator, skill validator | Passed |

The independent SDK workflow in `mcp_sdk_smoke.mjs` checks negotiated editor
commands, compilation and Bob where supported, explicit 1.13.2 requirements on
legacy editors, structured build results, paginated elements, game command
discovery, PNG screenshot and preview content, independent snapshot frames,
borrowed-client cleanup, native input contention across logical sessions,
cooperative cancellation with a cancelled receipt and empty input queue, and
reconnection with new handles after an MCP server restart. A returned screenshot
was also inspected visually. The SDK validates the declared tool output schemas.

Real host testing exposed three regressions covered by deterministic tests:

- Codex reads only the first `tools/list` page. That response now contains every
  focused tool; the larger operation catalog keeps its small summary pages.
- Flushing an idle observer acquired a native input lease. Cleanup now checks
  for input belonging to that client before flushing.
- Publishing a response before releasing Python resources could reject an
  immediate follow-up as busy. Request finalization now precedes the response.

## Remaining validation limits

The 1.13.1 native failures are outside the MCP call path:

- `test_editor_reboot_preserves_automation_bridge_endpoint`: after the editor's
  in-process engine reboot, the existing Automation Bridge health endpoint times
  out. This also reproduces when running that test alone through the shared
  Python API, without an MCP server.
- `test_remotery_sprite_counter_after_actions`: the final native run could not
  find the `Sprite` counter. Other editor/engine instances were running on the
  machine; the cause has not been isolated. This is not recorded as a pass.

The final 1.13.1 native run placed reboot last to prevent its dead endpoint from
obscuring other tests. The compile/Bob test skipped because 1.13.1 does not
advertise those capabilities. Neither failure has been disabled or hidden in
the committed suite.

`mcp_codex_smoke.py` verifies installed-plugin discovery through a temporary
Codex app-server without creating a task or invoking a model. Interactive tool
invocation and image rendering inside a fresh Codex task remain a manual host
check. The independent SDK exercises tool invocation and image content; it does
not establish another product's UI behavior.

CI is configured for Linux, macOS, and Windows with Python 3.10 and 3.14. Only
the local macOS/Python 3.14 run is claimed here. The stable 1.13.2 release and
other host UIs were not available in this validation run.

Reproduction commands and installed-layout guidance are in `DEVELOPMENT.md`.
