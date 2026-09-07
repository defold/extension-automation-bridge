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


class CancellationTest(unittest.TestCase):
    def test_wait_cancellation_stops_polling_and_requests_native_cleanup(self):
        runtime = BridgeRuntime(ROOT)
        game = engine.Client(54321)
        wire = serialize(game, runtime.handles)
        observed = threading.Event()
        responses = []
        def health():
            observed.set()
            return {'ready': False}
        with mock.patch.object(game, 'health', side_effect=health) as query, mock.patch.object(game.input, 'flush', return_value={}) as flush:
            worker = threading.Thread(target=lambda: responses.append(runtime.call_tool_request('wait', 'automation_bridge_wait', {
                'operation': 'automation_bridge.engine.Client.health', 'target': wire,
                'path': 'ready', 'interval': 60, 'timeout': 120,
            })))
            worker.start()
            self.assertTrue(observed.wait(1))
            runtime.cancel('wait')
            worker.join(1)
            self.assertFalse(worker.is_alive())
            self.assertEqual(1, query.call_count)
            flush.assert_called_once_with(release=True)
            self.assertEqual('operation_cancelled', responses[0]['error']['code'])
        runtime.cleanup()

    def test_cancel_before_worker_starts_and_id_reuse(self):
        runtime = BridgeRuntime(ROOT)
        runtime.prepare_request(1)
        runtime.cancel(1)
        result = runtime.call_tool_request(1, 'automation_bridge_catalog', {})
        self.assertEqual('operation_cancelled', result['error']['code'])
        runtime.finish_request(1)
        self.assertTrue(runtime.call_tool_request(1, 'automation_bridge_catalog', {})['ok'])
        runtime.cancel(1)
        self.assertFalse(runtime._requests)
        runtime.cleanup()

    def test_cleanup_failure_is_inspectable_after_cancelled_response_is_suppressed(self):
        runtime = BridgeRuntime(ROOT)
        game = engine.Client(54321)
        wire = serialize(game, runtime.handles)
        def health():
            runtime.cancel('cancel')
            return {'ready': False}
        with mock.patch.object(game, 'health', side_effect=health), mock.patch.object(game.input, 'flush', side_effect=RuntimeError('native refused cleanup')):
            response = runtime.call_tool_request('cancel', 'defold_health', {'engine': wire})
        self.assertIn('native refused cleanup', response['error']['data']['cleanup_error']['message'])
        self.assertEqual('cancel', runtime.cleanup_errors[-1]['request_id'])
        runtime.cleanup()

    def test_shutdown_discards_late_client_without_allocating_a_handle(self):
        runtime = BridgeRuntime(ROOT)
        started, release = threading.Event(), threading.Event()
        game = engine.Client(54321)
        def connect(**kwargs):
            started.set()
            release.wait(2)
            return game
        responses = []
        with mock.patch.object(editor, 'open_project', side_effect=connect):
            worker = threading.Thread(target=lambda: responses.append(runtime.call_tool_request('late', 'defold_open_project', {'project_path': str(ROOT)})))
            worker.start()
            self.assertTrue(started.wait(1))
            runtime.cleanup()
            release.set()
            worker.join(1)
        self.assertFalse(worker.is_alive())
        self.assertFalse(responses[0]['ok'])
        self.assertTrue(game.closed)
        self.assertEqual([], runtime.handles.snapshot())

    def test_stdio_cancellation_suppresses_response_and_stops_real_wait(self):
        from automation_bridge.mcp_protocol import McpProtocol, StdioServer
        runtime = BridgeRuntime(ROOT)
        protocol = McpProtocol(runtime)
        protocol.handle({'jsonrpc': '2.0', 'id': 0, 'method': 'initialize', 'params': {
            'protocolVersion': '2025-11-25', 'clientInfo': {'name': 'test', 'version': '1'}, 'capabilities': {}}})
        output = io.StringIO()
        server = StdioServer(protocol, stdout=output, stderr=io.StringIO())
        game = engine.Client(54321)
        wire = serialize(game, runtime.handles)
        started = threading.Event()
        def health():
            started.set()
            return False
        with mock.patch.object(game, 'health', side_effect=health), mock.patch.object(game.input, 'flush', return_value={}) as flush:
            server._accept({'jsonrpc': '2.0', 'id': 'cancel-me', 'method': 'tools/call', 'params': {
                'name': 'automation_bridge_wait', 'arguments': {
                    'operation': 'automation_bridge.engine.Client.health', 'target': wire, 'interval': 60, 'timeout': 120}}})
            self.assertTrue(started.wait(1))
            server.cancel('cancel-me')
            server._drain_workers()
            flush.assert_called_once_with(release=True)
        self.assertEqual('', output.getvalue())
        self.assertFalse(runtime._requests)
        server.close()


