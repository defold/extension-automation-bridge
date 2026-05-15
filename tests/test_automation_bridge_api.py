#!/usr/bin/env python3
import base64
import hashlib
import os
import socket
import struct
import sys
import threading
import time
import unittest
import zlib
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "automation_bridge" / "automation-bridge-python"))

from automation_bridge import (  # noqa: E402
    AutomationBridgeApiError,
    AutomationBridgeClient,
    AutomationBridgeError,
    EditorClient,
    EngineLogStream,
    ProfilerClient,
    ProfilerDataError,
    RemoteryCapture,
    RemoteryClient,
    RemoteryProtocolError,
    wait_until,
)
from automation_bridge.client import _encode_system_reboot  # noqa: E402
from automation_bridge.profiler import parse_resources_data  # noqa: E402
from automation_bridge.remotery import (  # noqa: E402
    build_message,
    build_sample_name,
    parse_property_frame,
    parse_sample_frame,
)


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


def _count_orange_debug_pixels(path):
    _, _, pixels = _read_automation_bridge_png(path)
    count = 0
    for i in range(0, len(pixels), 4):
        r, g, b, a = pixels[i], pixels[i + 1], pixels[i + 2], pixels[i + 3]
        if a > 0 and r >= 150 and 80 <= g <= 230 and b <= 120 and r > g + 20 and g > b + 25:
            count += 1
    return count


