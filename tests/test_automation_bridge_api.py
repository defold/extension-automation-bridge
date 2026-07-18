#!/usr/bin/env python3
import base64
import hashlib
import importlib.util
import json
import math
import os
import socket
import struct
import sys
import tempfile
import threading
import time
import unittest
import urllib.parse
import zlib
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "automation_bridge" / "automation-bridge-python"))

import automation_bridge as automation_bridge_package  # noqa: E402
from automation_bridge import editor, engine  # noqa: E402
from automation_bridge.engine import (  # noqa: E402
    AutomationBridgeApiError,
    Error as AutomationBridgeError,
    EventBufferOverflow,
    EventStream,
    IncompatibleApiVersionError,
    InputExecutionError,
    InputReceipt,
    Element,
    ProfilerClient,
    ProfilerDataError,
    ScreenshotReceipt,
    UnsupportedCapabilityError,
    WaitTimeoutError,
    wait_until,
)
EngineClient = engine.Client
EditorApiClient = editor.Client
InstallationType = editor.Installation
from automation_bridge.client import EngineLogStream, _encode_system_reboot  # noqa: E402
from automation_bridge.profiler import (  # noqa: E402
    ProfilerCapture,
    ProfilerConnection,
    ProfilerProtocolError,
    ProfilerRecording,
    parse_resources_data,
)
from automation_bridge.visual import difference  # noqa: E402
from automation_bridge.remotery import (  # noqa: E402
    build_message,
    build_sample_name,
    parse_property_frame,
    parse_sample_frame,
)


EDITOR_OPENAPI = json.loads((ROOT / "tests/fixtures/editor_openapi.json").read_text(encoding="utf-8"))


def _read_automation_bridge_png(path):
    data = path.read_bytes()
    if data[:8] != b"\x89PNG\r\n\x1a\n":
        raise AssertionError(f"not a png: {path}")

    cursor = 8
    width = None
    height = None
    idat = bytearray()
    while cursor < len(data):
        if cursor + 8 > len(data):
            raise AssertionError(f"truncated png chunk header: {path}")
        size = struct.unpack(">I", data[cursor : cursor + 4])[0]
        chunk_type = data[cursor + 4 : cursor + 8]
        chunk_data = data[cursor + 8 : cursor + 8 + size]
        cursor += 12 + size
        if chunk_type == b"IHDR":
            width, height, bit_depth, color_type, compression, filter_method, interlace = struct.unpack(">IIBBBBB", chunk_data)
            if bit_depth != 8 or color_type != 6 or compression != 0 or filter_method != 0 or interlace != 0:
                raise AssertionError(f"unexpected png format: {path}")
        elif chunk_type == b"IDAT":
            idat.extend(chunk_data)
        elif chunk_type == b"IEND":
            break

    if not width or not height:
        raise AssertionError(f"missing png size: {path}")

    raw = zlib.decompress(bytes(idat))
    stride = width * 4
    pixels = bytearray()
    offset = 0
    for _ in range(height):
        filter_type = raw[offset]
        if filter_type != 0:
            raise AssertionError(f"unexpected png filter {filter_type}: {path}")
        offset += 1
        pixels.extend(raw[offset : offset + stride])
        offset += stride
    return width, height, bytes(pixels)


def _rgba_png(width, height, pixels):
    raw = b"".join(b"\0" + pixels[row * width * 4 : (row + 1) * width * 4] for row in range(height))

    def chunk(kind, payload):
        return struct.pack(">I", len(payload)) + kind + payload + struct.pack(">I", zlib.crc32(kind + payload) & 0xFFFFFFFF)

    header = struct.pack(">IIBBBBB", width, height, 8, 6, 0, 0, 0)
    return b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", header) + chunk(b"IDAT", zlib.compress(raw)) + chunk(b"IEND", b"")


def _point_segment_distance(px, py, start, end):
    x1, y1 = start
    x2, y2 = end
    dx = x2 - x1
    dy = y2 - y1
    length_squared = dx * dx + dy * dy
    if length_squared <= 0:
        return ((px - x1) ** 2 + (py - y1) ** 2) ** 0.5
    t = max(0.0, min(1.0, ((px - x1) * dx + (py - y1) * dy) / length_squared))
    nearest_x = x1 + t * dx
    nearest_y = y1 + t * dy
    return ((px - nearest_x) ** 2 + (py - nearest_y) ** 2) ** 0.5


def _count_orange_debug_pixels_near_segment(path, start, end, max_distance=12):
    width, height, pixels = _read_automation_bridge_png(path)
    count = 0
    for i in range(0, len(pixels), 4):
        r, g, b, a = pixels[i], pixels[i + 1], pixels[i + 2], pixels[i + 3]
        if not (a > 0 and r >= 150 and 80 <= g <= 230 and b <= 120 and r > g + 20 and g > b + 25):
            continue
        pixel_index = i // 4
        x = pixel_index % width
        row = pixel_index // width
        if _point_segment_distance(x + 0.5, row + 0.5, start, end) <= max_distance:
            count += 1
    return count


def _count_light_pixels_in_rect(path, rect):
    width, height, pixels = _read_automation_bridge_png(path)
    x0 = max(0, min(width, math.floor(float(rect["x"]))))
    y0 = max(0, min(height, math.floor(float(rect["y"]))))
    x1 = max(x0, min(width, math.ceil(float(rect["x"]) + float(rect["w"]))))
    y1 = max(y0, min(height, math.ceil(float(rect["y"]) + float(rect["h"]))))
    count = 0
    for y in range(y0, y1):
        for x in range(x0, x1):
            offset = (y * width + x) * 4
            r, g, b, a = pixels[offset : offset + 4]
            if a > 0 and r >= 220 and g >= 220 and b >= 220:
                count += 1
    return count


