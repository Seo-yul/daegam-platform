"""P55 6절: 허브 image 항목의 허용(서명 모으기와 정하기), 설정과 그 검사, 거부한 digest 와
회수한 키.

서명은 F0 4번이 실제 cosign 3 으로 만든 번들이다(`fixtures/hub_signatures/README.md`). 레지스트리는
`tests/helpers/hub_registry.py` 의 대역이 OCI distribution API 의 모양으로 낸다.
"""

from __future__ import annotations

import asyncio
import json

import anyio
import httpx2
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec, rsa

from daegam.errors import ServiceError
from daegam.services import hub_catalog, hub_images, hub_verifier
from daegam.services.hub_catalog import Catalog, normalize_image, parse_image
from daegam.services.hub_images import (
    CREDENTIAL_SECRET,
    check_hub_images,
    decide,
    normalize_prefix,
    prefixes_overlap,
    under_prefix,
    usable_keys,
)
from daegam.services.hub_verifier import (
    HubRegistry,
    HubSignature,
    collect,
    load_key,
    read_bundle,
    referrer_layer,
)
from daegam.store.settings import HubImagesSettings
from tests.helpers.auth import as_principal
from tests.helpers.clusters import default_cluster, post_cluster
from tests.helpers.consts import DEFAULT_NS, GV, GW, HUB, PLATFORM_NS
from tests.helpers.gateways import create_gateway, deploy_gateway
from tests.helpers.http import detail_code
from tests.helpers.hub_registry import (
    HUB_PUB,
    OTHER_DIGEST,
    OTHER_PUB,
    PREFIX,
    SERVER_DIGEST,
    SERVER_IMAGE,
    SERVER_REPO,
    FakeHubRegistry,
    fixture,
    public_pem,
    referrer,
    referrer_manifest,
    signed_bundle,
    statement,
    tampered_bundle,
)
from tests.helpers.registry import digest_of

HUB_KEY = load_key(HUB_PUB)
OTHER_KEY = load_key(OTHER_PUB)
RELEASE = "v0.20261026.0"


def read(name: str, keys=(HUB_KEY,), digest: str = SERVER_DIGEST):
    raw = fixture(f"{name}.bundle.json")
    return read_bundle(raw, list(keys), digest, bundle=digest_of(raw))


def run_collect(reg: FakeHubRegistry, keys=(HUB_KEY,), repo: str = SERVER_REPO, **kw):
    async def go():
        async with HubRegistry(
            host="hub.test",
            credential=kw.pop("credential", None),
            verify=True,
            error_cls=ServiceError,
            transport=kw.pop("transport", None) or reg.transport(),
        ) as registry:
            return await collect(
                registry, repo, kw.pop("digest", SERVER_DIGEST), list(keys), prefix_path="hub"
            )

    return asyncio.run(go())


# -- 번들 하나 -----------------------------------------------------------------------------

# 이 판의 cryptography 가 읽지 못하는 곡선(secp112r1)의 공개 키
UNSUPPORTED_CURVE_PEM = (
    "-----BEGIN PUBLIC KEY-----\n"
    "MDIwEAYHKoZIzj0CAQYFK4EEAAYDHgAEAQEBAQEBAQEBAQEBAQECAgICAgICAgICAgICAg==\n"
    "-----END PUBLIC KEY-----\n"
)


def test_the_measured_bundles_are_read_from_their_signed_content_only() -> None:
    """hub.pub 로 서명하고 허브 주석을 단 번들만 모은다. 주석 없는 제품 서명, 다른 키, 다른 digest
    를 서명한 번들은 모으지 않는다."""
    sig, why = read("verified")
    assert why == "" and sig is not None
    assert sig.provenance == "verified" and sig.catalog == ("server", RELEASE)
    assert sig.upstream_ref.tag == "1.0.0" and sig.upstream_ref.digest == SERVER_DIGEST
    assert sig.run["by"] == "octocat" and sig.run["via"] == "job"
    assert sig.key == HUB_KEY.fingerprint
    approved, why = read("approved")
    assert approved is not None and approved.catalog is None and approved.provenance == "approved"
    assert approved.run == {"by": "operator", "via": "make"}
    assert read("no-annotations")[1] == "hub_annotations_missing"
    assert read("other-key")[1] == "no_key_verifies"
    assert read("other-digest")[1] == "subject_digest"
    # 회전 쌍이면 다른 키의 번들도 모은다
    assert read("other-key", keys=(HUB_KEY, OTHER_KEY))[0].key == OTHER_KEY.fingerprint


def test_changing_the_signed_annotations_breaks_the_signature() -> None:
    def to_other_server(st):
        st["subject"][0]["annotations"]["dev.daegam.hub.catalog"] = "elsewhere@" + RELEASE

    raw = tampered_bundle("verified", to_other_server)
    assert read_bundle(raw, [HUB_KEY], SERVER_DIGEST, bundle=digest_of(raw))[1] == (
        "no_key_verifies"
    )


