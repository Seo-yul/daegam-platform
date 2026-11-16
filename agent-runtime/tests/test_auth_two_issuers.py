"""노출한 Agent 의 두 발급자(P53b 6.8절, 능력 two_issuers). 데이터 플레인이 믿는 플랫폼과 그
곁의 IdP 다.

토큰의 iss 로 발급자를 고르고 발급자마다의 계약(허용 그룹, 그룹 클레임, client id 클레임과 목록,
위임 토큰 묶기)으로 판정한다. 어느 발급자도 아닌 iss, 한 발급자를 이름 대고 다른 발급자의 키로
서명한 토큰은 401 이고, 한쪽 설정이 없으면 다른 쪽이 혼자 하나의 발급자로 돈다.
"""

from __future__ import annotations

import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import astuple, replace
from pathlib import Path
from typing import Any

import httpx
import jwt
import pytest

from runtime.auth import (
    AGENT_TARGET,
    DUPLICATE_ISSUER,
    IDP_ENV,
    OIDC_ENV,
    IssuerSettings,
    read_issuers,
)
from runtime.config import ConfigError
from runtime.features import CAPABILITIES, DOCUMENT_FEATURES, TWO_ISSUERS
from runtime.state import Settings
from tests.app_fixtures import INSTANCE_TOKEN, bearer, status_and_code
from tests.conftest import RunningApp, run_app
from tests.idp_fixtures import AUDIENCE, ISSUER, FakeIdp, Signer

PLATFORM_ISSUER = "http://platform.test"
TENANT_GROUP = "daegam-acme"
IDP_GROUP = "agent-users"
FORBIDDEN = (403, "forbidden")
INVALID = (401, "invalid_token")

# local 모드와 같은 플랫폼 계약이다. 클러스터 안 JWKS 주소, 테넌트 그룹과 한정한 IdP 그룹이다.
# 렌더는 플랫폼에 client id 를 주지 않지만 준 것이 있어도 런타임이 버리는지 보려고 하나를 둔다
PLATFORM = IssuerSettings(
    issuer=PLATFORM_ISSUER,
    jwks_url=PLATFORM_ISSUER + "/jwks",
    insecure_http=True,
    allowed_groups=(TENANT_GROUP, f"daegam-group:acme:{IDP_GROUP}"),
    agent_client_ids=("svc-1",),
    is_platform=True,
)
# IdP 만 믿는 oidc 배포와 같은 IdP 계약이다. IdP 의 클레임 이름, 날 그룹, Agent client id
IDP = IssuerSettings(
    issuer=ISSUER,
    insecure_http=True,
    groups_claim="realm_access.roles",
    client_id_claim="azp",
    allowed_groups=(IDP_GROUP,),
    agent_client_ids=("svc-1",),
    beside_platform=True,
)


def serving(*idps: FakeIdp) -> httpx.AsyncClient:
    """발급자의 host 로 요청을 나눠 받는 클라이언트다. 검증기 둘이 같이 쓴다."""
    by_host = {httpx.URL(idp.signer.issuer).host: idp for idp in idps}

    def handler(request: httpx.Request) -> httpx.Response:
        idp = by_host.get(request.url.host)
        return idp.handler(request) if idp is not None else httpx.Response(404)

    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


@asynccontextmanager
async def issuers_app(
    doc: dict[str, Any],
    *idps: FakeIdp,
    oidc_settings: IssuerSettings | None = PLATFORM,
    idp_settings: IssuerSettings | None = IDP,
) -> AsyncIterator[RunningApp]:
    async with serving(*idps) as http:
        settings = Settings(
            instance_token=INSTANCE_TOKEN,
            oidc_audience=AUDIENCE,
            oidc=oidc_settings,
            idp=idp_settings,
        )
        async with run_app(doc, settings=settings, oidc_http_client=http) as running:
            yield running


def signed(signer: Signer, claims: dict[str, Any], *, kid: str | None = None) -> str:
    """claims 그대로(iss 를 채우지 않고) signer 의 키로 서명한 토큰이다. kid 는 머리에 달 값이다."""
    payload = {"exp": int(time.time()) + 300, **claims}
    headers = {"kid": kid or signer.kid}
    return jwt.encode(payload, signer.private_key, algorithm="RS256", headers=headers)