class AutomationBridgeClientUnitTest(unittest.TestCase):
    def test_wait_for_count_accepts_zero(self):
        class FakeAutomationBridge:
            def count(self, **selector):
                return 0

        self.assertEqual(0, AutomationBridgeClient.wait_for_count(FakeAutomationBridge(), 0, timeout=0.1, interval=0.01))

    def test_system_reboot_payload(self):
        self.assertEqual(b"\x0a\x01a\x12\x02bc", _encode_system_reboot(("a", "bc")))

    def test_system_reboot_rejects_too_many_args(self):
        with self.assertRaises(ValueError):
            _encode_system_reboot(("1", "2", "3", "4", "5", "6", "7"))

    def test_resize_remembers_window_size(self):
        bridge = FakeEngineClient()

        result = bridge.resize(640, 480, wait=0)

        self.assertEqual({"width": 640, "height": 480}, result)
        self.assertEqual((640, 480), bridge.last_window_size)
        self.assertEqual(
            [("PUT", "/screen", {"width": 640, "height": 480})],
            bridge.api_requests,
        )

    def test_orientation_helpers_swap_last_known_size(self):
        bridge = FakeEngineClient({"window": {"width": 320, "height": 568}})

        landscape = bridge.set_landscape(wait=0)
        portrait = bridge.set_portrait(wait=0)

        self.assertEqual({"width": 568, "height": 320}, landscape)
        self.assertEqual({"width": 320, "height": 568}, portrait)
        self.assertEqual((320, 568), bridge.last_window_size)
        self.assertEqual(
            [
                ("GET", "/screen", None),
                ("PUT", "/screen", {"width": 568, "height": 320}),
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

    def test_fresh_build_ignores_cached_remotery_url(self):
        class FakeEditor:
            def latest_registration_remotery_urls(self):
                return []

            def remotery_url(self):
                return "ws://127.0.0.1:17815/rmt"

        self.assertIsNone(AutomationBridgeClient._editor_remotery_url(FakeEditor(), fresh_build=True))
        self.assertEqual(
            "ws://127.0.0.1:17815/rmt",
            AutomationBridgeClient._editor_remotery_url(FakeEditor(), fresh_build=False),
        )

    def test_engine_log_stream_reads_lines(self):
        with FakeLogServer([b"0 OK\n", b"INFO:TEST: hello\n", b"WARNING:TEST: done\n"]) as server:
            with EngineLogStream("127.0.0.1", server.port, timeout=1.0, read_timeout=1.0) as logs:
                self.assertEqual("INFO:TEST: hello", logs.readline(timeout=1.0))
                self.assertEqual("WARNING:TEST: done", logs.readline(timeout=1.0))

    def test_read_logs_collects_future_lines(self):
        bridge = FakeEngineClient()
        with FakeLogServer([b"0 OK\n", b"INFO:TEST: one\n", b"INFO:TEST: two\n"]) as server:
            bridge._engine_info = {"log_port": str(server.port)}

            self.assertEqual(
                ["INFO:TEST: one", "INFO:TEST: two"],
                bridge.read_logs(duration=1.0, limit=2, idle_timeout=0.05),
            )

    def test_profiler_accessor_uses_engine_port(self):
        bridge = AutomationBridgeClient(12345, timeout=3.0, remotery_url="ws://127.0.0.1:17816/rmt")

        profiler = bridge.profiler

        self.assertIsInstance(profiler, ProfilerClient)
        self.assertEqual(12345, profiler.port)
        self.assertEqual(3.0, profiler.timeout)
        self.assertEqual("ws://127.0.0.1:17816/rmt", profiler.remotery_url)

        remotery = profiler.remotery()

        self.assertEqual("127.0.0.1", remotery.host)
        self.assertEqual(17816, remotery.port)
        self.assertEqual("/rmt", remotery.path)

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

    def test_profiler_remotery_accessor_uses_remotery_port(self):
        profiler = ProfilerClient(12345, timeout=3.0)

        remotery = profiler.remotery(port=23456)

        self.assertIsInstance(remotery, RemoteryClient)
        self.assertEqual(23456, remotery.port)
        self.assertEqual(3.0, remotery.timeout)

    def test_editor_latest_registration_remotery_urls(self):
        lines = [
            "INFO:ENGINE: Initialized Remotery (ws://127.0.0.1:11111/rmt)",
            "INFO:ENGINE: Automation Bridge endpoint registered",
            "INFO:ENGINE: Initialized Remotery (ws://127.0.0.1:22222/rmt)",
            "INFO:ENGINE: Engine service started on port 33333",
            "INFO:ENGINE: Automation Bridge endpoint registered",
        ]

        self.assertEqual(["ws://127.0.0.1:22222/rmt"], EditorClient._latest_registration_remotery_urls(lines))

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
        capture = RemoteryCapture(frames=(first, second))

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
        capture = RemoteryCapture(frames=(), property_frames=(first, second))

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

        with self.assertRaisesRegex(RemoteryProtocolError, "invalid integer value"):
            parse_property_frame(body, {202: "Fractional"})

    def test_remotery_client_get_frame_resolves_sample_names(self):
        sample_message = build_message("SMPL", _remotery_sample_frame_body())
        with FakeRemoteryServer(sample_message, {1: "Frame", 2: "Update"}) as server:
            with RemoteryClient(port=server.port, timeout=1.0) as remotery:
                frame = remotery.get_frame(timeout=1.0)

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
            profiler = ProfilerClient(12345, timeout=1.0, remotery_url=f"ws://127.0.0.1:{server.port}/rmt")
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


class FakeEngineClient(AutomationBridgeClient):
    def __init__(self, screen=None):
        super().__init__(12345)
        self.engine_posts = []
        self.api_requests = []
        self._screen = screen or {"window": {"width": 800, "height": 600}}
        self._engine_info = {"log_port": "0"}

    def _request(self, method, path, params=None):
        self.api_requests.append((method, path, dict(params) if params is not None else None))
        if method == "GET" and path == "/screen":
            return self._screen
        if method == "PUT" and path == "/screen":
            self._screen = {
                **self._screen,
                "window": {"width": int(params["width"]), "height": int(params["height"])},
            }
            return self._screen
        raise AssertionError(f"unexpected request: {method} {path} {params}")

    def _post_engine_message(self, path, payload, timeout=None):
        self.engine_posts.append((path, payload, timeout))
        return b"OK"

    def engine_info(self):
        return self._engine_info


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
                request = client.recv(4096).decode("iso-8859-1", "replace")
                self.request_line = request.splitlines()[0] if request else ""
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
            cls.bridge = AutomationBridgeClient(int(port), remotery_url=remotery_url)
            cls.bridge.wait_ready()
            return

        try:
            cls.editor = EditorClient.from_project(ROOT)
        except FileNotFoundError as exc:
            raise unittest.SkipTest(str(exc)) from exc

    @classmethod
    def close_bridge(cls):
        if cls.bridge is None:
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
                self.bridge = AutomationBridgeClient.from_editor(self.editor, build=True, timeout=20)
                self.__class__.bridge = self.bridge
                self.bridge.wait_ready()
                if previous_port is not None:
                    self.assertNotEqual(previous_port, self.bridge.port)
                previous_port = self.bridge.port

            with self.subTest(run=run_index + 1):
                if run_index == 1 or (run_index == 0 and self.editor is None):
                    resized = self.bridge.resize(800, 600, wait=0.3)
                    self.assertEqual({"width": 800, "height": 600}, resized)
                    portrait = self.bridge.set_portrait(wait=0.3)
                    self.assertEqual({"width": 600, "height": 800}, portrait)
                self.reset_if_popup_is_visible()
                self.run_automation_bridge_api_end_to_end()

    def test_drag_negative_duration_is_clamped_in_response(self):
        self.ensure_running_bridge()
        self.reset_if_popup_is_visible()
        spawner = self.bridge.node(type="goc", name_exact="/spawner", visible=True)

        start_count = self.label_count("L1")
        self.bridge.click(spawner)
        wait_until(lambda: self.label_count("L1") > start_count, timeout=2, message="first negative-drag item missing")
        self.bridge.click(spawner.center)
        wait_until(lambda: self.label_count("L1") > start_count + 1, timeout=2, message="second negative-drag item missing")

        before = self.label_count("L1")
        first, second = self.parents_for_label("L1")[:2]

        response = self.bridge.drag(first, second, duration=-0.25, wait=0.2)

        self.assertEqual("drag", response["queued"])
        self.assertEqual(0, response["duration"])
        self.wait_for_merge("L1", before, "L2")

    def test_drag_visualization_is_visible_in_screenshot(self):
        self.ensure_running_bridge()
        self.reset_if_popup_is_visible()
        screen = self.bridge.screen()
        window = screen["window"]
        start = (window["width"] * 0.25, window["height"] * 0.35)
        end = (window["width"] * 0.75, window["height"] * 0.65)

        self.bridge.drag(start, end, duration=1.0, wait=0)
        time.sleep(0.15)
        screenshot_path = self.bridge.screenshot(wait=True, timeout=5)
        orange_pixels = _count_orange_debug_pixels(screenshot_path)
        print(f"DRAG_VISUALIZATION_SCREENSHOT {screenshot_path} orange_pixels={orange_pixels}")

        try:
            self.assertGreater(orange_pixels, 20)
        finally:
            time.sleep(1.0)

    def test_remotery_sprite_counter_after_actions(self):
        self.ensure_running_bridge()
        if self.editor is None and self.bridge.remotery_url is None:
            raise unittest.SkipTest("set AUTOMATION_BRIDGE_REMOTERY_URL or AUTOMATION_BRIDGE_REMOTERY_PORT for Remotery tests")
        self.reset_if_popup_is_visible()
        spawner = self.bridge.node(type="goc", name_exact="/spawner", visible=True)
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

        spawner = self.bridge.node(type="goc", name_exact="/spawner", visible=True)
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

        restart = self.bridge.node(name_exact="restart", enabled=True)
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
        self.bridge = AutomationBridgeClient.from_editor(self.editor, build=True, timeout=20)
        self.__class__.bridge = self.bridge
        self.bridge.wait_ready()

    def record_sprite_counter(self, observations, label):
        scene_count = self.bridge.count(type="spritec")
        with self.bridge.profiler.remotery() as remotery:
            counter_value = self.wait_for_sprite_counter(remotery, scene_count)
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
        )

    def run_automation_bridge_api_end_to_end(self):
        health = self.bridge.health()
        self.assertEqual("1", health["version"])
        self.assertIn("scene", health["capabilities"])
        self.assertIn("input.click", health["capabilities"])

        screen = self.bridge.screen()
        self.assertEqual("top-left", screen["coordinates"]["origin"])
        self.assertGreater(screen["window"]["width"], 0)
        self.assertGreater(screen["window"]["height"], 0)
        self.assertEqual({"width": 960, "height": 640}, screen["display"])

        scene = self.bridge.scene(visible=True, include=["basic", "bounds", "properties"])
        self.assertEqual("main", scene["root"]["name"])
        self.assertGreater(scene["count"], 0)

        spawner = self.bridge.node(
            type="goc",
            name_exact="/spawner",
            visible=True,
            include=["basic", "bounds", "properties"],
            limit=20,
        )
        spawner_detail = self.bridge.by_id(spawner.id)
        self.assertEqual("/spawner", spawner_detail.name)
        self.assertGreaterEqual(len(spawner_detail.children), 2)

        self.assert_spawned_by_node_click(spawner)
        self.assert_spawned_by_coordinate_click(spawner)
        self.assert_drag_merge_by_node_ids("L1", expected_new_level="L2")

        self.bridge.click(spawner)
        self.bridge.click(spawner.center)
        self.assert_drag_merge_by_coordinates("L1", expected_new_level="L2")

        text_key = self.bridge.type_text("hello")
        self.assertEqual("key", text_key["queued"])
        self.assertEqual(5, text_key["length"])

        special_key = self.bridge.key("KEY_ENTER")
        self.assertEqual("key", special_key["queued"])

        self.finish_merge_game()
        gui_nodes = self.bridge.nodes(
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
        self.assertGreater(screenshot_path.stat().st_size, 0)

        restart = next(node for node in gui_nodes if node.name == "restart")
        self.bridge.click(restart, wait=0.4)
        self.assertEqual(0, len(self.item_labels()))
        panel = self.bridge.node(name_exact="panel")
        self.assertFalse(panel.enabled)

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

        spawner = self.bridge.node(type="goc", name_exact="/spawner", visible=True)
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
        restart = self.bridge.maybe_node(name_exact="restart")
        if restart and restart.enabled:
            self.bridge.click(restart, wait=0.4)

    def item_labels(self):
        nodes = self.bridge.nodes(type="labelc", limit=100)
        return [node.text for node in nodes if node.text != "SPAWN"]

    def label_count(self, label):
        return self.bridge.count(type="labelc", text=label)

    def parents_for_label(self, label):
        def resolve_parents():
            parents = []
            nodes = self.bridge.nodes(type="labelc", text=label, limit=100)
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
