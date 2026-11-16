"""Mock Petstore (Swagger 2.0 sample) on :18081. In-memory, standard library only.

Serves every operation in the sample spec under basePath /v2 so a converted
MCP server can be called end to end without reaching petstore.swagger.io.
Data resets when the pod restarts.
"""
from __future__ import annotations

import json
import re
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Lock
from urllib.parse import parse_qs, urlsplit

HOST, PORT, BASE = "0.0.0.0", 18081, "/v2"
_lock = Lock()
pets: dict[int, dict] = {
    1: {"id": 1, "name": "doggie", "status": "available", "photoUrls": ["http://img/1.jpg"],
        "category": {"id": 1, "name": "Dogs"}, "tags": [{"id": 1, "name": "tag1"}]},
    2: {"id": 2, "name": "kitty", "status": "pending", "photoUrls": [],
        "category": {"id": 2, "name": "Cats"}, "tags": [{"id": 2, "name": "tag2"}]},
    3: {"id": 3, "name": "goldie", "status": "sold", "photoUrls": [], "category": {"id": 3, "name": "Fish"}, "tags": []},
}
orders: dict[int, dict] = {1: {"id": 1, "petId": 1, "quantity": 1, "shipDate": "2026-09-05T00:00:00Z", "status": "placed", "complete": False}}
users: dict[str, dict] = {"user1": {"id": 1, "username": "user1", "firstName": "Test", "lastName": "User",
                                    "email": "user1@example.com", "password": "x", "phone": "010", "userStatus": 1}}
_next = {"pet": 100, "order": 100, "user": 100}


def _nid(kind: str) -> int:
    _next[kind] += 1
    return _next[kind]


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        print(f"{self.address_string()} {fmt % args}", flush=True)

    def _send(self, code: int, body=None):
        data = b"" if body is None else json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        if data:
            self.wfile.write(data)

    def _json(self):
        n = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(n) if n else b""
        ctype = self.headers.get("Content-Type", "")
        if "json" in ctype and raw:
            return json.loads(raw)
        if "x-www-form-urlencoded" in ctype and raw:
            return {k: v[0] for k, v in parse_qs(raw.decode()).items()}
        return {"_raw_len": len(raw)}

    def _route(self, method: str):
        u = urlsplit(self.path)
        path = u.path[len(BASE):] if u.path.startswith(BASE) else u.path
        q = {k: v for k, v in parse_qs(u.query).items()}
        with _lock:
            return self._dispatch(method, path, q)

    def _dispatch(self, m, p, q):
        # --- pet ---
        if m == "POST" and re.fullmatch(r"/pet/\d+/uploadImage", p):
            pid = int(p.split("/")[2])
            if pid not in pets:
                return 404, {"code": 404, "type": "error", "message": "Pet not found"}
            body = self._json()
            return 200, {"code": 200, "type": "unknown", "message": f"uploaded {body.get('_raw_len', 0)} bytes for pet {pid}"}
        if p == "/pet" and m in ("POST", "PUT"):
            body = self._json()
            if not body.get("name") or "photoUrls" not in body:
                return 405, {"code": 405, "type": "error", "message": "Invalid input"}
            if m == "PUT" and body.get("id") not in pets:
                return 404, {"code": 404, "type": "error", "message": "Pet not found"}
            pid = int(body.get("id") or _nid("pet"))
            pets[pid] = {**body, "id": pid}
            return 200, pets[pid]
        if m == "GET" and p == "/pet/findByStatus":
            wanted = set(q.get("status", []))
            return 200, [x for x in pets.values() if x.get("status") in wanted]
        if m == "GET" and p == "/pet/findByTags":
            wanted = set(q.get("tags", []))
            return 200, [x for x in pets.values() if {t.get("name") for t in x.get("tags", [])} & wanted]
        mm = re.fullmatch(r"/pet/(\d+)", p)
        if mm:
            pid = int(mm.group(1))
            if m == "GET":
                return (200, pets[pid]) if pid in pets else (404, {"code": 404, "type": "error", "message": "Pet not found"})
            if m == "POST":
                if pid not in pets:
                    return 405, {"code": 405, "type": "error", "message": "Invalid input"}
                body = self._json()
                for k in ("name", "status"):
                    if body.get(k):
                        pets[pid][k] = body[k]
                return 200, pets[pid]
            if m == "DELETE":
                return (200, {"code": 200, "type": "unknown", "message": str(pid)}) if pets.pop(pid, None) else (404, {"code": 404, "type": "error", "message": "Pet not found"})
        # --- store ---
        if m == "GET" and p == "/store/inventory":
            inv: dict[str, int] = {}
            for x in pets.values():
                inv[x.get("status", "unknown")] = inv.get(x.get("status", "unknown"), 0) + 1
            return 200, inv
        if m == "POST" and p == "/store/order":
            body = self._json()
            oid = int(body.get("id") or _nid("order"))
            orders[oid] = {"status": "placed", "complete": False, **body, "id": oid}
            return 200, orders[oid]
        mo = re.fullmatch(r"/store/order/(-?\d+)", p)
        if mo:
            oid = int(mo.group(1))
            if m == "GET":
                if not 1 <= oid <= 10:
                    return 400, {"code": 400, "type": "error", "message": "Invalid ID supplied"}
                return (200, orders[oid]) if oid in orders else (404, {"code": 404, "type": "error", "message": "Order not found"})
            if m == "DELETE":
                if oid < 1:
                    return 400, {"code": 400, "type": "error", "message": "Invalid ID supplied"}
                return (200, {"code": 200, "type": "unknown", "message": str(oid)}) if orders.pop(oid, None) else (404, {"code": 404, "type": "error", "message": "Order not found"})
        # --- user ---
        if m == "POST" and p in ("/user", "/user/createWithList", "/user/createWithArray"):
            body = self._json()
            items = body if isinstance(body, list) else [body]
            for u in items:
                if u.get("username"):
                    users[u["username"]] = {**u, "id": u.get("id") or _nid("user")}
            return 200, {"code": 200, "type": "unknown", "message": str(len(items))}
        if m == "GET" and p == "/user/login":
            return 200, "logged in user session:" + (q.get("username", ["?"])[0])
        if m == "GET" and p == "/user/logout":
            return 200, {"code": 200, "type": "unknown", "message": "ok"}
        mu = re.fullmatch(r"/user/([^/]+)", p)
        if mu:
            name = mu.group(1)
            if m == "GET":
                return (200, users[name]) if name in users else (404, {"code": 404, "type": "error", "message": "User not found"})
            if m == "PUT":
                if name not in users:
                    return 404, {"code": 404, "type": "error", "message": "User not found"}
                users[name] = {**users[name], **self._json(), "username": name}
                return 200, users[name]
            if m == "DELETE":
                return (200, {"code": 200, "type": "unknown", "message": name}) if users.pop(name, None) else (404, {"code": 404, "type": "error", "message": "User not found"})
        if p in ("/", "/healthz"):
            return 200, {"status": "ok"}
        return 404, {"code": 404, "type": "error", "message": f"no route {m} {p}"}

    def do_GET(self):
        self._send(*self._route("GET"))

    def do_POST(self):
        self._send(*self._route("POST"))

    def do_PUT(self):
        self._send(*self._route("PUT"))

    def do_DELETE(self):
        self._send(*self._route("DELETE"))


def main() -> None:
    print(f"mock-petstore listening on {HOST}:{PORT}{BASE}", flush=True)
    ThreadingHTTPServer((HOST, PORT), Handler).serve_forever()


if __name__ == "__main__":
    sys.exit(main())