async def sessions(running: RunningApp, token: str) -> httpx.Response:
    return await running.client.get("/sessions", headers=bearer(token))


async def status(running: RunningApp, token: str) -> int:
    return (await sessions(running, token)).status_code


async def refusal(running: RunningApp, token: str) -> tuple[int, str]:
    return status_and_code(await sessions(running, token))


async def mismatched(running: RunningApp, token: str) -> bool:
    """어느 발급자도 아닌 iss 로 거절됐는가."""
    response = await sessions(running, token)
    return status_and_code(response) == INVALID and response.json()["message"] == "issuer mismatch"


@pytest.fixture
def platform() -> FakeIdp:
    return FakeIdp(Signer(PLATFORM_ISSUER))


@pytest.fixture
def idp() -> FakeIdp:
    return FakeIdp(Signer())


# -- 발급자마다의 계약 -----------------------------------------------------------------------


async def test_each_issuer_admits_its_own_tokens(support_doc, platform, idp) -> None:
    """플랫폼 토큰은 groups 를 플랫폼 목록에, IdP 토큰은 IdP 의 그룹 클레임을 IdP 목록에 댄다.
    검증기마다 자기 JWKS 만 받는다."""
    async with issuers_app(support_doc, platform, idp) as running:
        member = platform.signer.token(sub="alice", groups=[TENANT_GROUP])
        assert await status(running, member) == 200
        qualified = platform.signer.token(sub="bob", groups=[f"daegam-group:acme:{IDP_GROUP}"])
        assert await status(running, qualified) == 200
        assert platform.calls == ["/jwks"] and idp.calls == []

        person = idp.signer.token(sub="carol", realm_access={"roles": [IDP_GROUP]})
        assert await status(running, person) == 200
        client = idp.signer.token(sub="svc-1", azp="svc-1")
        assert await status(running, client) == 200
        assert idp.calls == ["/.well-known/openid-configuration", "/jwks"]
        assert platform.calls == ["/jwks"]


async def test_a_claim_of_one_issuer_does_not_decide_for_the_other(
    support_doc, platform, idp
) -> None:
    """한 발급자의 그룹, 그룹 클레임, client id 는 다른 발급자의 토큰을 들이지 않는다."""
    async with issuers_app(support_doc, platform, idp) as running:
        # 플랫폼 토큰에 IdP 의 날 그룹이나 IdP 의 그룹 클레임을 실어도 플랫폼 목록에 들지 않는다
        raw_group = platform.signer.token(sub="dave", groups=[IDP_GROUP])
        assert await refusal(running, raw_group) == FORBIDDEN
        idp_claim = platform.signer.token(sub="erin", realm_access={"roles": [TENANT_GROUP]})
        assert await refusal(running, idp_claim) == FORBIDDEN
        # 플랫폼 토큰은 client id 로 들어오지 않는다. 플랫폼 목록의 것(cid)도, IdP 의 것(azp)도
        for claim in ("azp", "cid"):
            client = platform.signer.token(sub="svc-1", **{claim: "svc-1"})
            assert await refusal(running, client) == FORBIDDEN

        # IdP 토큰에 플랫폼 그룹을 실어도, 플랫폼의 그룹 클레임에 실어도 IdP 목록에 들지 않는다
        tenant = idp.signer.token(sub="frank", realm_access={"roles": [TENANT_GROUP]})
        assert await refusal(running, tenant) == FORBIDDEN
        top_level = idp.signer.token(sub="grace", groups=[IDP_GROUP])
        assert await refusal(running, top_level) == FORBIDDEN
        cid_only = idp.signer.token(sub="svc-1", cid="svc-1")
        assert await refusal(running, cid_only) == FORBIDDEN


