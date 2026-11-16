"""P47 6.2절 "Sending", 7절: 봇 토큰과 Connector 호출의 규칙.

올릴 주소는 https, 443, 사용자 정보 없음, 정확히 `smba.trafficmanager.net` 이어야 하고 GCC·GCC High·
DoD 의 호스트는 까닭을 달고 거절한다. 리다이렉트를 따르지 않고, `Authorization` 은 검사를 지난
뒤에만 싣는다. 봇 토큰은 (테넌트, App ID)마다 만료 5분 전까지 쓴다. Client Secret 을 읽지 못하면 503
`teams_secret_unavailable` 이다.
"""

from __future__ import annotations

import asyncio

import httpx
import pytest

from daegam.services.channels.connector import (
    REFUSED_CLOUD_HOST,
    REFUSED_HOST,
    ConnectorFailed,
    Member,
    ServiceUrlRefused,
    TeamsConnector,
    connector_base,
)
from daegam.services.channels.errors import ChannelError
from daegam.services.channels.inbound import CONNECTOR_ISSUER, CONNECTOR_KEYS_URL
from tests.helpers.consts import PLATFORM_NS
from tests.helpers.fake_secrets import FakeSecretStore
from tests.helpers.oidc import Signer
from tests.helpers.teams import (
    APP_ID,
    BOT_TOKEN,
    CLIENT_SECRET,
    SERVICE_URL,
    TENANT_ID,
    FakeMicrosoft,
    TeamsMember,
    fixture,
)


class Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


def _connector(secrets: FakeSecretStore | None, fake: FakeMicrosoft, clock=None) -> TeamsConnector:
    return TeamsConnector(
        lambda: secrets, namespace=PLATFORM_NS, clock=clock or Clock(), transport=fake.transport()
    )


@pytest.fixture
def secrets() -> FakeSecretStore:
    s = FakeSecretStore()
    s.put("daegam-channel-teams", {"client_secret": CLIENT_SECRET}, namespace=PLATFORM_NS)
    return s


@pytest.fixture
def fake() -> FakeMicrosoft:
    return FakeMicrosoft(Signer(None))


def _post(c: TeamsConnector, service_url: str = SERVICE_URL) -> None:
    asyncio.run(
        c.post(
            service_url,
            "a:conv-1",
            {"type": "message", "text": "hi"},
            tenant=TENANT_ID,
            app_id=APP_ID,
            ca_bundle=None,
        )
    )


@pytest.mark.parametrize(
    ("url", "base"),
    [
        (SERVICE_URL, "https://smba.trafficmanager.net/amer/"),
        # F0.1 의 봇 추가 활동이 실은 주소다
        (
            "https://smba.trafficmanager.net/amer-client-ss.msg/",
            "https://smba.trafficmanager.net/amer-client-ss.msg/",
        ),
        ("https://SMBA.trafficmanager.net:443/teams", "https://smba.trafficmanager.net/teams/"),
    ],
)
def test_connector_urls_that_pass(url, base):
    assert connector_base(url) == base


@pytest.mark.parametrize(
    "url",
    [
        "http://smba.trafficmanager.net/amer/",
        "https://smba.trafficmanager.net:8443/amer/",
        "https://user:pw@smba.trafficmanager.net/amer/",
        "https://evil.trafficmanager.net/amer/",
        "https://smba.trafficmanager.net.evil.example/amer/",
        "https://smba.trafficmanager.net/amer/?x=1",
        "https://smba.trafficmanager.net/../amer/",
        "https://smba.trafficmanager.net/a%2Fb/",
        "not a url",
    ],
)
def test_connector_urls_off_the_rules_are_refused(url):
    with pytest.raises(ServiceUrlRefused) as e:
        connector_base(url)
    assert e.value.reason == REFUSED_HOST


@pytest.mark.parametrize(
    "host",
    [
        "smba.infra.gcc.teams.microsoft.com",
        "smba.infra.gov.teams.microsoft.us",
        "smba.infra.dod.teams.microsoft.us",
    ],
)
def test_other_microsoft_clouds_are_refused_with_their_reason(host):
    with pytest.raises(ServiceUrlRefused) as e:
        connector_base(f"https://{host}/amer/")
    assert e.value.reason == REFUSED_CLOUD_HOST


