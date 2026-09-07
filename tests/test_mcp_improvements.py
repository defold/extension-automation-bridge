"""Regression contracts for the current shared API exposed through MCP."""
import base64
import io
import json
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

from automation_bridge import editor, engine
from automation_bridge.mcp_runtime import BridgeRuntime, serialize

ROOT = Path(__file__).resolve().parents[1]


class SharedToolsTest(unittest.TestCase):
    def setUp(self):
        self.runtime = BridgeRuntime(ROOT)
        self.game = engine.Client(54321)
        self.wire = serialize(self.game, self.runtime.handles)

    def tearDown(self):
        self.runtime.cleanup()

    def test_page_metadata_and_snapshot_identity_survive(self):
        page = engine.ElementPage.from_raw({
            'elements': [{'id': 'button', 'instance_id': 'instance-a'}],
            'matched': 8, 'total': 19, 'next_cursor': '1', 'truncated': True,
            'engine_frame': 27, 'scene_sequence': 3,
        })
        with mock.patch.object(self.game, 'elements_page', return_value=page) as query:
            result = self.runtime.call_tool('defold_find_elements', {'engine': self.wire, 'selector': {'limit': 1}})
        self.assertTrue(result['ok'], result)
        self.assertEqual((8, '1', 27, 3), tuple(result['data'][k] for k in ('matched', 'next_cursor', 'engine_frame', 'scene_sequence')))
        self.assertEqual('instance-a', result['data']['elements'][0]['raw']['instance_id'])
        query.assert_called_once_with(limit=1)

    def test_invalid_selector_rejected_before_io(self):
        for selector in ({'limit': True}, {'enabled': 'true'}, {'cursor': 2}):
            with self.subTest(selector=selector), mock.patch.object(self.game, 'elements_page') as query:
                result = self.runtime.call_tool('defold_find_elements', {'engine': self.wire, 'selector': selector})
                self.assertFalse(result['ok'], result)
                query.assert_not_called()

    def test_focused_key_and_pointer_modifiers_are_forwarded(self):
        for tool, method, arguments in (
            ('defold_key', 'key', {'key': 'M', 'hold': 0.2, 'modifiers': ['LSHIFT']}),
            ('defold_click', 'click', {'target': [10, 20], 'modifiers': 'LCTRL'}),
            ('defold_drag', 'drag', {'from_target': [10, 20], 'to_target': [30, 40], 'modifiers': ['LALT']}),
        ):
            with self.subTest(tool=tool), mock.patch.object(self.game, method, return_value={}) as action:
                result = self.runtime.call_tool(tool, {'engine': self.wire, **arguments})
                self.assertTrue(result['ok'], result)
                self.assertEqual(arguments['modifiers'], action.call_args.kwargs['modifiers'])
                if method == 'key':
                    self.assertEqual(0.2, action.call_args.kwargs['hold'])

    def test_doctor_and_application_discovery_use_shared_api(self):
        with mock.patch.object(editor, 'doctor', return_value={'ready': False}) as doctor:
            result = self.runtime.call_tool('defold_doctor', {'project_path': str(ROOT)})
            self.assertEqual({'ready': False}, result['data'])
            doctor.assert_called_once_with(ROOT, required_capabilities=())
        with mock.patch.object(self.game, 'application_catalog', return_value={'revision': 3}) as catalog:
            self.runtime.call_tool('defold_application_catalog', {'engine': self.wire, 'kind': 'command'})
            catalog.assert_called_once_with(kind='command', limit=20)
        result = self.runtime.call_tool('defold_session_info', {'engine': self.wire})
        self.assertFalse(result['data']['owns_engine'])
        self.assertFalse(result['data']['closed'])
