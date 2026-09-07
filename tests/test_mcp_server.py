#!/usr/bin/env python3
"""Dependency-free conformance tests for the Automation Bridge MCP adapter.

These tests intentionally never start Defold or connect to an editor/engine.  The
wire protocol is exercised with a fake runtime and the generic Python dispatcher
is exercised with objects allocated entirely in this process.
"""

from __future__ import annotations

import base64
import dataclasses
import importlib
import inspect
import io
import json
import os
import subprocess
import sys
import threading
import unittest
from dataclasses import dataclass
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
PYTHON_WRAPPER_ROOT = ROOT / "automation_bridge" / "automation-bridge-python"
sys.path.insert(0, str(PYTHON_WRAPPER_ROOT))

from automation_bridge import editor, engine  # noqa: E402
from automation_bridge.elements import Element  # noqa: E402
from automation_bridge.mcp_protocol import (  # noqa: E402
    LEGACY_PROTOCOL_VERSIONS,
    MODERN_PROTOCOL_VERSION,
    SUPPORTED_PROTOCOL_VERSIONS,
    McpProtocol,
    StdioServer,
)
from automation_bridge.mcp_runtime import (  # noqa: E402
    BridgeRuntime,
    HandleRegistry,
    ToolFailure,
    deserialize,
    serialize,
)
from automation_bridge.preferences import BUILTIN_PREFERENCES  # noqa: E402


EDITOR_OPERATIONS = {
    "automation_bridge.editor": {
        "open_project", "is_running", "installations", "latest_installation",
        "installation_registry_path", "doctor", "update_python_wrapper",
    },
    "automation_bridge.editor.Client": {
        "update_automation_bridge", "connect_engine", "build_and_run",
        "clean_build_and_run", "build_and_run_html5", "compile", "bob",
    },
    "automation_bridge.editor.Commands": {
        "fetch_libraries", "hot_reload", "rebundle", "reload_extensions",
        "reload_stylesheets", "catalog", "supports",
    },
    "automation_bridge.editor.Debugger": {
        "start", "stop", "break_", "continue_", "detach", "step_into",
        "step_out", "step_over",
    },
    "automation_bridge.editor.Console": {"read", "stream"},
    "automation_bridge.editor.ConsoleStream": {"readline", "close"},
    "automation_bridge.editor.Reference": {"search"},
    "automation_bridge.editor.Preview": {"render"},
    "automation_bridge.editor.Preferences": {"get", "set", "list", "describe"},
}

EDITOR_PROPERTIES = {
    "automation_bridge.editor.Client": {
        "root", "port", "base_url", "lifecycle_events", "commands", "debugger",
        "console", "reference", "preview", "preferences", "last_command_result",
    },
}

ENGINE_OPERATIONS = {
    "automation_bridge.engine": {"connect"},
    "automation_bridge.engine.Client": {
        "wait_ready", "request", "health", "lifecycle", "require", "supports",
        "trace_metadata", "screen", "scene", "elements", "element",
        "maybe_element", "element_by_id", "parent", "count", "click", "drag",
        "drag_path", "pointer", "type_text", "key", "events",
        "wait_for_input_acknowledgement", "states", "state", "wait_for_state",
        "start_command", "command_status", "cancel_command", "wait_for_command",
        "command", "mark", "screenshot", "convert_point", "engine_info",
        "engine_log_port", "log_stream", "read_logs", "trace", "resize",
        "set_portrait", "set_landscape", "reboot", "close_engine", "dump_scene",
        "format_elements", "wait_for_element", "wait_for_count", "wait_frames",
        "observe_element", "wait_for_disappearance",
        "elements_page", "application_catalog", "session_info", "close",
    },
    "automation_bridge.engine.InputController": {
        "configure", "pending", "status", "wait", "cancel", "flush",
    },
    "automation_bridge.engine.PointerSession": {"move", "hold", "up", "cancel"},
    "automation_bridge.engine.EngineLogStream": {"readline", "close"},
    "automation_bridge.engine.RuntimeLogs": {"start", "tail", "close"},
    "automation_bridge.engine.EventStream": {"poll", "wait", "close"},
    "automation_bridge.engine.ProfilerClient": {
        "resources", "connect", "capture", "start_recording",
    },
    "automation_bridge.engine.ProfilerConnection": {
        "from_url", "start", "stop", "get_frame", "aggregate_frame",
        "start_recording", "get_properties", "capture",
    },
    "automation_bridge.engine.ProfilerRecording": {
        "start", "stop", "abort", "snapshot",
    },
    "automation_bridge.engine.ProfilerCapture": {
        "aggregate", "scopes", "scope", "counters", "counter",
    },
    "automation_bridge.engine.ProfilerFrame": {
        "samples", "missing_name_hashes", "resolve_names", "aggregate",
    },
    "automation_bridge.engine.ProfilerSample": {"walk"},
    "automation_bridge.engine.ProfilerPropertyFrame": {
        "missing_name_hashes", "resolve_names", "entries", "find",
    },
    "automation_bridge.engine.VideoRecordingClient": {
        "capabilities", "status", "start",
    },
    "automation_bridge.engine.VideoRecordingSession": {"stop"},
    "automation_bridge.engine.MetalCaptureClient": {"status", "start", "wait", "stop"},
    "automation_bridge.engine.TraceSession": {
        "record", "record_event", "record_state", "record_input_acknowledgement",
        "record_profiler", "record_selector_error", "record_cleanup",
        "capture_screenshot", "close", "replay",
    },
    # These focused clients are returned by engine.Client properties, even though
    # their concrete types are intentionally not re-exported from engine.__all__.
    "automation_bridge.gestures.GestureGenerator": {"generate_drag"},
    "automation_bridge.visual.VisualClient": {
        "difference", "wait_for_stable_frame", "wait_for_region_change",
        "assert_matches",
    },
}

ENGINE_PROPERTIES = {
    "automation_bridge.engine.Client": {
        "port", "timeout", "base_url", "client_id", "session_id", "input", "logs",
        "engine_instance_id", "profiler", "gestures", "visual", "video_recording",
        "metal_capture", "profiler_url", "last_window_size", "owns_engine", "closed",
    },
    "automation_bridge.engine.PointerSession": {"receipt", "lease", "closed", "input_id"},
    "automation_bridge.engine.EngineLogStream": {"host", "port", "closed"},
    "automation_bridge.engine.EventStream": {"cursor"},
    "automation_bridge.engine.ProfilerClient": {"url"},
    "automation_bridge.engine.ProfilerConnection": {"connected", "sample_names"},
    "automation_bridge.engine.ProfilerRecording": {
        "running", "frame_count", "property_frame_count",
    },
}

REMOVED_API_NAMES = {
    "AutomationBridgeClient", "EditorClient", "Node", "node", "nodes",
    "maybe_node", "by_id", "wait_for_node", "wait_for_ack", "recording",
    "from_project", "from_editor", "wait_for_appearance", "assert_node",
    "record_video", "recording_capabilities", "recording_permission_diagnostics",
}


def _qualified_inventory(*groups):
    return {
        f"{owner}.{member}"
        for group in groups
        for owner, members in group.items()
        for member in members
    }


EXPECTED_CATALOG = _qualified_inventory(
    EDITOR_OPERATIONS,
    EDITOR_PROPERTIES,
    ENGINE_OPERATIONS,
    ENGINE_PROPERTIES,
)

EXPECTED_ADAPTATIONS = {
    "automation_bridge.engine.Client.require": {
        "availability": "adapted",
        "adapter": "capabilities_array_to_varargs",
    },
    "automation_bridge.engine.Client.reboot": {
        "availability": "adapted",
        "adapter": "args_array_to_varargs",
    },
    "automation_bridge.engine.Client.wait_ready": {
        "availability": "restricted",
        "unsupported_parameters": {"retry_exceptions"},
    },
    "automation_bridge.engine.Client.screenshot": {
        "availability": "restricted",
        "unsupported_parameters": {"retry_exceptions"},
    },
    "automation_bridge.engine.Client.wait_for_element": {
        "availability": "restricted",
        "unsupported_parameters": {"retry_exceptions"},
    },
    "automation_bridge.engine.Client.wait_for_count": {
        "availability": "restricted",
        "unsupported_parameters": {"retry_exceptions"},
    },
    "automation_bridge.engine.ProfilerClient.start_recording": {
        "availability": "restricted",
        "unsupported_parameters": {"on_finalize", "on_abort"},
    },
    "automation_bridge.engine.ProfilerConnection.start_recording": {
        "availability": "restricted",
        "unsupported_parameters": {"on_finalize", "on_abort"},
    },
    "automation_bridge.engine.ProfilerRecording.abort": {
        "availability": "adapted",
        "adapter": "cause_text_to_RuntimeError",
    },
    "automation_bridge.engine.TraceSession.record_selector_error": {
        "availability": "adapted",
        "adapter": "error_text_to_RuntimeError",
    },
    "automation_bridge.engine.ProfilerFrame.resolve_names": {
        "availability": "adapted",
        "adapter": "decimal_string_keys_to_int",
    },
    "automation_bridge.engine.ProfilerPropertyFrame.resolve_names": {
        "availability": "adapted",
        "adapter": "decimal_string_keys_to_int",
    },
    "automation_bridge.engine.ProfilerConnection.sample_names": {
        "availability": "adapted",
        "adapter": "int_keys_to_decimal_strings",
    },
    "automation_bridge.engine.VideoRecordingClient.start": {
        "availability": "adapted",
        "adapter": "size_array_to_tuple",
    },
}

