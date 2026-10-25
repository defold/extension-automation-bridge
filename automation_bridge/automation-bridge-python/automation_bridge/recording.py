"""Video capture through Defold 1.14.1's independent recording HTTP service."""

import hashlib
import http.client
import json
import math
import tempfile
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import asdict, dataclass, replace
from pathlib import Path
from typing import Any, Callable, Dict, Mapping, Optional, Tuple, Union

from .cancellation import CancellationToken, cancellation_scope, check_cancelled
from .waits import wait_until


_UNAVAILABLE = (
    "Video recording requires Defold 1.14.1 or newer with recording enabled "
    "in a desktop debug engine."
)


class VideoRecordingError(RuntimeError):
    """A recorder service, capture, or download failure.

    ``code`` and ``status`` preserve native HTTP errors when available.
    """

    def __init__(self, message: str, *, code: Optional[str] = None, status: Optional[int] = None):
        super().__init__(message)
        self.code = code
        self.status = status


@dataclass(frozen=True)
class VideoRecordingCapabilities:
    """Recorder support, protocol version, and identity from the engine service."""

    backend: str
    available: bool
    application_window: bool
    application_audio: bool
    resize_output: bool
    frame_rate: bool
    containers: Tuple[str, ...]
    video_codecs: Tuple[str, ...]
    minimum_platform_version: Optional[str] = None
    reason: Optional[str] = None
    version: int = 1
    instance_id: Optional[str] = None
    max_fps: int = 60

    @classmethod
    def from_raw(cls, raw: Mapping[str, Any]) -> "VideoRecordingCapabilities":
        """Translate the engine's version 1 capability receipt."""
        supported = bool(raw.get("supported", False))
        return cls(
            backend=str(raw.get("backend", "native")),
            available=supported,
            application_window=supported,
            application_audio=bool(raw.get("audio", False)),
            resize_output=False,
            frame_rate=supported,
            containers=(str(raw["format"]),) if raw.get("format") else (),
            video_codecs=(str(raw["codec"]),) if raw.get("codec") else (),
            minimum_platform_version=raw.get("minimum_platform_version") or None,
            reason=raw.get("unsupported_reason") or None,
            version=int(raw.get("version", 0)),
            instance_id=raw.get("instance_id"),
            max_fps=int(raw.get("max_fps", 0)),
        )


@dataclass(frozen=True)
class VideoRecordingFile:
    """Engine-owned MP4 receipt, retained until deletion or engine shutdown."""

    id: str
    url: str
    state: str
    size: int
    sha256: str

    @classmethod
    def from_raw(cls, raw: Mapping[str, Any]) -> "VideoRecordingFile":
        """Read a file receipt from recording status."""
        return cls(str(raw["id"]), str(raw["url"]), str(raw["state"]), int(raw["size"]), str(raw["sha256"]))


@dataclass(frozen=True)
class VideoRecordingMetadata:
    """Native operation and file receipt; ``path`` is set after a verified download."""

    path: Optional[str]
    backend: str
    active: bool
    finalized: bool
    audio: bool
    width: int
    height: int
    fps: int
    video_codec: str
    container: str
    started_wall_time_us: Optional[int] = None
    started_monotonic_time_us: Optional[int] = None
    stopped_wall_time_us: Optional[int] = None
    stopped_monotonic_time_us: Optional[int] = None
    duration_seconds: Optional[float] = None
    failure: Optional[str] = None
    instance_id: str = ""
    operation_id: int = 0
    state: str = "idle"
    pending: bool = False
    file: Optional[VideoRecordingFile] = None

    @classmethod
    def from_raw(cls, raw: Mapping[str, Any]) -> "VideoRecordingMetadata":
        """Translate an engine status receipt without assuming a local file exists."""
        started = raw.get("started_monotonic_time_us") or None
        stopped = raw.get("stopped_monotonic_time_us") or None
        return cls(
            path=None,
            backend=str(raw.get("backend", "native")),
            active=bool(raw.get("active", False)),
            finalized=bool(raw.get("finalized", False)),
            audio=bool(raw.get("audio", False)),
            width=int(raw.get("width", 0)),
            height=int(raw.get("height", 0)),
            fps=int(raw.get("fps", 0)),
            video_codec=str(raw.get("codec", "h264")),
            container=str(raw.get("format", "mp4")),
            started_wall_time_us=raw.get("started_wall_time_us") or None,
            started_monotonic_time_us=started,
            stopped_wall_time_us=raw.get("stopped_wall_time_us") or None,
            stopped_monotonic_time_us=stopped,
            duration_seconds=(stopped - started) / 1_000_000 if started and stopped and stopped >= started else None,
            failure=raw.get("failure") or None,
            instance_id=str(raw["instance_id"]),
            operation_id=int(raw["operation_id"]),
            state=str(raw["state"]),
            pending=bool(raw["pending"]),
            file=VideoRecordingFile.from_raw(raw["file"]) if raw.get("file") else None,
        )

    def to_dict(self) -> Dict[str, Any]:
        """Return JSON-serializable metadata."""
        return asdict(self)


