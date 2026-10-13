"""Local stand-in for POST /v1/decisions, used by the unit tests and the
real-Splunk end-to-end check. Standard library only.

Behaviour is chosen by markers in the input text:

    REFUSE     -> a refusal answer
    HTTP500    -> HTTP 500
    FLAKY      -> HTTP 503 on the first request for that input, then success
    RATELIMIT  -> HTTP 429 (Retry-After: 0) every time
    BADJSON    -> HTTP 200 with a body that is not JSON
    SLOW       -> sleeps 3 seconds before answering

Otherwise the probability is 0.95 when the input mentions "fail", "denied" or
"invalid", else 0.05. A wrong bearer token gets HTTP 401.

GET /stats returns request counts; POST /reset clears them.

    python3 stub_decisions_api.py --port 8765 --api-key sk-test
"""

import argparse
import json
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
from socketserver import ThreadingMixIn

YES_WORDS = ("fail", "denied", "invalid")


class StubState:
    def __init__(self, api_key):
        self.api_key = api_key
        self.lock = threading.Lock()
        self.reset()

    def reset(self):
        with self.lock:
            self.requests = 0  # every POST to /decisions, including rejected ones
            self.by_input = {}
            self.bodies = []

    def count(self):
        with self.lock:
            self.requests += 1

    def record(self, text, body):
        with self.lock:
            self.by_input[text] = self.by_input.get(text, 0) + 1
            self.bodies.append(body)
            return self.by_input[text]


class Handler(BaseHTTPRequestHandler):
    server_version = "stub-decisions/1"

    def log_message(self, format, *args):
        pass

    def _send(self, status, payload, headers=None):
        body = payload if isinstance(payload, bytes) else json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        for name, value in (headers or {}).items():
            self.send_header(name, value)
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass  # the client gave up waiting (timeout tests)

    def _error(self, status, message, code=None, headers=None):
        self._send(
            status,
            {"error": {"message": message, "type": "invalid_request_error", "code": code}},
            headers,
        )

    def do_GET(self):
        state = self.server.state
        if self.path.rstrip("/").endswith("/stats"):
            with state.lock:
                self._send(
                    200,
                    {
                        "requests": state.requests,
                        "by_input": state.by_input,
                        "bodies": state.bodies,
                    },
                )
            return
        self._error(404, "Not found")

    def do_POST(self):
        state = self.server.state
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length)
        if self.path.rstrip("/").endswith("/reset"):
            state.reset()
            self._send(200, {"ok": True})
            return
        if not self.path.rstrip("/").endswith("/decisions"):
            self._error(404, f"Unknown path {self.path}")
            return
        state.count()
        if self.headers.get("Authorization") != "Bearer " + state.api_key:
            self._error(401, "Incorrect API key provided.", "invalid_api_key")
            return
        try:
            body = json.loads(raw.decode("utf-8"))
            text = body["input"]
            (question,) = body["questions"]
            assert body["model"] and question["type"] == "predicate"
            name = question["name"]
            assert question["instructions"]
        except Exception as e:
            self._error(400, f"Invalid request body: {e}")
            return

        attempt = state.record(text, body)
        if "SLOW" in text:
            time.sleep(3)
        if "HTTP500" in text:
            self._error(500, "The server had an error while processing your request.")
        elif "FLAKY" in text and attempt == 1:
            self._error(503, "The engine is currently overloaded.")
        elif "RATELIMIT" in text:
            self._error(429, "Rate limit reached.", "rate_limit_exceeded", {"Retry-After": "0"})
        elif "BADJSON" in text:
            self._send(200, b"<html>not json</html>")
        elif "REFUSE" in text:
            self._send(200, {"answers": [{"type": "refusal", "name": name}]})
        else:
            yes = any(word in text.lower() for word in YES_WORDS)
            self._send(
                200,
                {
                    "answers": [
                        {"type": "predicate", "name": name, "probability": 0.95 if yes else 0.05}
                    ]
                },
            )


class StubServer(ThreadingMixIn, HTTPServer):
    daemon_threads = True

    def __init__(self, address, api_key):
        HTTPServer.__init__(self, address, Handler)
        self.state = StubState(api_key)

    @property
    def base_url(self):
        host, port = self.server_address[:2]
        return f"http://{host}:{port}/v1"


def serve_in_thread(api_key, host="127.0.0.1", port=0):
    server = StubServer((host, port), api_key)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--api-key", required=True)
    args = parser.parse_args()
    server = StubServer((args.host, args.port), args.api_key)
    print(f"stub Decisions API listening on {server.base_url}", flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