def test_the_key_hint_orders_tries_and_decides_nothing() -> None:
    doc = json.loads(fixture("verified.bundle.json"))
    doc["verificationMaterial"]["publicKey"]["hint"] = OTHER_KEY.hint
    raw = json.dumps(doc).encode()
    sig, _ = read_bundle(raw, [OTHER_KEY, HUB_KEY], SERVER_DIGEST, bundle=digest_of(raw))
    assert sig is not None and sig.key == HUB_KEY.fingerprint
    assert read_bundle(raw, [OTHER_KEY], SERVER_DIGEST, bundle=digest_of(raw))[0] is None


def test_only_p256_keys_are_hub_keys() -> None:
    rsa_pem = (
        rsa.generate_private_key(public_exponent=65537, key_size=2048)
        .public_key()
        .public_bytes(serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo)
        .decode()
    )
    for pem in (rsa_pem, UNSUPPORTED_CURVE_PEM, "garbage"):
        with pytest.raises(ValueError):
            load_key(pem)
    assert HUB_KEY.fingerprint.startswith("sha256:") and len(HUB_KEY.fingerprint) == 71


def test_an_unreadable_stored_key_is_skipped_and_a_revoked_one_left_out() -> None:
    assert [k.fingerprint for k in usable_keys([UNSUPPORTED_CURVE_PEM, HUB_PUB], [])] == [
        HUB_KEY.fingerprint
    ]
    assert usable_keys([HUB_PUB], [HUB_KEY.fingerprint]) == []


def own(key: ec.EllipticCurvePrivateKey, annotations=None, *, digest=SERVER_DIGEST, **kw) -> bytes:
    """테스트 키로 서명한 번들의 바이트."""
    body = statement(digest, annotations, kw.pop("predicate", None))
    return json.dumps(signed_bundle(key, body, **kw)).encode()


def hub_annotations(tag: str = "1.0.0", **extra: str) -> dict[str, str]:
    return {
        "dev.daegam.hub.upstream": f"registry.example.com/team/server:{tag}@{SERVER_DIGEST}",
        "dev.daegam.hub.provenance": "approved",
        "dev.daegam.hub.run": "by=tester via=make",
        **extra,
    }


def test_bundles_outside_the_measured_shape_are_not_collected_and_never_raise() -> None:
    """모양 밖의 번들은 까닭과 함께 모으지 않고 예외를 내지 않는다. 서명은 하나여야 하고, 빈
    predicate 여야 하며, 서명한 subject 의 digest 는 객체여야 한다."""
    key = ec.generate_private_key(ec.SECP256R1())
    mine = load_key(public_pem(key))

    def why(raw: bytes) -> str:
        return read_bundle(raw, [mine], SERVER_DIGEST, bundle=digest_of(raw))[1]

    assert why(own(key, hub_annotations())) == ""
    assert why(own(key, hub_annotations(), signatures=2)) == "signature_count"
    assert why(own(key, hub_annotations(), predicate={"x": 1})) == "predicate_not_empty"
    bad_digest = json.loads(statement(SERVER_DIGEST, hub_annotations()))
    bad_digest["subject"][0]["digest"] = "not-an-object"
    raw = json.dumps(signed_bundle(key, json.dumps(bad_digest).encode())).encode()
    assert why(raw) == "subject_digest"
    unsigned = json.loads(own(key, hub_annotations()))
    unsigned["dsseEnvelope"]["signatures"] = 5
    assert why(json.dumps(unsigned).encode()) == "signature_count"
    odd = json.loads(own(key, hub_annotations()))
    odd["verificationMaterial"] = "x"
    assert why(json.dumps(odd).encode()) == ""
    bad_ref = hub_annotations()
    bad_ref["dev.daegam.hub.upstream"] = f"registry.example.com/team/../x:1@{SERVER_DIGEST}"
    assert why(own(key, bad_ref)) == "upstream_reference"


def test_the_referrer_manifest_must_carry_its_own_type_and_empty_config() -> None:
    bundle = fixture("verified.bundle.json")
    good = json.loads(referrer_manifest(bundle, SERVER_DIGEST))
    assert referrer_layer(json.dumps(good).encode())[1] == ""
    no_type = {k: v for k, v in good.items() if k != "artifactType"}
    assert referrer_layer(json.dumps(no_type).encode())[1] == "referrer_type"
    other_config = {**good, "config": {**good["config"], "mediaType": "application/json"}}
    assert referrer_layer(json.dumps(other_config).encode())[1] == "referrer_config"


# -- 정하기 --------------------------------------------------------------------------------


def sig(bundle: str, provenance: str, tag: str, catalog=None) -> HubSignature:
    upstream = f"registry.example.com/team/server:{tag}@{SERVER_DIGEST}"
    return HubSignature(
        bundle=bundle,
        upstream=upstream,
        upstream_ref=normalize_image(upstream),
        provenance=provenance,
        catalog=catalog,
        run={"by": "x", "via": "make"},
        key=HUB_KEY.fingerprint,
    )


