"""Regression contracts for the current shared API exposed through MCP."""
import base64
import dataclasses
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


class GenericArgumentValidationTest(unittest.TestCase):
    def setUp(self):
        self.runtime = BridgeRuntime(ROOT)
        self.addCleanup(self.runtime.cleanup)

    def test_generic_open_rejects_malformed_boolean_before_editor_discovery(self):
        for value in ('false', 0, None):
            with self.subTest(value=value), mock.patch.object(editor.Client, '_open_project') as open_project:
                result = self.runtime.call_tool('automation_bridge_call', {
                    'operation': 'automation_bridge.editor.open_project',
                    'arguments': {'root': str(ROOT), 'start_if_needed': value},
                })
                self.assertFalse(result['ok'], result)
                self.assertEqual('invalid_arguments', result['error']['code'])
                open_project.assert_not_called()
        with mock.patch.object(editor.Client, '_open_project', return_value={'connected': True}) as open_project:
            result = self.runtime.call_tool('automation_bridge_call', {
                'operation': 'automation_bridge.editor.open_project',
                'arguments': {'root': str(ROOT), 'start_if_needed': False},
            })
            self.assertTrue(result['ok'], result)
            open_project.assert_called_once_with(str(ROOT), start_if_needed=False, timeout=30.0, launcher=None)

    def test_generic_methods_and_destructive_calls_reject_invalid_arguments(self):
        game = engine.Client(54321)
        self.addCleanup(game.close)
        wire = serialize(game, self.runtime.handles)
        for tool, method, arguments, extra in (
            ('automation_bridge_call', 'key', {'key': 'A', 'hold': 'long'}, {}),
            ('automation_bridge_destructive_call', 'close_engine', {'timeout': 'soon'}, {'confirm': True}),
        ):
            with self.subTest(method=method), mock.patch.object(game, method) as operation:
                result = self.runtime.call_tool(tool, {
                    'operation': 'automation_bridge.engine.Client.' + method,
                    'target': wire, 'arguments': arguments, **extra,
                })
                self.assertFalse(result['ok'], result)
                self.assertEqual('invalid_arguments', result['error']['code'], result)
                operation.assert_not_called()

    def test_declarative_wait_validates_nested_observation_arguments(self):
        game = engine.Client(54321)
        self.addCleanup(game.close)
        wire = serialize(game, self.runtime.handles)
        with mock.patch.object(game, 'scene') as scene:
            result = self.runtime.call_tool('automation_bridge_wait', {
                'operation': 'automation_bridge.engine.Client.scene', 'target': wire,
                'arguments': {'visible': 'false'},
            })
            self.assertFalse(result['ok'], result)
            self.assertEqual('invalid_arguments', result['error']['code'], result)
            scene.assert_not_called()

    def test_generic_wait_preserves_element_wire_arguments_until_dispatch(self):
        game = engine.Client(54321)
        self.addCleanup(game.close)
        wire = serialize(game, self.runtime.handles)
        element = engine.Element({'id': 'child', 'parent_id': 'parent', 'instance_id': 'instance-a'})
        element_wire = serialize(element, self.runtime.handles)
        with mock.patch.object(game, 'parent', return_value=element) as parent:
            result = self.runtime.call_tool('automation_bridge_call', {
                'operation': 'automation_bridge.engine.wait_until',
                'arguments': {
                    'operation': 'automation_bridge.engine.Client.parent', 'target': wire,
                    'arguments': {'element_or_id': element_wire},
                },
            })
        self.assertTrue(result['ok'], result)
        self.assertEqual(element.raw, parent.call_args.kwargs['element_or_id'].raw)
        self.assertEqual(element_wire, result['data'])

    def test_nested_event_handle_accepts_tokens_and_envelopes(self):
        game = engine.Client(54321)
        self.addCleanup(game.close)
        wire = serialize(game, self.runtime.handles)
        with mock.patch.object(game, 'request', return_value={'cursor': 0, 'oldest_cursor': 0}):
            created = self.runtime.call_tool('automation_bridge_call', {
                'operation': 'automation_bridge.engine.Client.events', 'target': wire,
            })
        self.assertTrue(created['ok'], created)
        stream_wire = created['data']
        stream = self.runtime.handles.get(stream_wire['$handle'])
        for value in (stream_wire, stream_wire['$handle']):
            with self.subTest(value=value), mock.patch.object(stream, 'wait', return_value={'acknowledged': True}) as wait:
                result = self.runtime.call_tool('automation_bridge_call', {
                    'operation': 'automation_bridge.engine.Client.wait_for_input_acknowledgement',
                    'target': wire, 'arguments': {'input_id': 7, 'events': value},
                })
                self.assertEqual({'ok': True, 'data': {'acknowledged': True}}, result)
                wait.assert_called_once_with('input.acknowledged', where={'input_id': 7},
                                             event_type='acknowledgement', timeout=10.0)
        self.runtime.call_tool('automation_bridge_release', {'target': stream_wire})
        result = self.runtime.call_tool('automation_bridge_call', {
            'operation': 'automation_bridge.engine.Client.wait_for_input_acknowledgement',
            'target': wire, 'arguments': {'input_id': 7, 'events': stream_wire['$handle']},
        })
        self.assertEqual('unknown_handle', result['error']['code'])

    def test_application_strings_matching_handles_remain_literal(self):
        game = engine.Client(54321)
        self.addCleanup(game.close)
        wire = serialize(game, self.runtime.handles)
        token = wire['$handle']
        payload = {'text': token, 'nested': [token]}
        with mock.patch.object(game, 'command', return_value=payload) as command:
            result = self.runtime.call_tool('automation_bridge_call', {
                'operation': 'automation_bridge.engine.Client.command', 'target': wire,
                'arguments': {'name': token, 'data': payload},
            })
        self.assertTrue(result['ok'], result)
        self.assertEqual(payload, result['data'])
        command.assert_called_once_with(name=token, data=payload)