async def test_only_the_platform_binds_delegation_tokens(support_doc, platform, idp) -> None:
    """플랫폼이 낸 위임 토큰은 이 Agent 에 묶인 것만 받는다. IdP 토큰은 IdP 만 믿는 oidc 배포처럼
    act 를 따로 보지 않는다 (P53 3.3절)."""
    target = AGENT_TARGET.format(support_doc["instance"]["id"])
    async with issuers_app(support_doc, platform, idp) as running:
        other = platform.signer.token(
            groups=[TENANT_GROUP], act={"sub": "svc:agent:x"}, bound_to="gateway:g"
        )
        assert await refusal(running, other) == FORBIDDEN
        bound = platform.signer.token(
            groups=[TENANT_GROUP], act={"sub": "svc:agent:x"}, bound_to=target
        )
        assert await status(running, bound) == 200
        delegated = idp.signer.token(realm_access={"roles": [IDP_GROUP]}, act={"sub": "x"})
        assert await status(running, delegated) == 200


async def test_empty_groups_admit_every_token_of_that_issuer_only(
    support_doc, platform, idp
) -> None:
    """빈 허용 그룹은 그 발급자가 audience 로 서명한 주체 전부다(D27). 다른 발급자의 빈 목록이 이
    발급자의 목록을 넓히지 않는다."""
    open_idp = IssuerSettings(issuer=ISSUER, insecure_http=True)
    async with issuers_app(support_doc, platform, idp, idp_settings=open_idp) as running:
        assert await status(running, idp.signer.token(sub="anyone")) == 200
        outsider = platform.signer.token(sub="mallory", groups=["daegam-other"])
        assert await refusal(running, outsider) == FORBIDDEN


async def test_the_idp_beside_refuses_service_subjects(support_doc, platform, idp) -> None:
    """곁의 IdP 토큰의 sub 가 서비스 어카운트(sa:)나 플랫폼 서비스(svc:)의 것이면 403 이다. 플랫폼은
    그런 sub 의 사람 토큰을 내지 않는다(P53b 6.5절). 플랫폼이 낸 서비스 어카운트 토큰과 IdP 만 믿는
    배포의 IdP 토큰은 그대로다."""
    member = {"realm_access": {"roles": [IDP_GROUP]}}
    async with issuers_app(support_doc, platform, idp) as running:
        for sub in ("sa:acct-1", "svc:agent:x"):
            assert await refusal(running, idp.signer.token(sub=sub, **member)) == FORBIDDEN
        assert await status(running, idp.signer.token(sub="saga", **member)) == 200
        account = platform.signer.token(sub="sa:acct-1", groups=[TENANT_GROUP])
        assert await status(running, account) == 200
    alone = replace(IDP, beside_platform=False)
    async with issuers_app(
        support_doc, platform, idp, oidc_settings=alone, idp_settings=None
    ) as running:
        assert await status(running, idp.signer.token(sub="sa:acct-1", **member)) == 200


# -- 어느 발급자의 토큰인가 ----------------------------------------------------------------------


async def test_a_token_of_neither_issuer_is_401_without_fetching_keys(
    support_doc, platform, idp
) -> None:
    async with issuers_app(support_doc, platform, idp) as running:
        stranger = Signer("http://stranger.test").token(groups=[TENANT_GROUP])
        assert await mismatched(running, stranger)
        no_iss = signed(platform.signer, {"aud": AUDIENCE, "sub": "x", "groups": [TENANT_GROUP]})
        assert await refusal(running, no_iss) == INVALID
        # 끝의 / 하나라도 다르면 다른 발급자다. 검증기가 iss 를 글자 그대로 견주는 것과 같다
        slashed = platform.signer.token(iss=PLATFORM_ISSUER + "/", groups=[TENANT_GROUP])
        assert await refusal(running, slashed) == INVALID
        assert platform.calls == [] and idp.calls == []


