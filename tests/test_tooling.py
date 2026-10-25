#!/usr/bin/env python3
import ast
import importlib
import inspect
import hashlib
import http.server
import json
import math
import re
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
PYTHON_WRAPPER_ROOT = ROOT / "automation_bridge" / "automation-bridge-python"
PYTHON_PACKAGE_ROOT = PYTHON_WRAPPER_ROOT / "automation_bridge"
sys.path.insert(0, str(PYTHON_WRAPPER_ROOT))

import automation_bridge as automation_bridge_package  # noqa: E402
from automation_bridge import editor, engine  # noqa: E402
EngineClient = engine.Client
from automation_bridge.gestures import GestureConstraintError, GestureGenerator  # noqa: E402
VideoRecordingClient = engine.VideoRecordingClient
from automation_bridge.trace import TraceError, TraceSession  # noqa: E402


_SNAKE_OR_PROSE_NODE_TERM = re.compile(
    r"(?i)(?<![A-Za-z0-9])nodes?(?![A-Za-z0-9])"
)
_CAMEL_CASE_NODE_TERM = re.compile(
    r"(?<=[a-z])Nodes?(?=[A-Z0-9]|$)|(?<![A-Za-z0-9])[Nn]odes?(?=[A-Z])"
)


def _non_gui_node_terms(text):
    """Yield generic node terms, permitting only explicitly GUI-qualified uses."""
    matches = list(_SNAKE_OR_PROSE_NODE_TERM.finditer(text))
    matches.extend(_CAMEL_CASE_NODE_TERM.finditer(text))
    for match in sorted(matches, key=lambda item: (item.start(), item.end())):
        prefix = text[max(0, match.start() - 16) : match.start()]
        if re.search(r"(?i)gui(?:[\s_.:/-]*)$", prefix):
            continue
        yield match


def _public_api_entries():
    """Yield every supported module export and public member defined by its classes."""
    seen = set()
    for module in (editor, engine):
        for export_name in module.__all__:
            value = getattr(module, export_name)
            qualified_name = f"{module.__name__}.{export_name}"
            if qualified_name not in seen:
                seen.add(qualified_name)
                yield qualified_name, value
            if not inspect.isclass(value):
                continue
            for owner in reversed(value.__mro__):
                if not owner.__module__.startswith("automation_bridge"):
                    continue
                for member_name, member in vars(owner).items():
                    if member_name.startswith("_"):
                        continue
                    qualified_member_name = f"{qualified_name}.{member_name}"
                    if qualified_member_name in seen:
                        continue
                    seen.add(qualified_member_name)
                    yield qualified_member_name, member


def _signature_target(value):
    if isinstance(value, property):
        return value.fget
    if isinstance(value, (classmethod, staticmethod)):
        return value.__func__
    return value


