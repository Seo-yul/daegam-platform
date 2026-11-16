"""P55 6절의 허브 미러 (scripts/hub_mirror.py). 레지스트리 대역과 cosign 대역으로 0~4단계와 다시
서명하기를 본다. cosign 대역은 서명을 테스트가 만든 P-256 키로 실제로 만들어, 미러가 플랫폼의
검증기로 다시 읽는 것까지 돈다.
"""

from __future__ import annotations

import base64
import datetime as dt
import json
import re
import subprocess
from pathlib import Path
from typing import Any

import pytest
import yaml
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID

from daegam.services import hub_verifier
from daegam.services.hub_catalog import Catalog
from tests.helpers.consts import REPO
from tests.helpers.hub_registry import (
    DOCKER_MANIFEST_TYPE,
    MANIFEST_TYPE,
    public_pem,
    referrer_manifest,
    signed_bundle,
    statement,
)
from tests.helpers.registry import INDEX_TYPE, digest_of
from tests.helpers.source import runtime_module

hub_mirror = runtime_module(REPO / "scripts" / "hub_mirror.py")
oci_registry = hub_mirror.oci_registry

PREFIX = "hub.test/hub"
UPSTREAM = "ghcr.io/acme/tools"
ISSUER = hub_mirror.GITHUB_ISSUER
PROVENANCE = {
    "repository_uri": "https://github.com/acme/tools",
    "signer_identity": "/.github/workflows/release.yml",
    "cert_issuer": ISSUER,
    "runner_environment": "github-hosted",
}


def catalog() -> Catalog:
    image = {"stdio": True, "transport": "streamable-http", "kind": "image"}
    return Catalog.model_validate(
        {
            "catalog": "test",
            "release": "v1",
            "license": "Apache-2.0",
            "servers": [
                {
                    **image,
                    "name": "tools",
                    "title": "Tools",
                    "image": {"repository": UPSTREAM, "tag": "1.0.0"},
                    "provenance": PROVENANCE,
                },
                {
                    **image,
                    "name": "plain",
                    "title": "Plain",
                    "image": {"repository": "ghcr.io/acme/plain", "tag": "2"},
                },
            ],
        }
    )


# -- 레지스트리 대역 -------------------------------------------------------------------------


class FakeRegistries:
    """호스트마다 저장소를 두는 레지스트리. subject 를 든 manifest 를 받으면 그 digest 의
    referrers 에 더한다(zot 처럼). docker 를 끄면 Docker schema 2 manifest 를 415 로 거절한다."""

    def __init__(self) -> None:
        self.repos: dict[tuple[str, str], dict[str, Any]] = {}
        self.docker = True
        self.retag: bytes | None = None

    def repo(self, host: str, name: str) -> dict[str, Any]:
        return self.repos.setdefault(
            (host, name), {"manifests": {}, "tags": {}, "blobs": {}, "referrers": {}}
        )

    def client(self, host: str, push: bool) -> FakeClient:
        return FakeClient(self, host)

    def seed_image(self, host: str, name: str, tag: str, media: str = MANIFEST_TYPE) -> str:
        """두 플랫폼 이미지의 인덱스 하나. 인덱스 digest 를 돌려준다."""
        r = self.repo(host, name)
        children = []
        for arch in ("amd64", "arm64"):
            config = json.dumps({"architecture": arch}).encode()
            layer = f"layer-{arch}".encode()
            r["blobs"][digest_of(config)] = config
            r["blobs"][digest_of(layer)] = layer
            man = json.dumps(
                {
                    "schemaVersion": 2,
                    "mediaType": media,
                    "config": {"mediaType": "x", "digest": digest_of(config), "size": len(config)},
                    "layers": [{"mediaType": "y", "digest": digest_of(layer), "size": len(layer)}],
                }
            ).encode()
            r["manifests"][digest_of(man)] = (man, media)
            children.append({"mediaType": media, "digest": digest_of(man), "size": len(man)})
        index = json.dumps(
            {"schemaVersion": 2, "mediaType": INDEX_TYPE, "manifests": children}
        ).encode()
        r["manifests"][digest_of(index)] = (index, INDEX_TYPE)
        r["tags"][tag] = digest_of(index)
        return digest_of(index)


