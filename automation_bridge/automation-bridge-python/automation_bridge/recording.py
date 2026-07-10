"""Optional FFmpeg-based platform recording companion.

The module uses only the standard library. FFmpeg/ffprobe are optional external
tools discovered at runtime; importing the core Automation Bridge client never
requires them.
"""

import json
import os
import re
import shutil
import subprocess
import sys
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence, Tuple, Union


class RecordingError(RuntimeError):
    """Base error for optional recording tooling."""


class UnsupportedRecordingCapability(RecordingError):
    """Raised before capture when a backend cannot guarantee a request."""


@dataclass(frozen=True)
class RecordingCapabilities:
    """Truthful feature flags for a concrete recorder/backend combination."""

    backend: str
    available: bool
    display: bool = False
    application_window: bool = False
    content_rectangle: bool = False
    exclude_titlebar: bool = False
    resize_output: bool = False
    frame_rate: bool = False
    video_codecs: Tuple[str, ...] = ()
    application_audio: bool = False
    audio_codecs: Tuple[str, ...] = ()
    reason: Optional[str] = None


@dataclass
class RecordingOptions:
    """Validated capture options passed to a recording backend."""

    path: Path
    size: Optional[Tuple[int, int]] = None
    fps: float = 30.0
    video_codec: str = "libx264"
    audio: str = "none"
    audio_codec: str = "aac"
    application: Optional[str] = None
    window_title: Optional[str] = None
    crop: Optional[Union[str, Tuple[int, int, int, int]]] = None
    source_rect: Optional[Tuple[int, int, int, int]] = None
    exclude_titlebar: bool = False
    extra_args: Tuple[str, ...] = ()


class RecordingClient:
    """Optional video-recording tools scoped beneath an Automation Bridge client."""

    def __init__(self, bridge: Any):
        self.bridge = bridge

    def capabilities(self, backend: Optional[Any] = None) -> RecordingCapabilities:
        """Return recorder capabilities without starting capture."""
        return (backend or default_backend()).capabilities()

    def permission_diagnostics(self, backend: Optional[Any] = None) -> Mapping[str, Any]:
        """Return recorder availability and platform permission guidance."""
        return (backend or default_backend()).permission_diagnostics()

    def start(
        self,
        path: Union[str, Path],
        *,
        size: Optional[Tuple[int, int]] = None,
        fps: float = 30.0,
        video_codec: str = "libx264",
        audio: str = "none",
        audio_codec: str = "aac",
        application: Optional[str] = None,
        window_title: Optional[str] = None,
        crop: Optional[Union[str, Tuple[int, int, int, int]]] = None,
        source_rect: Optional[Tuple[int, int, int, int]] = None,
        exclude_titlebar: bool = False,
        extra_args: Sequence[str] = (),
        backend: Optional[Any] = None,
    ) -> "RecordingSession":
        """Start optional platform recording and return its safe session."""
        selected_backend = backend or default_backend()
        if crop == "content" and source_rect is None:
            source_rect = self.bridge._recording_content_rect(self.bridge.screen())
        options = RecordingOptions(
            path=Path(path), size=size, fps=fps, video_codec=video_codec,
            audio=audio, audio_codec=audio_codec, application=application,
            window_title=window_title, crop=crop, source_rect=source_rect,
            exclude_titlebar=exclude_titlebar, extra_args=tuple(extra_args),
        )
        session = selected_backend.start(options)
        if hasattr(session, "_trace_callback"):
            session._trace_callback = self.bridge._trace_record
        self.bridge._trace_record("recording_started", {"path": str(path), "options": options})
        return session


@dataclass
class RecordingMetadata:
    """Requested and observed properties of a finalized recording."""

    path: str
    backend: str
    started_wall_time: float
    started_monotonic: float
    stopped_wall_time: Optional[float] = None
    duration: Optional[float] = None
    width: Optional[int] = None
    height: Optional[int] = None
    fps: Optional[float] = None
    video_codec: Optional[str] = None
    audio_codec: Optional[str] = None
    audio_channels: Optional[int] = None
    audio_sample_rate: Optional[int] = None
    finalized: bool = False
    interrupted: bool = False
    command: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        """Return JSON-serializable metadata."""
        return asdict(self)


