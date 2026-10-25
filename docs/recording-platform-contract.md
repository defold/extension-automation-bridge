# Defold-native video recording contract

## Ownership and requirements

Defold 1.14.1 introduces the independent recorder from
[defold/defold#13501](https://github.com/defold/defold/pull/13501).
The engine owns window selection, capture, audio, encoding, asynchronous state,
file preparation, and shutdown cleanup. Automation Bridge contains no recorder
backend, platform capture dependencies, recording routes, or recording health
capabilities. The extension requires Defold 1.14.1 or newer.

The built-in recorder ships in graphical desktop debug engines unless recording
is excluded by the app manifest. Release, headless, mobile, and web engines do
not expose this service. The authoritative wire schema is the running engine's
`/openapi.json`; implementation details are documented in the
[upstream recorder README](https://github.com/defold/defold/blob/c571fef566079dd309d009b62198e4cc5782cdf6/engine/record/README.md).

| Platform | Capture | Application audio |
| --- | --- | --- |
| macOS 15+ | ScreenCaptureKit | Supported |
| Windows 10 version 1903+ | Windows Graphics Capture and Media Foundation | Requires Windows build 20348+ |
| Linux with supported graphics | Framebuffer capture, OpenH264 and minimp4 | Unsupported |

Capabilities report backend and OS support; capture may still fail because of
permissions or device availability. Audio defaults to backend support. Explicit
unsupported audio is an error. There is no microphone or system-audio fallback.
No external encoder or helper process is required.

Capture uses the engine window's native content dimensions in physical pixels.
No resizing or scaling option is exposed. Odd dimensions crop at most one row
and column. Window resizing or a pixel-density change finalizes capture. The
frame rate is an integer from 1 to 60, defaulting to 30.

## Native HTTP protocol

Use the engine service port with root `/recording` paths, outside the bridge's
`/automation-bridge/v2` namespace. Successful JSON is the receipt itself, without
an `ok`/`data` envelope. Errors contain `error.code` and `error.message`, with the
actual HTTP status. Bridge query parameters and its HTTP status adapter do not
apply.

| Method | Path | Result |
| --- | --- | --- |
| GET | `/recording/capabilities` | Protocol `version`, `instance_id`, `supported`, `audio`, backend and limits |
| GET | `/recording/status` | Current operation, native state and file receipt |
| POST | `/recording/start` | HTTP 202; optional JSON `fps` and `audio` only |
| POST | `/recording/stop` | HTTP 202; JSON `operation_id` from start |
| GET | `/recording/files/{id}` | Finalized MP4 stream |
| DELETE | `/recording/files/{id}` | HTTP 204; release retained native storage |

The wrapper requires recorder protocol version 1. Discover `instance_id` through
capabilities, then retain it with the operation and file receipts. Send it as
`X-Recording-Instance` on every subsequent request. Mutations require the header;
GET status/download also accept it to reject results from a replacement engine.
Never refresh an old session's identity after restart or reboot. Stop additionally
identifies the original operation, so another Lua or HTTP recording is protected.

Start has no output-path parameter. Poll the accepted operation until `pending`
is false and `active` is true. After stop, wait for `pending == false`,
`finalized == true`, and `file.state == "complete"`. Native finalization and file
hashing are separate phases. Failures may arrive asynchronously in `failure`,
`state == "failed"`, or `file.state == "failed"`.

A complete file receipt has `id`, `url`, `state`, `size`, and `sha256`. Download
with a single GET, read incrementally, and verify size and SHA-256. Closed byte
ranges are supported by the engine but are unnecessary for the wrapper's whole
file download. Files survive capture completion until deletion or engine
shutdown/reboot. The service retains at most 128 files, so consumers should
release them after successful retrieval.

## Python behavior

`game.video_recording.capabilities()` discovers the independent native service.
Missing routes return `available=False` and a Defold 1.14.1 requirement message;
connection failures and incompatible protocols raise `VideoRecordingError`.
`game.supports()` and bridge health do not describe recording availability.

`game.video_recording.start(path, fps=30, audio=None, timeout=30.0)` validates its
options, creates the local output directory, submits native start, and waits for
active capture. The returned session retains the engine identity and operation
ID. The path belongs to the Python client's filesystem and is never sent to the
engine. Metadata maps native `format`/`codec` to `container`/`video_codec` and
computes duration from the native monotonic timestamps.

`session.stop(timeout=30.0)` handles both explicit and automatic finalization,
waits for the complete file receipt, and streams into a temporary file beside
the destination. Only a matching size and SHA-256 allow atomic replacement of
the destination. Then it deletes the engine-owned file. `metadata.path` remains
unset until the verified download succeeds. Repeated successful stops are local
and idempotent. A failed download retains the receipt for retry and leaves the
previous destination untouched.

Polling timeouts bound each wait phase; individual HTTP requests use the client's
transport timeout. The engine must continue updating during native transitions.
A startup timeout or interruption triggers cleanup of the accepted operation;
`exception.recording_session` allows retrying `stop()` and `exception.cleanup_error`
retains any cleanup failure. Context exit finalizes even during cooperative
cancellation and preserves an exception already raised by the workflow. Finish
recording before closing the engine, which removes its temporary files.

## Validation

The dependency-free suite uses a local HTTP service with the upstream receipt
schema. It exercises asynchronous capture and file preparation, option validation,
capability negotiation, 202/204 handling, whole-file downloads, checksum/size
failures, retries, stale instances and operations, automatic resize finalization,
startup cancellation/timeouts, and exception-preserving context cleanup.

A desktop release check should additionally build this extension with Defold
1.14.1+, probe recorder capabilities, capture a short clip, and verify the local
MP4 after context exit. Exercise platform capture permissions and application
audio where supported. Native platform implementation tests remain upstream in
Defold; this repository validates the Python integration and bridge coexistence.
