# Automation Bridge: Exact Window and Application-Audio Capture Contract

## Brief

The optional Python recorder can capture a display or an explicit content rectangle with FFmpeg. On Windows, FFmpeg can also select a window by title. It cannot portably and reliably identify the running Defold OS window, translate Defold content coordinates into global display coordinates, exclude decorations, or isolate the Defold process audio on every desktop platform.

Exact application-window capture therefore needs a small native/platform contract. This document proposes the data Automation Bridge needs from Defold and defines ownership between the engine, bridge, and optional platform recorder. It does not propose coupling video encoding to the engine.

## Background and motivation

`dmPlatform::HWindow` is an opaque handle in the public platform API. The Defold source tree has platform-specific internal helpers:

- `GetOSXNSWindow(HWindow)` and `GetOSXNSView(HWindow)` in `engine/platform/src/platform_window_osx.h`;
- `GetX11Window(HWindow)` in `engine/platform/src/platform_window_linux.h`;
- `GetWindowsHWND(HWindow)` in `engine/platform/src/platform_window_win32.h`.

Those helpers are not a portable DMSDK API. The public window API exposes dimensions, display scale, and input-related operations, but not a stable external window identifier or screen-space frame/content rectangles.

The sound mixer similarly exposes mixer controls through `engine/sound/src/dmsdk/sound/sound.h`, but no supported post-master PCM tap. Platform screen-capture APIs may supply process audio on some systems, but FFmpeg device capture cannot guarantee that it records only the target Defold process.

Without these contracts, a companion can only ask callers for an explicit rectangle/title or use brittle process/window enumeration. That is unsuitable for unattended automation and can record the wrong window or audio source.

## Goals

- Identify the current desktop Defold window without matching a mutable title.
- Report outer-window, client/content, viewport, and backbuffer rectangles with named coordinate spaces.
- Make title-bar exclusion and display-scale conversion explicit.
- Allow a platform recorder to request process-isolated application audio where the OS supports it.
- Report capabilities and permission failures without silently falling back to display capture or microphone audio.
- Keep encoding, muxing, and recorder dependencies out of the core engine.

## Non-goals

- A cross-platform video encoder in Defold.
- Guaranteed process-audio capture on platforms whose public API does not provide it.
- Microphone recording.
- Inferring application cameras, GUI layouts, or world-coordinate transforms.
- Production-game APIs; the intended consumer is a debug automation extension.

## Requirements

- The API must be capability-gated per platform and window backend.
- Native window handles must never be persisted across an engine restart.
- Every identity response must include the engine instance id and a generation that changes when the platform window is recreated.
- Rectangles must name their coordinate space and pixel/logical unit.
- Permission denial and unsupported operation must be distinct results.
- Audio samples must be timestamped on a monotonic clock suitable for video correlation.
- No capture callback may block the engine mix/update thread.
- Headless/null-window builds must return unsupported capabilities, not fake geometry.

## Proposed API

Automation Bridge would expose a debug-only endpoint backed by a narrow engine/DMSDK contract:

```json
{
  "window_generation": 3,
  "platform": "arm64-macos",
  "external_identity": {
    "kind": "cg_window_id",
    "value": 4187
  },
  "rectangles": {
    "window_display_points": {"x": 120, "y": 80, "width": 1112, "height": 1982},
    "content_display_points": {"x": 120, "y": 108, "width": 556, "height": 977},
    "content_display_pixels": {"x": 240, "y": 216, "width": 1112, "height": 1954},
    "viewport_content_pixels": {"x": 16, "y": 0, "width": 1080, "height": 1920},
    "backbuffer_pixels": {"x": 0, "y": 0, "width": 1112, "height": 1920}
  },
  "display_scale": 2.0,
  "capabilities": ["window.external_identity", "window.global_rectangles"]
}
```

The external identity kind is platform-specific (`cg_window_id`, `x11_window`, or `hwnd`) and is valid only for the reported engine instance/window generation. The HTTP bridge serializes it for a same-machine companion; it is not a general gameplay API.

For audio, prefer an engine-independent platform recorder first (ScreenCaptureKit, PipeWire portal, or a Windows process-loopback API). If a platform cannot isolate process audio, a separate opt-in debug PCM tap may be considered:

```cpp
typedef void (*FPostMasterMixCallback)(const float* interleaved,
                                       uint32_t frame_count,
                                       uint32_t channels,
                                       uint32_t sample_rate,
                                       uint64_t monotonic_time_ns,
                                       void* user_data);

Result RegisterPostMasterMixCallback(FPostMasterMixCallback callback,
                                     void* user_data);
Result UnregisterPostMasterMixCallback(FPostMasterMixCallback callback,
                                       void* user_data);
```