class PreferenceReadSafetyTest(unittest.TestCase):
    def setUp(self):
        self.runtime = BridgeRuntime(ROOT)
        self.addCleanup(self.runtime.cleanup)
        self.preferences = editor.Client(ROOT, port=51336).preferences
        self.wire = serialize(self.preferences, self.runtime.handles)

    def read(self, path):
        return self.runtime.call_tool('automation_bridge_call', {
            'operation': 'automation_bridge.editor.Preferences.get',
            'target': self.wire, 'arguments': {'preference': path},
        })

    def test_password_groups_are_rejected_before_reading_values(self):
        for path in ('extensions', '/extensions/', 'extensions/build-server-password'):
            with self.subTest(path=path), mock.patch.object(self.preferences, 'get') as read:
                result = self.read(path)
                self.assertEqual('sensitive_preference', result['error']['code'])
                read.assert_not_called()

    def test_password_type_metadata_covers_nested_groups(self):
        secret = dataclasses.replace(self.preferences.EXTENSIONS_BUILD_SERVER_PASSWORD,
                                     path='custom/nested/credential')
        with mock.patch('automation_bridge.preferences.BUILTIN_PREFERENCES', (secret,)):
            for path in ('custom', '/custom//nested/', 'custom/nested/credential'):
                with self.subTest(path=path), mock.patch.object(self.preferences, 'get') as read:
                    result = self.read(path)
                    self.assertEqual('sensitive_preference', result['error']['code'])
                    read.assert_not_called()

    def test_nonsecret_leaves_and_groups_remain_readable(self):
        for path in ('extensions/build-server', 'code/font', 'custom/window'):
            with self.subTest(path=path), mock.patch.object(self.preferences, 'get', return_value={'value': 12}) as read:
                result = self.read(path)
                self.assertEqual({'ok': True, 'data': {'value': 12}}, result)
                read.assert_called_once_with(preference=path)


