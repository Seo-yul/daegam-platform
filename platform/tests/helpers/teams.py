"""Teams 채널(P47)을 보는 시험이 함께 쓰는 도우미.

Microsoft 쪽 셋(Bot Connector 의 키 묶음, Entra 의 토큰 엔드포인트, Connector 의 대화 API)을 한
httpx MockTransport(`FakeMicrosoft`)로 흉내 낸다. 키는 시험마다 만들고 Microsoft 키 묶음의
모양(F0.1: `alg` 없음, `endorsements` 배열)을 따른다. 활동은 F0.1 의 고정물(`fixtures/teams/`)에서
시작해 이 시험의 봇과 테넌트로 바꾼다.
"""

from __future__ import annotations

import copy
import json
import time
from dataclasses import dataclass, field
from datetime import timedelta
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, unquote, urlsplit

import httpx

from daegam.services.channels.connector import SECRET_KEY, SECRET_NAME
from daegam.services.channels.inbound import CONNECTOR_ISSUER, CONNECTOR_KEYS_URL
from daegam.store.base import new_id, utcnow
from daegam.store.channels import ChannelLink, TeamsAccount
from tests.helpers.consts import PLATFORM_NS
from tests.helpers.oidc import Signer

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "teams"
APP_ID = "c9e8c047-2a74-40a2-b28a-b162d5f5327c"
TENANT_ID = "72f988bf-86f1-41af-91ab-2d7cd011db47"
SERVICE_URL = "https://smba.trafficmanager.net/amer/"
CLIENT_SECRET = "bot-client-secret"
BOT_TOKEN = "bot-token-1"


def fixture(name: str) -> dict[str, Any]:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def connector_jwks(signer: Signer, endorsements: list[str]) -> dict[str, Any]:
    """Microsoft 키 묶음 꼴의 JWKS. `alg` 를 빼고 `endorsements` 를 단다."""
    jwk = dict(signer.jwks()["keys"][0])
    jwk.pop("alg", None)
    jwk["endorsements"] = endorsements
    return {"keys": [jwk]}


def connector_token(signer: Signer, *, service_url: str = SERVICE_URL, **claims: Any) -> str:
    """Bot Connector 가 낸 꼴의 토큰. claims 의 값이 None 이면 그 클레임을 뺀다."""
    now = int(time.time())
    body: dict[str, Any] = {
        "iss": CONNECTOR_ISSUER,
        "aud": APP_ID,
        "nbf": now - 10,
        "exp": now + 3600,
        "serviceurl": service_url,
    }
    body.update(claims)
    return signer.sign({k: v for k, v in body.items() if v is not None})


@dataclass
class TeamsMember:
    """Connector 의 멤버 읽기가 답할 사람. F0.1 의 멤버 읽기 꼴(`objectId`)이다."""

    id: str
    object_id: str
    email: str | None = None
    upn: str | None = None
    name: str = "Teams User"

    def body(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "id": self.id,
            "givenName": self.name,
            "tenantId": TENANT_ID,
            "userRole": "user",
        }
        if self.object_id:
            out["objectId"] = self.object_id
        if self.email:
            out["email"] = self.email
        if self.upn:
            out["userPrincipalName"] = self.upn
        return out


@dataclass
class Posted:
    conversation_id: str
    text: str
    authorization: str