class PythonPublicApiContractTest(unittest.TestCase):
    def test_terminology_check_rejects_legacy_names_but_allows_gui_terms(self):
        for text in (
            "node",
            "nodes",
            "_select_nodes",
            "nodeCount",
            "waitForNode",
            "SceneNode",
        ):
            with self.subTest(text=text):
                self.assertIsNotNone(next(_non_gui_node_terms(text), None))
        for text in ("GUI node", "gui_nodes", "GuiNode", "gui.Node", "scene_gui_node"):
            with self.subTest(text=text):
                self.assertIsNone(next(_non_gui_node_terms(text), None))

    def test_every_public_api_uses_element_terminology(self):
        self.assertEqual(["editor", "engine"], automation_bridge_package.__all__)
        entries = list(_public_api_entries())
        names = {name for name, _ in entries}
        self.assertIn("automation_bridge.engine.Client.wait_for_element", names)
        self.assertIn("automation_bridge.engine.Element", names)
        self.assertIn("automation_bridge.editor.Client.update_automation_bridge", names)

        failures = []
        for qualified_name, value in entries:
            fragments = [("name", qualified_name)]
            documentation = inspect.getdoc(value)
            if documentation:
                fragments.append(("documentation", documentation))
            try:
                signature = inspect.signature(_signature_target(value))
            except (TypeError, ValueError):
                pass
            else:
                fragments.append(("signature", str(signature)))
            for source, fragment in fragments:
                if next(_non_gui_node_terms(fragment), None) is not None:
                    failures.append(f"{qualified_name} ({source}): {fragment!r}")

        self.assertFalse(
            failures,
            "generic node terminology leaked into the public Python API:\n"
            + "\n".join(failures),
        )

    def test_python_wrapper_sources_use_element_terminology(self):
        paths = sorted(PYTHON_PACKAGE_ROOT.rglob("*.py"))
        paths.append(PYTHON_WRAPPER_ROOT / "best_practices.py")
        failures = []
        for path in paths:
            for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
                if next(_non_gui_node_terms(line), None) is not None:
                    failures.append(f"{path.relative_to(ROOT)}:{line_number}: {line.strip()}")

        self.assertFalse(
            failures,
            "generic node terminology leaked into the Python wrapper; only explicit "
            "GUI-node terminology is allowed:\n" + "\n".join(failures),
        )

    def test_direct_internal_calls_refer_to_declared_members(self):
        failures = []
        for path in sorted(PYTHON_PACKAGE_ROOT.rglob("*.py")):
            relative_module = path.relative_to(PYTHON_WRAPPER_ROOT).with_suffix("")
            module_parts = [part for part in relative_module.parts if part != "__init__"]
            module = importlib.import_module(".".join(module_parts))
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            for definition in tree.body:
                if not isinstance(definition, ast.ClassDef):
                    continue
                runtime_class = getattr(module, definition.name, None)
                if not inspect.isclass(runtime_class):
                    continue

                assigned_attributes = set()
                for item in ast.walk(definition):
                    targets = []
                    if isinstance(item, ast.Assign):
                        targets = item.targets
                    elif isinstance(item, (ast.AnnAssign, ast.NamedExpr, ast.AugAssign)):
                        targets = [item.target]
                    for target in targets:
                        if (
                            isinstance(target, ast.Attribute)
                            and isinstance(target.value, ast.Name)
                            and target.value.id in {"self", "cls"}
                        ):
                            assigned_attributes.add(target.attr)

                for item in ast.walk(definition):
                    if not (
                        isinstance(item, ast.Call)
                        and isinstance(item.func, ast.Attribute)
                        and isinstance(item.func.value, ast.Name)
                        and item.func.value.id in {"self", "cls"}
                    ):
                        continue
                    member_name = item.func.attr
                    if hasattr(runtime_class, member_name) or member_name in assigned_attributes:
                        continue
                    failures.append(
                        f"{path.relative_to(ROOT)}:{item.lineno}: "
                        f"{definition.name}.{member_name}"
                    )

        self.assertFalse(
            failures,
            "direct self/cls calls refer to undeclared members:\n" + "\n".join(failures),
        )


class BestPracticesReferenceTest(unittest.TestCase):
    def test_reference_compiles_and_is_linked_from_agent_guidance(self):
        reference = ROOT / "automation_bridge/automation-bridge-python/best_practices.py"
        source = reference.read_text(encoding="utf-8")
        compile(source, str(reference), "exec")

        guidance = (
            ROOT / "automation_bridge/automation-bridge-python/AGENTS.md"
        ).read_text(encoding="utf-8")
        self.assertIn("best_practices.py", guidance)
        self.assertIn("if __name__ == \"__main__\":", source)