class ConnectionCleanupTest(unittest.TestCase):
    def test_failed_readiness_closes_unretained_clients_without_native_mutations(self):
        for through_project in (False, True):
            for error in (TimeoutError('not ready'), engine.UnsupportedCapabilityError('missing scene'),
                          engine.OperationCancelled('cancelled while connecting')):
                with self.subTest(through_project=through_project, error=type(error).__name__):
                    runtime = BridgeRuntime(ROOT)
                    self.addCleanup(runtime.cleanup)
                    game = engine.Client(54321)
                    self.addCleanup(game.close)
                    project = editor.Client(ROOT, port=51336)
                    arguments = {'project': serialize(project, runtime.handles)} if through_project else {'port': 54321}
                    owner, member = (project, 'connect_engine') if through_project else (engine, 'connect')
                    with mock.patch.object(owner, member, return_value=game), \
                         mock.patch.object(game, 'wait_ready', side_effect=error), \
                         mock.patch.object(game.logs, 'close') as close_logs, \
                         mock.patch.object(game.input, 'flush') as flush, \
                         mock.patch.object(game, 'close_engine') as terminate:
                        result = runtime.call_tool('defold_connect_engine', arguments)
                        self.assertFalse(result['ok'], result)
                        self.assertEqual(type(error).__name__, result['error']['type'])
                        self.assertTrue(game.closed)
                        close_logs.assert_called_once_with()
                        flush.assert_not_called()
                        terminate.assert_not_called()
                    self.assertIsNone(runtime.handles.token_for(game))

    def test_readiness_error_survives_cleanup_failure(self):
        runtime = BridgeRuntime(ROOT)
        self.addCleanup(runtime.cleanup)
        game = engine.Client(54321)
        self.addCleanup(game.close)
        with mock.patch.object(engine, 'connect', return_value=game), \
             mock.patch.object(game, 'wait_ready', side_effect=engine.UnsupportedCapabilityError('missing scene')), \
             mock.patch.object(game, 'close', side_effect=RuntimeError('collector cleanup failed')):
            result = runtime.call_tool('defold_connect_engine', {'port': 54321})
        self.assertEqual('unsupported_capability_error', result['error']['code'], result)
        info = runtime.call_tool('automation_bridge_session', {})['data']
        self.assertEqual('collector cleanup failed', info['cleanup_errors'][-1]['error']['message'])