class FakeClient:
    def __init__(self, regs: FakeRegistries, host: str) -> None:
        self.regs = regs
        self.host = host

    def manifest(self, repo: str, ref: str) -> tuple[bytes, str, str]:
        r = self.regs.repo(self.host, repo)
        digest = r["tags"].get(ref, ref)
        if digest not in r["manifests"]:
            raise oci_registry.RegistryError(404, f"{repo}@{ref}")
        raw, media = r["manifests"][digest]
        return raw, media, digest_of(raw)

    def blob(self, repo: str, digest: str, size: int) -> bytes:
        data = self.regs.repo(self.host, repo)["blobs"][digest]
        assert len(data) == size
        return data

    def has_blob(self, repo: str, digest: str) -> bool:
        return digest in self.regs.repo(self.host, repo)["blobs"]

    def put_blob(self, repo: str, digest: str, data: bytes) -> None:
        self.regs.repo(self.host, repo)["blobs"][digest] = data

    def put_manifest(self, repo: str, ref: str, raw: bytes, media: str) -> None:
        if not self.regs.docker and media == DOCKER_MANIFEST_TYPE:
            raise oci_registry.RegistryError(415, "MANIFEST_INVALID")
        r = self.regs.repo(self.host, repo)
        if not ref.startswith("sha256:") and self.regs.retag is not None:
            raw = self.regs.retag
        r["manifests"][digest_of(raw)] = (raw, media)
        if ref.startswith("sha256:"):
            subject = json.loads(raw).get("subject")
            if subject:
                desc = {
                    "mediaType": media,
                    "digest": digest_of(raw),
                    "size": len(raw),
                    "artifactType": json.loads(raw).get("artifactType"),
                }
                r["referrers"].setdefault(subject["digest"], []).append(desc)
        else:
            r["tags"][ref] = digest_of(raw)

    def referrers(self, repo: str, digest: str) -> list[dict[str, Any]]:
        return list(self.regs.repo(self.host, repo)["referrers"].get(digest, []))


def put_bundle(client: FakeClient, repo: str, digest: str, bundle: dict[str, Any]) -> None:
    """번들 하나를 referrer 로 둔다(cosign 3 의 모양)."""
    raw = json.dumps(bundle).encode()
    client.put_blob(repo, digest_of(raw), raw)
    client.put_blob(repo, digest_of(b"{}"), b"{}")
    body = referrer_manifest(raw, digest)
    client.put_manifest(repo, digest_of(body), body, MANIFEST_TYPE)


def _utf8(value: str) -> bytes:
    raw = value.encode()
    return b"\x0c" + bytes([len(raw)]) + raw


def upstream_bundle(
    digest: str, *, repo: str = "acme/tools", ref: str = "refs/tags/v1.0.0", san: bool = True
):
    """GitHub Actions 의 키 없는 서명처럼 Fulcio 칸을 든 인증서의 번들. 서명은 대역이 보지
    않는다. san 이 거짓이면 SAN 없는 인증서다."""
    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "sigstore")])
    now = dt.datetime.now(dt.UTC)
    uri = f"https://github.com/{repo}/.github/workflows/release.yml@{ref}"
    builder = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(1)
        .not_valid_before(now)
        .not_valid_after(now + dt.timedelta(minutes=10))
    )
    if san:
        builder = builder.add_extension(
            x509.SubjectAlternativeName([x509.UniformResourceIdentifier(uri)]), False
        )
    for oid, value in (
        (hub_mirror.OID_ISSUER, ISSUER),
        (hub_mirror.OID_RUNNER, "github-hosted"),
        (hub_mirror.OID_REPOSITORY, f"https://github.com/{repo}"),
        (hub_mirror.OID_REF, ref),
    ):
        builder = builder.add_extension(
            x509.UnrecognizedExtension(x509.ObjectIdentifier(oid), _utf8(value)), False
        )
    cert = builder.sign(key, hashes.SHA256())
    return {
        "mediaType": hub_verifier.BUNDLE_TYPE,
        "verificationMaterial": {
            "certificate": {
                "rawBytes": base64.b64encode(cert.public_bytes(serialization.Encoding.DER)).decode()
            }
        },
        "dsseEnvelope": {
            "payloadType": hub_verifier.INTOTO_TYPE,
            "payload": base64.b64encode(statement(digest)).decode(),
            "signatures": [{"sig": "AA=="}],
        },
    }


