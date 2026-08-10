"""Dependency-free Model Context Protocol JSON-RPC and stdio support.

The transport deliberately keeps stdout protocol-only.  It supports the
stateless 2026-07-28 revision as well as the initialization-based 2025-11-25
and 2025-06-18 revisions used by existing agent clients.
"""

from __future__ import annotations

import json
import math
import sys
import threading
import time
from typing import Any, Dict, Iterable, Mapping, Optional, Sequence, TextIO, Tuple


MODERN_PROTOCOL_VERSION = "2026-07-28"
LEGACY_PROTOCOL_VERSIONS = ("2025-11-25", "2025-06-18")
SUPPORTED_PROTOCOL_VERSIONS = (MODERN_PROTOCOL_VERSION,) + LEGACY_PROTOCOL_VERSIONS

PARSE_ERROR = -32700
INVALID_REQUEST = -32600
METHOD_NOT_FOUND = -32601
INVALID_PARAMS = -32602
INTERNAL_ERROR = -32603
LEGACY_RESOURCE_NOT_FOUND = -32002
UNSUPPORTED_PROTOCOL_VERSION = -32022

PROTOCOL_VERSION_META_KEY = "io.modelcontextprotocol/protocolVersion"
CLIENT_INFO_META_KEY = "io.modelcontextprotocol/clientInfo"
CLIENT_CAPABILITIES_META_KEY = "io.modelcontextprotocol/clientCapabilities"
SERVER_INFO_META_KEY = "io.modelcontextprotocol/serverInfo"

DEFAULT_SERVER_INFO = {"name": "automation-bridge", "version": "0.1.0"}
DEFAULT_INSTRUCTIONS = (
    "Use Automation Bridge to inspect and automate Defold projects. Always pass an "
    "explicit absolute project_path; probe with start_if_needed=false before launching "
    "Defold. Preserve returned handles and complete Element snapshots between calls so "
    "stale logical identity checks remain active. Open event streams before triggering "
    "the action they observe. Release contexts and handles after use, and close an "
    "engine only when the task owns it and the user has confirmed the destructive action."
)


def _valid_id(value: Any) -> bool:
    return isinstance(value, (str, int)) and not isinstance(value, bool)


def _reject_json_constant(value: str) -> None:
    raise ValueError("non-finite JSON number: " + value)


def _strict_json_float(value: str) -> float:
    result = float(value)
    if not math.isfinite(result):
        raise ValueError("non-finite JSON number: " + value)
    return result


def _strict_json_object(pairs: Iterable[Tuple[str, Any]]) -> Dict[str, Any]:
    result: Dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON object member: " + key)
        result[key] = value
    return result


def _validate_json_value(value: Any, depth: int = 0) -> None:
    """Reject values that cannot be represented as interoperable UTF-8 JSON."""
    if depth > 256:
        raise ValueError("JSON nesting is too deep")
    if isinstance(value, str):
        value.encode("utf-8", errors="strict")
        return
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError("non-finite JSON number")
        return
    if isinstance(value, list):
        for item in value:
            _validate_json_value(item, depth + 1)
        return
    if isinstance(value, dict):
        for key, item in value.items():
            key.encode("utf-8", errors="strict")
            _validate_json_value(item, depth + 1)


def decode_message(line: Any) -> Any:
    """Decode one newline-delimited UTF-8 JSON message strictly."""
    if isinstance(line, bytes):
        line = line.decode("utf-8", errors="strict")
    if not isinstance(line, str):
        raise ValueError("MCP input must be text or UTF-8 bytes")
    if line.endswith("\n"):
        line = line[:-1]
        if line.endswith("\r"):
            line = line[:-1]
    if "\n" in line or "\r" in line:
        raise ValueError("MCP stdio messages must not contain embedded newlines")
    result = json.loads(
        line,
        parse_constant=_reject_json_constant,
        parse_float=_strict_json_float,
        object_pairs_hook=_strict_json_object,
    )
    _validate_json_value(result)
    return result


def encode_message(message: Mapping[str, Any]) -> str:
    """Encode one compact JSON-RPC frame, rejecting NaN and Infinity."""
    _validate_json_value(message)
    return json.dumps(
        message,
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
    )


class ProtocolError(Exception):
    """An error that should be represented as a JSON-RPC error response."""

    def __init__(self, code: int, message: str, data: Any = None):
        super().__init__(message)
        self.code = int(code)
        self.message = str(message)
        self.data = data

    def as_dict(self) -> Dict[str, Any]:
        result = {"code": self.code, "message": self.message}
        if self.data is not None:
            result["data"] = self.data
        return result