class ProfilerCleanupTest(unittest.TestCase):
    def test_engine_cleanup_reaches_profiler_recordings_through_both_helpers(self):
        for through_connection in (False, True):
            for tool, argument in (('defold_close', 'engine'), ('automation_bridge_release', 'target')):
                with self.subTest(through_connection=through_connection, tool=tool):
                    runtime = BridgeRuntime(ROOT)
                    self.addCleanup(runtime.cleanup)
                    game = engine.Client(54321)
                    self.addCleanup(game.close)
                    wire = serialize(game, runtime.handles)
                    profiler = runtime.call_tool('automation_bridge_get', {
                        'operation': 'automation_bridge.engine.Client.profiler', 'target': wire,
                    })['data']
                    connection = engine.ProfilerConnection()
                    connection._socket = mock.Mock()
                    self.addCleanup(connection.stop)
                    polled = threading.Event()
                    pause = threading.Event()
                    def receive(deadline):
                        polled.set()
                        pause.wait(0.01)
                        raise engine.ProfilerTimeoutError('fixture poll')
                    with mock.patch.object(engine.ProfilerClient, 'connect', return_value=connection), \
                         mock.patch.object(connection, '_next_message', side_effect=receive), \
                         mock.patch.object(game.input, 'pending', return_value=[]):
                        target, owner = profiler, 'ProfilerClient'
                        if through_connection:
                            target = runtime.call_tool('automation_bridge_call', {
                                'operation': 'automation_bridge.engine.ProfilerClient.connect', 'target': profiler,
                            })['data']
                            owner = 'ProfilerConnection'
                        response = runtime.call_tool('automation_bridge_call', {
                            'operation': 'automation_bridge.engine.' + owner + '.start_recording', 'target': target,
                        })
                        self.assertTrue(response['ok'], response)
                        recording_wire = response['data']
                        recording = runtime.handles.get(recording_wire['$handle'])
                        self.addCleanup(recording.abort)
                        try:
                            self.assertTrue(polled.wait(1))
                            result = runtime.call_tool(tool, {argument: wire})
                            self.assertTrue(result['ok'], result)
                            self.assertTrue(game.closed)
                            self.assertFalse(recording.running)
                            self.assertFalse(connection.connected)
                            self.assertIsNone(runtime.handles.token_for(recording))
                        finally:
                            recording.abort()

    def test_stopped_recordings_are_finalized_on_release_and_session_close(self):
        from tests.test_automation_bridge_api import _remotery_sample_frame_body
        for reader_error in (False, True):
            for close_session in (False, True):
                with self.subTest(reader_error=reader_error, close_session=close_session):
                    runtime = BridgeRuntime(ROOT)
                    self.addCleanup(runtime.cleanup)
                    connection = engine.ProfilerConnection()
                    connection._socket = mock.Mock()
                    self.addCleanup(connection.stop)
                    recording = engine.ProfilerRecording(connection, max_frames=1, resolve_names=False, close_on_stop=True)
                    self.addCleanup(recording.abort)
                    result = ('SMPL', _remotery_sample_frame_body())
                    error = engine.ProfilerError('reader failed') if reader_error else None
                    with mock.patch.object(connection, '_next_message', return_value=result, side_effect=error):
                        recording.start()
                        recording._thread.join(1)
                    self.assertFalse(recording.running)
                    self.assertTrue(connection.connected)
                    wire = serialize(recording, runtime.handles)
                    if close_session:
                        response = runtime.call_tool('automation_bridge_session', {'action': 'close'})
                    else:
                        response = runtime.call_tool('automation_bridge_release', {'target': wire})
                    self.assertTrue(response['ok'], response)
                    self.assertFalse(connection.connected)
                    self.assertEqual([], runtime.handles.snapshot())


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
        with mock.patch.object(game, 'health', side_effect=health) as query, \
             mock.patch.object(game.input, 'pending', return_value=[{'client_id': game.client_id, 'session_id': game.session_id}]), \
             mock.patch.object(game.input, 'flush', return_value={}) as flush:
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
        with mock.patch.object(game, 'health', side_effect=health), \
             mock.patch.object(game.input, 'pending', return_value=[{'client_id': game.client_id, 'session_id': game.session_id}]), \
             mock.patch.object(game.input, 'flush', side_effect=RuntimeError('native refused cleanup')):
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
        with mock.patch.object(game, 'health', side_effect=health), \
             mock.patch.object(game.input, 'pending', return_value=[{'client_id': game.client_id, 'session_id': game.session_id}]), \
             mock.patch.object(game.input, 'flush', return_value={}) as flush:
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

    def test_stdio_finishes_resource_use_before_publishing_response(self):
        from automation_bridge.mcp_protocol import McpProtocol, StdioServer
        runtime = BridgeRuntime(ROOT)
        protocol = McpProtocol(runtime)
        protocol.handle({'jsonrpc': '2.0', 'id': 0, 'method': 'initialize', 'params': {
            'protocolVersion': '2025-11-25', 'clientInfo': {'name': 'test', 'version': '1'}, 'capabilities': {}}})
        output = io.StringIO()
        server = StdioServer(protocol, stdout=output, stderr=io.StringIO())
        game = engine.Client(54321)
        wire = serialize(game, runtime.handles)
        finishing, release = threading.Event(), threading.Event()
        original_finish = runtime.finish_request
        def finish(request_id):
            finishing.set()
            release.wait(2)
            original_finish(request_id)
        message = {'jsonrpc': '2.0', 'id': 'reused', 'method': 'tools/call', 'params': {
            'name': 'defold_health', 'arguments': {'engine': wire}}}
        try:
            with mock.patch.object(game, 'health', return_value={'ready': True}) as health, mock.patch.object(runtime, 'finish_request', side_effect=finish):
                server._accept(message)
                try:
                    self.assertTrue(finishing.wait(1))
                    self.assertEqual('', output.getvalue())
                finally:
                    release.set()
                    server._drain_workers()
                server._accept(message)
                server._drain_workers()
                self.assertEqual(2, health.call_count)
            replies = [json.loads(line) for line in output.getvalue().splitlines()]
            self.assertEqual(2, len(replies))
            self.assertTrue(all(reply['result']['structuredContent']['ok'] for reply in replies), replies)
            self.assertFalse(runtime._requests)
        finally:
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
        with mock.patch.object(game.input, 'pending', return_value=[{'client_id': game.client_id, 'session_id': game.session_id}]), mock.patch.object(game.input, 'flush', return_value={}) as flush, mock.patch.object(game, 'close_engine') as terminate:
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
        with mock.patch.object(game.input, 'pending', return_value=[{'client_id': game.client_id, 'session_id': game.session_id}]), mock.patch.object(game.input, 'flush', side_effect=RuntimeError('native unavailable')):
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


