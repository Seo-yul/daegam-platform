"""턴 위임 토큰을 사람 경계에 묶는 Cedar (P53 3.1·3.2절).

local 모드의 vMCP 는 자기 Gateway 와 테넌트에 묶인 위임 토큰을 받는 permit 을 바탕 정책 곁에 두고,
그 밖의 위임 토큰(다른 대상, 다른 테넌트, `act` 만 있고 `bound_to` 가 없는 것)을 막는 forbid 를 맨
끝에 둔다. local 모드의 노출 앞단은 forbid 만 둔다. oidc·header 모드의 경계에는 둘 다 없다. 그곳에는
플랫폼 토큰이 닿지 않는다. 배포한 Gateway 의 렌더 끝에 forbid 가 붙는 것은 test_cedar_statements.py
가 본다.
"""

from __future__ import annotations

import asyncio
import dataclasses

from daegam.auth.cedar import (
    FRONT_TARGET,
    GATEWAY_TARGET,
    delegation_forbid,
    delegation_permit,
    group_policy,
    split_statements,
)
from daegam.k8s.templates.vmcp import vmcp_policies
from daegam.services.cedar_check import parse_statements
from daegam.services.data_plane_auth import consumer_auth
from daegam.store.tenants import TenantSettings
from tests.helpers.consts import DEFAULT_NS, GV, MS
from tests.helpers.exposure import cluster_with_ingress, prep_ingress
from tests.helpers.gateways import CALL_ONLY, cedar_policies
from tests.helpers.mcp_servers import clone_and_deploy


def test_the_forbid_refuses_every_delegation_token_not_bound_to_its_target_and_tenant():
    """forbid 는 `act` 나 `bound_to` 가 있는 토큰 가운데 이 대상과 이 테넌트에 묶인 것만 남긴다
    (F0 2번: 테넌트를 보지 않으면 permit-all 곁에서 다른 테넌트를 적은 토큰이 지나갔다)."""
    forbid = delegation_forbid("gateway:g-1", "t-1")
    assert forbid.startswith("forbid(principal, action, resource) when { ")
    assert "(principal has claim_act || principal has claim_bound_to) && !(" in forbid
    assert 'principal.claim_bound_to == "gateway:g-1"' in forbid
    assert 'principal.claim_tenant == "t-1"' in forbid
    permit = delegation_permit("gateway:g-1", "t-1")
    assert permit.startswith("permit(principal, action, resource) when { ")
    assert "claim_act" not in permit and 'principal.claim_tenant == "t-1"' in permit
    # 값은 Cedar 문자열로 이스케이프한다
    assert 'claim_bound_to == "gateway:\\"x"' in delegation_permit('gateway:"x', "t")


def test_the_rendered_permit_and_forbid_parse_as_one_cedar_statement_each():
    """렌더하는 permit 과 forbid 가 테넌트 조건(F0 뒤에 더한 것)까지 Cedar 엔진에서 파싱되고, 정책
    원소 하나가 문장 하나다. 저장 검사와 같은 하위 프로세스 길로 본다 (T63)."""
    for target in (GATEWAY_TARGET.format("g-1"), FRONT_TARGET.format("m-1"), 'gateway:"x\\'):
        pair = [delegation_permit(target, "t-1"), delegation_forbid(target, "t-1")]
        text = "\n".join(pair)
        assert split_statements(text) == pair
        asyncio.run(parse_statements(text))


def test_the_permit_sits_with_the_base_policies_and_an_override_replaces_it():
    permit, forbid = delegation_permit("gateway:g", "t"), delegation_forbid("gateway:g", "t")
    base = vmcp_policies(
        tenant_group="daegam-t",
        agent_client_ids=[],
        extra_policy=CALL_ONLY,
        forbids=[forbid],
        delegation=permit,
        client_id_claim="cid",
    )
    assert base == [group_policy("daegam-t"), permit, CALL_ONLY, forbid]


def test_only_local_mode_binds_delegation_tokens(offline_settings):
    """플랫폼이 사람 발급자인 local 모드만 경계를 묶는다. 모드는 판의 입력이라 바뀌면 다시
    렌더된다."""
    ts = TenantSettings(tenant="default")
    modes = {
        mode: consumer_auth(dataclasses.replace(offline_settings, **extra), ts).binds_delegation
        for mode, extra in (
            ("header", {"auth_mode": "header"}),
            ("local", {"auth_mode": "local", "platform_issuer": "http://p.test"}),
            (
                "oidc",
                {"auth_mode": "oidc", "oidc_issuer": "http://idp.test", "data_plane_issuer": "idp"},
            ),
        )
    }
    assert modes == {"header": False, "local": True, "oidc": False}


def test_a_local_exposure_front_forbids_every_delegation_token(local_admin, fake_clusters):
    """앞단에 묶인 토큰은 아무도 내지 않으므로 앞단은 모든 턴 토큰을 거절한다 (P53 3.2절)."""
    _cid, _bp, _ds, mp = cluster_with_ingress(local_admin)
    fake = fake_clusters.by_name["edge"]
    prep_ingress(fake)
    r = clone_and_deploy(
        local_admin, mp, name="calc-1", exposure={"enabled": True, "host": "calc.e2e.local"}
    )
    assert r.status_code == 201, r.text
    server = r.json()
    front = fake.get(GV, "VirtualMCPServer", f"front-{server['k8s']['name']}", DEFAULT_NS)
    policies = cedar_policies(front, fake=fake)
    assert policies[-1] == delegation_forbid(FRONT_TARGET.format(server["id"]), "default")
    assert local_admin.get(f"{MS}/{server['id']}").status_code == 200
