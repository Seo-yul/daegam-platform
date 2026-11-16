"""JWKS 와 토큰을 내는 실험 서버 (P32 F0 스파이크). ToolHive 검증기가 JWKS 를 언제 다시 받는지 잰다.

키 묶음은 서버 메모리에 둔다. ConfigMap 볼륨은 kubelet 동기화가 수십 초 늦어 키를 넣고 뺀 시각이
흐려지므로 쓰지 않는다. 경로의 첫 조각이 채널이고, 채널마다 키 묶음과 응답 상태와 `Cache-Control`
이 따로다. 검증기마다 채널 하나를 주면 서버 하나로 여러 조건을 나란히 잰다.

  GET    /<ch>/.well-known/jwks.json[?...]   그 채널의 JWKS. 쿼리는 무시하고 기록만 한다
  POST   /admin/<ch>/keys    {"alg": "ES256"|"RS256"}                  -> {"kid"}
  DELETE /admin/<ch>/keys/<kid>
  PUT    /admin/<ch>/mode    {"status": 200|503, "cache_control": str|null}
  POST   /admin/<ch>/token   {"kid", "iss", "aud", "sub", "groups", "ttl"} -> {"token"}

관리 경로는 port-forward 로만 부른다. JWKS 조회와 관리 동작은 stdout 에 한 줄씩 남으므로
`kubectl logs` 가 시각표다. 새 채널의 기본은 200 에 캐시 헤더 없음이다.

실행: python jwks_lab.py [--port 8080]
  클러스터에서는 플랫폼 이미지에 이 파일을 ConfigMap 으로 얹어 띄우고, 로컬에서는
  uv run --project platform python deploy/dev/lab/jwks_lab.py 로 띄운다.
"""

from __future__ import annotations

import argparse
import json
import secrets
import sys
import threading
import time
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import urlsplit

import jwt
from cryptography.hazmat.primitives.asymmetric import ec
from jwt.algorithms import ECAlgorithm, RSAAlgorithm

from daegam.auth.signer import ACCESS_TOKEN_TYP, PlatformSigner, registered_claims

JWKS_SUFFIX = "/.well-known/jwks.json"
DEFAULT_TTL = 24 * 3600
KEY_TYPES = {
    "ES256": (lambda: ec.generate_private_key(ec.SECP256R1()), ECAlgorithm),
    "RS256": (lambda: PlatformSigner.generate().private_key, RSAAlgorithm),
}


@dataclass
class LabKey:
    alg: str
    private_key: Any
    kid: str = field(default_factory=lambda: f"lab-{secrets.token_hex(8)}")

    def jwk(self) -> dict[str, Any]:
        out = KEY_TYPES[self.alg][1].to_jwk(self.private_key.public_key(), as_dict=True)
        out.update({"kid": self.kid, "use": "sig", "alg": self.alg})
        return out

    def sign(self, claims: dict[str, Any]) -> str:
        headers = {"kid": self.kid, "typ": ACCESS_TOKEN_TYP}
        return jwt.encode(claims, self.private_key, algorithm=self.alg, headers=headers)


@dataclass
class Channel:
    keys: dict[str, LabKey] = field(default_factory=dict)
    status: int = 200
    cache_control: str | None = None


class Lab:
    def __init__(self) -> None:
        self.channels: dict[str, Channel] = {}
        self.lock = threading.Lock()

    def channel(self, name: str) -> Channel:
        with self.lock:
            return self.channels.setdefault(name, Channel())

    def jwks(self, name: str) -> tuple[int, dict[str, str], dict[str, Any]]:
        ch = self.channel(name)
        headers = {"Cache-Control": ch.cache_control} if ch.cache_control else {}
        if ch.status != 200:
            return ch.status, headers, {"detail": {"code": "jwks_unavailable"}}
        return 200, headers, {"keys": [k.jwk() for k in list(ch.keys.values())]}

    def add_key(self, name: str, alg: str) -> LabKey:
        if alg not in KEY_TYPES:
            raise ValueError(f"alg must be one of {sorted(KEY_TYPES)}")
        key = LabKey(alg, KEY_TYPES[alg][0]())
        self.channel(name).keys[key.kid] = key
        return key

    def mint(self, name: str, body: dict[str, Any]) -> str:
        key = self.channel(name).keys[body["kid"]]
        claims = registered_claims(
            issuer=body["iss"],
            audience=body.get("aud", "daegam-mcp"),
            sub=body.get("sub", "lab-user"),
            ttl=int(body.get("ttl", DEFAULT_TTL)),
        )
        claims["groups"] = list(body.get("groups", []))
        return key.sign(claims)


