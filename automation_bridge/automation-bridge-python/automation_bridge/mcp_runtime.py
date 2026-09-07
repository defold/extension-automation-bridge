"""Dependency-free Automation Bridge runtime exposed through MCP.

The adapter deliberately uses an explicit public-operation registry.  It never
accepts an arbitrary Python attribute name.  Stateful Python values are kept in
an opaque handle registry so editor/engine clients, streams, contexts,
recordings, profiler values, and stale-safe :class:`Element` snapshots can be
composed across tool calls.
"""

from __future__ import annotations

import base64
import dataclasses
import enum
import hashlib
import inspect
import json
import math
import os
import threading
import time
import uuid
from collections import deque
from collections.abc import Iterator, Mapping
from contextlib import ExitStack
from contextvars import ContextVar
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Optional, Sequence, Tuple

from automation_bridge import editor, engine
from automation_bridge.cancellation import check_cancelled, cancellable_sleep
from automation_bridge.elements import Element
from automation_bridge import mcp_schema
from automation_bridge.gestures import GestureGenerator
from automation_bridge.visual import VisualClient


JsonObject = Dict[str, Any]


class ToolFailure(Exception):
    """A stable, JSON-safe error returned by a runtime tool."""

    def __init__(
        self,
        code: str,
        message: str,
        data: Optional[Mapping[str, Any]] = None,
        retryable: bool = False,
        *,
        error_type: Optional[str] = None,
    ) -> None:
        super().__init__(message)
        self.code = str(code)
        self.message = str(message)
        self.data = dict(data or {})
        self.retryable = bool(retryable)
        self.error_type = str(error_type or self.__class__.__name__)

    def as_dict(self) -> JsonObject:
        result: JsonObject = {
            "code": self.code,
            "type": self.error_type,
            "message": self.message,
            "retryable": self.retryable,
        }
        if self.data:
            result["data"] = _plain_error_data(self.data)
        return result


class ToolResponse(dict):
    """JSON envelope plus trusted MCP content, kept out of serialized text."""

    def __init__(self, envelope: dict, images: Sequence[dict] = ()):
        super().__init__(envelope)
        self.image_content = list(images)


@dataclass
class RequestContext:
    request_id: Any
    token: engine.CancellationToken = field(default_factory=engine.CancellationToken)
    created: list = field(default_factory=list)
    session: str = "default"
    resources: dict = field(default_factory=dict)
    images: list = field(default_factory=list)
    engines: set = field(default_factory=set)
    scopes: ExitStack = field(default_factory=ExitStack)


_request_context: ContextVar[Optional[RequestContext]] = ContextVar("mcp_request", default=None)


def _resource_owner(value: Any) -> Any:
    seen = set()
    while id(value) not in seen:
        seen.add(id(value))
        attributes = vars(value) if hasattr(value, "__dict__") else {}
        parent = next((attributes[key] for key in ("_bridge", "bridge", "_client", "client", "_connection", "connection")
                       if key in attributes), None)
        if parent is None:
            break
        value = parent
    return value


def _resource_key(value: Any) -> Any:
    # The editor's last_command_result and command stream belong to the project.
    return ("editor", str(value.root)) if isinstance(value, editor.Client) else id(value)


class HandleRegistry:
    """Session-owned handles, with exclusive use of each Python resource per request.

    This coordinates access to local objects. Separate native client identities
    still execute concurrently under the engine's authoritative lease policy.
    """

    def __init__(self) -> None:
        self._values: Dict[str, Any] = {}
        self._identity: Dict[int, str] = {}
        self._owners: Dict[str, str] = {}
        self._resources: Dict[str, dict] = {}
        self._busy: Dict[Any, RequestContext] = {}
        self._retired: set = set()
        self._closed = False
        self._lock = threading.RLock()
        self.on_use: Optional[Callable[[Any], None]] = None

    def put(self, value: Any) -> str:
        with self._lock:
            if self._closed:
                raise ToolFailure("runtime_closed", "cannot retain objects after runtime shutdown")
            identity = id(value)
            existing = self._identity.get(identity)
            if existing is not None and self._values.get(existing) is value:
                self._check_owner(existing)
                return existing
            context = _request_context.get()
            token = "h_" + uuid.uuid4().hex
            self._values[token] = value
            self._identity[identity] = token
            self._owners[token] = context.session if context else "default"
            root = _resource_owner(value)
            if root is value and not isinstance(value, (engine.Client, editor.Client)) and context and context.resources:
                self._resources[token] = dict(context.resources)
            else:
                self._resources[token] = {_resource_key(root): root}
            if context is not None:
                context.created.append((token, value))
            return token

    def _check_owner(self, token: str) -> None:
        context = _request_context.get()
        if context is not None and self._owners[token] != context.session:
            raise ToolFailure("wrong_session", "handle belongs to another MCP session", {"handle": token})

    def get(self, token: str) -> Any:
        if not isinstance(token, str):
            raise ToolFailure("invalid_handle", "handle must be a string")
        with self._lock:
            if token not in self._values:
                raise ToolFailure("unknown_handle", "the handle is missing, expired, or already released", {"handle": token})
            self._check_owner(token)
            context = _request_context.get()
            if context is not None:
                if token in self._retired:
                    raise ToolFailure("handle_closing", "handle is being cleaned up")
                resources = self._resources[token]
                if any(key in self._busy and self._busy[key] is not context for key in resources):
                    raise ToolFailure("handle_busy", "another request is using this client or resource; wait for it to finish or use a separate client", retryable=True)
                for key, value in resources.items():
                    self._busy[key] = context
                    context.resources[key] = value
                    if self.on_use:
                        self.on_use(value)
            return self._values[token]

    def token_for(self, value: Any) -> Optional[str]:
        with self._lock:
            token = self._identity.get(id(value))
            return token if token is not None and self._values.get(token) is value else None

    def release(self, token: str) -> Any:
        with self._lock:
            if token not in self._values:
                raise ToolFailure("unknown_handle", "the handle is missing, expired, or already released", {"handle": token})
            self._check_owner(token)
            context = _request_context.get()
            if any(key in self._busy and self._busy[key] is not context for key in self._resources[token]):
                raise ToolFailure("handle_busy", "cannot release a resource while another request uses it", retryable=True)
            value = self._values.pop(token)
            if self._identity.get(id(value)) == token:
                del self._identity[id(value)]
            del self._owners[token]
            del self._resources[token]
            self._retired.discard(token)
            return value

    def related(self, token: str) -> List[Tuple[str, Any]]:
        with self._lock:
            roots = set(self._resources[token])
            return [(key, value) for key, value in self._values.items()
                    if self._owners[key] == self._owners[token] and roots.intersection(self._resources[key])]

    def snapshot(self, session: Optional[str] = None) -> List[Tuple[str, Any]]:
        with self._lock:
            return [(key, value) for key, value in self._values.items()
                    if session is None or self._owners[key] == session]

    def finish(self, context: RequestContext) -> None:
        with self._lock:
            for key in context.resources:
                if self._busy.get(key) is context:
                    del self._busy[key]

    def retire(self, session: Optional[str] = None) -> None:
        with self._lock:
            if session is None:
                self._closed = True
            self._retired.update(key for key in self._values if session is None or self._owners[key] == session)

    def idle_retired(self) -> List[Tuple[str, Any]]:
        with self._lock:
            return [(key, value) for key, value in self._values.items() if key in self._retired
                    and not any(resource in self._busy for resource in self._resources[key])]

    def cleanup(self) -> None:
        """Clear an unused standalone registry; the runtime finalizes values first."""
        self.retire()
        for key, _ in self.idle_retired():
            self.release(key)


def _qualified_type(value: Any) -> str:
    cls = type(value)
    return "%s.%s" % (cls.__module__, cls.__qualname__)


def _plain_error_data(value: Any, depth: int = 0) -> Any:
    """Best-effort JSON error details without handles or object repr leakage."""
    if depth > 12:
        return "<truncated>"
    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else str(value)
    if isinstance(value, Path):
        return str(value.expanduser().resolve())
    if isinstance(value, bytes):
        return {"encoding": "base64", "data": base64.b64encode(value).decode("ascii")}
    if dataclasses.is_dataclass(value):
        return _plain_error_data({field.name: getattr(value, field.name)
                                  for field in dataclasses.fields(value) if not field.name.startswith("_")}, depth + 1)
    if isinstance(value, BaseException):
        return {"type": type(value).__name__, "message": str(value)}
    if isinstance(value, Mapping):
        result = {}
        for key, item in value.items():
            text = str(key)
            if any(secret in text.lower() for secret in ("password", "secret", "token")):
                result[text] = "<redacted>"
            else:
                result[text] = _plain_error_data(item, depth + 1)
        return result
    if isinstance(value, (list, tuple, set, frozenset)):
        return [_plain_error_data(item, depth + 1) for item in value]
    return {"type": _qualified_type(value)}


def _wire_handle(value: Any, registry: HandleRegistry, preview: Any = None) -> JsonObject:
    result: JsonObject = {
        "$automation_bridge": "handle",
        "$handle": registry.put(value),
        "$type": _qualified_type(value),
    }
    if preview is not None:
        result["value"] = preview
    return result


def _is_context_or_iterator(value: Any) -> bool:
    return (
        hasattr(value, "__enter__")
        and hasattr(value, "__exit__")
    ) or isinstance(value, Iterator) or hasattr(value, "__next__")


def _operational_types() -> Tuple[type, ...]:
    candidates = (
        editor.Client,
        editor.Commands,
        editor.Debugger,
        editor.Console,
        editor.ConsoleStream,
        editor.Reference,
        editor.Preview,
        editor.Preferences,
        engine.Client,
        engine.InputController,
        engine.PointerSession,
        engine.EngineLogStream,
        engine.RuntimeLogs,
        engine.EventStream,
        engine.ProfilerClient,
        engine.ProfilerConnection,
        engine.ProfilerRecording,
        engine.ProfilerCapture,
        engine.ProfilerFrame,
        engine.ProfilerSample,
        engine.ProfilerPropertyFrame,
        engine.VideoRecordingClient,
        engine.VideoRecordingSession,
        engine.MetalCaptureClient,
        engine.TraceSession,
        GestureGenerator,
        VisualClient,
    )
    return tuple(dict.fromkeys(item for item in candidates if isinstance(item, type)))


_OPERATIONAL_TYPES = _operational_types()


def _public_property_names(value: Any) -> Tuple[str, ...]:
    names = set()
    for owner in type(value).__mro__:
        for name, descriptor in vars(owner).items():
            if not name.startswith("_") and isinstance(descriptor, property):
                names.add(name)
    return tuple(sorted(names))


def _dataclass_payload(
    value: Any,
    registry: HandleRegistry,
    seen: set,
    *,
    excluded_fields: Sequence[str] = (),
) -> JsonObject:
    """Serialize dataclass fields plus their public, computed wire properties."""
    excluded = set(excluded_fields)
    payload = {
        field.name: serialize(getattr(value, field.name), registry, seen)
        for field in dataclasses.fields(value)
        if not field.name.startswith("_") and field.name not in excluded
    }
    for name in _public_property_names(value):
        if name not in payload:
            payload[name] = serialize(getattr(value, name), registry, seen)
    return payload


def _operational_preview(value: Any, registry: HandleRegistry, seen: set) -> Any:
    """Return a bounded preview for retained stateful or queryable objects."""
    if isinstance(value, engine.ProfilerCapture):
        return {
            "frame_count": len(value.frames),
            "property_frame_count": len(value.property_frames),
        }
    if isinstance(value, engine.ProfilerFrame):
        return _dataclass_payload(value, registry, seen, excluded_fields=("root",))
    if isinstance(value, engine.ProfilerSample):
        preview = _dataclass_payload(value, registry, seen, excluded_fields=("children",))
        preview["child_count"] = len(value.children)
        return preview
    if isinstance(value, engine.ProfilerPropertyFrame):
        return {
            "property_frame": value.property_frame,
            "property_count": len(value.properties),
        }
    if dataclasses.is_dataclass(value):
        return _dataclass_payload(value, registry, seen)
    if isinstance(value, Mapping):
        return {
            str(key): serialize(item, registry, seen)
            for key, item in value.items()
        }
    return None


