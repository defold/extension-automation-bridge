"""Dependency-free Python client for the Automation Bridge runtime API."""

import json
import difflib
import os
import signal
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple, Union

from .nodes import Node
from .receipts import ObservationReceipt, ScreenshotReceipt
from .waits import RetryExceptions, WaitTimeoutError, wait_until
from .events import CommandTimeout, Event, EventStream, StateSnapshot, select_state_path


JsonDict = Dict[str, Any]
Target = Union[Node, str, Mapping[str, Any], Sequence[float]]
_SCREEN_DIMENSION_MAX = 0x7FFFFFFF
_INPUT_EASINGS = {"linear", "ease_in", "ease_out", "ease_in_out"}


class AutomationBridgeError(RuntimeError):
    """Base class for custom Automation Bridge wrapper errors."""

    pass


class HttpError(AutomationBridgeError):
    """Raised for transport failures, invalid JSON, or unexpected raw responses."""

    def __init__(self, method: str, url: str, message: str, status: Optional[int] = None):
        self.method = method
        self.url = url
        self.status = status
        super().__init__(f"{method} {url} failed: {message}")


class AutomationBridgeApiError(AutomationBridgeError):
    """Raised when the native Automation Bridge API returns `{ "ok": false }`."""

    def __init__(self, code: str, message: str, status: int, response: Mapping[str, Any]):
        self.code = code
        self.message = message
        self.status = status
        self.response = response
        super().__init__(f"Automation Bridge API error {status} {code}: {message}")


class SelectorError(AutomationBridgeError):
    """Raised when a node selector finds too many or too few nodes."""

    pass


class InputExecutionError(AutomationBridgeError):
    """Raised when an accepted native input is cancelled or fails before release."""

    def __init__(self, receipt: "InputReceipt"):
        self.receipt = receipt
        super().__init__(
            f"input {receipt.input_id} ended as {receipt.state}: "
            f"{receipt.get('reason') or 'no reason reported'}"
        )


class InputReceipt(dict):
    """Dictionary-compatible native input lifecycle receipt with attribute access."""

    def __getattr__(self, name: str) -> Any:
        try:
            return self[name]
        except KeyError as exc:
            raise AttributeError(name) from exc

    @property
    def input_id(self) -> int:
        return int(self["input_id"])

    @property
    def state(self) -> str:
        return str(self["state"])


class EngineLogStream:
    """Context-managed reader for Defold's TCP log service."""

    def __init__(
        self,
        host: str,
        port: int,
        timeout: float = 2.0,
        read_timeout: Optional[float] = None,
    ):
        self.host = host
        self.port = int(port)
        self._socket: Optional[socket.socket] = socket.create_connection(
            (host, self.port), timeout=timeout
        )
        self._buffer = bytearray()
        try:
            status = self._readline_raw(timeout).decode("utf-8", "replace").rstrip("\r\n")
            if status != "0 OK":
                raise AutomationBridgeError(f"log service rejected connection: {status}")
            self._socket.settimeout(read_timeout)
        except BaseException:
            _cleanup_without_masking(self.close)
            raise

    def __enter__(self) -> "EngineLogStream":
        return self

    def __exit__(self, exc_type: Any, exc: Any, traceback: Any) -> None:
        if exc_type is None:
            self.close()
        else:
            _cleanup_without_masking(self.close)

    def __iter__(self) -> "EngineLogStream":
        return self

    def __next__(self) -> str:
        line = self.readline()
        if line is None:
            raise StopIteration
        return line

    def close(self) -> None:
        """Close the underlying log service socket."""
        sock = self._socket
        self._socket = None
        if sock is None:
            return
        try:
            sock.close()
        except OSError:
            pass

    def readline(self, timeout: Optional[float] = None) -> Optional[str]:
        """Read one log line without its trailing newline; return `None` on timeout or EOF."""
        data = self._readline_raw(timeout)
        if not data:
            return None
        return data.decode("utf-8", "replace").rstrip("\r\n")

    def _readline_raw(self, timeout: Optional[float] = None) -> bytes:
        sock = self._socket
        if sock is None:
            return b""
        previous_timeout = sock.gettimeout()
        timeout_changed = timeout is not None
        if timeout is not None:
            sock.settimeout(timeout)
        try:
            while True:
                newline = self._buffer.find(b"\n")
                if newline >= 0:
                    line = bytes(self._buffer[: newline + 1])
                    del self._buffer[: newline + 1]
                    return line

                chunk = sock.recv(4096)
                if not chunk:
                    if not self._buffer:
                        return b""
                    line = bytes(self._buffer)
                    self._buffer.clear()
                    return line
                self._buffer.extend(chunk)
        except socket.timeout:
            return b""
        except BaseException:
            _cleanup_without_masking(self.close)
            raise
        finally:
            if timeout_changed and self._socket is sock:
                unwinding = sys.exc_info()[0] is not None
                try:
                    sock.settimeout(previous_timeout)
                except OSError:
                    # Concurrent socket closure makes restoration irrelevant.
                    pass
                except BaseException:
                    # Socket closure or a second interruption during restoration
                    # must not replace the exception already leaving recv().
                    if not unwinding:
                        raise


def _cleanup_without_masking(cleanup: Any) -> None:
    """Run best-effort cleanup while preserving an already-active exception."""
    try:
        cleanup()
    except BaseException:
        pass


class InputInterruptionScope:
    """Flush this client's input session if an enclosed operation is interrupted."""

    def __init__(self, controller: "InputController", flush: bool, release: bool):
        self._controller = controller
        self.flush = flush
        self.release = release

    def __enter__(self) -> "InputInterruptionScope":
        return self

    def __exit__(self, exc_type: Any, exc: Any, traceback: Any) -> None:
        if exc_type is None:
            return
        if self.flush:
            _cleanup_without_masking(lambda: self._controller.flush(release=self.release))


class InputController:
    """Queue, receipt, controller-lease, cancellation, and device operations."""

    def __init__(self, bridge: "AutomationBridgeClient"):
        self._bridge = bridge

    def configure(
        self,
        device: str = "auto",
        visualize: Optional[bool] = None,
        lease: float = 5.0,
    ) -> JsonDict:
        """Acquire/renew control and configure the default exclusive input device."""
        params = self._bridge._input_params(lease=lease)
        params["device"] = device
        if visualize is not None:
            params["visualize"] = visualize
        return self._bridge.put_json("/input/configure", params)

    def pending(self) -> List[InputReceipt]:
        """Return FIFO-ordered accepted/started input receipts."""
        data = self._bridge.get("/input/pending")
        return [InputReceipt(item) for item in data.get("inputs", [])]

    def status(self, input_id: int) -> InputReceipt:
        """Return the current or bounded-history receipt for `input_id`."""
        return InputReceipt(self._bridge.get("/input/status", {"input_id": input_id}))

    def wait(
        self,
        input_or_id: Union[int, Mapping[str, Any]],
        state: str = "released",
        timeout: float = 10.0,
        interval: float = 0.01,
        cancel_on_interrupt: bool = True,
        flush_on_interrupt: bool = False,
    ) -> InputReceipt:
        """Wait for native `started` or `released`; cancellation/failure raises."""
        if state not in {"accepted", "started", "released"}:
            raise ValueError("input wait state must be accepted, started, or released")
        receipt = InputReceipt(input_or_id) if isinstance(input_or_id, Mapping) else None
        input_id = int(receipt["input_id"] if receipt is not None else input_or_id)
        if state == "accepted" and receipt is not None:
            return receipt
        deadline = time.monotonic() + max(0.0, timeout)
        last = receipt
        try:
            while True:
                if last is None or last.state == "accepted" or state == "released":
                    last = self.status(input_id)
                if last.state in {"cancelled", "failed"}:
                    raise InputExecutionError(last)
                if state == "started" and last.state in {"started", "released"}:
                    return last
                if state == "released" and last.state == "released":
                    return last
                if time.monotonic() >= deadline:
                    raise AutomationBridgeError(
                        f"input {input_id} did not reach {state!r} within {timeout}s; "
                        f"last state was {last.state!r}"
                    )
                time.sleep(max(0.0, min(interval, deadline - time.monotonic())))
        except BaseException:
            if cancel_on_interrupt:
                def cleanup() -> None:
                    if flush_on_interrupt:
                        self.flush(release=True)
                    else:
                        self.cancel(input_id, release=True)

                # Keep cleanup failures from replacing the original timeout,
                # cancellation, KeyboardInterrupt, or API error.
                _cleanup_without_masking(cleanup)
            raise

    def cancel(self, input_id: int, release: bool = True) -> InputReceipt:
        """Request cancellation, releasing active pointer/key state by default."""
        params = self._bridge._input_params()
        params.update({"input_id": input_id, "release": release})
        return InputReceipt(self._bridge.post_json("/input/cancel", params))

    def flush(self, release: bool = True) -> JsonDict:
        """Cancel this session's active and later queued inputs."""
        params = self._bridge._input_params()
        params["release"] = release
        return self._bridge.post_json("/input/flush", params)

    def interruption_scope(
        self,
        flush: bool = True,
        release: bool = True,
    ) -> InputInterruptionScope:
        """Return a context that releases input if any enclosed operation fails.

        This is useful when an interruption may happen outside an input wait,
        for example while waiting for an application event or screenshot after
        queueing input with ``wait=False``. With ``flush=True`` (the default),
        both the active action and later actions owned by this client session
        are cancelled. Cleanup failures never mask the original exception.
        """
        return InputInterruptionScope(self, flush=flush, release=release)


