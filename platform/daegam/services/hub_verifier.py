"""허브 image 항목의 서명을 모으는 검증기 (P55 6절, D97 초안).

허브 레지스트리를 OCI distribution API 로 읽는다. 자격 증명은 Platform Settings 의 읽기 자격
증명이고(basic, 또는 레지스트리의 bearer 토큰 흐름), TLS 는 기본 신뢰에 `network.ca_bundle` 을
더한다. 자격 증명은 레지스트리 호스트 밖으로 나가지 않는다. 리다이렉트는 따라가지 않고, 토큰
realm 과 다음 쪽은 https 여야 하며, basic 은 레지스트리 호스트의 realm 에만 보낸다.

digest 의 referrers API 를 쪽마다 읽어 cosign 3 이 쓰는 sigstore 번들(v0.3)만 받는다. cosign 의
태그 대체 경로는 읽지 않으므로 허브 레지스트리는 referrers API 를 내야 한다(zot 은 낸다). cosign
바이너리 없이 `cryptography` 로 DSSE 서명을 설정한 공개 키에 맞춰 보고, 투명성 로그는 보지 않는다.
허브 키는 로그 없이 서명하기 때문이다.

믿는 것은 서명한 내용뿐이다. digest 와 `dev.daegam.hub.*` 주석은 DSSE payload(in-toto
Statement v1)의 subject 하나에서만 읽는다. referrer manifest 의 `subject` 와 주석, referrers
목록은 서명 밖이라 믿지 않는다. 번들의 키 힌트는 키를 맞춰 보는 차례만 정하고 아무것도 가르지
않는다. 번들의 모양은 F0 4번이 잰 것이다 (docs/plan/reviews/2026-11-16-p55-f0-item4.md "The
bundle shape"). 모양 밖의 번들은 오류 없이 모으지 않는다.

레지스트리에 닿지 못하거나 5xx 면 `registry_unreachable`, 자격 증명이 거절되면
`registry_credential_refused` 다(둘 다 502). 이미지가 거절된 것이 아니라 확인하지 못한 것이다.
일부만 읽은 referrers 로는 정하지 않는다.

이 클라이언트는 플랫폼 안에서 읽기만 하는 비동기 클라이언트이고 플랫폼 패키지 밖(키를 가진 컴퓨터와
미러 job)에서 push 까지 하는 동기 클라이언트(scripts/lib/oci_registry.py)와는 쓰임이 달라 일부러
따로 둔다.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
import re
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlencode, urljoin, urlsplit

import httpx2
from cryptography.exceptions import InvalidSignature, UnsupportedAlgorithm
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec

from daegam.errors import ServiceError
from daegam.services.hub_catalog import SHA256_DIGEST, ImageRef, parse_image

BUNDLE_TYPE = "application/vnd.dev.sigstore.bundle.v0.3+json"
EMPTY_CONFIG_TYPE = "application/vnd.oci.empty.v1+json"
INTOTO_TYPE = "application/vnd.in-toto+json"
STATEMENT_TYPE = "https://in-toto.io/Statement/v1"
COSIGN_PREDICATE = "https://sigstore.dev/cosign/sign/v1"

# 서명한 내용 안의 주석 (6절). catalog 는 카탈로그 서버를 옮겼을 때만 있다
UPSTREAM = "dev.daegam.hub.upstream"
PROVENANCE = "dev.daegam.hub.provenance"
CATALOG = "dev.daegam.hub.catalog"
RUN = "dev.daegam.hub.run"
REQUIRED_ANNOTATIONS = (UPSTREAM, PROVENANCE, RUN)
VERIFIED = "verified"
APPROVED = "approved"
PROVENANCES = (VERIFIED, APPROVED)

# OCI manifest 의 종류. 허브 미러의 클라이언트(scripts/lib/oci_registry.py)도 이 표를 쓴다
INDEX_TYPES = frozenset(
    {
        "application/vnd.oci.image.index.v1+json",
        "application/vnd.docker.distribution.manifest.list.v2+json",
    }
)
DOCKER_TYPES = frozenset(
    {
        "application/vnd.docker.distribution.manifest.list.v2+json",
        "application/vnd.docker.distribution.manifest.v2+json",
    }
)
MANIFEST_TYPES = (
    "application/vnd.oci.image.index.v1+json",
    "application/vnd.oci.image.manifest.v1+json",
    "application/vnd.docker.distribution.manifest.list.v2+json",
    "application/vnd.docker.distribution.manifest.v2+json",
)
# `WWW-Authenticate` 도전의 `key="value"` 칸
CHALLENGE_PARAM = re.compile(r'(\w+)="([^"]*)"')

# 읽는 크기와 수의 상한. referrers 는 다시 옮기기와 다시 서명하기가 하나씩 더하므로 넉넉히 둔다.
# 상한에 닿으면 일부만 보고 정하지 않고 거절한다(`image_signatures_too_many`)
MAX_MANIFEST_BYTES = 1024 * 1024
MAX_INDEX_BYTES = 1024 * 1024
MAX_BUNDLE_BYTES = 64 * 1024
MAX_TOKEN_BYTES = 64 * 1024
MAX_REFERRERS = 64
MAX_PAGES = 16
TIMEOUT_SECONDS = 15.0

_NEXT_LINK = re.compile(r"<([^>]+)>\s*;\s*rel=\"?next\"?")


# -- 키 ------------------------------------------------------------------------------------


@dataclass(frozen=True)
class HubKey:
    """허브 키의 공개 키 하나. fingerprint 는 DER(SubjectPublicKeyInfo)의 sha256 이고 Platform
    Settings 의 회수 목록과 항목의 `source.admission.key` 가 쓰는 꼴이다. hint 는 같은 해시의
    base64 로, cosign 이 번들의 `publicKey.hint` 에 적는 꼴이다."""

    fingerprint: str
    hint: str
    key: ec.EllipticCurvePublicKey


def load_key(pem: str) -> HubKey:
    """PEM 공개 키 하나. 읽지 못하거나 P-256 이 아니면 ValueError 다."""
    try:
        key = serialization.load_pem_public_key(pem.strip().encode())
    except (UnsupportedAlgorithm, TypeError) as e:
        raise ValueError(f"the hub key cannot be read: {e}") from e
    if not isinstance(key, ec.EllipticCurvePublicKey) or key.curve.name != "secp256r1":
        raise ValueError("the hub key must be an ECDSA P-256 public key")
    der = key.public_bytes(
        serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo
    )
    digest = hashlib.sha256(der).digest()
    return HubKey(
        fingerprint=f"sha256:{digest.hex()}", hint=base64.b64encode(digest).decode(), key=key
    )


# -- 서명 하나 -----------------------------------------------------------------------------


@dataclass(frozen=True)
class HubSignature:
    """모은 서명 하나. 값은 모두 서명한 내용에서 읽었다. bundle 은 번들 blob 의 digest 로, 같은
    조건의 서명 가운데 하나를 고를 때의 마지막 차례다."""

    bundle: str
    upstream: str
    upstream_ref: ImageRef
    provenance: str
    catalog: tuple[str, str] | None
    run: dict[str, str]
    key: str


def pae(payload_type: str, body: bytes) -> bytes:
    """DSSE 의 Pre-Authentication Encoding. 서명은 이 바이트 위에 있다."""
    t = payload_type.encode()
    return b"DSSEv1 %d %s %d %s" % (len(t), t, len(body), body)


def parse_run(value: str) -> dict[str, str]:
    """`dev.daegam.hub.run` 의 값. 빈칸으로 나눈 `key=value` 다 (6절). 모르는 키도 읽는다."""
    out: dict[str, str] = {}
    for token in value.split():
        k, sep, v = token.partition("=")
        if sep and k and v:
            out[k] = v
    return out


def _b64(value: Any) -> bytes | None:
    if not isinstance(value, str):
        return None
    try:
        return base64.b64decode(value, validate=True)
    except (binascii.Error, ValueError):
        return None


def _dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _verifying_key(sig: bytes, signed: bytes, keys: Sequence[HubKey], hint: Any) -> HubKey | None:
    """서명을 맞추는 키. 힌트가 가리키는 키부터 맞춰 보되 힌트로 정하지 않는다."""
    for k in sorted(keys, key=lambda k: k.hint != hint):
        try:
            k.key.verify(sig, signed, ec.ECDSA(hashes.SHA256()))
        except InvalidSignature:
            continue
        return k
    return None


def read_bundle(
    raw: bytes, keys: Sequence[HubKey], digest: str, *, bundle: str
) -> tuple[HubSignature | None, str]:
    """번들 하나를 읽는다. 모으면 (서명, ""), 아니면 (None, 까닭)이다. 모양 밖이면 예외 없이 까닭을
    돌려준다. digest 는 이미지의 digest 이고, 서명한 subject 의 digest 와 `dev.daegam.hub.upstream`
    의 digest 가 모두 그것이어야 한다. 서명은 하나여야 한다(재 본 모양)."""
    try:
        doc = json.loads(raw)
    except ValueError:
        return None, "bundle_json"
    if not isinstance(doc, dict) or doc.get("mediaType") != BUNDLE_TYPE:
        return None, "bundle_media_type"
    env = _dict(doc.get("dsseEnvelope"))
    if env.get("payloadType") != INTOTO_TYPE:
        return None, "not_dsse_in_toto"
    body = _b64(env.get("payload"))
    if body is None:
        return None, "payload_encoding"
    signatures = env.get("signatures")
    if not isinstance(signatures, list) or len(signatures) != 1:
        return None, "signature_count"
    sig = _b64(_dict(signatures[0]).get("sig"))
    if sig is None:
        return None, "signature_encoding"
    hint = _dict(_dict(doc.get("verificationMaterial")).get("publicKey")).get("hint")
    key = _verifying_key(sig, pae(INTOTO_TYPE, body), keys, hint)
    if key is None:
        return None, "no_key_verifies"
    try:
        st = json.loads(body)
    except ValueError:
        return None, "statement_json"
    if not isinstance(st, dict):
        return None, "statement_json"
    if st.get("_type") != STATEMENT_TYPE or st.get("predicateType") != COSIGN_PREDICATE:
        return None, "statement_type"
    if st.get("predicate") != {}:
        return None, "predicate_not_empty"
    subjects = st.get("subject")
    if not isinstance(subjects, list) or len(subjects) != 1 or not isinstance(subjects[0], dict):
        return None, "subject_count"
    subject = subjects[0]
    if f"sha256:{_dict(subject.get('digest')).get('sha256', '')}" != digest:
        return None, "subject_digest"
    ann = _dict(subject.get("annotations"))
    if not all(isinstance(ann.get(k), str) for k in REQUIRED_ANNOTATIONS):
        return None, "hub_annotations_missing"
    try:
        upstream_ref = parse_image(ann[UPSTREAM])
    except ValueError:
        return None, "upstream_reference"
    if upstream_ref.digest != digest:
        return None, "upstream_digest"
    provenance = ann[PROVENANCE]
    if provenance not in PROVENANCES:
        return None, "provenance_value"
    catalog = None
    if CATALOG in ann:
        server, sep, release = str(ann[CATALOG]).rpartition("@")
        if not (sep and server and release):
            return None, "catalog_annotation"
        catalog = (server, release)
    run = parse_run(ann[RUN])
    if not run.get("by") or not run.get("via"):
        return None, "run_annotation"
    return (
        HubSignature(
            bundle=bundle,
            upstream=ann[UPSTREAM],
            upstream_ref=upstream_ref,
            provenance=provenance,
            catalog=catalog,
            run=run,
            key=key.fingerprint,
        ),
        "",
    )


def binds_repository(sig: HubSignature, prefix_path: str, repository: str) -> bool:
    """서명한 upstream 이 사람이 준 저장소를 묶는가. `<접두사의 경로>/<서명한 upstream 의 호스트>/
    <저장소>` 여야 한다. D66 이 이미지 이름을 서명에 묶는 것과 같다. repository 는 호스트 뒤의
    경로다."""
    return repository == sig.upstream_ref.mirror_repository(prefix_path)


def referrer_layer(raw: bytes) -> tuple[dict[str, Any] | None, str]:
    """referrer manifest 가 번들 하나를 든 모양이면 (그 층, ""), 아니면 (None, 까닭)이다. 자기
    `artifactType`, 빈 config, 그 종류의 층 하나와 번들 크기 상한을 본다. 목록의 서술자는 서명
    밖이라 보지 않는다. 미러의 다시 읽기도 이것을 쓴다."""
    try:
        man = json.loads(raw)
    except ValueError:
        return None, "referrer_json"
    if not isinstance(man, dict) or man.get("artifactType") != BUNDLE_TYPE:
        return None, "referrer_type"
    if _dict(man.get("config")).get("mediaType") != EMPTY_CONFIG_TYPE:
        return None, "referrer_config"
    layers = man.get("layers")
    if not isinstance(layers, list) or len(layers) != 1 or not isinstance(layers[0], dict):
        return None, "referrer_layers"
    layer = layers[0]
    if layer.get("mediaType") != BUNDLE_TYPE or not SHA256_DIGEST.fullmatch(
        str(layer.get("digest") or "")
    ):
        return None, "referrer_layers"
    size = layer.get("size")
    if not isinstance(size, int) or size > MAX_BUNDLE_BYTES:
        return None, "bundle_size"
    return layer, ""


# -- 레지스트리 ----------------------------------------------------------------------------


class NotFound(Exception):
    """레지스트리가 404 로 답했다."""


@dataclass
class HubRegistry:
    """허브 레지스트리 하나의 읽기. 부르는 쪽이 `async with` 로 연다. credential 은 (사용자,
    비밀번호)이고 없으면 익명으로 읽는다. 오류는 error_cls 로 올린다."""

    host: str
    credential: tuple[str, str] | None
    verify: Any
    error_cls: type[ServiceError]
    transport: Any = None
    _client: httpx2.AsyncClient | None = field(default=None, init=False, repr=False)
    _tokens: dict[str, str] = field(default_factory=dict, init=False, repr=False)

    async def __aenter__(self) -> HubRegistry:
        # 리다이렉트는 따라가지 않는다. 허브 레지스트리(zot)는 리다이렉트하지 않고, 따라가면
        # 플랫폼 파드가 다른 주소로 나간다
        kwargs: dict[str, Any] = {
            "verify": self.verify,
            "timeout": TIMEOUT_SECONDS,
            "follow_redirects": False,
        }
        if self.transport is not None:
            kwargs["transport"] = self.transport
        self._client = httpx2.AsyncClient(**kwargs)
        return self

    async def __aexit__(self, *exc: object) -> None:
        if self._client is not None:
            await self._client.aclose()

    @property
    def base(self) -> str:
        return f"https://{self.host}"

    def _unreachable(self, detail: str) -> ServiceError:
        return self.error_cls(502, "registry_unreachable", f"the hub registry {self.host} {detail}")

    def _refused(self) -> ServiceError:
        return self.error_cls(
            502,
            "registry_credential_refused",
            f"the hub registry {self.host} refused the read credential",
        )

    def _basic(self) -> str | None:
        if self.credential is None:
            return None
        raw = f"{self.credential[0]}:{self.credential[1]}".encode()
        return "Basic " + base64.b64encode(raw).decode()

    async def _token(self, challenge: str, scope: str) -> str:
        """bearer 토큰. realm 은 https 여야 하고, 읽기 자격 증명은 레지스트리 호스트의 realm 에만
        싣는다. 다른 호스트의 realm 에는 익명으로 묻는다."""
        params = dict(CHALLENGE_PARAM.findall(challenge))
        realm = params.pop("realm", "")
        parts = urlsplit(realm)
        if parts.scheme != "https" or not parts.netloc:
            raise self._unreachable("answered with a token realm that is not an https URL")
        params.setdefault("scope", scope)
        basic = self._basic() if parts.netloc == self.host else None
        headers = {"Authorization": basic} if basic else {}
        raw, _ = await self._read(
            f"{realm}?{urlencode(params)}", headers=headers, limit=MAX_TOKEN_BYTES, token=True
        )
        try:
            body = json.loads(raw)
        except ValueError as e:
            raise self._unreachable("token endpoint answered no JSON") from e
        token = (body.get("token") or body.get("access_token")) if isinstance(body, dict) else None
        if not isinstance(token, str) or not token:
            raise self._unreachable("token endpoint answered no token")
        return token

    async def _read(
        self, url: str, *, headers: dict[str, str], limit: int, token: bool = False
    ) -> tuple[bytes, httpx2.Headers]:
        """GET 하나의 본문(limit 까지)과 머리. 401 은 그 머리를 실은 `_Challenge` 다."""
        assert self._client is not None
        try:
            async with self._client.stream("GET", url, headers=headers) as r:
                if r.status_code == 401 and not token:
                    raise _Challenge(r.headers.get("WWW-Authenticate", ""))
                if r.status_code in (401, 403):
                    raise self._refused()
                if r.status_code == 404:
                    raise NotFound(url)
                if r.status_code >= 300:
                    raise self._unreachable(f"answered {r.status_code}")
                body = bytearray()
                async for chunk in r.aiter_bytes():
                    body += chunk
                    if len(body) > limit:
                        raise self._unreachable(f"answered more than {limit} bytes")
                return bytes(body), r.headers
        except (httpx2.TransportError, httpx2.InvalidURL) as e:
            raise self._unreachable(f"could not be reached: {e}") from e

    async def _get(
        self, url: str, *, repo: str, accept: str | None, limit: int
    ) -> tuple[bytes, httpx2.Headers]:
        """인증한 GET 하나. 본문은 limit 까지만 읽는다. 404 는 `NotFound` 다."""
        scope = f"repository:{repo}:pull"
        for attempt in range(2):
            headers = {"Accept": accept} if accept else {}
            if token := self._tokens.get(scope):
                headers["Authorization"] = f"Bearer {token}"
            elif basic := self._basic():
                headers["Authorization"] = basic
            try:
                return await self._read(url, headers=headers, limit=limit)
            except _Challenge as c:
                if attempt == 0 and c.challenge.lower().startswith("bearer"):
                    self._tokens[scope] = await self._token(c.challenge, scope)
                    continue
                raise self._refused() from None
        raise self._refused()

    async def manifest(self, repo: str, ref: str) -> tuple[bytes, str]:
        """manifest 의 바이트와 그 digest. ref 가 digest 면 받은 바이트의 digest 가 그것이어야 한다.
        레지스트리가 적은 digest 헤더는 믿지 않는다."""
        raw, _ = await self._get(
            f"{self.base}/v2/{repo}/manifests/{ref}",
            repo=repo,
            accept=", ".join(MANIFEST_TYPES),
            limit=MAX_MANIFEST_BYTES,
        )
        digest = f"sha256:{hashlib.sha256(raw).hexdigest()}"
        if ref.startswith("sha256:") and ref != digest:
            raise self._unreachable(f"answered a manifest whose digest is not {ref}")
        return raw, digest

    async def blob(self, repo: str, digest: str, *, limit: int) -> bytes:
        raw, _ = await self._get(
            f"{self.base}/v2/{repo}/blobs/{digest}", repo=repo, accept=None, limit=limit
        )
        if f"sha256:{hashlib.sha256(raw).hexdigest()}" != digest:
            raise self._unreachable(f"answered a blob whose digest is not {digest}")
        return raw

    async def referrers(self, repo: str, digest: str) -> list[dict[str, Any]]:
        """digest 의 번들 referrer 서술자. 쪽마다(`Link`) 읽고 번들 종류만 남긴다. 종류 거르기는
        레지스트리가 해도 여기서 다시 한다. MAX_REFERRERS 를 넘거나 MAX_PAGES 쪽을 넘으면 422
        image_signatures_too_many 다. 첫 쪽의 404 는 referrers API 가 없는 것이라 빈 목록이고, 그
        뒤 쪽의 404 는 일부만 읽은 것이라 `registry_unreachable` 이다."""
        url: str | None = f"{self.base}/v2/{repo}/referrers/{digest}?" + urlencode(
            {"artifactType": BUNDLE_TYPE}
        )
        out: list[dict[str, Any]] = []
        for page in range(MAX_PAGES):
            assert url is not None
            try:
                raw, headers = await self._get(
                    url,
                    repo=repo,
                    accept="application/vnd.oci.image.index.v1+json",
                    limit=MAX_INDEX_BYTES,
                )
            except NotFound:
                if page == 0:
                    return out
                raise self._unreachable("lost a referrers page while it was being read") from None
            try:
                index = json.loads(raw)
            except ValueError as e:
                raise self._unreachable("answered a referrers list that is not JSON") from e
            manifests = index.get("manifests") if isinstance(index, dict) else None
            for d in manifests if isinstance(manifests, list) else []:
                if isinstance(d, dict) and d.get("artifactType") == BUNDLE_TYPE:
                    out.append(d)
            if len(out) > MAX_REFERRERS:
                raise self._too_many(f"more than {MAX_REFERRERS} signatures")
            url = self._next(headers.get("Link"), url)
            if url is None:
                return out
        raise self._too_many(f"more than {MAX_PAGES} pages of signatures")

    def _too_many(self, what: str) -> ServiceError:
        return self.error_cls(
            422,
            "image_signatures_too_many",
            f"this image carries {what}; a platform-admin clears old ones before it can be checked",
        )

    def _next(self, link: str | None, current: str) -> str | None:
        """다음 쪽의 주소. 같은 호스트와 포트의 https 만 따라간다."""
        m = _NEXT_LINK.search(link or "")
        if m is None:
            return None
        nxt = urljoin(current, m.group(1))
        parts = urlsplit(nxt)
        if parts.scheme != "https" or parts.netloc != urlsplit(self.base).netloc:
            raise self._unreachable("pointed the next referrers page at another origin")
        return nxt


class _Challenge(Exception):
    """레지스트리가 401 로 답했다. challenge 는 `WWW-Authenticate` 다."""

    def __init__(self, challenge: str):
        super().__init__(challenge)
        self.challenge = challenge


async def collect(
    registry: HubRegistry,
    repo: str,
    digest: str,
    keys: Sequence[HubKey],
    *,
    prefix_path: str,
    refused: Callable[[str, str], None] | None = None,
) -> list[HubSignature]:
    """digest 에서 모은 서명. referrer 자기 manifest 가 번들 하나를 든 모양인 것만 읽는다
    (`referrer_layer`). 서명이 맞고 허브 주석이 있으며 사람이 준 저장소(repo, 호스트 뒤의 경로)를
    묶는 것만 모은다. prefix_path 는 접두사에서 호스트를 뺀 경로다. 목록에 있던 referrer 가 404 면
    일부만 읽은 것이라 `registry_unreachable` 이다. refused 는 모으지 않은 referrer 와 그 까닭을
    받는다(기록과 테스트)."""
    out: list[HubSignature] = []
    for d in await registry.referrers(repo, digest):
        ref = str(d.get("digest") or "")
        if not SHA256_DIGEST.fullmatch(ref):
            continue
        try:
            raw, _ = await registry.manifest(repo, ref)
            layer, why = referrer_layer(raw)
            blob = (
                await registry.blob(repo, layer["digest"], limit=MAX_BUNDLE_BYTES)
                if layer is not None
                else b""
            )
        except NotFound:
            raise registry._unreachable(f"lost referrer {ref} while it was being read") from None
        sig = None
        if layer is not None:
            sig, why = read_bundle(blob, keys, digest, bundle=layer["digest"])
        if sig is not None and not binds_repository(sig, prefix_path, repo):
            sig, why = None, "repository_binding"
        if sig is None:
            if refused:
                refused(ref, why)
            continue
        out.append(sig)
    return out