class GestureGeneratorTest(unittest.TestCase):
    def test_seed_is_deterministic_and_result_is_drag_path_kwargs(self):
        generator = GestureGenerator()
        first = generator.generate_drag((10, 10), (90, 50), seed=42, duration=(0.7, 0.8), control_points=3)
        second = generator.generate_drag((10, 10), (90, 50), seed=42, duration=(0.7, 0.8), control_points=3)

        self.assertEqual(first, second)
        self.assertEqual({"points", "durations", "easing"}, set(first))
        self.assertEqual([10.0, 10.0], first["points"][0])
        self.assertEqual([90.0, 50.0], first["points"][-1])
        self.assertEqual(len(first["points"]) - 1, len(first["durations"]))
        self.assertAlmostEqual(sum(first["durations"]), sum(second["durations"]))

    def test_bounds_velocity_and_acceleration_are_enforced(self):
        gesture = GestureGenerator().generate_drag(
            (20, 50), (80, 50), seed=3, duration=1.0,
            lateral_offset=(-100, 100), control_points=4,
            bounds=(0, 0, 100, 100), max_velocity=100, max_acceleration=500,
        )
        self.assertTrue(all(0 <= x <= 100 and 0 <= y <= 100 for x, y in gesture["points"]))
        velocities = []
        for index, segment_duration in enumerate(gesture["durations"]):
            x1, y1 = gesture["points"][index]
            x2, y2 = gesture["points"][index + 1]
            velocity = ((x2 - x1) / segment_duration, (y2 - y1) / segment_duration)
            velocities.append(velocity)
            self.assertLessEqual(math.hypot(*velocity), 100 + 1e-7)
        for index in range(1, len(velocities)):
            transition = (gesture["durations"][index - 1] + gesture["durations"][index]) / 2
            acceleration = math.dist(velocities[index - 1], velocities[index]) / transition
            self.assertLessEqual(acceleration, 500 + 1e-7)

    def test_impossible_velocity_is_reported(self):
        with self.assertRaises(GestureConstraintError):
            GestureGenerator().generate_drag((0, 0), (100, 0), seed=1, duration=1.0, max_velocity=50)


