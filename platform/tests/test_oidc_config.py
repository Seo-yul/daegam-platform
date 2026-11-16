"""P6 F4a 1부: 테넌트 네임스페이스 MCPOIDCConfig(사람 토큰용은 발급자 있는 모드만, 서비스 토큰용은
모든 모드, D41), ToolHive 앞단의 사람 참조와 그 scope(P33 14절), /auth/config 의 mcp_audience."""

from __future__ import annotations

from dataclasses import replace

from daegam.config import Settings
from daegam.config.resolve import EffectiveNamespace
from daegam.k8s.labels import ANNOTATION_DESIRED_HASH, REV_LENGTH, stable_hash
from daegam.services.data_plane_auth import consumer_auth
from daegam.services.tenancy import (
    INTERNAL_OIDC_CONFIG_NAME,
    OIDC_CONFIG_NAME,
    TOOLHIVE_API,
    ensure_namespace_objects,
    ensure_oidc_configs,
    oidc_config_manifest,
    oidc_config_manifests,
    oidc_kwargs,
)
from daegam.store.tenants import TenantSettings
from tests.helpers.consts import ISSUER, OIDC_ISSUER, PLATFORM_JWKS, PUBLIC_URL
from tests.helpers.fake_cluster import FakeClusterClient


def _oidc(fake: FakeClusterClient, ns: str):
    return fake.get(TOOLHIVE_API, "MCPOIDCConfig", OIDC_CONFIG_NAME, ns)


def _internal(fake: FakeClusterClient, ns: str):
    return fake.get(TOOLHIVE_API, "MCPOIDCConfig", INTERNAL_OIDC_CONFIG_NAME, ns)


SERVICE_JWKS = f"{PLATFORM_JWKS}?use=service"
INTERNAL_KW = {"internal_issuer": ISSUER, "internal_insecure_http": True}
PLAIN = {"insecureAllowHTTP": True, "jwksAllowPrivateIP": True}


# -- 매니페스트 / ensure_namespace_objects ----------------------------------------------


def test_manifest_snapshot():
    m = oidc_config_manifest("acme", "daegam-acme", "https://okta.example/oauth2/default", "cid-1")
    annotations = m["metadata"].pop("annotations")
    assert m == {
        "apiVersion": "toolhive.stacklok.dev/v1beta1",
        "kind": "MCPOIDCConfig",
        "metadata": {
            "name": "daegam-oidc",
            "namespace": "daegam-acme",
            "labels": {
                "daegam.devops.ai.kr/tenant": "acme",
                "daegam.devops.ai.kr/resource": "tenant-oidc-config",
                "daegam.devops.ai.kr/id": "acme",
                "app.kubernetes.io/managed-by": "daegam",
            },
        },
        "spec": {
            "type": "inline",
            "inline": {"issuer": "https://okta.example/oauth2/default", "clientId": "cid-1"},
        },
    }
    # 객체 판은 이 어노테이션을 뺀 렌더 전체의 해시다 (P32 8절). install_id 가 없으면 설치 라벨도
    # 없다
    assert annotations == {ANNOTATION_DESIRED_HASH: stable_hash(m, REV_LENGTH)}
    # client_id 가 없으면 clientId 키를 넣지 않는다 (audience 는 참조 쪽에 있다)
    spec = oidc_config_manifest("acme", "ns", "https://okta.example", None)["spec"]
    assert spec == {"type": "inline", "inline": {"issuer": "https://okta.example"}}
    # 개발 전용 HTTP issuer 스위치 (스파이크 1번: 없으면 오퍼레이터가 Valid=False)
    spec = oidc_config_manifest("acme", "ns", "http://mock-oidc.svc", "c", insecure_http=True)[
        "spec"
    ]
    assert spec["inline"] == {
        "issuer": "http://mock-oidc.svc",
        "clientId": "c",
        "insecureAllowHTTP": True,
        "jwksAllowPrivateIP": True,
    }
    # 서비스 토큰용 (D41): 같은 꼴, 이름·라벨만 다르다
    m = oidc_config_manifest(
        "acme",
        "ns",
        ISSUER,
        None,
        insecure_http=True,
        name=INTERNAL_OIDC_CONFIG_NAME,
        resource="tenant-internal-oidc-config",
    )
    assert m["metadata"]["name"] == "daegam-internal-oidc"
    assert m["metadata"]["labels"]["daegam.devops.ai.kr/resource"] == "tenant-internal-oidc-config"
    assert m["spec"]["inline"] == {
        "issuer": ISSUER,
        "insecureAllowHTTP": True,
        "jwksAllowPrivateIP": True,
    }


