"""접근 정리와 사람 기록을 보는 oidc 시험이 함께 쓰는 도우미 (P53b 6.1·6.3·6.5절).

리컨실 단계 하나를 앱의 루프에서 돌리는 `run_step`, mock IdP 토큰으로 사람 기록과 소속 줄을
만드는 `sign_in`, 발급 경로 없이 Mongo 에 바로 넣는 개인 토큰(`plant`)과 OAuth 연결(`connect`),
회수 결과를 2.0.0-rc1 의 모델로 읽어 보는 `revoked`·`cut` 이다.
"""

from __future__ import annotations

import uuid
from datetime import timedelta
from typing import Any, Literal

from fastapi.testclient import TestClient
from pydantic import BaseModel, ConfigDict

from daegam.store.base import utcnow
from daegam.store.pat_keys import BOUND
from tests.helpers.auth import bearer

TENANT = "a"


def run_step(c: TestClient, name: str) -> dict[str, list[str]]:
    """리컨실의 배선된 단계 name 을 한 번 돌린다."""
    return c.portal.call(dict(c.app.state.reconciler.extra_steps)[name], utcnow())


def sweep(c: TestClient) -> dict[str, list[str]]:
    """리컨실의 접근 정리 단계 한 번."""
    return run_step(c, "access")


# 2.0.0-rc1 의 회수 사유 (그 태그의 store/personal_tokens.py, store/oauth_grants.py). rc1 으로
# 되돌린 플랫폼이 N 이 쓴 문서를 읽으려면 사유가 이 안이어야 한다
_RC1_SHARED = Literal[
    "user",
    "admin",
    "disabled",
    "deleted",
    "password",
    "membership",
    "issuer_changed",
    "key_rotated",
]


class Rc1Token(BaseModel):
    model_config = ConfigDict(extra="ignore")

    revoke_reason: Literal[_RC1_SHARED, "key_missing"] | None = None


class Rc1Grant(BaseModel):
    model_config = ConfigDict(extra="ignore")

    revoked_reason: (
        Literal[
            _RC1_SHARED,
            "client",
            "resource_gone",
            "oauth_disabled",
            "client_disallowed",
            "code_reuse",
            "refresh_reuse",
            "evicted",
        ]
        | None
    ) = None


def sign_in(c: TestClient, token, sub: str, *groups: str) -> None:
    """mock IdP 토큰으로 요청해 사람 기록과 소속 줄을 만든다."""
    assert c.get("/me", headers=bearer(token(sub, *groups))).status_code == 200


def plant(c: TestClient, mongo, sub: str, tenant: str = TENANT, **kw: Any) -> str:
    """활성 개인 토큰 하나와 그 슬롯을 넣는다. kw 는 토큰 문서의 필드를 덮는다."""
    issuer = c.app.state.internal_issuer
    token_id, kid, now = uuid.uuid4().hex, f"pat-{uuid.uuid4().hex[:16]}", utcnow()
    doc = {
        "_id": token_id,
        "sub": sub,
        "username": sub,
        "tenant": tenant,
        "group": f"daegam-{tenant}",
        "role": "developer",
        "label": "t",
        "kid": kid,
        "iss": issuer.human_issuer,
        "tv": 0,
        "created_at": now,
        "expires_at": now + timedelta(days=30),
        "purge_at": now + timedelta(days=60),
        **kw,
    }
    mongo["personal_tokens"].insert_one(doc)
    mongo["pat_keys"].insert_one(
        {
            "_id": kid,
            "master": issuer.kid,
            "state": BOUND,
            "created_at": now,
            "bound_at": now,
            "token_id": token_id,
        }
    )
    return token_id


def connect(c: TestClient, mongo, sub: str, tenant: str = TENANT) -> str:
    """활성 OAuth 연결 하나를 넣는다."""
    issuer = c.app.state.internal_issuer
    grant_id, now = uuid.uuid4().hex, utcnow()
    mongo["oauth_grants"].insert_one(
        {
            "_id": grant_id,
            "family_id": uuid.uuid4().hex,
            "sub": sub,
            "username": sub,
            "tv": 0,
            "client_id": "dcr-1",
            "client_ref": "dcr-1",
            "client_name": "client",
            "client_kind": "dcr",
            "tenant": tenant,
            "resource": "https://gw.example/mcp",
            "resource_kind": "gateway",
            "resource_id": "gw-1",
            "scope": "mcp",
            "iss": issuer.human_issuer,
            "signer_kid": issuer.kid,
            "refresh_sha256": "x",
            "refresh_generation": 0,
            "refresh_expires_at": now + timedelta(days=14),
            "created_at": now,
            "last_refresh_at": now,
            "expires_at": now + timedelta(days=30),
            "purge_at": now + timedelta(days=60),
        }
    )
    return grant_id


def revoked(mongo, token_id: str) -> tuple[str, str | None] | None:
    """회수가 끝난 토큰의 (사유, 까닭). 살아 있으면 None 이다. rc1 의 모델이 읽는지도 본다."""
    doc = mongo["personal_tokens"].find_one({"_id": token_id})
    Rc1Token.model_validate(doc)
    if doc.get("revoked_at") is None:
        return None
    return doc["revoke_reason"], doc.get("revoke_cause")


def cut(mongo, grant_id: str) -> tuple[str, str | None] | None:
    doc = mongo["oauth_grants"].find_one({"_id": grant_id})
    Rc1Grant.model_validate(doc)
    if doc.get("revoked_at") is None:
        return None
    return doc["revoked_reason"], doc.get("revoked_cause")
