# DMSDK: Stable automation identity, window geometry, transforms, and render receipts

Issue/Epic: Automation Bridge improvement backlog items 12, 14, 19, and 28

## Brief

Debug automation extensions can currently inspect Defold's scene traversal and
drawable geometry, but public DMSDK APIs do not expose enough information to
describe every requested coordinate space or correlate a readback with an
engine-owned render-frame identity.

Automation Bridge can safely expose drawable window size, configured graphics
size, viewport, display scale, game object identifier, and game object
generation using existing public APIs. It must not infer monitor bounds,
outer-window decorations, application camera transforms, GUI layout transforms,
collection generations, or an engine render-frame number.

This document proposes public, read-only DMSDK surfaces for those missing
facts. The bridge remains functional without them and capability-gates the
additional fields.

## Goals

- Let native extensions report client, outer-window, monitor, drawable, and
  viewport rectangles without platform-private code.
- Let extensions convert points through engine-owned camera and GUI layout
  transforms without reproducing Defold's projection/layout rules.
- Give a post-render capture an engine-owned render-frame receipt.
- Provide a globally qualified, generation-safe scene identity for collections
  and game objects.
- Keep all APIs read-only, cross-platform where the backend has the data, and
  explicitly unavailable where it does not.

## Non-goals

- A new automation or screenshot service in the engine.
- Arbitrary Lua evaluation or application semantic identifiers.
- OCR, template matching, image scaling, or recording.
- Guessing transforms for custom render scripts.

## Background

The current bridge uses these verified public APIs:

- `dmGraphics::GetWindowWidth()` / `GetWindowHeight()` for drawable window
  pixels;
- `dmGraphics::GetWidth()` / `GetHeight()` for configured graphics size;
- `dmGraphics::GetViewport()` for the current graphics viewport;
- `dmGraphics::GetDisplayScaleFactor()` for backing pixels per display point;
- `dmGameObject::SceneNode::m_Instance`, `GetIdentifier()`, and
  `GetGeneration()` for instance identity.

The bridge reports its own extension update counter as `engine_frame`. This is
useful for ordering bridge observations, but it is not proof that a particular
graphics submission was presented. Screenshot capture runs from the extension
post-render callback and atomically publishes the completed PNG.

`GetIdentifier()` plus `GetGeneration()` distinguishes a reused game object
slot inside its owning collection. Public traversal does not provide a stable,
generational collection handle that can qualify this tuple across collections.

## Requirements

- APIs must be callable by native extensions through shipped DMSDK headers.
- Unsupported values must return an explicit result, not zeros that can be
  mistaken for valid geometry.
- Rectangles must declare origin, units, and whether they describe the outer
  window, client/drawable content, monitor, or render target.
- Transform snapshots must have documented lifetime and become invalid after
  camera/layout destruction or reconfiguration.
- Render receipts must be monotonic within one engine instance and identify
  the render callback/submission they describe.
- Headless and null-graphics backends must return `RESULT_NOT_SUPPORTED` for
  geometry or readback facts they do not own.

## User Workflow / API

### Window and display geometry

```cpp
namespace dmGraphics
{
    enum GeometryResult
    {
        GEOMETRY_RESULT_OK,
        GEOMETRY_RESULT_NOT_SUPPORTED,
        GEOMETRY_RESULT_NO_WINDOW,
    };

    struct RectI
    {
        int32_t  m_X;
        int32_t  m_Y;
        uint32_t m_Width;
        uint32_t m_Height;
    };

    struct WindowGeometry
    {
        RectI m_OuterDisplayPoints;
        RectI m_ClientDisplayPoints;
        RectI m_DrawablePixels;
        RectI m_MonitorPixels;
        float m_DisplayScaleX;
        float m_DisplayScaleY;
    };

    GeometryResult GetWindowGeometry(HContext context, WindowGeometry* out);
}
```

Coordinates use a documented top-left origin. A backend may return partial
availability flags if, for example, mobile has drawable geometry but no
meaningful outer window.

### Camera and GUI transforms

```cpp
namespace dmRender
{
    enum TransformResult
    {
        TRANSFORM_RESULT_OK,
        TRANSFORM_RESULT_NOT_SUPPORTED,
        TRANSFORM_RESULT_INVALID_HANDLE,
        TRANSFORM_RESULT_OUTSIDE_VIEWPORT,
    };

    TransformResult WorldToDrawable(HRenderContext context,
                                    HRenderCamera camera,
                                    const dmVMath::Point3& world,
                                    dmVMath::Point3* drawable);
    TransformResult DrawableToWorld(HRenderContext context,
                                    HRenderCamera camera,
                                    const dmVMath::Point3& drawable,
                                    dmVMath::Point3* world);
}

namespace dmGui
{
    TransformResult LayoutToDrawable(HScene scene,
                                     HNode node,
                                     const dmVMath::Point3& layout,
                                     dmVMath::Point3* drawable);
    TransformResult DrawableToLayout(HScene scene,
                                     HNode node,
                                     const dmVMath::Point3& drawable,
                                     dmVMath::Point3* layout);
}
```

The render API needs an enumeration mechanism for active public camera handles,
or scene traversal must expose the public handle for camera components. Custom
render-script transforms remain application-owned.

### Render-frame receipt

