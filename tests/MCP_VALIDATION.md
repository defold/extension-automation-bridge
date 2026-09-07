# MCP validation — 2026-09-07

Local validation used macOS 26.6.2, Python 3.14.0, Codex 0.153.4, and the
official TypeScript MCP SDK 1.30.0. Defold installations were 1.13.1 stable
(`574678c7d44be490d874fbed2d0ae6211feec4d9`) and 1.13.2 alpha
(`dd22b1e40a8f4837578c14d635f6d91a98b6ec9f`). The alpha ran an isolated copy of
the sample project. Engines built by validation were closed afterward.

| Check | Result |
| --- | --- |
| Shared Python, tooling, and MCP deterministic suites, including committed review fixes | 313 tests passed |
| Full native sample contracts, 1.13.2 alpha | 16 tests passed |
| Native sample contracts, 1.13.1 stable | 15 passed, 1 expected skip |
| Independent SDK, 1.13.1 and 1.13.2 | Complete MCP workflow passed on both |
| Independent SDK against the installed Codex cache | Complete MCP workflow passed on both editor versions |
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

The two earlier native failures have been investigated. Their trigger and the
remaining engine limitation are documented below. The complete native suites
passed in their ordinary order with an available profiler port assigned to each
sample. Neither test was disabled or reordered. The compile/Bob test is the one
expected 1.13.1 skip because that editor does not advertise those capabilities.

`mcp_codex_smoke.py` verifies installed-plugin discovery through a temporary
Codex app-server without creating a task or invoking a model. Interactive tool
invocation and image rendering inside a fresh Codex task remain a manual host
check. The independent SDK exercises tool invocation and image content; it does
not establish another product's UI behavior.

CI is configured for Linux, macOS, and Windows with Python 3.10 and 3.14. Only
the local macOS/Python 3.14 run is claimed here. The stable 1.13.2 release and
other host UIs were not available in this validation run.

Reproduction commands and installed-layout guidance are in `DEVELOPMENT.md`.

## Profiler investigation

Both earlier failures involved a second game occupying Remotery's default port,
17815. A controlled local reservation of that port reproduced the reboot freeze
in 1.13.1. The same failure also reproduced in 1.13.2 alpha. Without a competing
listener, the original two 1.13.1 tests passed together.

The wrapper had three discovery problems:

- Remotery can initialize after Automation Bridge registers its endpoint. The
  parser previously searched only the preceding console lines.
- A 1.13.2 structured launch response can arrive before the editor displays the
  engine's startup logs. Optional profiler discovery now polls briefly for
  matching startup metadata, without making it a condition of engine readiness.
- A failed profiler startup could retain an earlier engine's cached URL. When
  metadata was missing, the profiler helper silently tried port 17815. This
  could read another game's counters, explaining the missing sample `Sprite`
  counter. New engine registrations now clear stale profiler metadata, and
  engine-scoped stream helpers require a discovered URL or an explicit override.

Five deterministic regressions cover registration order, delayed console
delivery, absent metadata and cancellation, stale cache replacement, and
explicit stream targeting. Both native suites exercised all 19 sprite-count
observations after the fixes; each matched the current sample scene.

The native engine still has a separate reboot failure after profiler startup
fails. The observed sequence was: another listener occupies the configured
Remotery port; a new sample engine reports `Failed to initialize Remotery: 5`;
its bridge initially responds; an editor-triggered in-process reboot returns a
successful build result, then the engine and its health endpoint stop responding.
The wrapper now reports the absent profiler instead of connecting to the port's
other owner.

On both engine versions, a one-second macOS process sample showed the main
thread waiting for the profiler mutex in `ProfileScopeBegin`, while an HTTP
thread repeatedly ran `ProfileSetThreadName` and the basic profiler's
`SetThreadName` callback. Other HTTP threads waited for the same mutex. The
Defold source identifies error 5 as `RMT_ERROR_RESOURCE_ACCESS_FAIL`; its socket
setup returns this error when bind/listen fails. The precise listener-state
defect remains upstream engine work; no native fix is claimed here.

The verified workaround is to assign each concurrent game an available
`[profiler] remotery_port` and start a new engine process. An in-process reboot
retains the profiler listener. With a dedicated port, discovery, native counters,
and in-process reboot all passed on 1.13.1 and 1.13.2 alpha while port 17815 was
occupied. Validation restored both project configurations and closed its owned
engines afterward.

The final installed development cache is `0.1.0+codex.20260907212807`. The plugin
and MCP API have not been released; these checks target the current first-release
implementation and do not imply compatibility with earlier development builds.