def _validate_timeout(timeout: float) -> None:
    if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or not math.isfinite(timeout) or timeout < 0:
        raise ValueError("timeout must be a finite non-negative number")


class VideoRecordingClient:
    """Control Defold 1.14.1+ recording directly on the engine service port.

    These root ``/recording`` routes have their own protocol and instance identity;
    they do not use the Automation Bridge envelope or capability list.
    """

    def __init__(self, bridge: Any):
        self.bridge = bridge
        self._service_url = urllib.parse.urljoin(bridge.base_url, "/")

    def _open(self, method: str, path: str, *, instance_id: Optional[str] = None,
              json_body: Optional[Mapping[str, Any]] = None) -> Any:
        self.bridge._ensure_open()
        headers = {"X-Recording-Instance": instance_id} if instance_id else {}
        data = None
        if json_body is not None:
            data = json.dumps(json_body, separators=(",", ":")).encode("utf-8")
            headers["Content-Type"] = "application/json"
        request = urllib.request.Request(self._service_url.rstrip("/") + path, data=data, headers=headers, method=method)
        try:
            return urllib.request.urlopen(request, timeout=self.bridge.timeout)
        except urllib.error.HTTPError as exc:
            with exc:
                body = exc.read().decode("utf-8", "replace")
            try:
                error = json.loads(body)["error"]
                code, message = error["code"], error["message"]
            except (ValueError, KeyError, TypeError):
                code, message = None, f"{method} {path} returned HTTP {exc.code}"
            raise VideoRecordingError(message, code=code, status=exc.code) from exc
        except (OSError, urllib.error.URLError, http.client.HTTPException) as exc:
            raise VideoRecordingError(f"{method} {path} failed: {exc}") from exc

    def _request(self, method: str, path: str, *, instance_id: Optional[str] = None,
                 json_body: Optional[Mapping[str, Any]] = None) -> Dict[str, Any]:
        try:
            with self._open(method, path, instance_id=instance_id, json_body=json_body) as response:
                if response.status == 204:
                    return {}
                raw = json.load(response)
        except (ValueError, OSError, http.client.HTTPException) as exc:
            raise VideoRecordingError(f"Invalid recording response from {path}: {exc}") from exc
        if not isinstance(raw, dict):
            raise VideoRecordingError(f"Recording response from {path} was not an object")
        return raw

    def capabilities(self) -> VideoRecordingCapabilities:
        """Probe native support; a missing service returns ``available=False``.

        Older engines and builds excluding recording have no recording service.
        Connection errors and incompatible protocol versions raise
        :class:`VideoRecordingError` instead of being treated as unsupported.
        """
        try:
            raw = self._request("GET", "/recording/capabilities")
        except VideoRecordingError as exc:
            if exc.status != 404:
                raise
            return VideoRecordingCapabilities.from_raw({"unsupported_reason": _UNAVAILABLE})
        if type(raw.get("version")) is not int or raw["version"] != 1 or not raw.get("instance_id"):
            raise VideoRecordingError("Unsupported native recording protocol; expected version 1 with an instance_id")
        return VideoRecordingCapabilities.from_raw(raw)

    def _status(self, instance_id: str) -> VideoRecordingMetadata:
        raw = self._request("GET", "/recording/status", instance_id=instance_id)
        metadata = VideoRecordingMetadata.from_raw(raw)
        if metadata.instance_id != instance_id:
            raise VideoRecordingError("Recording service instance changed", code="stale_instance")
        return metadata

    def status(self) -> VideoRecordingMetadata:
        """Return current native state, including asynchronous file preparation.

        ``finalized`` alone does not mean the download is ready: also check
        ``file.state == 'complete'``. This does not download the file.
        """
        capabilities = self.capabilities()
        if not capabilities.instance_id:
            raise VideoRecordingError(capabilities.reason or _UNAVAILABLE)
        return self._status(capabilities.instance_id)

    def start(
        self,
        path: Union[str, Path],
        *,
        fps: int = 30,
        audio: Optional[bool] = None,
        timeout: float = 30.0,
    ) -> "VideoRecordingSession":
        """Wait for native capture to start; save to local ``path`` on stop.

        Requires Defold 1.14.1+ with recording enabled in a desktop debug build.
        Capture uses the native content size, cropping odd dimensions without
        scaling. Resizing the game window automatically finalizes capture.
        Omitted ``audio`` uses native backend support; ``False`` is portable.
        ``timeout`` bounds startup polling; HTTP calls use the client's timeout.

        On interrupted or timed-out startup, cleanup is attempted. The raised
        exception's ``recording_session`` permits retrying ``stop()`` if needed;
        ``cleanup_error`` records any cleanup failure.
        """
        check_cancelled()
        _validate_timeout(timeout)
        if isinstance(fps, bool) or not isinstance(fps, int) or not 1 <= fps <= 60:
            raise ValueError("fps must be an integer between 1 and 60")
        if audio is not None and not isinstance(audio, bool):
            raise TypeError("audio must be a boolean or None")
        output = Path(path).expanduser().resolve()
        capabilities = self.capabilities()
        if not capabilities.available:
            raise VideoRecordingError(capabilities.reason or _UNAVAILABLE)
        json_body: Dict[str, Any] = {"fps": fps}
        if audio is not None:
            json_body["audio"] = audio
        output.parent.mkdir(parents=True, exist_ok=True)
        raw = self._request("POST", "/recording/start", instance_id=capabilities.instance_id, json_body=json_body)
        metadata = VideoRecordingMetadata.from_raw(raw)
        if metadata.instance_id != capabilities.instance_id or metadata.operation_id < 1:
            raise VideoRecordingError("Invalid recording start identity")
        session = VideoRecordingSession(self, metadata, output)
        try:
            session._wait(lambda value: not value.pending, timeout)
            if not session.metadata.active:
                raise VideoRecordingError("Recording ended before capture became active")
        except BaseException as exc:
            exc.recording_session = session
            try:
                # Cancellation must not prevent cleanup of the accepted operation.
                with cancellation_scope(CancellationToken()):
                    session.stop(timeout=timeout)
            except BaseException as cleanup_error:
                exc.cleanup_error = cleanup_error
            raise
        self.bridge._trace_record("video_recording_started", session.metadata.to_dict())
        return session

    def _download(self, metadata: VideoRecordingMetadata, output: Path) -> None:
        receipt = metadata.file
        if receipt is None or receipt.state != "complete" or receipt.size <= 0:
            raise VideoRecordingError("Recording has no completed file receipt")
        path = "/recording/files/" + urllib.parse.quote(receipt.id, safe="")
        if receipt.url != path or len(receipt.sha256) != 64 or any(c not in "0123456789abcdef" for c in receipt.sha256):
            raise VideoRecordingError("Invalid recording file receipt")
        temporary = None
        try:
            with self._open("GET", path, instance_id=metadata.instance_id) as response:
                if response.status != 200:
                    raise VideoRecordingError("Expected a complete MP4 response", status=response.status)
                with tempfile.NamedTemporaryFile(dir=output.parent, prefix=output.name + ".", suffix=".part", delete=False) as stream:
                    temporary = Path(stream.name)
                    digest = hashlib.sha256()
                    size = 0
                    while True:
                        chunk = response.read(64 * 1024)
                        if not chunk:
                            break
                        size += len(chunk)
                        if size > receipt.size:
                            raise VideoRecordingError("Recording download exceeds its file receipt size")
                        digest.update(chunk)
                        stream.write(chunk)
                if size != receipt.size or digest.hexdigest() != receipt.sha256:
                    raise VideoRecordingError("Recording download size or SHA-256 does not match its file receipt")
            temporary.replace(output)
        except (OSError, http.client.HTTPException) as exc:
            raise VideoRecordingError(f"Recording download failed: {exc}") from exc
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)


