"""OCI distribution API 의 작은 클라이언트 (P55 6절). 허브 미러(scripts/hub_mirror.py)와 카탈로그
스냅숏(scripts/hub_catalog_snapshot.py)이 플랫폼의 가상 환경(uv)에서 쓴다.

익명이나 basic 자격 증명으로 읽고, 레지스트리의 bearer 토큰 흐름을 따른다. manifest 와 blob 을
바이트 그대로 옮기고, 큰 blob 은 PATCH 조각으로 올린다. 레지스트리 앞단이 요청 하나를 60초쯤에서
끊기 때문이다(F0 4번). referrers 는 API 로 읽고, API 가 없으면(ghcr) cosign 의 태그 대체
경로(`sha256-<hex>` 인덱스)를 읽는다. 그 인덱스는 번들 referrer 에 빈 config 의 종류를 적으므로
종류는 referrer manifest 자신에서 읽는다.

자격 증명은 레지스트리 호스트 밖으로 나가지 않는다. `Authorization` 은 리다이렉트에 실리지 않고,
토큰 realm 은 https 여야 하며 basic 은 레지스트리 호스트의 realm 에만 보낸다. 다음 쪽(`Link`)과
업로드 주소는 같은 출처만 따른다. TLS 는 시스템 신뢰 목록에 DAEGAM_CA_FILE 을 더하고
(`public_fetch.context`), 이 컴퓨터의 시험용 레지스트리만 평문 http 다(`registry_auth.is_loopback`).

이 클라이언트는 플랫폼 패키지 밖(키를 가진 컴퓨터와 미러 job)에서 push 까지 하는 동기 클라이언트이고
플랫폼 안에서 읽기만 하는 비동기 검증기(`daegam.services.hub_verifier.HubRegistry`)와는 쓰임이 달라
일부러 따로 둔다.
"""

from __future__ import annotations

import base64
import hashlib
import json
import re
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

from public_fetch import USER_AGENT, context
from registry_auth import is_loopback

from daegam.services.hub_catalog import SHA256_DIGEST
from daegam.services.hub_verifier import CHALLENGE_PARAM, DOCKER_TYPES, INDEX_TYPES, MANIFEST_TYPES

__all__ = ["DOCKER_TYPES", "INDEX_TYPES", "Client", "RegistryError", "api_host", "digest_of"]

CHUNK = 12 * 1024 * 1024
MAX_MANIFEST = 4 * 1024 * 1024
MAX_TOKEN = 64 * 1024
MAX_PAGES = 16
TIMEOUT = 120

_NEXT_LINK = re.compile(r"<([^>]+)>\s*;\s*rel=\"?next\"?")


class RegistryError(Exception):
    """레지스트리의 실패. status 는 받은 HTTP 상태이고 다른 실패면 0 이다."""

    def __init__(self, status: int, message: str):
        super().__init__(f"{status} {message}" if status else message)
        self.status = status


def api_host(host: str) -> str:
    """API 를 부를 호스트. Docker Hub 의 정규 이름 docker.io 는 registry-1.docker.io 다."""
    return "registry-1.docker.io" if host == "docker.io" else host


def digest_of(raw: bytes) -> str:
    return "sha256:" + hashlib.sha256(raw).hexdigest()


def _origin(url: str) -> tuple[str, str]:
    parts = urllib.parse.urlsplit(url)
    return parts.scheme, parts.netloc