def log(event: str, **fields: Any) -> None:
    stamp = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    print(" ".join([stamp, event, *(f"{k}={v}" for k, v in fields.items())]), flush=True)


class Handler(BaseHTTPRequestHandler):
    lab: Lab

    def log_message(self, format: str, *args: Any) -> None:
        return  # 우리 log() 한 줄만 남긴다

    def _send(
        self, status: int, body: dict[str, Any], headers: dict[str, str] | None = None
    ) -> None:
        raw = json.dumps(body).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(raw)

    def _body(self) -> dict[str, Any]:
        size = int(self.headers.get("Content-Length") or 0)
        return json.loads(self.rfile.read(size) or b"{}")

    def _admin(self) -> list[str] | None:
        """`/admin/<ch>/<what>[/<id>]` 이면 [ch, what, id...], 아니면 None."""
        parts = urlsplit(self.path).path.strip("/").split("/")
        return parts[1:] if len(parts) >= 3 and parts[0] == "admin" else None

    def do_GET(self) -> None:
        url = urlsplit(self.path)
        if not url.path.endswith(JWKS_SUFFIX):
            return self._send(404, {"detail": "not found"})
        name = url.path[: -len(JWKS_SUFFIX)].strip("/") or "default"
        status, headers, body = self.lab.jwks(name)
        log(
            "jwks_fetch",
            ch=name,
            client=self.client_address[0],
            status=status,
            query=url.query or "-",
            kids=",".join(k["kid"] for k in body.get("keys", [])) or "-",
        )
        self._send(status, body, headers)

    def do_POST(self) -> None:
        parts = self._admin()
        if parts is None or len(parts) != 2:
            return self._send(404, {"detail": "not found"})
        name, what = parts
        body = self._body()
        try:
            if what == "keys":
                key = self.lab.add_key(name, body.get("alg", "ES256"))
                log("key_added", ch=name, kid=key.kid, alg=key.alg)
                return self._send(201, {"kid": key.kid})
            if what == "token":
                return self._send(200, {"token": self.lab.mint(name, body)})
        except (KeyError, ValueError) as e:
            return self._send(400, {"detail": str(e)})
        self._send(404, {"detail": "not found"})

    def do_PUT(self) -> None:
        parts = self._admin()
        if parts is None or parts[1:] != ["mode"]:
            return self._send(404, {"detail": "not found"})
        body = self._body()
        ch = self.lab.channel(parts[0])
        ch.status = int(body.get("status", 200))
        ch.cache_control = body.get("cache_control") or None
        log("mode", ch=parts[0], status=ch.status, cache_control=json.dumps(ch.cache_control))
        self._send(200, {"status": ch.status, "cache_control": ch.cache_control})

    def do_DELETE(self) -> None:
        parts = self._admin()
        if parts is None or len(parts) != 3 or parts[1] != "keys":
            return self._send(404, {"detail": "not found"})
        removed = self.lab.channel(parts[0]).keys.pop(parts[2], None)
        log("key_removed", ch=parts[0], kid=parts[2], found=removed is not None)
        self._send(200 if removed else 404, {"kid": parts[2]})


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("--port", type=int, default=8080)
    a = ap.parse_args(argv[1:])
    Handler.lab = Lab()
    log("listening", port=a.port)
    ThreadingHTTPServer(("0.0.0.0", a.port), Handler).serve_forever()
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