def test_a_refused_url_sends_nothing(secrets, fake):
    c = _connector(secrets, fake)
    with pytest.raises(ServiceUrlRefused):
        _post(c, "https://evil.example/amer/")
    assert fake.requests == []


def test_the_bot_token_is_fetched_once_and_sent_only_to_the_connector(secrets, fake):
    c = _connector(secrets, fake)
    _post(c)
    _post(c)
    assert len(fake.token_forms) == 1
    form = fake.token_forms[0]
    assert form["grant_type"] == ["client_credentials"]
    assert form["client_id"] == [APP_ID] and form["client_secret"] == [CLIENT_SECRET]
    assert form["scope"] == ["https://api.botframework.com/.default"]
    hosts = {r.url.host for r in fake.requests}
    assert hosts == {"login.microsoftonline.com", "smba.trafficmanager.net"}
    token_url = next(r.url for r in fake.requests if r.url.host == "login.microsoftonline.com")
    assert token_url.path == f"/{TENANT_ID}/oauth2/v2.0/token"
    assert all(
        r.headers.get("authorization") == f"Bearer {BOT_TOKEN}"
        for r in fake.requests
        if r.url.host == "smba.trafficmanager.net"
    )
    assert not any("authorization" in r.headers for r in fake.requests if "login" in r.url.host)
    # 같은 CA 묶음의 호출은 HTTP 클라이언트 하나를 나눠 쓴다
    assert len(c._clients) == 1


def test_the_bot_token_is_dropped_five_minutes_before_it_expires(secrets, fake):
    clock = Clock()
    c = _connector(secrets, fake, clock)
    _post(c)
    clock.now += 3600 - 300 - 1
    _post(c)
    assert len(fake.token_forms) == 1
    clock.now += 2
    _post(c)
    assert len(fake.token_forms) == 2
    c.forget_tokens()
    _post(c)
    assert len(fake.token_forms) == 3


def test_redirects_are_failures(secrets):
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "login.microsoftonline.com":
            return httpx.Response(200, json={"access_token": "t", "expires_in": 3600})
        return httpx.Response(302, headers={"Location": "https://evil.example/"})

    c = TeamsConnector(
        lambda: secrets, namespace=PLATFORM_NS, transport=httpx.MockTransport(handler)
    )
    with pytest.raises(ConnectorFailed) as e:
        _post(c)
    assert e.value.status == 302


@pytest.mark.parametrize("store", ["none", "empty"])
def test_a_missing_secret_is_503(fake, store):
    secrets = None if store == "none" else FakeSecretStore()
    c = _connector(secrets, fake)
    with pytest.raises(ChannelError) as e:
        _post(c)
    assert (e.value.status, e.value.code) == (503, "teams_secret_unavailable")
    assert fake.requests == []


def test_the_inbound_trust_points_are_microsofts_published_ones():
    """검증기의 발급자와 키 묶음 주소는 고정이고 F0.1 의 OpenID 문서와 같다."""
    openid = fixture("connector-openid.json")
    assert (CONNECTOR_ISSUER, CONNECTOR_KEYS_URL) == (openid["issuer"], openid["jwks_uri"])


def test_member_read_takes_the_published_shape():
    """F0.1 의 멤버 읽기는 Entra 객체 id 를 `objectId` 로 싣는다."""
    member = Member.of(fixture("member-get.json"))
    assert member.aad_object_id == "9d3e08f9-a7ae-43aa-a4d3-de3f319a8a9c"
    assert member.user_principal_name == "labrown@fabrikam.com"
    assert member.email == "Larry.Brown@fabrikam.com"
    assert member.name == "Larry Brown"
    assert Member.of({"id": "29:x", "aadObjectId": "a"}).aad_object_id == "a"


def test_member_read_answers_none_for_a_non_member(secrets, fake):
    fake.members[("a:conv-1", "29:user-1")] = TeamsMember("29:user-1", "aad-1", upn="u@x.test")
    c = _connector(secrets, fake)

    def read(member_id: str):
        return asyncio.run(
            c.member(
                SERVICE_URL,
                "a:conv-1",
                member_id,
                tenant=TENANT_ID,
                app_id=APP_ID,
                ca_bundle=None,
            )
        )

    assert read("29:user-1").user_principal_name == "u@x.test"
    assert read("29:someone-else") is None
    path = fake.requests[-1].url.raw_path.decode()
    assert path == "/amer/v3/conversations/a%3Aconv-1/members/29%3Asomeone-else"