def test_oidc_kwargs_follow_auth_mode():
    """사람 토큰의 발급자는 모드가 정하고, 서비스 토큰의 발급자(플랫폼)는 모든 모드에 있다 (D41)."""
    base = Settings(
        auth_mode="oidc",
        oidc_issuer="https://okta.example",
        oidc_client_id="c",
        platform_issuer="https://daegam.example",
        data_plane_issuer="idp",
    )
    internal = {
        "internal_issuer": "https://daegam.example",
        "internal_jwks_url": "https://daegam.example/.well-known/jwks.json?use=service",
        "internal_insecure_http": False,
    }
    # oidc 의 사람 토큰은 IdP 디스커버리로 키를 받으므로 JWKS 주소가 없다 (P32 1절)
    assert oidc_kwargs(base) == {
        "oidc_issuer": "https://okta.example",
        "oidc_jwks_url": None,
        "oidc_client_id": "c",
        "oidc_insecure_http": False,
        **internal,
    }
    insecure = replace(base, oidc_insecure_http=True)
    assert oidc_kwargs(insecure)["oidc_insecure_http"] is True
    assert oidc_kwargs(replace(base, auth_mode="header")) == internal
    assert oidc_kwargs(replace(base, oidc_issuer=None)) == internal
    # 플랫폼 발급자를 비우면 홈 클러스터의 내부 주소이고 평문이다
    plain = Settings(auth_mode="header", in_cluster=True)
    assert oidc_kwargs(plain) == {**INTERNAL_KW, "internal_jwks_url": SERVICE_JWKS}


def test_ensure_namespace_objects_creates_oidc_config_only_when_asked():
    ns = EffectiveNamespace(name="daegam-acme")
    fake = FakeClusterClient()
    ensure_namespace_objects(fake, "acme", ns)
    assert _oidc(fake, "daegam-acme") is None
    assert [o["kind"] for o in fake.applied] == [
        "Namespace",
        "NetworkPolicy",
        "NetworkPolicy",
        "NetworkPolicy",
        "MCPGroup",
    ]

    fake = FakeClusterClient()
    kw = {"oidc_issuer": "https://okta.example", "oidc_client_id": "c"}
    ensure_namespace_objects(fake, "acme", ns, **kw)
    assert [o["kind"] for o in fake.applied] == [
        "Namespace",
        "NetworkPolicy",
        "NetworkPolicy",
        "NetworkPolicy",
        "MCPOIDCConfig",
        "MCPGroup",
    ]
    got = _oidc(fake, "daegam-acme")
    assert got["spec"]["inline"] == {"issuer": "https://okta.example", "clientId": "c"}
    # 멱등
    ensure_namespace_objects(fake, "acme", ns, **kw)
    assert len(fake.find("MCPOIDCConfig")) == 1
    assert _oidc(fake, "daegam-acme")["metadata"]["generation"] == 2

    # 서비스 토큰용은 internal_issuer 가 있으면 사람용 뒤, Quota 앞에 온다 (D41)
    fake = FakeClusterClient()
    ensure_namespace_objects(fake, "acme", ns, **kw, **INTERNAL_KW)
    assert [(o["kind"], o["metadata"]["name"]) for o in fake.applied] == [
        ("Namespace", "daegam-acme"),
        ("NetworkPolicy", "daegam-sandbox-isolation"),
        ("NetworkPolicy", "daegam-mcp-backend"),
        ("NetworkPolicy", "daegam-tenant-ingress"),
        ("MCPOIDCConfig", "daegam-oidc"),
        ("MCPOIDCConfig", "daegam-internal-oidc"),
        ("MCPGroup", "acme"),
    ]
    assert _internal(fake, "daegam-acme")["spec"]["inline"] == {
        "issuer": ISSUER,
        "insecureAllowHTTP": True,
        "jwksAllowPrivateIP": True,
    }
    # header 모드 꼴: 사람용 없이 서비스 토큰용만
    fake = FakeClusterClient()
    ensure_namespace_objects(fake, "acme", ns, **INTERNAL_KW)
    assert [o["metadata"]["name"] for o in fake.find("MCPOIDCConfig")] == ["daegam-internal-oidc"]


