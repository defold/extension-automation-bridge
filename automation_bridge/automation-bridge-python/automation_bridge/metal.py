"""Metal GPU trace capture for a running Defold engine on macOS."""

from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Dict, Mapping, Optional, Union

from .waits import wait_until


class MetalCaptureError(RuntimeError):
    """Raised when a Metal GPU trace reports a native capture failure."""


@dataclass(frozen=True)
class MetalCaptureStatus:
    """State reported by the native ``/metal`` capture endpoint."""

    state: str
    path: Optional[str]
    frames: int
    frames_captured: int
    stop_requested: bool
    error: Optional[str] = None

    @classmethod
    def from_raw(cls, raw: Mapping[str, Any]) -> "MetalCaptureStatus":
        return cls(
            state=str(raw.get("state", "idle")),
            path=raw.get("path") or None,
            frames=int(raw.get("frames", 0)),
            frames_captured=int(raw.get("frames_captured", 0)),
            stop_requested=bool(raw.get("stop_requested", False)),
            error=raw.get("error"),
        )

    @property
    def complete(self) -> bool:
        """Return whether capture finished normally."""
        return self.state == "complete"

    @property
    def terminal(self) -> bool:
        """Return whether capture no longer needs polling."""
        return self.state in {"complete", "canceled", "failed"}

    def to_dict(self) -> Dict[str, Any]:
        """Return JSON-serializable capture state."""
        return asdict(self)


class MetalCaptureClient:
    """Capture rendered frames to an Apple Metal ``.gputrace`` document."""

    def __init__(self, bridge: Any):
        self.bridge = bridge

    def status(self) -> MetalCaptureStatus:
        """Return current or most recently completed Metal capture state."""
        return MetalCaptureStatus.from_raw(self.bridge.request("GET", "/metal"))

    def start(
        self,
        path: Union[str, Path],
        *,
        frames: int = 1,
        wait: bool = True,
        timeout: float = 30.0,
    ) -> MetalCaptureStatus:
        """Schedule a Metal GPU trace and optionally wait for completion.

        The running engine must use the Metal graphics adapter and must have
        been launched with ``METAL_CAPTURE_ENABLED=1``.
        """
        if isinstance(frames, bool) or not isinstance(frames, int) or not 1 <= frames <= 10000:
            raise ValueError("frames must be an integer from 1 through 10000")
        if wait and timeout < 0:
            raise ValueError("timeout must be non-negative")
        output = Path(path).expanduser().resolve()
        if output.suffix != ".gputrace":
            raise ValueError("Metal capture path must end in .gputrace")
        output.parent.mkdir(parents=True, exist_ok=True)
        capture = MetalCaptureStatus.from_raw(
            self.bridge.request(
                "POST",
                "/metal",
                params={"path": str(output), "frames": frames},
            )
        )
        self.bridge._trace_record("metal_capture_started", capture.to_dict())
        return self.wait(timeout=timeout) if wait else capture

    def wait(self, *, timeout: float = 30.0, interval: float = 0.02) -> MetalCaptureStatus:
        """Poll until capture completes, is canceled, or fails."""

        def terminal() -> Optional[MetalCaptureStatus]:
            capture = self.status()
            if capture.state == "failed":
                self.bridge._trace_record("metal_capture_failed", capture.to_dict())
                raise MetalCaptureError(capture.error or "Metal GPU trace capture failed")
            return capture if capture.terminal else None

        capture = wait_until(
            terminal,
            timeout=timeout,
            interval=interval,
            message="Metal GPU trace capture did not finish",
        )
        self.bridge._trace_record("metal_capture_finished", capture.to_dict())
        return capture

    def stop(
        self,
        *,
        wait: bool = True,
        timeout: float = 30.0,
    ) -> MetalCaptureStatus:
        """Cancel a pending capture or request early completion of an active one."""
        if wait and timeout < 0:
            raise ValueError("timeout must be non-negative")
        capture = MetalCaptureStatus.from_raw(self.bridge.request("DELETE", "/metal"))
        if wait and not capture.terminal:
            return self.wait(timeout=timeout)
        self.bridge._trace_record("metal_capture_finished", capture.to_dict())
        return capture