@dataclass
class FakeMicrosoft:
    """키 묶음, 토큰 엔드포인트, Connector 의 대화 API. 받은 요청을 모두 적는다."""

    signer: Signer
    endorsements: list[str] = field(default_factory=lambda: ["msteams", "skype"])
    members: dict[tuple[str, str], TeamsMember] = field(default_factory=dict)
    keys_down: bool = False
    token_expires_in: int = 3600
    requests: list[httpx.Request] = field(default_factory=list)
    posted: list[Posted] = field(default_factory=list)
    token_forms: list[dict[str, list[str]]] = field(default_factory=list)

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handle)

    def handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        url = str(request.url)
        if url == CONNECTOR_KEYS_URL:
            if self.keys_down:
                return httpx.Response(502)
            return httpx.Response(200, json=connector_jwks(self.signer, self.endorsements))
        if request.url.host == "login.microsoftonline.com":
            self.token_forms.append(parse_qs(request.content.decode()))
            # F0.1 의 토큰 응답 꼴에 이 시험의 토큰과 수명을 넣는다
            reply = {
                **fixture("entra-token-response.json"),
                "expires_in": self.token_expires_in,
                "access_token": BOT_TOKEN,
            }
            return httpx.Response(200, json=reply)
        if request.url.host == "smba.trafficmanager.net":
            return self._connector(request)
        return httpx.Response(404)

    def _connector(self, request: httpx.Request) -> httpx.Response:
        parts = [unquote(p) for p in urlsplit(str(request.url)).path.split("/") if p]
        # /<region>/v3/conversations/<id>/members/<member> 또는 .../activities
        conversation = parts[3]
        auth = request.headers.get("authorization", "")
        if request.method == "GET" and parts[4] == "members":
            member = self.members.get((conversation, parts[5]))
            if member is None:
                return httpx.Response(404, json={"error": {"code": "NotFound"}})
            return httpx.Response(200, json=member.body())
        if request.method == "POST" and parts[4] == "activities":
            body = json.loads(request.content)
            self.posted.append(Posted(conversation, body["text"], auth))
            return httpx.Response(201, json={"id": "posted"})
        return httpx.Response(404)

    def texts(self, conversation_id: str | None = None) -> list[str]:
        return [p.text for p in self.posted if conversation_id in (None, p.conversation_id)]


def personal_message(
    text: str,
    *,
    from_id: str = "29:user-1",
    aad: str = "aad-1",
    conversation: str = "a:conv-1",
    activity_id: str | None = None,
    service_url: str = SERVICE_URL,
    tenant: str = TENANT_ID,
    **over: Any,
) -> dict[str, Any]:
    """F0.1 의 개인 대화 메시지를 이 시험의 봇과 사람으로 바꾼 것. `text/html` 사본은 둔다."""
    doc = copy.deepcopy(fixture("message-personal.json"))
    doc.update(
        {
            "id": activity_id or f"act-{time.monotonic_ns()}",
            "text": text,
            "serviceUrl": service_url,
            "from": {"id": from_id, "name": "Megan Bowen", "aadObjectId": aad},
            "conversation": {"conversationType": "personal", "id": conversation},
            "recipient": {"id": f"28:{APP_ID}", "name": "Daegam"},
            "channelData": {"tenant": {"id": tenant}},
        }
    )
    doc.update(over)
    return doc


def configure_teams(mongo, fake_secrets, *, secret: bool = True) -> None:
    """Platform Settings 에 이 시험의 봇을 두고 Client Secret 을 관리 Secret 에 넣는다."""
    teams = {
        "enabled": False,
        "app_id": APP_ID,
        "entra_tenant_id": TENANT_ID,
        "secret_set": secret,
    }
    mongo["settings"].update_one(
        {"_id": "platform"}, {"$set": {"channels": {"teams": teams}}}, upsert=True
    )
    if secret:
        fake_secrets.put(SECRET_NAME, {SECRET_KEY: CLIENT_SECRET}, namespace=PLATFORM_NS)


def plant_link(
    mongo, sub: str = "u1", app_id: str = APP_ID, tenant: str = TENANT_ID, **over: Any
) -> str:
    """연결 하나를 Mongo 에 바로 넣는다. 30일 뒤에 끝난다."""
    now = utcnow()
    fields: dict[str, Any] = {
        "linked_at": now,
        "ends_at": now + timedelta(days=30),
        "last_turn_at": now,
        **over,
    }
    link = ChannelLink(
        id=new_id(),
        sub=sub,
        username=sub,
        teams=TeamsAccount(
            app_id=app_id,
            entra_tenant_id=tenant,
            aad_object_id=f"aad-{sub}",
            from_id=f"29:{sub}",
            conversation_id=f"a:{sub}",
            service_url=SERVICE_URL,
        ),
        **fields,
    )
    mongo["channel_links"].insert_one(link.to_doc())
    return link.id


def attach(app, fake: FakeMicrosoft) -> None:
    """플랫폼의 Teams 클라이언트 둘이 fake 로 부르게 한다."""
    app.state.teams_bot.verifiers.transport = fake.transport()
    app.state.teams_connector.transport = fake.transport()


__all__ = [
    "APP_ID",
    "BOT_TOKEN",
    "CLIENT_SECRET",
    "SERVICE_URL",
    "TENANT_ID",
    "FakeMicrosoft",
    "TeamsMember",
    "attach",
    "configure_teams",
    "connector_jwks",
    "connector_token",
    "fixture",
    "personal_message",
    "plant_link",
]