@pytest.mark.parametrize("named", ["platform", "idp"])
async def test_a_token_naming_one_issuer_signed_by_the_other_is_401(
    support_doc, platform, idp, named: str
) -> None:
    """iss 가 고른 검증기는 자기 발급자의 키로만 푼다. 다른 발급자의 kid 를 달아도, 이름 댄 발급자의
    kid 를 달아도 401 이다."""
    claimed, forger = (platform, idp) if named == "platform" else (idp, platform)
    claims = {
        "iss": claimed.signer.issuer,
        "aud": AUDIENCE,
        "sub": "mallory",
        "groups": [TENANT_GROUP],
        "realm_access": {"roles": [IDP_GROUP]},
    }
    async with issuers_app(support_doc, platform, idp) as running:
        own_kid = forger.signer.token(**claims)
        assert await refusal(running, own_kid) == INVALID
        borrowed_kid = signed(forger.signer, claims, kid=claimed.signer.kid)
        assert await refusal(running, borrowed_kid) == INVALID
        # 진짜 토큰은 그대로 통한다
        genuine = claimed.signer.token(**{k: v for k, v in claims.items() if k != "iss"})
        assert await status(running, genuine) == 200
        assert forger.calls == []


# -- 한쪽 설정만 있을 때 ---------------------------------------------------------------------


async def test_the_idp_alone_works_without_the_first_issuer(support_doc, platform, idp) -> None:
    async with issuers_app(support_doc, platform, idp, oidc_settings=None) as running:
        assert await status(running, idp.signer.token(realm_access={"roles": [IDP_GROUP]})) == 200
        member = platform.signer.token(groups=[TENANT_GROUP])
        assert await mismatched(running, member)
        assert platform.calls == []


async def test_the_first_issuer_alone_works_as_a_single_issuer(support_doc, platform, idp) -> None:
    async with issuers_app(support_doc, platform, idp, idp_settings=None) as running:
        assert await status(running, platform.signer.token(groups=[TENANT_GROUP])) == 200
        person = idp.signer.token(realm_access={"roles": [IDP_GROUP]})
        assert await mismatched(running, person)
        assert idp.calls == []


async def test_without_issuers_a_jwt_is_401_unauthorized(support_doc, platform, idp) -> None:
    """audience 가 없으면 두 발급자가 있어도 외부 경로가 없다(header 모드 배포)."""
    settings = Settings(instance_token=INSTANCE_TOKEN, oidc=PLATFORM, idp=IDP)
    assert settings.issuers == ()
    async with run_app(support_doc, settings=settings) as running:
        token = platform.signer.token(groups=[TENANT_GROUP])
        assert await refusal(running, token) == (401, "unauthorized")


# -- 설정 ------------------------------------------------------------------------------------


def test_settings_read_both_issuers_from_env(monkeypatch) -> None:
    """두 env 묶음은 서로 섞이지 않는다. IdP 는 JWKS 주소가 있어도 위임 토큰을 묶지 않는다."""
    env = {
        "DAEGAM_OIDC_AUDIENCE": AUDIENCE,
        "DAEGAM_OIDC_ISSUER": PLATFORM_ISSUER,
        "DAEGAM_OIDC_JWKS_URL": PLATFORM_ISSUER + "/jwks",
        "DAEGAM_OIDC_INSECURE_HTTP": "true",
        "DAEGAM_ALLOWED_GROUPS": f"{TENANT_GROUP}, daegam-group:acme:{IDP_GROUP}",
        "DAEGAM_IDP_ISSUER": ISSUER,
        "DAEGAM_IDP_JWKS_URL": ISSUER + "/keys",
        "DAEGAM_IDP_GROUPS_CLAIM": "realm_access.roles",
        "DAEGAM_IDP_CLIENT_ID_CLAIM": "azp",
        "DAEGAM_IDP_ALLOWED_GROUPS": IDP_GROUP,
        "DAEGAM_IDP_AGENT_CLIENT_IDS": "svc-1,svc-2",
    }
    for name in [*_env_names(), "DAEGAM_OIDC_AUDIENCE"]:
        monkeypatch.delenv(name, raising=False)
    for name, value in env.items():
        monkeypatch.setenv(name, value)
    settings = Settings.from_env()
    assert settings.issuers == (
        IssuerSettings(
            issuer=PLATFORM_ISSUER,
            jwks_url=PLATFORM_ISSUER + "/jwks",
            insecure_http=True,
            allowed_groups=(TENANT_GROUP, f"daegam-group:acme:{IDP_GROUP}"),
            is_platform=True,
        ),
        IssuerSettings(
            issuer=ISSUER,
            jwks_url=ISSUER + "/keys",
            groups_claim="realm_access.roles",
            client_id_claim="azp",
            allowed_groups=(IDP_GROUP,),
            agent_client_ids=("svc-1", "svc-2"),
            beside_platform=True,
        ),
    )
    monkeypatch.setenv("DAEGAM_OIDC_ISSUER", "")
    assert [spec.issuer for spec in Settings.from_env().issuers] == [ISSUER]


