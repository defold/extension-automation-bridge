# Public HID Device Availability Query

## Summary

Add public Defold extension SDK functions that report whether a mouse, keyboard, or touch device handle is currently connected and usable for injection. Automation Bridge needs this to advertise `input.device.touch` from runtime state instead of inferring support from the target platform.

## Problem

The public `dmsdk/hid/hid.h` API exposes `GetMouse()`, `GetKeyboard()`, and `GetTouchDevice()`. These return a handle for an in-range slot, not necessarily a connected device. Public mutation APIs such as `SetMouseButton()`, `SetMousePosition()`, `SetKey()`, and `AddTouch()` silently ignore disconnected handles in the engine implementation.

There is no public `IsConnected()` or capability query for these handles. Internal HID structures contain connection state, but extensions must not cast public opaque handles to private engine layouts. Doing so would create ABI coupling and make capability reporting dependent on unsupported implementation details.

Without a query, an extension has three bad choices:

- advertise touch on every platform even when injection is a silent no-op;
- reject touch everywhere despite a usable native touch device;
- infer availability from compile-time platform macros.

Automation Bridge currently chooses the last, conservative option: mouse is used by `auto` on desktop and touch is exposed on iOS, Android, and Switch. That avoids false desktop support but cannot represent unusual devices, headless contexts, platform changes, or future desktop touch support.

## Goals

- Let native extensions test whether a public HID handle is connected now.
- Keep the query safe for invalid handles.
- Support capability negotiation without mutating input state.
- Use the same connection state that existing HID injection functions already check.

## Non-goals

- Enumerating operating-system device metadata.
- Reporting application input bindings or whether Lua consumes an action.
- Adding synthetic input injection; public injection functions already exist.
- Promising that a connected device has a particular number of buttons or contacts.

## Proposed public API

Add overloads to `engine/hid/src/dmsdk/hid/hid.h`:

```cpp
namespace dmHID
{
    bool IsConnected(HMouse mouse);
    bool IsConnected(HKeyboard keyboard);
    bool IsConnected(HTouchDevice touch_device);
}
```

Each function returns `false` for an invalid/null handle and otherwise returns the device slot's current connection state. The name intentionally describes availability of the HID device, not whether the host has physical hardware or whether application bindings consume its events.

If overloads are undesirable for generated documentation, equivalent explicit names are acceptable:

```cpp
bool IsMouseConnected(HMouse mouse);
bool IsKeyboardConnected(HKeyboard keyboard);
bool IsTouchDeviceConnected(HTouchDevice touch_device);
```

## Engine implementation

Implement the functions in `engine/hid/src/hid.cpp`, adjacent to the public handle accessors. The implementation can read the existing internal `m_Connected` flag because it lives inside the HID module; the flag must remain hidden from extensions.

Illustrative implementation:

```cpp
bool IsConnected(HTouchDevice device)
{
    return device != INVALID_TOUCH_DEVICE_HANDLE && device->m_Connected;
}
```

Mouse and keyboard variants follow the same null-safe rule. If those internal device types do not currently maintain connection state consistently on every adapter, normalize their state during HID initialization and platform connect/disconnect callbacks before exposing the query.

No private struct declarations or ABI changes are required in extension code. Adding exported functions requires updating the SDK symbol/export lists for all supported platforms in the same way as other `dmHID` public functions.

## Automation Bridge integration

After the minimum supported Defold version contains the query:

1. Fetch device slot zero from the bridge's `dmHID::HContext`.
2. Advertise `input.device.mouse` only when `IsConnected(mouse)` is true.
3. Advertise `input.device.touch` only when `IsConnected(touch)` is true.
4. Resolve `device=auto` to touch when touch is connected on a native touch platform, otherwise mouse when mouse is connected.
5. Reject an explicit unavailable device with `input_device_unsupported` before accepting the input receipt.
6. Re-check availability when an accepted input starts. If the device disconnected while queued, finish its receipt as `failed` with `input_device_unavailable`.

Capability reporting should be recomputed for `/health` and `/input/configure`, not cached for the entire engine instance, because devices may disconnect.

## Tests

Add HID unit tests for every overload:

- invalid handle returns false;
- initialized disconnected slot returns false;
- connected slot returns true;
- disconnect changes true to false;
- querying does not modify packets, touch counts, or connection state.

Add Automation Bridge tests with injected/fake HID state:

- `/health` capabilities follow connection state;
- `auto` chooses one available device and never injects both;
- explicit unavailable device is rejected before queueing;
- disconnect between acceptance and start produces a failed receipt and does not block later FIFO input.

## Compatibility and rollout

This is an additive SDK API. Existing games and extensions are unaffected. Automation Bridge should retain compile-time platform gating while supporting older Defold versions, then switch to the runtime query once its declared `defold_min_version` reaches the release containing the new functions.
