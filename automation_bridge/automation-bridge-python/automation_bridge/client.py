"""Dependency-free Python client for the Automation Bridge runtime API."""

import json
import os
import signal
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple, Union

from .nodes import Node
from .waits import wait_until


JsonDict = Dict[str, Any]
Target = Union[Node, str, Mapping[str, Any], Sequence[float]]
_INPUT_SETTLE_SECONDS = 0.1
_UINT32_MAX = 0xFFFFFFFF


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
        self._socket = socket.create_connection((host, self.port), timeout=timeout)
        self._buffer = bytearray()
        try:
            status = self._readline_raw(timeout).decode("utf-8", "replace").rstrip("\r\n")
            if status != "0 OK":
                raise AutomationBridgeError(f"log service rejected connection: {status}")
            self._socket.settimeout(read_timeout)
        except Exception:
            self.close()
            raise

    def __enter__(self) -> "EngineLogStream":
        return self

    def __exit__(self, exc_type: Any, exc: Any, traceback: Any) -> None:
        self.close()

    def __iter__(self) -> "EngineLogStream":
        return self

    def __next__(self) -> str:
        line = self.readline()
        if line is None:
            raise StopIteration
        return line

    def close(self) -> None:
        """Close the underlying log service socket."""
        try:
            self._socket.close()
        except OSError:
            pass

    def readline(self, timeout: Optional[float] = None) -> Optional[str]:
        """Read one log line without its trailing newline; return `None` on timeout or EOF."""
        data = self._readline_raw(timeout)
        if not data:
            return None
        return data.decode("utf-8", "replace").rstrip("\r\n")

    def _readline_raw(self, timeout: Optional[float] = None) -> bytes:
        previous_timeout = self._socket.gettimeout()
        if timeout is not None:
            self._socket.settimeout(timeout)
        try:
            while True:
                newline = self._buffer.find(b"\n")
                if newline >= 0:
                    line = bytes(self._buffer[: newline + 1])
                    del self._buffer[: newline + 1]
                    return line

                chunk = self._socket.recv(4096)
                if not chunk:
                    if not self._buffer:
                        return b""
                    line = bytes(self._buffer)
                    self._buffer.clear()
                    return line
                self._buffer.extend(chunk)
        except socket.timeout:
            return b""
        finally:
            if timeout is not None:
                self._socket.settimeout(previous_timeout)


def request_json(url: str, method: str = "GET", timeout: float = 10.0) -> Tuple[int, JsonDict]:
    """Request JSON and return `(status, object)`."""
    request = urllib.request.Request(url, method=method)
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
    if width > _UINT32_MAX or height > _UINT32_MAX:
        raise ValueError(f"resize dimensions must fit uint32, got {width}x{height}")


def _encode_system_reboot(args: Sequence[str]) -> bytes:
    if len(args) > 6:
        raise ValueError("Defold reboot accepts at most six arguments")

    payload = bytearray()
    for index, arg in enumerate(args, start=1):
        payload.extend(_protobuf_string(index, arg))
    return bytes(payload)


