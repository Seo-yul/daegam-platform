"""허브 미러 (P55 6절). upstream 이미지를 허브 접두사 아래로 바이트 그대로 옮기고 허브 키로
서명한다.

허브 키와 허브 접두사에 올릴 수 있는 레지스트리 로그인을 가진 컴퓨터가 돌린다. 플랫폼은 push
자격 증명도 서명 키도 갖지 않으므로 미러는 플랫폼 밖의 일이다. 기본 경로는 make
target 이고, private 저장소의 workflow_dispatch job(deploy/hub-mirror)이 같은 스크립트로 같은
일을 한다.

    make hub-mirror HUB_UPSTREAM=<참조:태그> [HUB_SERVER=<서버>] [HUB_APPROVE=yes]
    make hub-resign HUB_IMAGE=<사본@digest> HUB_KEEP_KEYS="<공개 키 파일...>"
                    [HUB_REFUSED="<digest...>"]
    hub_mirror.py copy  (HUB_UPSTREAM, HUB_PREFIX) 서명 없이 3단계의 복사만 한다

둘 다 HUB_PREFIX(허브 접두사)와 HUB_KEY(허브 키, cosign 개인 키 파일)를 받는다. 키의
비밀번호는 COSIGN_PASSWORD, 없으면 HUB_KEY_PASSWORD_FILE 의 파일, 그것도 없으면 터미널에서
받고 cosign 을 부를 때만 넘긴다. 허브 레지스트리의 자격 증명은 docker login 이 둔 것이고
upstream 은 익명으로 읽는다.

mirror 의 단계 (6절):

0. 참조는 플랫폼의 `parse_image` 로 정규화하고 꼴을 본다. 카탈로그 서버를 이름 대면 upstream
   저장소가 스냅숏의 그 서버의 이미지 저장소와 같아야 한다. 다른 태그는 되고 기록한다. 이름
   대지 않으면 upstream 저장소가 어느 스냅숏 서버의 것도 아니어야 한다.
1. upstream 태그를 digest 로 푼다. digest 를 함께 주면 태그가 푼 digest 와 같아야 한다. 뒤의
   단계는 모두 `upstream@digest` 다.
2. 스냅숏에 출처(provenance)가 있으면 upstream 번들마다 cosign 이 키 없는 서명을 확인한다
   (발급자, 저장소와 workflow 경로를 묶은 신원, 그 digest). 확인한 번들의 Fulcio
   인증서에서 runner 환경, 저장소 URI, ref 를 읽어 ToolHive core 처럼 맞춘다. 출처에 저장소
   URI 와 workflow 경로가 없으면 멈춘다. 실패는 끝이다. 승인(HUB_APPROVE)은 출처가 없을 때만
   받는다.
3. digest 와 인덱스의 자식 모두를 `<prefix>/<upstream 호스트>/<저장소>` 로 바이트 그대로
   옮기고 upstream 태그도 민다. 옮긴 digest 가 다르면 멈춘다. Docker schema 2 이미지를 허브
   레지스트리가 받지 않으면 그 까닭을 말하고 멈춘다. 바꾸면 digest 가 바뀌므로 바꾸지 않는다.
4. 투명성 로그 없이(서비스 없는 signing config) 허브 키로 서명하고 dev.daegam.hub.* 주석을
   서명한 내용에 싣는다. 그 뒤 플랫폼의 검증기(daegam.services.hub_verifier)의 규칙으로 그
   서명을 다시 읽어 본다.

실행 기록(`dev.daegam.hub.run`)은 누가(`by`), 어디서(`via`) 돌렸는지와 job 이면 실행 주소,
workflow 커밋과 ref 에 더해, 돌린 플랫폼 커밋(`platform`), 0단계가 쓴 스냅숏 릴리스(`catalog`),
확인한 upstream 서명의 ref(`upstream_ref`)를 든다.

copy 는 서명하지 않은 사본을 만든다. 허용이 서명 없는 사본을 거절하는지 e2e(e2e-gateway STEP 9i)가 이것으로 본다. 키를
받지 않고, 0단계와 2단계를 거치지 않으며, 옮긴 사본의 digest 로 고정한 참조를 찍는다.

resign 은 키 바꾸기다. 사본의 허브 서명 가운데 HUB_KEEP_KEYS 의 키로 검증되는 것의 서명한
내용(upstream, provenance, catalog)을 새 키로 그대로 서명하고 제 실행 기록을 단다. 거부한
digest 는 건너뛴다.

    uv run --project platform python -I scripts/hub_mirror.py mirror|resign
"""