NONLITERAL_API_CLASSIFICATIONS = {
    **{name: {
        "availability": "restricted", "adapter": "mcp_request_cancellation",
        "purpose": "Per-request tokens are controlled by MCP cancellation notifications",
    } for name in (
        "automation_bridge.engine.cancellation_scope",
        "automation_bridge.engine.Client.cancellation_scope",
        "automation_bridge.engine.CancellationToken.cancel",
        "automation_bridge.engine.CancellationToken.raise_if_cancelled",
        "automation_bridge.engine.CancellationToken.cancelled",
    )},

    "automation_bridge.engine.wait_until": {
        "availability": "adapted",
        "adapter": "declarative_operation_predicate",
        "purpose": "Python callables become a declarative operation and predicate",
    },
    "automation_bridge.engine.InputController.interruption_scope": {
        "availability": "adapted",
        "adapter": "context_handle_with_explicit_exit",
        "purpose": "The Python context is represented by an explicit context-manager handle",
    },
}

# Exported value/result types are transported through recursive serialization,
# not exposed as meaningless constructor/accessor RPCs. Keep this list explicit
# so a newly exported operational class cannot silently bypass catalog coverage.
SERIALIZED_RESULT_TYPES = {
    "automation_bridge.editor.BuildResult", "automation_bridge.editor.CommandInfo",
    "automation_bridge.editor.DiagnosticCheck", "automation_bridge.editor.DoctorReport",
    "automation_bridge.engine.ElementPage", "automation_bridge.engine.ElementSelector",
    "automation_bridge.engine.ApplicationCatalogPage", "automation_bridge.engine.ApplicationEntry",
    "automation_bridge.editor.AutomationBridgeUpdateResult",
    "automation_bridge.editor.BuildIssue",
    "automation_bridge.editor.ConsoleRegion",
    "automation_bridge.editor.ConsoleSnapshot",
    "automation_bridge.editor.FetchLibrariesResult",
    "automation_bridge.editor.Installation",
    "automation_bridge.editor.LibraryResult",
    "automation_bridge.editor.PreferenceKey",
    "automation_bridge.editor.SourcePosition",
    "automation_bridge.editor.SourceRange",
    "automation_bridge.engine.Bounds",
    "automation_bridge.engine.Element",
    "automation_bridge.engine.Event",
    "automation_bridge.engine.InputReceipt",
    "automation_bridge.engine.MetalCaptureStatus",
    "automation_bridge.engine.ObservationReceipt",
    "automation_bridge.engine.ProfilerCounterStats",
    "automation_bridge.engine.ProfilerProperty",
    "automation_bridge.engine.ProfilerPropertyEntry",
    "automation_bridge.engine.ProfilerSampleAggregate",
    "automation_bridge.engine.ProfilerScopeStats",
    "automation_bridge.engine.ProfilerTimingStats",
    "automation_bridge.engine.ProfilerValueStats",
    "automation_bridge.engine.ResourceProfileEntry",
    "automation_bridge.engine.ScreenshotReceipt",
    "automation_bridge.engine.StateSnapshot",
    "automation_bridge.engine.VideoRecordingCapabilities",
    "automation_bridge.engine.VideoRecordingMetadata",
    "automation_bridge.engine.VisualObservation",
}

SERIALIZATION_HELPERS = {
    "as_dict", "compact", "exists", "from_raw", "read_bytes", "stat", "to_dict"
}


def _strict_object_schema(properties=None, required=()):
    return {
        "type": "object",
        "properties": dict(properties or {}),
        "required": list(required),
        "additionalProperties": False,
    }


class FakeRuntime:
    """Small runtime double containing every capability used by McpProtocol."""

    def __init__(self):
        self.calls = []
        self.reads = []
        self.cancelled = []
        self.cleaned = False
        self._tools = [
            {
                "name": "echo",
                "title": "Echo",
                "description": "Return the supplied JSON value.",
                "inputSchema": _strict_object_schema(
                    {"value": {"type": ["array", "boolean", "null", "number", "object", "string"]}}
                ),
                "outputSchema": _strict_object_schema(
                    {
                        "ok": {"type": "boolean"},
                        "data": {},
                    },
                    ("ok", "data"),
                ),
                "annotations": {
                    "readOnlyHint": True,
                    "destructiveHint": False,
                    "idempotentHint": True,
                    "openWorldHint": False,
                },
            }
        ]
        self._resources = [
            {
                "uri": "automation-bridge://test/reference",
                "name": "reference",
                "title": "Reference",
                "description": "Fake protocol resource.",
                "mimeType": "application/json",
            }
        ]

    def tool_descriptors(self):
        return json.loads(json.dumps(self._tools))

    def resource_descriptors(self):
        return json.loads(json.dumps(self._resources))

    def call_tool(self, name, arguments):
        self.calls.append((name, arguments))
        if name != "echo":
            raise KeyError(name)
        return {"ok": True, "data": {"value": arguments.get("value")}}

    def read_resource(self, uri):
        self.reads.append(uri)
        if uri != self._resources[0]["uri"]:
            raise KeyError(uri)
        return {
            "contents": [
                {
                    "uri": uri,
                    "mimeType": "application/json",
                    "text": json.dumps({"reference": True}, sort_keys=True),
                }
            ]
        }

    def cancel(self, request_id):
        self.cancelled.append(request_id)

    def cleanup(self):
        self.cleaned = True


def _legacy_initialize(protocol, version, request_id=1):
    return protocol.handle(
        {
            "jsonrpc": "2.0",
            "id": request_id,
            "method": "initialize",
            "params": {
                "protocolVersion": version,
                "capabilities": {},
                "clientInfo": {"name": "unit-test", "version": "1.0"},
            },
        }
    )


def _modern_meta(version=MODERN_PROTOCOL_VERSION):
    return {
        "io.modelcontextprotocol/protocolVersion": version,
        "io.modelcontextprotocol/clientInfo": {"name": "unit-test", "version": "1.0"},
        "io.modelcontextprotocol/clientCapabilities": {},
    }


def _request(method, request_id=1, params=None):
    message = {"jsonrpc": "2.0", "id": request_id, "method": method}
    if params is not None:
        message["params"] = params
    return message


def _modern_request(method, request_id=1, params=None, version=MODERN_PROTOCOL_VERSION):
    request_params = dict(params or {})
    request_params["_meta"] = _modern_meta(version)
    return _request(method, request_id, request_params)


def _error_code(response):
    return response["error"]["code"]


def _catalog_items(result):
    if isinstance(result, list):
        return result
    for key in ("items", "operations", "entries"):
        if key in result:
            return result[key]
    raise AssertionError(f"catalog result has no entries: {result!r}")


def _tool_data(result):
    """Return the data member from the runtime's stable ok/data envelope."""
    if not result.get("ok"):
        raise AssertionError(f"tool failed: {result!r}")
    return result["data"]


def _find_handle(value):
    if isinstance(value, dict):
        for key in ("$handle", "handle", "handleId", "handle_id"):
            token = value.get(key)
            if isinstance(token, str):
                return token
        for nested in value.values():
            token = _find_handle(nested)
            if token is not None:
                return token
    elif isinstance(value, list):
        for nested in value:
            token = _find_handle(nested)
            if token is not None:
                return token
    return None


def _resolve_qualified_name(path):
    """Resolve the longest importable module prefix, then its attributes."""
    parts = path.split(".")
    for split_at in range(len(parts), 0, -1):
        try:
            value = importlib.import_module(".".join(parts[:split_at]))
        except ImportError:
            continue
        for part in parts[split_at:]:
            value = getattr(value, part)
        return value
    raise ImportError(path)