def test_the_catalog_rules_and_provenance_come_from_every_signature() -> None:
    """카탈로그 서명과 카탈로그 없는 서명이 한 digest 에 있으면, 카탈로그 없는 서명의 태그로
    등록해도 카탈로그 규칙이 선다. verified 와 approved 가 함께 있으면 verified 다."""
    with_catalog = sig("sha256:b", "approved", "1.0.0", ("server", RELEASE))
    without = sig("sha256:a", "verified", "1.0.1")
    d = decide([with_catalog, without], "1.0.1", error_cls=ServiceError)
    assert d.catalog == ("server", RELEASE) and d.provenance == "verified" and d.count == 2
    # 쓴 서명은 verified 가 먼저다
    assert d.used is without


def test_two_catalog_servers_conflict() -> None:
    a = sig("sha256:a", "verified", "1", ("server", RELEASE))
    b = sig("sha256:b", "verified", "1", ("other", RELEASE))
    with pytest.raises(ServiceError) as e:
        decide([a, b], None, error_cls=ServiceError)
    assert e.value.code == "image_catalog_conflict"


def test_a_tag_needs_a_signature_that_carries_it() -> None:
    with pytest.raises(ServiceError) as e:
        decide([sig("sha256:a", "verified", "1.0.0")], "2.0.0", error_cls=ServiceError)
    assert e.value.code == "image_not_admitted"
    with pytest.raises(ServiceError) as e:
        decide([], None, error_cls=ServiceError)
    assert e.value.code == "image_not_admitted"


# -- 참조와 접두사 -------------------------------------------------------------------------


def test_prefixes_match_by_path_segment() -> None:
    assert normalize_prefix(" Registry.Example.com:443/hub/ ") == "registry.example.com/hub"
    bad = (
        "hub",
        "https://r.example.com/hub",
        "r.example.com/hub@sha256:00",
        "r.example.com/A",
        "r.example.com/hub/../x",
        "r.example.com//hub",
    )
    for value in bad:
        with pytest.raises(ValueError):
            normalize_prefix(value)
    assert prefixes_overlap("r.example.com/hub", "r.example.com/hub/x")
    assert prefixes_overlap("r.example.com/hub/x", "r.example.com/hub")
    assert prefixes_overlap("r.example.com:443/hub", "r.example.com/hub")
    assert not prefixes_overlap("r.example.com/hub", "r.example.com/hubx")
    assert not prefixes_overlap("r.example.com/daegam-hub", "r.example.com/daegam")
    image = normalize_image(f"{SERVER_IMAGE}:1.0.0")
    assert under_prefix(PREFIX, image)
    assert not under_prefix("hub.test/hu", image)
    assert not under_prefix(f"{PREFIX}/registry.example.com/team/server", image)


@pytest.mark.parametrize(
    "ref",
    [
        "hub.test/hub/../daegam/platform:1",
        "hub.test/hub/Registry.example.com/x:1",
        "hub.test/hub/a/x:-bad",
        "hub.test/hub/a/x@sha256:zz",
        "hub.test/hub/a?b/x:1",
        "hub.test/hub//x:1",
    ],
)
def test_a_reference_outside_the_grammar_is_refused(ref) -> None:
    with pytest.raises(ValueError):
        parse_image(ref)


def test_saving_drops_duplicate_keys_and_refuses_bad_keys_and_a_prefix_without_one() -> None:
    before = HubImagesSettings()
    value = HubImagesSettings(prefix="hub.test/hub/", public_keys=[HUB_PUB, HUB_PUB + "\n"])
    out = check_hub_images(value, [], before=before, error_cls=ServiceError)
    assert out.prefix == PREFIX and out.public_keys == [HUB_PUB]
    for keys in (["-----BEGIN PUBLIC KEY-----\nx\n"], [UNSUPPORTED_CURVE_PEM], []):
        with pytest.raises(ServiceError) as e:
            check_hub_images(
                HubImagesSettings(prefix=PREFIX, public_keys=keys),
                [],
                before=before,
                error_cls=ServiceError,
            )
        assert e.value.code == "invalid_value"


# -- 레지스트리에서 모으기 -----------------------------------------------------------------


def test_collecting_on_the_registry_keeps_the_hub_signatures() -> None:
    reg = FakeHubRegistry.with_fixtures()
    got = run_collect(reg)
    assert sorted(s.provenance for s in got) == ["approved", "verified"]
    assert {s.key for s in run_collect(reg, keys=(OTHER_KEY,))} == {OTHER_KEY.fingerprint}
    assert len(run_collect(reg, keys=(HUB_KEY, OTHER_KEY))) == 3


def test_a_bundle_moved_to_another_digest_is_not_collected() -> None:
    reg = FakeHubRegistry.with_fixtures()
    listing = reg.referrers[(SERVER_REPO, SERVER_DIGEST)]
    listing.append(referrer("other-digest"))
    assert len(run_collect(reg)) == 2
    # 제 digest 에서는 모은다
    assert (
        len(run_collect(reg, repo="hub/registry.example.com/team/other", digest=OTHER_DIGEST)) == 1
    )


