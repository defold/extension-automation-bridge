#!/usr/bin/env python3
import json
import math
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "automation_bridge" / "automation-bridge-python"))

from automation_bridge import engine  # noqa: E402
EngineClient = engine.Client
from automation_bridge.gestures import GestureConstraintError, GestureGenerator  # noqa: E402
VideoRecordingClient = engine.VideoRecordingClient
MetalCaptureClient = engine.MetalCaptureClient
MetalCaptureError = engine.MetalCaptureError
from automation_bridge.trace import TraceError, TraceSession  # noqa: E402


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
    class Bridge:
        def __init__(self, fail_stop=False):
            self.requests = []
            self.traces = []
            self.fail_stop = fail_stop

        def request(self, method, path, *, params=None, json=None):
            self.requests.append((method, path, json))
            if path == "/recording/capabilities":
                return {"backend": "screen_capture_kit", "available": True,
                        "application_window": True, "application_audio": True,
                        "resize_output": True, "frame_rate": True,
                        "containers": ["mp4"], "video_codecs": ["h264"],
                        "minimum_platform_version": "macOS 15.0"}
            if path == "/recording/start":
                return {"path": json["path"], "active": True, "finalized": False,
                        "audio": json.get("audio", True), "width": json.get("width", 800),
                        "height": json.get("height", 600), "fps": json["fps"]}
            if path == "/recording/stop":
                if self.fail_stop:
                    raise RuntimeError("native finalization failed")
                return {"path": "capture.mp4", "active": False, "finalized": True,
                        "audio": True, "width": 320, "height": 240, "fps": 24,
                        "duration_seconds": 1.25}
            raise AssertionError(path)

        def _trace_record(self, kind, payload):
            self.traces.append((kind, payload))

    def test_start_uses_native_endpoint_and_context_finalizes(self):
        with tempfile.TemporaryDirectory() as directory:
            bridge = self.Bridge()
            with VideoRecordingClient(bridge).start(Path(directory) / "capture.mp4", size=(320, 240), fps=24) as recording:
                self.assertTrue(recording.metadata.active)
            self.assertTrue(recording.metadata.finalized)
            self.assertEqual((320, 240, 24), (recording.metadata.width, recording.metadata.height, recording.metadata.fps))
            start = bridge.requests[0]
            self.assertEqual(("POST", "/recording/start"), start[:2])
            self.assertEqual({"width": 320, "height": 240, "fps": 24},
                             {key: start[2][key] for key in ("width", "height", "fps")})
            self.assertNotIn("audio", start[2])
        self.assertEqual(["video_recording_started", "video_recording_stopped"], [kind for kind, _ in bridge.traces])

    def test_capabilities_are_reported_by_engine(self):
        capabilities = VideoRecordingClient(self.Bridge()).capabilities()
        self.assertTrue(capabilities.available)
        self.assertTrue(capabilities.application_audio)
        self.assertEqual(("h264",), capabilities.video_codecs)
        self.assertEqual("macOS 15.0", capabilities.minimum_platform_version)

    def test_explicit_audio_false_is_sent_for_portable_video_only_capture(self):
        bridge = self.Bridge()
        with tempfile.TemporaryDirectory() as directory:
            session = VideoRecordingClient(bridge).start(Path(directory) / "capture.mp4", audio=False)
            self.assertFalse(bridge.requests[0][2]["audio"])
            session.stop()

    def test_original_exception_survives_native_finalization_failure(self):
        bridge = self.Bridge(fail_stop=True)
        with tempfile.TemporaryDirectory() as directory, self.assertRaisesRegex(KeyboardInterrupt, "stop now"):
            with VideoRecordingClient(bridge).start(Path(directory) / "capture.mp4"):
                raise KeyboardInterrupt("stop now")


class MetalCaptureTest(unittest.TestCase):
    class Bridge:
        def __init__(self, states=None):
            self.requests = []
            self.traces = []
            self.states = list(states or [])

        def request(self, method, path, *, params=None, json=None):
            self.requests.append((method, path, params))
            if method == "POST":
                return {
                    "state": "pending",
                    "path": params["path"],
                    "frames": params["frames"],
                    "frames_captured": 0,
                    "stop_requested": False,
                }
            if method == "DELETE":
                return {
                    "state": "canceled",
                    "path": "/tmp/frame.gputrace",
                    "frames": 1,
                    "frames_captured": 0,
                    "stop_requested": False,
                }
            if method == "GET" and self.states:
                return self.states.pop(0)
            raise AssertionError((method, path))

        def _trace_record(self, kind, payload):
            self.traces.append((kind, payload))

    def test_start_uses_metal_endpoint_and_waits_for_complete(self):
        states = [
            {"state": "capturing", "path": "/tmp/frame.gputrace", "frames": 2,
             "frames_captured": 1, "stop_requested": False},
            {"state": "complete", "path": "/tmp/frame.gputrace", "frames": 2,
             "frames_captured": 2, "stop_requested": False},
        ]
        bridge = self.Bridge(states)
        with tempfile.TemporaryDirectory() as directory:
            capture = MetalCaptureClient(bridge).start(
                Path(directory) / "traces" / "frame.gputrace",
                frames=2,
                timeout=1,
            )

        self.assertTrue(capture.complete)
        self.assertEqual(2, capture.frames_captured)
        self.assertEqual(("POST", "/metal"), bridge.requests[0][:2])
        self.assertTrue(bridge.requests[0][2]["path"].endswith("/traces/frame.gputrace"))
        self.assertEqual(2, bridge.requests[0][2]["frames"])
        self.assertEqual(
            ["metal_capture_started", "metal_capture_finished"],
            [kind for kind, _ in bridge.traces],
        )

    def test_failed_capture_raises_native_error(self):
        bridge = self.Bridge([{
            "state": "failed", "path": "/tmp/frame.gputrace", "frames": 1,
            "frames_captured": 0, "stop_requested": False, "error": "capture disabled",
        }])
        with tempfile.TemporaryDirectory() as directory, self.assertRaisesRegex(MetalCaptureError, "capture disabled"):
            MetalCaptureClient(bridge).start(Path(directory) / "frame.gputrace", timeout=1)

    def test_pending_capture_can_be_canceled_without_polling(self):
        bridge = self.Bridge()
        capture = MetalCaptureClient(bridge).stop()
        self.assertEqual("canceled", capture.state)
        self.assertEqual(("DELETE", "/metal"), bridge.requests[0][:2])

    def test_path_and_frame_count_are_validated(self):
        client = MetalCaptureClient(self.Bridge())
        with self.assertRaises(ValueError):
            client.start("frame.trace", wait=False)
        with self.assertRaises(ValueError):
            client.start("frame.gputrace", frames=0, wait=False)


class _TraceClient:
    def __init__(self):
        self._active_traces = []
        self.posts = []

    def health(self):
        return {"version": 1, "engine_instance_id": "engine-1", "capabilities": ["scene", "input.drag"]}

    def screen(self):
        return {"window": {"width": 800, "height": 600}, "viewport": {"x": 0, "y": 0, "width": 800, "height": 600}}

    def scene(self):
        return {"scene_sequence": 7, "engine_frame": 99, "root": {}}

    def screenshot(self, wait=True):
        raise RuntimeError("screenshot unavailable")

    def request(self, method, path, *, params=None, json=None):
        self.posts.append((path, params))
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
            self.assertEqual([("/input/drag", {"duration": 0.5})], client.posts)
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
        client.health = lambda: {"version": 1, "capabilities": ["input.click"]}
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


if __name__ == "__main__":
    unittest.main()
