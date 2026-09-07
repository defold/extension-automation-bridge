"""Local editor HTTP fixture for exercising discovery and command transport."""

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


class EditorHttpFixture:
    def __init__(self, document, respond):
        self.document = document
        self.respond = respond
        self.requests = []

    def __enter__(self):
        fixture = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                self.dispatch()

            def do_POST(self):
                self.dispatch()

            def log_message(self, *args):
                pass

            def dispatch(self):
                body = self.rfile.read(int(self.headers.get("Content-Length", "0")))
                request = {"method": self.command, "path": self.path,
                           "headers": dict(self.headers), "body": body}
                fixture.requests.append(request)
                if self.command == "GET" and self.path == "/openapi.json":
                    status, response = 200, fixture.document
                else:
                    status, response = fixture.respond(request)
                if isinstance(response, bytes):
                    content_type = "text/plain"
                else:
                    response = json.dumps(response).encode("utf-8")
                    content_type = "application/json"
                self.send_response(status)
                self.send_header("Content-Type", content_type)
                self.send_header("Content-Length", str(len(response)))
                self.end_headers()
                self.wfile.write(response)

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.port = self.server.server_port
        self.thread = threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True)
        self.thread.start()
        return self

    def __exit__(self, *args):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=1)