def serialize(
    value: Any,
    registry: HandleRegistry,
    _seen: Optional[set] = None,
) -> Any:
    """Convert a wrapper value into strict JSON while retaining useful objects."""
    if _seen is None:
        _seen = set()
    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ToolFailure("non_finite_number", "MCP results cannot contain NaN or Infinity")
        return value
    if isinstance(value, enum.Enum):
        return serialize(value.value, registry, _seen)
    if isinstance(value, Path):
        return str(value.expanduser().resolve())
    if isinstance(value, bytes):
        return {
            "$automation_bridge": "bytes",
            "$type": "bytes",
            "encoding": "base64",
            "data": base64.b64encode(value).decode("ascii"),
        }
    if isinstance(value, Element):
        _seen.add(id(value))
        try:
            preview = {
                name: serialize(getattr(value, name), registry, _seen)
                for name in _public_property_names(value)
            }
        finally:
            _seen.discard(id(value))
        return {
            "$automation_bridge": "element",
            "$type": "automation_bridge.elements.Element",
            "raw": serialize(value.raw, registry, _seen),
            "value": preview,
        }

    identity = id(value)
    if identity in _seen:
        return _wire_handle(value, registry)

    if isinstance(value, _OPERATIONAL_TYPES) or _is_context_or_iterator(value):
        preview = None
        _seen.add(identity)
        try:
            preview = _operational_preview(value, registry, _seen)
        except Exception:
            preview = None
        finally:
            _seen.discard(identity)
        return _wire_handle(value, registry, preview)

    if isinstance(value, Mapping):
        _seen.add(identity)
        try:
            return {
                str(key): serialize(item, registry, _seen)
                for key, item in sorted(value.items(), key=lambda pair: str(pair[0]))
            }
        finally:
            _seen.discard(identity)
    if isinstance(value, (list, tuple)):
        _seen.add(identity)
        try:
            return [serialize(item, registry, _seen) for item in value]
        finally:
            _seen.discard(identity)
    if isinstance(value, (set, frozenset)):
        encoded = [serialize(item, registry, _seen) for item in value]
        return sorted(encoded, key=lambda item: json.dumps(item, sort_keys=True, default=str))
    if dataclasses.is_dataclass(value):
        _seen.add(identity)
        try:
            return _dataclass_payload(value, registry, _seen)
        finally:
            _seen.discard(identity)
    to_dict = getattr(value, "to_dict", None)
    if callable(to_dict):
        try:
            return serialize(to_dict(), registry, _seen)
        except TypeError:
            pass
    isoformat = getattr(value, "isoformat", None)
    if callable(isoformat):
        try:
            return isoformat()
        except Exception:
            pass
    return _wire_handle(value, registry)


def deserialize(value: Any, registry: HandleRegistry) -> Any:
    """Resolve handles and lossless special wire values recursively."""
    if isinstance(value, list):
        return [deserialize(item, registry) for item in value]
    if not isinstance(value, dict):
        return value
    marker = value.get("$automation_bridge")
    handle = value.get("$handle")
    if marker == "handle" and isinstance(handle, str):
        return registry.get(handle)
    wire_type = value.get("$type")
    if marker == "element" and wire_type == "automation_bridge.elements.Element":
        raw = deserialize(value.get("raw", {}), registry)
        if not isinstance(raw, dict):
            raise ToolFailure("invalid_element", "Element.raw must be an object")
        return Element(raw)
    if marker == "bytes" and wire_type == "bytes":
        if value.get("encoding") != "base64" or not isinstance(value.get("data"), str):
            raise ToolFailure("invalid_bytes", "invalid base64 byte envelope")
        try:
            return base64.b64decode(value["data"], validate=True)
        except Exception as error:
            raise ToolFailure("invalid_bytes", "invalid base64 byte envelope") from error
    return {
        key: deserialize(item, registry)
        for key, item in value.items()
    }


@dataclass(frozen=True)
class OperationSpec:
    id: str
    qualified_name: str
    owner: Any
    owner_name: str
    member: str
    kind: str
    description: str
    signature: str
    availability: str = "direct"
    adapter: Optional[str] = None
    unsupported_parameters: Tuple[str, ...] = ()
    reason: Optional[str] = None
    read_only: bool = False
    destructive: bool = False

    def catalog_item(self, *, detail: bool = True) -> JsonObject:
        result: JsonObject = {
            "id": self.id,
            "qualified_name": self.qualified_name,
            "owner": self.owner_name,
            "name": self.member,
            "member": self.member,
            "kind": self.kind,
            "description": self.description,
            "signature": self.signature,
            "availability": self.availability,
            "read_only": self.read_only,
            "destructive": self.destructive,
        }
        if self.adapter:
            result["adapter"] = self.adapter
        if self.unsupported_parameters:
            result["unsupported_parameters"] = list(self.unsupported_parameters)
        if self.reason:
            result["reason"] = self.reason
        if detail:
            result["arguments_schema"] = mcp_schema.operation_arguments(self)
            if self.adapter == "declarative_operation_predicate":
                result["arguments_schema"] = _wait_schema()
        else:
            for key in ("signature", "qualified_name", "owner", "name", "member"):
                result.pop(key, None)
            result["description"] = self.description.split(". ", 1)[0][:180]
        return result


def _words(text: str) -> Tuple[str, ...]:
    return tuple(word for word in text.split() if word)


_OWNER_GROUPS = (
    ("automation_bridge.editor", editor, "function", _words(
        "open_project is_running installations latest_installation installation_registry_path doctor update_python_wrapper"
    )),
    ("automation_bridge.editor.Client", editor.Client, "method", _words(
        "update_automation_bridge connect_engine build_and_run clean_build_and_run build_and_run_html5 compile bob"
    )),
    ("automation_bridge.editor.Commands", editor.Commands, "method", _words(
        "fetch_libraries hot_reload rebundle reload_extensions reload_stylesheets catalog supports"
    )),
    ("automation_bridge.editor.Debugger", editor.Debugger, "method", _words(
        "start stop break_ continue_ detach step_into step_out step_over"
    )),
    ("automation_bridge.editor.Console", editor.Console, "method", _words("read stream")),
    ("automation_bridge.editor.ConsoleStream", editor.ConsoleStream, "method", _words("readline close")),
    ("automation_bridge.editor.Reference", editor.Reference, "method", _words("search")),
    ("automation_bridge.editor.Preview", editor.Preview, "method", _words("render")),
    ("automation_bridge.editor.Preferences", editor.Preferences, "method", _words("get set list describe")),
    ("automation_bridge.engine", engine, "function", _words("connect wait_until cancellation_scope")),
    ("automation_bridge.engine.Client", engine.Client, "method", _words(
        "wait_ready request health lifecycle require supports trace_metadata screen scene elements element "
        "elements_page application_catalog session_info close cancellation_scope "
        "maybe_element element_by_id parent count click drag drag_path pointer type_text key events "
        "wait_for_input_acknowledgement states state wait_for_state start_command command_status "
        "cancel_command wait_for_command command mark screenshot convert_point engine_info engine_log_port "
        "log_stream read_logs trace resize set_portrait set_landscape reboot close_engine dump_scene "
        "format_elements wait_for_element wait_for_count wait_frames observe_element wait_for_disappearance"
    )),
    ("automation_bridge.engine.InputController", engine.InputController, "method", _words(
        "configure pending status wait cancel flush interruption_scope"
    )),
    ("automation_bridge.engine.CancellationToken", engine.CancellationToken, "method", _words("cancel raise_if_cancelled")),
    ("automation_bridge.engine.PointerSession", engine.PointerSession, "method", _words("move hold up cancel")),
    ("automation_bridge.engine.EngineLogStream", engine.EngineLogStream, "method", _words("readline close")),
    ("automation_bridge.engine.RuntimeLogs", engine.RuntimeLogs, "method", _words("start tail close")),
    ("automation_bridge.engine.EventStream", engine.EventStream, "method", _words("poll wait close")),
    ("automation_bridge.engine.ProfilerClient", engine.ProfilerClient, "method", _words(
        "resources connect capture start_recording"
    )),
    ("automation_bridge.engine.ProfilerConnection", engine.ProfilerConnection, "method", _words(
        "from_url start stop get_frame aggregate_frame start_recording get_properties capture"
    )),
    ("automation_bridge.engine.ProfilerRecording", engine.ProfilerRecording, "method", _words(
        "start stop abort snapshot"
    )),
    ("automation_bridge.engine.ProfilerCapture", engine.ProfilerCapture, "method", _words(
        "aggregate scopes scope counters counter"
    )),
    ("automation_bridge.engine.ProfilerFrame", engine.ProfilerFrame, "method", _words(
        "samples missing_name_hashes resolve_names aggregate"
    )),
    ("automation_bridge.engine.ProfilerSample", engine.ProfilerSample, "method", _words("walk")),
    ("automation_bridge.engine.ProfilerPropertyFrame", engine.ProfilerPropertyFrame, "method", _words(
        "missing_name_hashes resolve_names entries find"
    )),
    ("automation_bridge.engine.VideoRecordingClient", engine.VideoRecordingClient, "method", _words(
        "capabilities status start"
    )),
    ("automation_bridge.engine.VideoRecordingSession", engine.VideoRecordingSession, "method", _words("stop")),
    ("automation_bridge.engine.MetalCaptureClient", engine.MetalCaptureClient, "method", _words(
        "status start wait stop"
    )),
    ("automation_bridge.engine.TraceSession", engine.TraceSession, "method", _words(
        "record record_event record_state record_input_acknowledgement record_profiler "
        "record_selector_error record_cleanup capture_screenshot close replay"
    )),
    ("automation_bridge.gestures.GestureGenerator", GestureGenerator, "method", _words("generate_drag")),
    ("automation_bridge.visual.VisualClient", VisualClient, "method", _words(
        "difference wait_for_stable_frame wait_for_region_change assert_matches"
    )),
)


_PROPERTY_GROUPS = (
    ("automation_bridge.editor.Client", editor.Client, _words(
        "root port base_url lifecycle_events commands debugger console reference preview preferences last_command_result"
    )),
    ("automation_bridge.engine.Client", engine.Client, _words(
        "port timeout base_url client_id session_id input logs engine_instance_id profiler gestures visual "
        "video_recording metal_capture profiler_url last_window_size owns_engine closed"
    )),
    ("automation_bridge.engine.CancellationToken", engine.CancellationToken, _words("cancelled")),
    ("automation_bridge.engine.PointerSession", engine.PointerSession, _words("receipt lease closed input_id")),
    ("automation_bridge.engine.EngineLogStream", engine.EngineLogStream, _words("host port closed")),
    ("automation_bridge.engine.EventStream", engine.EventStream, _words("cursor")),
    ("automation_bridge.engine.ProfilerClient", engine.ProfilerClient, _words("url")),
    ("automation_bridge.engine.ProfilerConnection", engine.ProfilerConnection, _words("connected sample_names")),
    ("automation_bridge.engine.ProfilerRecording", engine.ProfilerRecording, _words(
        "running frame_count property_frame_count"
    )),
)


_ADAPTATIONS: Dict[str, Dict[str, Any]] = {
    "automation_bridge.editor.Preview.render": {
        "availability": "adapted", "adapter": "png_bytes_to_mcp_image",
        "reason": "PNG bytes become an MCP image block with structured size and hash evidence.",
    },
    "automation_bridge.editor.Preferences.get": {
        "availability": "restricted",
        "reason": "Password preference values are never returned over MCP; all non-secret preferences remain readable.",
    },
    "automation_bridge.engine.Client.require": {
        "availability": "adapted", "adapter": "capabilities_array_to_varargs"
    },
    "automation_bridge.engine.Client.reboot": {
        "availability": "adapted", "adapter": "args_array_to_varargs"
    },
    "automation_bridge.engine.Client.wait_ready": {
        "availability": "restricted", "unsupported_parameters": ("retry_exceptions",),
        "reason": "Python exception classes cannot cross JSON; the wrapper defaults remain available.",
    },
    "automation_bridge.engine.Client.screenshot": {
        "availability": "restricted", "unsupported_parameters": ("retry_exceptions",),
        "reason": "Python exception classes cannot cross JSON; the wrapper defaults remain available.",
    },
    "automation_bridge.engine.Client.wait_for_element": {
        "availability": "restricted", "unsupported_parameters": ("retry_exceptions",),
        "reason": "Python exception classes cannot cross JSON; selector retries use the wrapper default.",
    },
    "automation_bridge.engine.Client.wait_for_count": {
        "availability": "restricted", "unsupported_parameters": ("retry_exceptions",),
        "reason": "Python exception classes cannot cross JSON; selector retries use the wrapper default.",
    },
    "automation_bridge.engine.ProfilerClient.start_recording": {
        "availability": "restricted", "unsupported_parameters": ("on_finalize", "on_abort"),
        "reason": "Python callbacks become explicit stop/abort and capture tool calls.",
    },
    "automation_bridge.engine.ProfilerConnection.start_recording": {
        "availability": "restricted", "unsupported_parameters": ("on_finalize", "on_abort"),
        "reason": "Python callbacks become explicit stop/abort and capture tool calls.",
    },
    "automation_bridge.engine.ProfilerRecording.abort": {
        "availability": "adapted", "adapter": "cause_text_to_RuntimeError"
    },
    "automation_bridge.engine.VideoRecordingClient.start": {
        "availability": "adapted", "adapter": "size_array_to_tuple"
    },
    "automation_bridge.engine.TraceSession.record_selector_error": {
        "availability": "adapted", "adapter": "error_text_to_RuntimeError"
    },
    "automation_bridge.engine.ProfilerFrame.resolve_names": {
        "availability": "adapted", "adapter": "decimal_string_keys_to_int"
    },
    "automation_bridge.engine.ProfilerPropertyFrame.resolve_names": {
        "availability": "adapted", "adapter": "decimal_string_keys_to_int"
    },
    "automation_bridge.engine.ProfilerConnection.sample_names": {
        "availability": "adapted", "adapter": "int_keys_to_decimal_strings"
    },
    "automation_bridge.engine.wait_until": {
        "availability": "adapted", "adapter": "declarative_operation_predicate",
        "reason": "The Python callable and predicate are represented by an allowlisted operation and JSON predicate.",
    },
    "automation_bridge.engine.InputController.interruption_scope": {
        "availability": "adapted", "adapter": "context_handle_with_explicit_exit",
        "reason": "The Python context manager is retained and entered/exited with explicit handle tools.",
    },
}