class VideoRecordingTest(unittest.TestCase):
    """Exercise the native recorder wire contract on a real local HTTP server."""

    def setUp(self):
        self.requests = []
        self.payload = b"\x00\x00\x00\x18ftypmp42" + b"native MP4 fixture" * 8192
        self.download = self.payload
        self.instance = "record-service-1"
        self.operation = 1
        self.state = "idle"
        self.start_pending = 1
        self.file_pending = 1
        self.fail_start = False
        self.fail_stop = False
        self.fail_file = False
        self.fail_delete = False
        self.stop_race = False
        self.missing = False
        self.start_callback = None
        self.capability = {
            "version": 1, "supported": True, "audio": True,
            "backend": "screen_capture_kit", "format": "mp4", "codec": "h264",
            "minimum_platform_version": "macOS 15.0", "unsupported_reason": "", "max_fps": 60,
        }
        self.audio = True
        self.fps = 30
        test = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def log_message(self, *_args):
                pass

            def do_GET(self):
                self.handle_request()

            def do_POST(self):
                self.handle_request()

            def do_DELETE(self):
                self.handle_request()

            def handle_request(self):
                data = self.rfile.read(int(self.headers.get("Content-Length", 0)))
                body = json.loads(data) if data else None
                status, value = test.respond(self.command, self.path, body, self.headers.get("X-Recording-Instance"))
                payload = value if isinstance(value, bytes) else json.dumps(value).encode()
                if status == 204:
                    payload = b""
                self.send_response(status)
                self.send_header("Content-Type", "video/mp4" if isinstance(value, bytes) else "application/json")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True)
        self.thread.start()
        self.addCleanup(self.close_server)
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.output = Path(self.directory.name).resolve() / "capture.mp4"
        self.bridge = EngineClient(self.server.server_port)
        self.trace = mock.patch.object(self.bridge, "_trace_record").start()
        self.addCleanup(mock.patch.stopall)
        mock.patch("automation_bridge.waits.cancellable_sleep").start()
        self.recorder = self.bridge.video_recording

    def close_server(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)

    def receipt(self):
        finalized = self.state == "complete"
        return {
            "instance_id": self.instance, "operation_id": self.operation,
            "state": self.state, "pending": self.state in {"starting", "stopping"},
            "active": self.state == "recording", "finalized": finalized, "supported": True,
            "backend": self.capability["backend"], "format": "mp4", "codec": "h264",
            "audio": self.audio, "width": 800, "height": 600, "fps": self.fps,
            "started_wall_time_us": 10000000, "started_monotonic_time_us": 1000000,
            "stopped_wall_time_us": 11250000 if finalized else 0,
            "stopped_monotonic_time_us": 2250000 if finalized else 0,
            "failure": "permission denied" if self.state == "failed" else "",
            "file": {
                "id": "r1-1-1", "url": "/recording/files/r1-1-1",
                "state": "failed" if self.fail_file else ("complete" if finalized and self.file_pending == 0 else "pending"),
                "size": len(self.payload) if finalized else 0,
                "sha256": hashlib.sha256(self.payload).hexdigest() if finalized else "",
            },
        }

    def respond(self, method, path, body, instance):
        self.requests.append((method, path, body, instance))
        if self.missing:
            return 404, b"No such resource"
        if method == "GET" and path == "/recording/capabilities":
            return 200, dict(self.capability, instance_id=self.instance)
        if instance != self.instance:
            return 410, {"error": {"code": "stale_instance", "message": "recording service restarted"}}
        if method == "POST" and path == "/recording/start":
            self.assertLessEqual(set(body), {"fps", "audio"})
            if body.get("audio") and not self.capability["audio"]:
                return 501, {"error": {"code": "audio_unsupported", "message": "application audio is unavailable"}}
            self.audio = body.get("audio", self.capability["audio"])
            self.fps = body["fps"]
            self.state = "starting"
            if self.start_callback:
                self.start_callback()
            return 202, self.receipt()
        if method == "GET" and path == "/recording/status":
            if self.state == "starting":
                if self.start_pending:
                    self.start_pending -= 1
                else:
                    self.state = "failed" if self.fail_start else "recording"
            elif self.state == "stopping":
                self.state = "complete"
            elif self.state == "complete" and self.file_pending:
                self.file_pending -= 1
            return 200, self.receipt()
        if method == "POST" and path == "/recording/stop":
            self.assertEqual({"operation_id": self.operation}, body)
            if self.fail_stop:
                return 500, {"error": {"code": "stop_failed", "message": "native finalization failed"}}
            if self.stop_race:
                self.state = "complete"
                return 409, {"error": {"code": "recording_inactive", "message": "recording already stopped"}}
            self.state = "stopping"
            return 202, self.receipt()
        if path == "/recording/files/r1-1-1":
            self.assertEqual("complete", self.state)
            self.assertEqual(0, self.file_pending)
            if method == "GET":
                return 200, self.download
            if method == "DELETE":
                if self.fail_delete:
                    return 500, {"error": {"code": "delete_failed", "message": "delete failed"}}
                return 204, None
        self.fail(f"Unexpected recording request: {method} {path}")

    def calls(self, method, path):
        return [request for request in self.requests if request[:2] == (method, path)]

    def test_context_waits_for_capture_and_file_then_verifies_download_and_deletes(self):
        with self.recorder.start(self.output, fps=24) as session:
            self.assertTrue(session.metadata.active)
            self.assertIsNone(session.metadata.path)
            self.assertFalse(self.output.exists())
        self.assertEqual(self.payload, self.output.read_bytes())
        self.assertTrue(session.metadata.finalized)
        self.assertEqual(str(self.output), session.metadata.path)
        self.assertEqual((800, 600, 24), (session.metadata.width, session.metadata.height, session.metadata.fps))
        self.assertEqual(1.25, session.metadata.duration_seconds)
        self.assertEqual({"fps": 24}, self.calls("POST", "/recording/start")[0][2])
        self.assertEqual({"operation_id": 1}, self.calls("POST", "/recording/stop")[0][2])
        for method, path, _, instance in self.requests:
            if path != "/recording/capabilities":
                self.assertEqual(self.instance, instance)
        self.assertEqual(1, len(self.calls("GET", "/recording/files/r1-1-1")))
        self.assertEqual("DELETE", self.requests[-1][0])
        count = len(self.requests)
        self.assertEqual(session.metadata, session.stop())
        self.assertEqual(count, len(self.requests))
        self.assertEqual(["video_recording_started", "video_recording_stopped"], [call.args[0] for call in self.trace.call_args_list])

    def test_capabilities_translate_native_schema_and_do_not_advertise_scaling(self):
        capabilities = self.recorder.capabilities()
        self.assertTrue(capabilities.available)
        self.assertTrue(capabilities.application_audio)
        self.assertFalse(capabilities.resize_output)
        self.assertEqual(("h264",), capabilities.video_codecs)
        self.assertEqual("macOS 15.0", capabilities.minimum_platform_version)
        self.assertEqual((1, self.instance, 60), (capabilities.version, capabilities.instance_id, capabilities.max_fps))

    def test_audio_false_and_linux_video_only_default(self):
        for options, supported in (({"audio": False}, True), ({}, False)):
            with self.subTest(options=options):
                self.capability["audio"] = supported
                with self.recorder.start(self.output, **options) as session:
                    self.assertFalse(session.metadata.audio)
                self.assertEqual(options.get("audio"), self.calls("POST", "/recording/start")[-1][2].get("audio"))

    def test_unsupported_audio_preserves_native_error(self):
        self.capability["audio"] = False
        with self.assertRaises(engine.VideoRecordingError) as error:
            self.recorder.start(self.output, audio=True)
        self.assertEqual((501, "audio_unsupported"), (error.exception.status, error.exception.code))

    def test_missing_service_is_optional_but_start_reports_minimum_version(self):
        self.missing = True
        self.assertFalse(self.recorder.capabilities().available)
        with self.assertRaisesRegex(engine.VideoRecordingError, "Defold 1.14.1"):
            self.recorder.start(self.output)
        self.assertFalse(self.calls("POST", "/recording/start"))

    def test_unsupported_backend_reports_its_reason(self):
        self.capability.update(supported=False, unsupported_reason="unsupported OS version")
        self.assertFalse(self.recorder.capabilities().available)
        with self.assertRaisesRegex(engine.VideoRecordingError, "unsupported OS version"):
            self.recorder.start(self.output)
        self.assertFalse(self.calls("POST", "/recording/start"))

    def test_incompatible_protocol_is_rejected_before_start(self):
        for version in (0, 2, True):
            with self.subTest(version=version):
                self.capability["version"] = version
                with self.assertRaisesRegex(engine.VideoRecordingError, "protocol"):
                    self.recorder.start(self.output)
        self.assertFalse(self.calls("POST", "/recording/start"))

    def test_invalid_options_fail_before_requests(self):
        for options in ({"fps": True}, {"fps": 0}, {"fps": 61}, {"fps": 2.5}, {"audio": 1},
                        {"timeout": -1}, {"timeout": float("nan")}, {"size": (320, 240)}):
            with self.subTest(options=options), self.assertRaises((TypeError, ValueError)):
                self.recorder.start(self.output, **options)
        self.assertEqual([], self.requests)

    def test_native_startup_failure_is_not_reported_as_active(self):
        self.fail_start = True
        with self.assertRaisesRegex(engine.VideoRecordingError, "permission denied"):
            self.recorder.start(self.output)
        self.assertFalse(self.output.exists())
        self.assertFalse(self.calls("GET", "/recording/files/r1-1-1"))

    def test_startup_timeout_cleans_up_the_accepted_operation(self):
        self.file_pending = 0
        with self.assertRaises(engine.WaitTimeoutError) as error:
            self.recorder.start(self.output, timeout=0)
        self.assertEqual(1, len(self.calls("POST", "/recording/stop")))
        self.assertTrue(error.exception.recording_session.metadata.finalized)
        self.assertEqual(self.payload, self.output.read_bytes())

    def test_cancelled_startup_still_finalizes_the_accepted_operation(self):
        token = engine.CancellationToken()
        self.start_callback = lambda: token.cancel("cancel capture")
        with self.assertRaisesRegex(engine.OperationCancelled, "cancel capture") as error:
            with engine.cancellation_scope(token):
                self.recorder.start(self.output)
        self.assertIsNone(error.exception.cleanup_error)
        self.assertEqual(self.payload, self.output.read_bytes())

    def test_context_preserves_original_exception_when_native_stop_fails(self):
        self.fail_stop = True
        with self.assertRaisesRegex(KeyboardInterrupt, "stop now") as error:
            with self.recorder.start(self.output):
                raise KeyboardInterrupt("stop now")
        self.assertIsInstance(error.exception.cleanup_error, engine.VideoRecordingError)
        self.assertFalse(self.output.exists())

    def test_restart_is_not_rebound_even_after_new_capability_discovery(self):
        session = self.recorder.start(self.output)
        original = self.instance
        self.instance = "replacement-service"
        self.assertEqual(self.instance, self.recorder.capabilities().instance_id)
        with self.assertRaises(engine.VideoRecordingError) as error:
            session.stop()
        self.assertEqual("stale_instance", error.exception.code)
        self.assertEqual(original, self.requests[-1][3])
        self.assertFalse(self.calls("POST", "/recording/stop"))

    def test_replaced_operation_is_not_stopped(self):
        session = self.recorder.start(self.output)
        self.operation += 1
        with self.assertRaises(engine.VideoRecordingError) as error:
            session.stop()
        self.assertEqual("stale_operation", error.exception.code)
        self.assertFalse(self.calls("POST", "/recording/stop"))

    def test_automatic_resize_finalization_is_downloaded_without_stop(self):
        session = self.recorder.start(self.output)
        self.state = "complete"
        session.stop()
        self.assertEqual(self.payload, self.output.read_bytes())
        self.assertFalse(self.calls("POST", "/recording/stop"))

    def test_resize_between_status_and_stop_is_handled(self):
        session = self.recorder.start(self.output)
        self.stop_race = True
        session.stop()
        self.assertEqual(self.payload, self.output.read_bytes())

    def test_file_preparation_failure_preserves_destination(self):
        session = self.recorder.start(self.output)
        self.fail_file = True
        self.output.write_bytes(b"previous capture")
        with self.assertRaisesRegex(engine.VideoRecordingError, "preparation failed"):
            session.stop()
        self.assertEqual(b"previous capture", self.output.read_bytes())
        self.assertFalse(self.calls("GET", "/recording/files/r1-1-1"))

    def test_corrupt_download_preserves_destination_and_can_be_retried(self):
        for download in (b"truncated", b"x" * len(self.payload), self.payload + b"extra"):
            with self.subTest(size=len(download)):
                session = self.recorder.start(self.output)
                self.output.write_bytes(b"previous capture")
                self.download = download
                deletes = len(self.calls("DELETE", "/recording/files/r1-1-1"))
                with self.assertRaisesRegex(engine.VideoRecordingError, "receipt"):
                    session.stop()
                self.assertEqual(b"previous capture", self.output.read_bytes())
                self.assertEqual([], list(self.output.parent.glob("*.part")))
                self.assertEqual(deletes, len(self.calls("DELETE", "/recording/files/r1-1-1")))
                self.download = self.payload
                session.stop()
                self.assertEqual(self.payload, self.output.read_bytes())

    def test_failed_engine_file_cleanup_retries_without_downloading_again(self):
        session = self.recorder.start(self.output)
        self.fail_delete = True
        with self.assertRaisesRegex(engine.VideoRecordingError, "delete failed"):
            session.stop()
        self.assertEqual(self.payload, self.output.read_bytes())
        self.fail_delete = False
        session.stop()
        self.assertEqual(1, len(self.calls("GET", "/recording/files/r1-1-1")))

    def test_finalization_timeout_can_be_retried(self):
        session = self.recorder.start(self.output)
        with self.assertRaises(engine.WaitTimeoutError):
            session.stop(timeout=0)
        self.assertFalse(self.output.exists())
        session.stop()
        self.assertEqual(self.payload, self.output.read_bytes())
        self.assertEqual(1, len(self.calls("POST", "/recording/stop")))