class SessionTest(unittest.TestCase):
    def test_logical_sessions_isolate_handles_and_report_cleanup_errors(self):
        runtime = BridgeRuntime(ROOT)
        session = runtime.call_tool('automation_bridge_session', {'action': 'open'})['data']['mcp_session']
        project = editor.Client(ROOT, port=51336)
        with mock.patch.object(editor, 'open_project', return_value=project):
            opened = runtime.call_tool('defold_open_project', {'mcp_session': session, 'project_path': str(ROOT)})
        wire = opened['data']
        operation = 'automation_bridge.editor.Client.root'
        rejected = runtime.call_tool('automation_bridge_get', {'operation': operation, 'target': wire})
        self.assertEqual('wrong_session', rejected['error']['code'])
        accepted = runtime.call_tool('automation_bridge_get', {'mcp_session': session, 'operation': operation, 'target': wire})
        self.assertTrue(accepted['ok'], accepted)
        closed = runtime.call_tool('automation_bridge_session', {'mcp_session': session, 'action': 'close'})
        self.assertTrue(closed['data']['closed'])
        self.assertEqual(0, closed['data']['handles'])
        rejected = runtime.call_tool('automation_bridge_get', {'mcp_session': session, 'operation': operation, 'target': wire})
        self.assertEqual('unknown_session', rejected['error']['code'])
        runtime.cleanup()

    def test_project_connections_forward_ids_and_reject_duplicate_native_identity(self):
        runtime = BridgeRuntime(ROOT)
        project = editor.Client(ROOT, port=51336)
        wire = serialize(project, runtime.handles)
        def connect(**kwargs):
            return engine.Client(54321, client_id=kwargs['client_id'], session_id=kwargs['session_id'])
        with mock.patch.object(project, 'connect_engine', side_effect=connect) as call:
            args = {'project': wire, 'client_id': 'agent-a', 'session_id': 'work-a', 'wait_ready': False}
            first = runtime.call_tool('defold_connect_engine', args)
            self.assertTrue(first['ok'], first)
            self.assertEqual('agent-a', call.call_args.kwargs['client_id'])
            self.assertEqual('work-a', call.call_args.kwargs['session_id'])
            duplicate = runtime.call_tool('defold_connect_engine', args)
            self.assertEqual('identity_in_use', duplicate['error']['code'])
            self.assertEqual(1, call.call_count)
        runtime.cleanup()

    def test_busy_client_cannot_be_released_and_shutdown_defers_its_close(self):
        runtime = BridgeRuntime(ROOT)
        game = engine.Client(54321)
        wire = serialize(game, runtime.handles)
        started, release = threading.Event(), threading.Event()
        def health():
            started.set()
            release.wait(2)
            return {}
        with mock.patch.object(game, 'health', side_effect=health), mock.patch.object(game.input, 'flush', return_value={}):
            worker = threading.Thread(target=lambda: runtime.call_tool_request('busy', 'defold_health', {'engine': wire}))
            worker.start()
            self.assertTrue(started.wait(1))
            response = runtime.call_tool('automation_bridge_release', {'target': wire})
            self.assertEqual('handle_busy', response['error']['code'])
            self.assertFalse(game.closed)
            runtime.cleanup()
            self.assertFalse(game.closed)
            release.set()
            worker.join(1)
            self.assertFalse(worker.is_alive())
            self.assertTrue(game.closed)
            self.assertEqual([], runtime.handles.snapshot())

    def test_closing_borrowed_client_releases_input_and_invalidates_child_handles(self):
        runtime = BridgeRuntime(ROOT)
        game = engine.Client(54321)
        wire = serialize(game, runtime.handles)
        child = runtime.call_tool('automation_bridge_get', {'operation': 'automation_bridge.engine.Client.input', 'target': wire})['data']
        with mock.patch.object(game.input, 'flush', return_value={}) as flush, mock.patch.object(game, 'close_engine') as terminate:
            response = runtime.call_tool('defold_close', {'engine': wire})
            self.assertTrue(response['ok'], response)
            self.assertTrue(response['data']['closed'])
            flush.assert_called_once_with(release=True)
            terminate.assert_not_called()
        rejected = runtime.call_tool('automation_bridge_call', {'operation': 'automation_bridge.engine.InputController.pending', 'target': child})
        self.assertEqual('unknown_handle', rejected['error']['code'])
        runtime.cleanup()

    def test_release_cleanup_error_is_visible_and_retains_handle_for_inspection(self):
        runtime = BridgeRuntime(ROOT)
        game = engine.Client(54321)
        wire = serialize(game, runtime.handles)
        with mock.patch.object(game.input, 'flush', side_effect=RuntimeError('native unavailable')):
            response = runtime.call_tool('automation_bridge_release', {'target': wire})
        self.assertEqual('cleanup_failed', response['error']['code'])
        self.assertTrue(game.closed)
        info = runtime.call_tool('automation_bridge_session', {})['data']
        self.assertIn('native unavailable', info['cleanup_errors'][-1]['error']['message'])
        self.assertEqual(1, info['handles'])
        runtime.cleanup()