def test_malformed_referrers_do_not_stop_the_collection() -> None:
    """번들 하나가 모양 밖이어도 제대로 서명한 나머지를 모은다. 줄바꿈이 붙은 digest 는 읽지
    않는다."""
    reg = FakeHubRegistry.with_fixtures()
    key = ec.generate_private_key(ec.SECP256R1())
    bad = json.loads(own(key, hub_annotations()))
    bad["dsseEnvelope"]["signatures"] = 5
    reg.add_bundle(SERVER_REPO, SERVER_DIGEST, bad)
    reg.add_bundle(SERVER_REPO, SERVER_DIGEST, b"not json")
    newline = referrer("verified")
    newline["digest"] += "\n"
    reg.referrers[(SERVER_REPO, SERVER_DIGEST)].append(newline)
    assert sorted(s.provenance for s in run_collect(reg)) == ["approved", "verified"]


def test_annotations_outside_the_signed_content_change_nothing() -> None:
    """referrer manifest 의 주석과 referrers 목록의 주석은 서명 밖이다. 거기에 다른 카탈로그 서버를
    적어도 모은 서명은 서명한 내용 그대로다."""
    reg = FakeHubRegistry.with_fixtures()
    man = json.loads(fixture("approved.referrer.json"))
    man["annotations"]["dev.daegam.hub.catalog"] = "elsewhere@" + RELEASE
    man["annotations"]["dev.daegam.hub.provenance"] = "verified"
    raw = json.dumps(man).encode()
    reg.manifests[digest_of(raw)] = raw
    desc = referrer("approved", raw)
    desc["annotations"] = dict(man["annotations"])
    reg.referrers[(SERVER_REPO, SERVER_DIGEST)] = [desc]
    (got,) = run_collect(reg)
    assert got.catalog is None and got.provenance == "approved"


def test_a_signature_binds_the_repository_it_was_signed_for() -> None:
    reg = FakeHubRegistry.with_fixtures()
    reg.copy_repo(SERVER_REPO, "hub/registry.example.com/team/elsewhere")
    assert run_collect(reg, repo="hub/registry.example.com/team/elsewhere") == []


def test_the_referrer_bound_refuses_rather_than_decide_on_part() -> None:
    reg = FakeHubRegistry.with_fixtures(page=10)
    one = referrer("verified")
    reg.referrers[(SERVER_REPO, SERVER_DIGEST)] = [one] * (hub_verifier.MAX_REFERRERS + 1)
    with pytest.raises(ServiceError) as e:
        run_collect(reg)
    assert e.value.code == "image_signatures_too_many"


def test_referrer_pages_are_followed_up_to_the_page_bound() -> None:
    reg = FakeHubRegistry.with_fixtures(page=1)
    assert len(run_collect(reg)) == 2
    assert sum("/referrers/" in str(r.url) for r in reg.requests) == 4
    # 꼭 16쪽이면 읽고, 17쪽이면 일부로 정하지 않는다
    one = referrer("approved")
    reg.referrers[(SERVER_REPO, SERVER_DIGEST)] = [one] * hub_verifier.MAX_PAGES
    assert len(run_collect(reg)) == hub_verifier.MAX_PAGES
    reg.referrers[(SERVER_REPO, SERVER_DIGEST)] = [one] * (hub_verifier.MAX_PAGES + 1)
    with pytest.raises(ServiceError) as e:
        run_collect(reg)
    assert e.value.code == "image_signatures_too_many"


def test_a_page_lost_after_the_first_is_not_a_partial_decision() -> None:
    reg = FakeHubRegistry.with_fixtures(page=1)

    def handle(request: httpx2.Request) -> httpx2.Response:
        if "n_start=1" in str(request.url):
            return httpx2.Response(404)
        return reg.handle(request)

    with pytest.raises(ServiceError) as e:
        run_collect(reg, transport=httpx2.MockTransport(handle))
    assert (e.value.status, e.value.code) == (502, "registry_unreachable")


@pytest.mark.parametrize(
    ("registry", "credential", "code"),
    [
        (
            FakeHubRegistry.with_fixtures(credential=("reader", "pw")),
            None,
            "registry_credential_refused",
        ),
        (FakeHubRegistry.with_fixtures(fail=503), None, "registry_unreachable"),
        (FakeHubRegistry.with_fixtures(bearer=True, realm="http://hub.test/token"), None, None),
        (FakeHubRegistry.with_fixtures(page=1, link_base="http://hub.test"), None, None),
    ],
)
def test_registry_failures_are_not_refusals_of_the_image(registry, credential, code) -> None:
    """닿지 못함, 자격 증명 거절, https 가 아닌 토큰 realm 과 다음 쪽은 모두 502 다."""
    with pytest.raises(ServiceError) as e:
        run_collect(registry, credential=credential)
    assert (e.value.status, e.value.code) == (502, code or "registry_unreachable")