from __future__ import annotations

import base64
import getpass
import json
import os
import re
import subprocess
import sys
import tempfile
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from cryptography import x509

from daegam.services import hub_verifier
from daegam.services.hub_catalog import (
    SHA256_DIGEST,
    Catalog,
    CatalogServer,
    ImageRef,
    parse_image,
    snapshot,
)
from daegam.services.hub_images import split_prefix

# -I 가 PYTHONPATH 를 보지 않으므로 scripts/lib 를 직접 더한다
sys.path.insert(0, str(Path(__file__).resolve().parent / "lib"))
import oci_registry  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
GITHUB_ISSUER = "https://token.actions.githubusercontent.com"
# Fulcio 인증서의 확장 (ToolHive core 가 맞추는 칸)
OID_ISSUER = "1.3.6.1.4.1.57264.1.8"
OID_RUNNER = "1.3.6.1.4.1.57264.1.11"
OID_REPOSITORY = "1.3.6.1.4.1.57264.1.12"
OID_REF = "1.3.6.1.4.1.57264.1.14"
# 서비스 없는 signing config. cosign 3 은 이것으로 키만으로 서명하고 투명성 로그에 올리지
# 않는다 (F0 4번)
SIGNING_CONFIG = {
    "mediaType": "application/vnd.dev.sigstore.signingconfig.v0.2+json",
    "rekorTlogConfig": {},
    "tsaConfig": {},
}
# cosign 은 -a 값을 CSV 로 읽으므로 따옴표, 쉼표, 제어 문자를 받지 않는다. 실행 기록은 빈칸으로
# 나눈 `key=value` 라 그 값에는 빈칸도 없어야 한다
_CSV_SAFE = re.compile(r'[^",\x00-\x1f\x7f]+')
_PLAIN = re.compile(r'[^",\s\x00-\x1f\x7f]+')
# 인덱스 안의 인덱스는 옮기지 않는다
MAX_DEPTH = 2


class MirrorStop(Exception):
    """미러가 멈춘 까닭. 한 줄로 찍고 1 로 끝난다."""


Cosign = Callable[[list[str], Mapping[str, str]], subprocess.CompletedProcess]
ClientFor = Callable[[str, bool], Any]


def run_cosign(args: list[str], env: Mapping[str, str]) -> subprocess.CompletedProcess:
    """cosign 을 부른다. env 는 이 호출에만 더한다(비밀번호)."""
    return subprocess.run(
        ["cosign", *args], env={**os.environ, **env}, capture_output=True, text=True, check=False
    )


def registry_client(host: str, push: bool) -> oci_registry.Client:
    """push 면 docker login 의 자격 증명으로, 아니면 익명으로 읽는 클라이언트."""
    if not push:
        return oci_registry.Client(host)
    from registry_auth import credentials

    return oci_registry.Client(host, basic=credentials(host))


def plain(name: str, value: str) -> str:
    if not _PLAIN.fullmatch(value):
        raise MirrorStop(f"{name} must hold no spaces, quotes, commas or control characters")
    return value


