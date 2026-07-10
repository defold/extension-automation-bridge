# Input action provenance for injected HID events

## Brief

Automation Bridge assigns an id to each accepted native input action and can report when its down, move, and release events are injected. An opt-in Defold application may then call `automation_bridge.ack(input_id, result)` to report a semantic outcome such as accepted, blocked, or intercepted.

The public `dmHID` mutation API carries device state only. `dmHID::SetMouseButton`, `dmHID::SetMousePosition`, `dmHID::AddTouch`, `dmHID::SetKey`, and `dmHID::AddKeyboardChar` do not accept provenance. The public `dmGameObject::InputAction` and the Lua action table therefore cannot identify which Automation Bridge action changed that state. Guessing from coordinates or frame timing becomes ambiguous when physical and injected devices overlap.

This document proposes a generic debug provenance tag that can travel with injected HID transitions. It is an engine API proposal, not a private API dependency of Automation Bridge.

## Goals

- Let a native extension associate an opaque 64-bit tag with an injected mouse, touch, key, or text transition.
- Deliver that tag with the corresponding `dmGameObject::InputAction` and Lua `on_input` action.
- Preserve the distinction between native delivery and application acknowledgement.
- Keep untagged physical input and existing extensions source-compatible.
- Work consistently on desktop and mobile input backends in debug and release engine builds, even though Automation Bridge itself remains debug-only.

## Non-goals

- Report which script or component consumed an action.
- Define Automation Bridge input ids in Defold core.
- Change focus-stack ordering, action binding, or `on_input` return semantics.
- Preempt or roll back application input handling.
- Add arbitrary application metadata to every HID packet.

## Background

Automation Bridge currently injects state through the public DMSDK HID functions. The engine later samples the devices, resolves input bindings, creates `dmGameObject::InputAction` values, and dispatches them through the input focus stack. The generated Lua action exposes coordinates, pressed/released flags, touch data, text, and device-specific values, but no origin tag.

An application can acknowledge an id it learned through its own test protocol, but automatic correlation is not reliable without provenance surviving the HID-to-action conversion.

## Requirements

- A tag value of zero means no provenance and matches current behavior.
- Tags are copied, never retained through a caller-owned pointer.
- A tag applies to one transition/sample, not indefinitely to a device.
- Coalescing must define which tag wins or whether mixed tagged input clears provenance.
- Touch contacts retain independent tags by touch id.
- The field must be available to native input callbacks and Lua scripts.
- Release and cancellation must retain the same tag as the corresponding press where the injector supplies it.
- No allocations are permitted per sampled input action.

## User workflow / API

One possible DMSDK surface is additive tagged variants:

```cpp
namespace dmHID
{
    struct InputProvenance
    {
        uint64_t m_Tag;
    };

    void SetMouseButtonTagged(HMouse mouse, MouseButton button, bool value, InputProvenance provenance);
    void SetMousePositionTagged(HMouse mouse, int32_t x, int32_t y, InputProvenance provenance);
    void AddTouchTagged(HTouchDevice device, int32_t x, int32_t y, int32_t id, Phase phase, InputProvenance provenance);
    void SetKeyTagged(HKeyboard keyboard, Key key, bool value, InputProvenance provenance);
    void AddKeyboardCharTagged(HContext context, int codepoint, InputProvenance provenance);
}
```

`dmGameObject::InputAction` would expose the copied tag as `m_ProvenanceTag`. Lua would expose it only when non-zero:

```lua
function on_input(self, action_id, action)
    if action.provenance_tag then
        automation_bridge.ack(action.provenance_tag, {
            accepted = not self.input_locked,
            reason = self.input_locked and "input_locked" or nil,
        })
    end
end
```

The name is intentionally generic. Defold core does not know that the value may be an Automation Bridge input id.

## Technical design

### HID ownership and lifetime

Each mutable device stores provenance alongside the pending state transition. The value is copied into the sampled packet and cleared after the packet is consumed. Mouse position and button transitions need separate tags until the engine combines them into an action. Touch provenance belongs to each contact. Keyboard character provenance belongs to each buffered codepoint.

### Engine input conversion

The engine input sampling path copies packet provenance into the generated `dmGameObject::InputAction`. When multiple source packets are folded into one bound action, the action keeps the tag only if all contributing non-zero tags agree. Otherwise it clears the tag to zero to avoid false correlation.

The `InputAction` ABI size guard must be updated deliberately. If preserving the struct size is required, an existing reserved field or a sidecar array indexed with the engine input buffer should be considered instead; source inspection found no documented reserved public field.

### Lua dispatch

`comp_script.cpp` adds `provenance_tag` to the action table when the value is non-zero. Lua numbers exactly represent integer tags only through `2^53`; either constrain tags to that range or expose a decimal string. Automation Bridge already keeps ids within the exact integer range and can use a Lua number.

### Threading and update order

Tagged setters follow the same threading contract as their untagged counterparts. Provenance is sampled and cleared in the same update step as device state, so no cross-frame pointer lifetime is introduced.

### Hot reload and reboot

Tags are transient packet state. They are cleared with the HID device state on engine reboot and do not participate in hot reload or live update.

## Alternatives

### Option 1: Automation Bridge global `active_input_id()`

Keep the current id in extension-global state and let Lua query it from `on_input`.

Pros:

- Requires no Defold engine change.
- Small implementation.

Cons:

- Ambiguous when physical input, multiple devices, or coalesced transitions are dispatched in the same frame.
- Depends on extension and input-dispatch update order.
- Does not provide provenance to other native components.

### Option 2: Encode the id in an unused device field

Pros:

- No public API change.

Cons:

- Private, platform-dependent, and likely to conflict with legitimate input data.
- Violates the requirement not to invent or depend on private Defold APIs.

### Option 3: Application command before every input

Publish the next input id to the application before injection.

Pros:

- Works with existing public APIs in controlled single-client tests.

Cons:

- Still relies on timing and ordering rather than packet identity.
- Cannot distinguish physical input and complicates cancellation/flush behavior.

The tagged HID proposal is the most reliable general solution. Option 1 may be an explicitly documented temporary approximation for FIFO, single-controller automation.

## Compatibility and migration

Existing untagged functions remain unchanged and produce a zero tag. Existing Lua scripts see no new field for ordinary input. Native extensions must be rebuilt when the DMSDK `InputAction` layout changes; preserving binary compatibility needs a separate ABI decision.

No resource formats, editor data, Bob outputs, or project migrations change.

## Testing

- HID unit tests for one-shot tag lifetime on mouse, touch, key, and text input.
- Engine input tests verifying tag propagation through binding and `dmGameObject::DispatchInput`.
- Coalescing tests for equal tags, conflicting tags, and tagged plus physical input.
- Script component tests verifying `action.provenance_tag` presence and absence.
- Automation Bridge integration test covering accepted, released, and acknowledged as three distinct stages.
- Multi-device test proving touch contacts retain independent tags.
- Reboot/cancellation test proving stale tags cannot leak into a later input.

## Open questions

- Q: Should provenance be a numeric tag, a small `{source, tag}` pair, or a private engine sidecar exposed through a query API?
- Q: Can `dmGameObject::InputAction` grow without violating supported extension ABI guarantees?
- Q: What is the correct provenance rule when position and button packets carry different tags?
- Q: Should Lua receive a number constrained to `2^53`, a decimal string, or both?
- Q: Should tagged functions be public in all builds or compiled only when engine debug features are enabled?