def _assert_json_schema(test_case, value, schema, path="$:"):
    """Validate the small dependency-free JSON Schema subset used by MCP tools."""
    if not schema:
        return
    expected_type = schema.get("type")
    if isinstance(expected_type, list):
        allowed_types = set(expected_type)
    elif expected_type is None:
        allowed_types = set()
    else:
        allowed_types = {expected_type}
    matches = {
        "object": isinstance(value, dict),
        "array": isinstance(value, list),
        "boolean": isinstance(value, bool),
        "integer": isinstance(value, int) and not isinstance(value, bool),
        "number": isinstance(value, (int, float)) and not isinstance(value, bool),
        "null": value is None,
        "string": isinstance(value, str),
    }
    if allowed_types:
        test_case.assertTrue(
            any(matches.get(kind, False) for kind in allowed_types),
            f"{path} expected {sorted(allowed_types)}, got {type(value).__name__}",
        )
    if "enum" in schema:
        test_case.assertIn(value, schema["enum"], path)
    if isinstance(value, dict) and "object" in allowed_types:
        properties = schema.get("properties", {})
        test_case.assertLessEqual(set(schema.get("required", ())), set(value), path)
        if schema.get("additionalProperties") is False:
            test_case.assertLessEqual(set(value), set(properties), path)
        for key, nested in value.items():
            if key in properties:
                _assert_json_schema(test_case, nested, properties[key], f"{path}.{key}")
    if isinstance(value, list) and "array" in allowed_types and "items" in schema:
        for index, nested in enumerate(value):
            _assert_json_schema(test_case, nested, schema["items"], f"{path}[{index}]")


class McpProtocolContractTest(unittest.TestCase):
    def setUp(self):
        self.runtime = FakeRuntime()
        self.protocol = McpProtocol(
            self.runtime,
            server_info={"name": "automation-bridge-test", "version": "1.2.3"},
            instructions="Use semantic selectors.",
        )

    def test_supported_versions_are_explicit_and_newest_first(self):
        self.assertEqual("2026-07-28", MODERN_PROTOCOL_VERSION)
        self.assertEqual(("2025-11-25", "2025-06-18"), tuple(LEGACY_PROTOCOL_VERSIONS))
        self.assertEqual(
            (MODERN_PROTOCOL_VERSION, *LEGACY_PROTOCOL_VERSIONS),
            tuple(SUPPORTED_PROTOCOL_VERSIONS),
        )

    def test_legacy_initialization_and_wire_shapes_for_both_supported_versions(self):
        for version in LEGACY_PROTOCOL_VERSIONS:
            with self.subTest(version=version):
                protocol = McpProtocol(
                    self.runtime,
                    server_info={"name": "automation-bridge-test", "version": "1.2.3"},
                    instructions="Use semantic selectors.",
                )
                initialized = _legacy_initialize(protocol, version)
                self.assertEqual("2.0", initialized["jsonrpc"])
                self.assertEqual(1, initialized["id"])
                result = initialized["result"]
                self.assertEqual(version, result["protocolVersion"])
                self.assertEqual("automation-bridge-test", result["serverInfo"]["name"])
                self.assertIn("tools", result["capabilities"])
                self.assertIn("resources", result["capabilities"])

                self.assertIsNone(
                    protocol.handle(
                        {"jsonrpc": "2.0", "method": "notifications/initialized"}
                    )
                )
                listed = protocol.handle(_request("tools/list", 2, {}))["result"]
                self.assertEqual(["echo"], [tool["name"] for tool in listed["tools"]])
                self.assertNotIn("resultType", listed)
                self.assertNotIn("ttlMs", listed)
                self.assertNotIn("cacheScope", listed)

                called = protocol.handle(
                    _request(
                        "tools/call",
                        3,
                        {"name": "echo", "arguments": {"value": [1, True, None]}},
                    )
                )["result"]
                self.assertNotIn("resultType", called)
                self.assertFalse(called.get("isError", False))
                self.assertEqual(
                    {"ok": True, "data": {"value": [1, True, None]}},
                    called["structuredContent"],
                )
                self.assertEqual(
                    called["structuredContent"],
                    json.loads(called["content"][0]["text"]),
                )

    def test_modern_discovery_contains_version_capabilities_server_meta_and_cache_hints(self):
        response = self.protocol.handle(_modern_request("server/discover", "discover"))
        self.assertEqual("discover", response["id"])
        result = response["result"]
        self.assertEqual("complete", result["resultType"])
        self.assertEqual(list(SUPPORTED_PROTOCOL_VERSIONS), result["supportedVersions"])
        self.assertEqual({"tools", "resources"}, set(result["capabilities"]))
        self.assertEqual("Use semantic selectors.", result["instructions"])
        self.assertEqual(
            {"name": "automation-bridge-test", "version": "1.2.3"},
            result["_meta"]["io.modelcontextprotocol/serverInfo"],
        )
        self.assertIsInstance(result["ttlMs"], int)
        self.assertGreaterEqual(result["ttlMs"], 0)
        self.assertIn(result["cacheScope"], {"public", "private"})

    def test_modern_list_read_and_call_results_have_required_result_shapes(self):
        tools = self.protocol.handle(_modern_request("tools/list", 1))["result"]
        self.assertEqual("complete", tools["resultType"])
        self.assertIsInstance(tools["ttlMs"], int)
        self.assertEqual("public", tools["cacheScope"])
        self.assertEqual(["echo"], [item["name"] for item in tools["tools"]])

        resources = self.protocol.handle(_modern_request("resources/list", 2))["result"]
        self.assertEqual("complete", resources["resultType"])
        self.assertIsInstance(resources["ttlMs"], int)
        self.assertIn(resources["cacheScope"], {"public", "private"})
        self.assertEqual(self.runtime._resources, resources["resources"])

        uri = self.runtime._resources[0]["uri"]
        read = self.protocol.handle(
            _modern_request("resources/read", 3, {"uri": uri})
        )["result"]
        self.assertEqual("complete", read["resultType"])
        self.assertIsInstance(read["ttlMs"], int)
        self.assertIn(read["cacheScope"], {"public", "private"})
        self.assertEqual(uri, read["contents"][0]["uri"])

        called = self.protocol.handle(
            _modern_request(
                "tools/call", 4, {"name": "echo", "arguments": {"value": {"x": 1}}}
            )
        )["result"]
        self.assertEqual("complete", called["resultType"])
        self.assertFalse(called.get("isError", False))
        self.assertEqual(
            {"ok": True, "data": {"value": {"x": 1}}},
            called["structuredContent"],
        )
        self.assertEqual(called["structuredContent"], json.loads(called["content"][0]["text"]))

    def test_missing_resource_uses_legacy_and_modern_revision_specific_codes(self):
        legacy = McpProtocol(self.runtime)
        _legacy_initialize(legacy, "2025-06-18")
        legacy_response = legacy.handle(
            _request("resources/read", 30, {"uri": "automation-bridge://missing"})
        )
        self.assertEqual(-32002, _error_code(legacy_response))

        modern_response = self.protocol.handle(
            _modern_request(
                "resources/read", 31, {"uri": "automation-bridge://missing"}
            )
        )
        self.assertEqual(-32602, _error_code(modern_response))

    def test_modern_requests_require_complete_request_metadata(self):
        malformed = (
            {},
            {"io.modelcontextprotocol/protocolVersion": MODERN_PROTOCOL_VERSION},
            {
                "io.modelcontextprotocol/protocolVersion": MODERN_PROTOCOL_VERSION,
                "io.modelcontextprotocol/clientCapabilities": [],
            },
        )
        for index, meta in enumerate(malformed):
            with self.subTest(meta=meta):
                response = self.protocol.handle(
                    _request("server/discover", index, {"_meta": meta})
                )
                self.assertEqual(-32602, _error_code(response))

    def test_unsupported_modern_version_has_standard_error_and_supported_versions(self):
        response = self.protocol.handle(
            _modern_request("tools/list", 8, version="1900-01-01")
        )
        self.assertEqual(-32022, _error_code(response))
        self.assertEqual(
            {"requested": "1900-01-01", "supported": list(SUPPORTED_PROTOCOL_VERSIONS)},
            response["error"]["data"],
        )

    def test_legacy_ping_allows_standard_progress_metadata(self):
        # Ping is permitted before initialization, and legacy request metadata
        # must not be mistaken for the modern protocol-version metadata shape.
        protocol = McpProtocol(self.runtime)
        response = protocol.handle(
            _request(
                "ping",
                "legacy-ping",
                {"_meta": {"progressToken": "progress-legacy"}},
            )
        )
        self.assertNotIn("error", response)
        self.assertEqual({}, response["result"])

    def test_json_rpc_validation_is_strict_and_preserves_only_valid_ids(self):
        cases = (
            (None, None),
            ([], None),
            ({}, None),
            ({"jsonrpc": "1.0", "id": 1, "method": "ping"}, 1),
            ({"jsonrpc": "2.0", "id": True, "method": "ping"}, None),
            ({"jsonrpc": "2.0", "id": {}, "method": "ping"}, None),
            ({"jsonrpc": "2.0", "id": 1, "method": 42}, 1),
            ({"jsonrpc": "2.0", "id": 1, "method": "ping", "params": []}, 1),
        )
        for message, expected_id in cases:
            with self.subTest(message=message):
                response = self.protocol.handle(message)
                self.assertEqual(-32600, _error_code(response))
                self.assertEqual(expected_id, response.get("id"))

    def test_method_and_parameter_errors_use_json_rpc_codes(self):
        unknown = self.protocol.handle(_modern_request("does/not/exist", 10))
        self.assertEqual(-32601, _error_code(unknown))

        bad_call = self.protocol.handle(
            _modern_request("tools/call", 11, {"name": "echo", "arguments": []})
        )
        self.assertEqual(-32602, _error_code(bad_call))

        bad_read = self.protocol.handle(_modern_request("resources/read", 12, {}))
        self.assertEqual(-32602, _error_code(bad_read))

    def test_structured_tool_failure_is_a_visible_tool_result_not_protocol_error(self):
        failure = ToolFailure(
            "stale_element",
            "The element snapshot is stale",
            {"logical_id": "instance:9", "expected_scene_sequence": 12},
        )
        with mock.patch.object(self.runtime, "call_tool", side_effect=failure):
            response = self.protocol.handle(
                _modern_request(
                    "tools/call", 13, {"name": "echo", "arguments": {"value": 1}}
                )
            )

        self.assertNotIn("error", response)
        result = response["result"]
        self.assertTrue(result["isError"])
        self.assertEqual("complete", result["resultType"])
        envelope = result["structuredContent"]
        self.assertFalse(envelope["ok"])
        error = envelope["error"]
        self.assertEqual("stale_element", error["code"])
        self.assertEqual("The element snapshot is stale", error["message"])
        self.assertIsInstance(error["type"], str)
        self.assertFalse(error["retryable"])
        details = error.get("data", error.get("details"))
        self.assertEqual(
            "instance:9", details["logical_id"]
        )
        self.assertEqual(envelope, json.loads(result["content"][0]["text"]))

    def test_notifications_and_cancellation_never_produce_responses(self):
        with mock.patch.object(
            self.protocol, "cancel", wraps=self.protocol.cancel
        ) as cancel:
            self.assertIsNone(
                self.protocol.handle(
                    {
                        "jsonrpc": "2.0",
                        "method": "notifications/cancelled",
                        "params": {
                            "_meta": _modern_meta(),
                            "requestId": "slow",
                            "reason": "unit test",
                        },
                    }
                )
            )
        cancel.assert_called_once_with("slow")

        calls_before_notification = list(self.runtime.calls)
        self.assertIsNone(
            self.protocol.handle(
                {
                    "jsonrpc": "2.0",
                    "method": "tools/call",
                    "params": {
                        "_meta": _modern_meta(),
                        "name": "echo",
                        "arguments": {"value": "notification"},
                    },
                }
            )
        )
        self.assertEqual(
            calls_before_notification,
            self.runtime.calls,
            "request-only tools/call notifications must not execute side effects",
        )

        self.assertIsNone(
            self.protocol.handle({"jsonrpc": "2.0", "method": "unknown/notification"})
        )

    def test_malformed_cancellation_notifications_are_ignored(self):
        malformed_params = (
            {"_meta": None, "requestId": "slow"},
            {"_meta": [], "requestId": "slow"},
            {"_meta": _modern_meta(), "requestId": "slow", "reason": None},
        )
        for params in malformed_params:
            with self.subTest(params=params), mock.patch.object(
                self.protocol, "cancel", wraps=self.protocol.cancel
            ) as cancel:
                self.assertIsNone(
                    self.protocol.handle(
                        {
                            "jsonrpc": "2.0",
                            "method": "notifications/cancelled",
                            "params": params,
                        }
                    )
                )
                cancel.assert_not_called()

    def test_tool_discovery_is_byte_stable_for_prompt_cache_safety(self):
        first = self.protocol.handle(_modern_request("tools/list", 20))["result"]
        second = self.protocol.handle(_modern_request("tools/list", 21))["result"]
        self.assertEqual(first, second)
        self.assertEqual(
            json.dumps(first, sort_keys=True, separators=(",", ":")),
            json.dumps(second, sort_keys=True, separators=(",", ":")),
        )

    def test_every_physical_tool_returns_its_advertised_envelope_without_live_io(self):
        runtime = BridgeRuntime(project_root=ROOT)
        protocol = McpProtocol(runtime)
        try:
            listed = protocol.handle(_modern_request("tools/list", "all-tools"))
            self.assertNotIn("error", listed)
            tools = listed["result"]["tools"]
            self.assertGreater(len(tools), 0)

            for index, descriptor in enumerate(tools):
                with self.subTest(tool=descriptor["name"]):
                    # Empty arguments either produce a side-effect-free result
                    # (catalog) or fail validation before editor/engine I/O.
                    response = protocol.handle(
                        _modern_request(
                            "tools/call",
                            f"tool-{index}",
                            {"name": descriptor["name"], "arguments": {}},
                        )
                    )
                    self.assertNotIn(
                        "error",
                        response,
                        "physical tool failures belong in CallToolResult, not JSON-RPC errors",
                    )
                    result = response["result"]
                    self.assertEqual("complete", result["resultType"])
                    envelope = result["structuredContent"]
                    self.assertEqual(envelope, json.loads(result["content"][0]["text"]))
                    self.assertEqual(not envelope["ok"], result.get("isError", False))
                    _assert_json_schema(
                        self,
                        envelope,
                        descriptor["outputSchema"],
                        f"$.{descriptor['name']}",
                    )
        finally:
            runtime.cleanup()