def test_the_read_credential_goes_through_basic_and_bearer_flows() -> None:
    basic = FakeHubRegistry.with_fixtures(credential=("reader", "pw"))
    assert len(run_collect(basic, credential=("reader", "pw"))) == 2
    bearer = FakeHubRegistry.with_fixtures(credential=("reader", "pw"), bearer=True)
    assert len(run_collect(bearer, credential=("reader", "pw"))) == 2
    assert any(r.url.path == "/token" for r in bearer.requests)


def test_the_read_credential_never_leaves_the_registry_host() -> None:
    """다른 호스트의 토큰 realm 에는 익명으로 묻고, 리다이렉트는 따라가지 않는다."""
    reg = FakeHubRegistry.with_fixtures(bearer=True, realm="https://auth.example/token")
    assert len(run_collect(reg, credential=("reader", "pw"))) == 2
    tokens = [r for r in reg.requests if r.url.path == "/token"]
    assert tokens and all(r.url.host == "auth.example" for r in tokens)
    assert all("Authorization" not in r.headers for r in tokens)

    def redirect(request: httpx2.Request) -> httpx2.Response:
        return httpx2.Response(307, headers={"Location": "https://elsewhere.example/v2/x"})

    with pytest.raises(ServiceError) as e:
        run_collect(reg, transport=httpx2.MockTransport(redirect))
    assert e.value.code == "registry_unreachable"


def test_an_unreachable_registry_is_reported_as_such() -> None:
    def down(_request):
        raise httpx2.ConnectError("refused")

    async def go(repo: str):
        async with HubRegistry(
            host="hub.test",
            credential=None,
            verify=True,
            error_cls=ServiceError,
            transport=httpx2.MockTransport(down),
        ) as registry:
            await registry.manifest(repo, "1.0.0")

    for repo in (SERVER_REPO, "bad\x00repo"):
        with pytest.raises(ServiceError) as e:
            asyncio.run(go(repo))
        assert (e.value.status, e.value.code) == (502, "registry_unreachable")


# -- API: 설정, 허용, 거부, 회수 ------------------------------------------------------------


@pytest.fixture
def cluster(client):
    return default_cluster(client)


@pytest.fixture
def registry(client) -> FakeHubRegistry:
    reg = FakeHubRegistry.with_fixtures()
    client.app.state.hub_service.image_admission.transport = reg.transport()
    return reg


def configure(client, **extra) -> dict:
    body = {"prefix": PREFIX, "public_keys": [HUB_PUB], **extra}
    r = client.patch("/settings", json={"hub_images": body})
    assert r.status_code == 200, r.text
    return r.json()["hub_images"]


def admitted(client, name: str = "srv", image: str = f"{SERVER_IMAGE}:1.0.0", **extra):
    return client.post(HUB, json={"name": name, "kind": "image", "image": image, **extra})


def warnings_of(client, item_id: str) -> list[str]:
    return [w.split(":", 1)[0] for w in client.get(f"{HUB}/{item_id}").json()["status"]["warnings"]]


def test_an_admitted_item_stores_the_digest_and_the_signed_record(client, cluster, registry):
    configure(client)
    r = admitted(client)
    assert r.status_code == 201, r.text
    source = r.json()["source"]
    assert source["image"] == f"{SERVER_IMAGE}@{SERVER_DIGEST}"
    adm = source["admission"]
    assert (adm["provenance"], adm["signatures"], adm["key"]) == (
        "verified",
        2,
        HUB_KEY.fingerprint,
    )
    assert adm["digest"] == SERVER_DIGEST and adm["run"]["via"] == "job"
    assert adm["upstream"] == f"registry.example.com/team/server:1.0.0@{SERVER_DIGEST}"
    # 카탈로그 서버를 이름 댄 서명이 있어 그 서버의 출처와 쓸 수 있는 /tmp 를 단다
    assert source["catalog"] == {"server": "server", "release": RELEASE}
    assert "hub_image_unverified" not in warnings_of(client, r.json()["id"])
    manifest = client.get(f"{HUB}/{r.json()['id']}/manifest").json()
    server = next(d for d in manifest["documents"] if d["kind"] == "MCPServer")
    assert server["rendered"]["spec"]["image"] == source["image"]
    pod = server["rendered"]["spec"]["podTemplateSpec"]["spec"]
    assert {"name": "tmp", "emptyDir": {"sizeLimit": "64Mi"}} in pod["volumes"]


def test_a_member_cannot_register_a_catalog_server_outside_the_current_catalog(
    client, cluster, registry
):
    """서명이 이름 댄 `server` 는 지금 스냅숏에 없으므로 platform-admin 만
    등록한다."""
    configure(client)
    with as_principal(client, "dev", "developer"):
        r = admitted(client)
    assert detail_code(r) == (403, "catalog_server_admin_only")


def catalog_with_server() -> Catalog:
    return Catalog.model_validate(
        {
            "catalog": "test",
            "release": RELEASE,
            "license": "Apache-2.0",
            "servers": [
                {
                    "name": "server",
                    "title": "Server",
                    "kind": "image",
                    "transport": "streamable-http",
                    "stdio": True,
                    "image": {"repository": "registry.example.com/team/server", "tag": "1.0.0"},
                    "args": ["--fixed"],
                    "env": [{"name": "LOG_LEVEL"}],
                }
            ],
        }
    )


