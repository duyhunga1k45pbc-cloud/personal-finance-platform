import base64
import json
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


_active_server = None


class MockHandler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def send_json(self, status, data):
        body = json.dumps(data).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        if self.path != "/internal/gateway/authorize":
            return self.send_json(404, {"error": "not found"})

        bearer = self.headers.get("Authorization", "")
        token = bearer.removeprefix("Bearer ").split(".")

        if len(token) != 3:
            return self.send_json(401, {"error": "invalid token"})

        try:
            payload = token[1]
            claims = json.loads(base64.urlsafe_b64decode(
                payload + "=" * (-len(payload) % 4)
            ))
            subject = claims["sub"]
        except (ValueError, KeyError):
            return self.send_json(401, {"error": "invalid claims"})

        response_mode = self.server.authz_response_mode
        if response_mode == "malformed":
            body = b"{invalid-json"
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return

        if response_mode == "http500":
            return self.send_json(500, {"error": "authorization failed"})

        # This is a test mock, not a production JWT verifier.
        if subject == "allow":
            return self.send_json(200, {"allow": True})

        if subject == "deny":
            return self.send_json(200, {"allow": False})

        if subject == "slow":
            time.sleep(2)
            return self.send_json(200, {"allow": True})

        if subject == "connection_failure":
            self.close_connection = True
            return

        return self.send_json(403, {"allow": False})

    def do_GET(self):
        if self.path == "/accounts":
            return self.send_json(200, {"accounts": []})

        return self.send_json(404, {"error": "not found"})


def start_mock_server():
    global _active_server
    server = ThreadingHTTPServer(("0.0.0.0", 18999), MockHandler)
    server.daemon_threads = True
    server.authz_response_mode = None
    _active_server = server
    return server


def set_authz_response_mode(mode):
    if _active_server is None:
        raise RuntimeError("Mock authorization server is not running")
    _active_server.authz_response_mode = mode