for _cancellation_member in (
    "automation_bridge.engine.cancellation_scope",
    "automation_bridge.engine.Client.cancellation_scope",
    "automation_bridge.engine.CancellationToken.cancel",
    "automation_bridge.engine.CancellationToken.raise_if_cancelled",
    "automation_bridge.engine.CancellationToken.cancelled",
):
    _ADAPTATIONS[_cancellation_member] = {
        "availability": "restricted", "adapter": "mcp_request_cancellation",
        "reason": "Cancellation tokens and scopes belong to each MCP request. Use the host cancellation notification; Python scopes cannot span MCP worker threads.",
    }


_READ_ONLY_MEMBERS = {
    "doctor", "catalog", "elements_page", "application_catalog", "session_info",
    "is_running", "installations", "latest_installation", "installation_registry_path",
    "read", "search", "get", "list", "describe", "health", "lifecycle", "supports",
    "trace_metadata", "screen", "scene", "elements", "element", "maybe_element",
    "element_by_id", "parent", "count", "states", "state", "command_status",
    "engine_info", "engine_log_port", "read_logs", "dump_scene", "format_elements",
    "pending", "status", "poll", "resources", "get_frame", "aggregate_frame",
    "get_properties", "snapshot", "aggregate", "scopes", "scope", "counters",
    "counter", "samples", "missing_name_hashes", "entries", "find", "capabilities",
    "difference", "walk",
}


_DESTRUCTIVE = {
    "automation_bridge.engine.Client.close_engine",
    "automation_bridge.engine.Client.request",
    "automation_bridge.editor.Client.update_automation_bridge",
    "automation_bridge.editor.update_python_wrapper",
}


def _member_signature(member: Any, kind: str) -> str:
    if kind == "property":
        return "property"
    try:
        return str(inspect.signature(member))
    except (TypeError, ValueError):
        return "(...)"


def _member_description(owner: Any, member_name: str, kind: str) -> str:
    try:
        raw = inspect.getattr_static(owner, member_name)
    except AttributeError:
        return "%s %s.%s." % (
            kind.capitalize(),
            getattr(owner, "__name__", owner),
            member_name,
        )
    if isinstance(raw, property):
        value = raw.fget
    elif isinstance(raw, (classmethod, staticmethod)):
        value = raw.__func__
    else:
        value = getattr(owner, member_name)
    text = inspect.getdoc(value) or inspect.getdoc(raw)
    if text:
        return " ".join(text.strip().split())
    return "%s %s.%s." % (kind.capitalize(), getattr(owner, "__name__", owner), member_name)


def _build_operation_specs() -> Tuple[OperationSpec, ...]:
    specs: List[OperationSpec] = []
    for owner_name, owner, kind, members in _OWNER_GROUPS:
        for member_name in members:
            if not hasattr(owner, member_name):
                raise RuntimeError("public Automation Bridge API is missing: %s.%s" % (owner_name, member_name))
            qualified = owner_name + "." + member_name
            adaptation = dict(_ADAPTATIONS.get(qualified, {}))
            raw = inspect.getattr_static(owner, member_name)
            callable_member = getattr(owner, member_name)
            specs.append(OperationSpec(
                id=qualified,
                qualified_name=qualified,
                owner=owner,
                owner_name=owner_name,
                member=member_name,
                kind=kind,
                description=_member_description(owner, member_name, kind),
                signature=_member_signature(callable_member, kind),
                availability=adaptation.pop("availability", "direct"),
                adapter=adaptation.pop("adapter", None),
                unsupported_parameters=tuple(adaptation.pop("unsupported_parameters", ())),
                reason=adaptation.pop("reason", None),
                read_only=member_name in _READ_ONLY_MEMBERS,
                destructive=qualified in _DESTRUCTIVE,
            ))
    for owner_name, owner, members in _PROPERTY_GROUPS:
        for member_name in members:
            qualified = owner_name + "." + member_name
            adaptation = dict(_ADAPTATIONS.get(qualified, {}))
            specs.append(OperationSpec(
                id=qualified,
                qualified_name=qualified,
                owner=owner,
                owner_name=owner_name,
                member=member_name,
                kind="property",
                description=_member_description(owner, member_name, "property"),
                signature="property",
                availability=adaptation.pop("availability", "direct"),
                adapter=adaptation.pop("adapter", None),
                unsupported_parameters=tuple(adaptation.pop("unsupported_parameters", ())),
                reason=adaptation.pop("reason", None),
                read_only=True,
                destructive=False,
            ))
    return tuple(sorted(specs, key=lambda item: item.qualified_name))


OPERATION_SPECS = _build_operation_specs()


def _object_schema(
    properties: Optional[Mapping[str, Any]] = None,
    required: Sequence[str] = (),
) -> JsonObject:
    return {
        "type": "object",
        "properties": dict(properties or {}),
        "required": list(required),
        "additionalProperties": False,
    }


_ANY_JSON: JsonObject = {}
_STRING = {"type": "string"}
_NUMBER = {"type": "number"}
_BOOLEAN = {"type": "boolean"}
_INTEGER = {"type": "integer"}
_HANDLE_VALUE: JsonObject = mcp_schema.HANDLE
_OPEN_OBJECT: JsonObject = {"type": "object", "additionalProperties": True}


def _wait_schema() -> JsonObject:
    return _object_schema({"operation": _STRING, "target": _HANDLE_VALUE, "arguments": _OPEN_OBJECT,
                           "path": _STRING, "predicate": _object_schema({"operator": {"type": "string", "enum": ["truthy", "equals", "not_equals", "exists"]}, "value": {}}),
                           "timeout": {"type": "number", "minimum": 0, "maximum": 300},
                           "interval": {"type": "number", "minimum": 0, "maximum": 60}}, ("operation",))


def _output_schema() -> JsonObject:
    return _object_schema(
        {
            "ok": _BOOLEAN,
            "data": _ANY_JSON,
            "error": _object_schema(
                {
                    "code": _STRING,
                    "type": _STRING,
                    "message": _STRING,
                    "retryable": _BOOLEAN,
                    "data": _ANY_JSON,
                },
                required=("code", "type", "message", "retryable"),
            ),
        },
        required=("ok",),
    )


def _tool_descriptor(
    name: str,
    title: str,
    description: str,
    input_schema: JsonObject,
    *,
    read_only: bool,
    destructive: bool = False,
    idempotent: bool = False,
) -> JsonObject:
    return {
        "name": name,
        "title": title,
        "description": description,
        "inputSchema": input_schema,
        "outputSchema": _output_schema(),
        "annotations": {
            "readOnlyHint": bool(read_only),
            "destructiveHint": bool(destructive),
            "idempotentHint": bool(idempotent),
            "openWorldHint": False,
        },
    }


_SELECTOR_KEYS = frozenset((
    "id", "instance_id", "logical_id", "type", "type_exact", "name", "name_exact",
    "text", "text_exact", "url", "url_exact", "path", "enabled", "kind",
    "has_bounds", "visible_and_enabled", "visible", "case_sensitive", "automation_id",
    "localization_key", "role", "include", "limit", "offset", "cursor",
))


def _target_token(wire: Any, registry: HandleRegistry) -> Optional[str]:
    if isinstance(wire, dict) and isinstance(wire.get("$handle"), str):
        return wire["$handle"]
    if isinstance(wire, str) and wire.startswith("h_"):
        try:
            registry.get(wire)
        except ToolFailure:
            return None
        return wire
    value = deserialize(wire, registry)
    return registry.token_for(value)


def _exception_failure(error: Exception, *, read_only: bool = False) -> ToolFailure:
    if isinstance(error, ToolFailure):
        return error
    name = type(error).__name__
    code = "".join(
        ("_" + character.lower()) if character.isupper() else character
        for character in name
    ).lstrip("_") or "tool_error"
    retryable_names = {
        "ConnectionError", "TimeoutError", "CommandTimeout", "WaitTimeoutError",
        "EventBufferOverflow",
    }
    details: Dict[str, Any] = {}
    for key in (
        "status", "method", "url", "command_id", "timeout", "elapsed", "attempts",
        "scene_sequence", "requested_cursor", "oldest_cursor", "latest_cursor", "receipt",
        "issues", "response", "code", "result", "minimum_version", "cleanup_error", "reason",
    ):
        if hasattr(error, key):
            value = getattr(error, key)
            if value is not None:
                details[key] = _plain_error_data(value)
    return ToolFailure(
        code,
        str(error) or name,
        details,
        retryable=read_only and (name in retryable_names or name.endswith("TimeoutError")),
        error_type=name,
    )


