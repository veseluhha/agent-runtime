import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.error import HTTPError
from urllib.request import Request, urlopen


class Fault(Exception):
    def __init__(self, kind, message, status=400):
        super().__init__(message)
        self.kind = kind
        self.status = status


def post(base, path, body, token=None, timeout=30):
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = "Bearer " + token
    req = Request(base + path, json.dumps(body).encode(), headers)
    try:
        with urlopen(req, timeout=timeout) as response:
            return json.load(response)
    except HTTPError as exc:
        error = json.load(exc)
        raise Fault(error["error"], error["message"], exc.code) from exc
    except OSError as exc:
        raise Fault("transport", "connection failed", 502) from exc


def serve(dispatch, health=lambda: None):
    class Handler(BaseHTTPRequestHandler):
        def setup(self):
            super().setup()
            self.connection.settimeout(10)

        def respond(self, action):
            try:
                data = action()
                status = 200
            except Fault as exc:
                status, data = exc.status, {"error": exc.kind, "message": str(exc)}
            except (ValueError, KeyError, TypeError):
                status, data = 400, {"error": "invalid", "message": "invalid request"}
            except Exception:
                self.log_error("unexpected server error")
                status, data = 500, {"error": "internal", "message": "server error"}
            encoded = json.dumps(data, ensure_ascii=False).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(encoded)))
            self.end_headers()
            self.wfile.write(encoded)

        def do_GET(self):
            def check():
                if self.path != "/health":
                    raise Fault("unknown", "unknown endpoint", 404)
                health()
                return {"ok": True}
            self.respond(check)

        def do_POST(self):
            def handle():
                size = int(self.headers.get("Content-Length", 0))
                if not 0 < size <= 131072:
                    raise Fault("invalid", "invalid body size", 413)
                body = json.loads(self.rfile.read(size))
                if not isinstance(body, dict):
                    raise ValueError
                return dispatch(self.path, body, self.headers)
            self.respond(handle)

    ThreadingHTTPServer(("0.0.0.0", 8080), Handler).serve_forever()
