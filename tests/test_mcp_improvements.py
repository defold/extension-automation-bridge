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


class EditorToolsTest(unittest.TestCase):
    def setUp(self):
        self.runtime = BridgeRuntime(ROOT)
        self.project = editor.Client(ROOT, port=51336)
        self.wire = serialize(self.project, self.runtime.handles)

    def tearDown(self):
        self.runtime.cleanup()

    def test_build_returns_engine_ownership_and_structured_evidence(self):
        game = engine.Client(54321)
        issue = editor.BuildIssue('warning', 'check this', '/main.script',
                                  editor.SourceRange(editor.SourcePosition(2, 1), editor.SourcePosition(2, 4)))
        result = editor.BuildResult('run', 200, True, True, (issue,), 'http://localhost:54321', {})
        self.project._last_command_result = result
        with mock.patch.object(self.project, 'build_and_run', return_value=game) as build:
            response = self.runtime.call_tool('defold_build_and_run', {'project': self.wire, 'focus': False})
        self.assertTrue(response['ok'], response)
        self.assertIn('$handle', response['data']['engine'])
        self.assertEqual('http://localhost:54321', response['data']['build_result']['target_url'])
        self.assertEqual(2, response['data']['build_result']['issues'][0]['range']['start']['line'])
        self.assertFalse(build.call_args.kwargs['focus'])

    def test_compile_and_bob_forward_without_fallback_or_retries(self):
        for tool, method, arguments in (
            ('defold_compile', 'compile', {}),
            ('defold_bob', 'bob', {'options': {'platform': 'wasm-web'}, 'commands': ['build', 'bundle']}),
        ):
            with self.subTest(tool=tool), mock.patch.object(self.project, method, side_effect=TimeoutError('still running')) as call:
                response = self.runtime.call_tool(tool, {'project': self.wire, **arguments})
                self.assertFalse(response['ok'])
                self.assertFalse(response['error']['retryable'])
                self.assertEqual(1, call.call_count)

    def test_version_requirement_and_build_failure_details_survive(self):
        errors = [editor.UnsupportedOperationError('compile unavailable', minimum_version='1.13.2')]
        issue = editor.BuildIssue('error', 'bad Lua', '/main.script',
                                  editor.SourceRange(editor.SourcePosition(1, 0), editor.SourcePosition(1, 4)))
        errors.append(editor.BuildError((issue,), result=editor.BuildResult('compile', 200, True, False, (issue,), None, {})))
        for error in errors:
            with self.subTest(error=type(error).__name__), mock.patch.object(self.project, 'compile', side_effect=error):
                response = self.runtime.call_tool('defold_compile', {'project': self.wire})
                details = response['error']['data']
                if isinstance(error, editor.UnsupportedOperationError):
                    self.assertEqual('1.13.2', details['minimum_version'])
                    self.assertIn('supported from Defold 1.13.2', response['error']['message'])
                else:
                    self.assertFalse(details['result']['success'])
                    self.assertEqual(4, details['result']['issues'][0]['range']['end']['character'])