# -- cosign 대역 ----------------------------------------------------------------------------


class FakeCosign:
    """cosign 의 세 명령. sign 은 키 파일 이름으로 고른 P-256 키로 실제 DSSE 서명을 만들어 대역
    레지스트리에 referrer 로 둔다. verify-blob-attestation 은 신원 정규식과 digest 를 맞춰 본다."""

    def __init__(self, regs: FakeRegistries, *, permissive: bool = False) -> None:
        self.regs = regs
        self.keys: dict[str, ec.EllipticCurvePrivateKey] = {}
        self.calls: list[tuple[list[str], dict[str, str]]] = []
        self.permissive = permissive

    def key(self, path: str) -> ec.EllipticCurvePrivateKey:
        return self.keys.setdefault(path, ec.generate_private_key(ec.SECP256R1()))

    def pem(self, path: str) -> str:
        return public_pem(self.key(path))

    def __call__(self, args: list[str], env: dict[str, str]) -> subprocess.CompletedProcess:
        self.calls.append((list(args), dict(env)))
        cmd = args[0]
        if cmd == "public-key":
            return subprocess.CompletedProcess(args, 0, self.pem(args[args.index("--key") + 1]), "")
        if cmd == "verify-blob-attestation":
            return self._verify(args)
        if cmd == "sign":
            return self._sign(args, env)
        raise AssertionError(args)

    def _verify(self, args: list[str]) -> subprocess.CompletedProcess:
        bundle = json.loads(Path(args[args.index("--bundle") + 1]).read_text())
        want = args[args.index("--digest") + 1]
        identity = args[args.index("--certificate-identity-regexp") + 1]
        st = json.loads(base64.b64decode(bundle["dsseEnvelope"]["payload"]))
        san = hub_mirror.cert_fields(bundle)["san"]
        ok = st["subject"][0]["digest"]["sha256"] == want and (
            self.permissive or re.match(identity, san) is not None
        )
        return subprocess.CompletedProcess(args, 0 if ok else 1, "", "" if ok else "no match")

    def _sign(self, args: list[str], env: dict[str, str]) -> subprocess.CompletedProcess:
        assert env.get("COSIGN_PASSWORD") == "pw"
        config = json.loads(Path(args[args.index("--signing-config") + 1]).read_text())
        assert config == hub_mirror.SIGNING_CONFIG
        key = self.key(args[args.index("--key") + 1])
        annotations = {}
        for i, a in enumerate(args):
            if a == "-a":
                k, _, v = args[i + 1].partition("=")
                annotations[k] = v
        target = args[-1]
        host, _, rest = target.partition("/")
        repo, _, digest = rest.partition("@")
        bundle = signed_bundle(key, statement(digest, annotations))
        put_bundle(FakeClient(self.regs, host), repo, digest, bundle)
        return subprocess.CompletedProcess(args, 0, "", "")


@pytest.fixture
def regs() -> FakeRegistries:
    return FakeRegistries()


def mirror(regs: FakeRegistries, cosign: FakeCosign | None = None, key: str = "a.key"):
    return hub_mirror.Mirror(
        catalog=catalog(),
        prefix=PREFIX,
        key=key,
        password="pw",
        run="by=tester via=make",
        client=regs.client,
        cosign=cosign or FakeCosign(regs),
        log=lambda _m: None,
    )


def hub_annotations(regs: FakeRegistries, repo: str, digest: str) -> list[dict[str, str]]:
    client = FakeClient(regs, "hub.test")
    out = []
    for d in client.referrers(repo, digest):
        layer = json.loads(client.manifest(repo, d["digest"])[0])["layers"][0]
        bundle = json.loads(client.blob(repo, layer["digest"], layer["size"]))
        st = json.loads(base64.b64decode(bundle["dsseEnvelope"]["payload"]))
        out.append(st["subject"][0]["annotations"])
    return out


def seeded(
    regs: FakeRegistries,
    *,
    repo: str = "acme/tools",
    ref: str = "refs/tags/v1.0.0",
    bundle: dict[str, Any] | None = None,
):
    digest = regs.seed_image("ghcr.io", "acme/tools", "1.0.0")
    put_bundle(
        FakeClient(regs, "ghcr.io"),
        "acme/tools",
        digest,
        bundle or upstream_bundle(digest, repo=repo, ref=ref),
    )
    return digest