def test_ensure_oidc_configs_is_the_namespace_step():
    """네임스페이스 보장과 인증 재적용은 같은 함수로 두 MCPOIDCConfig 를 적용한다. 같은 인자면 같은
    객체이고, 설치 라벨과 객체 판이 함께 실린다 (P32 8절)."""
    kw = {"oidc_issuer": "https://okta.example", "oidc_client_id": "c", **INTERNAL_KW}
    whole, alone = FakeClusterClient(), FakeClusterClient()
    ensure_namespace_objects(whole, "acme", EffectiveNamespace(name="ns"), install_id="i-1", **kw)
    ensure_oidc_configs(alone, "acme", "ns", "i-1", **kw)

    def rendered(fake: FakeClusterClient) -> list[tuple]:
        return [
            (
                o["metadata"]["name"],
                o["metadata"]["labels"],
                o["metadata"]["annotations"],
                o["spec"],
            )
            for o in fake.find("MCPOIDCConfig")
        ]

    assert rendered(whole) == rendered(alone) and len(rendered(alone)) == 2
    for _name, labels, stamped, _spec in rendered(alone):
        assert (
            labels["daegam.devops.ai.kr/platform-id"] == "i-1"
            and ANNOTATION_DESIRED_HASH in stamped
        )


# -- ToolHive 앞단의 사람 참조 (P33 14절) ------------------------------------------------


def test_the_human_ref_advertises_the_mcp_scope_only_when_oauth_is_on():
    """Gateway 의 vMCP 와 노출 앞단은 소비자 입력(`consumer_auth`)에서 참조를 받는다. OAuth 인가
    서버가 켜졌을 때만 `scopes: ["mcp"]` 가 든다. 비우면 ToolHive 가 PRM 에 openid 를 광고한다."""
    ts = TenantSettings(tenant="t")
    url = "https://gw.example/mcp"
    on = Settings(auth_mode="local", platform_issuer=ISSUER, public_url=PUBLIC_URL)
    assert consumer_auth(on, ts).oidc_ref(url) == {
        "name": OIDC_CONFIG_NAME,
        "audience": "daegam-mcp",
        "resourceUrl": url,
        "scopes": ["mcp"],
    }
    plain = replace(on, public_url="http://daegam.local", oauth_allow_http=True)
    assert consumer_auth(plain, ts).oidc_ref(None) == {
        "name": OIDC_CONFIG_NAME,
        "audience": "daegam-mcp",
        "scopes": ["mcp"],
    }
    off = {
        "no public url": replace(on, public_url=None),
        "plain http": replace(on, public_url="http://daegam.local"),
        "switched off": replace(on, oauth_switch=False),
        "oidc": replace(
            on, auth_mode="oidc", oidc_issuer="https://idp.example", data_plane_issuer="idp"
        ),
    }
    for name, settings in off.items():
        assert "scopes" not in consumer_auth(settings, ts).oidc_ref(url), name


# -- /auth/config ----------------------------------------------------------------------


def test_auth_config_has_mcp_audience(client, oidc_client):
    assert client.get("/auth/config").json() == {"auth_mode": "header"}
    cfg = oidc_client.get("/auth/config").json()
    assert cfg["mcp_audience"] == "daegam-mcp" and cfg["issuer"] == OIDC_ISSUER


def test_local_mode_makes_the_platform_the_issuer():
    """local 모드(D40)의 MCPOIDCConfig 는 플랫폼 자신을 발급자로 적고, 클라이언트 id 는 없다.
    발급자가 평문 http(클러스터 안 svc 주소)면 insecureAllowHTTP 가 붙고 https 면 붙지 않는다."""
    plain = Settings(
        auth_mode="local", platform_issuer="http://daegam-platform.daegam-system.svc:9000/"
    )
    assert oidc_kwargs(plain) == {
        "oidc_issuer": ISSUER,
        "oidc_jwks_url": PLATFORM_JWKS,
        "oidc_client_id": None,
        "oidc_insecure_http": True,
        # 서비스 토큰의 발급자도 같은 주소다 (D41). JWKS 는 같은 경로의 서비스용 모양이다
        "internal_issuer": ISSUER,
        "internal_jwks_url": SERVICE_JWKS,
        "internal_insecure_http": True,
    }
    public = Settings(auth_mode="local", platform_issuer="https://daegam.example.com")
    assert oidc_kwargs(public)["oidc_insecure_http"] is False
    spec = oidc_config_manifest("acme", "ns", **_manifest_args(oidc_kwargs(plain)))["spec"]
    assert spec["inline"] == {"issuer": ISSUER, "jwksUrl": PLATFORM_JWKS, **PLAIN}
    # 발급자를 비우면 홈 클러스터의 내부 주소다
    assert Settings(auth_mode="local", in_cluster=True).issuer == (
        "http://daegam-platform.daegam-system.svc:9000"
    )