PNG = base64.b64decode('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+aDTsAAAAASUVORK5CYII=')


class VisualToolsTest(unittest.TestCase):
    def setUp(self):
        from automation_bridge.mcp_protocol import McpProtocol
        self.runtime = BridgeRuntime(ROOT)
        self.game = engine.Client(54321)
        self.wire = serialize(self.game, self.runtime.handles)
        self.protocol = McpProtocol(self.runtime)
        self.protocol.handle({'jsonrpc': '2.0', 'id': 0, 'method': 'initialize', 'params': {
            'protocolVersion': '2025-11-25', 'clientInfo': {'name': 'test', 'version': '1'}, 'capabilities': {}}})

    def tearDown(self):
        self.runtime.cleanup()

    def call(self, name, arguments):
        return self.protocol.handle({'jsonrpc': '2.0', 'id': 1, 'method': 'tools/call', 'params': {'name': name, 'arguments': arguments}})['result']

    def test_completed_screenshot_is_image_content_without_base64_in_text(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'capture.png'
            path.write_bytes(PNG)
            receipt = engine.ScreenshotReceipt({'state': 'complete', 'path': str(path), 'engine_frame': 12, 'scene_sequence': 4})
            with mock.patch.object(self.game, 'screenshot', return_value=receipt) as screenshot:
                result = self.call('defold_screenshot', {'engine': self.wire})
        self.assertFalse(result['isError'], result)
        self.assertEqual(['text', 'image'], [content['type'] for content in result['content']])
        self.assertEqual(PNG, base64.b64decode(result['content'][1]['data']))
        self.assertNotIn(result['content'][1]['data'], result['content'][0]['text'])
        self.assertEqual(12, result['structuredContent']['data']['frame'])
        screenshot.assert_called_once_with(resolution_multiplier=0.5)

    def test_pending_screenshot_has_no_image_and_no_implicit_downscale(self):
        receipt = engine.ScreenshotReceipt({'state': 'pending', 'path': '/missing/pending.png'})
        with mock.patch.object(self.game, 'screenshot', return_value=receipt) as screenshot:
            result = self.call('defold_screenshot', {'engine': self.wire, 'wait': False})
        self.assertEqual(['text'], [content['type'] for content in result['content']])
        self.assertEqual('pending', result['structuredContent']['data']['state'])
        screenshot.assert_called_once_with(wait=False)

    def test_editor_preview_is_an_image_in_focused_and_generic_calls(self):
        project = editor.Client(ROOT, port=51336)
        wire = serialize(project, self.runtime.handles)
        preview_wire = serialize(project.preview, self.runtime.handles)
        with mock.patch.object(project.preview, 'render', return_value=PNG) as render:
            result = self.call('defold_preview', {'project': wire, 'path': '/main/main.collection'})
            self.assertEqual(1, result['structuredContent']['data']['width'])
            self.assertEqual('image/png', result['content'][1]['mimeType'])
            self.assertEqual(0.5, render.call_args.kwargs['resolution_multiplier'])
            result = self.call('automation_bridge_call', {'operation': 'automation_bridge.editor.Preview.render', 'target': preview_wire, 'arguments': {'path': '/main/main.collection'}})
            self.assertEqual('image', result['content'][1]['type'])

    def test_observation_preserves_different_frame_evidence_and_bounds_logs(self):
        page = engine.ElementPage.from_raw({'elements': [], 'matched': 20, 'engine_frame': 21, 'scene_sequence': 7})
        capture = engine.ScreenshotReceipt({'state': 'pending', 'engine_frame': 22, 'scene_sequence': 7})
        with mock.patch.object(self.game, 'elements_page', return_value=page), mock.patch.object(self.game, 'screenshot', return_value=capture), mock.patch.object(self.game.logs, 'tail', return_value=['info: ok', 'ERROR: ' + 'x' * 3000] * 30):
            result = self.call('defold_observe', {'engine': self.wire, 'error_limit': 2})
        data = result['structuredContent']['data']
        self.assertFalse(data['evidence']['same_frame'])
        self.assertEqual(21, data['page']['engine_frame'])
        self.assertEqual(22, data['screenshot']['frame'])
        self.assertEqual(2, len(data['recent_errors']['lines']))
        self.assertLessEqual(len(data['recent_errors']['lines'][0]), 2000)
        self.assertIsNone(data['recent_errors']['engine_frame'])

    def test_missing_or_replaced_capture_preserves_receipt_in_failure(self):
        receipt = engine.ScreenshotReceipt({'state': 'complete', 'path': '/missing/capture.png', 'capture_id': 17})
        with mock.patch.object(self.game, 'screenshot', return_value=receipt):
            result = self.call('defold_screenshot', {'engine': self.wire})
        self.assertTrue(result['isError'])
        self.assertEqual(17, result['structuredContent']['error']['data']['receipt']['capture_id'])