class FFmpegRecordingBackend:
    """Capture a display/content rectangle through a platform FFmpeg input.

    Windows additionally supports FFmpeg's `gdigrab` title selector. Exact
    per-application audio is deliberately not advertised: FFmpeg device capture
    cannot guarantee process isolation on all supported platforms.
    """

    def __init__(
        self,
        ffmpeg: str = "ffmpeg",
        ffprobe: str = "ffprobe",
        *,
        platform: Optional[str] = None,
        display: Optional[str] = None,
        video_codecs: Optional[Sequence[str]] = None,
        popen: Callable[..., subprocess.Popen] = subprocess.Popen,
    ):
        self.ffmpeg = ffmpeg
        self.ffprobe = ffprobe
        self.platform = platform or sys.platform
        self.display = display
        self._configured_video_codecs = tuple(video_codecs) if video_codecs is not None else None
        self._probed_video_codecs: Optional[Tuple[str, ...]] = None
        self._popen = popen

    def capabilities(self) -> RecordingCapabilities:
        """Return capabilities for the current executable and platform."""
        executable = shutil.which(self.ffmpeg)
        if executable is None:
            return RecordingCapabilities(backend="ffmpeg", available=False, reason=f"{self.ffmpeg!r} was not found on PATH")
        supported = self.platform.startswith(("linux", "darwin", "win"))
        if not supported:
            return RecordingCapabilities(backend="ffmpeg", available=False, reason=f"unsupported platform {self.platform!r}")
        is_windows = self.platform.startswith("win")
        video_codecs = self._available_video_codecs()
        return RecordingCapabilities(
            backend="ffmpeg",
            available=True,
            display=True,
            application_window=is_windows,
            content_rectangle=True,
            exclude_titlebar=True,  # via an explicit content rectangle
            resize_output=True,
            frame_rate=True,
            video_codecs=video_codecs,
            application_audio=False,
            audio_codecs=(),
        )

    def permission_diagnostics(self) -> Mapping[str, Any]:
        """Return actionable preflight information without prompting the OS."""
        capabilities = self.capabilities()
        notes: List[str] = []
        if self.platform == "darwin":
            notes.append("macOS Screen Recording permission must be granted to the Python/terminal host; FFmpeg reports denial when capture starts")
        elif self.platform.startswith("linux"):
            notes.append("X11 capture requires DISPLAY and access to its Xauthority; Wayland generally requires a portal-specific backend")
            if not (self.display or os.environ.get("DISPLAY")):
                notes.append("DISPLAY is not set")
        elif self.platform.startswith("win"):
            notes.append("protected/UAC-elevated windows may not be visible to a recorder running at a lower integrity level")
        return {"capabilities": asdict(capabilities), "notes": notes}

    def _available_video_codecs(self) -> Tuple[str, ...]:
        if self._configured_video_codecs is not None:
            return self._configured_video_codecs
        if self._probed_video_codecs is not None:
            return self._probed_video_codecs
        candidates = ("libx264", "h264", "hevc", "libvpx-vp9")
        try:
            output = subprocess.check_output(
                [self.ffmpeg, "-hide_banner", "-encoders"],
                stderr=subprocess.STDOUT,
                text=True,
                timeout=5.0,
            )
            encoder_names = {
                match.group(1)
                for line in output.splitlines()
                if (match := re.match(r"^\s*[A-Z.]{6}\s+(\S+)", line))
            }
            self._probed_video_codecs = tuple(codec for codec in candidates if codec in encoder_names)
        except (OSError, subprocess.SubprocessError):
            self._probed_video_codecs = ()
        return self._probed_video_codecs

    def validate(self, options: RecordingOptions) -> None:
        """Reject requests the backend cannot implement exactly."""
        capabilities = self.capabilities()
        if not capabilities.available:
            raise UnsupportedRecordingCapability(capabilities.reason or "FFmpeg recording is unavailable")
        if options.audio == "application":
            raise UnsupportedRecordingCapability(
                "the FFmpeg backend cannot isolate application audio; use a ScreenCaptureKit/PipeWire/WASAPI process-audio backend"
            )
        if options.audio != "none":
            raise UnsupportedRecordingCapability("this backend currently supports audio='none' only")
        if options.application and not options.window_title:
            raise UnsupportedRecordingCapability("application selection requires a concrete window_title for this backend")
        if options.window_title and not capabilities.application_window:
            raise UnsupportedRecordingCapability(f"exact window-title capture is not supported on {self.platform}")
        if options.crop == "content" and options.source_rect is None:
            raise UnsupportedRecordingCapability("crop='content' requires content geometry from the bridge or an explicit source_rect")
        if options.exclude_titlebar and options.source_rect is None:
            raise UnsupportedRecordingCapability("title-bar exclusion requires an explicit content source_rect")
        if options.video_codec not in capabilities.video_codecs:
            raise UnsupportedRecordingCapability(f"video codec {options.video_codec!r} is not in the advertised codec set")
        if options.size and (options.size[0] <= 0 or options.size[1] <= 0):
            raise ValueError("output size must be positive")
        if options.fps <= 0:
            raise ValueError("fps must be positive")

    def command(self, options: RecordingOptions) -> List[str]:
        """Build the FFmpeg command after capability validation."""
        self.validate(options)
        command = [self.ffmpeg, "-nostdin", "-y"]
        rect = options.source_rect
        if self.platform.startswith("linux"):
            display = self.display or os.environ.get("DISPLAY")
            if not display:
                raise UnsupportedRecordingCapability("X11 display capture requires DISPLAY or backend display=")
            command += ["-f", "x11grab", "-framerate", str(options.fps)]
            if rect:
                x, y, width, height = rect
                command += ["-video_size", f"{width}x{height}", "-i", f"{display}+{x},{y}"]
            else:
                command += ["-i", display]
        elif self.platform == "darwin":
            # AVFoundation device numbering varies. `display` is intentionally
            # explicit and defaults to the conventional first screen device.
            command += ["-f", "avfoundation", "-framerate", str(options.fps), "-i", self.display or "Capture screen 0:none"]
        else:
            command += ["-f", "gdigrab", "-framerate", str(options.fps)]
            if rect:
                # An explicit global content rectangle is the only reliable
                # decoration-free gdigrab source; title capture includes the
                # non-client frame and supplies no portable content offset.
                x, y, width, height = rect
                command += ["-offset_x", str(x), "-offset_y", str(y), "-video_size", f"{width}x{height}", "-i", "desktop"]
            else:
                source = f"title={options.window_title}" if options.window_title else "desktop"
                command += ["-i", source]

        filters: List[str] = []
        if rect and not (self.platform.startswith("linux") or self.platform.startswith("win")):
            x, y, width, height = rect
            filters.append(f"crop={width}:{height}:{x}:{y}")
        if isinstance(options.crop, tuple):
            x, y, width, height = options.crop
            filters.append(f"crop={width}:{height}:{x}:{y}")
        if options.size:
            filters.append(f"scale={options.size[0]}:{options.size[1]}:flags=lanczos")
        if filters:
            command += ["-vf", ",".join(filters)]
        command += ["-r", str(options.fps), "-c:v", options.video_codec, "-pix_fmt", "yuv420p"]
        command += list(options.extra_args)
        command.append(str(options.path))
        return command

    def start(self, options: RecordingOptions) -> "RecordingSession":
        """Start capture and return a context-manageable session."""
        command = self.command(options)
        options.path.parent.mkdir(parents=True, exist_ok=True)
        # Override -nostdin so a graceful `q` finalizes the container.
        command[1:2] = []
        process = self._popen(command, stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return RecordingSession(self, options, process, command)

    def probe(self, path: Path) -> Mapping[str, Any]:
        """Read stream/container metadata with ffprobe when available."""
        if shutil.which(self.ffprobe) is None:
            return {}
        try:
            output = subprocess.check_output(
                [self.ffprobe, "-v", "error", "-show_entries", "format=duration:stream=codec_type,codec_name,width,height,r_frame_rate,channels,sample_rate", "-of", "json", str(path)],
                stderr=subprocess.DEVNULL,
                timeout=10.0,
            )
            parsed = json.loads(output.decode("utf-8"))
            return parsed if isinstance(parsed, dict) else {}
        except (OSError, subprocess.SubprocessError, ValueError):
            return {}


class RecordingSession:
    """A running recorder that finalizes safely on normal or exceptional exit."""

    def __init__(self, backend: FFmpegRecordingBackend, options: RecordingOptions, process: subprocess.Popen, command: Sequence[str]):
        self.backend = backend
        self.options = options
        self.process = process
        self.metadata = RecordingMetadata(
            path=str(options.path), backend="ffmpeg", started_wall_time=time.time(), started_monotonic=time.monotonic(),
            width=options.size[0] if options.size else None, height=options.size[1] if options.size else None,
            fps=options.fps, video_codec=options.video_codec, audio_codec=None if options.audio == "none" else options.audio_codec,
            command=list(command),
        )
        self._trace_callback: Optional[Callable[[str, Any], None]] = None

    def __enter__(self) -> "RecordingSession":
        return self

    def __exit__(self, exc_type: Any, exc: Any, traceback: Any) -> bool:
        try:
            self.stop(interrupted=exc_type is not None)
        except BaseException:
            if exc_type is None:
                raise
            # Never replace the workflow's original exception with a recorder
            # finalization failure. The metadata remains `finalized=False`.
        return False

    def stop(self, timeout: float = 10.0, *, interrupted: bool = False) -> RecordingMetadata:
        """Request graceful FFmpeg finalization, escalating only on timeout."""
        if self.metadata.stopped_wall_time is not None:
            return self.metadata
        self.metadata.interrupted = interrupted
        if self.process.poll() is None:
            try:
                if self.process.stdin is not None:
                    self.process.stdin.write(b"q\n")
                    self.process.stdin.flush()
                self.process.wait(timeout=timeout)
            except (BrokenPipeError, OSError):
                self.process.wait(timeout=timeout)
            except subprocess.TimeoutExpired:
                self.process.terminate()
                try:
                    self.process.wait(timeout=2.0)
                except subprocess.TimeoutExpired:
                    self.process.kill()
                    self.process.wait(timeout=2.0)
        self.metadata.stopped_wall_time = time.time()
        self.metadata.duration = max(0.0, time.monotonic() - self.metadata.started_monotonic)
        if self.process.returncode not in (0, 255):
            self._trace("recording_failed", {"metadata": self.metadata, "error": f"FFmpeg exited with status {self.process.returncode}"})
            raise RecordingError(f"FFmpeg exited with status {self.process.returncode}; output may be incomplete")
        self._apply_probe(self.backend.probe(self.options.path))
        self.metadata.finalized = self.options.path.exists() and self.options.path.stat().st_size > 0
        if not self.metadata.finalized:
            self._trace("recording_failed", {"metadata": self.metadata, "error": "output file is missing or empty"})
            raise RecordingError(f"recording did not produce a non-empty file: {self.options.path}")
        self._trace("recording_stopped", self.metadata)
        return self.metadata

    def _trace(self, kind: str, payload: Any) -> None:
        if self._trace_callback is not None:
            self._trace_callback(kind, payload)

    def _apply_probe(self, probe: Mapping[str, Any]) -> None:
        format_data = probe.get("format", {})
        if isinstance(format_data, Mapping):
            try:
                self.metadata.duration = float(format_data.get("duration", self.metadata.duration))
            except (TypeError, ValueError):
                pass
        for stream in probe.get("streams", []):
            if not isinstance(stream, Mapping):
                continue
            if stream.get("codec_type") == "video":
                self.metadata.video_codec = stream.get("codec_name") or self.metadata.video_codec
                self.metadata.width = stream.get("width") or self.metadata.width
                self.metadata.height = stream.get("height") or self.metadata.height
                rate = str(stream.get("r_frame_rate", ""))
                try:
                    numerator, denominator = rate.split("/", 1)
                    self.metadata.fps = float(numerator) / float(denominator)
                except (ValueError, ZeroDivisionError):
                    pass
            elif stream.get("codec_type") == "audio":
                self.metadata.audio_codec = stream.get("codec_name")
                self.metadata.audio_channels = stream.get("channels")
                try:
                    self.metadata.audio_sample_rate = int(stream.get("sample_rate"))
                except (TypeError, ValueError):
                    pass


def default_backend() -> FFmpegRecordingBackend:
    """Return the default optional FFmpeg backend without starting capture."""
    return FFmpegRecordingBackend()