class PointerSession:
    """Leased low-level pointer that guarantees up/cancel cleanup in a context manager."""

    def __init__(self, bridge: "AutomationBridgeClient", receipt: InputReceipt, lease: float):
        self._bridge = bridge
        self.receipt = receipt
        self.lease = lease
        self.closed = False

    @property
    def input_id(self) -> int:
        return self.receipt.input_id

    def __enter__(self) -> "PointerSession":
        return self

    def __exit__(self, exc_type: Any, exc: Any, traceback: Any) -> None:
        if self.closed:
            return
        if exc_type is not None:
            _cleanup_without_masking(self.cancel)
            return
        self.up()

    def move(
        self,
        target: Target,
        duration: float = 0.2,
        easing: str = "linear",
    ) -> InputReceipt:
        """Append one continuous movement segment without releasing the pointer."""
        self._ensure_open()
        x, y = self._bridge._point(target)
        params = self._bridge._input_params(lease=max(5.0, self.lease))
        params.update(
            {
                "input_id": self.input_id,
                "x": x,
                "y": y,
                "duration": duration,
                "easing": easing,
                "pointer_lease": self.lease,
            }
        )
        self.receipt = InputReceipt(self._bridge.post_json("/input/pointer/move", params))
        return self.receipt

    def hold(self, duration: float) -> InputReceipt:
        """Keep the pointer down at its current position for `duration`."""
        self._ensure_open()
        params = self._bridge._input_params(lease=max(5.0, self.lease))
        params.update(
            {
                "input_id": self.input_id,
                "duration": duration,
                "pointer_lease": self.lease,
            }
        )
        self.receipt = InputReceipt(self._bridge.post_json("/input/pointer/hold", params))
        return self.receipt

    def up(self, wait: Union[str, bool] = "released", timeout: float = 10.0) -> InputReceipt:
        """Request one final up event and optionally wait for native release injection."""
        self._ensure_open()
        params = self._bridge._input_params(lease=max(5.0, self.lease))
        params.update({"input_id": self.input_id, "pointer_lease": self.lease})
        self.receipt = InputReceipt(self._bridge.post_json("/input/pointer/up", params))
        self.closed = True
        if wait:
            target_state = "released" if wait is True else str(wait)
            self.receipt = self._bridge.input.wait(self.receipt, target_state, timeout=timeout)
        return self.receipt

    def cancel(self, release: bool = True) -> InputReceipt:
        """Cancel the session and release/cancel the active contact."""
        if self.closed:
            return self.receipt
        self.receipt = self._bridge.input.cancel(self.input_id, release=release)
        self.closed = True
        return self.receipt

    def _ensure_open(self) -> None:
        if self.closed:
            raise AutomationBridgeError(f"pointer session {self.input_id} is closed")