class DiscoveryTest(unittest.TestCase):
    def test_summary_pages_and_detail_schemas_are_separate(self):
        runtime = BridgeRuntime(ROOT)
        first = runtime.call_tool('automation_bridge_catalog', {})['data']
        self.assertEqual(20, len(first['items']))
        self.assertEqual('python_wrapper', first['source'])
        self.assertNotIn('arguments_schema', first['items'][0])
        self.assertNotIn('signature', first['items'][0])
        found = runtime.call_tool('automation_bridge_catalog', {'query': 'Client elements_page'})['data']['items']
        operation = 'automation_bridge.engine.Client.elements_page'
        self.assertIn(operation, [item['id'] for item in found])
        detail = runtime.call_tool('automation_bridge_describe', {'operation': operation})['data']
        self.assertIn('signature', detail)
        self.assertFalse(detail['arguments_schema']['additionalProperties'])
        self.assertEqual(500, detail['arguments_schema']['properties']['limit']['maximum'])
        self.assertTrue(detail['arguments_schema']['properties']['cursor']['pattern'])
        resource = json.loads(runtime.read_resource('automation-bridge://api/catalog')['contents'][0]['text'])
        self.assertEqual(first, resource)
        runtime.cleanup()

    def test_protocol_tool_pages_are_complete_and_reject_bad_cursors(self):
        from automation_bridge.mcp_protocol import McpProtocol, ProtocolError
        runtime = BridgeRuntime(ROOT)
        protocol = McpProtocol(runtime)
        names, cursor = [], None
        while True:
            page = protocol._list_tools({} if cursor is None else {'cursor': cursor})
            self.assertEqual(len(runtime.tool_descriptors()), len(page['tools']))
            names.extend(item['name'] for item in page['tools'])
            cursor = page.get('nextCursor')
            if cursor is None:
                break
        self.assertEqual([item['name'] for item in runtime.tool_descriptors()], names)
        for cursor in ('resources:1', 'tools:-1', 'tools:999999'):
            with self.assertRaises(ProtocolError):
                protocol._list_tools({'cursor': cursor})
        runtime.cleanup()

    def test_focused_schema_rejects_malformed_values_before_io(self):
        runtime = BridgeRuntime(ROOT)
        game = engine.Client(54321)
        wire = serialize(game, runtime.handles)
        for tool, method, arguments in (
            ('defold_key', 'key', {'key': 'M', 'hold': True}),
            ('defold_key', 'key', {'key': 'M', 'wait': 'typo'}),
            ('defold_key', 'key', {'key': 'M', 'wait': 'completed'}),
            ('defold_key', 'key', {'key': 'M', 'wait': -0.1}),
            ('defold_click', 'click', {'target': [1, 2, 3]}),
            ('defold_screenshot', 'screenshot', {'wait': 'false'}),
        ):
            with self.subTest(tool=tool, arguments=arguments), mock.patch.object(game, method) as call:
                response = runtime.call_tool(tool, {'engine': wire, **arguments})
                self.assertEqual('invalid_arguments', response['error']['code'])
                call.assert_not_called()
        result = runtime.call_tool('automation_bridge_call', {'operation': {'bad': 'shape'}})
        self.assertEqual('invalid_arguments', result['error']['code'])
        runtime.cleanup()

    def test_focused_input_accepts_documented_wait_forms(self):
        runtime = BridgeRuntime(ROOT)
        game = engine.Client(54321)
        wire = serialize(game, runtime.handles)
        try:
            for wait in (True, False, None, 'accepted', 'started', 'released', 0, 0.25):
                with self.subTest(wait=wait), mock.patch.object(game, 'key', return_value={}) as call:
                    result = runtime.call_tool('defold_key', {'engine': wire, 'key': 'M', 'wait': wait})
                    self.assertTrue(result['ok'], result)
                    self.assertEqual(wait, call.call_args.kwargs['wait'])
        finally:
            runtime.cleanup()

    def test_adapted_operation_descriptions_explain_json_arguments(self):
        runtime = BridgeRuntime(ROOT)
        for operation, parameter in (
            ('automation_bridge.engine.Client.require', 'capabilities'),
            ('automation_bridge.engine.Client.reboot', 'args'),
            ('automation_bridge.engine.wait_until', 'operation'),
            ('automation_bridge.engine.ProfilerRecording.abort', 'cause'),
        ):
            with self.subTest(operation=operation):
                detail = runtime.call_tool('automation_bridge_describe', {'operation': operation})['data']
                self.assertIn(parameter, detail['arguments_schema']['properties'])
        runtime.cleanup()