class _TraceClient:
    def __init__(self):
        self._active_traces = []
        self.posts = []

    def health(self):
        return {"version": 2, "engine_instance_id": "engine-1", "capabilities": ["scene", "input.drag"]}

    def screen(self):
        return {"window": {"width": 800, "height": 600}, "viewport": {"x": 0, "y": 0, "width": 800, "height": 600}}

    def scene(self):
        return {"scene_sequence": 7, "engine_frame": 99, "root": {}}

    def screenshot(self, wait=True):
        raise RuntimeError("screenshot unavailable")

    def request(self, method, path, *, params=None, json_body=None):
        self.posts.append((path, params, json_body))
        return {"accepted": True}


class TraceTest(unittest.TestCase):
    def test_bundle_captures_initial_data_timeline_cleanup_and_replays_actions(self):
        client = _TraceClient()
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "session.trace.json"
            with TraceSession(client, path, screenshots="never", prerequisites={"random_seeds": [42]}) as trace:
                trace.record("generated_path", {"seed": 42, "points": [[0, 0], [10, 10]]})
                trace.record("action", {
                    "method": "POST", "path": "/input/drag", "params": {"duration": 0.5},
                    "response": {"input_id": 8, "engine_frame": 100, "scene_sequence": 7},
                })
                trace.record_event({"name": "complete", "scene_sequence": 8})
                trace.record_state("ui", 3, {"busy": False})
                trace.record_input_acknowledgement({"input_id": 8, "accepted": True})
                trace.record_profiler({"first_frame": 100, "last_frame": 110})
                trace.record_selector_error({"name_exact": "missing"}, RuntimeError("no match"))

            data = json.loads(path.read_text())
            self.assertEqual("best-effort", data["replay"]["mode"])
            self.assertEqual("engine-1", data["engine_instance_id"])
            self.assertEqual(7, data["initial"]["scene"]["scene_sequence"])
            self.assertIn("trace_detach", [item["operation"] for item in data["cleanup"]])
            self.assertIn("application_event", [item["kind"] for item in data["timeline"]])
            action = next(item for item in data["timeline"] if item["kind"] == "action")
            self.assertEqual(100, action["engine_frame"])
            self.assertEqual(7, action["scene_sequence"])
            self.assertEqual(8, action["input_id"])
            self.assertIn("selector_error", [item["kind"] for item in data["timeline"]])

            report = TraceSession.replay(client, path)
            self.assertEqual(1, report["replayed"])
            self.assertEqual([("/input/drag", None, {"duration": 0.5})], client.posts)
            self.assertIn("application_state", report["missing_prerequisites"])
            with self.assertRaises(TraceError):
                TraceSession.replay(client, path, require_deterministic=True)

    def test_trace_does_not_mask_keyboard_interrupt_when_screenshot_fails(self):
        client = _TraceClient()
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "interrupt.trace.json"
            with self.assertRaises(KeyboardInterrupt):
                with TraceSession(client, path, screenshots="on_error"):
                    raise KeyboardInterrupt()
            data = json.loads(path.read_text())
            self.assertEqual("interruption", data["timeline"][-1]["kind"])
            self.assertIn("capture_error", data["screenshots"][0])


