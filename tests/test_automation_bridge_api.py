#!/usr/bin/env python3
import os
import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "automation_bridge" / "automation-bridge-python"))

from automation_bridge import AutomationBridgeApiError, AutomationBridgeClient, EditorClient, wait_until  # noqa: E402


class AutomationBridgeClientUnitTest(unittest.TestCase):
    def test_wait_for_count_accepts_zero(self):
        class FakeAutomationBridge:
            def count(self, **selector):
                return 0

        self.assertEqual(0, AutomationBridgeClient.wait_for_count(FakeAutomationBridge(), 0, timeout=0.1, interval=0.01))


class AutomationBridgeApiTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.editor = None
        cls.bridge = None
        port = os.environ.get("AUTOMATION_BRIDGE_ENGINE_PORT")
        if port:
            cls.bridge = AutomationBridgeClient(int(port))
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
                self.reset_if_popup_is_visible()
                self.run_automation_bridge_api_end_to_end()

    def run_automation_bridge_api_end_to_end(self):
        health = self.bridge.health()
        self.assertEqual("1", health["version"])
        self.assertIn("scene", health["capabilities"])
        self.assertIn("input.click", health["capabilities"])

        screen = self.bridge.screen()
        self.assertEqual("top-left", screen["coordinates"]["origin"])
        self.assertGreater(screen["window"]["width"], 0)
        self.assertGreater(screen["window"]["height"], 0)

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
