"""서비스 어카운트 API (P53b 6.7절). 플랫폼이 사람 토큰을 내는 모드(local, oidc)에 있고 header
모드에서는 404 다. 모두 그 테넌트의 `members:write`(tenant-admin 이상)가 필요하다.

GET    /tenants/{tenant}/service-accounts                     계정과 토큰, 설정값(mode)과
                                                              이 사람이 넓힐 수 있는가(can_manage)
POST   /tenants/{tenant}/service-accounts                     {name, role} → 201. 409 name_conflict
PATCH  /tenants/{tenant}/service-accounts/{id}                {role}. 낮추면 그 계정의 토큰을 모두
                                                              회수한다
DELETE /tenants/{tenant}/service-accounts/{id}                지우고 토큰을 모두 회수한다
POST   /tenants/{tenant}/service-accounts/{id}/tokens         {label, expires_at?, password?} → 201.
                                                              평문은 이 응답에만 있다. local 은
                                                              password, oidc 는 `X-Daegam-Id-Token`
                                                              의 최근 로그인을 본다
DELETE /tenants/{tenant}/service-accounts/{id}/tokens/{tid}   관리자 회수(revoke_reason=admin)

넓히는 셋(만들기, 역할 바꾸기, 토큰 내기)은 `DAEGAM_SERVICE_ACCOUNTS` 를 따른다. off 면 409
`service_accounts_off`, platform-admin 이면 플랫폼 관리자가 아닌 사람은 403 `forbidden` 이다. 규칙은
services/service_accounts.py 다. 감사는 미들웨어가 남기고, 토큰 라우트는 핸들러가 토큰 id 를 둔다.
"""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends, Request

from daegam.api._common import audit_target, check_tenant, or_404
from daegam.api.deps import Settings_, require_minting, require_recent_sign_in, require_scope
from daegam.api.personal_tokens import token_out
from daegam.api.schemas import ERROR_RESPONSES
from daegam.api.schemas.backend_apis import TokenRevokeResponse
from daegam.api.schemas.personal_tokens import IssuedPersonalToken
from daegam.api.schemas.service_accounts import (
    ServiceAccountDeleteOut,
    ServiceAccountIn,
    ServiceAccountOut,
    ServiceAccountRoleIn,
    ServiceAccountsOut,
    ServiceAccountTokenIn,
)
from daegam.auth.principal import Principal
from daegam.services.service_accounts import WHAT, ServiceAccountService
from daegam.store.personal_tokens import PersonalToken
from daegam.store.service_accounts import ServiceAccount

router = APIRouter(
    responses=ERROR_RESPONSES,
    tags=["service-accounts"],
    dependencies=[Depends(require_minting)],
)

MembersWrite = Annotated[Principal, Depends(require_scope("members:write", path_tenant=True))]
BASE = "/tenants/{tenant}/service-accounts"


def _service(request: Request) -> ServiceAccountService:
    return request.app.state.service_accounts


def _out(account: ServiceAccount, tokens: list[PersonalToken]) -> dict[str, Any]:
    return {
        "account_id": account.id,
        "tenant": account.tenant,
        "name": account.name,
        "role": account.role,
        "created_by": account.created_by,
        "created_by_name": account.created_by_name,
        "created_at": account.created_at,
        "tokens": [token_out(t) for t in tokens],
    }


async def _account(request: Request, tenant: str, account_id: str) -> ServiceAccount:
    found = await _service(request).get(account_id, check_tenant(tenant))
    return or_404(found, WHAT, account_id)


@router.get(BASE, response_model=ServiceAccountsOut)
async def list_service_accounts(
    request: Request, tenant: str, principal: MembersWrite, settings: Settings_
) -> dict[str, Any]:
    svc = _service(request)
    accounts = await svc.list(check_tenant(tenant))
    return {
        "mode": settings.service_accounts,
        "can_manage": svc.widening_refusal(principal) is None,
        "accounts": [_out(a, tokens) for a, tokens in accounts],
    }


@router.post(BASE, response_model=ServiceAccountOut, status_code=201)
async def create_service_account(
    request: Request, tenant: str, body: ServiceAccountIn, principal: MembersWrite
) -> dict[str, Any]:
    svc = _service(request)
    account = await svc.create(principal, check_tenant(tenant), name=body.name, role=body.role)
    audit_target(request, account.id, account.tenant)
    return _out(account, [])


@router.patch(f"{BASE}/{{account_id}}", response_model=ServiceAccountOut)
async def change_service_account_role(
    request: Request,
    tenant: str,
    account_id: str,
    body: ServiceAccountRoleIn,
    principal: MembersWrite,
) -> dict[str, Any]:
    svc = _service(request)
    account = await _account(request, tenant, account_id)
    changed = await svc.change_role(principal, account, body.role)
    return _out(changed, await svc.tokens_of(changed))


@router.delete(f"{BASE}/{{account_id}}", response_model=ServiceAccountDeleteOut)
async def delete_service_account(
    request: Request, tenant: str, account_id: str, principal: MembersWrite
) -> dict[str, Any]:
    account = await _account(request, tenant, account_id)
    await _service(request).delete(principal, account)
    return {"account_id": account.id, "deleted": True}


@router.post(f"{BASE}/{{account_id}}/tokens", response_model=IssuedPersonalToken, status_code=201)
async def issue_service_account_token(
    request: Request,
    tenant: str,
    account_id: str,
    body: ServiceAccountTokenIn,
    principal: MembersWrite,
    settings: Settings_,
) -> dict[str, Any]:
    account = await _account(request, tenant, account_id)
    token, plain = await _service(request).issue(
        principal,
        account,
        label=body.label,
        expires_at=body.expires_at,
        password=body.password,
        confirm_recent=lambda: require_recent_sign_in(request, settings, principal),
    )
    audit_target(request, token.id, token.tenant)
    return {**token_out(token), "token": plain}


@router.delete(f"{BASE}/{{account_id}}/tokens/{{token_id}}", response_model=TokenRevokeResponse)
async def revoke_service_account_token(
    request: Request, tenant: str, account_id: str, token_id: str, principal: MembersWrite
) -> dict[str, Any]:
    svc = _service(request)
    account = await _account(request, tenant, account_id)
    token = or_404(await svc.owned_token(account, token_id), "token", token_id)
    audit_target(request, token.id, token.tenant)
    await svc.revoke(principal, token)
    return {"token_id": token.id, "revoked": True}
