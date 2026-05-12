#!/usr/bin/env python3
import json
import os
import re
import time
import unittest
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class HttpError(RuntimeError):
    pass


def request_json(url, method="GET", timeout=10):
    request = urllib.request.Request(url, method=method)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            data = response.read().decode("utf-8")
    except urllib.error.URLError as exc:
        raise HttpError(f"{method} {url} failed: {exc}") from exc
    return json.loads(data)


def wait_until(fn, timeout=10, interval=0.1):
    deadline = time.monotonic() + timeout
    last_error = None
    while time.monotonic() < deadline:
        try:
            value = fn()
            if value:
                return value
        except Exception as exc:  # noqa: BLE001 - preserve last polling failure.
            last_error = exc
        time.sleep(interval)
    if last_error:
        raise AssertionError(f"condition not met before timeout: {last_error}") from last_error
    raise AssertionError("condition not met before timeout")


class EditorClient:
    def __init__(self, root):
        self.root = root
        port_path = root / ".internal" / "editor.port"
        if not port_path.exists():
            raise unittest.SkipTest(f"Defold editor port file is missing: {port_path}")
        self.base_url = f"http://localhost:{port_path.read_text().strip()}"

    def build(self):
        response = request_json(f"{self.base_url}/command/build", method="POST", timeout=60)
        if not response.get("success"):
            self.fail_build(response)

    @staticmethod
    def fail_build(response):
        issues = response.get("issues", [])
        raise AssertionError(f"Defold build failed: {issues}")

    def console_lines(self):
        response = request_json(f"{self.base_url}/console", timeout=10)
        return response.get("lines", [])

    def engine_service_port(self):
        pattern = re.compile(r"Engine service started on port (\d+)")
        for line in reversed(self.console_lines()):
            match = pattern.search(line)
            if match:
                return int(match.group(1))
        return None


class AgentClient:
    def __init__(self, port):
        self.base_url = f"http://127.0.0.1:{port}/agent/v1"

    def get(self, path, params=None):
        return self._request("GET", path, params)

    def post(self, path, params=None):
        return self._request("POST", path, params)

    def _request(self, method, path, params=None):
        query = ""
        if params:
            query = "?" + urllib.parse.urlencode(params)
        response = request_json(f"{self.base_url}{path}{query}", method=method, timeout=10)
        if not response.get("ok"):
            raise AssertionError(f"{method} {path} failed: {response}")
        return response["data"]


class AgentApiTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        port = os.environ.get("AGENT_ENGINE_PORT")
        if port:
            cls.agent = AgentClient(int(port))
        else:
            editor = EditorClient(ROOT)
            editor.build()

            def agent_after_build():
                service_port = editor.engine_service_port()
                if not service_port:
                    return None
                agent = AgentClient(service_port)
                agent.get("/health")
                return agent

            cls.agent = wait_until(agent_after_build, timeout=20)

    def setUp(self):
        self.reset_if_popup_is_visible()

    def test_agent_api_end_to_end(self):
        health = self.agent.get("/health")
        self.assertEqual("1", health["version"])
        self.assertIn("scene", health["capabilities"])
        self.assertIn("input.click", health["capabilities"])

        screen = self.agent.get("/screen")
        self.assertEqual("top-left", screen["coordinates"]["origin"])
        self.assertGreater(screen["window"]["width"], 0)
        self.assertGreater(screen["window"]["height"], 0)

        scene = self.agent.get("/scene", {
            "visible": "1",
            "include": "basic,bounds,properties",
        })
        self.assertEqual("main", scene["root"]["name"])
        self.assertGreater(scene["count"], 0)

        spawner = self.find_one("/nodes", {
            "type": "goc",
            "name": "spawner",
            "visible": "1",
            "limit": "20",
        })
        spawner_detail = self.agent.get("/node", {
            "id": spawner["id"],
            "include": "basic,bounds,properties,children",
        })["node"]
        self.assertEqual("/spawner", spawner_detail["name"])
        self.assertGreaterEqual(len(spawner_detail["children"]), 2)

        self.assert_spawned_by_node_click(spawner)
        self.assert_spawned_by_coordinate_click(spawner)
        self.assert_drag_merge_by_node_ids("L1", expected_new_level="L2")

        self.click_node(spawner)
        self.click_coordinates(spawner["bounds"]["center"]["x"], spawner["bounds"]["center"]["y"])
        self.assert_drag_merge_by_coordinates("L1", expected_new_level="L2")

        text_key = self.agent.post("/input/key", {"text": "hello"})
        self.assertEqual("key", text_key["queued"])
        self.assertEqual(5, text_key["length"])

        special_key = self.agent.post("/input/key", {"keys": "{KEY_ENTER}"})
        self.assertEqual("key", special_key["queued"])

        self.finish_merge_game()
        gui_nodes = self.agent.get("/nodes", {
            "type": "gui_node",
            "limit": "100",
            "include": "basic,bounds,properties",
        })["nodes"]
        enabled_popup_nodes = {
            node["name"]
            for node in gui_nodes
            if node["enabled"] and node["name"] in {"panel", "restart", "title"}
        }
        self.assertEqual({"panel", "restart", "title"}, enabled_popup_nodes)

        screenshot = self.agent.get("/screenshot")
        screenshot_path = Path(screenshot["path"])
        wait_until(lambda: screenshot_path.exists() and screenshot_path.stat().st_size > 0, timeout=5)
        self.assertGreater(screenshot_path.stat().st_size, 0)

        restart = next(node for node in gui_nodes if node["name"] == "restart")
        self.click_node(restart)
        time.sleep(0.4)
        self.assertEqual(0, len(self.item_labels()))
        panel = self.find_one("/nodes", {"name": "panel", "limit": "1"})
        self.assertFalse(panel["enabled"])

    def assert_spawned_by_node_click(self, node):
        before = self.label_count("L1")
        self.click_node(node)
        self.assertGreater(self.label_count("L1"), before)

    def assert_spawned_by_coordinate_click(self, node):
        before = self.label_count("L1")
        center = node["bounds"]["center"]
        self.click_coordinates(center["x"], center["y"])
        self.assertGreater(self.label_count("L1"), before)

    def assert_drag_merge_by_node_ids(self, label, expected_new_level):
        before = self.label_count(label)
        first, second = self.parents_for_label(label)[:2]
        self.agent.post("/input/drag", {
            "from_id": first,
            "to_id": second,
            "duration": "0.16",
        })
        time.sleep(0.45)
        self.assertLess(self.label_count(label), before)
        self.assertGreaterEqual(self.label_count(expected_new_level), 1)

    def assert_drag_merge_by_coordinates(self, label, expected_new_level):
        before = self.label_count(label)
        first, second = self.parents_for_label(label)[:2]
        first_node = self.agent.get("/node", {
            "id": first,
            "include": "basic,bounds,properties",
        })["node"]
        second_node = self.agent.get("/node", {
            "id": second,
            "include": "basic,bounds,properties",
        })["node"]
        first_center = first_node["bounds"]["center"]
        second_center = second_node["bounds"]["center"]
        self.agent.post("/input/drag", {
            "x1": first_center["x"],
            "y1": first_center["y"],
            "x2": second_center["x"],
            "y2": second_center["y"],
            "duration": "0.16",
        })
        time.sleep(0.45)
        self.assertLess(self.label_count(label), before)
        self.assertGreaterEqual(self.label_count(expected_new_level), 1)

    def finish_merge_game(self):
        self.merge_label("L2")

        spawner = self.find_one("/nodes", {
            "type": "goc",
            "name": "spawner",
            "visible": "1",
            "limit": "1",
        })
        for _ in range(4):
            self.click_node(spawner)
        self.merge_label("L1")
        self.merge_label("L1")
        self.merge_label("L2")
        self.merge_label("L3")

        self.assertEqual(1, self.label_count("L4"))

    def merge_label(self, label):
        first, second = self.parents_for_label(label)[:2]
        self.agent.post("/input/drag", {
            "from_id": first,
            "to_id": second,
            "duration": "0.16",
        })
        time.sleep(0.45)

    def reset_if_popup_is_visible(self):
        nodes = self.agent.get("/nodes", {"name": "restart", "limit": "1"})["nodes"]
        if nodes and nodes[0]["enabled"]:
            self.click_node(nodes[0])
            time.sleep(0.4)

    def click_node(self, node):
        self.agent.post("/input/click", {"id": node["id"]})
        time.sleep(0.25)

    def click_coordinates(self, x, y):
        self.agent.post("/input/click", {"x": x, "y": y})
        time.sleep(0.25)

    def find_one(self, path, params):
        nodes = self.agent.get(path, params)["nodes"]
        self.assertEqual(1, len(nodes), nodes)
        return nodes[0]

    def item_labels(self):
        nodes = self.agent.get("/nodes", {
            "type": "labelc",
            "limit": "100",
        })["nodes"]
        return [node["text"] for node in nodes if node.get("text") != "SPAWN"]

    def label_count(self, label):
        return self.agent.get("/nodes", {
            "type": "labelc",
            "text": label,
            "limit": "100",
        })["count"]

    def parents_for_label(self, label):
        nodes = self.agent.get("/nodes", {
            "type": "labelc",
            "text": label,
            "limit": "100",
        })["nodes"]
        parents = [node["parent"] for node in nodes]
        self.assertGreaterEqual(len(parents), 2, f"missing pair for {label}: {self.item_labels()}")
        return parents


if __name__ == "__main__":
    unittest.main()
