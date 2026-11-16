"""허브 레지스트리의 테스트 대역 (P55 6절). F0 4번의 실제 cosign 3 서명(`fixtures/hub_signatures`)을
OCI distribution API 의 모양으로 낸다. 서명한 이미지는 `hub/registry.example.com/team/server` 와
`.../other` 에 있었으므로 접두사 `hub.test/hub` 아래에서 그 바이트가 서명한 upstream 을 묶는다.

`signed_bundle` 은 테스트가 만든 P-256 키로 같은 모양의 번들을 실제로 서명한다. F0 의 서명에 없는
조합(다른 태그의 카탈로그 서명, 모양 밖의 번들)을 만들 때 쓴다.
"""

from __future__ import annotations

import base64
import json
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx2
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec

from daegam.services import hub_verifier
from daegam.services.hub_verifier import BUNDLE_TYPE, EMPTY_CONFIG_TYPE
from tests.helpers.registry import INDEX_TYPE, digest_of

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "hub_signatures"
HOST = "hub.test"
PREFIX = f"{HOST}/hub"
SERVER_REPO = "hub/registry.example.com/team/server"
OTHER_REPO = "hub/registry.example.com/team/other"
SERVER_IMAGE = f"{HOST}/{SERVER_REPO}"
OTHER_IMAGE = f"{HOST}/{OTHER_REPO}"
MANIFEST_TYPE = "application/vnd.oci.image.manifest.v1+json"
DOCKER_MANIFEST_TYPE = "application/vnd.docker.distribution.manifest.v2+json"


def fixture(name: str) -> bytes:
    return (FIXTURES / name).read_bytes()


def text(name: str) -> str:
    return fixture(name).decode().strip()


SERVER_DIGEST = text("server.digest")
OTHER_DIGEST = text("other.digest")
HUB_PUB = text("hub.pub") + "\n"
OTHER_PUB = text("other.pub") + "\n"
SIGNED = ("verified", "approved", "no-annotations", "other-key", "other-digest")


def referrer(name: str, raw: bytes | None = None) -> dict[str, Any]:
    """referrers 목록의 서술자 하나. raw 를 주면 그 manifest 의 것이다."""
    body = raw if raw is not None else fixture(f"{name}.referrer.json")
    return {
        "mediaType": MANIFEST_TYPE,
        "digest": digest_of(body),
        "size": len(body),
        "artifactType": BUNDLE_TYPE,
    }


def tampered_bundle(name: str, change: Callable[[dict[str, Any]], None]) -> bytes:
    """서명한 payload 를 고친 번들. 서명은 그대로라 맞지 않는다."""
    doc = json.loads(fixture(f"{name}.bundle.json"))
    statement = json.loads(base64.b64decode(doc["dsseEnvelope"]["payload"]))
    change(statement)
    doc["dsseEnvelope"]["payload"] = base64.b64encode(json.dumps(statement).encode()).decode()
    return json.dumps(doc).encode()


def public_pem(key: ec.EllipticCurvePrivateKey) -> str:
    return (
        key.public_key()
        .public_bytes(serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo)
        .decode()
    )


def statement(
    digest: str, annotations: dict[str, str] | None = None, predicate: Any = None
) -> bytes:
    subject: dict[str, Any] = {"digest": {"sha256": digest.removeprefix("sha256:")}}
    if annotations is not None:
        subject["annotations"] = annotations
    return json.dumps(
        {
            "_type": hub_verifier.STATEMENT_TYPE,
            "subject": [subject],
            "predicateType": hub_verifier.COSIGN_PREDICATE,
            "predicate": {} if predicate is None else predicate,
        }
    ).encode()


def signed_bundle(
    key: ec.EllipticCurvePrivateKey, body: bytes, *, signatures: int = 1
) -> dict[str, Any]:
    """body(in-toto Statement)를 key 로 cosign 3 처럼 서명한 번들."""
    sig = key.sign(hub_verifier.pae(hub_verifier.INTOTO_TYPE, body), ec.ECDSA(hashes.SHA256()))
    return {
        "mediaType": BUNDLE_TYPE,
        "verificationMaterial": {"publicKey": {"hint": "x"}},
        "dsseEnvelope": {
            "payloadType": hub_verifier.INTOTO_TYPE,
            "payload": base64.b64encode(body).decode(),
            "signatures": [{"sig": base64.b64encode(sig).decode()}] * signatures,
        },
    }


def referrer_manifest(bundle: bytes, subject: str) -> bytes:
    """번들 하나를 든 referrer manifest (cosign 3 의 모양)."""
    return json.dumps(
        {
            "schemaVersion": 2,
            "mediaType": MANIFEST_TYPE,
            "artifactType": BUNDLE_TYPE,
            "config": {"mediaType": EMPTY_CONFIG_TYPE, "digest": digest_of(b"{}"), "size": 2},
            "layers": [
                {"mediaType": BUNDLE_TYPE, "digest": digest_of(bundle), "size": len(bundle)}
            ],
            "subject": {"mediaType": INDEX_TYPE, "digest": subject, "size": 1},
        }
    ).encode()


