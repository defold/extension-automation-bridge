# Automation Bridge: Native Video Recording Contract

## Current implementation

Automation Bridge records video inside the Defold process on macOS. The native
Objective-C++ implementation uses ScreenCaptureKit's `SCRecordingOutput`, which
encodes H.264 video and optional application audio directly into an MP4 file.
It does not launch FFmpeg, run a companion process, or expose platform window
handles to Python.

The implementation requires macOS 15 or newer because `SCRecordingOutput` was
introduced there. The extension remains portable: other platforms compile a
small unsupported implementation and do not advertise recording capabilities.

## Capture selection

The recorder asks ScreenCaptureKit for visible windows and selects the largest
window whose owning application process id matches the running Defold process.
This avoids mutable title matching and prevents the Python controller from
capturing another application's window. `SCContentFilter` captures that window
independently of the desktop and excludes the cursor, title-bar shadow, and
other applications.

The default output size is the selected window's point size multiplied by the
main screen backing scale. Callers may provide an explicit paired width and
height. Frame rate is an integer from 1 through 60 and defaults to 30.

## Audio

Application audio is enabled by default. The stream sets
`excludesCurrentProcessAudio` to false, uses a 48 kHz stereo configuration, and
lets ScreenCaptureKit mux the audio and video on its shared capture clock. A
caller can disable audio explicitly.

The recorder never substitutes microphone or system-wide audio. Platforms
without an equivalent process-aware native API must report recording as
unsupported until they receive a dedicated implementation.

## Native API

The debug HTTP service exposes:

- `GET /recording/capabilities`
- `GET /recording/status`
- `POST /recording/start`
- `POST /recording/stop`

Start accepts `path`, optional `width` and `height`, `fps`, and `audio`. The
native response reports active/finalized state, dimensions, frame rate, codec,
container, timestamps, duration, path, and failure text. Only one recording may
be active. Stop is synchronous: success means ScreenCaptureKit delivered its
finish callback and the MP4 exists with a non-zero size.

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

The game process owns the ScreenCaptureKit stream, recording output, and
delegate. Start waits for both stream startup and the recording-start callback.
Stop removes the recording output, waits for the recording-finished callback,
then stops the capture stream. Extension finalization stops an active recording
before releasing bridge state.

Permission denial, an absent process window, timeouts, duplicate start, inactive
stop, and finalization errors are explicit failures. The bridge never silently
falls back to display capture.

## Future platforms

Windows and Linux implementations should preserve this endpoint contract but
use their native process/window capture facilities. A platform must not
advertise application audio unless it can intentionally include the target
process audio without falling back to microphone input. Backbuffer recording is
a separate possible capability because it does not capture window presentation
or application audio.

## Testing

- Build every target to verify non-macOS stubs remain linkable.
- On macOS 15+, assert capability reporting and record a short resized MP4.
- Verify start rejects a second active session and stop rejects an inactive one.
- Verify permission denial and missing-window failures do not report success.
- Verify normal stop and extension shutdown leave a finalized non-empty MP4.
- Verify Python context cleanup preserves the workflow's original exception.
