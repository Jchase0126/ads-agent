"""Fake OpenAI-compatible endpoints for testing /test_connection (no real API).

Serves:
  :8990  GET /models          -> 200 {"data": [{"id": "m-b"}, {"id": "m-a"}]}
                               401 when Authorization header is wrong/missing
  :8992  GET /anything-else   -> 200; /models -> 404 (service without model list)

Run:  python tests/fake_api_server.py
"""

import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


class OkHandler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _json(self, obj, code=200):
        data = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        if self.path == "/models":
            auth = self.headers.get("Authorization", "")
            if auth != "Bearer good-key":
                self._json({"error": {"message": "invalid api key"}}, 401)
                return
            self._json({"data": [{"id": "m-b"}, {"id": "m-a"}, {"id": "m-a"}]})
        else:
            self._json({"error": "not found"}, 404)


class NoModelsHandler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_GET(self):
        self.send_response(404)
        self.send_header("Content-Length", "0")
        self.end_headers()


def main():
    ThreadingHTTPServer(("127.0.0.1", 8990), OkHandler).serve_forever()


def main2():
    ThreadingHTTPServer(("127.0.0.1", 8992), NoModelsHandler).serve_forever()


if __name__ == "__main__":
    import threading

    threading.Thread(target=main2, daemon=True).start()
    main()