class RuntimeDescriptorAndCatalogTest(unittest.TestCase):
    def setUp(self):
        self.runtime = BridgeRuntime(project_root=ROOT)

    def tearDown(self):
        self.runtime.cleanup()

    def test_descriptors_are_deterministic_unique_and_match_handlers_exactly(self):
        first = self.runtime.tool_descriptors()
        second = self.runtime.tool_descriptors()
        self.assertEqual(first, second)
        self.assertEqual(
            json.dumps(first, sort_keys=True, separators=(",", ":")),
            json.dumps(second, sort_keys=True, separators=(",", ":")),
        )
        names = [descriptor["name"] for descriptor in first]
        self.assertEqual(sorted(names), names)
        self.assertEqual(len(names), len(set(names)))
        self.assertEqual(set(names), set(self.runtime.tool_handlers))
        self.assertTrue(all(callable(self.runtime.tool_handlers[name]) for name in names))

    def test_every_tool_has_closed_object_root_input_and_output_schemas(self):
        for descriptor in self.runtime.tool_descriptors():
            with self.subTest(tool=descriptor["name"]):
                for schema_name in ("inputSchema", "outputSchema"):
                    schema = descriptor[schema_name]
                    self.assertEqual("object", schema.get("type"), schema_name)
                    self.assertIs(False, schema.get("additionalProperties"), schema_name)
                    self.assertIsInstance(schema.get("properties"), dict, schema_name)
                    self.assertLessEqual(
                        set(schema.get("required", ())),
                        set(schema["properties"]),
                    )

    def test_dynamic_argument_bags_remain_open_inside_closed_tool_roots(self):
        descriptors = {
            descriptor["name"]: descriptor
            for descriptor in self.runtime.tool_descriptors()
        }
        dynamic_bags = (
            ("automation_bridge_call", "arguments"),
            ("automation_bridge_destructive_call", "arguments"),
            ("automation_bridge_wait", "arguments"),
            ("defold_wait_for_event", "where"),
        )
        for tool_name, property_name in dynamic_bags:
            with self.subTest(tool=tool_name, property=property_name):
                root = descriptors[tool_name]["inputSchema"]
                self.assertIs(False, root["additionalProperties"])
                nested = root["properties"][property_name]
                self.assertEqual("object", nested.get("type"))
                self.assertIsNot(
                    False,
                    nested.get("additionalProperties", True),
                    "application-defined operation/filter keys must remain schema-valid",
                )

        # Representative keys accepted by the Python runtime must therefore be
        # valid against the advertised nested schemas.
        self.assertIs(
            True,
            descriptors["automation_bridge_call"]["inputSchema"]["properties"]
            ["arguments"]["additionalProperties"],
        )
        self.assertIs(
            True,
            descriptors["defold_wait_for_event"]["inputSchema"]["properties"]
            ["where"]["additionalProperties"],
        )

    def test_every_tool_has_explicit_safety_annotations(self):
        descriptors = {
            descriptor["name"]: descriptor
            for descriptor in self.runtime.tool_descriptors()
        }
        expected_keys = {
            "readOnlyHint", "destructiveHint", "idempotentHint", "openWorldHint"
        }
        for name, descriptor in descriptors.items():
            with self.subTest(tool=name):
                annotations = descriptor["annotations"]
                self.assertEqual(expected_keys, set(annotations))
                self.assertTrue(all(isinstance(value, bool) for value in annotations.values()))
                self.assertFalse(annotations["openWorldHint"])

        self.assertTrue(descriptors["defold_health"]["annotations"]["readOnlyHint"])
        self.assertFalse(descriptors["defold_screenshot"]["annotations"]["readOnlyHint"])
        self.assertFalse(
            descriptors["automation_bridge_call"]["annotations"]["destructiveHint"]
        )
        self.assertTrue(
            descriptors["automation_bridge_destructive_call"]["annotations"]
            ["destructiveHint"]
        )
        self.assertTrue(
            descriptors["defold_close_engine"]["annotations"]["destructiveHint"]
        )

    def test_resources_are_deterministic_unique_and_all_readable(self):
        first = self.runtime.resource_descriptors()
        second = self.runtime.resource_descriptors()
        self.assertEqual(first, second)
        uris = [resource["uri"] for resource in first]
        self.assertEqual(sorted(uris), uris)
        self.assertEqual(len(uris), len(set(uris)))
        self.assertGreater(len(uris), 0)
        for descriptor in first:
            with self.subTest(uri=descriptor["uri"]):
                result = self.runtime.read_resource(descriptor["uri"])
                contents = result["contents"] if isinstance(result, dict) else result
                self.assertGreater(len(contents), 0)
                for content in contents:
                    self.assertEqual(descriptor["uri"], content["uri"])
                    self.assertTrue("text" in content or "blob" in content)

    def test_preference_resource_matches_source_metadata_without_values(self):
        def immutable_json(value):
            if isinstance(value, list):
                return tuple(immutable_json(item) for item in value)
            if isinstance(value, dict):
                return tuple(
                    sorted((str(key), immutable_json(item)) for key, item in value.items())
                )
            return value

        content = self.runtime.read_resource(
            "automation-bridge://api/preferences"
        )["contents"][0]
        catalog = json.loads(content["text"])
        expected_fields = {
            "name", "path", "type", "scope", "default", "has_default",
            "description", "group", "enum_values", "ui",
        }
        self.assertEqual(len(BUILTIN_PREFERENCES), len(catalog))
        self.assertEqual(23, sum(bool(item.group) for item in BUILTIN_PREFERENCES))
        self.assertEqual(72, sum(not item.group for item in BUILTIN_PREFERENCES))
        self.assertEqual(
            [item.path for item in BUILTIN_PREFERENCES],
            [item["path"] for item in catalog],
        )
        self.assertEqual(len(catalog), len({item["path"] for item in catalog}))
        for source, item in zip(BUILTIN_PREFERENCES, catalog):
            with self.subTest(preference=source.path):
                self.assertEqual(expected_fields, set(item))
                for field in expected_fields:
                    expected = getattr(source, field)
                    actual = immutable_json(item[field])
                    if field == "enum_values" and not expected:
                        actual = () if item[field] is None else actual
                    self.assertEqual(expected, actual)
                self.assertNotIn("value", item)

        sensitive = [item for item in catalog if item["type"] == "password"]
        self.assertGreater(len(sensitive), 0)
        self.assertTrue(all(item["path"] for item in sensitive))

    def test_catalog_pagination_is_complete_stable_and_non_overlapping(self):
        expected = _catalog_items(self.runtime.catalog(limit=10_000))
        actual = []
        cursor = 0
        while cursor is not None:
            page = self.runtime.catalog(cursor=cursor, limit=17)
            self.assertEqual(len(expected), page["total"])
            actual.extend(page["items"])
            cursor = page["next_cursor"]
        self.assertEqual(expected, actual)
        self.assertEqual(
            len(actual), len({item["qualified_name"] for item in actual})
        )

    def test_catalog_covers_every_documented_operational_member(self):
        result = self.runtime.catalog(limit=10_000)
        items = _catalog_items(result)
        by_qualified_name = {item["qualified_name"]: item for item in items}
        qualified = set(by_qualified_name)
        expected_qualified = EXPECTED_CATALOG | set(NONLITERAL_API_CLASSIFICATIONS)
        self.assertEqual(
            expected_qualified,
            qualified,
            "catalog must contain the complete public surface and no private/unsafe extras",
        )

        ordered = [item["qualified_name"] for item in items]
        self.assertEqual(sorted(ordered), ordered)
        self.assertEqual(len(ordered), len(set(ordered)))
        for item in items:
            self.assertIn(item["kind"], {"function", "method", "property"})
            self.assertTrue(item["id"])
            self.assertTrue(item["description"])

        for qualified_name, expected in EXPECTED_ADAPTATIONS.items():
            with self.subTest(adaptation=qualified_name):
                item = by_qualified_name[qualified_name]
                self.assertEqual(expected["availability"], item.get("availability"))
                if "adapter" in expected:
                    self.assertEqual(expected["adapter"], item.get("adapter"))
                if "unsupported_parameters" in expected:
                    self.assertEqual(
                        expected["unsupported_parameters"],
                        set(item.get("unsupported_parameters", ())),
                    )
                    self.assertTrue(item.get("reason"))

        for qualified_name, expected in NONLITERAL_API_CLASSIFICATIONS.items():
            with self.subTest(nonliteral_api=qualified_name):
                self.assertIn(
                    qualified_name,
                    by_qualified_name,
                    f"{qualified_name} must be classified: {expected['purpose']}",
                )
                item = by_qualified_name[qualified_name]
                self.assertEqual(expected["availability"], item.get("availability"))
                self.assertEqual(expected["adapter"], item.get("adapter"))
                self.assertTrue(item.get("reason"))

    def test_exported_python_surface_has_no_unclassified_operations(self):
        classified = EXPECTED_CATALOG | set(NONLITERAL_API_CLASSIFICATIONS)
        operational_owners = (
            set(EDITOR_OPERATIONS)
            | set(EDITOR_PROPERTIES)
            | set(ENGINE_OPERATIONS)
            | set(ENGINE_PROPERTIES)
        )
        value_only_types = SERIALIZED_RESULT_TYPES | {
            # This concrete context is returned by interruption_scope and is
            # manipulated through automation_bridge_enter/exit.
            "automation_bridge.engine.InputInterruptionScope",
            "automation_bridge.engine.CancellationToken",
        }

        for module in (editor, engine):
            for exported_name in module.__all__:
                exported = getattr(module, exported_name)
                qualified_owner = f"{module.__name__}.{exported_name}"
                with self.subTest(export=qualified_owner):
                    if inspect.isfunction(exported):
                        self.assertIn(qualified_owner, classified)
                        continue
                    if not inspect.isclass(exported):
                        self.fail(f"unclassified public export: {qualified_owner}")
                    if issubclass(exported, BaseException):
                        continue
                    self.assertIn(
                        qualified_owner,
                        operational_owners | value_only_types,
                        "new public classes must be classified as operational or serialized",
                    )

                for owner in reversed(exported.__mro__):
                    if not owner.__module__.startswith("automation_bridge"):
                        continue
                    for member_name, raw in vars(owner).items():
                        if member_name.startswith("_"):
                            continue
                        is_method = inspect.isfunction(raw) or isinstance(
                            raw, (classmethod, staticmethod)
                        )
                        is_property = isinstance(raw, property)
                        if not (is_method or is_property):
                            continue
                        operation = f"{qualified_owner}.{member_name}"
                        with self.subTest(member=operation):
                            if operation in classified:
                                continue
                            if is_method:
                                self.assertIn(
                                    member_name,
                                    SERIALIZATION_HELPERS,
                                    "public callable is neither dispatchable nor a serialization helper",
                                )
                            else:
                                self.assertTrue(
                                    dataclasses.is_dataclass(exported)
                                    or qualified_owner in SERIALIZED_RESULT_TYPES,
                                    "public property is neither dispatchable nor a serialized derived field",
                                )

    def test_every_catalog_entry_has_a_resolvable_owner_and_callable_dispatcher(self):
        items = _catalog_items(self.runtime.catalog(limit=10_000))
        executable = {
            item["qualified_name"]
            for item in items
            if item.get("availability", "native") != "unavailable"
        }
        self.assertEqual(executable, set(self.runtime.operation_handlers))

        for item in items:
            qualified_name = item["qualified_name"]
            with self.subTest(operation=qualified_name):
                owner = _resolve_qualified_name(item["owner"])
                self.assertIsNotNone(owner)
                self.assertEqual(qualified_name.rsplit(".", 1)[-1], item["name"])
                self.assertIn(item["kind"], {"function", "method", "property"})
                self.assertIsInstance(item.get("signature"), str)
                self.assertTrue(item["signature"])

                # Methods and functions must resolve directly from their public
                # Python owner. Some properties are documented instance fields,
                # so their dispatcher is the authoritative safe resolver.
                if item["kind"] in {"function", "method"}:
                    self.assertTrue(callable(getattr(owner, item["name"])))
                elif hasattr(owner, item["name"]):
                    self.assertTrue(
                        isinstance(inspect.getattr_static(owner, item["name"]), property)
                        or not callable(getattr(owner, item["name"]))
                    )

                if qualified_name in executable:
                    handler = self.runtime.operation_handlers[qualified_name]
                    self.assertTrue(callable(handler))
                    inspect.signature(handler)

    def test_removed_and_unsafe_legacy_names_do_not_reappear_in_catalog(self):
        items = _catalog_items(self.runtime.catalog(limit=10_000))
        paths = {item["qualified_name"] for item in items}
        leaked = sorted(
            path for path in paths if any(path.rsplit(".", 1)[-1] == name for name in REMOVED_API_NAMES)
        )
        self.assertFalse(leaked, "removed Python aliases leaked into MCP: " + ", ".join(leaked))

        # Arbitrary editor eval and UI/help/navigation commands are explicitly not
        # part of the supported public automation surface.
        catalog_text = json.dumps(items, sort_keys=True)
        self.assertNotIn('"/eval"', catalog_text)
        for command in (
            "asset-portal", "documentation", "donate-page", "report-issue",
            "support-forum", "toggle-pane-left", "toggle-pane-right",
        ):
            self.assertNotIn(command, catalog_text)