# -- 테스트 --------------------------------------------------------------------------------

RUN = "by=tester via=make"


def test_a_catalogued_image_is_copied_signed_and_read_back(regs) -> None:
    digest = seeded(regs)
    cosign = FakeCosign(regs)
    out = mirror(regs, cosign).mirror(f"{UPSTREAM}:1.0.0", "tools", approve=False)
    dst = "hub/ghcr.io/acme/tools"
    assert out == f"hub.test/{dst}@{digest}"
    hub = regs.repo("hub.test", dst)
    assert hub["tags"]["1.0.0"] == digest
    # 인덱스와 두 플랫폼 이미지, 그 blob 이 그대로다. upstream 의 서명은 옮기지 않는다
    up = regs.repo("ghcr.io", "acme/tools")
    for child in json.loads(up["manifests"][digest][0])["manifests"]:
        assert child["digest"] in hub["manifests"]
        man = json.loads(up["manifests"][child["digest"]][0])
        wanted = {man["config"]["digest"], *(layer["digest"] for layer in man["layers"])}
        assert wanted <= set(hub["blobs"])
    (ann,) = hub_annotations(regs, dst, digest)
    assert ann == {
        hub_verifier.UPSTREAM: f"ghcr.io/acme/tools:1.0.0@{digest}",
        hub_verifier.PROVENANCE: "verified",
        hub_verifier.CATALOG: "tools@v1",
        hub_verifier.RUN: f"{RUN} catalog=v1 upstream_ref=refs/tags/v1.0.0",
    }
    sign = next(args for args, _ in cosign.calls if args[0] == "sign")
    assert "--signing-config" in sign and "--tlog-upload=false" not in sign
    # 플랫폼의 검증기가 그 서명을 모은다
    key = hub_verifier.load_key(cosign.pem("a.key"))
    sigs = mirror(regs, cosign).hub_signatures(dst, digest, [key])
    assert [s.provenance for s in sigs] == ["verified"]


def test_another_repository_with_the_same_workflow_path_is_refused(regs) -> None:
    """같은 workflow 경로를 쓰는 다른 저장소의 서명은 cosign 의 신원과 인증서의 저장소 URI 가 모두
    거절한다. 실패는 끝이다."""
    seeded(regs, repo="acme/other")
    with pytest.raises(hub_mirror.MirrorStop, match="final"):
        mirror(regs).mirror(f"{UPSTREAM}:1.0.0", "tools", approve=False)
    with pytest.raises(hub_mirror.MirrorStop, match="final"):
        mirror(regs, FakeCosign(regs, permissive=True)).mirror(
            f"{UPSTREAM}:1.0.0", "tools", approve=False
        )
    assert ("hub.test", "hub/ghcr.io/acme/tools") not in regs.repos


def test_a_certificate_without_a_san_or_fields_is_a_one_line_refusal(regs) -> None:
    """SAN 이 없거나 칸이 빠진 인증서는 트레이스백 없이 출처 확인의 거절로 끝난다."""
    digest = regs.seed_image("ghcr.io", "acme/tools", "1.0.0")
    put_bundle(
        FakeClient(regs, "ghcr.io"), "acme/tools", digest, upstream_bundle(digest, san=False)
    )
    with pytest.raises(hub_mirror.MirrorStop, match="final"):
        mirror(regs, FakeCosign(regs, permissive=True)).mirror(
            f"{UPSTREAM}:1.0.0", "tools", approve=False
        )
    empty = dict.fromkeys(("issuer", "runner_environment", "repository_uri", "ref", "san"), "")
    assert hub_mirror.cert_fields({}) == empty
    garbage = {"verificationMaterial": {"certificate": {"rawBytes": "AAAA"}}}
    assert hub_mirror.cert_fields(garbage) == empty
    assert hub_mirror.cert_fields({"verificationMaterial": "x"}) == empty


def test_provenance_refuses_the_approval(regs) -> None:
    seeded(regs, repo="acme/other")
    with pytest.raises(hub_mirror.MirrorStop, match="HUB_APPROVE is refused"):
        mirror(regs).mirror(f"{UPSTREAM}:1.0.0", "tools", approve=True)


