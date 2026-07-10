# Defold Public APIs for Automation Lifecycle and Headless Debug Services

## Summary

Automation Bridge can negotiate capabilities and operate when individual runtime
contexts are missing, but a true `DM_HEADLESS` engine cannot currently expose the
bridge using only supported Defold extension APIs. Defold should expose a debug
service transport in headless builds, stable process/build identity, and structured
editor-to-runtime lifecycle notifications.

This proposal deliberately avoids private engine symbols. It describes the minimum
public APIs needed for Automation Bridge and other diagnostic extensions.

## Problem

The current public DMSDK has these gaps:

- `dmEngine::GetWebServer()` is documented as valid only in debug builds. Headless
  builds use the null engine-service implementation, so an extension has no supported
  HTTP transport even when scene/state/command diagnostics do not need graphics.
- `dmTime::GetTime()` and `dmTime::GetMonotonicTime()` are public, but DMSDK has no
  portable process id or opaque engine-start identity.
- `dmConfigFile` exposes project settings, but there is no public project build/content
  digest. Hashing title/version/bootstrap settings distinguishes projects without
  exposing them, but it cannot prove two bundles contain the same resources.
- The editor `/command/build` response reports build success, while runtime
  registration and shutdown are only observable indirectly through console lines and
  transient engine-service ports.
- Render callbacks do not form a lifecycle contract for a headless application. An
  extension needs an explicit application-ready signal independent of a window or
  rendered frame.

## Proposed Engine APIs

### Debug service transport in headless builds

Make `dmEngine::GetWebServer(dmExtension::AppParams*)` valid for both `DM_DEBUG` and
`DM_HEADLESS`, backed by the normal loopback engine service or a documented equivalent.
The service must be opt-in, loopback-only by default, and retain existing debug-build
security expectations.

If enabling the complete engine service is undesirable, expose a narrower public
debug route registry:

```cpp
namespace dmEngine
{
    HDebugService GetDebugService(dmExtension::AppParams* params);
    DebugServiceResult AddDebugHandler(HDebugService, const char* path,
                                       FDebugHandler handler, void* user_data);
}
```

The handle must be available before extension `AppInitialize` returns and remain valid
until `AppFinalize`. It must not require graphics, a platform window, HID, or render
callbacks.

### Opaque engine and build identities

Add public, non-secret identity accessors:

```cpp
namespace dmEngine
{
    // New for each OS process start; stable across the process lifetime.
    const char* GetInstanceId(dmExtension::AppParams* params);

    // Digest of the bundled project/resource manifest, not a source path.
    const char* GetBuildId(dmExtension::AppParams* params);
}

namespace dmSys
{
    // Returns false on platforms where a process id has no useful meaning.
    bool GetProcessId(uint64_t* process_id);
}
```

`GetInstanceId()` should not encode a host name, executable path, user name, signing
identity, or other secret. `GetBuildId()` should use a canonical resource-manifest or
bundle digest and remain identical for identical build output.

### Runtime lifecycle notification

Expose application lifecycle stages independently of rendering:

```cpp
enum AppLifecycleStage
{
    APP_LIFECYCLE_INITIALIZING,
    APP_LIFECYCLE_COLLECTION_LOADED,
    APP_LIFECYCLE_READY,
    APP_LIFECYCLE_CLOSING,
    APP_LIFECYCLE_CLOSED,
};

typedef void (*FAppLifecycleCallback)(AppLifecycleStage stage,
                                      uint64_t monotonic_time_us,
                                      void* user_data);
```

`READY` should mean that the bootstrap collection has completed initial loading and
the update loop can service application messages. It must work with null graphics and
null HID backends. Extensions need either a query for the latest stage or replay of the
current stage when registering late.

## Proposed Editor API

Add a structured build/run operation resource rather than requiring console parsing:

```json
{
  "operation_id": "build-42",
  "state": "running",
  "events": [
    {"stage":"editor_build_started"},
    {"stage":"editor_build_completed"},
    {"stage":"previous_engine_closing","engine_instance_id":"..."},
    {"stage":"previous_engine_closed","engine_instance_id":"..."},
    {"stage":"new_engine_registered","engine_instance_id":"...","service_port":51337},
    {"stage":"bridge_healthy"},
    {"stage":"initial_scene_ready"}
  ]
}
```

Suggested endpoints are `POST /command/build` returning `operation_id` and
`GET /operations/<id>` for polling. Failure events must carry structured issues. Port,
instance id, and build id must be supplied by engine registration rather than parsed
from console text.

## Automation Bridge Behavior After These APIs Exist

A headless build will advertise only capabilities backed by present contexts, for
example `runtime.health`, `runtime.lifecycle`, `diagnostics.metadata`, `scene`,
`application.events`, `application.state`, `application.commands`, and tracing. It will
omit `screen`, `screen.resize`, `screenshot`, and `input.*` when graphics/window/HID are
absent. Calling an omitted endpoint returns `501 unsupported_capability`, not a generic
runtime failure.

## Compatibility and Security

- All APIs are debug/headless-development only and must be compiled out of release
  builds unless a project explicitly opts in.
- Debug services remain loopback-only unless Defold's existing service configuration
  explicitly allows remote access.
- Identities are opaque and contain no source paths or raw project settings.
- Existing extension callbacks and editor responses remain compatible.

## Test Plan

- Build graphical debug and headless targets on macOS, Linux, and Windows.
- Verify the headless service registers without graphics, window, or HID contexts.
- Assert capability sets for graphical, null-graphics, null-HID, and fully headless
  backends.
- Assert instance ids are stable during one process and change after process restart.
- Assert build ids match identical bundles and change after a resource change.
- Assert ordered editor lifecycle events on success, compile failure, engine shutdown
  timeout, registration failure, and initial collection load failure.
- Verify omitted capabilities return `unsupported_capability` with the absent backend
  context named in the diagnostic.

## Current Safe Fallback

Until these APIs exist, Automation Bridge uses public wall/monotonic clocks, an opaque
per-application-start id, and hashes of non-secret project configuration. Desktop and
mobile process ids use public platform APIs; unsupported platforms return `null`.
Runtime capabilities are derived from available graphics, HID, and game-object
contexts. The extension intentionally remains disabled in `DM_HEADLESS`, because
calling a debug-only web-server API or private engine-service symbols there would not
be a supported implementation.