def test_a_signed_catalog_server_brings_its_rules_for_members(
    client, cluster, registry, monkeypatch
):
    """서명이 이름 댄 서버가 지금 스냅숏에 있으면 그 서버의 인자와 env 규칙이 선다. 카탈로그 없는
    서명의 태그로 등록해도 같다 (5절)."""
    monkeypatch.setattr(hub_catalog, "snapshot", catalog_with_server)
    configure(client)
    with as_principal(client, "dev", "developer"):
        assert admitted(client, args=["--fixed"]).status_code == 201
        assert detail_code(admitted(client, name="b", args=["--other"])) == (
            403,
            "catalog_settings_admin_only",
        )
        undeclared = [{"name": "UNDECLARED", "value": "x"}]
        assert detail_code(admitted(client, name="c", args=["--fixed"], env=undeclared)) == (
            403,
            "catalog_settings_admin_only",
        )
    # 카탈로그 서명은 1.0.0, 카탈로그 없는 서명은 3.0.0 을 든다
    key = ec.generate_private_key(ec.SECP256R1())
    registry.referrers[(SERVER_REPO, SERVER_DIGEST)] = []
    registry.tags[(SERVER_REPO, "3.0.0")] = SERVER_DIGEST
    catalog = {"dev.daegam.hub.catalog": f"server@{RELEASE}"}
    registry.add_bundle(
        SERVER_REPO, SERVER_DIGEST, json.loads(own(key, hub_annotations(**catalog)))
    )
    registry.add_bundle(SERVER_REPO, SERVER_DIGEST, json.loads(own(key, hub_annotations("3.0.0"))))
    configure(client, public_keys=[public_pem(key)])
    with as_principal(client, "dev", "developer"):
        r = admitted(client, name="d", image=f"{SERVER_IMAGE}:3.0.0", args=["--other"])
    assert detail_code(r) == (403, "catalog_settings_admin_only")


@pytest.mark.parametrize(
    ("image", "code"),
    [
        (f"{SERVER_IMAGE}:2.0.0", "image_not_admitted"),
        (f"{SERVER_IMAGE}:9.9.9", "image_not_admitted"),
        ("hub.test/hu/registry.example.com/team/server:1.0.0", "image_outside_registry"),
        ("ghcr.io/acme/tools:1", "image_outside_registry"),
        ("hub.test/hub/../daegam/platform:1", "invalid_value"),
        ("hub.test/hub/registry.example.com/Team/server:1.0.0", "invalid_value"),
    ],
)
def test_images_without_a_vouching_signature_are_refused(client, cluster, registry, image, code):
    configure(client)
    assert detail_code(admitted(client, image=image)) == (422, code)


def test_a_refused_digest_is_refused_with_no_registry_call_when_given(client, cluster, registry):
    """digest 를 준 참조는 레지스트리에 묻기 전에, 태그는 푼 digest 로 거절한다."""
    configure(client, refused_digests=[SERVER_DIGEST])
    given = admitted(client, image=f"{SERVER_IMAGE}@{SERVER_DIGEST}")
    assert detail_code(given) == (422, "image_refused")
    assert registry.requests == []
    assert detail_code(admitted(client)) == (422, "image_refused")


def test_a_refused_digest_is_refused_with_admission_off(client, cluster):
    """허용이 꺼져 있어도 거부한 digest 로는 새 항목을 만들지 않는다 (P55 6절)."""
    r = client.patch("/settings", json={"hub_images": {"refused_digests": [SERVER_DIGEST]}})
    assert r.status_code == 200, r.text
    r = client.post(
        HUB, json={"name": "x", "kind": "image", "image": f"ghcr.io/evil/x@{SERVER_DIGEST}"}
    )
    assert detail_code(r) == (422, "image_refused")


def test_a_prefix_is_never_saved_without_a_key(client, cluster):
    """접두사가 있으면 허용이 켜지므로 키 없이 저장하지 않는다. 키를 모두 회수하면 아무것도
    허용하지 않는다."""
    r = client.patch("/settings", json={"hub_images": {"prefix": PREFIX}})
    assert detail_code(r) == (422, "invalid_value")
    configure(client, revoked_keys=[HUB_KEY.fingerprint])
    assert detail_code(admitted(client, image="ghcr.io/evil/x:1")) == (
        422,
        "image_outside_registry",
    )


def test_a_catalog_name_must_be_the_one_the_signatures_name(client, cluster, registry):
    configure(client)
    assert detail_code(admitted(client, catalog="osv")) == (422, "invalid_value")
    assert admitted(client, catalog="server").status_code == 201


def test_without_settings_items_are_created_as_before_and_warn(client, cluster):
    r = client.post(HUB, json={"name": "plain", "kind": "image", "image": "ghcr.io/a/b:1"})
    assert r.status_code == 201, r.text
    assert "admission" not in r.json()["source"]
    assert "hub_image_unverified" in warnings_of(client, r.json()["id"])