def _env_names() -> list[str]:
    return [name for env in (OIDC_ENV, IDP_ENV) for name in astuple(env)]


async def test_an_idp_beside_makes_the_first_issuer_the_platform(
    support_doc, platform, idp
) -> None:
    """곁에 IdP 가 있으면 첫 발급자는 JWKS 주소가 빠진 렌더에서도 플랫폼이다. 위임 토큰은 이
    Agent 에 묶인 것만 받고 client id 로는 들이지 않는다. 검사가 닫힌 쪽으로 실패한다."""
    oidc, beside = read_issuers(
        {
            "DAEGAM_OIDC_ISSUER": PLATFORM_ISSUER,
            "DAEGAM_OIDC_INSECURE_HTTP": "true",
            "DAEGAM_ALLOWED_GROUPS": TENANT_GROUP,
            "DAEGAM_AGENT_CLIENT_IDS": "svc-1",
            "DAEGAM_IDP_ISSUER": ISSUER,
            "DAEGAM_IDP_INSECURE_HTTP": "true",
        }
    )
    assert oidc is not None and oidc.jwks_url is None and oidc.is_platform
    assert beside is not None and beside.beside_platform and not beside.is_platform
    alone, _ = read_issuers({"DAEGAM_OIDC_ISSUER": PLATFORM_ISSUER})
    assert alone is not None and not alone.is_platform  # IdP 만 믿는 oidc 배포의 첫 발급자다
    async with issuers_app(
        support_doc, platform, idp, oidc_settings=oidc, idp_settings=beside
    ) as running:
        assert await status(running, platform.signer.token(groups=[TENANT_GROUP])) == 200
        foreign = platform.signer.token(
            groups=[TENANT_GROUP], act={"sub": "svc:agent:x"}, bound_to="gateway:g"
        )
        assert await refusal(running, foreign) == FORBIDDEN
        client = platform.signer.token(sub="svc-1", cid="svc-1")
        assert await refusal(running, client) == FORBIDDEN
        assert await status(running, idp.signer.token(act={"sub": "x"})) == 200


async def test_the_same_issuer_twice_fails_the_start(support_doc, termination_log: Path) -> None:
    """두 env 묶음이 같은 발급자를 가리키면 어느 계약이 판정할지 정할 수 없다. 기동이 실패하고
    이유가 termination log 에 남아 플랫폼이 CrashLoopBackOff 의 이유로 보인다."""
    settings = Settings(
        instance_token=INSTANCE_TOKEN,
        oidc_audience=AUDIENCE,
        oidc=PLATFORM,
        idp=replace(IDP, issuer=PLATFORM_ISSUER + "/"),  # 검증기가 끝의 / 를 벗기므로 같다
    )
    with pytest.raises(ConfigError) as info:
        async with run_app(support_doc, settings=settings):
            pass
    assert info.value.code == DUPLICATE_ISSUER == "duplicate_issuer"
    assert termination_log.read_text(encoding="utf-8") == str(info.value)


def test_two_issuers_is_a_capability_not_a_document_feature() -> None:
    """플랫폼은 이 이름을 알린 이미지에만 플랫폼을 첫 발급자로 렌더한다. 문서에는 싣지 않는다."""
    assert TWO_ISSUERS == "two_issuers"
    assert TWO_ISSUERS in CAPABILITIES and TWO_ISSUERS not in DOCUMENT_FEATURES