class BridgeRuntime:
    """Stateful, allowlisted Automation Bridge tool and resource runtime."""

    def __init__(self, project_root: Optional[Path] = None) -> None:
        self.project_root = (
            Path(project_root).expanduser().resolve()
            if project_root is not None
            else self._discover_root()
        )
        self.handles = HandleRegistry()
        self.handle_registry = self.handles
        self.handles.on_use = self._bind_engine
        self._sessions = {"default": True}
        self._identity_claims = {}
        self._operation_specs = {spec.id: spec for spec in OPERATION_SPECS}
        self.operation_handlers: Dict[str, Callable[..., Any]] = {
            spec.qualified_name: self._operation_handler(spec)
            for spec in OPERATION_SPECS
        }
        self._entered: set = set()
        self._requests: Dict[Any, RequestContext] = {}
        self.cleanup_errors = deque(maxlen=50)
        self._lock = threading.RLock()
        self._closed = False
        self.tool_handlers: Dict[str, Callable[[Mapping[str, Any]], Any]] = {
            "automation_bridge_call": self._tool_call,
            "automation_bridge_session": self._tool_session,
            "automation_bridge_catalog": self._tool_catalog,
            "automation_bridge_describe": self._tool_describe,
            "defold_editor_capabilities": self._focused_editor_capabilities,
            "automation_bridge_destructive_call": self._tool_destructive_call,
            "automation_bridge_enter": self._tool_enter,
            "automation_bridge_exit": self._tool_exit,
            "automation_bridge_get": self._tool_get,
            "automation_bridge_next": self._tool_next,
            "automation_bridge_release": self._tool_release,
            "automation_bridge_wait": self._tool_wait,
            "defold_build_and_run": self._focused_build_and_run,
            "defold_compile": self._focused_compile,
            "defold_bob": self._focused_bob,
            "defold_click": self._focused_click,
            "defold_close_engine": self._focused_close_engine,
            "defold_command": self._focused_command,
            "defold_connect_engine": self._focused_connect_engine,
            "defold_drag": self._focused_drag,
            "defold_find_elements": self._focused_find_elements,
            "defold_get_element": self._focused_get_element,
            "defold_health": self._focused_health,
            "defold_doctor": self._focused_doctor,
            "defold_update_python_wrapper": self._focused_update_python_wrapper,
            "defold_application_catalog": self._focused_application_catalog,
            "defold_session_info": self._focused_session_info,
            "defold_close": self._focused_close,
            "defold_key": self._focused_key,
            "defold_open_project": self._focused_open_project,
            "defold_screenshot": self._focused_screenshot,
            "defold_preview": self._focused_preview,
            "defold_observe": self._focused_observe,
            "defold_type_text": self._focused_type_text,
            "defold_wait_for_element": self._focused_wait_for_element,
            "defold_wait_for_event": self._focused_wait_for_event,
            "defold_wait_for_state": self._focused_wait_for_state,
        }
        self._tools = self._build_tool_descriptors()
        self._resources = self._build_resource_descriptors()

    @staticmethod
    def _discover_root() -> Path:
        configured = os.environ.get("PLUGIN_ROOT")
        if configured:
            candidate = Path(configured).expanduser().resolve()
            if candidate.is_dir():
                return candidate
        source = Path(__file__).resolve()
        for parent in source.parents:
            if (parent / "plugin.json").is_file() and (parent / "mcp.json").is_file():
                return parent
            if (parent / "automation_bridge" / "automation-bridge-python").is_dir():
                return parent
        return Path.cwd().resolve()

    def _operation_handler(self, spec: OperationSpec) -> Callable[..., Any]:
        def handler(target: Any = None, arguments: Optional[Mapping[str, Any]] = None) -> Any:
            return self._invoke_operation(spec, target, dict(arguments or {}))

        handler.__name__ = "dispatch_" + spec.member
        return handler

    def tool_descriptors(self) -> List[JsonObject]:
        return json.loads(json.dumps(self._tools, sort_keys=True))

    def resource_descriptors(self) -> List[JsonObject]:
        return json.loads(json.dumps(self._resources, sort_keys=True))

    def catalog(
        self,
        query: Optional[str] = None,
        cursor: int = 0,
        limit: int = 20,
        detail: bool = False,
    ) -> JsonObject:
        if not isinstance(cursor, int) or isinstance(cursor, bool) or cursor < 0:
            raise ToolFailure("invalid_cursor", "cursor must be a non-negative integer")
        if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= 10_000:
            raise ToolFailure("invalid_limit", "limit must be between 1 and 10000")
        items = [spec.catalog_item(detail=detail) for spec in OPERATION_SPECS]
        if query is not None:
            if not isinstance(query, str):
                raise ToolFailure("invalid_query", "query must be a string")
            needle = query.casefold().strip()
            if needle:
                items = [
                    item for item in items
                    if all(word in (item["id"] + " " + self._operation_specs[item["id"]].description).casefold() for word in needle.split())
                ]
        page = items[cursor:cursor + limit]
        next_cursor = cursor + len(page)
        return {
            "items": page,
            "source": "python_wrapper",
            "total": len(items),
            "next_cursor": next_cursor if next_cursor < len(items) else None,
        }

    def prepare_request(self, request_id: Any) -> None:
        """Register before starting a worker so immediate cancellation is retained."""
        with self._lock:
            if request_id not in self._requests:
                context = RequestContext(request_id)
                if self._closed:
                    context.token.cancel("MCP runtime closed")
                self._requests[request_id] = context

    def finish_request(self, request_id: Any) -> None:
        with self._lock:
            context = self._requests.pop(request_id, None)
            self._identity_claims = {pair: owner for pair, owner in self._identity_claims.items() if owner != request_id}
        if context is None:
            return
        self.handles.finish(context)
        if context.token.cancelled or self._closed or not self._sessions.get(context.session):
            for token, value in reversed(context.created):
                if self.handles.token_for(value) == token:
                    self._cleanup_value(token, value, failure=True)
                    self.handles.release(token)
        self._cleanup_retired()

    def _cleanup_retired(self) -> None:
        # Snapshot and cleanup can run on shutdown and worker completion threads.
        with self._lock:
            for token, value in reversed(self.handles.idle_retired()):
                self._cleanup_value(token, value, failure=True)
                self.handles.release(token)

    def call_tool_request(self, request_id: Any, name: str, arguments: Mapping[str, Any]) -> JsonObject:
        with self._lock:
            owned = request_id not in self._requests
            self.prepare_request(request_id)
            context = self._requests[request_id]
        previous = _request_context.set(context)
        result = None
        try:
            if not isinstance(name, str) or name not in self.tool_handlers:
                raise ToolFailure("unknown_tool", "unknown Automation Bridge tool", {"name": name})
            if not isinstance(arguments, Mapping):
                raise ToolFailure("invalid_arguments", "tool arguments must be an object")
            arguments = dict(arguments)
            descriptor = next(tool for tool in self._tools if tool["name"] == name)
            # Required/unknown fields retain the established structured errors.
            self._expect_keys(arguments, required=descriptor["inputSchema"].get("required", ()),
                              optional=tuple(descriptor["inputSchema"]["properties"]))
            try:
                mcp_schema.validate(arguments, descriptor["inputSchema"])
            except ValueError as error:
                raise ToolFailure("invalid_arguments", str(error)) from error
            session = arguments.pop("mcp_session", "default")
            if not isinstance(session, str) or not self._sessions.get(session):
                raise ToolFailure("unknown_session", "MCP session is missing or closed")
            context.session = session
            if self._closed:
                raise ToolFailure("runtime_closed", "the Automation Bridge MCP runtime is closed")
            with engine.cancellation_scope(context.token), context.scopes:
                result = self.tool_handlers[name](dict(arguments))
                check_cancelled()
                self._capture_images(result)
                check_cancelled()
                with self._lock:
                    check_cancelled()
                    if self._closed:
                        raise engine.OperationCancelled("MCP runtime closed")
                    data = serialize(result, self.handles)
            return ToolResponse({"ok": True, "data": data}, context.images)
        except Exception as error:
            if isinstance(error, engine.OperationCancelled):
                self._discard_unretained(result)
                self._record_cleanup_error(request_id, error)
            read_only = next((tool["annotations"]["readOnlyHint"] for tool in self._tools if tool["name"] == name), False)
            if name in {"automation_bridge_call", "automation_bridge_destructive_call"} and isinstance(arguments, Mapping):
                spec = self._operation_specs.get(arguments.get("operation")) if isinstance(arguments.get("operation"), str) else None
                read_only = bool(spec and spec.read_only)
            failure = _exception_failure(error, read_only=read_only)
            return {"ok": False, "error": failure.as_dict()}
        finally:
            _request_context.reset(previous)
            if owned:
                self.finish_request(request_id)

    def call_tool(self, name: str, arguments: Mapping[str, Any]) -> JsonObject:
        return self.call_tool_request(uuid.uuid4().hex, name, arguments)

    def cancel(self, request_id: Any) -> None:
        with self._lock:
            context = self._requests.get(request_id)
            if context is not None:
                context.token.cancel("MCP request cancelled")

    def _record_cleanup_error(self, request_id: Any, error: Exception, session: Optional[str] = None) -> None:
        with self._lock:
            context = _request_context.get()
            self.cleanup_errors.append({"request_id": request_id, "mcp_session": session if session is not None else (context.session if context else None), "error": _exception_failure(error).as_dict()})

    def _discard_unretained(self, value: Any) -> None:
        if isinstance(value, Mapping):
            for item in value.values():
                self._discard_unretained(item)
        elif isinstance(value, (list, tuple)):
            for item in value:
                self._discard_unretained(item)
        elif value is not None and self.handles.token_for(value) is None:
            self._cleanup_value("", value, failure=True)

    def _bind_engine(self, value: Any) -> None:
        context = _request_context.get()
        if context is None:
            return
        seen = set()
        while value is not None and id(value) not in seen:
            seen.add(id(value))
            if isinstance(value, engine.Client):
                if id(value) not in context.engines:
                    context.scopes.enter_context(value.cancellation_scope(context.token))
                    context.engines.add(id(value))
                return
            attributes = vars(value) if hasattr(value, "__dict__") else {}
            value = next((attributes[key] for key in ("_bridge", "bridge", "_client", "client") if key in attributes), None)

    def _expect_keys(
        self,
        arguments: Mapping[str, Any],
        *,
        required: Sequence[str] = (),
        optional: Sequence[str] = (),
    ) -> None:
        required_keys = set(required)
        keys = set(arguments)
        missing = required_keys - keys
        unknown = keys - required_keys - set(optional)
        if missing:
            raise ToolFailure("missing_argument", "missing required tool argument", {"fields": sorted(missing)})
        if unknown:
            raise ToolFailure("unknown_argument", "unknown tool argument", {"fields": sorted(unknown)})

    def _spec(self, operation: Any) -> OperationSpec:
        if not isinstance(operation, str) or operation not in self._operation_specs:
            raise ToolFailure("unknown_operation", "operation is not in the public API catalog", {"operation": operation})
        return self._operation_specs[operation]

    def _resolve_target(self, wire: Any) -> Any:
        value = deserialize(wire, self.handles)
        if isinstance(value, str) and value.startswith("h_"):
            value = self.handles.get(value)
        self._bind_engine(value)
        return value

    @staticmethod
    def _static_kind(owner: Any, member: str) -> str:
        raw = inspect.getattr_static(owner, member)
        if isinstance(raw, classmethod):
            return "classmethod"
        if isinstance(raw, staticmethod):
            return "staticmethod"
        return "instance"

    def _invoke_operation(
        self,
        spec: OperationSpec,
        target_wire: Any,
        arguments: Mapping[str, Any],
    ) -> Any:
        if not isinstance(arguments, Mapping):
            raise ToolFailure("invalid_arguments", "operation arguments must be an object")
        if spec.adapter == "mcp_request_cancellation":
            raise ToolFailure("request_cancellation", spec.reason, {"operation": spec.id})
        kwargs = deserialize(dict(arguments), self.handles)
        if spec.qualified_name in {
            "automation_bridge.engine.connect", "automation_bridge.editor.Client.connect_engine",
            "automation_bridge.editor.Client.build_and_run", "automation_bridge.editor.Client.clean_build_and_run",
        }:
            kwargs = self._connection_identity(kwargs)
        if not isinstance(kwargs, dict):
            raise ToolFailure("invalid_arguments", "operation arguments must be an object")
        prohibited = sorted(set(kwargs) & set(spec.unsupported_parameters))
        if prohibited:
            raise ToolFailure(
                "unsupported_parameter",
                spec.reason or "the parameter cannot be represented over MCP",
                {"operation": spec.qualified_name, "parameters": prohibited},
            )
        if spec.qualified_name == "automation_bridge.editor.Preferences.get":
            preference = kwargs.get("preference")
            path = getattr(preference, "path", preference)
            if isinstance(path, str) and "password" in path.casefold():
                raise ToolFailure(
                    "sensitive_preference",
                    "password preference values are not returned over MCP",
                    {"path": path},
                )
        if spec.qualified_name == "automation_bridge.engine.wait_until":
            return self._wait_declarative(kwargs)

        if spec.kind == "function":
            if target_wire is not None:
                raise ToolFailure("unexpected_target", "module functions do not accept a target handle")
            function = getattr(spec.owner, spec.member)
            return function(**kwargs)

        static_kind = self._static_kind(spec.owner, spec.member) if hasattr(spec.owner, spec.member) else "instance"
        if static_kind in {"classmethod", "staticmethod"}:
            if target_wire is not None:
                raise ToolFailure("unexpected_target", "%s does not accept a target handle" % static_kind)
            function = getattr(spec.owner, spec.member)
        else:
            if target_wire is None:
                raise ToolFailure("missing_target", "this operation requires an object handle", {"operation": spec.id})
            target = self._resolve_target(target_wire)
            if not isinstance(target, spec.owner):
                raise ToolFailure(
                    "wrong_target_type",
                    "the handle has the wrong Python type",
                    {"expected": spec.owner_name, "actual": _qualified_type(target)},
                )
            if spec.kind == "property":
                if kwargs:
                    raise ToolFailure("unexpected_arguments", "properties do not accept arguments")
                return getattr(target, spec.member)
            function = getattr(target, spec.member)

        if spec.adapter == "capabilities_array_to_varargs":
            capabilities = kwargs.pop("capabilities", kwargs.pop("args", ()))
            if not isinstance(capabilities, (list, tuple)) or not all(isinstance(item, str) for item in capabilities):
                raise ToolFailure("invalid_capabilities", "capabilities must be an array of strings")
            return function(*capabilities, **kwargs)
        if spec.adapter == "args_array_to_varargs":
            positional = kwargs.pop("args", ())
            if not isinstance(positional, (list, tuple)) or not all(isinstance(item, str) for item in positional):
                raise ToolFailure("invalid_args", "args must be an array of strings")
            return function(*positional, **kwargs)
        if spec.adapter == "cause_text_to_RuntimeError":
            cause = kwargs.pop("cause", None)
            if isinstance(cause, str):
                cause = RuntimeError(cause)
            return function(cause, **kwargs)
        if spec.adapter == "error_text_to_RuntimeError" and isinstance(kwargs.get("error"), str):
            kwargs["error"] = RuntimeError(kwargs["error"])
        if spec.adapter == "decimal_string_keys_to_int" and "names" in kwargs:
            names = kwargs["names"]
            if not isinstance(names, Mapping):
                raise ToolFailure("invalid_names", "names must be an object with decimal integer keys")
            try:
                kwargs["names"] = {int(key): value for key, value in names.items()}
            except (TypeError, ValueError) as error:
                raise ToolFailure("invalid_names", "name-map keys must be decimal integers") from error
        if spec.adapter == "size_array_to_tuple" and isinstance(kwargs.get("size"), list):
            size = kwargs["size"]
            if len(size) != 2 or not all(isinstance(item, int) and not isinstance(item, bool) for item in size):
                raise ToolFailure("invalid_size", "size must contain exactly two integer dimensions")
            kwargs["size"] = tuple(size)
        result = function(**kwargs)
        if spec.qualified_name == "automation_bridge.editor.Preview.render":
            return self._preview_result(result, kwargs.get("path"))
        return result

    def _tool_catalog(self, arguments: Mapping[str, Any]) -> Any:
        self._expect_keys(arguments, optional=("query", "cursor", "limit"))
        return self.catalog(
            query=arguments.get("query"),
            cursor=arguments.get("cursor", 0),
            limit=arguments.get("limit", 20),
        )

    def _tool_describe(self, arguments: Mapping[str, Any]) -> Any:
        self._expect_keys(arguments, required=("operation",))
        return self._spec(arguments["operation"]).catalog_item()

    def _focused_editor_capabilities(self, arguments: Mapping[str, Any]) -> Any:
        self._expect_keys(arguments, required=("project",), optional=("refresh",))
        return {"source": "connected_editor", "commands": self._editor_target(arguments).commands.catalog(refresh=arguments.get("refresh", False))}

    def _tool_call(self, arguments: Mapping[str, Any]) -> Any:
        self._expect_keys(arguments, required=("operation",), optional=("target", "arguments"))
        spec = self._spec(arguments["operation"])
        if spec.kind == "property":
            raise ToolFailure("wrong_operation_kind", "use automation_bridge_get for properties")
        if spec.destructive:
            raise ToolFailure(
                "destructive_operation_requires_tool",
                "use automation_bridge_destructive_call with confirm=true",
                {"operation": spec.id},
            )
        nested = arguments.get("arguments", {})
        if not isinstance(nested, Mapping):
            raise ToolFailure("invalid_arguments", "arguments must be an object")
        return self.operation_handlers[spec.qualified_name](arguments.get("target"), nested)

    def _tool_destructive_call(self, arguments: Mapping[str, Any]) -> Any:
        self._expect_keys(
            arguments,
            required=("operation", "confirm"),
            optional=("target", "arguments"),
        )
        spec = self._spec(arguments["operation"])
        if not spec.destructive:
            raise ToolFailure(
                "wrong_operation_kind",
                "use automation_bridge_call for non-destructive operations",
                {"operation": spec.id},
            )
        if arguments.get("confirm") is not True:
            raise ToolFailure(
                "confirmation_required",
                "this operation is destructive and requires confirm=true",
                {"operation": spec.id},
            )
        nested = arguments.get("arguments", {})
        if not isinstance(nested, Mapping):
            raise ToolFailure("invalid_arguments", "arguments must be an object")
        return self.operation_handlers[spec.qualified_name](arguments.get("target"), nested)

    def _tool_get(self, arguments: Mapping[str, Any]) -> Any:
        self._expect_keys(arguments, required=("operation", "target"))
        spec = self._spec(arguments["operation"])
        if spec.kind != "property":
            raise ToolFailure("wrong_operation_kind", "use automation_bridge_call for functions and methods")
        return self.operation_handlers[spec.qualified_name](arguments["target"], {})

    def _tool_enter(self, arguments: Mapping[str, Any]) -> Any:
        self._expect_keys(arguments, required=("target",))
        target = self._resolve_target(arguments["target"])
        enter = getattr(target, "__enter__", None)
        if not callable(enter):
            raise ToolFailure("not_a_context", "target does not implement __enter__ and __exit__")
        token = self.handles.token_for(target) or self.handles.put(target)
        if token in self._entered:
            raise ToolFailure("context_entered", "context is already entered")
        result = enter()
        with self._lock:
            self._entered.add(token)
        return result

    def _exit_context(self, token: str, target: Any, error_text: Optional[str]) -> bool:
        exit_method = getattr(target, "__exit__", None)
        if not callable(exit_method):
            raise ToolFailure("not_a_context", "target does not implement __enter__ and __exit__")
        if error_text is None:
            suppressed = exit_method(None, None, None)
        else:
            error = RuntimeError(error_text)
            suppressed = exit_method(RuntimeError, error, None)
        with self._lock:
            self._entered.discard(token)
        return bool(suppressed)

    def _tool_exit(self, arguments: Mapping[str, Any]) -> Any:
        self._expect_keys(arguments, required=("target",), optional=("error",))
        error_text = arguments.get("error")
        if error_text is not None and not isinstance(error_text, str):
            raise ToolFailure("invalid_error", "error must be a string")
        target = self._resolve_target(arguments["target"])
        token = self.handles.token_for(target) or self.handles.put(target)
        suppressed = self._exit_context(token, target, error_text)
        return {"exited": True, "suppressed": suppressed}

    def _tool_next(self, arguments: Mapping[str, Any]) -> Any:
        self._expect_keys(arguments, required=("target",))
        target = self._resolve_target(arguments["target"])
        next_method = getattr(target, "__next__", None)
        if not callable(next_method):
            raise ToolFailure("not_an_iterator", "target does not implement __next__")
        try:
            return next_method()
        except StopIteration:
            return {"done": True}

    def _release_related(self, token: str, *, keep_client: bool = False) -> None:
        value = self.handles.get(token)
        related = self.handles.related(token) if isinstance(value, (engine.Client, editor.Client)) else [(token, value)]
        for child_token, child in reversed(related):
            self._cleanup_value(child_token, child, failure=False)
            if child_token != token or not keep_client:
                self.handles.release(child_token)

    def _tool_release(self, arguments: Mapping[str, Any]) -> Any:
        self._expect_keys(arguments, required=("target",))
        token = _target_token(arguments["target"], self.handles)
        if token is None:
            raise ToolFailure("unknown_handle", "target does not contain a live handle")
        self._release_related(token)
        return {"released": True, "handle": token}

    def _tool_session(self, arguments: Mapping[str, Any]) -> Any:
        self._expect_keys(arguments, optional=("action",))
        action = arguments.get("action", "info")
        context = _request_context.get()
        session = context.session
        if action == "open":
            session = "s_" + uuid.uuid4().hex
            with self._lock:
                self._sessions[session] = True
        elif action == "close":
            with self._lock:
                self._sessions[session] = False
                for pending in self._requests.values():
                    if pending is not context and pending.session == session:
                        pending.token.cancel("MCP session closed")
                self.handles.retire(session)
            self._cleanup_retired()
        elif action != "info":
            raise ToolFailure("invalid_action", "action must be open, info or close")
        return {"mcp_session": session, "closed": not self._sessions[session],
                "handles": len(self.handles.snapshot(session)),
                "cleanup_errors": [entry for entry in self.cleanup_errors if entry.get("mcp_session") == session]}

    def _connection_identity(self, arguments: Mapping[str, Any]) -> dict:
        kwargs = dict(arguments)
        context = _request_context.get()
        if kwargs.get("client_id") is None:
            kwargs["client_id"] = "mcp-" + (context.session if context else "default")
        if kwargs.get("session_id") is None:
            kwargs["session_id"] = "mcp-" + uuid.uuid4().hex
        if any(not isinstance(kwargs[key], str) or not kwargs[key] for key in ("client_id", "session_id")):
            raise ToolFailure("invalid_identity", "client_id and session_id must be non-empty strings")
        pair = (kwargs["client_id"], kwargs["session_id"])
        with self._lock:
            used = pair in self._identity_claims or any(
                isinstance(value, engine.Client) and not value.closed and (value.client_id, value.session_id) == pair
                for _, value in self.handles.snapshot())
            if used:
                raise ToolFailure("identity_in_use", "use distinct native client/session identities for independent MCP clients")
            if context:
                self._identity_claims[pair] = context.request_id
        return kwargs

    def _extract_path(self, value: Any, path: Optional[str]) -> Any:
        if not path:
            return value
        current = value
        for segment in path.replace("/", ".").split("."):
            if not segment:
                continue
            if isinstance(current, Mapping):
                if segment not in current:
                    raise ToolFailure("missing_predicate_path", "predicate path is absent", {"path": path})
                current = current[segment]
            elif isinstance(current, (list, tuple)) and segment.isdigit():
                index = int(segment)
                if index >= len(current):
                    raise ToolFailure("missing_predicate_path", "predicate list index is absent", {"path": path})
                current = current[index]
            else:
                if not hasattr(current, segment):
                    raise ToolFailure("missing_predicate_path", "predicate path is absent", {"path": path})
                current = getattr(current, segment)
        return current

    @staticmethod
    def _predicate_matches(value: Any, predicate: Mapping[str, Any]) -> bool:
        operator = predicate.get("operator", "truthy")
        if operator == "truthy":
            return bool(value)
        if operator == "equals":
            return value == predicate.get("value")
        if operator == "not_equals":
            return value != predicate.get("value")
        if operator == "exists":
            return value is not None
        raise ToolFailure("invalid_predicate", "unknown predicate operator", {"operator": operator})

    def _wait_declarative(self, arguments: Mapping[str, Any]) -> Any:
        self._expect_keys(
            arguments,
            required=("operation",),
            optional=("target", "arguments", "path", "predicate", "timeout", "interval"),
        )
        spec = self._spec(arguments["operation"])
        if spec.kind == "property":
            nested_arguments: Mapping[str, Any] = {}
        else:
            nested_arguments = arguments.get("arguments", {})
            if not isinstance(nested_arguments, Mapping):
                raise ToolFailure("invalid_arguments", "nested wait arguments must be an object")
        if spec.destructive or not spec.read_only:
            raise ToolFailure(
                "unsafe_wait_operation",
                "declarative waits may poll only catalog operations marked read_only",
                {"operation": spec.id},
            )
        timeout = arguments.get("timeout", 10.0)
        interval = arguments.get("interval", 0.1)
        if not isinstance(timeout, (int, float)) or isinstance(timeout, bool) or not 0 <= timeout <= 300:
            raise ToolFailure("invalid_timeout", "timeout must be between 0 and 300 seconds")
        if not isinstance(interval, (int, float)) or isinstance(interval, bool) or not 0 <= interval <= 60:
            raise ToolFailure("invalid_interval", "interval must be between 0 and 60 seconds")
        path = arguments.get("path")
        if path is not None and not isinstance(path, str):
            raise ToolFailure("invalid_path", "path must be a string")
        predicate = arguments.get("predicate", {"operator": "truthy"})
        if not isinstance(predicate, Mapping):
            raise ToolFailure("invalid_predicate", "predicate must be an object")
        if set(predicate) - {"operator", "value"}:
            raise ToolFailure("invalid_predicate", "predicate contains unknown fields")
        deadline = time.monotonic() + float(timeout)
        attempts = 0
        last = None
        while True:
            check_cancelled()
            attempts += 1
            last = self.operation_handlers[spec.qualified_name](arguments.get("target"), nested_arguments)
            observed = self._extract_path(last, path)
            if self._predicate_matches(observed, predicate):
                return last
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise ToolFailure(
                    "wait_timeout",
                    "declarative predicate was not satisfied before timeout",
                    {"operation": spec.id, "attempts": attempts, "last_value": _plain_error_data(last)},
                    retryable=True,
                    error_type="WaitTimeoutError",
                )
            cancellable_sleep(min(float(interval), remaining))

    def _tool_wait(self, arguments: Mapping[str, Any]) -> Any:
        return self._wait_declarative(arguments)

    @staticmethod
    def _selector(value: Any) -> Dict[str, Any]:
        if not isinstance(value, Mapping):
            raise ToolFailure("invalid_selector", "selector must be an object")
        unknown = set(value) - _SELECTOR_KEYS
        if unknown:
            raise ToolFailure("invalid_selector", "selector contains unknown fields", {"fields": sorted(unknown)})
        try:
            mcp_schema.validate(dict(value), mcp_schema.SELECTOR)
        except ValueError as error:
            raise ToolFailure("invalid_selector", str(error)) from error
        return dict(value)

    def _focused_open_project(self, arguments: Mapping[str, Any]) -> Any:
        self._expect_keys(
            arguments,
            required=("project_path",),
            optional=("start_if_needed", "timeout", "launcher"),
        )
        raw_path = arguments["project_path"]
        if not isinstance(raw_path, str) or not raw_path:
            raise ToolFailure("invalid_project_path", "project_path must be a non-empty absolute path")
        project_path = Path(raw_path).expanduser()
        if not project_path.is_absolute():
            raise ToolFailure("invalid_project_path", "project_path must be absolute")
        project_path = project_path.resolve()
        if not (project_path / "game.project").is_file():
            raise ToolFailure("invalid_project_path", "project_path must contain game.project", {"path": str(project_path)})
        start = arguments.get("start_if_needed", False)
        if not isinstance(start, bool):
            raise ToolFailure("invalid_start_if_needed", "start_if_needed must be boolean")
        kwargs: Dict[str, Any] = {
            "root": str(project_path),
            "start_if_needed": start,
            "timeout": arguments.get("timeout", 30.0),
        }
        if "launcher" in arguments:
            kwargs["launcher"] = arguments["launcher"]
        return editor.open_project(**kwargs)

    def _editor_target(self, arguments: Mapping[str, Any]) -> editor.Client:
        project = self._resolve_target(arguments["project"])
        if not isinstance(project, editor.Client):
            raise ToolFailure("wrong_target_type", "project must be an editor.Client handle")
        return project

    def _focused_compile(self, arguments: Mapping[str, Any]) -> Any:
        self._expect_keys(arguments, required=("project",), optional=("timeout",))
        return self._editor_target(arguments).compile(timeout=arguments.get("timeout", 60.0))

    def _focused_bob(self, arguments: Mapping[str, Any]) -> Any:
        self._expect_keys(arguments, required=("project",), optional=("options", "commands", "timeout"))
        return self._editor_target(arguments).bob(
            options=arguments.get("options"), commands=arguments.get("commands", ()),
            timeout=arguments.get("timeout", 300.0))

    def _focused_build_and_run(self, arguments: Mapping[str, Any]) -> Any:
        self._expect_keys(
            arguments,
            required=("project",),
            optional=("clean", "timeout", "required_capabilities", "focus", "client_id", "session_id"),
        )
        project = self._resolve_target(arguments["project"])
        if not isinstance(project, editor.Client):
            raise ToolFailure("wrong_target_type", "project must be an editor.Client handle")
        capabilities = arguments.get("required_capabilities", ())
        if not isinstance(capabilities, (list, tuple)) or not all(isinstance(item, str) for item in capabilities):
            raise ToolFailure("invalid_capabilities", "required_capabilities must be an array of strings")
        clean = arguments.get("clean", False)
        if not isinstance(clean, bool):
            raise ToolFailure("invalid_clean", "clean must be boolean")
        method = project.clean_build_and_run if clean else project.build_and_run
        kwargs = self._connection_identity({key: arguments[key] for key in ("client_id", "session_id", "focus") if key in arguments})
        if clean and "focus" in kwargs:
            raise ToolFailure("unsupported_parameter", "clean builds use the editor's native focus behavior; omit focus or use a regular build")
        game = method(timeout=arguments.get("timeout", 60.0), required_capabilities=capabilities, **kwargs)
        return {"engine": game, "build_result": project.last_command_result, "session": game.session_info()}

    def _focused_connect_engine(self, arguments: Mapping[str, Any]) -> Any:
        self._expect_keys(
            arguments,
            optional=(
                "project", "port", "timeout", "profiler_url", "client_id", "session_id",
                "required_capabilities", "wait_ready",
            ),
        )
        has_project = "project" in arguments
        has_port = "port" in arguments
        if has_project == has_port:
            raise ToolFailure("invalid_connection", "provide exactly one of project or port")
        capabilities = arguments.get("required_capabilities", ())
        if not isinstance(capabilities, (list, tuple)) or not all(isinstance(item, str) for item in capabilities):
            raise ToolFailure("invalid_capabilities", "required_capabilities must be an array of strings")
        identity = self._connection_identity({key: arguments[key] for key in ("client_id", "session_id") if key in arguments})
        timeout = arguments.get("timeout", 20.0 if has_project else 10.0)
        if has_project:
            direct_only = sorted(
                set(arguments) & {"profiler_url"}
            )
            if direct_only:
                raise ToolFailure(
                    "invalid_arguments",
                    "profiler_url is only valid with port",
                    {"fields": direct_only},
                )
            project = self._resolve_target(arguments["project"])
            if not isinstance(project, editor.Client):
                raise ToolFailure("wrong_target_type", "project must be an editor.Client handle")
            game = project.connect_engine(timeout=timeout, required_capabilities=capabilities, **identity)
        else:
            port = arguments["port"]
            if not isinstance(port, int) or isinstance(port, bool) or not 1 <= port <= 65535:
                raise ToolFailure("invalid_port", "port must be an integer between 1 and 65535")
            kwargs = {
                "timeout": timeout,
                "profiler_url": arguments.get("profiler_url"),
                **identity,
                "required_capabilities": capabilities,
            }
            game = engine.connect(port, **kwargs)
        if arguments.get("wait_ready", True):
            game.wait_ready(timeout=timeout)
        return game

    def _engine_target(self, arguments: Mapping[str, Any]) -> engine.Client:
        game = self._resolve_target(arguments["engine"])
        if not isinstance(game, engine.Client):
            raise ToolFailure("wrong_target_type", "engine must be an engine.Client handle")
        return game

    def _focused_doctor(self, arguments: Mapping[str, Any]) -> Any:
        self._expect_keys(arguments, required=("project_path",), optional=("required_capabilities",))
        return editor.doctor(self._project_path(arguments["project_path"]),
                             required_capabilities=arguments.get("required_capabilities", ()))

    @staticmethod
    def _project_path(value: Any) -> Path:
        if not isinstance(value, str) or not value or not Path(value).expanduser().is_absolute():
            raise ToolFailure("invalid_project_path", "project_path must be a non-empty absolute path")
        return Path(value).expanduser().resolve()

    def _focused_update_python_wrapper(self, arguments: Mapping[str, Any]) -> Any:
        self._expect_keys(arguments, required=("project_path",))
        return editor.update_python_wrapper(self._project_path(arguments["project_path"]))

    def _focused_application_catalog(self, arguments: Mapping[str, Any]) -> Any:
        self._expect_keys(arguments, required=("engine",), optional=("kind", "name", "limit", "offset", "cursor"))
        kwargs = dict(arguments)
        kwargs.pop("engine")
        kwargs.setdefault("limit", 20)
        return self._engine_target(arguments).application_catalog(**kwargs)

    def _focused_session_info(self, arguments: Mapping[str, Any]) -> Any:
        self._expect_keys(arguments, required=("engine",))
        return self._engine_target(arguments).session_info()

    def _focused_close(self, arguments: Mapping[str, Any]) -> Any:
        self._expect_keys(arguments, required=("engine",))
        game = self._engine_target(arguments)
        token = self.handles.token_for(game)
        self._release_related(token, keep_client=True)
        return game.session_info()

    def _focused_health(self, arguments: Mapping[str, Any]) -> Any:
        self._expect_keys(arguments, required=("engine",))
        return self._engine_target(arguments).health()

    def _focused_find_elements(self, arguments: Mapping[str, Any]) -> Any:
        self._expect_keys(arguments, required=("engine",), optional=("selector",))
        return self._engine_target(arguments).elements_page(**self._selector(arguments.get("selector", {})))

    def _focused_get_element(self, arguments: Mapping[str, Any]) -> Any:
        self._expect_keys(arguments, required=("engine", "selector"), optional=("optional",))
        game = self._engine_target(arguments)
        selector = self._selector(arguments["selector"])
        if arguments.get("optional", False):
            return game.maybe_element(**selector)
        return game.element(**selector)

    def _focused_click(self, arguments: Mapping[str, Any]) -> Any:
        self._expect_keys(
            arguments,
            required=("engine", "target"),
            optional=(
                "y", "wait", "visualize", "device", "pointer_id", "expected_scene_sequence",
                "timeout", "cancel_on_interrupt", "flush_on_interrupt", "modifiers",
            ),
        )
        game = self._engine_target(arguments)
        kwargs = deserialize(dict(arguments), self.handles)
        kwargs.pop("engine")
        target = kwargs.pop("target")
        return game.click(target, **kwargs)

    def _focused_drag(self, arguments: Mapping[str, Any]) -> Any:
        self._expect_keys(
            arguments,
            required=("engine", "from_target", "to_target"),
            optional=(
                "duration", "wait", "visualize", "easing", "hold_before", "hold_after", "device",
                "pointer_id", "expected_scene_sequence", "timeout", "cancel_on_interrupt",
                "flush_on_interrupt", "modifiers",
            ),
        )
        game = self._engine_target(arguments)
        kwargs = deserialize(dict(arguments), self.handles)
        kwargs.pop("engine")
        from_target = kwargs.pop("from_target")
        to_target = kwargs.pop("to_target")
        return game.drag(from_target, to_target, **kwargs)

    def _focused_type_text(self, arguments: Mapping[str, Any]) -> Any:
        self._expect_keys(
            arguments,
            required=("engine", "text"),
            optional=("wait", "expected_scene_sequence", "timeout", "cancel_on_interrupt", "flush_on_interrupt"),
        )
        game = self._engine_target(arguments)
        kwargs = dict(arguments)
        kwargs.pop("engine")
        return game.type_text(**kwargs)

    def _focused_key(self, arguments: Mapping[str, Any]) -> Any:
        self._expect_keys(
            arguments,
            required=("engine", "key"),
            optional=("wait", "expected_scene_sequence", "timeout", "cancel_on_interrupt", "flush_on_interrupt", "hold", "modifiers"),
        )
        game = self._engine_target(arguments)
        kwargs = dict(arguments)
        kwargs.pop("engine")
        return game.key(**kwargs)

    def _focused_wait_for_element(self, arguments: Mapping[str, Any]) -> Any:
        self._expect_keys(
            arguments,
            required=("engine", "selector"),
            optional=("timeout", "interval", "after_scene_sequence"),
        )
        game = self._engine_target(arguments)
        selector = self._selector(arguments["selector"])
        return game.wait_for_element(
            timeout=arguments.get("timeout", 10.0),
            interval=arguments.get("interval", 0.1),
            after_scene_sequence=arguments.get("after_scene_sequence"),
            **selector,
        )

    def _focused_wait_for_state(self, arguments: Mapping[str, Any]) -> Any:
        self._expect_keys(
            arguments,
            required=("engine", "path", "expected"),
            optional=("timeout", "after_revision", "state_name"),
        )
        game = self._engine_target(arguments)
        kwargs = dict(arguments)
        kwargs.pop("engine")
        return game.wait_for_state(**kwargs)

    def _focused_wait_for_event(self, arguments: Mapping[str, Any]) -> Any:
        self._expect_keys(
            arguments,
            required=("engine", "name"),
            optional=("stream", "from_cursor", "where", "timeout", "event_type"),
        )
        game = self._engine_target(arguments)
        owns_stream = "stream" not in arguments
        stream = game.events(arguments.get("from_cursor", "now")) if owns_stream else self._resolve_target(arguments["stream"])
        if not isinstance(stream, engine.EventStream):
            raise ToolFailure("wrong_target_type", "stream must be an EventStream handle")
        try:
            return stream.wait(
                arguments["name"],
                where=arguments.get("where"),
                timeout=arguments.get("timeout", 10.0),
                event_type=arguments.get("event_type"),
            )
        finally:
            if owns_stream:
                stream.close()

    def _focused_command(self, arguments: Mapping[str, Any]) -> Any:
        self._expect_keys(arguments, required=("engine", "name"), optional=("data", "timeout"))
        return self._engine_target(arguments).command(
            arguments["name"],
            data=arguments.get("data"),
            timeout=arguments.get("timeout", 30.0),
        )

    def _focused_screenshot(self, arguments: Mapping[str, Any]) -> Any:
        self._expect_keys(
            arguments,
            required=("engine",),
            optional=("wait", "timeout", "after_frames", "resolution_multiplier"),
        )
        game = self._engine_target(arguments)
        kwargs = dict(arguments)
        kwargs.pop("engine")
        if kwargs.get("wait", True):
            kwargs.setdefault("resolution_multiplier", 0.5)
        return game.screenshot(**kwargs)

    @staticmethod
    def _image_block(data: bytes) -> dict:
        if len(data) > 8 * 1024 * 1024:
            raise ToolFailure("image_too_large", "PNG exceeds the 8 MiB MCP image limit; lower resolution_multiplier")
        if len(data) < 24 or not data.startswith(b"\x89PNG\r\n\x1a\n") or data[12:16] != b"IHDR":
            raise ToolFailure("invalid_image", "capture did not contain a PNG image")
        return {"type": "image", "mimeType": "image/png", "data": base64.b64encode(data).decode("ascii")}

    def _capture_images(self, value: Any) -> None:
        context = _request_context.get()
        if isinstance(value, engine.ScreenshotReceipt):
            if value.state != "complete":
                return
            try:
                with value.path.open("rb") as stream:
                    data = stream.read(8 * 1024 * 1024 + 1)
                if value.sha256 and hashlib.sha256(data).hexdigest() != value.sha256:
                    raise ValueError("capture file no longer matches the receipt hash")
                context.images.append(self._image_block(data))
            except (OSError, ValueError, ToolFailure) as error:
                raise ToolFailure("image_unavailable", str(error), {"receipt": serialize(value, self.handles)}) from error
        elif isinstance(value, Mapping):
            for item in value.values():
                self._capture_images(item)
        elif isinstance(value, (list, tuple)):
            for item in value:
                self._capture_images(item)

    def _preview_result(self, data: bytes, path: Any) -> dict:
        block = self._image_block(data)
        _request_context.get().images.append(block)
        return {"source": "editor_preview", "path": str(path), "state": "complete",
                "width": int.from_bytes(data[16:20], "big"), "height": int.from_bytes(data[20:24], "big"),
                "sha256": hashlib.sha256(data).hexdigest(), "bytes": len(data)}

    def _focused_preview(self, arguments: Mapping[str, Any]) -> Any:
        self._expect_keys(arguments, required=("project", "path"), optional=("width", "height", "resolution_multiplier", "timeout"))
        project = self._editor_target(arguments)
        kwargs = {key: value for key, value in arguments.items() if key != "project"}
        if "width" not in kwargs and "height" not in kwargs:
            kwargs.setdefault("resolution_multiplier", 0.5)
        return self._preview_result(project.preview.render(**kwargs), arguments["path"])

    def _focused_observe(self, arguments: Mapping[str, Any]) -> Any:
        self._expect_keys(arguments, required=("engine",), optional=("selector", "error_limit", "screenshot", "resolution_multiplier", "timeout"))
        game = self._engine_target(arguments)
        selector = self._selector(arguments.get("selector", {}))
        selector.setdefault("limit", 20)
        if selector["limit"] > 50:
            raise ToolFailure("invalid_limit", "observations allow at most 50 elements; use defold_find_elements for larger pages")
        limit = arguments.get("error_limit", 10)
        if isinstance(limit, bool) or not isinstance(limit, int) or not 0 <= limit <= 50:
            raise ToolFailure("invalid_limit", "error_limit must be between 0 and 50")
        started = time.time_ns()
        page = game.elements_page(**selector)
        errors = {"lines": [], "sampled_at_ns": time.time_ns(), "source": "runtime_log_tail", "engine_frame": None}
        if limit:
            try:
                lines = game.logs.tail(limit=200)
                errors["lines"] = [line[:2000] for line in lines if any(word in line.casefold() for word in ("error", "exception", "fatal"))][-limit:]
            except Exception as error:
                errors["unavailable"] = _exception_failure(error).as_dict()
        capture = None
        if arguments.get("screenshot", True):
            capture = game.screenshot(wait=True, timeout=arguments.get("timeout", 5.0),
                                      resolution_multiplier=arguments.get("resolution_multiplier", 0.5))
        return {
            "engine_instance_id": game.engine_instance_id,
            "elements": page.elements,
            "page": {key: getattr(page, key) for key in ("matched", "total", "next_cursor", "truncated", "scene_sequence", "engine_frame")},
            "recent_errors": errors, "screenshot": capture,
            "evidence": {"started_at_ns": started, "finished_at_ns": time.time_ns(), "snapshots": "independent",
                         "same_frame": bool(capture and capture.state == "complete" and page.engine_frame > 0
                                            and page.engine_frame == capture.frame and page.scene_sequence == capture.scene_sequence)},
        }

    def _focused_close_engine(self, arguments: Mapping[str, Any]) -> Any:
        self._expect_keys(arguments, required=("engine", "confirm"), optional=("timeout",))
        if arguments.get("confirm") is not True:
            raise ToolFailure("confirmation_required", "closing the engine requires confirm=true")
        game = self._engine_target(arguments)
        game.close_engine(timeout=arguments.get("timeout", 2.0))
        return {"closed": True}

    def _build_tool_descriptors(self) -> List[JsonObject]:
        wait_predicate = _object_schema(
            {"operator": {"type": "string", "enum": ["truthy", "equals", "not_equals", "exists"]}, "value": _ANY_JSON}
        )
        selector_schema = mcp_schema.SELECTOR
        modifiers_schema = {"anyOf": [_STRING, {"type": "array", "items": _STRING, "maxItems": 4}]}
        definitions = {
            "automation_bridge_session": (
                "Manage an MCP session", "Open an isolated logical session, inspect its handles and cleanup errors, or close it. Pass the returned mcp_session on subsequent tools. Closing releases local resources and input; engines stay running.",
                _object_schema({"action": {"type": "string", "enum": ["open", "info", "close"]}}), False, False, False,
            ),
            "defold_doctor": (
                "Diagnose Defold automation", "Inspect project setup, editor, bridge versions and capabilities without launching or writing files.",
                _object_schema({"project_path": _STRING, "required_capabilities": {"type": "array", "items": _STRING}}, ("project_path",)), True, False, True,
            ),
            "defold_update_python_wrapper": (
                "Update the project Python wrapper", "Copy the fetched extension's Python wrapper into the explicit target project.",
                _object_schema({"project_path": _STRING}, ("project_path",)), False, True, True,
            ),
            "defold_application_catalog": (
                "Discover application contracts", "Page through the connected game's registered command, state and event contracts.",
                _object_schema({"engine": _HANDLE_VALUE, "kind": {"type": "string", "enum": ["command", "state", "event"]}, "name": _STRING, "limit": {"type": "integer", "minimum": 0, "maximum": 100}, "offset": _INTEGER, "cursor": _STRING}, ("engine",)), True, False, True,
            ),
            "defold_session_info": (
                "Inspect engine session", "Read client and session identities, engine ownership and local closed state.",
                _object_schema({"engine": _HANDLE_VALUE}, ("engine",)), True, False, True,
            ),
            "defold_close": (
                "Close the local engine client", "Release client resources while leaving the engine process running.",
                _object_schema({"engine": _HANDLE_VALUE}, ("engine",)), False, False, True,
            ),
            "automation_bridge_describe": (
                "Describe one wrapper operation", "Load the full docstring, signature, JSON argument schema and restrictions for one operation id from automation_bridge_catalog.",
                _object_schema({"operation": _STRING}, ("operation",)), True, False, True,
            ),
            "defold_editor_capabilities": (
                "Discover connected editor commands", "Inspect commands and parameters advertised by this editor. This is separate from wrapper API discovery and game application contracts.",
                _object_schema({"project": _HANDLE_VALUE, "refresh": _BOOLEAN}, ("project",)), True, False, True,
            ),
            "automation_bridge_catalog": (
                "Catalog Automation Bridge APIs",
                "Search small summary pages of the wrapper API (20 by default). Follow next_cursor and use automation_bridge_describe for full schemas. Editor and application capabilities have separate discovery tools.",
                _object_schema({"query": _STRING, "cursor": {"type": "integer", "minimum": 0}, "limit": {"type": "integer", "minimum": 1, "maximum": 100}}), True, False, True,
            ),
            "automation_bridge_call": (
                "Call an Automation Bridge API",
                "Invoke one non-destructive allowlisted public function or method by catalog operation id; stateful results are returned as handles.",
                _object_schema({"operation": _STRING, "target": _HANDLE_VALUE, "arguments": _OPEN_OBJECT}, ("operation",)), False, False, False,
            ),
            "automation_bridge_destructive_call": (
                "Call a destructive Automation Bridge API",
                "Invoke an allowlisted destructive operation after explicit confirmation. This is limited to engine shutdown, raw native requests, and bridge updates.",
                _object_schema({"operation": _STRING, "target": _HANDLE_VALUE, "arguments": _OPEN_OBJECT, "confirm": _BOOLEAN}, ("operation", "confirm")), False, True, False,
            ),
            "automation_bridge_get": (
                "Read an Automation Bridge property",
                "Read one allowlisted public property from a retained object handle.",
                _object_schema({"operation": _STRING, "target": _HANDLE_VALUE}, ("operation", "target")), True, False, True,
            ),
            "automation_bridge_enter": (
                "Enter a retained context",
                "Enter a context-manager handle such as a pointer, trace, profiler, or input interruption scope.",
                _object_schema({"target": _HANDLE_VALUE}, ("target",)), False, False, False,
            ),
            "automation_bridge_exit": (
                "Exit a retained context",
                "Exit a context-manager handle, optionally providing an error string to trigger interruption cleanup.",
                _object_schema({"target": _HANDLE_VALUE, "error": _STRING}, ("target",)), False, False, True,
            ),
            "automation_bridge_next": (
                "Advance a retained iterator",
                "Return the next item from an allowlisted iterator or stream handle.",
                _object_schema({"target": _HANDLE_VALUE}, ("target",)), True, False, False,
            ),
            "automation_bridge_release": (
                "Release a retained handle",
                "Best-effort finalize and release a stateful Automation Bridge object handle.",
                _object_schema({"target": _HANDLE_VALUE}, ("target",)), False, False, True,
            ),
            "automation_bridge_wait": (
                "Wait on an Automation Bridge observation",
                "Poll an allowlisted read-only operation until a JSON predicate matches; this adapts Python wait_until without executable callbacks.",
                _object_schema({"operation": _STRING, "target": _HANDLE_VALUE, "arguments": _OPEN_OBJECT, "path": _STRING, "predicate": wait_predicate, "timeout": _NUMBER, "interval": _NUMBER}, ("operation",)), True, False, False,
            ),
            "defold_open_project": (
                "Open a Defold project",
                "Probe or launch the Defold Editor for an explicit absolute project path and return an editor handle.",
                _object_schema({"project_path": _STRING, "start_if_needed": _BOOLEAN, "timeout": _NUMBER, "launcher": _STRING}, ("project_path",)), False, False, False,
            ),
            "defold_compile": (
                "Compile a Defold project", "Compile resources and Lua without launching. Requires Defold 1.13.2; returns structured completion and diagnostics.",
                _object_schema({"project": _HANDLE_VALUE, "timeout": _NUMBER}, ("project",)), False, False, False,
            ),
            "defold_bob": (
                "Build or bundle with Bob", "Run Bob through the editor using its session authentication. Requires Defold 1.13.2. Timeouts do not imply that work stopped; inspect before retrying.",
                _object_schema({"project": _HANDLE_VALUE, "options": _OPEN_OBJECT, "commands": {"type": "array", "items": _STRING}, "timeout": _NUMBER}, ("project",)), False, False, False,
            ),
            "defold_build_and_run": (
                "Build and run a Defold project",
                "Compile and run directly; return an engine handle, session ownership and build_result. Uses run on 1.13.2 and build on 1.13.1. Omitted focus uses false when supported.",
                _object_schema({"project": _HANDLE_VALUE, "clean": _BOOLEAN, "focus": _BOOLEAN, "client_id": _STRING, "session_id": _STRING, "timeout": _NUMBER, "required_capabilities": {"type": "array", "items": _STRING}}, ("project",)), False, False, False,
            ),
            "defold_connect_engine": (
                "Connect to a Defold engine",
                "Connect through an editor handle or an explicit Automation Bridge port and optionally wait for readiness.",
                _object_schema({"project": _HANDLE_VALUE, "port": _INTEGER, "timeout": _NUMBER, "profiler_url": _STRING, "client_id": _STRING, "session_id": _STRING, "required_capabilities": {"type": "array", "items": _STRING}, "wait_ready": _BOOLEAN}), False, False, False,
            ),
            "defold_health": (
                "Read Defold engine health",
                "Read health, identity, lifecycle, version, and capability information from an engine handle.",
                _object_schema({"engine": _HANDLE_VALUE}, ("engine",)), True, False, True,
            ),
            "defold_find_elements": (
                "Find Defold elements",
                "Query a page of elements including matched count, next_cursor, scene_sequence and engine_frame. Each page is a live snapshot.",
                _object_schema({"engine": _HANDLE_VALUE, "selector": selector_schema}, ("engine",)), True, False, True,
            ),
            "defold_get_element": (
                "Get one Defold element",
                "Resolve exactly one runtime scene element, retaining its logical stale-identity fields.",
                _object_schema({"engine": _HANDLE_VALUE, "selector": selector_schema, "optional": _BOOLEAN}, ("engine", "selector")), True, False, True,
            ),
            "defold_click": (
                "Click a Defold target",
                "Click an Element snapshot, element id, or screen point through the engine input queue.",
                _object_schema({"engine": _HANDLE_VALUE, "modifiers": modifiers_schema, "target": mcp_schema.TARGET, "y": _NUMBER, "wait": mcp_schema.WAIT, "visualize": _BOOLEAN, "device": _STRING, "pointer_id": _INTEGER, "expected_scene_sequence": _INTEGER, "timeout": _NUMBER, "cancel_on_interrupt": _BOOLEAN, "flush_on_interrupt": _BOOLEAN}, ("engine", "target")), False, False, False,
            ),
            "defold_drag": (
                "Drag between Defold targets",
                "Drag between stale-safe Element snapshots, element ids, or screen points.",
                _object_schema({"engine": _HANDLE_VALUE, "modifiers": modifiers_schema, "from_target": mcp_schema.TARGET, "to_target": mcp_schema.TARGET, "duration": _NUMBER, "wait": mcp_schema.WAIT, "visualize": _BOOLEAN, "easing": _STRING, "hold_before": _NUMBER, "hold_after": _NUMBER, "device": _STRING, "pointer_id": _INTEGER, "expected_scene_sequence": _INTEGER, "timeout": _NUMBER, "cancel_on_interrupt": _BOOLEAN, "flush_on_interrupt": _BOOLEAN}, ("engine", "from_target", "to_target")), False, False, False,
            ),
            "defold_type_text": (
                "Type text in Defold",
                "Queue literal UTF-8 text input for a running Defold engine.",
                _object_schema({"engine": _HANDLE_VALUE, "text": _STRING, "wait": mcp_schema.WAIT, "expected_scene_sequence": _INTEGER, "timeout": _NUMBER, "cancel_on_interrupt": _BOOLEAN, "flush_on_interrupt": _BOOLEAN}, ("engine", "text")), False, False, False,
            ),
            "defold_key": (
                "Press a Defold key",
                "Queue a validated special key input for a running Defold engine.",
                _object_schema({"engine": _HANDLE_VALUE, "modifiers": modifiers_schema, "key": _STRING, "hold": {"type": "number", "minimum": 0, "maximum": 60}, "wait": mcp_schema.WAIT, "expected_scene_sequence": _INTEGER, "timeout": _NUMBER, "cancel_on_interrupt": _BOOLEAN, "flush_on_interrupt": _BOOLEAN}, ("engine", "key")), False, False, False,
            ),
            "defold_wait_for_element": (
                "Wait for a Defold element",
                "Wait for a semantic scene selector to resolve after an optional scene sequence.",
                _object_schema({"engine": _HANDLE_VALUE, "selector": selector_schema, "timeout": _NUMBER, "interval": _NUMBER, "after_scene_sequence": _INTEGER}, ("engine", "selector")), True, False, False,
            ),
            "defold_wait_for_state": (
                "Wait for Defold application state",
                "Wait for application-published state to equal an expected JSON value.",
                _object_schema({"engine": _HANDLE_VALUE, "path": _STRING, "expected": _ANY_JSON, "timeout": _NUMBER, "after_revision": _INTEGER, "state_name": _STRING}, ("engine", "path", "expected")), True, False, False,
            ),
            "defold_wait_for_event": (
                "Wait for a Defold application event",
                "Wait on an existing EventStream handle or a newly opened stream for a matching event.",
                _object_schema({"engine": _HANDLE_VALUE, "stream": _HANDLE_VALUE, "from_cursor": _ANY_JSON, "name": _STRING, "where": _OPEN_OBJECT, "timeout": _NUMBER, "event_type": _STRING}, ("engine", "name")), True, False, False,
            ),
            "defold_command": (
                "Run a Defold application command",
                "Send a bounded application-defined JSON command and wait for its terminal result.",
                _object_schema({"engine": _HANDLE_VALUE, "name": _STRING, "data": _ANY_JSON, "timeout": _NUMBER}, ("engine", "name")), False, False, False,
            ),
            "defold_preview": (
                "Render a Defold editor preview", "Render a project scene resource without running the game. Returns an MCP PNG image with dimensions and hash; defaults to half resolution.",
                _object_schema({"project": _HANDLE_VALUE, "path": _STRING, "width": {"type": "integer", "minimum": 1, "maximum": 4096}, "height": {"type": "integer", "minimum": 1, "maximum": 4096}, "resolution_multiplier": {"type": "number", "minimum": 0.01, "maximum": 1}, "timeout": _NUMBER}, ("project", "path")), False, False, False,
            ),
            "defold_observe": (
                "Observe the Defold runtime", "Return up to 50 selected elements, bounded recent errors, and an optional screenshot image. Keeps each snapshot's frame evidence; defaults to 20 elements and half resolution.",
                _object_schema({"engine": _HANDLE_VALUE, "selector": selector_schema, "error_limit": {"type": "integer", "minimum": 0, "maximum": 50}, "screenshot": _BOOLEAN, "resolution_multiplier": {"type": "number", "minimum": 0.01, "maximum": 1}, "timeout": _NUMBER}, ("engine",)), False, False, False,
            ),
            "defold_screenshot": (
                "Capture a Defold screenshot",
                "Capture a runtime PNG image alongside its atomic receipt with size, frame, and hash metadata. Defaults to half resolution when waiting. Pending receipts have no image.",
                _object_schema({"engine": _HANDLE_VALUE, "wait": _BOOLEAN, "timeout": _NUMBER, "after_frames": _INTEGER, "resolution_multiplier": _NUMBER}, ("engine",)), False, False, False,
            ),
            "defold_close_engine": (
                "Close a Defold engine",
                "Close the engine process. This is destructive and requires explicit confirm=true.",
                _object_schema({"engine": _HANDLE_VALUE, "confirm": _BOOLEAN, "timeout": _NUMBER}, ("engine", "confirm")), False, True, False,
            ),
        }
        descriptors = []
        for name, (title, description, schema, read_only, destructive, idempotent) in definitions.items():
            schema["properties"]["mcp_session"] = {"type": "string", "description": "Logical session returned by automation_bridge_session; omitted uses the transport default."}
            descriptors.append(_tool_descriptor(
                name, title, description, schema,
                read_only=read_only, destructive=destructive, idempotent=idempotent,
            ))
        build_result = _object_schema({"command": _STRING, "status": _INTEGER, "completed": _BOOLEAN,
                                       "success": {"anyOf": [_BOOLEAN, {"type": "null"}]}, "issues": {"type": "array", "items": _OPEN_OBJECT},
                                       "target_url": {"anyOf": [_STRING, {"type": "null"}]}, "raw": _OPEN_OBJECT},
                                      ("command", "status", "completed", "success", "issues", "target_url", "raw"))
        for descriptor in descriptors:
            name = descriptor["name"]
            props = descriptor["inputSchema"]["properties"]
            if name == "automation_bridge_wait":
                descriptor["inputSchema"] = _wait_schema()
                descriptor["inputSchema"]["properties"]["mcp_session"] = _STRING
            if name in {"defold_compile", "defold_bob"}:
                descriptor["outputSchema"]["properties"]["data"] = build_result
            elif name == "defold_build_and_run":
                descriptor["outputSchema"]["properties"]["data"] = _object_schema({"engine": _HANDLE_VALUE, "build_result": {"anyOf": [build_result, {"type": "null"}]}, "session": _OPEN_OBJECT}, ("engine", "build_result", "session"))
            elif name == "defold_find_elements":
                descriptor["outputSchema"]["properties"]["data"] = _object_schema({
                    "elements": {"type": "array", "items": mcp_schema.ELEMENT}, "matched": _INTEGER, "total": _INTEGER,
                    "count": _INTEGER, "offset": _INTEGER, "next_cursor": {"anyOf": [_STRING, {"type": "null"}]},
                    "truncated": _BOOLEAN, "scene_sequence": _INTEGER, "engine_frame": _INTEGER, "raw": _OPEN_OBJECT,
                }, ("elements", "matched", "next_cursor", "scene_sequence", "engine_frame"))
        return sorted(descriptors, key=lambda item: item["name"])

    @staticmethod
    def _build_resource_descriptors() -> List[JsonObject]:
        resources = (
            ("automation-bridge://api/catalog", "api-catalog", "Automation Bridge API catalog", "First summary page of the wrapper API; continue through automation_bridge_catalog and load schemas with automation_bridge_describe.", "application/json"),
            ("automation-bridge://api/preferences", "preference-catalog", "Defold preference catalog", "Complete generated Defold preference metadata without preference values.", "application/json"),
            ("automation-bridge://docs/best-practices", "best-practices", "Automation Bridge best practices", "Dependency-free Python examples and synchronization guidance.", "text/x-python"),
            ("automation-bridge://docs/plugin", "plugin-guide", "Automation Bridge plugin guide", "Installation, portability, API, and validation guide.", "text/markdown"),
            ("automation-bridge://docs/python", "python-guide", "Automation Bridge Python guide", "Complete Python wrapper usage and API guidance.", "text/markdown"),
        )
        return [
            {"uri": uri, "name": name, "title": title, "description": description, "mimeType": mime}
            for uri, name, title, description, mime in sorted(resources)
        ]

    def _wrapper_file(self, name: str) -> Optional[Path]:
        candidates = (
            self.project_root / "python" / name,
            self.project_root / "automation_bridge" / "automation-bridge-python" / name,
            Path(__file__).resolve().parents[1] / name,
        )
        for candidate in candidates:
            if candidate.is_file():
                return candidate
        return None

    def read_resource(self, uri: str) -> JsonObject:
        if not isinstance(uri, str):
            raise KeyError(uri)
        known = {item["uri"] for item in self._resources}
        if uri not in known:
            raise KeyError(uri)
        mime = next(item["mimeType"] for item in self._resources if item["uri"] == uri)
        if uri == "automation-bridge://api/catalog":
            text = json.dumps(self.catalog(), sort_keys=True, indent=2)
        elif uri == "automation-bridge://api/preferences":
            from automation_bridge._preferences_catalog import CATALOG

            text = json.dumps(CATALOG, sort_keys=True, indent=2)
        elif uri == "automation-bridge://docs/best-practices":
            path = self._wrapper_file("best_practices.py")
            text = path.read_text(encoding="utf-8") if path else "Best-practice source is unavailable in this installation."
        elif uri == "automation-bridge://docs/python":
            path = self._wrapper_file("README.md")
            text = path.read_text(encoding="utf-8") if path else "Python wrapper guide is unavailable in this installation."
        else:
            path = self.project_root / "README.md"
            if not ((self.project_root / "plugin.json").is_file() and path.is_file()):
                path = self.project_root / "plugins" / "automation-bridge" / "README.md"
            text = path.read_text(encoding="utf-8") if path.is_file() else "Plugin guide is unavailable in this source layout."
        return {"contents": [{"uri": uri, "mimeType": mime, "text": text}]}

    def _cleanup_value(self, token: str, value: Any, *, failure: bool) -> None:
        try:
            with self._lock:
                entered = token in self._entered
            if entered and not isinstance(value, engine.Client) and callable(getattr(value, "__exit__", None)):
                self._exit_context(token, value, "MCP session ended" if failure else None)
                return
            if isinstance(value, engine.PointerSession) and not value.closed:
                value.cancel(release=True)
            elif isinstance(value, engine.ProfilerRecording) and value.running:
                value.abort(RuntimeError("MCP session ended"))
            elif isinstance(value, engine.VideoRecordingSession):
                value.stop()
            elif isinstance(value, engine.TraceSession):
                value.close()
            elif isinstance(value, engine.ProfilerConnection) and value.connected:
                value.stop()
            elif isinstance(value, (editor.ConsoleStream, engine.EngineLogStream, engine.EventStream, engine.RuntimeLogs)):
                value.close()
            elif isinstance(value, engine.Client) and not value.closed:
                try:
                    value.input.flush(release=True)
                finally:
                    value.close()
        except Exception as error:
            context = _request_context.get()
            self._record_cleanup_error(context.request_id if context else None, error, self.handles._owners.get(token))
            if not failure:
                raise ToolFailure("cleanup_failed", "resource cleanup failed; inspect cleanup_errors before retrying", {"handle": token, "cause": _exception_failure(error).as_dict()}) from error

    def cleanup(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
            for context in self._requests.values():
                context.token.cancel("MCP runtime closed")
        self.handles.retire()
        self._cleanup_retired()


__all__ = [
    "BridgeRuntime",
    "HandleRegistry",
    "OPERATION_SPECS",
    "OperationSpec",
    "ToolFailure",
    "deserialize",
    "serialize",
]