def test_a_provenance_without_repository_or_workflow_stops(regs) -> None:
    seeded(regs)
    m = mirror(regs)
    server = m.catalog.server("tools")
    assert server is not None and server.provenance is not None
    server.provenance.pop("signer_identity")
    with pytest.raises(hub_mirror.MirrorStop, match="signer_identity"):
        m.mirror(f"{UPSTREAM}:1.0.0", "tools", approve=False)


def test_a_catalogued_repository_needs_its_server_named(regs) -> None:
    seeded(regs)
    with pytest.raises(hub_mirror.MirrorStop, match="catalog server tools"):
        mirror(regs).mirror(f"{UPSTREAM}:1.0.0", None, approve=True)
    # 기본 포트를 적은 철자도 같은 저장소다
    with pytest.raises(hub_mirror.MirrorStop, match="catalog server tools"):
        mirror(regs).mirror("ghcr.io:443/acme/tools:1.0.0", None, approve=True)
    # 대문자 저장소는 참조의 문법 밖이다
    with pytest.raises(hub_mirror.MirrorStop, match="HUB_UPSTREAM"):
        mirror(regs).mirror("ghcr.io/ACME/tools:1.0.0", None, approve=True)
    with pytest.raises(hub_mirror.MirrorStop, match="not ghcr.io/acme/elsewhere"):
        mirror(regs).mirror("ghcr.io/acme/elsewhere:1", "tools", approve=False)


def test_a_given_digest_must_be_the_tag_s_digest(regs) -> None:
    seeded(regs)
    stale = "sha256:" + "0" * 64
    with pytest.raises(hub_mirror.MirrorStop, match="resolves to"):
        mirror(regs).mirror(f"{UPSTREAM}:1.0.0@{stale}", "tools", approve=False)


def test_an_image_without_provenance_needs_the_approval(regs) -> None:
    digest = regs.seed_image("ghcr.io", "acme/plain", "2")
    with pytest.raises(hub_mirror.MirrorStop, match="HUB_APPROVE=yes"):
        mirror(regs).mirror("ghcr.io/acme/plain:2", "plain", approve=False)
    mirror(regs).mirror("ghcr.io/acme/plain:2", "plain", approve=True)
    (ann,) = hub_annotations(regs, "hub/ghcr.io/acme/plain", digest)
    assert ann[hub_verifier.PROVENANCE] == "approved" and ann[hub_verifier.CATALOG] == "plain@v1"
    assert ann[hub_verifier.RUN] == f"{RUN} catalog=v1"


def test_an_image_outside_the_catalog_is_mirrored_without_a_catalog_annotation(regs) -> None:
    digest = regs.seed_image("ghcr.io", "acme/own", "3")
    mirror(regs).mirror("ghcr.io/acme/own:3", None, approve=True)
    (ann,) = hub_annotations(regs, "hub/ghcr.io/acme/own", digest)
    assert hub_verifier.CATALOG not in ann and ann[hub_verifier.PROVENANCE] == "approved"
    # 0단계가 쓴 스냅숏 릴리스는 실행 기록에 남는다
    assert hub_verifier.parse_run(ann[hub_verifier.RUN])["catalog"] == "v1"


def test_copy_only_copies_without_a_signature(regs) -> None:
    digest = regs.seed_image("ghcr.io", "acme/plain", "2")
    cosign = FakeCosign(regs)
    ref = mirror(regs, cosign).copy_only("ghcr.io/acme/plain:2")
    assert ref == f"hub.test/hub/ghcr.io/acme/plain@{digest}"
    assert hub_annotations(regs, "hub/ghcr.io/acme/plain", digest) == []
    with pytest.raises(hub_mirror.MirrorStop, match="needs a tag"):
        mirror(regs).copy_only("ghcr.io/acme/plain")


def test_a_copy_whose_digest_differs_is_refused(regs) -> None:
    seeded(regs)
    regs.retag = b'{"schemaVersion": 2, "mediaType": "application/vnd.oci.image.index.v1+json"}'
    with pytest.raises(hub_mirror.MirrorStop, match="digest differs"):
        mirror(regs).mirror(f"{UPSTREAM}:1.0.0", "tools", approve=False)