This callback would be an internal/debug extension contract until its realtime behavior and lifetime rules are proven.

## Technical design

### Window identity and geometry

`dmPlatform` owns the platform window and computes global outer/content rectangles on the window thread. A new portable query copies a value struct; consumers never own the underlying `NSWindow`, X11 `Window`, or `HWND`.

The engine increments `window_generation` after every successful native window creation/recreation. Automation Bridge combines this with its engine instance id. The Python recorder must reject stale identity/geometry after either changes.

On macOS the implementation derives a `CGWindowID` and content-view frame from the `NSWindow`/`NSView`. On Windows it derives the client rectangle and maps it to screen coordinates from `HWND`. On X11 it resolves the client window geometry/root coordinates and reports whether decorations are owned by a reparenting window manager. Wayland requires a portal capture token rather than an X11-style global id/rectangle and advertises a different capability.

Calls touching UI objects must run on the platform/window thread or return cached data populated there. No Objective-C/Win32/X11 handle crosses into JSON directly except as a same-process-lifetime numeric identity.

### Audio

The preferred companion obtains application audio from the operating-system capture session so its timestamps share the video capture clock. The recorder reports `recording.application_audio` only after the platform session confirms process isolation.

If a mixer tap is implemented, the sound thread copies post-master float PCM into a bounded lock-free ring. A writer thread drains it. Overflow increments a dropped-frame counter; the mixer never waits for disk/network/encoder work. Registration is single-owner initially, explicitly removed before sound finalization, and automatically invalidated during engine reboot. The stream reports channel count, mix rate, first/last sample clock, and drops.

### Automation Bridge/Python ownership

The native bridge reports identity, geometry, capability versions, and timestamps. The optional Python backend owns permission prompts, capture sessions, codecs, scaling, file finalization, and metadata probing. The base Python client retains no dependency on FFmpeg or platform frameworks.

## Alternatives

### Option 1: Enumerate windows by process id/title in Python

Pros:

- No engine changes.
- Can be prototyped independently.

Cons:

- Titles are mutable and non-unique.
- Helper/editor windows can belong to the same process.
- Geometry and DPI conversions are race-prone.
- Wayland deliberately prevents global enumeration.

### Option 2: Record only the backbuffer

Pros:

- Exact pixels and deterministic dimensions.
- Avoids window permissions on some platforms.

Cons:

- Does not include platform/window presentation.
- Adds readback and encoding cost to the render path.
- Does not solve application-audio capture.

### Option 3: Expose platform handles directly in DMSDK

Pros:

- Native extensions can implement all platform behavior.

Cons:

- Leaks backend/platform types into public APIs.
- Handles have subtle thread and lifetime constraints.
- Does not define rectangles, scaling, recreation, or audio semantics.

Recommendation: expose a value-based identity/geometry query for debug tooling and keep platform capture in the optional companion. Add a PCM tap only for platforms where process audio cannot be obtained externally and the realtime cost is accepted.

## Compatibility

This is additive. Existing projects, bundles, and public Lua APIs are unchanged. Null/headless/mobile builds advertise no desktop-window capture capability. Platform-specific identities are optional response fields and must be ignored by clients that do not recognize the kind.

## Implementation plan

1. Add a platform value struct for external identity, generation, scale, and named rectangles.
2. Implement and unit-test macOS, Windows, and X11 queries; return unsupported for null/Wayland until a portal design exists.
3. Expose the value through an Automation Bridge capability-versioned endpoint.
4. Implement ScreenCaptureKit, PipeWire portal, and Windows capture companions independently.
5. Evaluate OS process-audio coverage before deciding whether a post-master mixer tap is necessary.

## Testing

- Platform tests compare reported content dimensions to `GetWindowWidth/Height` and verify global origins after moving the window.
- Tests recreate/resize the window and assert generation and rectangle changes.
- Retina/HiDPI and mixed-scale display tests verify point/pixel conversions.
- Capture tests place another similarly titled window onscreen and verify identity-based selection.
- Permission-denied tests must return a diagnostic without creating a partial success result.
- Audio tests verify process isolation, channels/rate, A/V clock drift, dropped-frame reporting, teardown, and engine reboot.
- Headless/null tests verify truthful unsupported capabilities.

## Open questions

- Q: Should external window identity remain an Automation Bridge-only internal API or become a supported DMSDK value query?
- Q: Which Wayland portal token/lifetime model fits editor-launched debug applications?
- Q: Do supported Windows versions provide acceptable process-loopback capture, or is a mixer tap required there?
- Q: Is post-master capture acceptable in shipping builds, or must it be compiled only into debug/dev builds?
- Q: Which monotonic clock is the canonical bridge/recorder correlation clock on each platform?