class InstalledLayoutTest(unittest.TestCase):
    def test_cached_plugin_with_spaces_and_unrelated_cwd_uses_its_bundle(self):
        import shutil
        import subprocess
        import sys
        from tests.mcp_client import StdioClient
        with tempfile.TemporaryDirectory(prefix='automation plugin ') as directory:
            root = Path(directory)
            plugin = root / 'installed cache' / 'automation bridge'
            shutil.copytree(ROOT / 'plugins/automation-bridge', plugin, ignore=shutil.ignore_patterns('__pycache__', '*.pyc'))
            cwd = root / 'unrelated project'
            cwd.mkdir()
            (cwd / 'game.project').write_text('[project]\ntitle = Decoy\n')
            with StdioClient([sys.executable, str(plugin / 'scripts/run_mcp.py')], cwd=cwd, env={'PYTHONPATH': str(cwd)}) as client:
                self.assertEqual('2025-11-25', client.initialize()['protocolVersion'])
                tools = client.request('tools/list')['tools']
                self.assertTrue(tools)
                missing = client.tool('defold_open_project', {})['structuredContent']
                self.assertEqual('missing_argument', missing['error']['code'])
                doctor = client.tool('defold_doctor', {'project_path': str(cwd)})['structuredContent']
                self.assertTrue(doctor['ok'], doctor)
                self.assertIn(str(cwd), json.dumps(doctor['data']))
            self.assertEqual(0, client.process.returncode)
            subprocess.run([sys.executable, str(plugin / 'scripts/validate_plugin.py')], cwd=cwd,
                           check=True, capture_output=True, text=True)