class EngineClientUnitTest(unittest.TestCase):
    def test_public_surface_excludes_removed_aliases_and_backend_types(self):
        bridge = EngineClient(1)
        removed_client_names = {
            "get", "post", "put", "delete", "post_json", "put_json",
            "optional", "assert_node", "wait_for_appearance",
            "wait_for_stable_frame", "wait_for_region_change", "record_video",
            "recording_capabilities", "recording_permission_diagnostics",
        }
        self.assertTrue(all(not hasattr(bridge, name) for name in removed_client_names))
        self.assertFalse(hasattr(Element({"parent": "root"}), "parent"))
        self.assertEqual(["editor", "engine"], automation_bridge_package.__all__)
        for removed in ("AutomationBridgeClient", "EditorClient", "Node", "Element"):
            self.assertFalse(hasattr(automation_bridge_package, removed))
        for removed in ("node", "nodes", "maybe_node", "by_id", "wait_for_node", "wait_for_ack", "recording"):
            self.assertFalse(hasattr(bridge, removed))
        self.assertTrue(hasattr(bridge, "element"))
        self.assertTrue(hasattr(bridge, "wait_for_input_acknowledgement"))
        self.assertTrue(hasattr(bridge, "video_recording"))
        self.assertTrue(hasattr(bridge, "metal_capture"))
        for removed in (
            "RecordingClient", "RecordingSession", "RecordingCapabilities",
            "RecordingMetadata", "RecordingError", "Node",
        ):
            self.assertFalse(hasattr(engine, removed))
        for removed in ("from_project", "from_editor"):
            self.assertFalse(hasattr(engine, removed))
        self.assertFalse(hasattr(editor, "Extensions"))

    def test_engine_connect_forwards_the_complete_connection_contract(self):
        sentinel = object()
        with mock.patch.object(engine, "Client", return_value=sentinel) as client:
            actual = engine.connect(
                51337,
                timeout=4.5,
                profiler_url="ws://localhost:17815/rmt",
                client_id="client",
                session_id="session",
                required_capabilities=("scene", "input.drag"),
            )

        self.assertIs(sentinel, actual)
        client.assert_called_once_with(
            51337,
            timeout=4.5,
            profiler_url="ws://localhost:17815/rmt",
            client_id="client",
            session_id="session",
            required_capabilities=("scene", "input.drag"),
        )

    def test_event_stream_resolves_now_before_wait_and_preserves_unmatched_events(self):
        class ScriptedClient:
            timeout = 10.0

            def __init__(self):
                self.requests = []

            def request(self, method, path, *, params=None, json=None):
                self.requests.append((path, params))
                if path == "/events/cursor":
                    return {"cursor": 5, "oldest_cursor": 2}
                if path == "/events":
                    return {
                        "events": [
                            {"sequence": 5, "type": "event", "name": "other", "data": {"id": 1}},
                            {"sequence": 6, "type": "event", "name": "done", "data": {"id": 42}},
                        ],
                        "next_cursor": 7,
                        "overflow": False,
                    }
                raise AssertionError(path)

        client = ScriptedClient()
        stream = EventStream(client, from_cursor="now")

        done = stream.wait("done", where={"id": 42}, timeout=0.1)
        other = stream.wait("other", timeout=0.1)

        self.assertEqual(5, client.requests[1][1]["cursor"])
        self.assertEqual(6, done.sequence)
        self.assertEqual(5, other.sequence)
        self.assertEqual(2, len(client.requests))

    def test_wait_for_input_acknowledgement_uses_full_protocol_event_name(self):
        game = EngineClient(1)
        stream = mock.Mock()
        expected = mock.sentinel.acknowledgement
        stream.wait.return_value = expected

        actual = game.wait_for_input_acknowledgement(42, timeout=3.0, events=stream)

        self.assertIs(expected, actual)
        stream.wait.assert_called_once_with(
            "input.acknowledged",
            where={"input_id": 42},
            event_type="acknowledgement",
            timeout=3.0,
        )

    def test_event_stream_reports_ring_overflow(self):
        class OverflowClient:
            timeout = 10.0

            def request(self, method, path, *, params=None, json=None):
                if path == "/events/cursor":
                    return {"cursor": 20, "oldest_cursor": 10}
                return {"events": [], "next_cursor": 10, "oldest_cursor": 10, "latest_cursor": 20, "overflow": True}

        stream = EventStream(OverflowClient(), from_cursor=1)
        with self.assertRaises(EventBufferOverflow) as raised:
            stream.poll()
        self.assertEqual(1, raised.exception.requested_cursor)
        self.assertEqual(10, raised.exception.oldest_cursor)

    def test_wait_for_state_can_require_a_new_revision(self):
        class StateClient(EngineClient):
            def __init__(self):
                super().__init__(12345)
                self.requests = []

            def _request(self, method, path, params=None):
                self.requests.append((method, path, params))
                if path == "/state":
                    return {"states": [{"name": "ui", "value": {"busy": False}, "revision": 2}], "revision": 2}
                if path == "/state/wait":
                    return {"states": [{"name": "ui", "value": {"busy": True}, "revision": 3}], "revision": 3}
                raise AssertionError(path)

        bridge = StateClient()
        result = bridge.wait_for_state("ui.busy", True, after_revision=2, timeout=0.1)

        self.assertTrue(result.value)
        self.assertEqual(3, result.revision)
        self.assertEqual(2, bridge.requests[1][2]["after_revision"])

    def test_semantic_element_metadata_is_typed(self):
        element = Element(
            {
                "id": "n:1",
                "automation_id": "operation_status",
                "localization_key": "status_ready",
                "role": "status",
            }
        )

        self.assertEqual("operation_status", element.automation_id)
        self.assertEqual("status_ready", element.localization_key)
        self.assertEqual("status", element.role)

    def test_command_and_marker_encode_bounded_json(self):
        class ApplicationClient(EngineClient):
            def __init__(self):
                super().__init__(12345)
                self.requests = []

            def _request(self, method, path, params=None):
                self.requests.append((method, path, params))
                if path == "/commands":
                    return {"command_id": 9, "state": "pending"}
                if path == "/markers":
                    return {"event_sequence": 12}
                raise AssertionError(path)

        bridge = ApplicationClient()
        accepted = bridge.start_command("test.load_fixture", {"name": "standard"}, timeout=2)
        marker = bridge.mark("workflow_started", {"case": 7}, recording_timestamp_us=1234)

        self.assertEqual(9, accepted["command_id"])
        self.assertEqual('{"name":"standard"}', bridge.requests[0][2]["data"])
        self.assertEqual(2000, bridge.requests[0][2]["timeout_ms"])
        self.assertEqual(12, marker["event_sequence"])
        self.assertEqual(1234, bridge.requests[1][2]["recording_timestamp_us"])

    def test_health_rejects_incompatible_native_api_version(self):
        bridge = FakeEngineClient()
        bridge._api_version = "2"

        with self.assertRaisesRegex(IncompatibleApiVersionError, "supported native range is 1..1"):
            bridge.health()

    def test_bridge_startup_does_not_retry_incompatible_api(self):
        calls = []

        def incompatible_probe():
            calls.append(True)
            raise IncompatibleApiVersionError("unsupported")

        with self.assertRaises(IncompatibleApiVersionError):
            EngineClient._wait_for_bridge(incompatible_probe, timeout=1.0, message="not ready")

        self.assertEqual([True], calls)

    def test_required_capability_reports_backend_and_available_capabilities(self):
        bridge = FakeEngineClient(capabilities=["runtime.health", "scene"])

        with self.assertRaisesRegex(UnsupportedCapabilityError, "application.events"):
            bridge.require("application.events")

    def test_capability_versions_support_minimum_declarations(self):
        bridge = FakeEngineClient(capabilities=["runtime.health", "scene"])
        bridge._capability_versions_data = {"scene": "2"}

        self.assertTrue(bridge.supports("scene>=2"))
        self.assertFalse(bridge.supports("scene>=3"))

    def test_trace_metadata_records_native_and_python_versions(self):
        bridge = FakeEngineClient(capabilities=["runtime.health"])

        metadata = bridge.trace_metadata()

        self.assertEqual("2.0.0", metadata["python_package_version"])
        self.assertEqual("1.1.0", metadata["native_version"])
        self.assertEqual({"runtime.health": 1}, metadata["capability_versions"])

    def test_headless_capability_subset_keeps_health_and_lifecycle_usable(self):
        bridge = FakeEngineClient(capabilities=["runtime.health", "runtime.lifecycle", "application.events"])
        bridge._backend = {"headless": True, "graphics": False, "hid": False}

        health = bridge.health()
        optional = {
            capability: bridge.supports(capability)
            for capability in ("application.events", "scene", "screenshot", "input.drag")
        }

        self.assertTrue(health["backend"]["headless"])
        self.assertEqual(
            {"application.events": True, "scene": False, "screenshot": False, "input.drag": False},
            optional,
        )

    def test_editor_rejects_cached_port_reused_by_another_engine_instance(self):
        with tempfile.TemporaryDirectory() as root:
            internal = Path(root) / ".internal"
            internal.mkdir()
            (internal / "automation_bridge.engine.port").write_text("43210\n", encoding="utf-8")
            (internal / "automation_bridge.engine.identity.json").write_text(
                '{"port":43210,"engine_instance_id":"engine:old","project_identity":"project:same"}\n',
                encoding="utf-8",
            )
            editor = EditorApiClient(root, port=12345)

            accepted = editor._validate_cached_engine_health(
                43210,
                {"identity": {"engine_instance_id": "engine:new", "project_identity": "project:same"}},
                fresh_build=False,
            )

        self.assertFalse(accepted)
        self.assertEqual("cached_port_rejected", editor.lifecycle_events[-1]["stage"])

    def test_editor_accepts_new_instance_after_fresh_build(self):
        with tempfile.TemporaryDirectory() as root:
            internal = Path(root) / ".internal"
            internal.mkdir()
            (internal / "automation_bridge.engine.port").write_text("43210\n", encoding="utf-8")
            (internal / "automation_bridge.engine.identity.json").write_text(
                '{"port":43210,"engine_instance_id":"engine:old","project_identity":"project:same"}\n',
                encoding="utf-8",
            )
            editor = EditorApiClient(root, port=12345)

            accepted = editor._validate_cached_engine_health(
                43210,
                {"identity": {"engine_instance_id": "engine:new", "project_identity": "project:same"}},
                fresh_build=True,
            )

        self.assertTrue(accepted)

    def test_editor_installations_returns_newest_valid_launcher(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            older_launcher = root / "older" / "Defold"
            newer_launcher = root / "newer" / "Defold"
            older_launcher.parent.mkdir()
            newer_launcher.parent.mkdir()
            older_launcher.touch()
            newer_launcher.touch()
            registry = root / "installations.json"
            registry.write_text(
                json.dumps([
                    {
                        "launcherPath": str(older_launcher),
                        "installPath": str(older_launcher.parent),
                        "lastLaunchedAt": "2026-07-06T12:00:00Z",
                    },
                    {
                        "launcherPath": str(root / "missing" / "Defold"),
                        "installPath": str(root / "missing"),
                        "lastLaunchedAt": "2026-07-10T12:00:00Z",
                    },
                    {
                        "launcherPath": str(newer_launcher),
                        "installPath": str(newer_launcher.parent),
                        "lastLaunchedAt": "2026-07-09T12:00:00Z",
                    },
                ]),
                encoding="utf-8",
            )
            with mock.patch.object(EditorApiClient, "_installation_registry_path", return_value=registry):
                installations = editor.installations()
                latest = editor.latest_installation()

        self.assertEqual([newer_launcher, older_launcher], [item.launcher_path for item in installations])
        self.assertIsInstance(latest, InstallationType)
        self.assertEqual(newer_launcher, latest.launcher_path)

    def test_editor_namespaces_expose_only_supported_automation_operations(self):
        project = EditorApiClient(".", port=12345)
        project._openapi_document = EDITOR_OPENAPI

        self.assertFalse(hasattr(project, "extensions"))
        self.assertFalse(hasattr(project, "window"))
        self.assertFalse(hasattr(project, "help"))
        for removed in ("from_project", "build", "is_running", "console_lines", "engine_service_port"):
            self.assertFalse(hasattr(project, removed))
        with mock.patch("automation_bridge.editor.request_raw", return_value=(202, b"")) as request:
            project.commands.hot_reload()
            project.debugger.step_over()
            project.build_and_run_html5()
        self.assertEqual(3, request.call_count)

        parameters = EDITOR_OPENAPI["paths"]["/command/{command}"]["post"]["parameters"]
        advertised = set(parameters[0]["schema"]["enum"])
        self.assertEqual(advertised, editor._SUPPORTED_COMMANDS | editor._EXCLUDED_COMMANDS)
        self.assertFalse(editor._SUPPORTED_COMMANDS & editor._EXCLUDED_COMMANDS)
        self.assertEqual({("/eval", "post")}, editor._EXCLUDED_PATHS)
        advertised_paths = {
            (path, method)
            for path, operations in EDITOR_OPENAPI["paths"].items()
            for method in operations
        }
        self.assertEqual(advertised_paths, editor._SUPPORTED_PATHS | editor._EXCLUDED_PATHS)

    def test_every_editor_command_wrapper_uses_its_advertised_command(self):
        project = EditorApiClient(".", port=12345)
        project._openapi_document = EDITOR_OPENAPI
        expected_empty_commands = [
            "hot-reload", "rebundle", "reload-extensions", "reload-stylesheets",
            "debugger-start", "debugger-stop", "debugger-break", "debugger-continue",
            "debugger-detach", "debugger-step-into", "debugger-step-out",
            "debugger-step-over", "build-html5",
        ]

        with mock.patch("automation_bridge.editor.request_raw", return_value=(202, b"")) as request:
            project.commands.hot_reload()
            project.commands.rebundle()
            project.commands.reload_extensions()
            project.commands.reload_stylesheets()
            project.debugger.start()
            project.debugger.stop()
            project.debugger.break_()
            project.debugger.continue_()
            project.debugger.detach()
            project.debugger.step_into()
            project.debugger.step_out()
            project.debugger.step_over()
            project.build_and_run_html5()

        actual_commands = [call.args[0].rsplit("/", 1)[-1] for call in request.call_args_list]
        self.assertEqual(expected_empty_commands, actual_commands)
        self.assertTrue(all(call.kwargs["method"] == "POST" for call in request.call_args_list))

    def test_editor_fetch_libraries_parses_results_and_reports_failure(self):
        project = EditorApiClient(".", port=12345)
        project._openapi_document = EDITOR_OPENAPI
        response = {
            "success": True,
            "libraries": [
                {"uri": "https://example.test/one.zip", "success": True},
                {"uri": "https://example.test/two.zip", "success": False, "message": "missing"},
            ],
        }
        with mock.patch("automation_bridge.editor.request_json", return_value=(200, response)) as request:
            result = project.commands.fetch_libraries(timeout=7)
        self.assertTrue(result.success)
        self.assertEqual((True, False), tuple(item.success for item in result.libraries))
        self.assertEqual("missing", result.libraries[1].message)
        self.assertEqual(7, request.call_args.kwargs["timeout"])

        with mock.patch("automation_bridge.editor.request_json", return_value=(500, {"success": False})):
            with self.assertRaises(editor.CommandError):
                project.commands.fetch_libraries()

    def test_editor_console_read_and_context_managed_stream(self):
        project = EditorApiClient(".", port=12345)
        project._openapi_document = EDITOR_OPENAPI
        snapshot_body = {
            "lines": ["first", "second"],
            "regions": [{"type": "stdout", "from": 0, "to": 2}, "ignored"],
        }
        with mock.patch("automation_bridge.editor.request_json", return_value=(200, snapshot_body)):
            snapshot = project.console.read()
        self.assertEqual(("first", "second"), snapshot.lines)
        self.assertEqual("stdout", snapshot.regions[0].raw["type"])

        response = mock.MagicMock()
        response.readline.side_effect = [b"streamed line\r\n", b""]
        with mock.patch("automation_bridge.editor.urllib.request.urlopen", return_value=response) as urlopen:
            with project.console.stream(connect_timeout=3, read_timeout=1.5) as stream:
                self.assertEqual("streamed line", stream.readline())
                self.assertIsNone(stream.readline())
        urlopen.assert_called_once_with("http://localhost:12345/console/stream", timeout=3)
        response.fp.raw._sock.settimeout.assert_called_with(1.5)
        response.close.assert_called_once_with()

    def test_editor_reference_search_encodes_filters_and_validates_response(self):
        project = EditorApiClient(".", port=12345)
        project._openapi_document = EDITOR_OPENAPI
        with mock.patch(
            "automation_bridge.editor.request_raw",
            return_value=(200, b'[{"name":"go.property"},null]'),
        ) as request:
            result = project.reference.search(environment="runtime", language="lua", query="go property")
        self.assertEqual([{"name": "go.property"}], result)
        url = request.call_args.args[0]
        self.assertEqual(
            {"environment": ["runtime"], "language": ["lua"], "q": ["go property"]},
            urllib.parse.parse_qs(urllib.parse.urlsplit(url).query),
        )

        with mock.patch("automation_bridge.editor.request_raw", return_value=(200, b'{"not":"a list"}')):
            with self.assertRaises(editor.HttpError):
                project.reference.search()

    def test_editor_build_error_preserves_typed_source_location(self):
        project = EditorApiClient(".", port=12345)
        project._openapi_document = EDITOR_OPENAPI
        issue = {
            "severity": "error",
            "message": "unexpected token",
            "resource": "/main/main.script",
            "range": {
                "start": {"line": 7, "character": 3},
                "end": {"line": 7, "character": 8},
            },
        }
        with (
            mock.patch.object(project, "_console_lines", return_value=[]),
            mock.patch.object(project, "_engine_service_port_value", return_value=None),
            mock.patch("automation_bridge.editor.request_json", return_value=(400, {"success": False, "issues": [issue]})),
        ):
            with self.assertRaises(editor.BuildError) as raised:
                project._build_and_run_command("build")

        actual = raised.exception.issues[0]
        self.assertEqual("/main/main.script", actual.resource)
        self.assertEqual((7, 3), (actual.range.start.line, actual.range.start.character))
        self.assertEqual((7, 8), (actual.range.end.line, actual.range.end.character))

    def test_editor_build_workflows_delegate_with_explicit_command_names(self):
        project = EditorApiClient(".", port=12345)
        sentinel = object()
        with mock.patch.object(EngineClient, "_from_editor", return_value=sentinel) as connect:
            self.assertIs(sentinel, project.build_and_run(timeout=12, required_capabilities=("scene",)))
            self.assertIs(sentinel, project.clean_build_and_run(timeout=13))
            self.assertIs(sentinel, project.connect_engine(timeout=14))
        self.assertEqual("build", connect.call_args_list[0].kwargs["build_command"])
        self.assertEqual("clean-build", connect.call_args_list[1].kwargs["build_command"])
        self.assertIsNone(connect.call_args_list[2].kwargs["build_command"])

    def test_editor_preferences_catalog_and_custom_paths(self):
        project = EditorApiClient(".", port=12345)
        project._openapi_document = EDITOR_OPENAPI
        preference = project.preferences.CODE_FONT_SIZE

        self.assertEqual("code/font/size", preference.path)
        self.assertEqual(12.0, preference.default)
        self.assertIn("Font size", preference.description)
        self.assertEqual(preference, project.preferences.describe("code/font/size"))
        self.assertIn(preference, project.preferences.list(prefix="code", type="number"))
        self.assertIsNone(project.preferences.describe("my-extension/custom"))

        response = mock.MagicMock()
        response.__enter__.return_value = response
        response.getcode.return_value = 200
        response.read.return_value = b"16"
        with mock.patch("automation_bridge.preferences.urllib.request.urlopen", return_value=response) as urlopen:
            self.assertEqual(16, project.preferences.get(preference))
            project.preferences.set("my-extension/custom", True)
        self.assertEqual(2, urlopen.call_count)

        descriptions_path = ROOT / "automation_bridge/automation-bridge-python/tools/preference_descriptions.json"
        descriptions = json.loads(descriptions_path.read_text(encoding="utf-8"))
        all_preferences = project.preferences.list(include_groups=True)
        self.assertEqual({item.path for item in all_preferences}, set(descriptions))

    def test_editor_preferences_filter_scope_groups_and_validate_paths(self):
        project = EditorApiClient(".", port=12345)
        project._openapi_document = EDITOR_OPENAPI
        groups = project.preferences.list(prefix="scene/grid", include_groups=True)
        self.assertTrue(any(item.group for item in groups))
        self.assertTrue(all(item.path == "scene/grid" or item.path.startswith("scene/grid/") for item in groups))
        project_scoped = project.preferences.list(scope="project")
        self.assertTrue(project_scoped)
        self.assertTrue(all(item.scope == "project" and not item.group for item in project_scoped))
        with self.assertRaises(ValueError):
            project.preferences.get("")
        with self.assertRaises(TypeError):
            project.preferences.describe(42)

    def test_preference_catalog_generator_fixture_and_failures(self):
        script_path = ROOT / "automation_bridge/automation-bridge-python/tools/sync_preferences.py"
        spec = importlib.util.spec_from_file_location("sync_preferences_for_test", script_path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        fixture = (ROOT / "tests/fixtures/preferences_schema.clj").read_text(encoding="utf-8")
        marker = "(def default-schema"
        schema = module.Reader(fixture[fixture.index(marker) + len(marker):]).read()
        generated = module.catalog(schema)
        descriptions = {item["path"]: f"Description for {item['path']}" for item in generated}
        generated = module.catalog(schema, descriptions)
        by_path = {item["path"]: item for item in generated}

        self.assertEqual("project", by_path["code/font-size"]["scope"])
        self.assertEqual("CODE_FONT_SIZE", by_path["code/font-size"]["name"])
        self.assertEqual(["light", "dark"], by_path["code/theme"]["enum_values"])
        self.assertEqual({"step": 1}, by_path["code/font-size"]["ui"])
        with self.assertRaisesRegex(RuntimeError, "missing curated preference descriptions"):
            module.catalog(schema, {})
        collision = {
            "type": "object",
            "properties": {
                "a-b": {"type": "string"},
                "a_b": {"type": "string"},
            },
        }
        with self.assertRaisesRegex(RuntimeError, "constant collision"):
            module.catalog(collision)

    def test_editor_installation_registry_paths_cover_supported_platforms(self):
        with mock.patch("automation_bridge.editor.sys.platform", "darwin"), mock.patch.object(Path, "home", return_value=Path("/home/me")):
            self.assertEqual(Path("/home/me/Library/Application Support/Defold/installations.json"), editor.installation_registry_path())
        with mock.patch("automation_bridge.editor.sys.platform", "win32"), mock.patch.dict(os.environ, {"LOCALAPPDATA": "C:/Users/me/AppData/Local"}):
            self.assertEqual(Path("C:/Users/me/AppData/Local/Defold/installations.json"), editor.installation_registry_path())
        with mock.patch("automation_bridge.editor.sys.platform", "linux"), mock.patch.dict(os.environ, {"XDG_STATE_HOME": "/state"}):
            self.assertEqual(Path("/state/Defold/installations.json"), editor.installation_registry_path())

    def test_lua_public_api_uses_only_full_acknowledgement_name(self):
        source = (ROOT / "automation_bridge/src/automation_bridge_application.cpp").read_text(encoding="utf-8")
        self.assertIn('{"acknowledge_input", LuaAcknowledgeInput}', source)
        self.assertNotIn('{"ack", LuaAcknowledgeInput}', source)

    def test_editor_unsupported_operation_is_reported_from_openapi(self):
        project = EditorApiClient(".", port=12345)
        project._openapi_document = {"paths": {}}
        with self.assertRaises(editor.UnsupportedOperationError):
            project.commands.hot_reload()

    def test_editor_from_project_reuses_running_editor(self):
        with tempfile.TemporaryDirectory() as root:
            internal = Path(root) / ".internal"
            internal.mkdir()
            with FakeHttpServer(b'{"openapi":"3.0.3"}') as server:
                (internal / "editor.port").write_text(str(server.port), encoding="utf-8")
                editor_client = editor.open_project(root)

        self.assertEqual(server.port, editor_client.port)
        self.assertEqual("editor_reused", editor_client.lifecycle_events[-1]["stage"])

    def test_editor_from_project_launches_latest_installation_when_port_is_missing(self):
        with tempfile.TemporaryDirectory() as root:
            project_root = Path(root).resolve()
            project_file = project_root / "game.project"
            project_file.write_text("[project]\ntitle = Test\n", encoding="utf-8")
            launcher = project_root / "Defold"
            launcher.touch()
            process = mock.Mock(pid=4321)

            def launch(*args, **kwargs):
                internal = project_root / ".internal"
                internal.mkdir()
                (internal / "editor.port").write_text("54321", encoding="utf-8")
                return process

            with mock.patch("automation_bridge.editor.sys.platform", "linux"):
                with mock.patch("automation_bridge.editor.subprocess.Popen", side_effect=launch) as popen:
                    with mock.patch.object(EditorApiClient, "_is_running", return_value=True):
                        editor_client = editor.open_project(root, launcher=launcher, timeout=0.2)

        self.assertEqual(54321, editor_client.port)
        self.assertEqual("editor_started", editor_client.lifecycle_events[-1]["stage"])
        self.assertEqual([str(launcher), str(project_file)], popen.call_args.args[0])
        self.assertEqual(project_root, popen.call_args.kwargs["cwd"])

    def test_editor_refuses_to_launch_gui_from_restricted_macos_sandbox(self):
        with tempfile.TemporaryDirectory() as root:
            project_root = Path(root).resolve()
            (project_root / "game.project").write_text("[project]\ntitle = Test\n", encoding="utf-8")
            launcher = project_root / "Defold"
            launcher.touch()

            with mock.patch("automation_bridge.editor.sys.platform", "darwin"):
                with mock.patch.dict(os.environ, {"CODEX_SANDBOX": "seatbelt"}):
                    with mock.patch("automation_bridge.editor.subprocess.Popen") as popen:
                        with self.assertRaisesRegex(editor.LaunchError, "escalated/unsandboxed"):
                            editor.open_project(root, launcher=launcher, timeout=0.2)

        popen.assert_not_called()

    def test_editor_preview_returns_png_and_encodes_dimensions(self):
        png = _rgba_png(2, 3, b"\x00\x00\x00\xff" * 6)
        with tempfile.TemporaryDirectory() as root:
            with FakeHttpServer(png) as server:
                editor_client = EditorApiClient(root, port=server.port)
                editor_client._openapi_document = {"paths": {"/preview/{path}": {"get": {}}}}
                preview = editor_client.preview.render(
                    "gui/main menu.gui",
                    width=320,
                    height=180,
                )

        self.assertEqual(png, preview)
        self.assertEqual(
            "GET /preview/gui/main%20menu.gui?width=320&height=180 HTTP/1.1",
            server.request_line,
        )

    def test_editor_preview_rejects_invalid_dimensions_before_request(self):
        with tempfile.TemporaryDirectory() as root:
            editor = EditorApiClient(root, port=12345)
            with self.assertRaisesRegex(ValueError, "width"):
                editor.preview.render("main/main.collection", width=0)

    def test_editor_preview_scales_project_display_dimensions(self):
        png = _rgba_png(2, 3, b"\x00\x00\x00\xff" * 6)
        with tempfile.TemporaryDirectory() as root:
            Path(root, "game.project").write_text(
                "[display]\nwidth = 960\nheight = 640\n",
                encoding="utf-8",
            )
            with FakeHttpServer(png) as server:
                editor_client = EditorApiClient(root, port=server.port)
                editor_client._openapi_document = {"paths": {"/preview/{path}": {"get": {}}}}
                preview = editor_client.preview.render(
                    "main/main.collection",
                    resolution_multiplier=0.5,
                )

        self.assertEqual(png, preview)
        self.assertEqual(
            "GET /preview/main/main.collection?width=480&height=320 HTTP/1.1",
            server.request_line,
        )

    def test_editor_preview_validates_resolution_multiplier(self):
        with tempfile.TemporaryDirectory() as root:
            editor = EditorApiClient(root, port=12345)
            with self.assertRaisesRegex(ValueError, "0.01 through 1.0"):
                editor.preview.render("main/main.collection", resolution_multiplier=0.001)
            with self.assertRaisesRegex(ValueError, "mutually exclusive"):
                editor.preview.render("main/main.collection", width=320, resolution_multiplier=0.5)

    def test_wait_for_count_accepts_zero(self):
        class FakeAutomationBridge:
            def count(self, **selector):
                return 0

        self.assertEqual(0, EngineClient.wait_for_count(FakeAutomationBridge(), 0, timeout=0.1, interval=0.01))

    def test_system_reboot_payload(self):
        self.assertEqual(b"\x0a\x01a\x12\x02bc", _encode_system_reboot(("a", "bc")))

    def test_system_reboot_rejects_too_many_args(self):
        with self.assertRaises(ValueError):
            _encode_system_reboot(("1", "2", "3", "4", "5", "6", "7"))

    def test_resize_remembers_window_size(self):
        bridge = FakeEngineClient()

        result = bridge.resize(640, 480, wait=0)

        self.assertEqual((640, 480, "resized"), (result["width"], result["height"], result["outcome"]))
        self.assertTrue(result["window_matches"])
        self.assertEqual((640, 480), bridge.last_window_size)
        self.assertEqual(
            [
                ("GET", "/health", None),
                ("PUT", "/screen", {"width": 640, "height": 480}),
            ],
            bridge.api_requests,
        )

    def test_resize_requires_screen_resize_capability(self):
        bridge = FakeEngineClient(capabilities=["scene", "nodes"])

        with self.assertRaisesRegex(AutomationBridgeError, "screen.resize"):
            bridge.resize(640, 480, wait=0)

        self.assertEqual([("GET", "/health", None)], bridge.api_requests)

    def test_resize_rejects_oversized_dimensions_before_request(self):
        bridge = FakeEngineClient()

        with self.assertRaises(ValueError):
            bridge.resize(0x80000000, 480, wait=0)

        self.assertEqual([], bridge.api_requests)

    def test_click_visualize_false_is_encoded(self):
        with FakeHttpServer(b'{"ok":true,"data":{"input_id":1,"state":"accepted"}}') as server:
            bridge = EngineClient(server.port, timeout=1.0, client_id="test-client", session_id="test-session")

            bridge.click(10, 20, wait=0, visualize=False)

        method, target, _ = server.request_line.split(" ")
        parsed = urllib.parse.urlsplit(target)
        payload = json.loads(server.request_body.decode("utf-8"))
        self.assertEqual("POST", method)
        self.assertEqual("/automation-bridge/v1/input/click", parsed.path)
        self.assertEqual(10, payload["x"])
        self.assertEqual(20, payload["y"])
        self.assertFalse(payload["visualize"])
        self.assertEqual("test-client", payload["client_id"])
        self.assertEqual("test-session", payload["session_id"])
        self.assertEqual(14, len(payload["request_id"]))

    def test_drag_visualize_false_is_encoded(self):
        with FakeHttpServer(b'{"ok":true,"data":{"input_id":2,"state":"accepted"}}') as server:
            bridge = EngineClient(server.port, timeout=1.0, client_id="test-client", session_id="test-session")

            bridge.drag((10, 20), (30, 40), duration=0.1, wait=0, visualize=False)

        method, target, _ = server.request_line.split(" ")
        parsed = urllib.parse.urlsplit(target)
        payload = json.loads(server.request_body.decode("utf-8"))
        self.assertEqual("POST", method)
        self.assertEqual("/automation-bridge/v1/input/drag", parsed.path)
        self.assertEqual(10, payload["x1"])
        self.assertEqual(40, payload["y2"])
        self.assertEqual(0.1, payload["duration"])
        self.assertFalse(payload["visualize"])

    def test_drag_path_serializes_segments_and_correlation(self):
        bridge = FakeInputClient()

        receipt = bridge.drag_path(
            [(1, 2), (3, 4), (5, 6)],
            durations=[0.1, 0.2],
            easing=["ease_in", "ease_out"],
            hold_before=0.03,
            hold_after=0.04,
            wait=False,
            expected_scene_sequence=7,
        )

        self.assertIsInstance(receipt, InputReceipt)
        self.assertEqual(42, receipt.input_id)
        _, path, params = bridge.api_requests[-1]
        self.assertEqual("/input/drag_path", path)
        self.assertEqual("1,2;3,4;5,6", params["points"])
        self.assertEqual("0.1,0.2", params["durations"])
        self.assertEqual("ease_in,ease_out", params["easing"])
        self.assertEqual(7, params["expected_scene_sequence"])
        self.assertEqual("test-client", params["client_id"])
        self.assertEqual("test-session", params["session_id"])

    def test_drag_path_serializes_quadratic_and_cubic_curves(self):
        bridge = FakeInputClient()

        bridge.drag_path([(0, 0), (20, 40), (60, 0)], [0.3], path="quadratic", wait=False)
        _, _, quadratic = bridge.api_requests[-1]
        self.assertEqual("quadratic", quadratic["path"])
        self.assertEqual("0,0;20,40;60,0", quadratic["points"])
        self.assertEqual("0.3", quadratic["durations"])

        bridge.drag_path(
            [(0, 0), (20, 40), (40, 40), (60, 0)],
            [0.4],
            path="cubic",
            wait=False,
        )
        _, _, cubic = bridge.api_requests[-1]
        self.assertEqual("cubic", cubic["path"])
        self.assertEqual("0,0;20,40;40,40;60,0", cubic["points"])
        self.assertEqual("0.4", cubic["durations"])

    def test_drag_path_rejects_invalid_curve_control_points(self):
        bridge = FakeInputClient()
        with self.assertRaisesRegex(ValueError, "quadratic.*exactly three"):
            bridge.drag_path([(0, 0), (1, 1)], [0.1], path="quadratic", wait=False)
        with self.assertRaisesRegex(ValueError, "cubic.*exactly four"):
            bridge.drag_path([(0, 0), (1, 1), (2, 2)], [0.1], path="cubic", wait=False)

    def test_input_wait_uses_native_status_until_released(self):
        bridge = FakeInputClient(statuses=["started", "released"])
        accepted = InputReceipt({"input_id": 42, "state": "accepted"})

        released = bridge.input.wait(accepted, timeout=0.1, interval=0)

        self.assertEqual("released", released.state)
        self.assertEqual(2, sum(1 for method, path, _ in bridge.api_requests if method == "GET" and path == "/input/status"))

    def test_input_wait_reports_native_failure(self):
        bridge = FakeInputClient(statuses=["failed"])

        with self.assertRaisesRegex(InputExecutionError, "device_unavailable"):
            bridge.input.wait({"input_id": 42, "state": "accepted"}, timeout=0.1, interval=0)

    def test_input_wait_interrupt_cancels_without_masking_interrupt(self):
        bridge = FakeInputClient(statuses=[KeyboardInterrupt()])

        with self.assertRaises(KeyboardInterrupt):
            bridge.input.wait({"input_id": 42, "state": "accepted"}, timeout=0.1, interval=0)

        cancel = next(request for request in bridge.api_requests if request[1] == "/input/cancel")
        self.assertTrue(cancel[2]["release"])

    def test_active_drag_interrupt_can_flush_later_input_without_masking(self):
        class CleanupFailureClient(FakeInputClient):
            cleanup_attempted = False

            def _request(self, method, path, params=None, json_body=None):
                if path == "/input/flush":
                    self.cleanup_attempted = True
                    raise RuntimeError("cleanup failed")
                return super()._request(method, path, params, json_body=json_body)

        bridge = CleanupFailureClient(statuses=[KeyboardInterrupt("stop")])

        with self.assertRaisesRegex(KeyboardInterrupt, "stop"):
            bridge.drag(
                (0, 0),
                (10, 10),
                duration=0.1,
                device="touch",
                flush_on_interrupt=True,
            )

        self.assertTrue(bridge.cleanup_attempted)

    def test_held_key_interrupt_requests_release(self):
        bridge = FakeInputClient(statuses=[KeyboardInterrupt("key stop")])

        with self.assertRaisesRegex(KeyboardInterrupt, "key stop"):
            bridge.key("KEY_ENTER", wait="released")

        cancel = next(request for request in bridge.api_requests if request[1] == "/input/cancel")
        self.assertTrue(cancel[2]["release"])

    def test_input_interruption_scope_flushes_after_event_wait_interrupt(self):
        bridge = FakeInputClient()

        with self.assertRaises(KeyboardInterrupt):
            with bridge.input.interruption_scope():
                bridge.drag((0, 0), (10, 10), duration=0.1, wait=False)
                wait_until(lambda: (_ for _ in ()).throw(KeyboardInterrupt()))

        flush = next(request for request in bridge.api_requests if request[1] == "/input/flush")
        self.assertTrue(flush[2]["release"])

    def test_pointer_context_exception_requests_cancel(self):
        bridge = FakeInputClient()

        with self.assertRaisesRegex(RuntimeError, "original"):
            with bridge.pointer((10, 20), lease=2.0) as pointer:
                pointer.move((20, 30), duration=0.1, easing="ease_in_out")
                raise RuntimeError("original")

        self.assertIn("/input/pointer/open", [path for _, path, _ in bridge.api_requests])
        self.assertIn("/input/pointer/move", [path for _, path, _ in bridge.api_requests])
        self.assertIn("/input/cancel", [path for _, path, _ in bridge.api_requests])

    def test_held_pointer_interrupt_survives_cancel_failure(self):
        class CancelFailureClient(FakeInputClient):
            def _request(self, method, path, params=None, json_body=None):
                if path == "/input/cancel":
                    raise RuntimeError("cancel failed")
                return super()._request(method, path, params, json_body=json_body)

        bridge = CancelFailureClient()

        with self.assertRaisesRegex(KeyboardInterrupt, "stop"):
            with bridge.pointer((10, 20), lease=2.0):
                raise KeyboardInterrupt("stop")

    def test_orientation_helpers_swap_last_known_size(self):
        bridge = FakeEngineClient({"window": {"width": 320, "height": 568}})

        landscape = bridge.set_landscape(wait=0)
        portrait = bridge.set_portrait(wait=0)

        self.assertEqual((568, 320), (landscape["width"], landscape["height"]))
        self.assertEqual((320, 568), (portrait["width"], portrait["height"]))
        self.assertEqual((320, 568), bridge.last_window_size)
        self.assertEqual(
            [
                ("GET", "/screen", None),
                ("GET", "/health", None),
                ("PUT", "/screen", {"width": 568, "height": 320}),
                ("GET", "/health", None),
                ("PUT", "/screen", {"width": 320, "height": 568}),
            ],
            bridge.api_requests,
        )

    def test_reboot_posts_system_reboot_without_waiting(self):
        bridge = FakeEngineClient()

        bridge.reboot("--config=foo=bar", "build/game.projectc", wait=False)

        self.assertEqual(
            [("/post/@system/reboot", _encode_system_reboot(("--config=foo=bar", "build/game.projectc")), None)],
            bridge.engine_posts,
        )

    def test_reboot_wait_accepts_endpoint_that_stays_available(self):
        bridge = RebootReadyClient()

        bridge.reboot("build/game.projectc", timeout=0.02)

        self.assertGreaterEqual(bridge.health_calls, 1)

    def test_reboot_wait_accepts_endpoint_after_temporary_outage(self):
        bridge = RebootReadyClient(failures=1)

        bridge.reboot("build/game.projectc", timeout=0.05)

        self.assertGreaterEqual(bridge.health_calls, 2)

    def test_fresh_build_ignores_cached_profiler_url(self):
        class FakeEditor:
            def _current_registration_remotery_urls(self):
                return []

            def _remotery_url_value(self):
                return "ws://127.0.0.1:17815/rmt"

        self.assertIsNone(EngineClient._editor_profiler_url(FakeEditor(), fresh_build=True))
        self.assertEqual(
            "ws://127.0.0.1:17815/rmt",
            EngineClient._editor_profiler_url(FakeEditor(), fresh_build=False),
        )

    def test_engine_log_stream_reads_lines(self):
        with FakeLogServer([b"0 OK\n", b"INFO:TEST: hello\n", b"WARNING:TEST: done\n"]) as server:
            with EngineLogStream("127.0.0.1", server.port, timeout=1.0, read_timeout=1.0) as logs:
                self.assertEqual("INFO:TEST: hello", logs.readline(timeout=1.0))
                self.assertEqual("WARNING:TEST: done", logs.readline(timeout=1.0))

    def test_engine_log_interrupt_closes_stream_and_preserves_interrupt(self):
        class InterruptingSocket:
            def __init__(self):
                self.timeout = None
                self.closed = False

            def gettimeout(self):
                return self.timeout

            def settimeout(self, value):
                self.timeout = value

            def recv(self, size):
                raise KeyboardInterrupt("log stop")

            def close(self):
                self.closed = True
                raise RuntimeError("close failed")

        stream = EngineLogStream.__new__(EngineLogStream)
        stream._socket = InterruptingSocket()
        stream._buffer = bytearray()

        with self.assertRaisesRegex(KeyboardInterrupt, "log stop"):
            stream.readline(timeout=0.1)

        self.assertIsNone(stream._socket)

    def test_log_timeout_restore_tolerates_concurrent_socket_close(self):
        class ClosingSocket:
            def __init__(self):
                self.set_calls = 0

            def gettimeout(self):
                return None

            def settimeout(self, value):
                self.set_calls += 1
                if self.set_calls == 2:
                    raise OSError("already closed")

            def recv(self, size):
                return b""

        stream = EngineLogStream.__new__(EngineLogStream)
        stream._socket = ClosingSocket()
        stream._buffer = bytearray()

        self.assertIsNone(stream.readline(timeout=0.1))

    def test_read_logs_collects_future_lines(self):
        bridge = FakeEngineClient()
        with FakeLogServer([b"0 OK\n", b"INFO:TEST: one\n", b"INFO:TEST: two\n"]) as server:
            bridge._engine_info = {"log_port": str(server.port)}

            self.assertEqual(
                ["INFO:TEST: one", "INFO:TEST: two"],
                bridge.read_logs(duration=1.0, limit=2, idle_timeout=0.05),
            )

    def test_logs_tail_buffers_engine_stream_and_filters_errors(self):
        bridge = FakeEngineClient()
        with FakeLogServer(
            [
                b"0 OK\n",
                b"INFO:GAME: started\n",
                b"ERROR:SCRIPT: missing value\n",
                b"INFO:GAME: continuing\n",
                b"ERROR:RESOURCE: missing atlas\n",
            ]
        ) as server:
            bridge._engine_info = {"log_port": str(server.port)}
            bridge.logs.start()
            lines = wait_until(
                lambda: bridge.logs.tail(100),
                timeout=1.0,
                interval=0.01,
                predicate=lambda value: len(value) == 4,
            )

        self.assertEqual(
            ["INFO:GAME: continuing", "ERROR:RESOURCE: missing atlas"],
            lines[-2:],
        )
        self.assertEqual(
            ["ERROR:SCRIPT: missing value", "ERROR:RESOURCE: missing atlas"],
            bridge.logs.tail(100, contains="ERROR:"),
        )
        self.assertEqual([], bridge.logs.tail(0))
        bridge.logs.close()

    def test_logs_tail_validates_limit(self):
        bridge = EngineClient(1)
        with self.assertRaisesRegex(ValueError, "non-negative integer"):
            bridge.logs.tail(-1)

    def test_profiler_accessor_uses_engine_port(self):
        bridge = EngineClient(12345, timeout=3.0, profiler_url="ws://127.0.0.1:17816/rmt")

        profiler = bridge.profiler

        self.assertIsInstance(profiler, ProfilerClient)
        self.assertEqual(12345, profiler.port)
        self.assertEqual(3.0, profiler.timeout)
        self.assertEqual("ws://127.0.0.1:17816/rmt", profiler.url)

        connection = profiler.connect()

        self.assertEqual("127.0.0.1", connection.host)
        self.assertEqual(17816, connection.port)
        self.assertEqual("/rmt", connection.path)

    def test_profiler_resources_requests_resources_data(self):
        payload = _resources_payload()
        with FakeHttpServer(payload) as server:
            resources = ProfilerClient(server.port, timeout=1.0).resources()

        self.assertEqual("GET /resources_data HTTP/1.1", server.request_line)
        self.assertEqual("/assets/player.texturec", resources[0].name)
        self.assertEqual(8192, resources[0].size)
        self.assertEqual("/main/main.collectionc", resources[1].name)
        self.assertEqual(4096, resources[1].size)

    def test_parse_resources_data(self):
        resources = parse_resources_data(_resources_payload())

        self.assertEqual(2, len(resources))
        self.assertEqual("/main/main.collectionc", resources[0].name)
        self.assertEqual(".collectionc", resources[0].type)
        self.assertEqual(4096, resources[0].size)
        self.assertEqual(1024, resources[0].size_on_disc)
        self.assertEqual(2, resources[0].ref_count)
        self.assertEqual("/assets/player.texturec", resources[1].name)

    def test_parse_resources_data_uses_disc_size_when_memory_size_is_missing(self):
        payload = (
            _profiler_string("RESS")
            + _profiler_string("/dynamic.texturec")
            + _profiler_string(".texturec")
            + (0).to_bytes(4, "little")
            + (2048).to_bytes(4, "little")
            + (1).to_bytes(4, "little")
        )

        resources = parse_resources_data(payload)

        self.assertEqual(2048, resources[0].size)
        self.assertEqual(2048, resources[0].size_on_disc)

    def test_parse_resources_data_rejects_unexpected_tag(self):
        with self.assertRaisesRegex(ProfilerDataError, "unexpected resource profiler tag"):
            parse_resources_data(_profiler_string("GOBJ"))

    def test_parse_resources_data_rejects_truncated_record(self):
        payload = _profiler_string("RESS") + _profiler_string("/main/main.collectionc") + b"\x08"

        with self.assertRaisesRegex(ProfilerDataError, "truncated profiler data"):
            parse_resources_data(payload)

    def test_profiler_connection_uses_requested_port(self):
        profiler = ProfilerClient(12345, timeout=3.0)

        connection = profiler.connect(port=23456)

        self.assertIsInstance(connection, ProfilerConnection)
        self.assertEqual(23456, connection.port)
        self.assertEqual(3.0, connection.timeout)

    def test_editor_latest_registration_remotery_urls(self):
        lines = [
            "INFO:ENGINE: Initialized Remotery (ws://127.0.0.1:11111/rmt)",
            "INFO:ENGINE: Automation Bridge endpoint registered",
            "INFO:ENGINE: Initialized Remotery (ws://127.0.0.1:22222/rmt)",
            "INFO:ENGINE: Engine service started on port 33333",
            "INFO:ENGINE: Automation Bridge endpoint registered",
        ]

        self.assertEqual(["ws://127.0.0.1:22222/rmt"], EditorApiClient._latest_registration_remotery_urls(lines))

    def test_parse_remotery_sample_frame(self):
        frame = parse_sample_frame(_remotery_sample_frame_body(), {1: "Frame", 2: "Update"})

        self.assertEqual("Main", frame.thread_name)
        self.assertEqual("Frame", frame.root.name)
        self.assertEqual(1000, frame.root.start_us)
        self.assertEqual(16000, frame.root.duration_us)
        self.assertEqual("Update", frame.root.children[0].name)
        self.assertEqual(1, frame.root.children[0].depth)
        self.assertEqual(17000, frame.end_us)

        aggregate = {entry.label: entry for entry in frame.aggregate()}
        self.assertEqual(16000, aggregate["Frame"].total_us)
        self.assertEqual(7000, aggregate["Update"].total_us)
        self.assertEqual(2, aggregate["Update"].call_count)

    def test_remotery_capture_scope_stats_filter_and_aggregate_frames(self):
        names = {1: "Frame", 2: "Update"}
        first = parse_sample_frame(_remotery_sample_frame_body(child_duration_us=7000), names)
        second = parse_sample_frame(_remotery_sample_frame_body(child_duration_us=9000, root_duration_us=18000), names)
        capture = ProfilerCapture(frames=(first, second))

        update = capture.scope("Frame/Update")

        self.assertEqual("Main", update.thread_name)
        self.assertEqual("Update", update.name)
        self.assertEqual(2, update.frames_seen)
        self.assertEqual(2, update.occurrences)
        self.assertEqual(4, update.calls_total)
        self.assertEqual(2.0, update.calls_avg)
        self.assertEqual(16.0, update.total.total_ms)
        self.assertEqual(8.0, update.total.avg_ms)
        self.assertEqual(8.0, update.total.median_ms)
        self.assertEqual(8.9, update.total.p95_ms)
        self.assertEqual(["Frame/Update"], [entry.path for entry in capture.scopes(path="Frame/*")])
        self.assertEqual(["Frame/Update"], [entry.path for entry in capture.scopes(regex="update")])
        self.assertEqual([], capture.scopes(regex="update", case_sensitive=True))

    def test_remotery_capture_counter_stats_filter_and_aggregate_snapshots(self):
        names = {100: "Memory", 101: "Used"}
        first = parse_property_frame(_remotery_property_frame_body(property_frame=1, used=100), names)
        second = parse_property_frame(_remotery_property_frame_body(property_frame=2, used=140), names)
        capture = ProfilerCapture(frames=(), property_frames=(first, second))

        used = capture.counter("Memory/Used")

        self.assertEqual("Used", used.name)
        self.assertEqual("u32", used.type)
        self.assertEqual(2, used.frames_seen)
        self.assertEqual(140, used.last_value)
        self.assertEqual(120.0, used.values.avg)
        self.assertEqual(120.0, used.values.median)
        self.assertEqual(138.0, used.values.p95)
        self.assertEqual(["Memory/Used"], [entry.path for entry in capture.counters(path="Memory/*")])
        self.assertEqual(["Memory/Used"], [entry.path for entry in capture.counters(contains="used")])
        self.assertEqual(["Memory/Used"], [entry.path for entry in capture.counters(regex="used")])
        self.assertEqual([], capture.counters(regex="used", case_sensitive=True))
        self.assertEqual([], capture.counters(name="Memory"))
        self.assertEqual("group", capture.counters(name="Memory", include_groups=True)[0].type)
        self.assertEqual(["Memory/Used"], [entry.path for entry in second.find("used", include_groups=False)])

    def test_parse_remotery_integer64_properties_from_writer_format(self):
        body = (
            struct.pack("<II", 2, 7)
            + _remotery_property(200, 2000, 0, 5, 42, previous_value=40, previous_value_frame=6)
            + _remotery_property(201, 2001, 0, 6, 1234567890123, previous_value=1234567890000, previous_value_frame=6)
        )

        frame = parse_property_frame(body, {200: "Signed", 201: "Unsigned"})

        self.assertEqual("s64", frame.properties[0].type)
        self.assertEqual(42, frame.properties[0].value)
        self.assertEqual(40, frame.properties[0].previous_value)
        self.assertEqual("u64", frame.properties[1].type)
        self.assertEqual(1234567890123, frame.properties[1].value)
        self.assertEqual(1234567890000, frame.properties[1].previous_value)

    def test_parse_remotery_integer_properties_reject_fractional_values(self):
        body = (
            struct.pack("<II", 1, 7)
            + _remotery_property(202, 2002, 0, 3, 1.5, previous_value=1, previous_value_frame=6)
        )

        with self.assertRaisesRegex(ProfilerProtocolError, "invalid integer value"):
            parse_property_frame(body, {202: "Fractional"})

    def test_remotery_client_get_frame_resolves_sample_names(self):
        sample_message = build_message("SMPL", _remotery_sample_frame_body())
        with FakeRemoteryServer(sample_message, {1: "Frame", 2: "Update"}) as server:
            with ProfilerConnection(port=server.port, timeout=1.0) as profiler:
                frame = profiler.get_frame(timeout=1.0)

        self.assertEqual({"GSMP1", "GSMP2"}, set(server.client_messages))
        self.assertEqual("Frame", frame.root.name)
        self.assertEqual("Update", frame.root.children[0].name)

    def test_profiler_start_recording_collects_until_stop(self):
        messages = [
            build_message("SMPL", _remotery_sample_frame_body(child_duration_us=7000)),
            build_message("SMPL", _remotery_sample_frame_body(child_duration_us=9000, root_duration_us=18000)),
            build_message("PSNP", _remotery_property_frame_body(property_frame=1, used=100)),
        ]
        names = {1: "Frame", 2: "Update", 100: "Memory", 101: "Used"}
        with FakeRemoteryServer(messages, names) as server:
            profiler = ProfilerClient(12345, timeout=1.0, profiler_url=f"ws://127.0.0.1:{server.port}/rmt")
            recording = profiler.start_recording(read_timeout=0.05)
            try:
                wait_until(
                    lambda: True if recording.frame_count >= 2 and recording.property_frame_count >= 1 else None,
                    timeout=2.0,
                    interval=0.01,
                    message="Remotery recording did not collect test frames",
                )
            finally:
                capture = recording.stop()

        update = capture.scope("Frame/Update")
        self.assertFalse(recording.running)
        self.assertEqual(2, len(capture.frames))
        self.assertEqual(8.0, update.self.avg_ms)
        self.assertEqual(100, capture.counter("Memory/Used").last_value)

    def test_element_exposes_snapshot_and_generational_identity(self):
        node = engine.Element(
            {
                "id": "n:1",
                "snapshot_id": "n:1",
                "instance_id": "/item",
                "instance_generation": 9,
                "logical_id": "instance:1:g9",
                "created_scene_sequence": 4,
                "scene_sequence": 12,
                "engine_frame": 99,
            }
        )

        self.assertEqual("n:1", node.snapshot_id)
        self.assertEqual("/item", node.instance_id)
        self.assertEqual(9, node.instance_generation)
        self.assertEqual("instance:1:g9", node.logical_id)
        self.assertEqual(4, node.created_scene_sequence)
        self.assertEqual(12, node.scene_sequence)
        self.assertEqual(99, node.engine_frame)

    def test_exact_selectors_and_count_stay_server_side(self):
        class SelectorClient(EngineClient):
            def __init__(self):
                super().__init__(1)
                self.requests = []

            def _request(self, method, path, params=None, json_body=None):
                self.requests.append((path, params))
                return {
                    "nodes": [{"id": "n:1", "name": "Play", "enabled": True}],
                    "matched": 1200,
                    "truncated": True,
                    "next_cursor": "10",
                    "scene_sequence": 5,
                    "engine_frame": 8,
                }

        bridge = SelectorClient()
        nodes = bridge.elements(name_exact="Play", enabled=True, case_sensitive=True, limit=10)
        count = bridge.count(name_exact="Play", enabled=True)

        self.assertEqual(1, len(nodes))
        self.assertEqual(1200, count)
        self.assertEqual("Play", bridge.requests[0][1]["name_exact"])
        self.assertNotIn("name", bridge.requests[0][1])
        self.assertEqual(0, bridge.requests[1][1]["limit"])

    def test_click_forwards_expected_scene_sequence(self):
        with FakeHttpServer(b'{"ok":true,"data":{"queued":"click"}}') as server:
            EngineClient(server.port, timeout=1.0).click(
                "n:1", wait=0, expected_scene_sequence=42
            )

        self.assertEqual(42, json.loads(server.request_body)["expected_scene_sequence"])

    def test_convert_point_uses_json_body(self):
        response = b'{"ok":true,"data":{"point":{"x":400,"y":300}}}'
        with FakeHttpServer(response) as server:
            point = EngineClient(server.port, timeout=1.0).convert_point(
                (0.5, 0.5), "normalized_viewport", "window"
            )

        self.assertEqual({"x": 400, "y": 300}, point)
        self.assertIn("Content-Type: application/json", server.request_text)
        self.assertIn(
            b'{"point":{"x":0.5,"y":0.5},"from_space":"normalized_viewport","to_space":"window"}',
            server.request_bytes,
        )

    def test_screenshot_waits_for_native_complete_receipt(self):
        class ScreenshotClient(EngineClient):
            def __init__(self):
                super().__init__(1)
                self.status_calls = 0

            def _request(self, method, path, params=None, json_body=None):
                if path == "/screenshot":
                    return {"capture_id": 7, "state": "pending", "path": "/tmp/shot.png"}
                self.status_calls += 1
                if self.status_calls == 1:
                    return {"capture_id": 7, "state": "pending", "path": "/tmp/shot.png"}
                return {
                    "capture_id": 7,
                    "state": "complete",
                    "path": "/tmp/shot.png",
                    "engine_frame": 20,
                    "scene_sequence": 10,
                    "width": 320,
                    "height": 200,
                    "sha256": "abc",
                }

        receipt = ScreenshotClient().screenshot(timeout=0.2)

        self.assertIsInstance(receipt, ScreenshotReceipt)
        self.assertEqual((7, 20, 10, 320, 200, "abc"), (
            receipt.capture_id,
            receipt.frame,
            receipt.scene_sequence,
            receipt.width,
            receipt.height,
            receipt.sha256,
        ))

    def test_screenshot_resolution_multiplier_returns_scaled_receipt(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "native.png"
            pixels = b"".join(
                bytes((x * 40, y * 80, 120, 255))
                for y in range(2)
                for x in range(4)
            )
            source.write_bytes(_rgba_png(4, 2, pixels))

            class ScreenshotClient(EngineClient):
                def __init__(self):
                    super().__init__(1)

                def _request(self, method, path, params=None, json_body=None):
                    if path == "/screenshot":
                        return {"capture_id": 9, "state": "pending", "path": str(source)}
                    return {
                        "capture_id": 9,
                        "state": "complete",
                        "path": str(source),
                        "engine_frame": 30,
                        "scene_sequence": 12,
                        "width": 4,
                        "height": 2,
                        "sha256": "native-sha",
                    }

            receipt = ScreenshotClient().screenshot(resolution_multiplier=0.5)

            self.assertEqual((2, 1), (receipt.width, receipt.height))
            self.assertEqual(0.5, receipt.raw["resolution_multiplier"])
            self.assertEqual(str(source), receipt.raw["source_path"])
            self.assertNotEqual(source, receipt.path)
            self.assertTrue(receipt.exists())
            self.assertEqual((2, 1), _read_automation_bridge_png(receipt.path)[:2])
            self.assertEqual(hashlib.sha256(receipt.read_bytes()).hexdigest(), receipt.sha256)

    def test_screenshot_validates_resolution_multiplier(self):
        bridge = EngineClient(1)
        with self.assertRaisesRegex(ValueError, "0.01 through 1.0"):
            bridge.screenshot(resolution_multiplier=0.001)
        with self.assertRaisesRegex(ValueError, "wait=True"):
            bridge.screenshot(wait=False, resolution_multiplier=0.5)

    def test_wait_frames_uses_native_frame_receipts(self):
        class FrameClient(EngineClient):
            def __init__(self):
                super().__init__(1)
                self.frame = 10

            def _request(self, method, path, params=None, json_body=None):
                value = self.frame
                self.frame += 1
                return {"engine_frame": value, "scene_sequence": value - 2}

        receipt = FrameClient().wait_frames(2, timeout=0.2, interval=0)

        self.assertGreaterEqual(receipt["engine_frame"], 12)

    def test_wait_frames_calculates_default_timeout_from_frame_count(self):
        class FrameClient(EngineClient):
            def _request(self, method, path, params=None, json_body=None):
                return {"engine_frame": 10, "scene_sequence": 8}

        with mock.patch("automation_bridge.client.wait_until", return_value={"engine_frame": 490}) as wait:
            receipt = FrameClient(1).wait_frames(480)

        self.assertEqual(490, receipt["engine_frame"])
        self.assertEqual(16.0, wait.call_args.kwargs["timeout"])

    def test_wait_frames_timeout_reports_progress_health_and_lifecycle(self):
        class FrameClient(EngineClient):
            def __init__(self):
                super().__init__(1)
                self.frame_requests = 0

            def _request(self, method, path, params=None, json_body=None):
                if path == "/health":
                    return {
                        "version": "1",
                        "capabilities": [],
                        "lifecycle": {"current_stage": "initial_scene_ready"},
                    }
                self.frame_requests += 1
                return {
                    "engine_frame": 10 if self.frame_requests == 1 else 11,
                    "scene_sequence": 4,
                }

        with self.assertRaises(WaitTimeoutError) as raised:
            FrameClient().wait_frames(5, timeout=0, interval=0)

        message = str(raised.exception)
        self.assertIn("initial_frame=10", message)
        self.assertIn("last_observed_frame=11", message)
        self.assertIn("frames_advancing=True", message)
        self.assertIn("lifecycle_stage='initial_scene_ready'", message)
        self.assertIn("engine_health=reachable", message)
        self.assertEqual(11, raised.exception.last_value["engine_frame"])

    def test_observe_element_counts_distinct_frames(self):
        class ObservationClient(EngineClient):
            def __init__(self):
                super().__init__(1)
                self.frame = 3

            def maybe_element(self, **selector):
                node = engine.Element(
                    {
                        "id": "n:1",
                        "logical_id": "instance:1:g2",
                        "scene_sequence": self.frame,
                        "engine_frame": self.frame,
                    }
                )
                self.frame += 1
                return node

        receipt = ObservationClient().observe_element(minimum_frames=3, timeout=0.2, interval=0)

        self.assertEqual("instance:1:g2", receipt.identity)
        self.assertEqual(3, receipt.observed_frames)
        self.assertEqual((3, 5), (receipt.first_frame, receipt.last_frame))

    def test_visual_difference_uses_normalized_rgb_mean_absolute_error(self):
        black = _rgba_png(1, 1, bytes((0, 0, 0, 255)))
        white = _rgba_png(1, 1, bytes((255, 255, 255, 255)))
        transparent_black = _rgba_png(1, 1, bytes((0, 0, 0, 0)))

        self.assertEqual(1.0, difference(black, white))
        self.assertEqual(0.0, difference(black, transparent_black))
        self.assertEqual(0.25, difference(black, transparent_black, include_alpha=True))

    def test_profiler_recording_context_abort_hook_cannot_mask_interrupt(self):
        class ProfilerConnectionStub:
            connected = True

            def __init__(self):
                self.stopped = 0

            def stop(self):
                self.stopped += 1

        class IdleThread:
            def is_alive(self):
                return False

            def join(self, timeout=None):
                pass

        client = ProfilerConnectionStub()
        hook_causes = []

        def failing_abort_hook(cause, capture):
            hook_causes.append(cause)
            raise RuntimeError("hook failed")

        recording = ProfilerRecording(
            client,
            close_on_stop=True,
            on_abort=failing_abort_hook,
        )
        recording._thread = IdleThread()

        with self.assertRaisesRegex(KeyboardInterrupt, "recording stop"):
            with recording:
                raise KeyboardInterrupt("recording stop")

        self.assertEqual("recording stop", str(hook_causes[0]))
        self.assertEqual(1, client.stopped)

    def test_profiler_recording_finalize_hook_receives_capture(self):
        class ProfilerConnectionStub:
            connected = True

            def stop(self):
                pass

        captures = []
        recording = ProfilerRecording(
            ProfilerConnectionStub(),
            close_on_stop=True,
            on_finalize=captures.append,
        )

        capture = recording.stop()

        self.assertIs(capture, captures[0])

    def test_interrupt_during_recorder_finalization_preserves_interrupt(self):
        class FailingClient:
            connected = True

            def stop(self):
                raise RuntimeError("socket close failed")

        class InterruptingThread:
            def is_alive(self):
                return True

            def join(self, timeout=None):
                raise KeyboardInterrupt("finalization stop")

        aborts = []
        recording = ProfilerRecording(
            FailingClient(),
            close_on_stop=True,
            on_abort=lambda cause, capture: aborts.append(cause),
        )
        recording._thread = InterruptingThread()

        with self.assertRaisesRegex(KeyboardInterrupt, "finalization stop"):
            recording.stop()

        self.assertEqual("finalization stop", str(aborts[0]))

    def test_screenshot_wait_interrupt_escapes_immediately(self):
        bridge = FakeInputClient()
        bridge._request = lambda *args, **kwargs: {
            "capture_id": 7,
            "state": "pending",
            "path": "/tmp/not-written.png",
        }

        with mock.patch("automation_bridge.client.wait_until", side_effect=KeyboardInterrupt("capture stop")):
            with self.assertRaisesRegex(KeyboardInterrupt, "capture stop"):
                bridge.screenshot(wait=True)

    def test_wait_timeout_reports_last_observation_and_scene(self):
        values = iter([{"ready": False, "scene_sequence": 7}, {"ready": False, "scene_sequence": 8}])
        with mock.patch("automation_bridge.waits.time.monotonic", side_effect=[10.0, 10.0, 10.5]), mock.patch(
            "automation_bridge.waits.time.sleep"
        ):
            with self.assertRaises(WaitTimeoutError) as raised:
                wait_until(
                    lambda: next(values),
                    timeout=0.1,
                    interval=0.1,
                    message="event missing",
                    predicate=lambda value: value["ready"],
                )

        error = raised.exception
        self.assertEqual({"ready": False, "scene_sequence": 8}, error.last_value)
        self.assertEqual(2, error.attempts)
        self.assertEqual(8, error.scene_sequence)
        self.assertEqual(0.5, error.elapsed)
        self.assertIn("last_value=", str(error))

    def test_wait_retries_only_selected_exceptions(self):
        class TransientEventError(RuntimeError):
            pass

        with self.assertRaises(WaitTimeoutError) as raised:
            wait_until(
                lambda: (_ for _ in ()).throw(TransientEventError("cursor unavailable")),
                timeout=0,
                retry_exceptions=(TransientEventError,),
            )

        self.assertIsInstance(raised.exception.last_exception, TransientEventError)
        with self.assertRaisesRegex(TypeError, "bug"):
            wait_until(
                lambda: (_ for _ in ()).throw(TypeError("bug")),
                timeout=1,
                retry_exceptions=(TransientEventError,),
            )


class FakeEngineClient(EngineClient):
    def __init__(self, screen=None, capabilities=None):
        super().__init__(12345)
        self.engine_posts = []
        self.api_requests = []
        self._screen = screen or {"window": {"width": 800, "height": 600}}
        self._capabilities = ["scene", "nodes", "node", "screen.resize"] if capabilities is None else list(capabilities)
        self._engine_info = {"log_port": "0"}
        self._api_version = "1"
        self._capability_versions_data = {name: "1" for name in self._capabilities}
        self._backend = {"headless": False, "graphics": True, "hid": True}

    def _request(self, method, path, params=None, json_body=None):
        request_values = json_body if json_body is not None else params
        self.api_requests.append((method, path, dict(request_values) if request_values is not None else None))
        if method == "GET" and path == "/health":
            return {
                "version": self._api_version,
                "native_version": "1.1.0",
                "capabilities": list(self._capabilities),
                "capability_versions": dict(self._capability_versions_data),
                "backend": dict(self._backend),
                "screen": self._screen,
            }
        if method == "GET" and path == "/screen":
            return self._screen
        if method == "PUT" and path == "/screen":
            self._screen = {
                **self._screen,
                "window": {"width": int(request_values["width"]), "height": int(request_values["height"])},
            }
            return self._screen
        raise AssertionError(f"unexpected request: {method} {path} {params}")

    def _post_engine_message(self, path, payload, timeout=None):
        self.engine_posts.append((path, payload, timeout))
        return b"OK"

    def engine_info(self):
        return self._engine_info


class FakeInputClient(EngineClient):
    def __init__(self, statuses=None):
        super().__init__(12345, client_id="test-client", session_id="test-session")
        self.api_requests = []
        self.statuses = list(statuses or [])

    def _request(self, method, path, params=None, json_body=None):
        params = json_body if json_body is not None else params
        params = dict(params) if params is not None else None
        self.api_requests.append((method, path, params))
        if method == "GET" and path == "/input/status":
            state = self.statuses.pop(0) if self.statuses else "released"
            if isinstance(state, BaseException):
                raise state
            return {
                "input_id": int(params["input_id"]),
                "state": state,
                "reason": "device_unavailable" if state == "failed" else None,
            }
        if method == "GET" and path == "/input/pending":
            return {"inputs": [], "count": 0}
        if path == "/input/flush":
            return {"cancel_requested": 1, "release": bool(params["release"])}
        if path == "/input/configure":
            return {"device": params["device"], "visualize": params.get("visualize", True)}
        if path.startswith("/input/"):
            return {
                "input_id": int(params.get("input_id", 42)),
                "state": "started" if path == "/input/cancel" else "accepted",
                "request_id": params.get("request_id"),
            }
        raise AssertionError(f"unexpected request: {method} {path} {params}")

    def _request_json(self, method, path, payload):
        return self._request(method, path, payload)


class RebootReadyClient(FakeEngineClient):
    def __init__(self, failures=0):
        super().__init__()
        self.failures = failures
        self.health_calls = 0

    def health(self):
        self.health_calls += 1
        if self.health_calls <= self.failures:
            raise AutomationBridgeError("engine is rebooting")
        return {"version": "1"}


def _profiler_string(value):
    encoded = value.encode("utf-8")
    return len(encoded).to_bytes(2, "little") + encoded


def _resources_payload():
    return (
        _profiler_string("RESS")
        + _profiler_string("/main/main.collectionc")
        + _profiler_string(".collectionc")
        + (4096).to_bytes(4, "little")
        + (1024).to_bytes(4, "little")
        + (2).to_bytes(4, "little")
        + _profiler_string("/assets/player.texturec")
        + _profiler_string(".texturec")
        + (8192).to_bytes(4, "little")
        + (2048).to_bytes(4, "little")
        + (1).to_bytes(4, "little")
    )


def _remotery_string(value):
    encoded = value.encode("utf-8")
    return struct.pack("<I", len(encoded)) + encoded


def _remotery_sample_frame_body(child_duration_us=7000, root_duration_us=16000, root_self_us=9000):
    child = _remotery_sample(
        name_hash=2,
        unique_id=20,
        colour=(20, 30, 40),
        depth=1,
        start_us=5000,
        duration_us=child_duration_us,
        self_us=child_duration_us,
        call_count=2,
    )
    root = _remotery_sample(
        name_hash=1,
        unique_id=10,
        colour=(10, 20, 30),
        depth=0,
        start_us=1000,
        duration_us=root_duration_us,
        self_us=root_self_us,
        call_count=1,
        children=child,
        child_count=1,
    )
    return _remotery_string("Main") + struct.pack("<II", 2, 0) + root


def _remotery_sample(
    name_hash,
    unique_id,
    colour,
    depth,
    start_us,
    duration_us,
    self_us,
    call_count,
    children=b"",
    child_count=0,
):
    return (
        struct.pack(
            "<II4BddddIII",
            name_hash,
            unique_id,
            colour[0],
            colour[1],
            colour[2],
            depth,
            start_us,
            duration_us,
            self_us,
            0,
            call_count,
            0,
            child_count,
        )
        + children
    )


def _remotery_property_frame_body(property_frame, used):
    memory = _remotery_property(
        name_hash=100,
        unique_id=1000,
        depth=0,
        property_type=0,
        value=0,
        child_count=1,
    )
    used_memory = _remotery_property(
        name_hash=101,
        unique_id=1001,
        depth=1,
        property_type=3,
        value=used,
        previous_value=used - 10,
        previous_value_frame=property_frame - 1,
    )
    return struct.pack("<II", 2, property_frame) + memory + used_memory


def _remotery_property(
    name_hash,
    unique_id,
    depth,
    property_type,
    value,
    previous_value=0,
    previous_value_frame=0,
    child_count=0,
):
    if property_type == 0:
        values = b"\x00" * 16
    elif property_type in (1, 2, 3, 4, 5, 6, 7):
        values = struct.pack("<dd", value, previous_value)
    else:
        raise ValueError(f"unsupported property type: {property_type}")
    return (
        struct.pack("<II", name_hash, unique_id)
        + bytes((0, 0, 0, depth))
        + struct.pack("<I", property_type)
        + values
        + struct.pack("<II", previous_value_frame, child_count)
    )


class FakeLogServer:
    def __init__(self, chunks):
        self.chunks = chunks
        self.port = 0
        self._socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._thread = None
        self._error = None

    def __enter__(self):
        self._socket.bind(("127.0.0.1", 0))
        self._socket.listen(1)
        self.port = self._socket.getsockname()[1]
        self._thread = threading.Thread(target=self._serve, daemon=True)
        self._thread.start()
        return self

    def __exit__(self, exc_type, exc, traceback):
        self._socket.close()
        if self._thread:
            self._thread.join(timeout=1.0)
        if exc_type is None and self._error:
            raise self._error

    def _serve(self):
        try:
            client, _ = self._socket.accept()
            with client:
                for chunk in self.chunks:
                    client.sendall(chunk)
        except OSError as exc:
            self._error = exc


class FakeHttpServer:
    def __init__(self, body, status=200, reason="OK"):
        self.body = body
        self.status = status
        self.reason = reason
        self.port = 0
        self.request_line = None
        self.request_body = b""
        self.request_text = ""
        self.request_bytes = b""
        self._socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._thread = None
        self._error = None

    def __enter__(self):
        self._socket.bind(("127.0.0.1", 0))
        self._socket.listen(1)
        self.port = self._socket.getsockname()[1]
        self._thread = threading.Thread(target=self._serve, daemon=True)
        self._thread.start()
        return self

    def __exit__(self, exc_type, exc, traceback):
        self._socket.close()
        if self._thread:
            self._thread.join(timeout=1.0)
        if exc_type is None and self._error:
            raise self._error

    def _serve(self):
        try:
            client, _ = self._socket.accept()
            with client:
                request_bytes = bytearray()
                while b"\r\n\r\n" not in request_bytes:
                    chunk = client.recv(4096)
                    if not chunk:
                        break
                    request_bytes.extend(chunk)
                header_bytes, _, initial_body = bytes(request_bytes).partition(b"\r\n\r\n")
                content_length = 0
                for line in header_bytes.split(b"\r\n")[1:]:
                    if line.lower().startswith(b"content-length:"):
                        content_length = int(line.split(b":", 1)[1].strip())
                body = bytearray(initial_body)
                while len(body) < content_length:
                    chunk = client.recv(content_length - len(body))
                    if not chunk:
                        break
                    body.extend(chunk)
                self.request_bytes = header_bytes + b"\r\n\r\n" + bytes(body)
                request = self.request_bytes.decode("iso-8859-1", "replace")
                self.request_text = request
                self.request_line = request.splitlines()[0] if request else ""
                self.request_body = bytes(body)
                headers = (
                    f"HTTP/1.1 {self.status} {self.reason}\r\n"
                    f"Content-Length: {len(self.body)}\r\n"
                    "Connection: close\r\n"
                    "\r\n"
                ).encode("ascii")
                client.sendall(headers + self.body)
        except OSError as exc:
            self._error = exc


class FakeRemoteryServer:
    def __init__(self, sample_message, sample_names):
        self.sample_messages = sample_message if isinstance(sample_message, list) else [sample_message]
        self.sample_names = sample_names
        self.client_messages = []
        self.port = 0
        self._socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._thread = None
        self._error = None

    def __enter__(self):
        self._socket.bind(("127.0.0.1", 0))
        self._socket.listen(1)
        self.port = self._socket.getsockname()[1]
        self._thread = threading.Thread(target=self._serve, daemon=True)
        self._thread.start()
        return self

    def __exit__(self, exc_type, exc, traceback):
        self._socket.close()
        if self._thread:
            self._thread.join(timeout=1.0)
        if exc_type is None and self._error:
            raise self._error

    def _serve(self):
        try:
            client, _ = self._socket.accept()
            with client:
                client.settimeout(1.0)
                self._handshake(client)
                for sample_message in self.sample_messages:
                    self._send_frame(client, 0x2, sample_message)
                while True:
                    try:
                        opcode, payload = self._recv_frame(client)
                    except socket.timeout:
                        continue
                    if opcode == 0x8:
                        break
                    message = payload.decode("utf-8")
                    self.client_messages.append(message)
                    if message.startswith("GSMP"):
                        name_hash = int(message[4:])
                        name = self.sample_names.get(name_hash)
                        if name is not None:
                            body = build_sample_name(name_hash, name)
                            self._send_frame(client, 0x2, build_message("SSMP", body))
        except OSError as exc:
            self._error = exc

    def _handshake(self, client):
        request = self._recv_until(client, b"\r\n\r\n").decode("iso-8859-1", "replace")
        key = None
        for line in request.split("\r\n"):
            if line.lower().startswith("sec-websocket-key:"):
                key = line.split(":", 1)[1].strip()
                break
        if key is None:
            raise AssertionError("missing Sec-WebSocket-Key")

        accept = base64.b64encode(
            hashlib.sha1((key + "258EAFA5-E914-47DA-95CA-C5AB0DC85B11").encode("ascii")).digest()
        ).decode("ascii")
        response = (
            "HTTP/1.1 101 Switching Protocols\r\n"
            "Upgrade: websocket\r\n"
            "Connection: Upgrade\r\n"
            f"Sec-WebSocket-Accept: {accept}\r\n"
            "\r\n"
        ).encode("ascii")
        client.sendall(response)

    def _recv_until(self, client, marker):
        data = bytearray()
        while marker not in data:
            chunk = client.recv(1)
            if not chunk:
                raise AssertionError("connection closed")
            data.extend(chunk)
        return bytes(data)

    def _recv_frame(self, client):
        header = self._recv_exact(client, 2)
        opcode = header[0] & 0x0F
        length = header[1] & 0x7F
        if length == 126:
            length = struct.unpack("!H", self._recv_exact(client, 2))[0]
        elif length == 127:
            length = struct.unpack("!Q", self._recv_exact(client, 8))[0]
        mask = self._recv_exact(client, 4) if (header[1] & 0x80) else b""
        payload = self._recv_exact(client, length) if length else b""
        if mask:
            payload = bytes(byte ^ mask[index & 3] for index, byte in enumerate(payload))
        return opcode, payload

    def _recv_exact(self, client, size):
        data = bytearray()
        while len(data) < size:
            chunk = client.recv(size - len(data))
            if not chunk:
                raise AssertionError("connection closed")
            data.extend(chunk)
        return bytes(data)

    def _send_frame(self, client, opcode, payload):
        length = len(payload)
        if length <= 125:
            header = bytes([0x80 | opcode, length])
        elif length <= 0xFFFF:
            header = bytes([0x80 | opcode, 126]) + struct.pack("!H", length)
        else:
            header = bytes([0x80 | opcode, 127]) + struct.pack("!Q", length)
        client.sendall(header + payload)


class AutomationBridgeApiTest(unittest.TestCase):
    SPRITE_COUNTER_NAME = "Sprite"

    @classmethod
    def setUpClass(cls):
        cls.editor = None
        cls.bridge = None
        port = os.environ.get("AUTOMATION_BRIDGE_ENGINE_PORT")
        if port:
            remotery_url = os.environ.get("AUTOMATION_BRIDGE_REMOTERY_URL")
            remotery_port = os.environ.get("AUTOMATION_BRIDGE_REMOTERY_PORT")
            if remotery_url is None and remotery_port:
                remotery_url = f"ws://127.0.0.1:{remotery_port}/rmt"
            cls.bridge = EngineClient(int(port), profiler_url=remotery_url)
            cls.bridge.wait_ready()
            return

        try:
            cls.editor = editor.open_project(ROOT, start_if_needed=False)
        except (FileNotFoundError, editor.NotRunningError) as exc:
            raise unittest.SkipTest(str(exc)) from exc

    @classmethod
    def close_bridge(cls):
        if getattr(cls, "bridge", None) is None:
            return
        bridge = cls.bridge
        cls.bridge = None
        bridge.close_engine()

    @classmethod
    def tearDownClass(cls):
        cls.close_bridge()

    def setUp(self):
        if self.bridge:
            self.reset_if_popup_is_visible()

    def test_automation_bridge_api_end_to_end(self):
        previous_port = None
        run_count = 1 if self.editor is None else 2

        for run_index in range(run_count):
            if self.editor is not None:
                self.bridge = self.editor.build_and_run(timeout=20)
                self.__class__.bridge = self.bridge
                self.bridge.wait_ready()
                if previous_port is not None:
                    self.assertNotEqual(previous_port, self.bridge.port)
                previous_port = self.bridge.port

            with self.subTest(run=run_index + 1):
                if run_index == 1 or (run_index == 0 and self.editor is None):
                    if self.supports_capability("screen.resize"):
                        resized = self.bridge.resize(1600, 1200, wait=0.3)
                        self.assertEqual((1600, 1200), (resized["width"], resized["height"]))
                        self.assertTrue(resized["window_matches"])
                        portrait = self.bridge.set_portrait(wait=0.3)
                        self.assertEqual((1200, 1600), (portrait["width"], portrait["height"]))
                    else:
                        print("SCREEN_RESIZE_SKIPPED capability unavailable")
                self.reset_if_popup_is_visible()
                self.run_automation_bridge_api_end_to_end()

    def test_drag_item_along_cubic_curve_and_closed_circle(self):
        self.ensure_running_bridge()
        self.reset_if_popup_is_visible()
        spawner = self.bridge.element(type="goc", name_exact="/spawner", visible=True)

        for _ in range(2):
            before = self.label_count("L1")
            self.bridge.click(spawner)
            wait_until(lambda: self.label_count("L1") > before, timeout=2, message="curved-drag item missing")

        before = self.label_count("L1")
        first, second = self.parents_for_label("L1")[:2]
        screen = self.bridge.screen()["window"]
        start = first.center
        target = second.center
        dx = target["x"] - start["x"]
        dy = target["y"] - start["y"]
        distance = max(1.0, math.hypot(dx, dy))
        bend = min(140.0, max(70.0, distance * 0.6))
        normal_x = -dy / distance
        normal_y = dx / distance

        def controls(side):
            return tuple(
                (
                    min(screen["width"] - 8.0, max(8.0, start["x"] + dx * fraction + normal_x * bend * side)),
                    min(screen["height"] - 8.0, max(8.0, start["y"] + dy * fraction + normal_y * bend * side)),
                )
                for fraction in (1.0 / 3.0, 2.0 / 3.0)
            )

        positive_controls = controls(1.0)
        negative_controls = controls(-1.0)

        def bend_score(points):
            return sum(abs(dx * (point[1] - start["y"]) - dy * (point[0] - start["x"])) / distance for point in points)

        control_one, control_two = max((positive_controls, negative_controls), key=bend_score)
        curved = self.bridge.drag_path(
            [start, control_one, control_two, target],
            [0.45],
            path="cubic",
            easing="ease_in_out",
        )
        self.assertEqual("released", curved.state)
        self.wait_for_merge("L1", before, "L2")

        before = self.label_count("L1")
        self.bridge.click(spawner)
        wait_until(lambda: self.label_count("L1") > before, timeout=2, message="circular-drag item missing")
        item_label = self.bridge.element(type="labelc", text="L1", visible=True)
        item = self.bridge.parent(item_label)
        center_x = float(item.center["x"])
        center_y = float(item.center["y"])
        right_space = screen["width"] - center_x - 8.0
        left_space = center_x - 8.0
        direction = 1.0 if right_space >= left_space else -1.0
        horizontal_space = right_space if direction > 0 else left_space
        radius = min(70.0, horizontal_space / 2.0, center_y - 8.0, screen["height"] - center_y - 8.0)
        self.assertGreaterEqual(radius, 30.0, "test item has insufficient room for a visible circular drag")
        circle_center_x = center_x + direction * radius
        start_angle = math.pi if direction > 0 else 0.0
        segment_count = 24
        circle = [
            (
                circle_center_x + radius * math.cos(start_angle + 2.0 * math.pi * index / segment_count),
                center_y + radius * math.sin(start_angle + 2.0 * math.pi * index / segment_count),
            )
            for index in range(segment_count + 1)
        ]
        circular = self.bridge.drag_path(
            circle,
            [0.02] * segment_count,
            path="sampled",
            easing="linear",
        )
        self.assertEqual("released", circular.state)
        self.assertEqual(before + 1, self.label_count("L1"))

    def test_drag_negative_duration_is_rejected(self):
        self.ensure_running_bridge()
        self.reset_if_popup_is_visible()
        spawner = self.bridge.element(type="goc", name_exact="/spawner", visible=True)

        start_count = self.label_count("L1")
        self.bridge.click(spawner)
        wait_until(lambda: self.label_count("L1") > start_count, timeout=2, message="first negative-drag item missing")
        self.bridge.click(spawner.center)
        wait_until(lambda: self.label_count("L1") > start_count + 1, timeout=2, message="second negative-drag item missing")

        before = self.label_count("L1")
        first, second = self.parents_for_label("L1")[:2]

        with self.assertRaises(AutomationBridgeApiError) as raised:
            self.bridge.drag(first, second, duration=-0.25, wait=False)

        self.assertEqual("bad_request", raised.exception.code)
        self.assertEqual(before, self.label_count("L1"))

    def test_drag_visualization_is_visible_in_screenshot(self):
        self.ensure_running_bridge()
        self.reset_if_popup_is_visible()
        screen = self.bridge.screen()
        window = screen["window"]
        y = max(4, window["height"] - 16)
        start = (max(4, window["width"] * 0.15), y)
        end = (min(window["width"] - 4, window["width"] * 0.85), y)
        baseline_path = self.bridge.screenshot(wait=True, timeout=5)
        baseline_orange = _count_orange_debug_pixels_near_segment(baseline_path, start, end)

        self.bridge.drag(start, end, duration=1.0, wait=0)
        last_observation = {"path": None, "orange_pixels": 0, "baseline_path": baseline_path, "baseline_orange": baseline_orange}

        def visible_overlay():
            screenshot_path = self.bridge.screenshot(wait=True, timeout=5)
            orange_pixels = _count_orange_debug_pixels_near_segment(screenshot_path, start, end)
            last_observation["path"] = screenshot_path
            last_observation["orange_pixels"] = orange_pixels
            print(
                "DRAG_VISUALIZATION_SCREENSHOT "
                f"{screenshot_path} orange_pixels={orange_pixels} baseline_orange={baseline_orange}"
            )
            return orange_pixels if orange_pixels > max(20, baseline_orange + 20) else None

        try:
            orange_pixels = wait_until(
                visible_overlay,
                timeout=1.2,
                interval=0.05,
                message=f"drag visualization was not visible: {last_observation}",
            )
            self.assertGreater(orange_pixels, max(20, baseline_orange + 20))
        finally:
            time.sleep(1.0)

    def test_screenshot_rows_match_top_left_scene_bounds(self):
        self.ensure_running_bridge()
        fixture = self.bridge.element(
            type="goc",
            name_exact="/bounds_fixture",
            visible=True,
            include=["basic", "bounds"],
        )
        self.assertIsNotNone(fixture.bounds)

        screenshot = self.bridge.screenshot(wait=True, timeout=5)
        bounds = dict(fixture.bounds.screen)
        mirrored = dict(bounds)
        mirrored["y"] = screenshot.height - float(bounds["y"]) - float(bounds["h"])
        aligned_light_pixels = _count_light_pixels_in_rect(screenshot, bounds)
        mirrored_light_pixels = _count_light_pixels_in_rect(screenshot, mirrored)

        self.assertGreater(aligned_light_pixels, 20, (screenshot, bounds))
        self.assertGreater(aligned_light_pixels, mirrored_light_pixels + 20, (screenshot, bounds, mirrored))

    def test_remotery_sprite_counter_after_actions(self):
        self.ensure_running_bridge()
        if self.editor is None and self.bridge.profiler_url is None:
            raise unittest.SkipTest("set AUTOMATION_BRIDGE_REMOTERY_URL or AUTOMATION_BRIDGE_REMOTERY_PORT for profiler tests")
        self.reset_if_popup_is_visible()
        spawner = self.bridge.element(type="goc", name_exact="/spawner", visible=True)
        observations = []

        self.record_sprite_counter(observations, "initial")

        before = self.label_count("L1")
        self.bridge.click(spawner)
        wait_until(lambda: self.label_count("L1") > before, timeout=2, message="first spawn did not appear")
        self.record_sprite_counter(observations, "click spawner node")

        before = self.label_count("L1")
        self.bridge.click(spawner.center)
        wait_until(lambda: self.label_count("L1") > before, timeout=2, message="second spawn did not appear")
        self.record_sprite_counter(observations, "click spawner coordinates")

        self.assert_drag_merge_by_node_ids("L1", expected_new_level="L2")
        self.record_sprite_counter(observations, "drag merge L1 by ids")

        before = self.label_count("L1")
        self.bridge.click(spawner)
        wait_until(lambda: self.label_count("L1") > before, timeout=2, message="third spawn did not appear")
        self.record_sprite_counter(observations, "click spawner node again")

        before = self.label_count("L1")
        self.bridge.click(spawner.center)
        wait_until(lambda: self.label_count("L1") > before, timeout=2, message="fourth spawn did not appear")
        self.record_sprite_counter(observations, "click spawner coordinates again")

        self.assert_drag_merge_by_coordinates("L1", expected_new_level="L2")
        self.record_sprite_counter(observations, "drag merge L1 by coordinates")

        self.bridge.type_text("hello")
        self.record_sprite_counter(observations, "type text")

        self.bridge.key("KEY_ENTER")
        self.record_sprite_counter(observations, "key enter")

        self.merge_label("L2")
        self.record_sprite_counter(observations, "drag merge L2")

        spawner = self.bridge.element(type="goc", name_exact="/spawner", visible=True)
        for index in range(4):
            before = self.label_count("L1")
            self.bridge.click(spawner)
            wait_until(
                lambda: self.label_count("L1") > before,
                timeout=2,
                message=f"finish spawn {index + 1} did not appear",
            )
            self.record_sprite_counter(observations, f"finish spawn {index + 1}")

        self.merge_label("L1")
        self.record_sprite_counter(observations, "finish merge L1 #1")
        self.merge_label("L1")
        self.record_sprite_counter(observations, "finish merge L1 #2")
        self.merge_label("L2")
        self.record_sprite_counter(observations, "finish merge L2")
        self.merge_label("L3")
        self.assertEqual(1, self.label_count("L4"))
        self.record_sprite_counter(observations, "finish merge L3")

        restart = self.bridge.element(name_exact="restart", enabled=True)
        self.bridge.click(restart, wait=0.4)
        self.assertEqual(0, len(self.item_labels()))
        self.record_sprite_counter(observations, "restart")

        for label, scene_count, counter_value in observations:
            print(f"SPRITE_COUNTER {label}: scene_spritec={scene_count} remotery_Sprite={counter_value}")

    def ensure_running_bridge(self):
        if self.bridge is not None:
            self.bridge.wait_ready()
            return
        if self.editor is None:
            raise unittest.SkipTest("no Automation Bridge engine or Defold editor is available")
        self.bridge = self.editor.build_and_run(timeout=20)
        self.__class__.bridge = self.bridge
        self.bridge.wait_ready()

    def supports_capability(self, capability):
        return capability in self.bridge.health().get("capabilities", [])

    def record_sprite_counter(self, observations, label):
        scene_count = self.bridge.count(type="spritec")
        with self.bridge.profiler.connect() as profiler:
            counter_value = self.wait_for_sprite_counter(profiler, scene_count)
        self.assertEqual(scene_count, counter_value, label)
        observations.append((label, scene_count, counter_value))

    def wait_for_sprite_counter(self, remotery, expected):
        def matching_counter():
            properties = remotery.get_properties(timeout=2.0)
            for entry in properties.find(self.SPRITE_COUNTER_NAME, include_groups=False):
                if entry.name == self.SPRITE_COUNTER_NAME:
                    value = int(entry.value)
                    if value == expected:
                        return value
                    raise AssertionError(f"{entry.path}={value}, expected {expected}")
            raise AssertionError(f"missing Remotery counter {self.SPRITE_COUNTER_NAME}")

        return wait_until(
            matching_counter,
            timeout=5,
            interval=0.05,
            message=f"Remotery {self.SPRITE_COUNTER_NAME} did not match scene spritec count",
            retry_exceptions=(AssertionError,),
        )

    def run_automation_bridge_api_end_to_end(self):
        health = self.bridge.health()
        self.assertEqual("1", health["version"])
        self.assertEqual("1.3.0", health["native_version"])
        self.assertIn("scene", health["capabilities"])
        self.assertIn("input.click", health["capabilities"])
        self.assertIn("scene.pagination", health["capabilities"])
        self.assertGreaterEqual(health["engine_frame"], 0)
        self.assertEqual("1", health["capability_versions"]["scene"])
        self.assertTrue(health["identity"]["engine_instance_id"].startswith("engine:"))
        self.assertTrue(health["identity"]["project_identity"].startswith("project:"))
        self.assertGreater(health["identity"]["start_wall_time_us"], 0)
        self.assertGreater(health["identity"]["start_monotonic_time_us"], 0)
        self.assertEqual("initial_scene_ready", health["lifecycle"]["current_stage"])
        self.assertEqual(health["identity"]["engine_instance_id"], self.bridge.health()["identity"]["engine_instance_id"])

        screen = self.bridge.screen()
        self.assertEqual("top-left", screen["coordinates"]["origin"])
        self.assertEqual("window", screen["coordinates"]["input_space"])
        self.assertGreater(screen["window"]["width"], 0)
        self.assertGreater(screen["window"]["height"], 0)
        self.assertEqual((960, 640), (screen["display"]["width"], screen["display"]["height"]))
        self.assertGreater(screen["display_scale"], 0)

        viewport_center = self.bridge.convert_point(
            (0.5, 0.5), from_space="normalized_viewport", to_space="viewport"
        )
        self.assertAlmostEqual(screen["viewport"]["width"] / 2, viewport_center["x"], delta=1)
        self.assertAlmostEqual(screen["viewport"]["height"] / 2, viewport_center["y"], delta=1)

        scene = self.bridge.scene(visible=True, include=["basic", "bounds", "properties"])
        self.assertEqual("main", scene["root"]["name"])
        self.assertGreater(scene["count"], 0)
        self.assertGreater(scene["scene_sequence"], 0)
        self.assertIn("snapshot_id", scene["root"])

        spawner = self.bridge.element(
            type="goc",
            name_exact="/spawner",
            visible=True,
            include=["basic", "bounds", "properties"],
            limit=20,
        )
        spawner_detail = self.bridge.element_by_id(spawner.id)
        self.assertEqual("/spawner", spawner_detail.name)
        self.assertGreaterEqual(len(spawner_detail.children), 2)
        self.assertIsNotNone(spawner_detail.instance_id)
        self.assertIsNotNone(spawner_detail.instance_generation)
        self.assertIsNotNone(spawner_detail.logical_id)

        with self.assertRaises(AutomationBridgeApiError) as stale:
            self.bridge.click(spawner, wait=0, expected_scene_sequence=0)
        self.assertEqual("stale_scene", stale.exception.code)

        self.assert_game_object_bounds_follow_child_component()
        self.assert_spawned_by_node_click(spawner)
        self.assert_spawned_by_coordinate_click(spawner)
        self.assert_drag_merge_by_node_ids("L1", expected_new_level="L2")

        self.bridge.click(spawner)
        self.bridge.click(spawner.center)
        self.assert_drag_merge_by_coordinates("L1", expected_new_level="L2")

        text_key = self.bridge.type_text("hello")
        self.assertEqual("key", text_key["kind"])
        self.assertEqual("accepted", text_key["state"])

        special_key = self.bridge.key("KEY_ENTER")
        self.assertEqual("key", special_key["kind"])

        self.finish_merge_game()
        gui_nodes = self.bridge.elements(
            type="gui_node",
            limit=100,
            include=["basic", "bounds", "properties"],
        )
        enabled_popup_nodes = {
            node.name
            for node in gui_nodes
            if node.enabled and node.name in {"panel", "restart", "title"}
        }
        self.assertEqual({"panel", "restart", "title"}, enabled_popup_nodes)

        screenshot_path = self.bridge.screenshot(wait=True, timeout=5)
        self.assertEqual("complete", screenshot_path.state)
        self.assertGreater(screenshot_path.stat().st_size, 0)
        self.assertEqual(hashlib.sha256(screenshot_path.read_bytes()).hexdigest(), screenshot_path.sha256)
        self.assertGreater(screenshot_path.frame, 0)
        self.assertGreater(screenshot_path.scene_sequence, 0)

        restart = next(node for node in gui_nodes if node.name == "restart")
        self.bridge.click(restart, wait=0.4)
        self.assertEqual(0, len(self.item_labels()))
        panel = self.bridge.element(name_exact="panel")
        self.assertFalse(panel.enabled)

    def assert_game_object_bounds_follow_child_component(self):
        fixture = self.bridge.element(
            type="goc",
            name_exact="/bounds_fixture",
            visible=True,
            include=["basic", "bounds", "children"],
        )
        sprite_children = [child for child in fixture.children if child.type == "spritec"]
        self.assertEqual(1, len(sprite_children), fixture.compact())

        sprite = sprite_children[0]
        fixture_x = fixture.center["x"]
        fixture_y = fixture.center["y"]
        sprite_x = sprite.center["x"]
        sprite_y = sprite.center["y"]
        distance = ((fixture_x - sprite_x) ** 2 + (fixture_y - sprite_y) ** 2) ** 0.5
        self.assertLessEqual(distance, 2.0)

        response = self.bridge.click(fixture, wait=0.1)
        self.assertEqual("click", response["kind"])
        self.assertEqual("released", response["state"])
        self.assertEqual("mouse", response["device"])

    def assert_spawned_by_node_click(self, node):
        before = self.label_count("L1")
        self.bridge.click(node)
        self.assertGreater(self.label_count("L1"), before)

    def assert_spawned_by_coordinate_click(self, node):
        before = self.label_count("L1")
        self.bridge.click(node.center)
        self.assertGreater(self.label_count("L1"), before)

    def assert_drag_merge_by_node_ids(self, label, expected_new_level):
        before = self.label_count(label)
        first, second = self.parents_for_label(label)[:2]
        self.bridge.drag(first, second, duration=0.16)
        self.wait_for_merge(label, before, expected_new_level)

    def wait_for_merge(self, label, before, expected_new_level):
        wait_until(
            lambda: self.label_count(label) < before and self.label_count(expected_new_level) >= 1,
            timeout=2,
            message=f"merge did not create {expected_new_level}",
        )
        self.assertLess(self.label_count(label), before)
        self.assertGreaterEqual(self.label_count(expected_new_level), 1)

    def assert_drag_merge_by_coordinates(self, label, expected_new_level):
        before = self.label_count(label)
        first, second = self.parents_for_label(label)[:2]
        self.bridge.drag(first.center, second.center, duration=0.16)
        self.wait_for_merge(label, before, expected_new_level)

    def finish_merge_game(self):
        self.merge_label("L2")

        spawner = self.bridge.element(type="goc", name_exact="/spawner", visible=True)
        for _ in range(4):
            self.bridge.click(spawner)
        self.merge_label("L1")
        self.merge_label("L1")
        self.merge_label("L2")
        self.merge_label("L3")

        self.assertEqual(1, self.label_count("L4"))

    def merge_label(self, label):
        before = self.label_count(label)
        first, second = self.parents_for_label(label)[:2]
        self.bridge.drag(first, second, duration=0.16)
        wait_until(
            lambda: self.label_count(label) < before,
            timeout=2,
            message=f"merge did not consume {label}",
        )

    def reset_if_popup_is_visible(self):
        restart = self.bridge.maybe_element(name_exact="restart")
        if restart and restart.enabled:
            self.bridge.click(restart, wait=0.4)

    def item_labels(self):
        nodes = self.bridge.elements(type="labelc", limit=100)
        return [node.text for node in nodes if node.text != "SPAWN"]

    def label_count(self, label):
        return self.bridge.count(type="labelc", text=label)

    def parents_for_label(self, label):
        def resolve_parents():
            parents = []
            nodes = self.bridge.elements(type="labelc", text=label, limit=100)
            for node in nodes:
                try:
                    parents.append(self.bridge.parent(node))
                except AutomationBridgeApiError as exc:
                    if exc.code != "not_found":
                        raise
                    return None
            return parents if len(parents) >= 2 else None

        parents = wait_until(
            resolve_parents,
            timeout=2,
            message=f"missing pair for {label}: {self.item_labels()}",
        )
        return parents


class _ClientCloseFailure:
    failureException = AssertionError

    def id(self):
        return "close Automation Bridge client"

    def shortDescription(self):
        return "close Automation Bridge client"

    def __str__(self):
        return self.id()


class ClosingTextTestResult(unittest.TextTestResult):
    def stopTestRun(self):
        super().stopTestRun()
        if not self.wasSuccessful():
            return
        try:
            AutomationBridgeApiTest.close_bridge()
        except Exception:  # noqa: BLE001 - surface cleanup failures as unittest errors.
            self.addError(_ClientCloseFailure(), sys.exc_info())


class ClosingTextTestRunner(unittest.TextTestRunner):
    resultclass = ClosingTextTestResult


if __name__ == "__main__":
    unittest.main(testRunner=ClosingTextTestRunner)