def error_response(
    request_id: Any,
    code: int,
    message: str,
    data: Any = None,
) -> Dict[str, Any]:
    error = {"code": int(code), "message": str(message)}
    if data is not None:
        error["data"] = data
    return {"jsonrpc": "2.0", "id": request_id, "error": error}


class McpProtocol:
    """Dispatch parsed MCP JSON-RPC messages to a ``BridgeRuntime``.

    Modern requests are stateless and must carry their version and client
    capabilities in ``params._meta``.  Legacy requests use the version selected
    by ``initialize``.  The two modes may be interleaved in one stdio process.
    """

    def __init__(
        self,
        runtime: Any,
        server_info: Optional[Mapping[str, Any]] = None,
        instructions: str = DEFAULT_INSTRUCTIONS,
    ) -> None:
        self.runtime = runtime
        self.server_info = dict(server_info or DEFAULT_SERVER_INFO)
        self.instructions = str(instructions)
        self._legacy_version: Optional[str] = None
        self._legacy_ready = False
        self._state_lock = threading.RLock()

    @property
    def legacy_protocol_version(self) -> Optional[str]:
        return self._legacy_version

    def handle_line(self, line: Any) -> Optional[Dict[str, Any]]:
        """Decode and handle one frame, returning a parse error when invalid."""
        try:
            message = decode_message(line)
        except (UnicodeError, ValueError, json.JSONDecodeError, RecursionError):
            return error_response(None, PARSE_ERROR, "Parse error")
        return self.handle(message)

    def handle(self, message: Any) -> Optional[Dict[str, Any]]:
        """Handle a parsed JSON-RPC request or notification."""
        request_id = self._preserved_error_id(message)
        notification = self._notification_candidate(message)
        try:
            method, params, request_id, notification = self._validate_envelope(message)
            if notification:
                self._handle_notification(method, params)
                return None
            result, modern = self._dispatch(method, params)
            response = self._success_response(request_id, result, modern)
            encode_message(response)
            return response
        except ProtocolError as error:
            if notification:
                return None
            response = error_response(request_id, error.code, error.message, error.data)
            try:
                encode_message(response)
            except (TypeError, ValueError):
                response = error_response(request_id, INTERNAL_ERROR, "Internal error")
            return response
        except Exception:
            if notification:
                return None
            return error_response(request_id, INTERNAL_ERROR, "Internal error")

    def cancel(self, request_id: Any) -> None:
        """Forward a cancellation hint to the runtime when it supports one."""
        cancel = getattr(self.runtime, "cancel", None)
        if callable(cancel):
            try:
                cancel(request_id)
            except Exception:
                pass

    @staticmethod
    def _preserved_error_id(message: Any) -> Any:
        if not isinstance(message, dict) or "id" not in message:
            return None
        value = message.get("id")
        return value if _valid_id(value) else None

    @staticmethod
    def _notification_candidate(message: Any) -> bool:
        return (
            isinstance(message, dict)
            and "id" not in message
            and message.get("jsonrpc") == "2.0"
            and isinstance(message.get("method"), str)
        )

    def _validate_envelope(
        self, message: Any
    ) -> Tuple[str, Dict[str, Any], Any, bool]:
        if not isinstance(message, dict):
            raise ProtocolError(INVALID_REQUEST, "Invalid Request")

        request_id = self._preserved_error_id(message)
        if message.get("jsonrpc") != "2.0":
            raise ProtocolError(INVALID_REQUEST, "Invalid Request")
        if "id" in message and not _valid_id(message.get("id")):
            raise ProtocolError(INVALID_REQUEST, "Invalid Request")
        method = message.get("method")
        if not isinstance(method, str) or not method:
            raise ProtocolError(INVALID_REQUEST, "Invalid Request")
        if any(key not in {"jsonrpc", "id", "method", "params"} for key in message):
            raise ProtocolError(INVALID_REQUEST, "Invalid Request")

        params = message.get("params", {})
        if not isinstance(params, dict):
            # MCP narrows JSON-RPC's object-or-array params to an object.
            raise ProtocolError(INVALID_REQUEST, "Invalid Request")
        return method, params, request_id, "id" not in message

    def _handle_notification(self, method: str, params: Dict[str, Any]) -> None:
        if method == "notifications/cancelled":
            request_id = params.get("requestId")
            if (
                _valid_id(request_id)
                and set(params).issubset({"requestId", "reason", "_meta"})
                and ("reason" not in params or isinstance(params["reason"], str))
                and ("_meta" not in params or isinstance(params["_meta"], dict))
            ):
                self.cancel(request_id)
            return
        if method == "notifications/initialized":
            if (
                set(params).issubset({"_meta"})
                and ("_meta" not in params or isinstance(params["_meta"], dict))
            ):
                with self._state_lock:
                    if self._legacy_version is not None:
                        self._legacy_ready = True
            return

        # MCP defines these methods as request-only. Unknown and request-only
        # notifications are ignored without executing handlers or side effects.

    def _dispatch(
        self, method: str, params: Dict[str, Any]
    ) -> Tuple[Dict[str, Any], bool]:
        if method == "initialize":
            return self._initialize(params), False
        if method == "server/discover":
            self._validate_modern(params)
            self._validate_keys(params, required=("_meta",), optional=())
            return self._discover_result(), True

        modern = self._has_modern_metadata(params)
        if modern:
            self._validate_modern(params)
            if method == "ping":
                raise ProtocolError(METHOD_NOT_FOUND, "Method not found")
        else:
            if method == "ping":
                self._validate_keys(params, optional=("_meta",))
                return {}, False
            with self._state_lock:
                if self._legacy_version is None:
                    raise ProtocolError(INVALID_REQUEST, "Server not initialized")

        if method == "tools/list":
            return self._list_tools(params), modern
        if method == "tools/call":
            return self._call_tool(params), modern
        if method == "resources/list":
            return self._list_resources(params), modern
        if method == "resources/read":
            return self._read_resource(params, modern), modern
        raise ProtocolError(METHOD_NOT_FOUND, "Method not found")

    def _initialize(self, params: Dict[str, Any]) -> Dict[str, Any]:
        self._validate_keys(
            params,
            required=("protocolVersion", "capabilities", "clientInfo"),
            optional=("_meta",),
        )
        requested = params["protocolVersion"]
        if not isinstance(requested, str) or not requested:
            raise ProtocolError(INVALID_PARAMS, "Invalid params")
        if not isinstance(params["capabilities"], dict):
            raise ProtocolError(INVALID_PARAMS, "Invalid params")
        self._validate_implementation(params["clientInfo"])

        with self._state_lock:
            if self._legacy_version is not None:
                raise ProtocolError(INVALID_REQUEST, "Already initialized")
            selected = (
                requested
                if requested in LEGACY_PROTOCOL_VERSIONS
                else LEGACY_PROTOCOL_VERSIONS[0]
            )
            self._legacy_version = selected
            self._legacy_ready = False

        return {
            "protocolVersion": selected,
            "capabilities": self._capabilities(),
            "serverInfo": dict(self.server_info),
            "instructions": self.instructions,
        }

    def _discover_result(self) -> Dict[str, Any]:
        return {
            "supportedVersions": list(SUPPORTED_PROTOCOL_VERSIONS),
            "capabilities": self._capabilities(),
            "instructions": self.instructions,
            "ttlMs": 3_600_000,
            "cacheScope": "public",
        }

    @staticmethod
    def _capabilities() -> Dict[str, Any]:
        return {"tools": {}, "resources": {}}

    def _has_modern_metadata(self, params: Dict[str, Any]) -> bool:
        if "_meta" not in params:
            return False
        meta = params.get("_meta")
        if not isinstance(meta, dict):
            return True
        has_modern_key = any(
            key in meta
            for key in (
                PROTOCOL_VERSION_META_KEY,
                CLIENT_INFO_META_KEY,
                CLIENT_CAPABILITIES_META_KEY,
            )
        )
        if has_modern_key:
            return True
        return False

    def _validate_modern(self, params: Dict[str, Any]) -> None:
        meta = params.get("_meta")
        if not isinstance(meta, dict):
            raise ProtocolError(INVALID_PARAMS, "Invalid params")
        requested = meta.get(PROTOCOL_VERSION_META_KEY)
        if not isinstance(requested, str) or not requested:
            raise ProtocolError(INVALID_PARAMS, "Invalid params")
        if requested != MODERN_PROTOCOL_VERSION:
            raise ProtocolError(
                UNSUPPORTED_PROTOCOL_VERSION,
                "Unsupported protocol version",
                {
                    "requested": requested,
                    "supported": list(SUPPORTED_PROTOCOL_VERSIONS),
                },
            )
        capabilities = meta.get(CLIENT_CAPABILITIES_META_KEY)
        if not isinstance(capabilities, dict):
            raise ProtocolError(INVALID_PARAMS, "Invalid params")
        if CLIENT_INFO_META_KEY in meta:
            self._validate_implementation(meta[CLIENT_INFO_META_KEY])

    @staticmethod
    def _validate_implementation(value: Any) -> None:
        if not isinstance(value, dict):
            raise ProtocolError(INVALID_PARAMS, "Invalid params")
        if not isinstance(value.get("name"), str) or not value.get("name"):
            raise ProtocolError(INVALID_PARAMS, "Invalid params")
        if not isinstance(value.get("version"), str) or not value.get("version"):
            raise ProtocolError(INVALID_PARAMS, "Invalid params")

    @staticmethod
    def _validate_keys(
        params: Dict[str, Any],
        required: Sequence[str] = (),
        optional: Sequence[str] = (),
    ) -> None:
        keys = set(params)
        required_keys = set(required)
        if not required_keys.issubset(keys):
            raise ProtocolError(INVALID_PARAMS, "Invalid params")
        if not keys.issubset(required_keys | set(optional)):
            raise ProtocolError(INVALID_PARAMS, "Invalid params")
        if "_meta" in params and not isinstance(params["_meta"], dict):
            raise ProtocolError(INVALID_PARAMS, "Invalid params")

    def _list_tools(self, params: Dict[str, Any]) -> Dict[str, Any]:
        self._validate_keys(params, optional=("cursor", "_meta"))
        if "cursor" in params and not isinstance(params["cursor"], str):
            raise ProtocolError(INVALID_PARAMS, "Invalid params")
        tools = self._runtime_descriptors("tool_descriptors", "tools")
        return {"tools": tools, "ttlMs": 300_000, "cacheScope": "public"}

    def _call_tool(self, params: Dict[str, Any]) -> Dict[str, Any]:
        self._validate_keys(params, required=("name",), optional=("arguments", "_meta"))
        name = params["name"]
        arguments = params.get("arguments", {})
        if not isinstance(name, str) or not name or not isinstance(arguments, dict):
            raise ProtocolError(INVALID_PARAMS, "Invalid params")

        descriptors = self._runtime_descriptors("tool_descriptors", "tools")
        if name not in {
            item.get("name") for item in descriptors if isinstance(item, dict)
        }:
            raise ProtocolError(INVALID_PARAMS, "Unknown tool", {"name": name})

        call_tool = getattr(self.runtime, "call_tool", None)
        if not callable(call_tool):
            raise ProtocolError(INTERNAL_ERROR, "Internal error")
        try:
            envelope = call_tool(name, arguments)
        except Exception as error:
            envelope = {"ok": False, "error": self._tool_error(error)}
        if not isinstance(envelope, dict):
            envelope = {"ok": True, "data": envelope}

        try:
            text = json.dumps(
                envelope,
                ensure_ascii=False,
                allow_nan=False,
                sort_keys=True,
                separators=(",", ":"),
            )
        except (TypeError, ValueError):
            raise ProtocolError(INTERNAL_ERROR, "Internal error")
        return {
            "content": [{"type": "text", "text": text}],
            "structuredContent": envelope,
            "isError": envelope.get("ok") is False,
        }

    @staticmethod
    def _tool_error(error: Exception) -> Dict[str, Any]:
        as_dict = getattr(error, "as_dict", None)
        if callable(as_dict):
            try:
                value = as_dict()
                encode_message({"jsonrpc": "2.0", "id": 0, "result": value})
                if isinstance(value, dict):
                    return value
            except Exception:
                pass
        result = {
            "code": str(getattr(error, "code", "tool_error")),
            "message": str(getattr(error, "message", str(error) or "Tool call failed")),
        }
        data = getattr(error, "data", None)
        if isinstance(data, dict):
            try:
                encode_message({"jsonrpc": "2.0", "id": 0, "result": data})
                result["data"] = data
            except (TypeError, ValueError):
                pass
        return result

    def _list_resources(self, params: Dict[str, Any]) -> Dict[str, Any]:
        self._validate_keys(params, optional=("cursor", "_meta"))
        if "cursor" in params and not isinstance(params["cursor"], str):
            raise ProtocolError(INVALID_PARAMS, "Invalid params")
        resources = self._runtime_descriptors("resource_descriptors", "resources")
        return {"resources": resources, "ttlMs": 300_000, "cacheScope": "public"}

    def _read_resource(self, params: Dict[str, Any], modern: bool) -> Dict[str, Any]:
        self._validate_keys(params, required=("uri",), optional=("_meta",))
        uri = params["uri"]
        if not isinstance(uri, str) or not uri:
            raise ProtocolError(INVALID_PARAMS, "Invalid params")
        read_resource = getattr(self.runtime, "read_resource", None)
        if not callable(read_resource):
            raise ProtocolError(INTERNAL_ERROR, "Internal error")
        try:
            value = read_resource(uri)
        except KeyError:
            code = INVALID_PARAMS if modern else LEGACY_RESOURCE_NOT_FOUND
            raise ProtocolError(code, "Resource not found", {"uri": uri})
        except ProtocolError:
            raise
        except Exception:
            raise ProtocolError(INTERNAL_ERROR, "Internal error")

        if isinstance(value, dict) and "contents" in value:
            contents = value.get("contents")
        elif isinstance(value, dict):
            # Keep the protocol adapter compatible with runtimes that return
            # one ResourceContents object directly instead of the result wrapper.
            contents = [value]
        else:
            contents = value
        if isinstance(contents, dict):
            contents = [contents]
        if not isinstance(contents, list):
            raise ProtocolError(INTERNAL_ERROR, "Internal error")
        for content in contents:
            if not isinstance(content, dict) or not isinstance(content.get("uri"), str):
                raise ProtocolError(INTERNAL_ERROR, "Internal error")
            if not (
                isinstance(content.get("text"), str)
                or isinstance(content.get("blob"), str)
            ):
                raise ProtocolError(INTERNAL_ERROR, "Internal error")
        return {
            "contents": contents,
            "ttlMs": 60_000,
            "cacheScope": "private",
        }

    def _runtime_descriptors(self, primary: str, fallback: str) -> list:
        value = getattr(self.runtime, primary, None)
        if value is None:
            value = getattr(self.runtime, fallback, None)
        if callable(value):
            value = value()
        if isinstance(value, dict) and isinstance(value.get(fallback), list):
            value = value[fallback]
        if not isinstance(value, (list, tuple)):
            raise ProtocolError(INTERNAL_ERROR, "Internal error")
        result = list(value)
        try:
            encode_message({"jsonrpc": "2.0", "id": 0, "result": {fallback: result}})
        except (TypeError, ValueError):
            raise ProtocolError(INTERNAL_ERROR, "Internal error")
        return result

    def _success_response(
        self,
        request_id: Any,
        result: Dict[str, Any],
        modern: bool,
    ) -> Dict[str, Any]:
        payload = dict(result)
        if modern:
            payload["resultType"] = "complete"
            meta = payload.get("_meta")
            meta = dict(meta) if isinstance(meta, dict) else {}
            meta[SERVER_INFO_META_KEY] = dict(self.server_info)
            payload["_meta"] = meta
        else:
            # Cache hints were introduced by 2026-07-28 and must not leak into
            # either initialization-based response shape.
            payload.pop("ttlMs", None)
            payload.pop("cacheScope", None)
            payload.pop("resultType", None)
            payload.pop("_meta", None)
        return {"jsonrpc": "2.0", "id": request_id, "result": payload}