class AutomationBridgeClient:
    """High-level client for `/automation-bridge/v1` scene inspection and input control."""

    _SERVER_FILTERS = {"id", "type", "name", "text", "url", "visible"}
    _CLIENT_FILTERS = {
        "enabled",
        "kind",
        "path",
        "name_exact",
        "text_exact",
        "has_bounds",
        "visible_and_enabled",
    }
    _SELECTOR_KEYS = _SERVER_FILTERS | _CLIENT_FILTERS | {"include", "limit"}

    def __init__(self, port: int, timeout: float = 10.0, remotery_url: Optional[str] = None):
        """Create a client for an already-known engine service `port`."""
        self.port = int(port)
        self.timeout = timeout
        self.base_url = f"http://127.0.0.1:{self.port}/automation-bridge/v1"
        self._remotery_url = remotery_url
        self._last_window_size: Optional[Tuple[int, int]] = None

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
                except Exception:  # noqa: BLE001 - keep polling other candidate ports.
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
            )
        except AssertionError:
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

    def wait_ready(self, timeout: float = 20.0) -> JsonDict:
        """Wait until `/health` succeeds and return the health payload."""
        return wait_until(
            self.health,
            timeout=timeout,
            interval=0.1,
            message="Automation Bridge endpoint did not become ready",
        )

    def get(self, path: str, params: Optional[Mapping[str, Any]] = None) -> JsonDict:
        """GET an Automation Bridge API path and return its `data` object."""
        return self._request("GET", path, params)

    def post(self, path: str, params: Optional[Mapping[str, Any]] = None) -> JsonDict:
        """POST an Automation Bridge API path with query parameters and return `data`."""
        return self._request("POST", path, params)

    def put(self, path: str, params: Optional[Mapping[str, Any]] = None) -> JsonDict:
        """PUT an Automation Bridge API path with query parameters and return `data`."""
        return self._request("PUT", path, params)

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
        """Return nodes matching server-side and Python-side selector filters."""
        nodes, _, _ = self._select_nodes(selector)
        return nodes

    def node(self, **selector: Any) -> Node:
        """Return exactly one matching node or raise `SelectorError`."""
        nodes, server_nodes, selector_text = self._select_nodes(selector)
        if len(nodes) == 1:
            return nodes[0]
        raise SelectorError(self._selector_error("expected exactly one node", selector_text, nodes, server_nodes))

    def maybe_node(self, **selector: Any) -> Optional[Node]:
        """Return zero or one matching node, raising if multiple nodes match."""
        nodes, server_nodes, selector_text = self._select_nodes(selector)
        if len(nodes) <= 1:
            return nodes[0] if nodes else None
        raise SelectorError(self._selector_error("expected zero or one node", selector_text, nodes, server_nodes))

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
        """Return the number of nodes matching `selector`."""
        self._validate_selector(selector)
        if self._has_client_filters(selector):
            selector = dict(selector)
            selector["limit"] = 500
            return len(self.nodes(**selector))

        params = self._server_params(selector, limit=0)
        return int(self.get("/nodes", params).get("matched", 0))

    def click(
        self,
        target: Union[Target, float, int],
        y: Optional[float] = None,
        wait: float = 0.25,
        visualize: Optional[bool] = None,
    ) -> JsonDict:
        """Queue a left-click on a Node, node id, mapping, tuple, or x/y pair."""
        if isinstance(target, Node):
            params: Dict[str, Any] = {"id": target.id}
        elif isinstance(target, str):
            params = {"id": target}
        else:
            x_value, y_value = self._point(target, y)
            params = {"x": x_value, "y": y_value}

        if visualize is not None:
            params["visualize"] = visualize

        response = self.post("/input/click", params)
        if wait:
            time.sleep(wait)
        return response

    def drag(
        self,
        from_target: Target,
        to_target: Target,
        duration: float = 0.35,
        wait: Optional[float] = None,
        visualize: Optional[bool] = None,
    ) -> JsonDict:
        """Queue a drag between nodes or coordinates and block until it finishes."""
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

        if visualize is not None:
            params["visualize"] = visualize

        response = self.post("/input/drag", params)
        block_for = max(0.0, duration) + _INPUT_SETTLE_SECONDS if wait is None else wait
        if block_for:
            time.sleep(block_for)
        return response

    def type_text(self, text: str) -> JsonDict:
        """Queue plain text keyboard input."""
        return self.post("/input/key", {"text": text})

    def key(self, key: str) -> JsonDict:
        """Queue a special key such as `KEY_ENTER`."""
        keys = key if key.startswith("{") and key.endswith("}") else f"{{{key}}}"
        return self.post("/input/key", {"keys": keys})

    def screenshot(self, wait: bool = True, timeout: float = 5.0) -> Path:
        """Schedule a screenshot and optionally wait for the PNG file."""
        response = self.get("/screenshot")
        path = Path(response["path"])
        if wait:
            wait_until(
                lambda: path.exists() and path.stat().st_size > 0 and path,
                timeout=timeout,
                interval=0.1,
                message=f"screenshot was not written: {path}",
            )
        return path

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
    def remotery_url(self) -> Optional[str]:
        """Return the Remotery websocket URL discovered while bootstrapping, if known."""
        return self._remotery_url

    @property
    def last_window_size(self) -> Optional[Tuple[int, int]]:
        """Return the last known `(width, height)` from `screen()`, `health()`, or `resize()`."""
        return self._last_window_size

    def resize(self, width: int, height: int, wait: float = 0.25) -> JsonDict:
        """Resize the Defold window through `PUT /screen` and remember the new size."""
        _validate_screen_size(width, height)
        screen = self.put("/screen", {"width": width, "height": height})
        self._remember_window_size(screen)
        if wait:
            time.sleep(wait)
        window = screen.get("window") if isinstance(screen, Mapping) else None
        if isinstance(window, Mapping):
            screen_width = window.get("width")
            screen_height = window.get("height")
            if isinstance(screen_width, int) and isinstance(screen_height, int):
                width = screen_width
                height = screen_height
        return {"width": width, "height": height}

    def set_portrait(self, wait: float = 0.25) -> JsonDict:
        """Resize to portrait by swapping the last known width and height when needed."""
        width, height = self._known_window_size()
        if width > height:
            return self.resize(height, width, wait=wait)
        return {"width": width, "height": height}

    def set_landscape(self, wait: float = 0.25) -> JsonDict:
        """Resize to landscape by swapping the last known width and height when needed."""
        width, height = self._known_window_size()
        if width < height:
            return self.resize(height, width, wait=wait)
        return {"width": width, "height": height}

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

    def wait_for_node(self, timeout: float = 10.0, interval: float = 0.1, **selector: Any) -> Node:
        """Wait until `node(**selector)` succeeds."""
        return wait_until(
            lambda: self.node(**selector),
            timeout=timeout,
            interval=interval,
            message=f"node did not appear: {selector}",
        )

    def wait_for_count(
        self,
        expected: int,
        timeout: float = 10.0,
        interval: float = 0.1,
        **selector: Any,
    ) -> int:
        """Wait until `count(**selector)` equals `expected`."""
        def count_if_expected() -> Optional[bool]:
            count = self.count(**selector)
            return True if count == expected else None

        wait_until(
            count_if_expected,
            timeout=timeout,
            interval=interval,
            message=f"node count did not become {expected}: {selector}",
        )
        return expected

    def _request(self, method: str, path: str, params: Optional[Mapping[str, Any]] = None) -> JsonDict:
        url = self.base_url + path
        encoded_params = self._encoded_params(params)
        if encoded_params:
            url += "?" + urllib.parse.urlencode(encoded_params)

        status, response = request_json(url, method=method, timeout=self.timeout)
        if not response.get("ok"):
            error = response.get("error", {})
            code = str(error.get("code", "unknown"))
            message = str(error.get("message", response))
            raise AutomationBridgeApiError(code, message, status, response)
        return response.get("data", {})

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

    def _select_nodes(self, selector: Mapping[str, Any]) -> Tuple[List[Node], List[Node], str]:
        self._validate_selector(selector)
        limit = selector.get("limit", 50)
        request_limit = 500 if self._has_client_filters(selector) and limit != 0 else limit
        params = self._server_params(selector, limit=request_limit)
        data = self.get("/nodes", params)
        server_nodes = [Node(node) for node in data.get("nodes", []) if isinstance(node, dict)]
        filtered = [node for node in server_nodes if self._matches_client_filters(node, selector)]
        if limit is not None:
            filtered = filtered[: int(limit)]
        return filtered, server_nodes, self._selector_text(selector)

    def _server_params(self, selector: Mapping[str, Any], limit: Any) -> Dict[str, Any]:
        params: Dict[str, Any] = {}
        for key in self._SERVER_FILTERS:
            value = selector.get(key)
            if value is not None:
                params[key] = value

        if selector.get("name_exact") is not None and "name" not in params:
            params["name"] = selector["name_exact"]
        if selector.get("text_exact") is not None and "text" not in params:
            params["text"] = selector["text_exact"]
        if selector.get("visible_and_enabled") is True and "visible" not in params:
            params["visible"] = True

        include = selector.get("include")
        if include is None and selector.get("has_bounds") is not None:
            include = "basic,bounds"
        normalized_include = _normalize_include(include)
        if normalized_include:
            params["include"] = normalized_include
        if limit is not None:
            params["limit"] = limit
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
        selector_text: str,
        nodes: Sequence[Node],
        candidates: Sequence[Node],
    ) -> str:
        shown = nodes if nodes else candidates
        lines = [f"{prefix}; selector: {selector_text}; found {len(nodes)}"]
        if shown:
            lines.append("candidates:")
            lines.extend(f"  {node.compact()}" for node in list(shown)[:10])
        return "\n".join(lines)

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