def test_upstream_descriptors_need_digests_and_one_level_of_index(regs) -> None:
    inner = regs.seed_image("ghcr.io", "acme/nested", "0")
    raw = regs.repo("ghcr.io", "acme/nested")["manifests"][inner][0]
    outer = json.dumps(
        {
            "schemaVersion": 2,
            "mediaType": INDEX_TYPE,
            "manifests": [{"mediaType": INDEX_TYPE, "digest": inner, "size": len(raw)}],
        }
    ).encode()
    regs.repo("ghcr.io", "acme/nested")["manifests"][digest_of(outer)] = (outer, INDEX_TYPE)
    regs.repo("ghcr.io", "acme/nested")["tags"]["1"] = digest_of(outer)
    with pytest.raises(hub_mirror.MirrorStop, match="nests an index"):
        mirror(regs).mirror("ghcr.io/acme/nested:1", None, approve=True)
    bad = json.dumps(
        {
            "schemaVersion": 2,
            "mediaType": MANIFEST_TYPE,
            "config": {"mediaType": "x", "digest": "sha256:../../x", "size": 1},
            "layers": [],
        }
    ).encode()
    regs.repo("ghcr.io", "acme/bad")["manifests"][digest_of(bad)] = (bad, MANIFEST_TYPE)
    regs.repo("ghcr.io", "acme/bad")["tags"]["1"] = digest_of(bad)
    with pytest.raises(hub_mirror.MirrorStop, match="sha256 digest"):
        mirror(regs).mirror("ghcr.io/acme/bad:1", None, approve=True)


def test_docker_schema_2_is_never_converted(regs) -> None:
    regs.seed_image("ghcr.io", "acme/own", "3", media=DOCKER_MANIFEST_TYPE)
    regs.docker = False
    with pytest.raises(hub_mirror.MirrorStop, match="docker2s2"):
        mirror(regs).mirror("ghcr.io/acme/own:3", None, approve=True)


def test_annotation_values_refuse_quotes_commas_and_control_characters(regs) -> None:
    m = mirror(regs)
    for bad in ('a"b', "a,b", "a\nb"):
        with pytest.raises(hub_mirror.MirrorStop):
            m.sign("hub/x", "sha256:" + "0" * 64, {hub_verifier.UPSTREAM: bad})
    with pytest.raises(hub_mirror.MirrorStop):
        hub_mirror.plain("run by", "a\n")


def test_another_tag_takes_a_tag_ref_and_the_catalogued_tag_an_exact_one() -> None:
    assert hub_mirror.ref_allowed("refs/tags/v1", "refs/tags/v1", same_tag=True)
    assert not hub_mirror.ref_allowed("refs/tags/v1", "refs/tags/v2", same_tag=True)
    assert hub_mirror.ref_allowed("refs/tags/v1", "refs/tags/v2", same_tag=False)
    assert not hub_mirror.ref_allowed("refs/tags/v1", "refs/heads/main", same_tag=False)
    assert hub_mirror.ref_allowed("refs/heads/main", "refs/heads/main", same_tag=False)
    assert not hub_mirror.ref_allowed("refs/heads/main", "refs/heads/dev", same_tag=False)
    assert hub_mirror.ref_allowed("", "anything", same_tag=True)


def test_resign_signs_the_kept_content_with_the_new_key(regs) -> None:
    digest = seeded(regs)
    cosign = FakeCosign(regs)
    mirror(regs, cosign, key="old.key").mirror(f"{UPSTREAM}:1.0.0", "tools", approve=False)
    image = f"hub.test/hub/ghcr.io/acme/tools@{digest}"
    new = mirror(regs, cosign, key="new.key")
    assert new.resign(image, [cosign.pem("old.key")], refused=set()) == 1
    anns = hub_annotations(regs, "hub/ghcr.io/acme/tools", digest)
    assert len(anns) == 2
    assert {a[hub_verifier.UPSTREAM] for a in anns} == {f"ghcr.io/acme/tools:1.0.0@{digest}"}
    assert {a[hub_verifier.CATALOG] for a in anns} == {"tools@v1"}
    sigs = new.hub_signatures(
        "hub/ghcr.io/acme/tools", digest, [hub_verifier.load_key(cosign.pem("new.key"))]
    )
    assert len(sigs) == 1
    # 거부한 digest 는 건너뛴다
    assert new.resign(image, [cosign.pem("old.key")], refused={digest}) == 0
    with pytest.raises(hub_mirror.MirrorStop, match="HUB_KEEP_KEYS"):
        new.resign(image, [cosign.pem("other.key")], refused=set())