class Client:
    """레지스트리 호스트 하나. basic 은 (사용자, 비밀번호)이고 없으면 익명이다."""

    def __init__(self, host: str, basic: tuple[str, str] | None = None) -> None:
        self.host = host
        scheme = "http" if is_loopback(host) else "https"
        self.base = f"{scheme}://{api_host(host)}"
        self.basic = basic
        self.tokens: dict[str, str] = {}
        # 리다이렉트(blob 의 CDN)는 따라가되 `Authorization` 은 `add_unredirected_header` 로 달아
        # 다른 주소로 옮기지 않는다
        handlers: list[Any] = []
        if scheme == "https":
            handlers.append(urllib.request.HTTPSHandler(context=context()))
        self._opener = urllib.request.build_opener(*handlers)

    # -- 인증 ------------------------------------------------------------------------------

    def _basic_header(self) -> str | None:
        if not self.basic:
            return None
        raw = f"{self.basic[0]}:{self.basic[1]}".encode()
        return "Basic " + base64.b64encode(raw).decode()

    def _same_origin(self, url: str) -> bool:
        return _origin(url) == _origin(self.base)

    def _token(self, challenge: str, scope: str) -> str:
        params = dict(CHALLENGE_PARAM.findall(challenge))
        realm = params.pop("realm", "")
        scheme, netloc = _origin(realm)
        if not netloc or (scheme != "https" and not (scheme == "http" and is_loopback(netloc))):
            raise RegistryError(0, f"{self.host} answered a token realm that is not an https URL")
        params["scope"] = scope
        req = urllib.request.Request(
            realm + "?" + urllib.parse.urlencode(params), headers={"User-Agent": USER_AGENT}
        )
        basic = self._basic_header()
        if basic and netloc == urllib.parse.urlsplit(self.base).netloc:
            req.add_unredirected_header("Authorization", basic)
        try:
            with self._opener.open(req, timeout=TIMEOUT) as r:
                raw = r.read(MAX_TOKEN + 1)
        except urllib.error.HTTPError as e:
            raise RegistryError(e.code, f"token request to {realm} refused") from None
        except urllib.error.URLError as e:
            raise RegistryError(0, f"token request to {realm}: {e.reason}") from None
        if len(raw) > MAX_TOKEN:
            raise RegistryError(0, f"{realm} answered more than {MAX_TOKEN} bytes")
        body = json.loads(raw)
        token = (body.get("token") or body.get("access_token")) if isinstance(body, dict) else None
        if not token:
            raise RegistryError(0, f"{realm} answered no token")
        return token

    def request(
        self,
        method: str,
        url: str,
        repo: str,
        push: bool = False,
        data: bytes | None = None,
        headers: dict[str, str] | None = None,
    ) -> Any:
        """인증한 요청 하나의 응답(열린 것). url 은 경로이거나 같은 출처의 주소다. 401 의 bearer
        도전에는 토큰을 받아 한 번 더 보낸다. 실패는 RegistryError 다."""
        scope = f"repository:{repo}:{'pull,push' if push else 'pull'}"
        if not url.startswith("http"):
            url = self.base + url
        if not self._same_origin(url):
            raise RegistryError(0, f"{self.host} pointed a request at another origin: {url}")
        for attempt in range(2):
            req = urllib.request.Request(
                url, data=data, method=method, headers={"User-Agent": USER_AGENT, **(headers or {})}
            )
            token = self.tokens.get(scope)
            auth = f"Bearer {token}" if token else self._basic_header()
            if auth:
                req.add_unredirected_header("Authorization", auth)
            try:
                return self._opener.open(req, timeout=TIMEOUT)
            except urllib.error.HTTPError as e:
                challenge = e.headers.get("WWW-Authenticate", "") if e.headers else ""
                if e.code == 401 and attempt == 0 and challenge.lower().startswith("bearer"):
                    self.tokens[scope] = self._token(challenge, scope)
                    continue
                body = e.read()[:1500].decode(errors="replace")
                raise RegistryError(e.code, f"{method} {url}: {body}") from None
            except urllib.error.URLError as e:
                raise RegistryError(0, f"{method} {url}: {e.reason}") from None
        raise RegistryError(401, f"{method} {url}: not authorized")

    # -- 읽기 ------------------------------------------------------------------------------

    def manifest(self, repo: str, ref: str) -> tuple[bytes, str, str]:
        """(바이트, 종류, 바이트의 digest). ref 가 digest 면 받은 바이트가 그 digest 여야 한다."""
        accept = ", ".join([*MANIFEST_TYPES, "application/vnd.oci.artifact.manifest.v1+json"])
        path = f"/v2/{repo}/manifests/{ref}"
        with self.request("GET", path, repo, headers={"Accept": accept}) as r:
            raw = r.read(MAX_MANIFEST + 1)
            media = (r.headers.get("Content-Type") or "").split(";")[0]
        if len(raw) > MAX_MANIFEST:
            raise RegistryError(0, f"manifest {repo}@{ref} is larger than {MAX_MANIFEST} bytes")
        digest = digest_of(raw)
        if ref.startswith("sha256:") and ref != digest:
            raise RegistryError(0, f"manifest {repo}@{ref} came back as {digest}")
        doc = json.loads(raw)
        return raw, (doc.get("mediaType") if isinstance(doc, dict) else None) or media, digest

    def blob(self, repo: str, digest: str, size: int) -> bytes:
        """blob 하나. 서술자의 크기까지만 읽고 digest 를 맞춘다."""
        if not SHA256_DIGEST.fullmatch(digest):
            raise RegistryError(0, f"blob digest {digest!r} is not sha256:<hex>")
        with self.request("GET", f"/v2/{repo}/blobs/{digest}", repo) as r:
            data = r.read(size + 1)
        if len(data) != size or digest_of(data) != digest:
            raise RegistryError(0, f"blob {repo}@{digest} came back with another size or digest")
        return data

    def has_blob(self, repo: str, digest: str) -> bool:
        if not SHA256_DIGEST.fullmatch(digest):
            raise RegistryError(0, f"blob digest {digest!r} is not sha256:<hex>")
        try:
            with self.request("HEAD", f"/v2/{repo}/blobs/{digest}", repo, push=True):
                return True
        except RegistryError as e:
            if e.status == 404:
                return False
            raise

    def referrers(self, repo: str, digest: str) -> list[dict[str, Any]]:
        """digest 를 subject 로 둔 서술자. API 를 쪽마다(같은 출처의 `rel="next"`, MAX_PAGES 까지)
        읽고, API 가 없으면 태그 대체 경로다."""
        out: list[dict[str, Any]] = []
        url = f"{self.base}/v2/{repo}/referrers/{digest}"
        try:
            for _ in range(MAX_PAGES):
                with self.request("GET", url, repo) as r:
                    body = json.load(r)
                    link = r.headers.get("Link")
                out.extend(body.get("manifests") or [])
                m = _NEXT_LINK.search(link or "")
                if m is None:
                    return out
                url = urllib.parse.urljoin(url, m.group(1))
                if not self._same_origin(url):
                    raise RegistryError(0, f"{self.host} pointed the next page at another origin")
            raise RegistryError(0, f"{self.host} answered more than {MAX_PAGES} referrer pages")
        except RegistryError as e:
            if out or e.status not in (400, 404, 405):
                raise
        try:
            raw, _, _ = self.manifest(repo, digest.replace(":", "-"))
        except RegistryError as e:
            if e.status != 404:
                raise
            return []
        return json.loads(raw).get("manifests") or []

    # -- 쓰기 ------------------------------------------------------------------------------

    def put_blob(self, repo: str, digest: str, data: bytes) -> None:
        if self.has_blob(repo, digest):
            return
        headers = {"Content-Length": "0"}
        with self.request(
            "POST", f"/v2/{repo}/blobs/uploads/", repo, push=True, data=b"", headers=headers
        ) as r:
            location = self._absolute(r.headers["Location"])
        if len(data) > CHUNK:
            offset = 0
            while offset < len(data):
                part = data[offset : offset + CHUNK]
                headers = {
                    "Content-Type": "application/octet-stream",
                    "Content-Range": f"{offset}-{offset + len(part) - 1}",
                    "Content-Length": str(len(part)),
                }
                with self.request(
                    "PATCH", location, repo, push=True, data=part, headers=headers
                ) as r:
                    location = self._absolute(r.headers["Location"])
                offset += len(part)
            data = b""
        url = location + ("&" if "?" in location else "?") + "digest=" + digest
        headers = {"Content-Type": "application/octet-stream", "Content-Length": str(len(data))}
        with self.request("PUT", url, repo, push=True, data=data, headers=headers):
            pass

    def put_manifest(self, repo: str, ref: str, raw: bytes, media_type: str) -> None:
        headers = {"Content-Type": media_type}
        with self.request(
            "PUT", f"/v2/{repo}/manifests/{ref}", repo, push=True, data=raw, headers=headers
        ):
            pass

    def _absolute(self, location: str) -> str:
        return urllib.parse.urljoin(self.base + "/", location)