def platform_commit() -> str | None:
    """돌린 플랫폼 체크아웃의 커밋. 읽지 못하면 None 이다."""
    try:
        r = subprocess.run(
            ["git", "-C", str(ROOT), "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
            check=False,
        )
    except OSError:
        return None
    sha = r.stdout.strip()
    return sha if r.returncode == 0 and re.fullmatch(r"[0-9a-f]{40}", sha) else None


def run_record(env: Mapping[str, str], platform: str | None = None) -> str:
    """`dev.daegam.hub.run` 의 처음 칸. job 이면 실행 주소, workflow 커밋과 ref 를 더하고, 플랫폼
    커밋을 알면 `platform` 이다. 0단계와 2단계의 칸은 `Mirror.mirror` 가 더한다."""
    if env.get("GITHUB_ACTIONS") == "true":
        url = "{}/{}/actions/runs/{}".format(
            env.get("GITHUB_SERVER_URL", "https://github.com"),
            env.get("GITHUB_REPOSITORY", ""),
            env.get("GITHUB_RUN_ID", ""),
        )
        pairs = [
            ("by", env.get("GITHUB_ACTOR", "")),
            ("via", "job"),
            ("url", url),
            ("commit", env.get("GITHUB_WORKFLOW_SHA", "")),
            ("ref", env.get("GITHUB_WORKFLOW_REF", "")),
        ]
    else:
        pairs = [("by", getpass.getuser()), ("via", "make")]
    if platform:
        pairs.append(("platform", platform))
    return " ".join(f"{k}={plain('run ' + k, v)}" for k, v in pairs)


def prefix_parts(prefix: str) -> tuple[str, str]:
    """허브 접두사의 (호스트, 호스트 뒤 경로). 정규화는 플랫폼의 `split_prefix` 다."""
    try:
        return split_prefix(prefix)
    except ValueError as e:
        raise MirrorStop(f"HUB_PREFIX: {e}") from e


def parsed(name: str, value: str) -> ImageRef:
    try:
        return parse_image(value)
    except ValueError as e:
        raise MirrorStop(f"{name}: {e}") from e


# -- 2단계: upstream 서명 ---------------------------------------------------------------------


def _der_utf8(value: bytes) -> str:
    """v2 Fulcio 확장 값은 DER UTF8String 이다."""
    if value[:1] != b"\x0c":
        return value.decode()
    n = value[1]
    if n & 0x80:
        k = n & 0x7F
        n = int.from_bytes(value[2 : 2 + k], "big")
        return value[2 + k : 2 + k + n].decode()
    return value[2 : 2 + n].decode()


def cert_fields(bundle: Mapping[str, Any]) -> dict[str, str]:
    """번들 인증서의 칸. 발급자, runner 환경, 저장소 URI, ref, SAN 이다. 인증서가 없거나 읽지
    못하거나 칸이 없으면 그 칸은 빈 글이다. 빈 칸은 카탈로그의 출처와 맞지 않으므로 그 번들은
    거절되고, 미러는 한 줄로 멈춘다."""
    out = dict.fromkeys(("issuer", "runner_environment", "repository_uri", "ref", "san"), "")
    vm = bundle.get("verificationMaterial")
    vm = vm if isinstance(vm, Mapping) else {}
    raw = (vm.get("certificate") or {}).get("rawBytes")
    if raw is None:
        chain = (vm.get("x509CertificateChain") or {}).get("certificates") or [{}]
        raw = chain[0].get("rawBytes") if isinstance(chain[0], Mapping) else None
    try:
        cert = x509.load_der_x509_certificate(base64.b64decode(raw or "", validate=True))
    except (ValueError, TypeError):
        return out
    for name, oid in (
        ("issuer", OID_ISSUER),
        ("runner_environment", OID_RUNNER),
        ("repository_uri", OID_REPOSITORY),
        ("ref", OID_REF),
    ):
        try:
            ext = cert.extensions.get_extension_for_oid(x509.ObjectIdentifier(oid))
            out[name] = _der_utf8(ext.value.value)
        except (x509.ExtensionNotFound, ValueError, IndexError, AttributeError):
            continue
    try:
        san = cert.extensions.get_extension_for_class(x509.SubjectAlternativeName).value
    except (x509.ExtensionNotFound, ValueError):
        return out
    uris = san.get_values_for_type(x509.UniformResourceIdentifier)
    out["san"] = uris[0] if uris else ""
    return out


def signer_identity(fields: Mapping[str, str]) -> str:
    """ToolHive core 의 서명자 신원. SAN 에서 `@ref` 를 떼고 앞의 저장소 URI 를 뗀 것이다."""
    ident = fields.get("san", "")
    if fields.get("issuer") != GITHUB_ISSUER:
        return ident
    ident = ident.split("@", 1)[0]
    repo = fields.get("repository_uri", "")
    return ident[len(repo) :] if repo and ident.startswith(repo) else ident


def ref_allowed(catalog_ref: str, actual: str, same_tag: bool) -> bool:
    """카탈로그의 ref 와 실제 ref (6절 2단계). 카탈로그 태그는 정확히 같아야 한다. 다른 태그는
    카탈로그가 태그 ref 면 같은 저장소의 태그 ref, 브랜치 ref 면 같은 브랜치다."""
    if not catalog_ref:
        return True
    if same_tag:
        return actual == catalog_ref
    if catalog_ref.startswith("refs/tags/"):
        return actual.startswith("refs/tags/")
    return actual == catalog_ref


def mismatches(fields: Mapping[str, str], prov: Mapping[str, Any], *, same_tag: bool) -> list[str]:
    """카탈로그가 정한 칸 가운데 인증서와 다른 것."""
    bad = []
    pairs = (
        ("repository_uri", "repository_uri"),
        ("runner_environment", "runner_environment"),
        ("cert_issuer", "issuer"),
    )
    for want, have in pairs:
        if prov.get(want) and prov[want] != fields.get(have):
            bad.append(want)
    if prov.get("signer_identity") and prov["signer_identity"] != signer_identity(fields):
        bad.append("signer_identity")
    if not ref_allowed(str(prov.get("repository_ref") or ""), fields.get("ref", ""), same_tag):
        bad.append("repository_ref")
    return bad


def identity_regexp(prov: Mapping[str, Any]) -> str:
    """cosign 의 신원 정규식. 저장소 URI 와 workflow 경로를 묶는다."""
    return "^" + re.escape(str(prov["repository_uri"]) + str(prov["signer_identity"])) + "@"


# -- 미러 --------------------------------------------------------------------------------


@dataclass
class Mirror:
    """미러 한 벌. client 는 (호스트, push) 의 레지스트리 클라이언트를 짓고, cosign 은 cosign 을
    부른다. 테스트가 둘을 대역으로 바꾼다."""

    catalog: Catalog
    prefix: str
    key: str
    password: str
    run: str
    client: ClientFor = registry_client
    cosign: Cosign = run_cosign
    log: Callable[[str], None] = field(default=lambda m: print(m, file=sys.stderr))

    # 0단계
    def plan(self, upstream: str, server_name: str | None) -> tuple[ImageRef, CatalogServer | None]:
        ref = parsed("HUB_UPSTREAM", upstream)
        if ref.tag is None:
            raise MirrorStop(f"HUB_UPSTREAM needs a tag: {upstream}")
        same = [
            s for s in self.catalog.servers if s.image and _repo(s) == (ref.host, ref.repository)
        ]
        if server_name is None:
            if same:
                raise MirrorStop(
                    f"{ref.host}/{ref.repository} is catalog server {same[0].name}'s image; "
                    "name it with HUB_SERVER"
                )
            return ref, None
        server = self.catalog.server(server_name)
        if server is None or server.kind != "image" or server.image is None:
            raise MirrorStop(f"the catalog snapshot has no image server {server_name}")
        if _repo(server) != (ref.host, ref.repository):
            raise MirrorStop(
                f"catalog server {server_name}'s image is {server.image.repository}, "
                f"not {ref.host}/{ref.repository}"
            )
        if ref.tag != server.image.tag:
            self.log(f"tag {ref.tag} differs from the catalog's {server.image.tag}; recorded")
        return ref, server

    # 1단계
    def resolve(self, ref: ImageRef) -> str:
        """태그를 digest 로 푼다. 참조가 digest 를 함께 들면 두 값이 같아야 한다."""
        _, _, digest = self.client(ref.host, False).manifest(ref.repository, ref.tag)
        if ref.digest is not None and ref.digest != digest:
            raise MirrorStop(f"tag {ref.tag} resolves to {digest}, not the given {ref.digest}")
        return digest

    # 2단계
    def provenance(
        self, ref: ImageRef, digest: str, server: CatalogServer | None, approve: bool
    ) -> tuple[str, str | None]:
        """(provenance, 확인한 upstream 서명의 ref)."""
        prov = server.provenance if server is not None else None
        if not prov:
            if not approve:
                raise MirrorStop(
                    "the catalog has no provenance for this image; "
                    "set HUB_APPROVE=yes to approve it"
                )
            return hub_verifier.APPROVED, None
        if approve:
            raise MirrorStop("HUB_APPROVE is refused: the catalog has provenance for this server")
        if not prov.get("repository_uri") or not prov.get("signer_identity"):
            raise MirrorStop(
                "the catalog's provenance for this server lacks repository_uri or "
                "signer_identity, so the signer cannot be bound"
            )
        same_tag = server is not None and server.image is not None and ref.tag == server.image.tag
        upstream = self.client(ref.host, False)
        for desc in upstream.referrers(ref.repository, digest):
            bundle, raw = self._upstream_bundle(upstream, ref.repository, desc)
            if bundle is None:
                continue
            why = self._check_bundle(raw, bundle, digest, prov, same_tag=same_tag)
            if not why:
                actual = cert_fields(bundle).get("ref") or None
                self.log(f"upstream signature verified (ref {actual})")
                return hub_verifier.VERIFIED, actual
            self.log(f"upstream bundle {desc.get('digest')} not accepted: {why}")
        raise MirrorStop(
            "no upstream signature passes the catalog's provenance; the check is final"
        )

    def _upstream_bundle(
        self, client: Any, repo: str, desc: Mapping[str, Any]
    ) -> tuple[dict[str, Any] | None, bytes]:
        ref = str(desc.get("digest") or "")
        if not SHA256_DIGEST.fullmatch(ref):
            return None, b""
        raw_man, _, _ = client.manifest(repo, ref)
        man = json.loads(raw_man)
        layers = man.get("layers") or []
        # 태그 대체 경로의 인덱스는 번들에 빈 config 의 종류를 적으므로 종류는 manifest 에서 읽는다
        if not layers or (
            man.get("artifactType") != hub_verifier.BUNDLE_TYPE
            and layers[0].get("mediaType") != hub_verifier.BUNDLE_TYPE
        ):
            return None, b""
        size = layers[0].get("size")
        if not isinstance(size, int) or size > hub_verifier.MAX_BUNDLE_BYTES:
            return None, b""
        raw = client.blob(repo, layers[0]["digest"], size)
        return json.loads(raw), raw

    def _check_bundle(
        self,
        raw: bytes,
        bundle: Mapping[str, Any],
        digest: str,
        prov: Mapping[str, Any],
        *,
        same_tag: bool,
    ) -> str:
        env = bundle.get("dsseEnvelope") or {}
        try:
            predicate = json.loads(base64.b64decode(env.get("payload") or ""))["predicateType"]
        except (ValueError, KeyError, TypeError):
            return "not a DSSE bundle"
        with tempfile.NamedTemporaryFile("wb", suffix=".json", delete=False) as f:
            f.write(raw)
        try:
            issuer = (
                ["--certificate-oidc-issuer", str(prov["cert_issuer"])]
                if prov.get("cert_issuer")
                else ["--certificate-oidc-issuer-regexp", ".*"]
            )
            r = self.cosign(
                [
                    "verify-blob-attestation",
                    "--bundle",
                    f.name,
                    "--digest",
                    digest.removeprefix("sha256:"),
                    "--digestAlg",
                    "sha256",
                    "--type",
                    predicate,
                    *issuer,
                    "--certificate-identity-regexp",
                    identity_regexp(prov),
                ],
                {},
            )
        finally:
            os.unlink(f.name)
        if r.returncode != 0:
            lines = (r.stderr or "").strip().splitlines()
            return "cosign: " + (lines[-1] if lines else "verification failed")
        return ", ".join(mismatches(cert_fields(bundle), prov, same_tag=same_tag))

    # 3단계
    def copy(self, ref: ImageRef, digest: str) -> str:
        host, path = prefix_parts(self.prefix)
        dst_repo = ref.mirror_repository(path)
        src = self.client(ref.host, False)
        dst = self.client(host, True)
        raw, media = self._copy_manifest(src, ref.repository, dst, dst_repo, digest, depth=1)
        if ref.tag:
            self._put(dst, dst_repo, ref.tag, raw, media)
        _, _, copied = dst.manifest(dst_repo, digest)
        by_tag = dst.manifest(dst_repo, ref.tag)[2] if ref.tag else copied
        if copied != digest or by_tag != digest:
            raise MirrorStop(f"the copy's digest differs: {copied} (tag {by_tag}) is not {digest}")
        return dst_repo

    def _copy_manifest(
        self, src: Any, src_repo: str, dst: Any, dst_repo: str, digest: str, *, depth: int
    ) -> tuple[bytes, str]:
        """manifest 하나와 그 아래를 옮긴다. 자식(플랫폼 이미지, attestation)을 먼저 옮긴다.
        서술자의 digest 는 `sha256:<hex>` 여야 하고 blob 은 서술자의 크기까지만 읽는다."""
        if not SHA256_DIGEST.fullmatch(digest):
            raise MirrorStop(f"a descriptor digest is not sha256:<hex>: {digest!r}")
        raw, media, got = src.manifest(src_repo, digest)
        doc = json.loads(raw)
        if media in oci_registry.INDEX_TYPES:
            if depth >= MAX_DEPTH:
                raise MirrorStop(f"{digest} nests an index inside an index")
            for child in doc.get("manifests") or []:
                self._copy_manifest(src, src_repo, dst, dst_repo, child["digest"], depth=depth + 1)
        else:
            for desc in [doc["config"], *(doc.get("layers") or [])]:
                d, size = desc.get("digest"), desc.get("size")
                if not SHA256_DIGEST.fullmatch(str(d)) or not isinstance(size, int) or size < 0:
                    raise MirrorStop(f"a descriptor in {got} has no sha256 digest or size")
                if not dst.has_blob(dst_repo, d):
                    dst.put_blob(dst_repo, d, src.blob(src_repo, d, size))
        self._put(dst, dst_repo, got, raw, media)
        return raw, media

    @staticmethod
    def _put(dst: Any, repo: str, ref: str, raw: bytes, media: str) -> None:
        try:
            dst.put_manifest(repo, ref, raw, media)
        except oci_registry.RegistryError as e:
            if media in oci_registry.DOCKER_TYPES and e.status in (400, 415):
                raise MirrorStop(
                    "the hub registry refused a Docker schema 2 manifest; it must accept them "
                    '(zot: http.compat ["docker2s2"]). The mirror never converts, since that '
                    "changes the digest"
                ) from e
            raise

    # 4단계
    def sign(self, dst_repo: str, digest: str, annotations: Mapping[str, str]) -> None:
        """허브 키로 서명한다. 서비스 없는 signing config 라 투명성 로그에 올리지 않는다."""
        host, _ = prefix_parts(self.prefix)
        for k, v in annotations.items():
            if not _CSV_SAFE.fullmatch(v):
                raise MirrorStop(
                    f"annotation {k} must hold no quotes, commas or control characters"
                )
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as f:
            json.dump(SIGNING_CONFIG, f)
        args = ["sign", "--key", self.key, "--signing-config", f.name, "-y"]
        for k, v in annotations.items():
            args += ["-a", f"{k}={v}"]
        try:
            r = self.cosign(
                [*args, f"{host}/{dst_repo}@{digest}"], {"COSIGN_PASSWORD": self.password}
            )
        finally:
            os.unlink(f.name)
        if r.returncode != 0:
            raise MirrorStop("cosign sign failed: " + (r.stderr or "").strip()[-500:])

    def public_key(self) -> hub_verifier.HubKey:
        r = self.cosign(["public-key", "--key", self.key], {"COSIGN_PASSWORD": self.password})
        if r.returncode != 0:
            raise MirrorStop("cosign public-key failed: " + (r.stderr or "").strip()[-500:])
        return hub_verifier.load_key(r.stdout)

    def hub_signatures(
        self, dst_repo: str, digest: str, keys: list[hub_verifier.HubKey]
    ) -> list[hub_verifier.HubSignature]:
        """플랫폼의 검증기와 같은 규칙(`referrer_layer`, `read_bundle`, `binds_repository`)으로
        사본의 허브 서명을 모은다."""
        host, path = prefix_parts(self.prefix)
        dst = self.client(host, True)
        out = []
        for desc in dst.referrers(dst_repo, digest):
            ref = str(desc.get("digest") or "")
            if not SHA256_DIGEST.fullmatch(ref):
                continue
            layer, _ = hub_verifier.referrer_layer(dst.manifest(dst_repo, ref)[0])
            if layer is None:
                continue
            raw = dst.blob(dst_repo, layer["digest"], layer["size"])
            sig, _ = hub_verifier.read_bundle(raw, keys, digest, bundle=layer["digest"])
            if sig is not None and hub_verifier.binds_repository(sig, path, dst_repo):
                out.append(sig)
        return out

    def check_signed(self, dst_repo: str, digest: str, upstream: str, provenance: str) -> None:
        """방금 단 서명을 플랫폼의 검증기 규칙으로 다시 읽어 본다."""
        sigs = self.hub_signatures(dst_repo, digest, [self.public_key()])
        if not any(s.upstream == upstream and s.provenance == provenance for s in sigs):
            raise MirrorStop("the signature just made does not read back with the hub key")

    def copy_only(self, upstream: str) -> str:
        """서명하지 않은 사본. 1단계와 3단계만 하고 사본의 digest 로 고정한 참조를 돌려준다."""
        ref = parsed("HUB_UPSTREAM", upstream)
        if ref.tag is None:
            raise MirrorStop(f"HUB_UPSTREAM needs a tag: {upstream}")
        digest = self.resolve(ref)
        dst_repo = self.copy(ref, digest)
        host, _ = prefix_parts(self.prefix)
        return f"{host}/{dst_repo}@{digest}"

    def mirror(self, upstream: str, server_name: str | None, approve: bool) -> str:
        ref, server = self.plan(upstream, server_name)
        digest = self.resolve(ref)
        provenance, actual_ref = self.provenance(ref, digest, server, approve)
        dst_repo = self.copy(ref, digest)
        signed_upstream = f"{ref.host}/{ref.repository}:{ref.tag}@{digest}"
        run = f"{self.run} catalog={plain('run catalog', self.catalog.release)}"
        if actual_ref:
            run += f" upstream_ref={plain('run upstream_ref', actual_ref)}"
        annotations = {
            hub_verifier.UPSTREAM: signed_upstream,
            hub_verifier.PROVENANCE: provenance,
            hub_verifier.RUN: run,
        }
        if server is not None:
            annotations[hub_verifier.CATALOG] = f"{server.name}@{self.catalog.release}"
        self.sign(dst_repo, digest, annotations)
        self.check_signed(dst_repo, digest, signed_upstream, provenance)
        host, _ = prefix_parts(self.prefix)
        return f"{host}/{dst_repo}@{digest}"

    def resign(self, image: str, keep: list[str], refused: set[str]) -> int:
        """키 바꾸기. 남길 키로 검증되는 서명마다 그 서명한 내용을 새 키로 서명한다. 서명한 수다."""
        ref = parsed("HUB_IMAGE", image)
        host, path = prefix_parts(self.prefix)
        under = not path or ref.repository.startswith(f"{path}/")
        if ref.digest is None or ref.host != host or not under:
            raise MirrorStop(f"HUB_IMAGE must be a hub copy with a digest under {self.prefix}")
        if ref.digest in refused:
            self.log(f"{ref.digest} is refused; skipped")
            return 0
        keys = [hub_verifier.load_key(p) for p in keep]
        contents = {
            (s.upstream, s.provenance, s.catalog)
            for s in self.hub_signatures(ref.repository, ref.digest, keys)
        }
        if not contents:
            raise MirrorStop("no hub signature verifies under HUB_KEEP_KEYS")
        for upstream, provenance, catalog in sorted(contents, key=str):
            annotations = {
                hub_verifier.UPSTREAM: upstream,
                hub_verifier.PROVENANCE: provenance,
                hub_verifier.RUN: self.run,
            }
            if catalog is not None:
                annotations[hub_verifier.CATALOG] = f"{catalog[0]}@{catalog[1]}"
            self.sign(ref.repository, ref.digest, annotations)
            self.check_signed(ref.repository, ref.digest, upstream, provenance)
        return len(contents)


def _repo(server: CatalogServer) -> tuple[str, str]:
    assert server.image is not None
    ref = parse_image(f"{server.image.repository}:{server.image.tag}")
    return ref.host, ref.repository


def key_password(env: Mapping[str, str]) -> str:
    """키의 비밀번호. COSIGN_PASSWORD, HUB_KEY_PASSWORD_FILE 의 파일, 터미널 차례다."""
    if env.get("COSIGN_PASSWORD") is not None:
        return env["COSIGN_PASSWORD"]
    path = env.get("HUB_KEY_PASSWORD_FILE")
    if path:
        return Path(path).read_text().rstrip("\r\n")
    return getpass.getpass("hub key password: ")


def required(env: Mapping[str, str], name: str) -> str:
    value = (env.get(name) or "").strip()
    if not value:
        raise MirrorStop(f"{name} is empty")
    return value


def main(argv: list[str], env: Mapping[str, str] = os.environ) -> int:
    mode = argv[1] if len(argv) > 1 else "mirror"
    if mode not in ("mirror", "resign", "copy"):
        print("usage: hub_mirror.py mirror|resign|copy (inputs through env)", file=sys.stderr)
        return 2
    try:
        if mode == "copy":
            prefix = required(env, "HUB_PREFIX")
            prefix_parts(prefix)
            copier = Mirror(catalog=snapshot(), prefix=prefix, key="", password="", run="")
            print(copier.copy_only(required(env, "HUB_UPSTREAM")))
            return 0
        key = required(env, "HUB_KEY")
        if not Path(key).is_file():
            raise MirrorStop(f"HUB_KEY {key} is not a file")
        prefix = required(env, "HUB_PREFIX")
        prefix_parts(prefix)
        mirror = Mirror(
            catalog=snapshot(),
            prefix=prefix,
            key=key,
            password=key_password(env),
            run=run_record(env, platform_commit()),
        )
        if mode == "mirror":
            server = (env.get("HUB_SERVER") or "").strip() or None
            approve = (env.get("HUB_APPROVE") or "").strip().lower() in ("yes", "true", "1")
            print(mirror.mirror(required(env, "HUB_UPSTREAM"), server, approve))
        else:
            keep = [Path(p).read_text() for p in required(env, "HUB_KEEP_KEYS").split()]
            refused = set((env.get("HUB_REFUSED") or "").split())
            print(mirror.resign(required(env, "HUB_IMAGE"), keep, refused))
    except (MirrorStop, oci_registry.RegistryError, OSError, ValueError) as e:
        print(f"hub-mirror: {e}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