class VideoRecordingSession:
    """Capture that finalizes, verifies a local MP4, and deletes its engine copy.

    Context exit preserves exceptions from the workflow. Failed finalization or
    downloads can be retried with ``stop()`` on the same session while the engine
    retains the file. Instance and operation identities never change on retry.
    """

    def __init__(self, client: VideoRecordingClient, metadata: VideoRecordingMetadata, output: Path):
        self.client = client
        self.metadata = metadata
        self._output = output
        self._instance_id = metadata.instance_id
        self._operation_id = metadata.operation_id
        self._deleted = False

    def __enter__(self) -> "VideoRecordingSession":
        return self

    def __exit__(self, exc_type: Any, exc: Any, traceback: Any) -> bool:
        try:
            with cancellation_scope(CancellationToken()):
                self.stop()
        except BaseException as cleanup_error:
            if exc is None:
                raise
            exc.cleanup_error = cleanup_error
        return False

    def _accept(self, metadata: VideoRecordingMetadata) -> VideoRecordingMetadata:
        if metadata.instance_id != self._instance_id or metadata.operation_id != self._operation_id:
            raise VideoRecordingError("Recording operation was replaced", code="stale_operation")
        self.metadata = metadata
        if metadata.failure or metadata.state == "failed":
            raise VideoRecordingError(metadata.failure or "Native recording failed")
        if metadata.file and metadata.file.state == "failed":
            raise VideoRecordingError("Native recording file preparation failed")
        return metadata

    def _refresh(self) -> VideoRecordingMetadata:
        return self._accept(self.client._status(self._instance_id))

    def _wait(self, predicate: Callable[[VideoRecordingMetadata], bool], timeout: float) -> VideoRecordingMetadata:
        return wait_until(self._refresh, predicate=predicate, timeout=timeout, interval=0.05,
                          message="Native recording did not reach the requested state")

    def stop(self, *, timeout: float = 30.0) -> VideoRecordingMetadata:
        """Finalize, stream and verify the MP4, then release its engine storage.

        Polling waits up to ``timeout`` per phase; each HTTP request uses the
        client's transport timeout. Repeated successful calls return the same
        metadata. A download failure leaves the destination unchanged and keeps
        the engine file for retry. A window resize may already have stopped
        capture; the same session still downloads that completed recording.
        """
        _validate_timeout(timeout)
        if self._deleted:
            return self.metadata
        if self.metadata.path is None:
            if not (self.metadata.finalized and self.metadata.file and self.metadata.file.state == "complete"):
                self._refresh()
                if self.metadata.state == "starting":
                    self._wait(lambda value: value.state != "starting", timeout)
                if self.metadata.active and not self.metadata.pending:
                    try:
                        raw = self.client._request("POST", "/recording/stop", instance_id=self._instance_id,
                                                   json_body={"operation_id": self._operation_id})
                        self._accept(VideoRecordingMetadata.from_raw(raw))
                    except VideoRecordingError as exc:
                        # Resize can finalize between the status and stop requests.
                        if exc.code != "recording_inactive":
                            raise
                        self._refresh()
                        if self.metadata.active and not self.metadata.pending:
                            raise
                self._wait(lambda value: not value.pending and value.finalized and value.file is not None
                           and value.file.state == "complete", timeout)
            self.client._download(self.metadata, self._output)
            self.metadata = replace(self.metadata, path=str(self._output))
        receipt = self.metadata.file
        try:
            self.client._request("DELETE", receipt.url, instance_id=self._instance_id)
        except VideoRecordingError as exc:
            if exc.code != "stale_file":
                raise
        self._deleted = True
        self.client.bridge._trace_record("video_recording_stopped", self.metadata.to_dict())
        return self.metadata
