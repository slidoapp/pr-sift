"""Serve one PR's report and its review state on localhost.

Only requests addressed to this server (by Host header) and carrying
the per-session token are answered, so other web pages open in the
browser can neither read the report nor change review state.
"""

import json
import mimetypes
import secrets
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from urllib.parse import unquote, urlsplit

from reviewer.report import FLAGGED, REVIEWED, page_data
from reviewer.store import Store

WEB = Path(__file__).parent / "web"
CSP = (
    "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; "
    "img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'"
)


class ReportServer(HTTPServer):
    def __init__(self, store: Store, pr_id: int, port: int = 0):
        super().__init__(("127.0.0.1", port), Handler)
        self.store = store
        self.pr_id = pr_id
        self.token = secrets.token_urlsafe(32)
        self.static = {p.relative_to(WEB).as_posix(): p for p in WEB.rglob("*") if p.is_file()}

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.server_port}/"


class Handler(BaseHTTPRequestHandler):
    server: ReportServer

    def log_message(self, format, *args):
        pass

    def do_GET(self):
        path = self._checked_path()
        if path is None:
            return
        if path == "/":
            page = (WEB / "index.html").read_text().replace("__TOKEN__", self.server.token)
            self._send(HTTPStatus.OK, page.encode(), "text/html; charset=utf-8")
        elif path.startswith("/static/") and (file := self.server.static.get(path.removeprefix("/static/"))):
            kind = mimetypes.guess_type(file.name)[0] or "application/octet-stream"
            self._send(HTTPStatus.OK, file.read_bytes(), kind)
        elif path == "/api/pr":
            if self._authorized():
                self._json(HTTPStatus.OK, page_data(self.server.store, self.server.pr_id))
        else:
            self._json(HTTPStatus.NOT_FOUND, {"error": "not found"})

    def do_PUT(self):
        path = self._checked_path()
        if path is None or not self._authorized():
            return
        prefix, suffix = "/api/units/", "/review"
        if not (path.startswith(prefix) and path.endswith(suffix)):
            return self._json(HTTPStatus.NOT_FOUND, {"error": "not found"})
        qualname = unquote(path[len(prefix) : -len(suffix)])
        try:
            body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))))
        except (ValueError, json.JSONDecodeError):
            return self._json(HTTPStatus.BAD_REQUEST, {"error": "invalid JSON"})
        self._update_review(qualname, body)

    def _update_review(self, qualname, body):
        store, pr_id = self.server.store, self.server.pr_id
        unit = next((u for u in store.units(pr_id) if u["qualname"] == qualname), None)
        if unit is None:
            return self._json(HTTPStatus.NOT_FOUND, {"error": f"no unit {qualname}"})
        if body.get("fingerprint") != unit["fingerprint"]:
            return self._json(HTTPStatus.CONFLICT, {"error": "the unit changed; reload the report"})
        for key, status in (("reviewed", REVIEWED), ("flagged", FLAGGED)):
            if key not in body:
                continue
            if body[key]:
                store.set_review(pr_id, qualname, unit["fingerprint"], unit["new_body"], status)
            else:
                store.clear_review(pr_id, qualname, status)
        view = next(u for u in page_data(store, pr_id)["units"] if u["qualname"] == qualname)
        self._json(HTTPStatus.OK, view)

    def _checked_path(self):
        """The request path, or None after rejecting a foreign Host."""
        port = self.server.server_port
        if self.headers.get("Host") not in (f"127.0.0.1:{port}", f"localhost:{port}"):
            self._json(HTTPStatus.FORBIDDEN, {"error": "unexpected Host"})
            return None
        return urlsplit(self.path).path

    def _authorized(self):
        if secrets.compare_digest(self.headers.get("X-Reviewer-Token", ""), self.server.token):
            return True
        self._json(HTTPStatus.FORBIDDEN, {"error": "missing or wrong token"})
        return False

    def _json(self, status, data):
        self._send(status, json.dumps(data).encode(), "application/json")

    def _send(self, status, body, content_type):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Content-Security-Policy", CSP)
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)