def test_the_catalog_answer_follows_the_settings(client, cluster):
    assert client.get(f"{HUB}/catalog").json()["admission"] is False
    configure(client)
    out = client.get(f"{HUB}/catalog").json()
    assert out["admission"] is True
    image = next(s for s in out["servers"] if s["kind"] == "image")
    assert image["mirrored_image"].startswith(f"{PREFIX}/")


def test_admission_has_an_overall_deadline(client, cluster, monkeypatch):
    async def slow(_request):
        await anyio.sleep(2)
        return httpx2.Response(503)

    monkeypatch.setattr(hub_images, "ADMISSION_SECONDS", 0.05)
    client.app.state.hub_service.image_admission.transport = httpx2.MockTransport(slow)
    configure(client)
    assert detail_code(admitted(client)) == (502, "registry_unreachable")


def test_the_read_credential_is_kept_in_a_secret_and_used(client, cluster, fake_secrets):
    configure(client)
    reg = FakeHubRegistry.with_fixtures(credential=("reader", "s3cret-pw"))
    client.app.state.hub_service.image_admission.transport = reg.transport()
    assert detail_code(admitted(client)) == (502, "registry_credential_refused")
    r = client.put(
        "/settings/hub-images/credential", json={"username": "reader", "password": "s3cret-pw"}
    )
    assert r.status_code == 200 and r.json() == {"credential_set": True}
    assert "s3cret-pw" not in r.text
    stored = fake_secrets.secrets[(PLATFORM_NS, CREDENTIAL_SECRET)]
    assert stored == {"username": "reader", "password": "s3cret-pw"}
    settings = client.get("/settings")
    assert settings.json()["hub_images"]["credential_set"] is True
    assert "s3cret-pw" not in settings.text
    assert admitted(client).status_code == 201


def test_an_unreadable_stored_credential_answers_503(client, cluster, registry, fake_secrets):
    """넣은 읽기 자격 증명의 Secret 을 읽지 못하면 익명으로 물러나지 않고 503 이다."""
    configure(client)
    r = client.put("/settings/hub-images/credential", json={"username": "u", "password": "p"})
    assert r.status_code == 200
    del fake_secrets.secrets[(PLATFORM_NS, CREDENTIAL_SECRET)]
    assert detail_code(admitted(client)) == (503, "hub_images_secret_unavailable")

    class Broken:
        def get(self, *_a, **_k):
            raise RuntimeError("k8s api down")

    client.app.state.hub_service.image_admission.secrets = lambda: Broken()
    assert detail_code(admitted(client)) == (503, "hub_images_secret_unavailable")
    client.app.state.hub_service.image_admission.secrets = lambda: None
    assert detail_code(admitted(client)) == (503, "hub_images_secret_unavailable")
    assert registry.requests == []


def test_the_read_credential_is_dropped_when_the_prefix_host_changes(client, cluster, fake_secrets):
    configure(client)
    r = client.put("/settings/hub-images/credential", json={"username": "u", "password": "p"})
    assert r.status_code == 200
    configure(client, prefix="hub.test/hub/other")
    assert client.get("/settings").json()["hub_images"]["credential_set"] is True
    configure(client, prefix="elsewhere.test/hub")
    assert client.get("/settings").json()["hub_images"]["credential_set"] is False
    assert (PLATFORM_NS, CREDENTIAL_SECRET) not in fake_secrets.secrets


def test_a_prefix_overlapping_a_product_prefix_is_refused_on_either_side(client, cluster):
    r = client.patch(f"/clusters/{cluster}", json={"images": {"registry_prefix": "hub.test/hub"}})
    assert r.status_code == 200, r.text
    body = {"prefix": "hub.test/hub/mirror", "public_keys": [HUB_PUB]}
    r = client.patch("/settings", json={"hub_images": body})
    assert detail_code(r) == (422, "hub_prefix_overlap")
    r = client.patch(
        f"/clusters/{cluster}", json={"images": {"registry_prefix": "hub.test/daegam"}}
    )
    assert r.status_code == 200, r.text
    configure(client)
    r = client.patch(f"/clusters/{cluster}", json={"images": {"registry_prefix": "hub.test"}})
    assert detail_code(r) == (422, "hub_prefix_overlap")
    images = {"registry_prefix": "hub.test:443/hub"}
    assert detail_code(post_cluster(client, "edge2", images=images)) == (422, "hub_prefix_overlap")


def test_an_existing_overlap_blocks_no_other_save(client, cluster, mongo):
    """home 클러스터의 접두사처럼 저장 밖에서 생긴 겹침은 거부 목록이나 pull Secret 저장을 막지
    않고, 설정 카드의 겹침으로 보인다."""
    configure(client)
    mongo.clusters.update_one({"_id": cluster}, {"$set": {"images.registry_prefix": "hub.test"}})
    r = client.patch("/settings", json={"hub_images": {"refused_digests": [SERVER_DIGEST]}})
    assert r.status_code == 200, r.text
    images = {"registry_prefix": "hub.test", "pull_secret": "hub-pull"}
    assert client.patch(f"/clusters/{cluster}", json={"images": images}).status_code == 200
    overlaps = client.get("/settings/hub-images/status").json()["overlaps"]
    assert [o["registry_prefix"] for o in overlaps] == ["hub.test"]