MCPProtocol = McpProtocol


class _RequestState:
    def __init__(self, method: str) -> None:
        self.method = method
        self.cancelled = threading.Event()


class StdioServer:
    """Newline-delimited stdio MCP server with cancellable daemon workers."""

    _CONCURRENT_METHODS = frozenset(("tools/call", "resources/read"))

    def __init__(
        self,
        protocol: McpProtocol,
        stdin: Optional[TextIO] = None,
        stdout: Optional[TextIO] = None,
        stderr: Optional[TextIO] = None,
        eof_join_timeout: float = 1.0,
    ) -> None:
        self.protocol = protocol
        self.stdin = stdin if stdin is not None else sys.stdin
        self.stdout = stdout if stdout is not None else sys.stdout
        self.stderr = stderr if stderr is not None else sys.stderr
        self.eof_join_timeout = max(0.0, float(eof_join_timeout))
        self._write_lock = threading.Lock()
        self._state_lock = threading.RLock()
        self._active: Dict[Any, _RequestState] = {}
        self._threads = set()
        self._closing = False
        self._cleaned = False

    def serve_forever(self) -> int:
        try:
            while True:
                try:
                    line = self.stdin.readline()
                except UnicodeError:
                    self._write(error_response(None, PARSE_ERROR, "Parse error"))
                    break
                if line == "" or line == b"":
                    break
                try:
                    message = decode_message(line)
                except (UnicodeError, ValueError, json.JSONDecodeError, RecursionError):
                    self._write(error_response(None, PARSE_ERROR, "Parse error"))
                    continue
                self._accept(message)
            self._drain_workers()
            return 0
        finally:
            self.close()

    def close(self) -> None:
        with self._state_lock:
            if self._cleaned:
                return
            self._cleaned = True
            self._closing = True
            for state in self._active.values():
                state.cancelled.set()
        cleanup = getattr(self.protocol.runtime, "cleanup", None)
        if callable(cleanup):
            try:
                cleanup()
            except Exception as error:
                try:
                    self.stderr.write("automation-bridge MCP cleanup failed: %s\n" % error)
                    self.stderr.flush()
                except Exception:
                    pass

    def cancel(self, request_id: Any) -> bool:
        with self._state_lock:
            state = self._active.get(request_id)
            if state is None or state.method == "initialize":
                return False
            state.cancelled.set()
        self.protocol.cancel(request_id)
        return True

    def _accept(self, message: Any) -> None:
        if self._is_cancellation(message):
            params = message["params"]
            self.cancel(params["requestId"])
            # Protocol.handle forwards to the runtime too; avoid forwarding the
            # same cancellation twice by treating it as consumed here.
            return

        method = message.get("method") if isinstance(message, dict) else None
        request_id = message.get("id") if isinstance(message, dict) else None
        is_request = (
            isinstance(message, dict)
            and "id" in message
            and _valid_id(request_id)
            and isinstance(method, str)
        )
        if is_request:
            with self._state_lock:
                existing = self._active.get(request_id)
                if existing is not None:
                    existing.cancelled.set()
                    self._write(
                        error_response(request_id, INVALID_REQUEST, "Duplicate request id")
                    )
                    return
        if is_request and method in self._CONCURRENT_METHODS:
            with self._state_lock:
                state = _RequestState(method)
                self._active[request_id] = state
                thread = threading.Thread(
                    target=self._run_request,
                    args=(message, request_id, state),
                    name="automation-bridge-mcp-%s" % request_id,
                    daemon=True,
                )
                self._threads.add(thread)
                thread.start()
            return

        response = self.protocol.handle(message)
        if response is not None:
            self._write(response)

    @staticmethod
    def _is_cancellation(message: Any) -> bool:
        if not isinstance(message, dict):
            return False
        if message.get("jsonrpc") != "2.0" or "id" in message:
            return False
        if message.get("method") != "notifications/cancelled":
            return False
        params = message.get("params")
        if not isinstance(params, dict) or not _valid_id(params.get("requestId")):
            return False
        if not set(params).issubset({"requestId", "reason", "_meta"}):
            return False
        if "reason" in params and not isinstance(params["reason"], str):
            return False
        if "_meta" in params and not isinstance(params["_meta"], dict):
            return False
        return True

    def _run_request(
        self,
        message: Dict[str, Any],
        request_id: Any,
        state: _RequestState,
    ) -> None:
        try:
            response = self.protocol.handle(message)
            with self._state_lock:
                if (
                    response is not None
                    and not state.cancelled.is_set()
                    and not self._closing
                ):
                    self._write(response)
                if self._active.get(request_id) is state:
                    del self._active[request_id]
        finally:
            with self._state_lock:
                self._threads.discard(threading.current_thread())

    def _drain_workers(self) -> None:
        deadline = time.monotonic() + self.eof_join_timeout
        while True:
            with self._state_lock:
                threads = list(self._threads)
            if not threads:
                return
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return
            threads[0].join(remaining)

    def _write(self, response: Mapping[str, Any]) -> None:
        try:
            frame = encode_message(response)
        except (TypeError, ValueError):
            request_id = response.get("id") if isinstance(response, dict) else None
            frame = encode_message(
                error_response(request_id, INTERNAL_ERROR, "Internal error")
            )
        with self._write_lock:
            self.stdout.write(frame + "\n")
            self.stdout.flush()


MCPStdioServer = StdioServer


__all__ = [
    "LEGACY_PROTOCOL_VERSIONS",
    "MODERN_PROTOCOL_VERSION",
    "SUPPORTED_PROTOCOL_VERSIONS",
    "MCPProtocol",
    "MCPStdioServer",
    "McpProtocol",
    "ProtocolError",
    "StdioServer",
    "decode_message",
    "encode_message",
    "error_response",
]
