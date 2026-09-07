"""Small stdio test client; keeps stdin open until calls complete."""
import json
import os
import queue
import subprocess
import threading
from collections import deque


class StdioClient:
    def __init__(self, command, *, cwd, env=None):
        self.process = subprocess.Popen(command, cwd=cwd, env={**os.environ, **(env or {})},
                                        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                        text=True, encoding='utf-8', bufsize=1)
        self.responses = queue.Queue()
        self.pending = {}
        self.errors = deque(maxlen=50)
        self._next_id = 0
        self._write_lock = threading.Lock()
        def read():
            try:
                for line in self.process.stdout:
                    self.responses.put(json.loads(line))
            except Exception as error:
                self.responses.put(error)
            finally:
                self.responses.put(EOFError('MCP process ended'))
        def stderr():
            for line in self.process.stderr:
                self.errors.append(line.rstrip())
        self.read_thread = threading.Thread(target=read, daemon=True)
        self.error_thread = threading.Thread(target=stderr, daemon=True)
        self.read_thread.start()
        self.error_thread.start()

    def send(self, method, params=None, *, notify=False):
        with self._write_lock:
            message = {'jsonrpc': '2.0', 'method': method, 'params': params or {}}
            if not notify:
                self._next_id += 1
                message['id'] = self._next_id
            self.process.stdin.write(json.dumps(message) + '\n')
            self.process.stdin.flush()
            return message.get('id')

    def receive(self, request_id, timeout=15):
        import time
        deadline = time.monotonic() + timeout
        while request_id not in self.pending:
            message = self.responses.get(timeout=max(0, deadline - time.monotonic()))
            if isinstance(message, BaseException):
                raise message
            if 'id' in message:
                self.pending[message['id']] = message
        message = self.pending.pop(request_id)
        if 'error' in message:
            raise AssertionError(message['error'])
        return message['result']

    def request(self, method, params=None, timeout=15):
        return self.receive(self.send(method, params), timeout)

    def initialize(self):
        result = self.request('initialize', {'protocolVersion': '2025-11-25', 'clientInfo': {'name': 'automation-bridge-test', 'version': '1'}, 'capabilities': {}})
        self.send('notifications/initialized', notify=True)
        return result

    def tool(self, name, arguments, *, timeout=90):
        return self.request('tools/call', {'name': name, 'arguments': arguments}, timeout)

    def close(self):
        self.process.stdin.close()
        try:
            self.process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            self.process.terminate()
            self.process.wait(timeout=5)
        self.read_thread.join(1)
        self.error_thread.join(1)
        self.process.stdout.close()
        self.process.stderr.close()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()
