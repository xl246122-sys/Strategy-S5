"""Read-only paper state. The bind address is localhost or the LAN."""

import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from bstocks_v2.console.hub import Hub

_PAGE = Path(__file__).with_name("index.html")


def serve(hub: Hub, host: str = "127.0.0.1", port: int = 8765) -> None:
    page = _PAGE.read_text(encoding="utf-8")

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            path = self.path.split("?", 1)[0]
            if path == "/api/state":
                body = json.dumps(hub.state()).encode("utf-8")
                self._send(200, "application/json; charset=utf-8", body)
                return
            if path in {"/", "/index.html"}:
                self._send(200, "text/html; charset=utf-8", page.encode("utf-8"))
                return
            self._send(404, "text/plain; charset=utf-8", b"not found")

        def _send(self, status: int, content_type: str, body: bytes) -> None:
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, format: str, *args: object) -> None:
            return

    server = ThreadingHTTPServer((host, port), Handler)
    try:
        server.serve_forever()
    finally:
        server.server_close()