@dataclass
class FakeHubRegistry:
    """저장소마다 태그, manifest, blob, referrers 를 두는 레지스트리. credential 을 주면 basic
    인증을 요구하고, bearer 를 켜면 토큰 흐름을 거친다. requests 에 받은 요청을 남긴다."""

    credential: tuple[str, str] | None = None
    bearer: bool = False
    realm: str = f"https://{HOST}/token"
    tags: dict[tuple[str, str], str] = field(default_factory=dict)
    manifests: dict[str, bytes] = field(default_factory=dict)
    blobs: dict[str, bytes] = field(default_factory=dict)
    referrers: dict[tuple[str, str], list[dict[str, Any]]] = field(default_factory=dict)
    # 쪽 크기. 넘으면 Link 로 다음 쪽을 준다. link_base 는 다음 쪽 주소의 앞부분이다
    page: int = 1000
    link_base: str = ""
    fail: int | None = None
    requests: list[httpx2.Request] = field(default_factory=list)

    @classmethod
    def with_fixtures(cls, **kwargs: Any) -> FakeHubRegistry:
        """F0 의 서명을 서버 저장소에 둔 레지스트리. server 의 referrers 는 F0 이 받은 목록
        그대로(번들 넷)이고, other 는 하나다. 태그 1.0.0 과 2.0.0 이 서버 digest 를 가리킨다."""
        reg = cls(**kwargs)
        for name in SIGNED:
            man = fixture(f"{name}.referrer.json")
            reg.manifests[digest_of(man)] = man
            bundle = fixture(f"{name}.bundle.json")
            reg.blobs[digest_of(bundle)] = bundle
        for name in ("server", "other"):
            index = fixture(f"{name}-index.json")
            reg.manifests[digest_of(index)] = index
        listing = json.loads(fixture("server-referrers.json"))["manifests"]
        reg.referrers[(SERVER_REPO, SERVER_DIGEST)] = listing
        reg.referrers[(OTHER_REPO, OTHER_DIGEST)] = json.loads(fixture("other-referrers.json"))[
            "manifests"
        ]
        reg.tags[(SERVER_REPO, "1.0.0")] = SERVER_DIGEST
        reg.tags[(SERVER_REPO, "2.0.0")] = SERVER_DIGEST
        return reg

    def add_bundle(self, repo: str, subject: str, bundle: dict[str, Any] | bytes) -> str:
        """번들 하나를 subject 의 referrer 로 더한다. 번들 blob 의 digest 를 돌려준다."""
        raw = bundle if isinstance(bundle, bytes) else json.dumps(bundle).encode()
        self.blobs[digest_of(raw)] = raw
        man = referrer_manifest(raw, subject)
        self.manifests[digest_of(man)] = man
        self.referrers.setdefault((repo, subject), []).append(referrer("", man))
        return digest_of(raw)

    def copy_repo(self, src: str, dst: str) -> None:
        """한 저장소의 referrers 와 태그를 다른 저장소에도 둔다(묶음 검사)."""
        for (repo, d), listing in list(self.referrers.items()):
            if repo == src:
                self.referrers[(dst, d)] = list(listing)
        for (repo, t), d in list(self.tags.items()):
            if repo == src:
                self.tags[(dst, t)] = d

    def transport(self) -> httpx2.MockTransport:
        return httpx2.MockTransport(self.handle)

    def _basic(self) -> str:
        assert self.credential is not None
        raw = f"{self.credential[0]}:{self.credential[1]}".encode()
        return "Basic " + base64.b64encode(raw).decode()

    def _authorized(self, request: httpx2.Request) -> bool:
        auth = request.headers.get("Authorization", "")
        if self.bearer:
            return auth == "Bearer t0ken"
        return self.credential is None or auth == self._basic()

    def handle(self, request: httpx2.Request) -> httpx2.Response:
        self.requests.append(request)
        if self.fail is not None:
            return httpx2.Response(self.fail)
        path = request.url.path
        if path == "/token":
            if (
                self.credential is not None
                and request.headers.get("Authorization") != self._basic()
            ):
                return httpx2.Response(401)
            return httpx2.Response(200, json={"token": "t0ken"})
        if not self._authorized(request):
            challenge = (
                f'Bearer realm="{self.realm}",service="{HOST}"'
                if self.bearer
                else f'Basic realm="{HOST}"'
            )
            return httpx2.Response(401, headers={"WWW-Authenticate": challenge})
        repo, kind, ref = _split(path)
        if kind == "manifests":
            raw = self.manifests.get(self.tags.get((repo, ref), ref))
            if raw is None:
                return httpx2.Response(404)
            media = json.loads(raw).get("mediaType") or MANIFEST_TYPE
            return httpx2.Response(200, content=raw, headers={"Content-Type": media})
        if kind == "blobs":
            raw = self.blobs.get(ref)
            return httpx2.Response(200, content=raw) if raw is not None else httpx2.Response(404)
        if kind == "referrers":
            listing = self.referrers.get((repo, ref), [])
            start = int(request.url.params.get("n_start", "0"))
            chunk = listing[start : start + self.page]
            headers = {"Content-Type": INDEX_TYPE}
            if start + self.page < len(listing):
                nxt = f"{self.link_base}/v2/{repo}/referrers/{ref}?n_start={start + self.page}"
                headers["Link"] = f'<{nxt}>; rel="next"'
            body = {"schemaVersion": 2, "mediaType": INDEX_TYPE, "manifests": chunk}
            return httpx2.Response(200, json=body, headers=headers)
        return httpx2.Response(404)


def _split(path: str) -> tuple[str, str, str]:
    """`/v2/<repo>/<kind>/<ref>` 를 나눈다."""
    rest = path.removeprefix("/v2/")
    for kind in ("manifests", "blobs", "referrers"):
        marker = f"/{kind}/"
        if marker in rest:
            repo, _, ref = rest.rpartition(marker)
            return repo, kind, ref
    return rest, "", ""