def test_resign_works_with_a_host_only_prefix(regs) -> None:
    digest = seeded(regs)
    cosign = FakeCosign(regs)
    old = mirror(regs, cosign, key="old.key")
    old.prefix = "hub.test"
    old.mirror(f"{UPSTREAM}:1.0.0", "tools", approve=False)
    new = mirror(regs, cosign, key="new.key")
    new.prefix = "hub.test"
    image = f"hub.test/ghcr.io/acme/tools@{digest}"
    assert new.resign(image, [cosign.pem("old.key")], refused=set()) == 1


WORKFLOW_REF = "example/hub-mirror/.github/workflows/mirror.yml@refs/heads/main"


def test_the_run_record_names_the_job_or_the_local_user() -> None:
    job = hub_mirror.run_record(
        {
            "GITHUB_ACTIONS": "true",
            "GITHUB_ACTOR": "octocat",
            "GITHUB_SERVER_URL": "https://github.com",
            "GITHUB_REPOSITORY": "example/hub-mirror",
            "GITHUB_RUN_ID": "7",
            "GITHUB_WORKFLOW_SHA": "abc",
            "GITHUB_WORKFLOW_REF": WORKFLOW_REF,
        },
        "f" * 40,
    )
    assert hub_verifier.parse_run(job) == {
        "by": "octocat",
        "via": "job",
        "url": "https://github.com/example/hub-mirror/actions/runs/7",
        "commit": "abc",
        "ref": WORKFLOW_REF,
        "platform": "f" * 40,
    }
    assert hub_verifier.parse_run(hub_mirror.run_record({}))["via"] == "make"
    assert hub_mirror.platform_commit() is None or len(hub_mirror.platform_commit() or "") == 40


def test_the_entry_point_stops_with_a_line_and_needs_its_inputs(tmp_path, capsys) -> None:
    assert hub_mirror.main(["hub_mirror.py", "mirror"], {}) == 1
    assert "HUB_KEY is empty" in capsys.readouterr().err
    key = tmp_path / "hub.key"
    key.write_text("x")
    env = {"HUB_KEY": str(key), "HUB_PREFIX": "hub", "COSIGN_PASSWORD": "pw"}
    assert hub_mirror.main(["hub_mirror.py", "mirror"], env) == 1
    assert "registry host" in capsys.readouterr().err
    assert hub_mirror.main(["hub_mirror.py", "other"], env) == 2
    # copy 는 키를 받지 않는다
    assert hub_mirror.main(["hub_mirror.py", "copy"], {"HUB_PREFIX": "hub"}) == 1
    assert "registry host" in capsys.readouterr().err


def test_make_passes_inputs_through_the_environment_only() -> None:
    """입력 값은 레시피 글에 들어가지 않는다. 그래서 값의 따옴표나 make 함수가 글로 읽히지
    않는다."""
    value = 'a"b$(shell echo injected)'
    r = subprocess.run(
        ["make", "-n", "hub-mirror", f"HUB_UPSTREAM={value}"],
        cwd=REPO,
        capture_output=True,
        text=True,
        check=False,
    )
    assert r.returncode == 0, r.stderr
    assert value not in r.stdout and "injected" not in r.stdout
    assert "scripts/hub_mirror.py mirror" in r.stdout


def test_the_mirror_job_pins_its_actions_and_its_platform_commit() -> None:
    job = yaml.safe_load((REPO / "deploy" / "hub-mirror" / "mirror.yml").read_text())
    steps = job["jobs"]["mirror"]["steps"]
    uses = [s["uses"] for s in steps if "uses" in s]
    assert uses and all(re.fullmatch(r"[\w.-]+/[\w.-]+@[0-9a-f]{40}", u) for u in uses), uses
    checks = next(s["run"] for s in steps if s.get("id") == "registry")
    assert "^[0-9a-f]{40}$" in checks
    make = next(s for s in steps if s.get("name") == "make hub-mirror")
    assert make["env"]["UV_NO_SYNC"] == "1" and "${{" not in make["run"]