```cpp
namespace dmExtension
{
    struct RenderCallbackParams
    {
        uint64_t m_UpdateFrame;
        uint64_t m_RenderFrame;
        uint64_t m_SubmissionId;
        uint64_t m_MonotonicTimeUs;
    };

    typedef ExtensionResult (*RenderCallback)(ExtensionParams*,
                                              const RenderCallbackParams*);
}
```

`m_RenderFrame` advances only for rendered frames. `m_SubmissionId` identifies
the submission associated with post-render readback. Backends that defer GPU
completion may add an asynchronous completion/fence API rather than claiming
that post-render means presented.

### Qualified scene identity

```cpp
namespace dmGameObject
{
    struct SceneIdentity
    {
        dmhash_t m_CollectionIdentifier;
        uint32_t m_CollectionGeneration;
        dmhash_t m_InstanceIdentifier;
        uint32_t m_InstanceGeneration;
    };

    bool GetSceneIdentity(const SceneNode* node, SceneIdentity* out);
}
```

Collection identity and generation are required even for collection traversal
nodes. Component/subcomponent identities build on the owning `SceneIdentity`
plus their component identifier and component generation when one exists.

## Technical Design

### Platform ownership

`dmPlatform` backends already own native windows and display scale. They should
collect outer/client/monitor rectangles and normalize them into the public
top-left convention. `dmGraphics` exposes the normalized result so extensions
do not depend on GLFW, Win32, Cocoa, Android, or iOS handles.

### Transform ownership

Render cameras own view/projection and viewport state. GUI scenes own adjust
mode, layout, pivot, parent transforms, and clipping. Conversion remains in
those subsystems so extensions cannot drift from engine behavior.

Transform calls are main-thread-only in the first version. The engine validates
handles before reading matrices. No transform object is retained by the caller.

### Identity lifetime

Collection and instance generations advance on allocation. `SceneIdentity` is
valid only while all generations still match. The engine must not expose raw
pointers as serialized identity.

### Render receipt lifetime

Counters reset when the engine instance starts. Trace consumers correlate them
with a separate engine-instance id. The callback values are immutable and need
no allocation.

### Performance and memory

Geometry and transform queries are on-demand and allocation-free. Identity adds
fixed-size fields to traversal output only when requested. Four 64-bit receipt
values per render callback are negligible. No per-frame history is retained by
the engine.

## Alternatives

### Option 1: Keep platform-private bridge implementations

Pros:

- No engine API change.
- Can expose platform-specific facts immediately.

Cons:

- Native extensions depend on private window/render structures.
- Behavior diverges by backend and engine version.
- Extender builds can break without a compatibility signal.

### Option 2: Application-published transforms only

Pros:

- Supports custom cameras and render scripts exactly as the application sees
  them.
- No engine API change.

Cons:

- Loses zero-configuration inspection.
- Every project must implement and version the same plumbing.
- Does not solve outer/client/display geometry or render receipts.

### Option 3: Public capability-bounded DMSDK APIs (recommended)

Pros:

- Engine subsystems remain the source of truth.
- Extensions receive explicit unsupported results.
- Works for automation, accessibility inspection, capture tools, and debuggers.

Cons:

- Requires backend implementation and public API maintenance.
- Custom application render transforms still need opt-in publication.

## Compatibility and Migration

All APIs are additive. Existing games, render scripts, resources, Bob output,
hot reload, and live update are unchanged.

Automation Bridge keeps current names and adds capability-versioned fields when
the engine APIs exist. Its existing `window` rectangle continues to mean
drawable pixels. It does not relabel that rectangle as outer-window geometry.

Older engines continue returning bridge update frames and instance
identifier/generation where available. Consumers must check capabilities before
requesting monitor geometry, world/GUI conversion, or engine render receipts.

## Implementation Plan

1. Add platform window geometry structs and desktop backend implementations.
2. Normalize origins/units in `dmGraphics::GetWindowGeometry()` and add mobile,
   web, and null-backend unsupported behavior.
3. Add render callback receipts sourced from the engine render loop.
4. Expose camera transforms and camera enumeration/handles.
5. Expose GUI layout transforms.
6. Add collection generation and `GetSceneIdentity()` to public traversal.
7. Update DMSDK reference docs and native extension examples.

## Testing

- Graphics unit tests validate outer/client/drawable relationships at display
  scales 1.0, 1.5, and 2.0, including a moved window and secondary monitor.
- Platform tests cover macOS, Windows, Linux, Android, iOS, HTML5, and null
  graphics, asserting explicit unsupported results where appropriate.
- Render tests verify update frames, skipped renders, render frames, and
  submission ids are monotonic and correctly separated.
- Camera tests round-trip world -> drawable -> world for perspective,
  orthographic, auto-fit, auto-cover, viewport, and high-DPI cases.
- GUI tests round-trip every adjust mode, pivot, parent transform, and active
  layout.
- Game object tests delete/recreate an identifier and assert generation changes;
  collection tests unload/reload the same collection identifier and assert the
  qualified identity changes.
- Automation Bridge integration tests verify capability negotiation and reject
  stale identities across both instance and collection reuse.

## Open Questions

- Q: Should geometry use per-axis display scale everywhere, even though the
  existing public API returns one float?
- Q: Can every graphics backend provide a submission id, or should that field
  be optional behind a flag?
- Q: Should camera/GUI conversion accept a captured transform revision so a
  caller can reject changes between discovery and input?
- Q: Is collection generation already available internally in a form suitable
  for a public, serialization-safe handle?
- Q: Should component pools expose their own generation, or is owning game
  object generation plus component id sufficient for traversal identity?