def test_the_status_route_is_for_platform_admins_only(client, cluster):
    configure(client)
    for role in ("viewer", "tenant-admin"):
        with as_principal(client, "someone", role):
            r = client.get("/settings/hub-images/status")
        assert detail_code(r) == (403, "forbidden"), role


def test_a_refused_digest_stops_spreading_and_warns(client, cluster, registry, fake_clusters):
    """거부한 digest 는 Gateway 의 새 저장, 배포와 재배포, 접근 넓히기를 막고 항목과 배포한
    Gateway 에 경고를 단다. 도는 항목은 그대로다 (P55 6절)."""
    configure(client)
    item = admitted(client).json()
    fake = fake_clusters.by_name["edge"]
    fake.set_status(
        GV,
        "MCPServer",
        "hub-srv",
        DEFAULT_NS,
        {"phase": "Ready", "url": "http://mcp-hub-srv-proxy.daegam-default.svc:8080/mcp"},
    )
    backends = [{"hub_item_id": item["id"]}]
    gw = create_gateway(client, None, name="g1", backends=backends)
    assert gw.status_code == 201, gw.text
    assert deploy_gateway(client, gw.json()["id"]).status_code == 201
    draft = create_gateway(client, None, name="g3", backends=backends)
    assert draft.status_code == 201, draft.text

    configure(client, refused_digests=[SERVER_DIGEST])
    assert "hub_image_refused" in warnings_of(client, item["id"])
    status = client.get(f"{GW}/{gw.json()['id']}").json()["status"]
    assert "hub_backend_refused: srv" in status["warnings"]
    redeploy = client.post(f"{GW}/{gw.json()['id']}/redeploy", json={})
    assert detail_code(redeploy) == (422, "hub_backend_refused")
    save = client.put(f"{GW}/{gw.json()['id']}", json={"name": "g1", "backends": backends})
    assert detail_code(save) == (422, "hub_backend_refused")
    assert detail_code(deploy_gateway(client, draft.json()["id"])) == (422, "hub_backend_refused")
    r = create_gateway(client, None, name="g2", backends=backends)
    assert detail_code(r) == (422, "hub_backend_refused")
    wide = {"view": {"tenants": ["*"]}, "consume": {"tenants": ["*"]}}
    assert detail_code(client.put(f"{HUB}/{item['id']}/access", json=wide)) == (
        422,
        "refused_image_not_shareable",
    )
    # 좁히기는 된다
    assert client.put(f"{HUB}/{item['id']}/access", json={}).status_code == 200
    usage = client.get("/settings/hub-images/status").json()
    assert usage["refused"] == [
        {
            "digest": SERVER_DIGEST,
            "items": [{"id": item["id"], "tenant": "default", "name": "srv", "gateways": 2}],
        }
    ]
    assert usage["keys"] == [{"fingerprint": HUB_KEY.fingerprint, "revoked": False}]


def test_the_usage_listing_holds_a_full_refused_list(client, cluster, registry):
    configure(client)
    item = admitted(client).json()
    many = [f"sha256:{i:064x}" for i in range(499)] + [SERVER_DIGEST]
    configure(client, refused_digests=many)
    refused = client.get("/settings/hub-images/status").json()["refused"]
    assert len(refused) == 500
    assert refused[-1]["items"][0]["id"] == item["id"]


def test_a_revoked_key_warns_only_the_items_it_admitted(client, cluster, registry):
    """한 키로 허용한 항목과 다른 키로 허용한 항목 가운데 회수한 키의 것만 경고한다."""
    configure(client)
    by_hub = admitted(client, name="a").json()
    configure(client, public_keys=[OTHER_PUB])
    by_other = admitted(client, name="b").json()
    assert by_other["source"]["admission"]["key"] == OTHER_KEY.fingerprint
    configure(client, public_keys=[HUB_PUB, OTHER_PUB], revoked_keys=[OTHER_KEY.fingerprint])
    assert "hub_image_key_revoked" in warnings_of(client, by_other["id"])
    assert "hub_image_key_revoked" not in warnings_of(client, by_hub["id"])
    status = client.get("/settings/hub-images/status").json()
    assert {k["fingerprint"]: k["revoked"] for k in status["keys"]} == {
        HUB_KEY.fingerprint: False,
        OTHER_KEY.fingerprint: True,
    }
    # 회수한 키는 새 항목을 허용하지 않는다
    configure(client, public_keys=[OTHER_PUB], revoked_keys=[OTHER_KEY.fingerprint])
    assert detail_code(admitted(client, name="again")) == (422, "image_not_admitted")
