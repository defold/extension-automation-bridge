# Automation Bridge: Native Video Recording Contract

## Current implementation

Automation Bridge records video inside the Defold process on macOS and Windows.
The Objective-C++ macOS implementation uses ScreenCaptureKit's
`SCRecordingOutput`, which encodes H.264 video and optional application audio
directly into an MP4 file. The C++ Windows implementation combines Windows
Graphics Capture with Media Foundation to produce video-only H.264 MP4. Neither
backend launches FFmpeg, runs a companion process, or exposes platform window
handles to Python.

The macOS implementation requires macOS 15 or newer because
`SCRecordingOutput` was introduced there. Windows video requires Windows 10
version 1903 or newer. Other platforms compile a small unsupported
implementation and do not advertise recording capabilities.

## Capture selection

The recorder asks ScreenCaptureKit for visible windows and selects the largest
window whose owning application process id matches the running Defold process.
This avoids mutable title matching and prevents the Python controller from
capturing another application's window. `SCContentFilter` captures that window
independently of the desktop and excludes the cursor, title-bar shadow, and
other applications.

On Windows, `EnumWindows` selects the largest visible unowned top-level window
belonging to the current process. `IGraphicsCaptureItemInterop::CreateForWindow`
creates an occlusion-independent capture item for its `HWND`. The backend maps
`GetClientRect` into the capture surface, crops every Direct3D 11 frame to that
client area, and temporarily disables Windows 11 DWM corner rounding. The prior
corner preference is restored during finalization.

The default output size is the selected window's point size multiplied by the
window screen's backing scale. AppKit includes the rounded bottom edge in its
content rectangle, so the macOS backend removes a 16-point bottom safety band
to return a fully rectangular game frame. Callers may provide an explicit
paired width and height. Frame rate is an integer from 1 through 60 and defaults
to 30.

## Audio

On macOS, application audio is enabled by default. The stream sets
`excludesCurrentProcessAudio` to false, uses a 48 kHz stereo configuration, and
lets ScreenCaptureKit mux the audio and video on its shared capture clock. A
caller can disable audio explicitly.

The recorder never substitutes microphone or system-wide audio. Platforms
without an equivalent process-aware native API must report recording as
unsupported until they receive a dedicated implementation.

The current Windows backend advertises video but not application audio. Its
backend default is `audio=false`, and an explicit `audio=true` request fails.

## Native API

The debug HTTP service exposes:

- `GET /recording/capabilities`
- `GET /recording/status`
- `POST /recording/start`
- `POST /recording/stop`

Start accepts `path`, optional `width` and `height`, `fps`, and `audio`. The
native response reports active/finalized state, dimensions, frame rate, codec,
container, timestamps, duration, path, and failure text. Only one recording may
be active. Stop is synchronous: success means the platform encoder finalized
and the MP4 exists with a non-zero size.

Health advertises `recording.video` and `recording.application_audio` only when
the current runtime supports the native implementation.

## Python ownership

`bridge.recording` is deliberately a thin HTTP controller. It validates the
small public option set, resolves and creates the output directory, and returns
a context-managed session. Context exit always requests native finalization and
does not replace an exception already raised by the automated workflow.

Python does not select windows, request OS permissions, encode video, probe the
result with an external binary, or manage a subprocess.

## Lifecycle and errors

The game process owns its platform capture and encoding objects. On macOS,
start waits for both stream startup and the recording-start callback; stop waits
for the recording-finished callback before stopping the stream. On Windows,
start waits until the first Direct3D frame is written, and stop closes the frame
pool, waits for any in-flight callback, then synchronously finalizes the Media
Foundation sink writer. Extension finalization stops an active recording before
releasing bridge state.

Permission denial, an absent process window, timeouts, duplicate start, inactive
stop, and finalization errors are explicit failures. The bridge never silently
falls back to display capture.

## Future platforms

Linux implementations should preserve this endpoint contract but use native
process/window capture facilities. A platform must not
advertise application audio unless it can intentionally include the target
process audio without falling back to microphone input. Backbuffer recording is
a separate possible capability because it does not capture window presentation
or application audio.

## Testing

- Build every target to verify platform implementations and stubs remain linkable.
- On macOS 15+, assert capability reporting and record a short resized MP4.
- On Windows 10 1903+, verify client-only capture, resizing, requested FPS,
  synchronous MP4 finalization, and DWM corner restoration.
- Verify start rejects a second active session and stop rejects an inactive one.
- Verify permission denial and missing-window failures do not report success.
- Verify normal stop and extension shutdown leave a finalized non-empty MP4.
- Verify Python context cleanup preserves the workflow's original exception.