class ClientToolingTest(unittest.TestCase):
    def test_active_trace_automatically_records_input_request_and_receipt(self):
        client = EngineClient(1234)
        client.health = lambda: {"version": 2, "capabilities": ["input.click"]}
        client.screen = lambda: {"window": {"width": 100, "height": 100}}
        client.scene = lambda: {"scene_sequence": 4}
        response = {"ok": True, "data": {"input_id": 12, "engine_frame": 90, "state": "accepted"}}
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "automatic.trace.json"
            with mock.patch("automation_bridge.client.request_json", return_value=(200, response)):
                with client.trace(path, screenshots="never"):
                    client.click((10, 20), wait=0)
            data = json.loads(path.read_text())
            action = next(item for item in data["timeline"] if item["kind"] == "action")
            self.assertEqual("/input/click", action["payload"]["path"])
            self.assertEqual(12, action["input_id"])
            self.assertEqual(90, action["engine_frame"])

            replay_client = _TraceClient()
            report = TraceSession.replay(replay_client, path)
            self.assertEqual(1, report["replayed"])
            self.assertEqual("/input/click", replay_client.posts[0][0])
            self.assertEqual({}, replay_client.posts[0][1])
            replayed_json = replay_client.posts[0][2]
            self.assertEqual((10, 20), (replayed_json["x"], replayed_json["y"]))


if __name__ == "__main__":
    unittest.main()