class IdleObserverCleanupTest(unittest.TestCase):
    def test_closing_idle_observer_never_acquires_a_native_input_lease(self):
        runtime = BridgeRuntime(ROOT)
        game = engine.Client(54321)
        wire = serialize(game, runtime.handles)
        with mock.patch.object(game.input, 'pending', return_value=[{'client_id': 'another-client', 'session_id': 'work'}]), mock.patch.object(game.input, 'flush') as flush:
            result = runtime.call_tool('defold_close', {'engine': wire})
            self.assertTrue(result['ok'], result)
            flush.assert_not_called()
        runtime.cleanup()

    def test_cancelling_observations_does_not_acquire_input_control(self):
        runtime = BridgeRuntime(ROOT)
        self.addCleanup(runtime.cleanup)
        game = engine.Client(54321)
        self.addCleanup(game.close)
        wire = serialize(game, runtime.handles)

        def observe():
            runtime.cancel('observer')
            return {'ready': False}

        for receipts in ([], [{'client_id': 'other', 'session_id': game.session_id}],
                         [{'client_id': game.client_id, 'session_id': 'other'}]):
            with self.subTest(receipts=receipts), mock.patch.object(game, 'health', side_effect=observe), \
                 mock.patch.object(game.input, 'pending', return_value=receipts), \
                 mock.patch.object(game.input, 'flush') as flush:
                result = runtime.call_tool_request('observer', 'defold_health', {'engine': wire})
                self.assertEqual('operation_cancelled', result['error']['code'])
                self.assertNotIn('cleanup_error', result['error'].get('data', {}))
                flush.assert_not_called()


class ContextCleanupEvidenceTest(unittest.TestCase):
    def test_exceptional_pointer_exit_reports_refusal_and_can_be_retried(self):
        runtime = BridgeRuntime(ROOT)
        self.addCleanup(runtime.cleanup)
        game = engine.Client(54321)
        self.addCleanup(game.close)
        pointer = engine.PointerSession(game, engine.InputReceipt({'input_id': 7, 'state': 'started'}), lease=5)
        wire = serialize(pointer, runtime.handles)
        self.assertTrue(runtime.call_tool('automation_bridge_enter', {'target': wire})['ok'])
        with mock.patch.object(game.input, 'cancel', side_effect=RuntimeError('native cleanup refused')):
            result = runtime.call_tool('automation_bridge_exit', {'target': wire, 'error': 'interrupted'})
        self.assertEqual('cleanup_failed', result['error']['code'])
        self.assertFalse(pointer.closed)
        self.assertIs(pointer, runtime.handles.get(wire['$handle']))
        info = runtime.call_tool('automation_bridge_session', {})['data']
        self.assertIn('native cleanup refused', info['cleanup_errors'][-1]['error']['message'])
        with mock.patch.object(game.input, 'cancel', return_value=engine.InputReceipt({'input_id': 7, 'state': 'cancelled'})) as cancel:
            result = runtime.call_tool('automation_bridge_exit', {'target': wire, 'error': 'interrupted'})
        self.assertTrue(result['ok'], result)
        self.assertTrue(pointer.closed)
        cancel.assert_called_once_with(7, release=True)

    def test_session_close_retains_entered_pointer_cleanup_errors(self):
        runtime = BridgeRuntime(ROOT)
        self.addCleanup(runtime.cleanup)
        game = engine.Client(54321)
        self.addCleanup(game.close)
        pointer = engine.PointerSession(game, engine.InputReceipt({'input_id': 7, 'state': 'started'}), lease=5)
        wire = serialize(pointer, runtime.handles)
        self.assertTrue(runtime.call_tool('automation_bridge_enter', {'target': wire})['ok'])
        with mock.patch.object(game.input, 'cancel', side_effect=RuntimeError('native cleanup refused')):
            result = runtime.call_tool('automation_bridge_session', {'action': 'close'})
        self.assertTrue(result['ok'], result)
        self.assertTrue(result['data']['closed'])
        self.assertEqual(0, result['data']['handles'])
        self.assertIn('native cleanup refused', result['data']['cleanup_errors'][-1]['error']['message'])

    def test_interruption_scope_keeps_cleanup_failure_on_original_cancellation(self):
        game = engine.Client(54321)
        self.addCleanup(game.close)
        original = engine.OperationCancelled('interrupted')
        refusal = RuntimeError('native cleanup refused')
        with mock.patch.object(game.input, 'flush', side_effect=refusal):
            with self.assertRaises(engine.OperationCancelled) as caught:
                with game.input.interruption_scope():
                    raise original
        self.assertIs(original, caught.exception)
        self.assertIs(refusal, caught.exception.cleanup_error)
