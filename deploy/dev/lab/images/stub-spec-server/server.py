"""Stub spec server: serves one OpenAPI spec and its profile behind a Bearer token.

GET /specs/calculator/v1/openapi.yaml   (P1)
GET /specs/calculator/v1/profile.json   (P3 F1: {"tools": ["Add"], ...})
  - Authorization: Bearer <SPEC_TOKEN>  -> 200, body = /data/openapi.yaml | /data/profile.json
  - header missing                      -> 401 (auth=missing)
  - header present but wrong            -> 401 (auth=invalid)
Every request is logged to stdout as:
  <method> <path> auth=<present|missing|invalid> status=<NNN>
"""
from __future__ import annotations

import os
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

SPEC_PATH = os.environ.get("SPEC_PATH", "/data/openapi.yaml")
PROFILE_PATH = os.environ.get("PROFILE_PATH", "/data/profile.json")
SPEC_TOKEN = os.environ.get("SPEC_TOKEN", "")
ROUTES = {
    "/specs/calculator/v1/openapi.yaml": (SPEC_PATH, "application/yaml"),
    "/specs/calculator/v1/profile.json": (PROFILE_PATH, "application/json"),
}
PORT = int(os.environ.get("PORT", "8080"))


class Handler(BaseHTTPRequestHandler):
    server_version = "stub-spec-server/0.1"

    def log_message(self, *args):  # replaced by _log below
        pass

    def _log(self, auth: str, status: int) -> None:
        print(f"{self.command} {self.path} auth={auth} status={status}", flush=True)

    def _auth_state(self) -> str:
        header = self.headers.get("Authorization")
        if header is None:
            return "missing"
        if SPEC_TOKEN and header == f"Bearer {SPEC_TOKEN}":
            return "present"
        return "invalid"

    def _reply(self, status: int, body: bytes, ctype: str) -> None:
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        auth = self._auth_state()
        route = ROUTES.get(self.path)
        if route is None:
            self._reply(404, b"not found\n", "text/plain")
            self._log(auth, 404)
            return
        file_path, ctype = route
        if auth != "present":
            self.send_response(401)
            self.send_header("WWW-Authenticate", 'Bearer realm="spec"')
            self.send_header("Content-Type", "text/plain")
            self.send_header("Content-Length", "13")
            self.end_headers()
            self.wfile.write(b"unauthorized\n")
            self._log(auth, 401)
            return
        try:
            with open(file_path, "rb") as fh:
                body = fh.read()
        except OSError as exc:
            msg = f"file unreadable: {exc}\n".encode()
            self._reply(500, msg, "text/plain")
            self._log(auth, 500)
            return
        self._reply(200, body, ctype)
        self._log(auth, 200)


def main() -> None:
    if not SPEC_TOKEN:
        print("SPEC_TOKEN is empty; every request will be rejected", flush=True)
    print(f"stub-spec-server listening on :{PORT}, spec={SPEC_PATH}, profile={PROFILE_PATH}", flush=True)
    ThreadingHTTPServer(("0.0.0.0", PORT), Handler).serve_forever()


if __name__ == "__main__":
    sys.exit(main())