class RuntimeSerializationAndDispatchTest(unittest.TestCase):
    def test_element_round_trip_preserves_stale_identity_fields_losslessly(self):
        registry = HandleRegistry()
        raw = {
            "id": "go:/button#sprite",
            "snapshot_id": "go:/button#sprite@scene-17",
            "instance_id": "0x1042",
            "instance_generation": 7,
            "logical_id": "0x1042:7",
            "created_scene_sequence": 11,
            "scene_sequence": 17,
            "engine_frame": 923,
            "name": "button",
            "type": "sprite",
            "bounds": {
                "screen": {"x": 10, "y": 20, "w": 30, "h": 40},
                "center": {"x": 25, "y": 40},
            },
        }
        encoded = serialize(Element(dict(raw)), registry)
        json.dumps(encoded)  # The wire representation itself must be plain JSON.
        decoded = deserialize(json.loads(json.dumps(encoded)), registry)
        self.assertIsInstance(decoded, Element)
        self.assertEqual(raw, decoded.raw)
        self.assertEqual("0x1042:7", decoded.logical_id)
        self.assertEqual(11, decoded.created_scene_sequence)
        self.assertEqual(17, decoded.scene_sequence)

    def test_serialization_normalizes_paths_bytes_dataclasses_sets_and_mappings(self):
        @dataclass
        class Record:
            path: Path
            values: tuple[int, ...]

        registry = HandleRegistry()
        encoded = serialize(
            {
                "record": Record(Path("artifact.bin"), (3, 1)),
                "bytes": b"\x00\xff",
                "set": {3, 1, 2},
            },
            registry,
        )
        json.dumps(encoded)
        self.assertEqual([1, 2, 3], encoded["set"])
        self.assertEqual([3, 1], encoded["record"]["values"])
        self.assertTrue(encoded["record"]["path"].endswith("artifact.bin"))
        self.assertTrue(Path(encoded["record"]["path"]).is_absolute())

        byte_value = encoded["bytes"]
        if isinstance(byte_value, str):
            self.assertEqual(b"\x00\xff", base64.b64decode(byte_value))
        else:
            self.assertEqual(b"\x00\xff", base64.b64decode(byte_value["data"]))

    def test_profiler_previews_include_derived_values_without_expanding_captures(self):
        sample = engine.ProfilerSample(
            name_hash=42,
            name=None,
            unique_id=7,
            colour=(1, 2, 3),
            depth=0,
            start_us=100,
            duration_us=2_500,
            self_us=1_000,
            gpu_to_cpu_us=0,
            call_count=1,
            max_recursion_depth=1,
        )
        frame = engine.ProfilerFrame("Main", sample)

        sample_wire = serialize(sample, HandleRegistry())
        self.assertEqual(
            {
                "label": "0x0000002a",
                "end_us": 2_600,
                "duration_ms": 2.5,
                "self_ms": 1.0,
            },
            {
                key: sample_wire["value"][key]
                for key in ("label", "end_us", "duration_ms", "self_ms")
            },
        )

        frame_wire = serialize(frame, HandleRegistry())
        self.assertEqual(
            {
                "start_us": 100,
                "end_us": 2_600,
                "duration_us": 2_500,
                "duration_ms": 2.5,
            },
            {
                key: frame_wire["value"][key]
                for key in ("start_us", "end_us", "duration_us", "duration_ms")
            },
        )

        frames = tuple(
            engine.ProfilerFrame(
                "Main",
                engine.ProfilerSample(
                    name_hash=index,
                    name=f"sample-{index}",
                    unique_id=index,
                    colour=(0, 0, 0),
                    depth=0,
                    start_us=index,
                    duration_us=10,
                    self_us=10,
                    gpu_to_cpu_us=0,
                    call_count=1,
                    max_recursion_depth=1,
                ),
            )
            for index in range(300)
        )
        registry = HandleRegistry()
        capture_wire = serialize(engine.ProfilerCapture(frames), registry)
        self.assertLess(
            len(json.dumps(capture_wire, sort_keys=True)),
            16_384,
            "capture handles need a bounded summary, not hundreds of frame previews",
        )
        self.assertEqual(
            1,
            len(registry.snapshot()),
            "serializing a capture must not retain every nested frame/sample handle",
        )

    def test_handle_registry_round_trip_release_and_cleanup(self):
        registry = HandleRegistry()
        target = object()
        handle = registry.put(target)
        self.assertIsInstance(handle, str)
        self.assertIs(target, registry.get(handle))
        registry.release(handle)
        with self.assertRaises((KeyError, ToolFailure)):
            registry.get(handle)

        second = registry.put(object())
        registry.cleanup()
        with self.assertRaises((KeyError, ToolFailure)):
            registry.get(second)

    def test_generic_dispatch_invokes_module_function_method_and_property_with_fakes(self):
        class FakeEngine(engine.Client):
            def __init__(self):
                super().__init__(51337, timeout=2.0)
                self.health_calls = 0

            def health(self):
                self.health_calls += 1
                return {"api_version": "fake"}

        fake_engine = FakeEngine()
        with mock.patch.object(editor, "open_project", autospec=True) as open_project:
            open_project.return_value = fake_engine
            runtime = BridgeRuntime(project_root=ROOT)
            try:
                items = _catalog_items(runtime.catalog(limit=10_000))
                ids = {item["qualified_name"]: item["id"] for item in items}
                opened = runtime.call_tool(
                    "automation_bridge_call",
                    {
                        "operation": ids["automation_bridge.editor.open_project"],
                        "arguments": {
                            "root": str(ROOT),
                            "start_if_needed": False,
                            "timeout": 0.25,
                        },
                    },
                )
                open_project.assert_called_once_with(
                    root=str(ROOT), start_if_needed=False, timeout=0.25
                )
                target = _tool_data(opened)
                self.assertIsNotNone(_find_handle(target))

                health = runtime.call_tool(
                    "automation_bridge_call",
                    {
                        "operation": ids["automation_bridge.engine.Client.health"],
                        "target": target,
                        "arguments": {},
                    },
                )
                self.assertEqual({"api_version": "fake"}, _tool_data(health))
                self.assertEqual(1, fake_engine.health_calls)

                port = runtime.call_tool(
                    "automation_bridge_get",
                    {
                        "operation": ids["automation_bridge.engine.Client.port"],
                        "target": target,
                    },
                )
                self.assertEqual(51337, _tool_data(port))
            finally:
                runtime.cleanup()

    def test_destructive_catalog_operations_use_the_separate_confirmed_tool(self):
        class FakeEngine(engine.Client):
            def __init__(self):
                super().__init__(51337)
                self.closed_with = None

            def close_engine(self, timeout=2.0):
                self.closed_with = timeout

        runtime = BridgeRuntime(project_root=ROOT)
        try:
            game = FakeEngine()
            target = serialize(game, runtime.handles)
            operation = "automation_bridge.engine.Client.close_engine"

            ordinary = runtime.call_tool(
                "automation_bridge_call",
                {"operation": operation, "target": target, "arguments": {}},
            )
            self.assertFalse(ordinary["ok"])
            self.assertEqual(
                "destructive_operation_requires_tool", ordinary["error"]["code"]
            )
            self.assertIsNone(game.closed_with)

            unconfirmed = runtime.call_tool(
                "automation_bridge_destructive_call",
                {
                    "operation": operation,
                    "target": target,
                    "arguments": {"timeout": 0.5},
                    "confirm": False,
                },
            )
            self.assertFalse(unconfirmed["ok"])
            self.assertEqual("confirmation_required", unconfirmed["error"]["code"])
            self.assertIsNone(game.closed_with)

            confirmed = runtime.call_tool(
                "automation_bridge_destructive_call",
                {
                    "operation": operation,
                    "target": target,
                    "arguments": {"timeout": 0.5},
                    "confirm": True,
                },
            )
            self.assertTrue(confirmed["ok"], confirmed)
            self.assertEqual(0.5, game.closed_with)
        finally:
            runtime.cleanup()

    def test_project_connect_rejects_direct_port_only_identity_parameters(self):
        class FakeProject(editor.Client):
            def __init__(self):
                super().__init__(ROOT, port=51336)
                self.connect_calls = 0

            def connect_engine(self, **kwargs):
                self.connect_calls += 1
                raise AssertionError("invalid parameters must be rejected before connection")

        runtime = BridgeRuntime(project_root=ROOT)
        try:
            project = FakeProject()
            result = runtime.call_tool(
                "defold_connect_engine",
                {
                    "project": serialize(project, runtime.handles),
                    "profiler_url": "ws://127.0.0.1:17816/rmt",
                    "client_id": "client-test",
                    "session_id": "session-test",
                },
            )
            self.assertFalse(result["ok"], result)
            self.assertEqual("invalid_arguments", result["error"]["code"])
            self.assertEqual(
                {"profiler_url", "client_id", "session_id"},
                set(result["error"].get("data", {}).get("fields", ())),
            )
            self.assertEqual(0, project.connect_calls)
        finally:
            runtime.cleanup()

    def test_stale_element_wire_value_reaches_engine_method_as_an_element(self):
        class FakeEngine(engine.Client):
            def __init__(self):
                super().__init__(51337)
                self.clicked = None

            def click(self, target, **kwargs):
                self.clicked = (target, kwargs)
                return {"input_id": 1, "state": "released"}

        fake_engine = FakeEngine()
        runtime = BridgeRuntime(project_root=ROOT)
        try:
            items = _catalog_items(runtime.catalog(limit=10_000))
            ids = {item["qualified_name"]: item["id"] for item in items}
            registry = getattr(runtime, "handles", getattr(runtime, "handle_registry", None))
            self.assertIsNotNone(registry, "BridgeRuntime must expose its explicit handle registry")
            engine_wire = serialize(fake_engine, registry)
            raw = {
                "id": "go:/button#sprite",
                "logical_id": "instance:9",
                "instance_id": "instance",
                "instance_generation": 9,
                "created_scene_sequence": 4,
                "scene_sequence": 12,
            }
            element_wire = serialize(Element(dict(raw)), registry)

            result = runtime.call_tool(
                "automation_bridge_call",
                {
                    "operation": ids["automation_bridge.engine.Client.click"],
                    "target": engine_wire,
                    "arguments": {"target": element_wire, "wait": "released"},
                },
            )
            self.assertEqual({"input_id": 1, "state": "released"}, _tool_data(result))
            actual, kwargs = fake_engine.clicked
            self.assertIsInstance(actual, Element)
            self.assertEqual(raw, actual.raw)
            self.assertEqual("released", kwargs["wait"])
        finally:
            runtime.cleanup()

    def test_application_json_reserved_looking_keys_round_trip_losslessly(self):
        class FakeEngine(engine.Client):
            def __init__(self):
                super().__init__(51337)
                self.command_data = []

            def command(self, name, data=None, timeout=30.0):
                self.command_data.append((name, data, timeout))
                return {"received": data}

        payloads = (
            {"$type": "enemy", "nested": {"$type": "boss"}},
            {"$handle": "business-id", "nested": {"$handle": "customer-id"}},
        )
        runtime = BridgeRuntime(project_root=ROOT)
        try:
            game = FakeEngine()
            game_wire = serialize(game, runtime.handles)
            for payload in payloads:
                with self.subTest(payload=payload):
                    result = runtime.call_tool(
                        "automation_bridge_call",
                        {
                            "operation": "automation_bridge.engine.Client.command",
                            "target": game_wire,
                            "arguments": {
                                "name": "wire-round-trip",
                                "data": payload,
                                "timeout": 0.25,
                            },
                        },
                    )
                    self.assertTrue(result.get("ok"), result)
                    self.assertEqual(payload, _tool_data(result)["received"])
                    self.assertEqual(payload, game.command_data[-1][1])
        finally:
            runtime.cleanup()

    def test_video_recording_size_json_array_is_adapted_to_wrapper_tuple(self):
        class FakeBridge:
            def __init__(self):
                self.requests = []

            def request(self, method, path, *, json_body=None, **kwargs):
                json = json_body
                if path == "/recording/stop":
                    return {"active": False, "finalized": True}
                self.requests.append((method, path, json, kwargs))
                return {
                    "path": json["path"],
                    "active": True,
                    "width": json["width"],
                    "height": json["height"],
                    "fps": json["fps"],
                }

            def _trace_record(self, *args, **kwargs):
                return None

        bridge = FakeBridge()
        client = engine.VideoRecordingClient(bridge)
        runtime = BridgeRuntime(project_root=ROOT)
        try:
            result = runtime.call_tool(
                "automation_bridge_call",
                {
                    "operation": "automation_bridge.engine.VideoRecordingClient.start",
                    "target": serialize(client, runtime.handles),
                    "arguments": {
                        "path": str(ROOT / "recording.mp4"),
                        "size": [320, 180],
                        "fps": 24,
                        "audio": False,
                    },
                },
            )
            self.assertTrue(result.get("ok"), result)
            method, path, request_data, kwargs = bridge.requests[-1]
            self.assertEqual(("POST", "/recording/start"), (method, path))
            self.assertEqual((320, 180, 24), (
                request_data["width"], request_data["height"], request_data["fps"]
            ))
            self.assertFalse(request_data["audio"])
            self.assertEqual({}, kwargs)
        finally:
            runtime.cleanup()

    def test_context_iterator_and_release_tools_manage_explicit_handles(self):
        class FakeContext:
            def __init__(self):
                self.entered = False
                self.exited = False

            def __enter__(self):
                self.entered = True
                return {"scope": "active"}

            def __exit__(self, error_type, error, traceback):
                self.exited = True
                self.exit_arguments = (error_type, error, traceback)
                return False

        runtime = BridgeRuntime(project_root=ROOT)
        try:
            registry = getattr(runtime, "handles", None)
            self.assertIsNotNone(registry, "BridgeRuntime must expose handles")

            context = FakeContext()
            context_wire = serialize(context, registry)
            entered = runtime.call_tool(
                "automation_bridge_enter", {"target": context_wire}
            )
            self.assertEqual({"scope": "active"}, _tool_data(entered))
            self.assertTrue(context.entered)
            self.assertFalse(context.exited)

            exited = runtime.call_tool(
                "automation_bridge_exit", {"target": context_wire}
            )
            self.assertTrue(_tool_data(exited)["exited"])
            self.assertTrue(context.exited)
            self.assertEqual((None, None, None), context.exit_arguments)

            iterator = iter(({"value": 1}, {"value": 2}))
            iterator_wire = serialize(iterator, registry)
            first = runtime.call_tool(
                "automation_bridge_next", {"target": iterator_wire}
            )
            second = runtime.call_tool(
                "automation_bridge_next", {"target": iterator_wire}
            )
            self.assertEqual({"value": 1}, _tool_data(first))
            self.assertEqual({"value": 2}, _tool_data(second))

            token = _find_handle(iterator_wire)
            released = runtime.call_tool(
                "automation_bridge_release", {"target": iterator_wire}
            )
            self.assertTrue(_tool_data(released)["released"])
            with self.assertRaises((KeyError, ToolFailure)):
                registry.get(token)
        finally:
            runtime.cleanup()

    def test_interruption_scope_adapter_returns_a_context_handle_spanning_calls(self):
        class FakeScope:
            def __init__(self):
                self.entered = False
                self.exited = False

            def __enter__(self):
                self.entered = True
                return self

            def __exit__(self, error_type, error, traceback):
                self.exited = True
                return False

        class FakeController(engine.InputController):
            def __init__(self):
                self.scope = FakeScope()
                self.scope_arguments = None

            def interruption_scope(self, flush=True, release=True):
                self.scope_arguments = {"flush": flush, "release": release}
                return self.scope

        runtime = BridgeRuntime(project_root=ROOT)
        try:
            items = _catalog_items(runtime.catalog(limit=10_000))
            ids = {item["qualified_name"]: item["id"] for item in items}
            controller = FakeController()
            controller_wire = serialize(controller, runtime.handles)
            created = runtime.call_tool(
                "automation_bridge_call",
                {
                    "operation": ids[
                        "automation_bridge.engine.InputController.interruption_scope"
                    ],
                    "target": controller_wire,
                    "arguments": {"flush": True, "release": False},
                },
            )
            scope_wire = _tool_data(created)
            self.assertIsNotNone(_find_handle(scope_wire))
            self.assertEqual(
                {"flush": True, "release": False}, controller.scope_arguments
            )

            runtime.call_tool("automation_bridge_enter", {"target": scope_wire})
            self.assertTrue(controller.scope.entered)
            self.assertFalse(controller.scope.exited)
            runtime.call_tool("automation_bridge_exit", {"target": scope_wire})
            self.assertTrue(controller.scope.exited)
        finally:
            runtime.cleanup()

    def test_declarative_wait_adapter_polls_an_allowlisted_operation(self):
        class FakeEngine(engine.Client):
            def __init__(self):
                super().__init__(51337)
                self.values = iter(({"ready": False}, {"ready": False}, {"ready": True}))
                self.health_calls = 0

            def health(self):
                self.health_calls += 1
                return next(self.values)

        runtime = BridgeRuntime(project_root=ROOT)
        try:
            game = FakeEngine()
            game_wire = serialize(game, runtime.handles)
            result = runtime.call_tool(
                "automation_bridge_wait",
                {
                    "operation": "automation_bridge.engine.Client.health",
                    "target": game_wire,
                    "arguments": {},
                    "path": "ready",
                    "predicate": {"operator": "equals", "value": True},
                    "timeout": 0.5,
                    "interval": 0.0,
                },
            )
            self.assertEqual({"ready": True}, _tool_data(result))
            self.assertEqual(3, game.health_calls)
        finally:
            runtime.cleanup()

    def test_varargs_integer_map_and_exception_adapters_reach_python_shapes(self):
        class FakeEngine(engine.Client):
            def __init__(self):
                super().__init__(51337)
                self.required = None
                self.rebooted = None

            def require(self, *capabilities):
                self.required = capabilities
                return self

            def reboot(self, *args, wait=True, timeout=None):
                self.rebooted = (args, wait, timeout)

        runtime = BridgeRuntime(project_root=ROOT)
        try:
            game = FakeEngine()
            game_wire = serialize(game, runtime.handles)
            runtime.call_tool(
                "automation_bridge_call",
                {
                    "operation": "automation_bridge.engine.Client.require",
                    "target": game_wire,
                    "arguments": {"capabilities": ["scene", "input.drag"]},
                },
            )
            self.assertEqual(("scene", "input.drag"), game.required)

            runtime.call_tool(
                "automation_bridge_call",
                {
                    "operation": "automation_bridge.engine.Client.reboot",
                    "target": game_wire,
                    "arguments": {
                        "args": ["--config=bootstrap.main_collection=/main.collectionc"],
                        "wait": False,
                        "timeout": 1.25,
                    },
                },
            )
            self.assertEqual(
                (
                    ("--config=bootstrap.main_collection=/main.collectionc",),
                    False,
                    1.25,
                ),
                game.rebooted,
            )

            root_sample = engine.ProfilerSample(
                name_hash=101,
                name=None,
                unique_id=1,
                colour=(0, 0, 0),
                depth=0,
                start_us=0,
                duration_us=10,
                self_us=10,
                gpu_to_cpu_us=0,
                call_count=1,
                max_recursion_depth=1,
            )
            frame = engine.ProfilerFrame("Main", root_sample)
            frame_wire = serialize(frame, runtime.handles)
            with mock.patch.object(
                engine.ProfilerFrame,
                "resolve_names",
                autospec=True,
                return_value=frame,
            ) as resolve_names:
                runtime.call_tool(
                    "automation_bridge_call",
                    {
                        "operation": "automation_bridge.engine.ProfilerFrame.resolve_names",
                        "target": frame_wire,
                        "arguments": {"names": {"101": "render"}},
                    },
                )
            resolve_names.assert_called_once_with(frame, {101: "render"})

            recording = engine.ProfilerRecording(mock.Mock())
            recording_wire = serialize(recording, runtime.handles)
            with mock.patch.object(
                engine.ProfilerRecording,
                "abort",
                autospec=True,
                return_value=mock.sentinel.capture,
            ) as abort:
                runtime.call_tool(
                    "automation_bridge_call",
                    {
                        "operation": "automation_bridge.engine.ProfilerRecording.abort",
                        "target": recording_wire,
                        "arguments": {"cause": "cancelled by unit test", "timeout": 0.1},
                    },
                )
                actual_cause = abort.call_args.args[1]
                self.assertIsInstance(actual_cause, RuntimeError)
                self.assertEqual("cancelled by unit test", str(actual_cause))
                self.assertEqual(0.1, abort.call_args.kwargs["timeout"])
        finally:
            runtime.cleanup()


