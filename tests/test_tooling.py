#!/usr/bin/env python3
import io
import json
import math
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "automation_bridge" / "automation-bridge-python"))

from automation_bridge import AutomationBridgeClient  # noqa: E402
from automation_bridge.gestures import GestureConstraintError, GestureGenerator  # noqa: E402
from automation_bridge.recording import (  # noqa: E402
    FFmpegRecordingBackend,
    RecordingOptions,
    UnsupportedRecordingCapability,
)
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


class _FakeProcess:
    def __init__(self):
        self.stdin = io.BytesIO()
        self.returncode = None

    def poll(self):
        return self.returncode

    def wait(self, timeout=None):
        self.returncode = 0
        return 0

    def terminate(self):
        self.returncode = 0

    def kill(self):
        self.returncode = 0


class RecordingTest(unittest.TestCase):
    def test_linux_command_supports_content_rect_output_size_fps_and_codec(self):
        backend = FFmpegRecordingBackend(ffmpeg=sys.executable, platform="linux", display=":9", video_codecs=("libx264",))
        options = RecordingOptions(
            path=Path("capture.mp4"), source_rect=(10, 20, 640, 480), crop="content",
            size=(320, 240), fps=24, video_codec="libx264",
        )
        command = backend.command(options)
        joined = " ".join(command)
        self.assertIn("-video_size 640x480", joined)
        self.assertIn(":9+10,20", joined)
        self.assertIn("scale=320:240", joined)
        self.assertIn("-r 24", joined)
        self.assertIn("-c:v libx264", joined)

    def test_application_audio_fails_instead_of_recording_wrong_source(self):
        backend = FFmpegRecordingBackend(ffmpeg=sys.executable, platform="linux", display=":9", video_codecs=("libx264",))
        with self.assertRaisesRegex(UnsupportedRecordingCapability, "isolate application audio"):
            backend.command(RecordingOptions(path=Path("capture.mp4"), audio="application"))

    def test_content_crop_requires_global_content_geometry(self):
        backend = FFmpegRecordingBackend(ffmpeg=sys.executable, platform="linux", display=":9", video_codecs=("libx264",))
        with self.assertRaisesRegex(UnsupportedRecordingCapability, "content geometry"):
            backend.command(RecordingOptions(path=Path("capture.mp4"), crop="content"))

    def test_context_finalizes_and_returns_observed_metadata(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "capture.mp4"
            output.write_bytes(b"video")
            backend = FFmpegRecordingBackend(
                ffmpeg=sys.executable, platform="linux", display=":9", video_codecs=("libx264",), popen=lambda *args, **kwargs: _FakeProcess()
            )
            with backend.start(RecordingOptions(path=output, size=(320, 240), fps=30)) as recording:
                pass
            self.assertTrue(recording.metadata.finalized)
            self.assertEqual(320, recording.metadata.width)
            self.assertEqual(240, recording.metadata.height)
            self.assertIn(b"q\n", recording.process.stdin.getvalue())

    def test_original_exception_survives_finalization_failure(self):
        with tempfile.TemporaryDirectory() as directory:
            backend = FFmpegRecordingBackend(
                ffmpeg=sys.executable, platform="linux", display=":9", video_codecs=("libx264",), popen=lambda *args, **kwargs: _FakeProcess()
            )
            with self.assertRaisesRegex(KeyboardInterrupt, "stop now"):
                with backend.start(RecordingOptions(path=Path(directory) / "missing.mp4")):
                    raise KeyboardInterrupt("stop now")


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
                trace.record_acknowledgement({"input_id": 8, "accepted": True})
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


class _CapturingBackend:
    def __init__(self):
        self.options = None

    def start(self, options):
        self.options = options
        return "session"


class ClientToolingTest(unittest.TestCase):
    def test_record_video_resolves_content_geometry(self):
        backend = _CapturingBackend()
        client = AutomationBridgeClient(1)
        client.screen = lambda: {"rectangles": {"content_display_pixels": {"x": 3, "y": 4, "width": 640, "height": 480}}}
        session = client.recording.start("capture.mp4", crop="content", backend=backend)
        self.assertEqual("session", session)
        self.assertEqual((3, 4, 640, 480), backend.options.source_rect)

    def test_record_video_does_not_treat_local_viewport_as_global_crop(self):
        backend = _CapturingBackend()
        client = AutomationBridgeClient(1)
        client.screen = lambda: {"viewport": {"x": 3, "y": 4, "width": 640, "height": 480}}
        client.recording.start("capture.mp4", crop="content", backend=backend)
        self.assertIsNone(backend.options.source_rect)

    def test_active_trace_automatically_records_input_request_and_receipt(self):
        client = AutomationBridgeClient(1234)
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