def _manifest_args(kwargs: dict) -> dict:
    return {
        "issuer": kwargs["oidc_issuer"],
        "client_id": kwargs["oidc_client_id"],
        "jwks_url": kwargs["oidc_jwks_url"],
        "insecure_http": kwargs["oidc_insecure_http"],
    }


def _inline(settings: Settings) -> dict[str, dict]:
    return {
        m["metadata"]["name"]: m["spec"]["inline"]
        for m in oidc_config_manifests("t", "ns", **oidc_kwargs(settings))
    }


def test_jwks_url_per_mode_and_object():
    """플랫폼이 서명한 토큰의 검증기는 JWKS 를 클러스터 안 주소에서 받는다. 서비스 토큰은 모든
    모드에서 `?use=service` 모양을, local 의 사람 토큰은 같은 경로를 받는다. oidc 의 사람 토큰은
    지금처럼 IdP 디스커버리다. 평문 플래그는 발급자나 JWKS 주소가 http 일 때만 붙는다. local 의
    사람 발급자는 공개 주소가 있으면 그것이다 (P32 1절)."""
    internal = {"issuer": ISSUER, "jwksUrl": SERVICE_JWKS, **PLAIN}
    header = Settings(auth_mode="header", platform_issuer=ISSUER)
    assert _inline(header) == {INTERNAL_OIDC_CONFIG_NAME: internal}
    local = replace(header, auth_mode="local")
    assert _inline(local) == {
        OIDC_CONFIG_NAME: {"issuer": ISSUER, "jwksUrl": PLATFORM_JWKS, **PLAIN},
        INTERNAL_OIDC_CONFIG_NAME: internal,
    }
    # IdP 가 https 면 플랫폼 JWKS 가 평문이어도 daegam-oidc 에는 플래그가 없다. 그 객체는 플랫폼
    # JWKS 를 받지 않는다
    oidc = replace(
        header,
        auth_mode="oidc",
        oidc_issuer="https://okta.example",
        oidc_client_id="c",
        data_plane_issuer="idp",
    )
    assert _inline(oidc) == {
        OIDC_CONFIG_NAME: {"issuer": "https://okta.example", "clientId": "c"},
        INTERNAL_OIDC_CONFIG_NAME: internal,
    }
    # 공개 주소가 https 인 설치. local 의 사람 발급자는 공개 주소이고 JWKS 는 클러스터 안 평문
    # 주소라 daegam-oidc 에 플래그가 붙는다. 서비스 토큰용은 공개 주소와 무관하고, 공개 주소는 local
    # 에서만 발급자다
    assert _inline(replace(local, public_url=PUBLIC_URL)) == {
        OIDC_CONFIG_NAME: {"issuer": PUBLIC_URL, "jwksUrl": PLATFORM_JWKS, **PLAIN},
        INTERNAL_OIDC_CONFIG_NAME: internal,
    }
    assert _inline(replace(oidc, public_url=PUBLIC_URL)) == _inline(oidc)
    assert _inline(replace(header, public_url=PUBLIC_URL)) == _inline(header)
    # https 플랫폼 발급자는 두 객체 모두 플래그가 없다
    tls = replace(local, platform_issuer="https://daegam.example.com")
    assert _inline(tls) == {
        OIDC_CONFIG_NAME: {
            "issuer": "https://daegam.example.com",
            "jwksUrl": "https://daegam.example.com/.well-known/jwks.json",
        },
        INTERNAL_OIDC_CONFIG_NAME: {
            "issuer": "https://daegam.example.com",
            "jwksUrl": "https://daegam.example.com/.well-known/jwks.json?use=service",
        },
    }