def request_json(
    url: str,
    method: str = "GET",
    timeout: float = 10.0,
    data: Optional[bytes] = None,
    headers: Optional[Mapping[str, str]] = None,
    json_body: Optional[Mapping[str, Any]] = None,
) -> Tuple[int, JsonDict]:
    """Request JSON and return `(status, object)`."""
    request_headers = dict(headers or {})
    if json_body is not None:
        if data is not None:
            raise ValueError("request_json accepts either data or json_body, not both")
        data = json.dumps(json_body, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        request_headers["Content-Type"] = "application/json"
    request = urllib.request.Request(url, data=data, headers=request_headers, method=method)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            status = response.getcode()
            body = response.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        status = exc.code
        body = exc.read().decode("utf-8", "replace")
    except urllib.error.URLError as exc:
        raise HttpError(method, url, str(exc)) from exc
    except OSError as exc:
        raise HttpError(method, url, str(exc)) from exc

    try:
        parsed = json.loads(body)
    except json.JSONDecodeError as exc:
        preview = body[:200].replace("\n", "\\n")
        raise HttpError(method, url, f"invalid JSON response: {exc}; body starts with {preview!r}", status=status) from exc

    if not isinstance(parsed, dict):
        raise HttpError(method, url, "JSON response was not an object", status=status)
    return status, parsed


def request_raw(url: str, method: str = "GET", timeout: float = 10.0) -> Tuple[int, bytes]:
    """Request raw bytes and return `(status, body)`."""
    request = urllib.request.Request(url, method=method)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.getcode(), response.read()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read()
    except urllib.error.URLError as exc:
        raise HttpError(method, url, str(exc)) from exc
    except OSError as exc:
        raise HttpError(method, url, str(exc)) from exc


def request_bytes(url: str, data: bytes, method: str = "POST", timeout: float = 10.0) -> Tuple[int, bytes]:
    """POST bytes and return `(status, body)`."""
    request = urllib.request.Request(url, data=data, method=method)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.getcode(), response.read()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read()
    except urllib.error.URLError as exc:
        raise HttpError(method, url, str(exc)) from exc
    except OSError as exc:
        raise HttpError(method, url, str(exc)) from exc


def _normalize_include(include: Optional[Union[str, Iterable[str]]]) -> Optional[str]:
    if include is None:
        return None
    if isinstance(include, str):
        return include
    return ",".join(include)


def _encode_param(value: Any) -> str:
    if isinstance(value, bool):
        return "1" if value else "0"
    return str(value)


def _protobuf_varint(value: int) -> bytes:
    if not isinstance(value, int):
        raise TypeError(f"protobuf varint value must be int, got {type(value).__name__}")
    if value < 0:
        raise ValueError("protobuf varint value must be non-negative")

    encoded = bytearray()
    while value >= 0x80:
        encoded.append((value & 0x7F) | 0x80)
        value >>= 7
    encoded.append(value)
    return bytes(encoded)


def _protobuf_string(field: int, value: str) -> bytes:
    if not isinstance(value, str):
        raise TypeError(f"string field {field} must be str, got {type(value).__name__}")
    encoded = value.encode("utf-8")
    return _protobuf_varint((field << 3) | 2) + _protobuf_varint(len(encoded)) + encoded


def _validate_screen_size(width: int, height: int) -> None:
    if not isinstance(width, int) or not isinstance(height, int):
        raise TypeError(f"resize dimensions must be ints, got {type(width).__name__}x{type(height).__name__}")
    if width <= 0 or height <= 0:
        raise ValueError(f"resize dimensions must be positive, got {width}x{height}")
    if width > _SCREEN_DIMENSION_MAX or height > _SCREEN_DIMENSION_MAX:
        raise ValueError(f"resize dimensions must fit platform window limits, got {width}x{height}")


def _encode_system_reboot(args: Sequence[str]) -> bytes:
    if len(args) > 6:
        raise ValueError("Defold reboot accepts at most six arguments")

    payload = bytearray()
    for index, arg in enumerate(args, start=1):
        payload.extend(_protobuf_string(index, arg))
    return bytes(payload)


class AutomationBridgeClient:
    """High-level client for `/automation-bridge/v1` scene inspection and input control."""

    _SERVER_FILTERS = {
        "id",
        "instance_id",
        "logical_id",
        "type",
        "type_exact",
        "name",
        "name_exact",
        "text",
        "text_exact",
        "url",
        "url_exact",
        "path",
        "enabled",
        "kind",
        "has_bounds",
        "visible_and_enabled",
        "visible",
        "case_sensitive",
        "automation_id",
        "localization_key",
        "role",
    }
    _CLIENT_FILTERS: set = set()
    _SELECTOR_KEYS = _SERVER_FILTERS | {"include", "limit", "offset", "cursor"}

    def __init__(
        self,
        port: int,
        timeout: float = 10.0,
        remotery_url: Optional[str] = None,
        client_id: Optional[str] = None,
        session_id: Optional[str] = None,
    ):
        """Create a client with stable controller/session correlation ids."""
        self.port = int(port)
        self.timeout = timeout
        self.base_url = f"http://127.0.0.1:{self.port}/automation-bridge/v1"
        self._remotery_url = remotery_url
        self._last_window_size: Optional[Tuple[int, int]] = None
        self._last_scene_sequence: Optional[int] = None
        # Defold v1 exposes query-only control endpoints with a bounded resource
        # string, so defaults are compact while remaining random per process/session.
        self.client_id = client_id or f"py-{uuid.uuid4().hex[:12]}"
        self.session_id = session_id or f"s-{uuid.uuid4().hex[:12]}"
        self._input_controller = InputController(self)
        self._active_traces: List[Any] = []

    @property
    def input(self) -> InputController:
        """Return queue, status, cancellation, lease, and device controls."""
        return self._input_controller

    @classmethod
    def from_editor(cls, editor: Any, build: bool = True, timeout: float = 20.0) -> "AutomationBridgeClient":
        """Build through `editor`, discover the engine port, and wait for health."""
        if build:
            cls._close_candidate_engine_ports(editor)
            time.sleep(0.5)
            editor.build()

        def bridge_after_build() -> Optional["AutomationBridgeClient"]:
            service_ports = editor.engine_service_ports() if hasattr(editor, "engine_service_ports") else [editor.engine_service_port()]
            remotery_url = cls._editor_remotery_url(editor, fresh_build=build)
            for service_port in service_ports:
                if not service_port:
                    continue
                try:
                    bridge = cls(service_port, remotery_url=remotery_url)
                    bridge.health()
                except AutomationBridgeError:
                    continue
                if hasattr(editor, "remember_engine_service_port"):
                    editor.remember_engine_service_port(service_port)
                if remotery_url and hasattr(editor, "remember_remotery_url"):
                    editor.remember_remotery_url(remotery_url)
                return bridge
            return None

        if build and cls._last_build_missing_engine_service_port(editor):
            return cls._recover_after_stale_build(editor, bridge_after_build, timeout)

        try:
            return wait_until(
                bridge_after_build,
                timeout=timeout,
                interval=0.1,
                message="Automation Bridge endpoint did not become ready",
                retry_exceptions=(AutomationBridgeError,),
            )
        except WaitTimeoutError:
            if not build:
                raise

        return cls._recover_after_stale_build(editor, bridge_after_build, timeout)

    @classmethod
    def _recover_after_stale_build(cls, editor: Any, bridge_after_build: Any, timeout: float) -> "AutomationBridgeClient":
        cls._close_candidate_engine_ports(editor)
        time.sleep(0.5)
        editor.build()
        return wait_until(
            bridge_after_build,
            timeout=timeout,
            interval=0.1,
            message="Automation Bridge endpoint did not become ready after engine restart",
            retry_exceptions=(AutomationBridgeError,),
        )

    @staticmethod
    def _last_build_missing_engine_service_port(editor: Any) -> bool:
        if not hasattr(editor, "last_build_had_engine_service_port"):
            return False
        return editor.last_build_had_engine_service_port() is False

    @staticmethod
    def _editor_remotery_url(editor: Any, fresh_build: bool) -> Optional[str]:
        if fresh_build and hasattr(editor, "latest_registration_remotery_urls"):
            urls = editor.latest_registration_remotery_urls()
            return urls[0] if urls else None
        if hasattr(editor, "remotery_url"):
            return editor.remotery_url()
        return None

    @classmethod
    def from_project(cls, root: Union[str, Path] = ".", build: bool = True, timeout: float = 20.0) -> "AutomationBridgeClient":
        """Create an editor client from `root`, then connect to the Automation Bridge API."""
        from .editor import EditorClient

        editor = EditorClient.from_project(root)
        return cls.from_editor(editor, build=build, timeout=timeout)

    def wait_ready(
        self,
        timeout: float = 20.0,
        retry_exceptions: RetryExceptions = (AutomationBridgeError,),
    ) -> JsonDict:
        """Wait until `/health` succeeds and return the health payload."""
        return wait_until(
            self.health,
            timeout=timeout,
            interval=0.1,
            message="Automation Bridge endpoint did not become ready",
            retry_exceptions=retry_exceptions,
            scene_sequence=lambda: getattr(self, "_last_scene_sequence", None),
        )

    def get(self, path: str, params: Optional[Mapping[str, Any]] = None) -> JsonDict:
        """GET an Automation Bridge API path and return its `data` object."""
        return self._request("GET", path, params)

    def post(self, path: str, params: Optional[Mapping[str, Any]] = None) -> JsonDict:
        """POST an Automation Bridge API path with query parameters and return `data`."""
        return self._request("POST", path, params)

    def post_json(self, path: str, payload: Mapping[str, Any]) -> JsonDict:
        """POST an `application/json` object for structured Automation Bridge operations."""
        return self._request("POST", path, json_body=payload)

    def put_json(self, path: str, payload: Mapping[str, Any]) -> JsonDict:
        """PUT an `application/json` object for structured Automation Bridge operations."""
        return self._request("PUT", path, json_body=payload)

    def put(self, path: str, params: Optional[Mapping[str, Any]] = None) -> JsonDict:
        """PUT an Automation Bridge API path with query parameters and return `data`."""
        return self._request("PUT", path, params)

    def delete(self, path: str, params: Optional[Mapping[str, Any]] = None) -> JsonDict:
        """DELETE an Automation Bridge API path and return its `data` object."""
        return self._request("DELETE", path, params)

    def health(self) -> JsonDict:
        """Return API version, capabilities, platform, and screen data."""
        data = self.get("/health")
        screen = data.get("screen")
        if isinstance(screen, Mapping):
            self._remember_window_size(screen)
        return data

    def screen(self) -> JsonDict:
        """Return window, backbuffer, viewport, and coordinate metadata."""
        data = self.get("/screen")
        self._remember_window_size(data)
        return data

    def scene(
        self,
        visible: Optional[bool] = None,
        include: Optional[Union[str, Iterable[str]]] = None,
    ) -> JsonDict:
        """Return the runtime scene tree."""
        params: Dict[str, Any] = {}
        if visible is not None:
            params["visible"] = visible
        normalized_include = _normalize_include(include)
        if normalized_include:
            params["include"] = normalized_include
        return self.get("/scene", params)

    def nodes(self, **selector: Any) -> List[Node]:
        """Return one server-filtered page of nodes, with exact filters applied natively."""
        nodes, _, _ = self._select_nodes(selector)
        return nodes

    def node(self, **selector: Any) -> Node:
        """Return exactly one matching node or raise `SelectorError`."""
        nodes, metadata, selector_text = self._select_nodes(selector)
        if len(nodes) == 1:
            return nodes[0]
        error = SelectorError(self._selector_error("expected exactly one node", selector, selector_text, nodes, metadata))
        self._trace_record("selector_error", {"selector": selector, "error": str(error)})
        raise error

    def maybe_node(self, **selector: Any) -> Optional[Node]:
        """Return zero or one matching node, raising if multiple nodes match."""
        nodes, metadata, selector_text = self._select_nodes(selector)
        if len(nodes) <= 1:
            return nodes[0] if nodes else None
        error = SelectorError(self._selector_error("expected zero or one node", selector, selector_text, nodes, metadata))
        self._trace_record("selector_error", {"selector": selector, "error": str(error)})
        raise error

    def by_id(
        self,
        id: str,
        include: Optional[Union[str, Iterable[str]]] = "basic,bounds,properties,children",
    ) -> Node:
        """Fetch one node by stable Automation Bridge node id."""
        params: Dict[str, Any] = {"id": id}
        normalized_include = _normalize_include(include)
        if normalized_include:
            params["include"] = normalized_include
        return Node(self.get("/node", params)["node"])

    def parent(
        self,
        node_or_id: Union[Node, str],
        include: Optional[Union[str, Iterable[str]]] = "basic,bounds,properties",
    ) -> Node:
        """Return the parent node for a component or child node."""
        node = node_or_id if isinstance(node_or_id, Node) else self.by_id(node_or_id, include="basic")
        if not node.parent_id:
            raise SelectorError(f"node has no parent: {node.compact()}")
        return self.by_id(node.parent_id, include=include)

    def count(self, **selector: Any) -> int:
        """Return the complete native match count, independent of page size."""
        self._validate_selector(selector)
        params = self._server_params(selector, limit=0)
        return int(self.get("/nodes", params).get("matched", 0))

    def click(
        self,
        target: Union[Target, float, int],
        y: Optional[float] = None,
        wait: Union[str, bool, float] = "released",
        visualize: Optional[bool] = None,
        device: Optional[str] = None,
        pointer_id: int = 0,
        expected_scene_sequence: Optional[int] = None,
        timeout: float = 5.0,
        cancel_on_interrupt: bool = True,
        flush_on_interrupt: bool = False,
    ) -> InputReceipt:
        """Queue one FIFO click and optionally wait for the native release receipt."""
        if isinstance(target, Node):
            params: Dict[str, Any] = {"id": target.id}
        elif isinstance(target, str):
            params = {"id": target}
        else:
            x_value, y_value = self._point(target, y)
            params = {"x": x_value, "y": y_value}

        params.update(self._input_params())
        params.update({"visualize": visualize, "device": device, "expected_scene_sequence": expected_scene_sequence})
        if pointer_id:
            params["pointer_id"] = pointer_id
        receipt = InputReceipt(self.post_json("/input/click", params))
        return self._wait_input_compat(
            receipt, wait, timeout, cancel_on_interrupt, flush_on_interrupt
        )

    def drag(
        self,
        from_target: Target,
        to_target: Target,
        duration: float = 0.35,
        wait: Union[str, bool, float, None] = "released",
        visualize: Optional[bool] = None,
        easing: str = "linear",
        hold_before: float = 0.0,
        hold_after: float = 0.0,
        device: Optional[str] = None,
        pointer_id: int = 0,
        expected_scene_sequence: Optional[int] = None,
        timeout: Optional[float] = None,
        cancel_on_interrupt: bool = True,
        flush_on_interrupt: bool = False,
    ) -> InputReceipt:
        """Queue one FIFO drag and wait on native lifecycle state, never wall-clock guessing."""
        if self._is_node_ref(from_target) and self._is_node_ref(to_target):
            params: Dict[str, Any] = {
                "from_id": self._node_id(from_target),
                "to_id": self._node_id(to_target),
                "duration": duration,
            }
        else:
            x1, y1 = self._point(from_target)
            x2, y2 = self._point(to_target)
            params = {"x1": x1, "y1": y1, "x2": x2, "y2": y2, "duration": duration}

        controller_lease = min(60.0, max(5.0, duration + hold_before + hold_after + 2.0))
        params.update(self._input_params(lease=controller_lease))
        params.update({"visualize": visualize, "device": device, "expected_scene_sequence": expected_scene_sequence})
        if easing != "linear":
            params["easing"] = easing
        if hold_before:
            params["hold_before"] = hold_before
        if hold_after:
            params["hold_after"] = hold_after
        if pointer_id:
            params["pointer_id"] = pointer_id
        receipt = InputReceipt(self.post_json("/input/drag", params))
        return self._wait_input_compat(
            receipt,
            wait,
            timeout or max(5.0, duration + hold_before + hold_after + 5.0),
            cancel_on_interrupt,
            flush_on_interrupt,
        )

    def drag_path(
        self,
        points: Sequence[Target],
        durations: Sequence[float],
        easing: Union[str, Sequence[str]] = "linear",
        hold_before: float = 0.0,
        hold_after: float = 0.0,
        path: str = "sampled",
        visualize: Optional[bool] = None,
        wait: Union[str, bool, float, None] = "released",
        device: Optional[str] = None,
        pointer_id: int = 0,
        expected_scene_sequence: Optional[int] = None,
        timeout: Optional[float] = None,
        cancel_on_interrupt: bool = True,
        flush_on_interrupt: bool = False,
    ) -> InputReceipt:
        """Run one down/path/up gesture with native segment timing, easing, holds, and curves."""
        normalized_points = [self._point(point) for point in points]
        if path not in {"sampled", "linear", "quadratic", "cubic"}:
            raise ValueError("path must be sampled, linear, quadratic, or cubic")
        expected_segments = len(normalized_points) - 1 if path in {"sampled", "linear"} else 1
        if len(normalized_points) < 2:
            raise ValueError("drag_path requires at least two points")
        if path == "quadratic" and len(normalized_points) != 3:
            raise ValueError("quadratic drag_path requires exactly three points")
        if path == "cubic" and len(normalized_points) != 4:
            raise ValueError("cubic drag_path requires exactly four points")
        if len(durations) != expected_segments:
            raise ValueError(f"drag_path requires exactly {expected_segments} durations")
        duration_values = [self._input_duration(value, "duration") for value in durations]
        hold_before = self._input_duration(hold_before, "hold_before")
        hold_after = self._input_duration(hold_after, "hold_after")
        total_duration = sum(duration_values) + hold_before + hold_after
        if total_duration > 60.0:
            raise ValueError("total gesture duration exceeds 60 seconds")
        easing_values = [easing] * expected_segments if isinstance(easing, str) else list(easing)
        if len(easing_values) != expected_segments or any(value not in _INPUT_EASINGS for value in easing_values):
            raise ValueError(f"drag_path requires {expected_segments} supported easing values")
        params = self._input_params(lease=min(60.0, max(5.0, total_duration + 2.0)))
        params.update(
            {
                "points": ";".join(f"{x},{y}" for x, y in normalized_points),
                "durations": ",".join(str(value) for value in duration_values),
                "easing": ",".join(easing_values),
                "hold_before": hold_before,
                "hold_after": hold_after,
                "path": path,
                "visualize": visualize,
                "device": device,
                "pointer_id": pointer_id,
                "expected_scene_sequence": expected_scene_sequence,
            }
        )
        receipt = InputReceipt(self.post_json("/input/drag_path", params))
        return self._wait_input_compat(
            receipt,
            wait,
            timeout or max(5.0, total_duration + 5.0),
            cancel_on_interrupt,
            flush_on_interrupt,
        )

    def pointer(
        self,
        start: Target,
        lease: float = 2.0,
        visualize: Optional[bool] = None,
        device: Optional[str] = None,
        pointer_id: int = 0,
        expected_scene_sequence: Optional[int] = None,
    ) -> PointerSession:
        """Press a leased pointer for continuous `move`/`hold` operations and safe cleanup."""
        lease = self._input_duration(lease, "lease")
        if lease <= 0:
            raise ValueError("lease must be greater than zero")
        x, y = self._point(start)
        params = self._input_params(lease=max(5.0, lease))
        params.update(
            {
                "x": x,
                "y": y,
                "pointer_lease": lease,
                "visualize": visualize,
                "device": device,
                "pointer_id": pointer_id,
                "expected_scene_sequence": expected_scene_sequence,
            }
        )
        receipt = InputReceipt(self.post_json("/input/pointer/open", params))
        return PointerSession(self, receipt, lease)

    def type_text(
        self,
        text: str,
        wait: Union[str, bool] = False,
        expected_scene_sequence: Optional[int] = None,
        timeout: float = 10.0,
        cancel_on_interrupt: bool = True,
        flush_on_interrupt: bool = False,
    ) -> InputReceipt:
        """Queue FIFO text input and optionally wait for native completion."""
        params = self._input_params()
        params.update({"text": text, "expected_scene_sequence": expected_scene_sequence})
        receipt = InputReceipt(self.post_json("/input/key", params))
        return self._wait_input_compat(
            receipt, wait, timeout, cancel_on_interrupt, flush_on_interrupt
        )

    def key(
        self,
        key: str,
        wait: Union[str, bool] = False,
        expected_scene_sequence: Optional[int] = None,
        timeout: float = 10.0,
        cancel_on_interrupt: bool = True,
        flush_on_interrupt: bool = False,
    ) -> InputReceipt:
        """Queue a FIFO special key such as `KEY_ENTER`."""
        keys = key if key.startswith("{") and key.endswith("}") else f"{{{key}}}"
        params = self._input_params()
        params.update({"keys": keys, "expected_scene_sequence": expected_scene_sequence})
        receipt = InputReceipt(self.post_json("/input/key", params))
        return self._wait_input_compat(
            receipt, wait, timeout, cancel_on_interrupt, flush_on_interrupt
        )

    def events(self, from_cursor: Union[str, int] = "now") -> EventStream:
        """Create a cursor subscription, resolving ``'now'`` before returning.

        Use ``with bridge.events('now') as events`` before sending an action to
        avoid the classic subscribe-after-action race.
        """
        return EventStream(self, from_cursor=from_cursor)

    def wait_for_ack(
        self,
        input_id: int,
        timeout: float = 10.0,
        events: Optional[EventStream] = None,
    ) -> Event:
        """Wait for the application's opt-in acknowledgement of ``input_id``.

        Native input release and application acknowledgement are separate
        lifecycle stages. Pass a stream created before the input for strict
        race-free ordering; without one this searches the retained ring.
        """
        stream = events or self.events(from_cursor="oldest")
        try:
            return stream.wait(
                "input.acknowledged",
                where={"input_id": int(input_id)},
                event_type="acknowledgement",
                timeout=timeout,
            )
        finally:
            if events is None:
                stream.close()

    def states(self, name: Optional[str] = None) -> JsonDict:
        """Return published JSON states and the latest global revision."""
        return self.get("/state", {"name": name} if name is not None else None)

    def state(self, name: str) -> StateSnapshot:
        """Return one published state by its exact application name."""
        entries = self.states(name=name).get("states", [])
        if len(entries) != 1 or not isinstance(entries[0], Mapping):
            raise AutomationBridgeError(f"published state {name!r} is unavailable")
        return StateSnapshot(entries[0])

    def wait_for_state(
        self,
        path: str,
        expected: Any,
        timeout: float = 10.0,
        after_revision: Optional[int] = None,
        state_name: Optional[str] = None,
    ) -> StateSnapshot:
        """Wait for a JSON state path to equal ``expected`` without missing revisions.

        Pass ``after_revision=bridge.state(name).revision`` when an already
        matching value must not satisfy a wait for a *new* publication.
        ``state_name`` disambiguates a path before that state has first appeared.
        """
        snapshot = self.states()
        entries = [item for item in snapshot.get("states", []) if isinstance(item, Mapping)]
        current_revision = int(snapshot.get("revision", 0))
        selected = select_state_path(entries, path, state_name=state_name)
        if after_revision is None and selected is not None and selected.value == expected:
            return selected
        cursor = current_revision if after_revision is None else int(after_revision)
        deadline = time.monotonic() + timeout
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                observed = selected.value if selected is not None else "<unpublished>"
                raise TimeoutError(
                    f"state {path!r} did not become {expected!r} within {timeout:g}s; "
                    f"last value={observed!r}, revision={cursor}"
                )
            safe_wait = min(remaining, max(0.0, float(self.timeout) - 0.1), 1.0)
            changed = self.get(
                "/state/wait",
                {"after_revision": cursor, "timeout_ms": int(safe_wait * 1000), "name": state_name},
            )
            cursor = max(cursor, int(changed.get("revision", cursor)))
            changed_entries = [item for item in changed.get("states", []) if isinstance(item, Mapping)]
            candidate = select_state_path(changed_entries, path, state_name=state_name)
            if candidate is not None:
                selected = candidate
                if candidate.value == expected and candidate.revision > (after_revision or 0):
                    return candidate

    def start_command(self, name: str, data: Any = None, timeout: float = 30.0) -> JsonDict:
        """Submit a registered named Lua command and return its pending id."""
        if timeout <= 0 or timeout > 300:
            raise ValueError("command timeout must be greater than 0 and at most 300 seconds")
        payload = json.dumps({} if data is None else data, allow_nan=False, separators=(",", ":"))
        if len(payload.encode("utf-8")) > 32768:
            raise ValueError("command JSON payload exceeds 32768 bytes")
        return self.post(
            "/commands",
            {"name": name, "data": payload, "timeout_ms": max(1, int(timeout * 1000))},
        )

    def command_status(self, command_id: int) -> JsonDict:
        """Return command state, JSON result, error, and timestamps."""
        return self.get("/commands", {"id": int(command_id)})

    def cancel_command(self, command_id: int) -> JsonDict:
        """Cancel a queued command; running Lua callbacks are never preempted."""
        return self.delete("/commands", {"id": int(command_id)})

    def wait_for_command(self, command_id: int, timeout: float = 30.0, interval: float = 0.02) -> JsonDict:
        """Wait for a command result, cancelling a still-pending command on timeout."""
        deadline = time.monotonic() + timeout
        terminal = {"completed", "failed", "cancelled", "timed_out"}
        while True:
            status = self.command_status(command_id)
            if status.get("state") in terminal:
                return status
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                cancellation_error: Optional[BaseException] = None
                try:
                    self.cancel_command(command_id)
                except AutomationBridgeError as exc:
                    cancellation_error = exc
                raise CommandTimeout(command_id, timeout, cancellation_error) from cancellation_error
            time.sleep(min(interval, remaining))

    def command(self, name: str, data: Any = None, timeout: float = 30.0) -> JsonDict:
        """Run a registered command and return its terminal result record."""
        accepted = self.start_command(name, data=data, timeout=timeout)
        result = self.wait_for_command(int(accepted["command_id"]), timeout=timeout)
        if result.get("state") != "completed":
            raise AutomationBridgeError(
                f"command {result.get('command_id')} {result.get('state')}: {result.get('error')}"
            )
        return result

    def mark(
        self,
        name: str,
        data: Any = None,
        recording_timestamp_us: Optional[int] = None,
    ) -> JsonDict:
        """Add a trace marker with native and host recording-clock timestamps."""
        payload = json.dumps({} if data is None else data, allow_nan=False, separators=(",", ":"))
        if len(payload.encode("utf-8")) > 32768:
            raise ValueError("marker JSON payload exceeds 32768 bytes")
        if recording_timestamp_us is None:
            recording_timestamp_us = time.monotonic_ns() // 1000
        return self.post(
            "/markers",
            {"name": name, "data": payload, "recording_timestamp_us": int(recording_timestamp_us)},
        )

    def screenshot(
        self,
        wait: bool = True,
        timeout: float = 5.0,
        after_frames: int = 0,
        retry_exceptions: RetryExceptions = (),
    ) -> ScreenshotReceipt:
        """Schedule an atomic PNG capture and return its native completion receipt."""
        if not isinstance(after_frames, int) or after_frames < 0 or after_frames > 600:
            raise ValueError("after_frames must be an integer from 0 through 600")
        response = self.get("/screenshot", {"after_frames": after_frames})
        receipt = ScreenshotReceipt(response)
        if not wait:
            return receipt
        if receipt.capture_id <= 0:
            raise AutomationBridgeError("native screenshot response did not include a capture_id")

        def completed() -> Optional[ScreenshotReceipt]:
            current = ScreenshotReceipt(self.get("/screenshot/status", {"capture_id": receipt.capture_id}))
            if current.state == "failed":
                raise AutomationBridgeError(
                    f"screenshot {current.capture_id} failed: {current.failure_reason or 'unknown reason'}"
                )
            return current if current.state == "complete" else None

        return wait_until(
            completed,
            timeout=timeout,
            interval=0.02,
            message=f"screenshot capture {receipt.capture_id} did not complete",
            retry_exceptions=retry_exceptions,
            scene_sequence=lambda: self._last_scene_sequence,
        )

    def convert_point(
        self,
        point: Union[Mapping[str, Any], Sequence[float]],
        from_space: str,
        to_space: str,
    ) -> Mapping[str, Any]:
        """Convert a top-left-origin point through native window/viewport geometry."""
        x, y = self._point(point)
        data = self.post_json(
            "/coordinates/convert",
            {"point": {"x": x, "y": y}, "from_space": from_space, "to_space": to_space},
        )
        converted = data.get("point")
        if not isinstance(converted, Mapping):
            raise AutomationBridgeError("coordinate conversion response did not include a point")
        return converted

    def engine_info(self) -> JsonDict:
        """Return Defold engine service `/info`, including version, platform, sha1, and log port."""
        status, response = request_json(f"http://127.0.0.1:{self.port}/info", timeout=self.timeout)
        if status < 200 or status >= 300:
            raise HttpError("GET", f"http://127.0.0.1:{self.port}/info", f"unexpected status {status}", status=status)
        return response

    def engine_log_port(self) -> int:
        """Return the Defold TCP log service port from engine `/info`."""
        value = self.engine_info().get("log_port")
        try:
            port = int(value)
        except (TypeError, ValueError) as exc:
            raise AutomationBridgeError(f"engine /info did not include a valid log_port: {value!r}") from exc
        if port <= 0:
            raise AutomationBridgeError(f"engine log service is unavailable: {value!r}")
        return port

    def log_stream(
        self,
        timeout: float = 2.0,
        read_timeout: Optional[float] = None,
        host: str = "127.0.0.1",
    ) -> EngineLogStream:
        """Open Defold's TCP log stream. Use as `with bridge.log_stream() as logs:`."""
        return EngineLogStream(host, self.engine_log_port(), timeout=timeout, read_timeout=read_timeout)

    def read_logs(
        self,
        duration: float = 1.0,
        limit: Optional[int] = None,
        idle_timeout: float = 0.1,
    ) -> List[str]:
        """Collect future engine log lines for `duration` seconds or until `limit` lines are read."""
        lines: List[str] = []
        deadline = time.monotonic() + max(0.0, duration)
        with self.log_stream(timeout=self.timeout) as logs:
            while time.monotonic() < deadline:
                if limit is not None and len(lines) >= limit:
                    break
                remaining = deadline - time.monotonic()
                line = logs.readline(timeout=max(0.0, min(idle_timeout, remaining)))
                if line is not None:
                    lines.append(line)
        return lines

    @property
    def profiler(self) -> "ProfilerClient":
        """Return a client for Defold's built-in engine profiler endpoints."""
        from .profiler import ProfilerClient

        return ProfilerClient(self.port, timeout=self.timeout, remotery_url=self._remotery_url)

    @property
    def gestures(self) -> "GestureGenerator":
        """Return deterministic, dependency-free generated gesture helpers."""
        from .gestures import GestureGenerator

        return GestureGenerator(self)

    def trace(
        self,
        path: Union[str, Path],
        *,
        screenshots: str = "on_error",
        screenshot_directory: Optional[Union[str, Path]] = None,
        prerequisites: Optional[Mapping[str, Any]] = None,
    ) -> "TraceSession":
        """Create a diagnostic trace context with explicitly best-effort replay."""
        from .trace import TraceSession

        return TraceSession(
            self,
            path,
            screenshots=screenshots,
            screenshot_directory=screenshot_directory,
            prerequisites=prerequisites,
        )

    def recording_capabilities(self, backend: Optional[Any] = None) -> Any:
        """Return explicit optional recorder capabilities without starting it."""
        if backend is None:
            from .recording import default_backend

            backend = default_backend()
        return backend.capabilities()

    def recording_permission_diagnostics(self, backend: Optional[Any] = None) -> Mapping[str, Any]:
        """Return recorder availability and platform permission guidance."""
        if backend is None:
            from .recording import default_backend

            backend = default_backend()
        return backend.permission_diagnostics()

    def record_video(
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
        """Start an optional platform recording and return its safe session.

        `crop="content"` uses a rectangle advertised by `/screen`. Exact
        application audio is capability-gated and is never replaced by a
        microphone recording.
        """
        from .recording import RecordingOptions, default_backend

        selected_backend = backend or default_backend()
        if crop == "content" and source_rect is None:
            source_rect = self._recording_content_rect(self.screen())
        options = RecordingOptions(
            path=Path(path), size=size, fps=fps, video_codec=video_codec,
            audio=audio, audio_codec=audio_codec, application=application,
            window_title=window_title, crop=crop, source_rect=source_rect,
            exclude_titlebar=exclude_titlebar, extra_args=tuple(extra_args),
        )
        session = selected_backend.start(options)
        if hasattr(session, "_trace_callback"):
            session._trace_callback = self._trace_record
        self._trace_record("recording_started", {"path": str(path), "options": options})
        return session

    @property
    def remotery_url(self) -> Optional[str]:
        """Return the Remotery websocket URL discovered while bootstrapping, if known."""
        return self._remotery_url

    @property
    def last_window_size(self) -> Optional[Tuple[int, int]]:
        """Return the last known `(width, height)` from `screen()`, `health()`, or `resize()`."""
        return self._last_window_size

    def resize(self, width: int, height: int, wait: float = 0.25) -> JsonDict:
        """Request a resize and return requested, window, viewport, and outcome data."""
        _validate_screen_size(width, height)
        capabilities = self.health().get("capabilities", [])
        if not isinstance(capabilities, (list, tuple, set)) or "screen.resize" not in capabilities:
            raise AutomationBridgeError("Automation Bridge endpoint does not advertise the screen.resize capability")

        response = self.put_json("/screen", {"width": width, "height": height})
        screen_value = response.get("screen") if isinstance(response, Mapping) else None
        screen = dict(screen_value) if isinstance(screen_value, Mapping) else dict(response)
        self._remember_window_size(screen)
        if wait:
            deadline = time.monotonic() + wait
            while time.monotonic() < deadline:
                screen = self.screen()
                window = screen.get("window")
                if isinstance(window, Mapping) and window.get("width") == width and window.get("height") == height:
                    break
                time.sleep(min(0.02, max(0.0, deadline - time.monotonic())))
        window = screen.get("window") if isinstance(screen, Mapping) else None
        observed_width = None
        observed_height = None
        if isinstance(window, Mapping):
            observed_width = window.get("width")
            observed_height = window.get("height")
        window_matches = observed_width == width and observed_height == height
        backbuffer = screen.get("backbuffer") if isinstance(screen, Mapping) else None
        backbuffer_matches = isinstance(backbuffer, Mapping) and backbuffer.get("width") == width and backbuffer.get("height") == height
        result = dict(response)
        result.update(
            {
                "width": observed_width if isinstance(observed_width, int) else width,
                "height": observed_height if isinstance(observed_height, int) else height,
                "requested": {"width": width, "height": height},
                "screen": screen,
                "window_matches": window_matches,
                "backbuffer_matches": backbuffer_matches,
            }
        )
        if not window_matches:
            result["outcome"] = "requested_not_observed_before_timeout"
        elif result.get("outcome") == "requested_not_observed":
            result["outcome"] = "resized"
        elif "outcome" not in result:
            result["outcome"] = "resized"
        return result

    def set_portrait(self, wait: float = 0.25) -> JsonDict:
        """Resize to portrait by swapping the last known width and height when needed."""
        width, height = self._known_window_size()
        if width > height:
            return self.resize(height, width, wait=wait)
        return self._unchanged_resize_result(width, height)

    def set_landscape(self, wait: float = 0.25) -> JsonDict:
        """Resize to landscape by swapping the last known width and height when needed."""
        width, height = self._known_window_size()
        if width < height:
            return self.resize(height, width, wait=wait)
        return self._unchanged_resize_result(width, height)

    def _unchanged_resize_result(self, width: int, height: int) -> JsonDict:
        screen = self.screen()
        return {
            "width": width,
            "height": height,
            "requested": {"width": width, "height": height},
            "outcome": "already_correct",
            "window_matches": True,
            "backbuffer_matches": (
                isinstance(screen.get("backbuffer"), Mapping)
                and screen["backbuffer"].get("width") == width
                and screen["backbuffer"].get("height") == height
            ),
            "screen": screen,
        }

    def reboot(self, *args: str, wait: bool = True, timeout: Optional[float] = None) -> None:
        """Reboot the engine through `/post/@system/reboot` with up to six command-line args."""
        payload = _encode_system_reboot(args)
        self._post_engine_message("/post/@system/reboot", payload, timeout=timeout)
        self._last_window_size = None
        if wait:
            wait_timeout = self.timeout if timeout is None else timeout
            self._wait_ready_after_reboot(wait_timeout)

    def close_engine(self, timeout: float = 2.0) -> None:
        """Ask the running Defold engine to exit, falling back to the local listener PID."""
        url = f"http://127.0.0.1:{self.port}/post/@system/exit"
        try:
            self._post_engine_message("/post/@system/exit", b"\010\000", timeout=timeout)
        except AutomationBridgeError:
            if self._terminate_process_on_port(timeout):
                return
            raise

        if self._wait_until_unavailable(timeout):
            return
        if self._terminate_process_on_port(timeout):
            return
        raise HttpError("POST", url, "engine did not stop after exit request")

    @classmethod
    def _close_candidate_engine_ports(cls, editor: Any) -> None:
        service_ports = editor.engine_service_ports() if hasattr(editor, "engine_service_ports") else [editor.engine_service_port()]
        for service_port in service_ports:
            if not service_port:
                continue
            try:
                cls(service_port, timeout=1.0).close_engine(timeout=1.0)
            except AutomationBridgeError:
                continue

    def _wait_until_unavailable(self, timeout: float) -> bool:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                self.health()
            except AutomationBridgeError:
                return True
            time.sleep(0.05)
        return False

    def _wait_ready_after_reboot(self, timeout: float) -> JsonDict:
        deadline = time.monotonic() + timeout
        accept_ready_after = time.monotonic() + min(0.25, max(0.0, timeout) / 2.0)
        saw_unavailable = False
        last_error: Optional[BaseException] = None

        while time.monotonic() <= deadline:
            try:
                data = self.health()
            except AutomationBridgeError as exc:
                saw_unavailable = True
                last_error = exc
            else:
                if saw_unavailable or time.monotonic() >= accept_ready_after:
                    return data

            remaining = deadline - time.monotonic()
            if remaining <= 0.0:
                break
            time.sleep(min(0.05, remaining))

        try:
            data = self.health()
        except AutomationBridgeError as exc:
            last_error = exc
        else:
            if saw_unavailable or time.monotonic() >= accept_ready_after:
                return data

        message = "Automation Bridge endpoint did not become ready after reboot"
        if last_error:
            raise AssertionError(f"{message}: {last_error}") from last_error
        raise AssertionError(message)

    def _terminate_process_on_port(self, timeout: float) -> bool:
        for pid in self._listening_pids(self.port):
            if pid == os.getpid():
                continue
            try:
                os.kill(pid, signal.SIGTERM)
            except OSError:
                continue
        return self._wait_until_unavailable(timeout)

    @staticmethod
    def _listening_pids(port: int) -> List[int]:
        if sys.platform.startswith("win"):
            return []
        try:
            output = subprocess.check_output(
                ["lsof", "-nP", f"-iTCP:{port}", "-sTCP:LISTEN", "-t"],
                stderr=subprocess.DEVNULL,
                text=True,
                timeout=1.0,
            )
        except (OSError, subprocess.SubprocessError):
            return []

        pids: List[int] = []
        for line in output.splitlines():
            try:
                pid = int(line.strip())
            except ValueError:
                continue
            if pid not in pids:
                pids.append(pid)
        return pids

    def dump_scene(
        self,
        path: Optional[Union[str, Path]] = None,
        visible: Optional[bool] = None,
        include: Optional[Union[str, Iterable[str]]] = None,
    ) -> Union[str, Path]:
        """Return scene JSON text or write it to `path`."""
        text = json.dumps(self.scene(visible=visible, include=include), indent=2, sort_keys=True)
        if path is None:
            return text
        output_path = Path(path)
        output_path.write_text(text + "\n", encoding="utf-8")
        return output_path

    def format_nodes(self, nodes: Optional[Iterable[Node]] = None, **selector: Any) -> str:
        """Format nodes as compact one-line diagnostics."""
        selected = list(nodes if nodes is not None else self.nodes(**selector))
        return "\n".join(node.compact() for node in selected)

    def wait_for_node(
        self,
        timeout: float = 10.0,
        interval: float = 0.1,
        retry_exceptions: RetryExceptions = (SelectorError,),
        **selector: Any,
    ) -> Node:
        """Wait for one node, retrying only caller-approved exception types."""
        def one_node() -> Optional[Node]:
            nodes, _, _ = self._select_nodes(selector)
            return nodes[0] if len(nodes) == 1 else None

        return wait_until(
            one_node,
            timeout=timeout,
            interval=interval,
            message=f"node did not appear: {selector}",
            retry_exceptions=retry_exceptions,
            scene_sequence=lambda: getattr(self, "_last_scene_sequence", None),
        )

    def wait_for_count(
        self,
        expected: int,
        timeout: float = 10.0,
        interval: float = 0.1,
        retry_exceptions: RetryExceptions = (),
        **selector: Any,
    ) -> int:
        """Wait for an exact count, retaining each observed count in diagnostics."""
        return wait_until(
            lambda: self.count(**selector),
            timeout=timeout,
            interval=interval,
            message=f"node count did not become {expected}: {selector}",
            retry_exceptions=retry_exceptions,
            scene_sequence=lambda: getattr(self, "_last_scene_sequence", None),
            predicate=lambda count: count == expected,
        )

    def wait_frames(self, count: int, timeout: float = 5.0, interval: float = 0.005) -> JsonDict:
        """Wait for ``count`` native engine updates and return the final frame receipt."""
        if not isinstance(count, int) or count < 0:
            raise ValueError("frame count must be a non-negative integer")
        initial = self.get("/frame")
        target = int(initial.get("engine_frame", 0)) + count
        if count == 0:
            return initial
        return wait_until(
            lambda: (data if int(data.get("engine_frame", 0)) >= target else None)
            if (data := self.get("/frame"))
            else None,
            timeout=timeout,
            interval=interval,
            message=f"engine frame did not reach {target}",
        )

    def wait_for_appearance(
        self,
        timeout: float = 10.0,
        interval: float = 0.02,
        after_scene_sequence: Optional[int] = None,
        **selector: Any,
    ) -> ObservationReceipt:
        """Wait for a node observed after an optional scene cursor and return frame evidence."""
        def appeared() -> Optional[ObservationReceipt]:
            node = self.maybe_node(**selector)
            if node is None or (after_scene_sequence is not None and node.scene_sequence <= after_scene_sequence):
                return None
            identity = node.logical_id or node.snapshot_id
            return ObservationReceipt(
                node=node,
                identity=identity,
                first_frame=node.engine_frame,
                last_frame=node.engine_frame,
                first_scene_sequence=node.scene_sequence,
                last_scene_sequence=node.scene_sequence,
                observed_frames=1,
            )

        return wait_until(
            appeared,
            timeout=timeout,
            interval=interval,
            message=f"node did not appear after scene sequence {after_scene_sequence}: {selector}",
        )

    def observe_node(
        self,
        minimum_frames: int = 3,
        timeout: float = 10.0,
        interval: float = 0.01,
        identity: str = "logical",
        **selector: Any,
    ) -> ObservationReceipt:
        """Require one node identity across distinct frames and return its observation span."""
        if minimum_frames <= 0:
            raise ValueError("minimum_frames must be positive")
        if identity not in {"logical", "snapshot", "instance"}:
            raise ValueError("identity must be logical, snapshot, or instance")
        observation: Dict[str, Any] = {}

        def sample() -> Optional[ObservationReceipt]:
            node = self.maybe_node(**selector)
            if node is None:
                observation.clear()
                return None
            node_identity = {
                "logical": node.logical_id or node.snapshot_id,
                "snapshot": node.snapshot_id,
                "instance": node.instance_id or node.snapshot_id,
            }[identity]
            if observation.get("identity") != node_identity:
                observation.update(
                    identity=node_identity,
                    node=node,
                    first_frame=node.engine_frame,
                    last_frame=node.engine_frame,
                    first_sequence=node.scene_sequence,
                    last_sequence=node.scene_sequence,
                    frames=1,
                )
            elif node.engine_frame != observation["last_frame"]:
                observation["node"] = node
                observation["last_frame"] = node.engine_frame
                observation["last_sequence"] = node.scene_sequence
                observation["frames"] += 1
            if observation["frames"] < minimum_frames:
                return None
            return ObservationReceipt(
                node=observation["node"],
                identity=observation["identity"],
                first_frame=observation["first_frame"],
                last_frame=observation["last_frame"],
                first_scene_sequence=observation["first_sequence"],
                last_scene_sequence=observation["last_sequence"],
                observed_frames=observation["frames"],
            )

        return wait_until(
            sample,
            timeout=timeout,
            interval=interval,
            message=f"node was not observed for {minimum_frames} distinct frames: {selector}",
        )

    def wait_for_disappearance(
        self,
        node_id: str,
        timeout: float = 10.0,
        interval: float = 0.02,
    ) -> ObservationReceipt:
        """Wait until a snapshot id is absent and return its last and disappearance receipts."""
        first = self.maybe_node(id=node_id)
        last = first
        first_frame = first.engine_frame if first else 0
        first_sequence = first.scene_sequence if first else 0
        observed = 1 if first else 0

        def disappeared() -> Optional[ObservationReceipt]:
            nonlocal last, observed
            node = self.maybe_node(id=node_id)
            if node is not None:
                if last is None or node.engine_frame != last.engine_frame:
                    observed += 1
                last = node
                return None
            current = self.get("/frame")
            return ObservationReceipt(
                node=last,
                identity=(last.logical_id or last.snapshot_id) if last else node_id,
                first_frame=first_frame,
                last_frame=last.engine_frame if last else first_frame,
                first_scene_sequence=first_sequence,
                last_scene_sequence=last.scene_sequence if last else first_sequence,
                observed_frames=observed,
                disappeared_frame=int(current.get("engine_frame", 0)),
                disappeared_scene_sequence=int(current.get("scene_sequence", 0)),
            )

        return wait_until(
            disappeared,
            timeout=timeout,
            interval=interval,
            message=f"node did not disappear: {node_id}",
        )

    def assert_node(self, **selector: Any) -> Node:
        """Assert scene/state-level node properties using native selectors."""
        return self.node(**selector)

    def wait_for_stable_frame(self, *args: Any, **kwargs: Any):
        """Delegate pixel stability to the optional ``automation_bridge.visual`` layer."""
        from .visual import VisualClient

        return VisualClient(self).wait_for_stable_frame(*args, **kwargs)

    def wait_for_region_change(self, *args: Any, **kwargs: Any):
        """Delegate region differencing to the optional ``automation_bridge.visual`` layer."""
        from .visual import VisualClient

        return VisualClient(self).wait_for_region_change(*args, **kwargs)

    def _request(
        self,
        method: str,
        path: str,
        params: Optional[Mapping[str, Any]] = None,
        json_body: Optional[Mapping[str, Any]] = None,
    ) -> JsonDict:
        url = self.base_url + path
        encoded_params = self._encoded_params(params)
        if encoded_params:
            url += "?" + urllib.parse.urlencode(encoded_params)

        request_trace = {"method": method, "path": path, "params": dict(params or {})}
        if json_body is not None:
            request_trace["json_body"] = dict(json_body)
        try:
            status, response = request_json(url, method=method, timeout=self.timeout, json_body=json_body)
        except BaseException as error:
            request_trace["error"] = repr(error)
            self._trace_record("action" if path.startswith("/input/") else "request_error", request_trace)
            raise
        if not response.get("ok"):
            error = response.get("error", {})
            code = str(error.get("code", "unknown"))
            message = str(error.get("message", response))
            request_trace.update({"status": status, "error": response})
            self._trace_record("action" if path.startswith("/input/") else "request_error", request_trace)
            raise AutomationBridgeApiError(code, message, status, response)
        data = response.get("data", {})
        self._remember_scene_sequence(data)
        request_trace.update({"status": status, "response": data})
        self._trace_record("action" if path.startswith("/input/") else "request", request_trace)
        return data

    def _request_json(self, method: str, path: str, payload: Mapping[str, Any]) -> JsonDict:
        url = self.base_url + path
        compact_payload = {key: value for key, value in payload.items() if value is not None}
        data = json.dumps(compact_payload, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
        status, response = request_json(
            url,
            method=method,
            timeout=self.timeout,
            data=data,
            headers={"Content-Type": "application/json"},
        )
        if not response.get("ok"):
            error = response.get("error", {})
            code = str(error.get("code", "unknown"))
            message = str(error.get("message", response))
            raise AutomationBridgeApiError(code, message, status, response)
        data = response.get("data", {})
        self._remember_scene_sequence(data)
        return data

    def _input_params(self, lease: float = 5.0) -> Dict[str, Any]:
        """Return ownership and per-request correlation fields for mutating input calls."""
        params: Dict[str, Any] = {
            "client_id": self.client_id,
            "session_id": self.session_id,
            "request_id": f"r-{uuid.uuid4().hex[:12]}",
        }
        if lease != 5.0:
            params["lease"] = lease
        return params

    def _wait_input_compat(
        self,
        receipt: InputReceipt,
        wait: Union[str, bool, float, None],
        timeout: float,
        cancel_on_interrupt: bool = True,
        flush_on_interrupt: bool = False,
    ) -> InputReceipt:
        if wait is False or wait == 0:
            return receipt
        if wait is None or wait is True:
            state = "released"
        elif isinstance(wait, str):
            state = wait
        elif isinstance(wait, (int, float)):
            state = "released"
            timeout = float(wait)
        else:
            raise TypeError("wait must be a lifecycle state, bool, numeric timeout, or None")
        return self.input.wait(
            receipt,
            state=state,
            timeout=timeout,
            cancel_on_interrupt=cancel_on_interrupt,
            flush_on_interrupt=flush_on_interrupt,
        )

    @staticmethod
    def _input_duration(value: Any, name: str) -> float:
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise TypeError(f"{name} must be a number")
        result = float(value)
        if result < 0.0 or result > 60.0 or result != result or result in {float("inf"), float("-inf")}:
            raise ValueError(f"{name} must be finite and between 0 and 60 seconds")
        return result

    def _trace_record(self, kind: str, payload: Any) -> None:
        for trace in tuple(self._active_traces):
            trace.record(kind, payload)

    @staticmethod
    def _recording_content_rect(screen: Mapping[str, Any]) -> Optional[Tuple[int, int, int, int]]:
        containers = [screen]
        rectangles = screen.get("rectangles")
        if isinstance(rectangles, Mapping):
            containers.append(rectangles)
        for container in containers:
            # Local viewport/client coordinates are intentionally excluded:
            # desktop capture needs a rectangle whose global display-pixel
            # coordinate space is explicit.
            rectangle = container.get("content_display_pixels")
            if isinstance(rectangle, Mapping):
                x = rectangle.get("x", 0)
                y = rectangle.get("y", 0)
                width = rectangle.get("width", rectangle.get("w"))
                height = rectangle.get("height", rectangle.get("h"))
                if all(isinstance(value, (int, float)) for value in (x, y, width, height)) and width > 0 and height > 0:
                    return int(x), int(y), int(width), int(height)
        return None

    def _post_engine_message(self, path: str, payload: bytes, timeout: Optional[float] = None) -> bytes:
        url = f"http://127.0.0.1:{self.port}{path}"
        status, body = request_bytes(url, payload, timeout=self.timeout if timeout is None else timeout)
        if status < 200 or status >= 300:
            preview = body[:200].decode("utf-8", "replace")
            raise HttpError("POST", url, f"unexpected status {status}: {preview}", status=status)
        return body

    def _remember_window_size(self, screen: Mapping[str, Any]) -> None:
        window = screen.get("window")
        if not isinstance(window, Mapping):
            return
        width = window.get("width")
        height = window.get("height")
        if isinstance(width, int) and isinstance(height, int) and width > 0 and height > 0:
            self._last_window_size = (width, height)

    def _remember_scene_sequence(self, data: Any) -> None:
        if not isinstance(data, Mapping):
            return
        sequence = data.get("scene_sequence")
        if isinstance(sequence, int) and not isinstance(sequence, bool):
            self._last_scene_sequence = sequence

    def _known_window_size(self) -> Tuple[int, int]:
        if self._last_window_size is None:
            self.screen()
        if self._last_window_size is None:
            raise AutomationBridgeError("window size is unavailable")
        return self._last_window_size

    @staticmethod
    def _encoded_params(params: Optional[Mapping[str, Any]]) -> Dict[str, str]:
        if not params:
            return {}
        return {key: _encode_param(value) for key, value in params.items() if value is not None}

    def _select_nodes(self, selector: Mapping[str, Any]) -> Tuple[List[Node], JsonDict, str]:
        self._validate_selector(selector)
        limit = selector.get("limit", 50)
        params = self._server_params(selector, limit=limit)
        data = self.get("/nodes", params)
        server_nodes = [Node(node) for node in data.get("nodes", []) if isinstance(node, dict)]
        filtered = [node for node in server_nodes if self._matches_client_filters(node, selector)]
        data = dict(data)
        data["_nodes"] = server_nodes
        return filtered, data, self._selector_text(selector)

    def _server_params(self, selector: Mapping[str, Any], limit: Any) -> Dict[str, Any]:
        params: Dict[str, Any] = {}
        for key in self._SERVER_FILTERS:
            value = selector.get(key)
            if value is not None:
                params[key] = value

        include = selector.get("include")
        if include is None and selector.get("has_bounds") is not None:
            include = "basic,bounds"
        normalized_include = _normalize_include(include)
        if normalized_include:
            params["include"] = normalized_include
        if limit is not None:
            params["limit"] = limit
        for key in ("offset", "cursor"):
            if selector.get(key) is not None:
                params[key] = selector[key]
        return params

    def _matches_client_filters(self, node: Node, selector: Mapping[str, Any]) -> bool:
        if selector.get("enabled") is not None and node.enabled != bool(selector["enabled"]):
            return False
        if selector.get("kind") is not None and node.kind != selector["kind"]:
            return False
        if selector.get("path") is not None and node.path != selector["path"]:
            return False
        if selector.get("name_exact") is not None and node.name != selector["name_exact"]:
            return False
        if selector.get("text_exact") is not None and node.text != selector["text_exact"]:
            return False
        if selector.get("has_bounds") is not None and (node.bounds is not None) != bool(selector["has_bounds"]):
            return False
        visible_and_enabled = selector.get("visible_and_enabled")
        if visible_and_enabled is not None and (node.visible and node.enabled) != bool(visible_and_enabled):
            return False
        return True

    def _validate_selector(self, selector: Mapping[str, Any]) -> None:
        unknown = set(selector) - self._SELECTOR_KEYS
        if unknown:
            raise TypeError(f"unknown node selector keys: {', '.join(sorted(unknown))}")

    def _has_client_filters(self, selector: Mapping[str, Any]) -> bool:
        return any(selector.get(key) is not None for key in self._CLIENT_FILTERS)

    @staticmethod
    def _selector_text(selector: Mapping[str, Any]) -> str:
        parts = [f"{key}={value!r}" for key, value in sorted(selector.items()) if value is not None]
        return ", ".join(parts) if parts else "<all nodes>"

    def _selector_error(
        self,
        prefix: str,
        selector: Mapping[str, Any],
        selector_text: str,
        nodes: Sequence[Node],
        metadata: Mapping[str, Any],
    ) -> str:
        candidates = metadata.get("_nodes", [])
        shown = nodes if nodes else candidates
        matched = int(metadata.get("matched", len(nodes)))
        truncated = bool(metadata.get("truncated", False))
        lines = [
            f"{prefix}; selector: {selector_text}; returned {len(nodes)}; "
            f"server matched {matched}; truncated={truncated}; "
            f"scene_sequence={metadata.get('scene_sequence')}; engine_frame={metadata.get('engine_frame')}"
        ]
        active_collections = metadata.get("active_collections")
        if active_collections:
            lines.append(f"active collections: {active_collections}")
        excluded = metadata.get("excluded")
        if isinstance(excluded, Mapping) and any(excluded.values()):
            lines.append(
                "matching nodes excluded by state: "
                f"visibility={excluded.get('visibility', 0)}, "
                f"enabled={excluded.get('enabled', 0)}, bounds={excluded.get('bounds', 0)}"
            )
        if not shown:
            shown = self._nearest_selector_nodes(selector)
        if shown:
            lines.append("candidates:")
            lines.extend(f"  {node.compact()}" for node in list(shown)[:10])
            requested_values = [
                str(value)
                for key, value in (
                    (part.split("=", 1)[0], part.split("=", 1)[1] if "=" in part else "")
                    for part in selector_text.split(", ")
                )
                if key in {"name", "name_exact", "text", "text_exact", "path"}
            ]
            choices = [value for node in shown for value in (node.name, node.text or "", node.path) if value]
            nearest = []
            for value in requested_values:
                nearest.extend(difflib.get_close_matches(value.strip("'\""), choices, n=3, cutoff=0.2))
            if nearest:
                lines.append(f"nearest name/text/path values: {list(dict.fromkeys(nearest))[:5]}")
        if matched > 1:
            lines.append("suggestion: add name_exact, text_exact, path, logical_id, or instance_id")
        return "\n".join(lines)

    def _nearest_selector_nodes(self, selector: Mapping[str, Any], maximum: int = 5000) -> List[Node]:
        """Fetch bounded complete pages only while formatting a failed direct selector."""
        candidates: List[Node] = []
        cursor: Optional[str] = None
        while len(candidates) < maximum:
            params: Dict[str, Any] = {"limit": min(500, maximum - len(candidates)), "include": "basic"}
            if cursor is not None:
                params["cursor"] = cursor
            data = self.get("/nodes", params)
            candidates.extend(Node(raw) for raw in data.get("nodes", []) if isinstance(raw, dict))
            cursor = data.get("next_cursor")
            if not cursor:
                break
        requested = [
            str(selector[key])
            for key in ("name_exact", "name", "text_exact", "text", "path")
            if selector.get(key) is not None
        ]
        if not requested:
            return candidates[:10]
        scored = []
        for node in candidates:
            choices = [node.name, node.text or "", node.path]
            ratios = [
                difflib.SequenceMatcher(None, expected, choice).ratio()
                for expected in requested
                for choice in choices
                if choice
            ]
            score = max(ratios) if ratios else 0.0
            scored.append((score, node))
        scored.sort(key=lambda item: item[0], reverse=True)
        return [node for _, node in scored[:10]]

    @staticmethod
    def _is_node_ref(target: Target) -> bool:
        return isinstance(target, (Node, str))

    @staticmethod
    def _node_id(target: Target) -> str:
        if isinstance(target, Node):
            return target.id
        if isinstance(target, str):
            return target
        raise TypeError(f"target is not a node reference: {target!r}")

    def _point(self, target: Union[Target, float, int], y: Optional[float] = None) -> Tuple[Any, Any]:
        if isinstance(target, Node):
            center = target.center or self.by_id(target.id, include="basic,bounds").center
            if center:
                return center["x"], center["y"]
        elif isinstance(target, str):
            center = self.by_id(target, include="basic,bounds").center
            if center:
                return center["x"], center["y"]
        elif isinstance(target, Mapping):
            if "x" in target and "y" in target:
                return target["x"], target["y"]
            center = target.get("center")
            if isinstance(center, Mapping) and "x" in center and "y" in center:
                return center["x"], center["y"]
        elif isinstance(target, Sequence) and not isinstance(target, (bytes, bytearray, str)) and len(target) == 2:
            return target[0], target[1]
        elif isinstance(target, (float, int)) and y is not None:
            return target, y

        raise TypeError(f"target does not provide coordinates: {target!r}")