class StdioTransportContractTest(unittest.TestCase):
    def test_parse_error_batch_rejection_notifications_and_eof(self):
        runtime = FakeRuntime()
        protocol = McpProtocol(runtime)
        input_stream = io.StringIO(
            "{not-json}\n"
            '{"jsonrpc":"2.0","id":1,"id":2,"method":"ping"}\n'
            '{"jsonrpc":"2.0","id":3,"method":"ping","params":{"n":NaN}}\n'
            '{"jsonrpc":"2.0","id":4,"method":"ping","params":{"n":1e9999}}\n'
            "[]\n"
            + json.dumps({"jsonrpc": "2.0", "method": "ping"})
            + "\n"
        )
        output_stream = io.StringIO()
        StdioServer(protocol, stdin=input_stream, stdout=output_stream).serve_forever()

        lines = output_stream.getvalue().splitlines()
        self.assertEqual(5, len(lines))
        responses = [json.loads(line) for line in lines]
        self.assertCountEqual(
            [-32700, -32700, -32700, -32700, -32600],
            [_error_code(item) for item in responses],
        )
        self.assertTrue(runtime.cleaned)

    def test_cancelling_an_in_flight_stdio_call_suppresses_its_response(self):
        class SlowRuntime(FakeRuntime):
            def __init__(self):
                super().__init__()
                self.release_call = threading.Event()

            def call_tool(self, name, arguments):
                self.calls.append((name, arguments))
                self.release_call.wait(1.0)
                return {"ok": True, "data": "too late"}

            def cancel(self, request_id):
                super().cancel(request_id)
                self.release_call.set()

        runtime = SlowRuntime()
        call = _modern_request(
            "tools/call", "slow", {"name": "echo", "arguments": {"value": 1}}
        )
        cancellation = {
            "jsonrpc": "2.0",
            "method": "notifications/cancelled",
            "params": {
                "_meta": _modern_meta(),
                "requestId": "slow",
                "reason": "unit test",
            },
        }
        input_stream = io.StringIO(
            json.dumps(call) + "\n" + json.dumps(cancellation) + "\n"
        )
        output_stream = io.StringIO()
        StdioServer(
            McpProtocol(runtime),
            stdin=input_stream,
            stdout=output_stream,
            eof_join_timeout=2.0,
        ).serve_forever()

        self.assertEqual("", output_stream.getvalue())
        self.assertIn("slow", runtime.cancelled)
        self.assertTrue(runtime.cleaned)

    def test_module_entrypoint_stdout_contains_only_json_rpc_and_exits_at_eof(self):
        request = _modern_request("server/discover", "eof")
        environment = os.environ.copy()
        environment["PYTHONPATH"] = str(PYTHON_WRAPPER_ROOT)
        completed = subprocess.run(
            [sys.executable, "-m", "automation_bridge.mcp_server"],
            cwd=ROOT,
            env=environment,
            input=json.dumps(request) + "\n",
            text=True,
            capture_output=True,
            timeout=10,
            check=False,
        )
        self.assertEqual(0, completed.returncode, completed.stderr)
        lines = completed.stdout.splitlines()
        self.assertEqual(1, len(lines), completed.stdout)
        response = json.loads(lines[0])
        self.assertEqual("2.0", response["jsonrpc"])
        self.assertEqual("eof", response["id"])
        self.assertEqual("complete", response["result"]["resultType"])


if __name__ == "__main__":
    unittest.main()
