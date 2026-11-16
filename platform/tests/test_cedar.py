"""P6 F4a: vMCP Cedar 테넌트 기본 정책 스냅샷."""

from __future__ import annotations

import pytest

from daegam.auth.cedar import (
    claim_path,
    claim_values,
    forbid_action_policy,
    group_policy,
    holds_permit,
    named_groups,
    policy_code,
    reads_claim,
    tenant_policies,
)

GROUP = 'permit(principal in THVGroup::"daegam-acme", action, resource);'
CID = (
    "permit(principal, action, resource) when "
    '{ principal has claim_cid && principal.claim_cid == "agent-1" };'
)


def test_tenant_policies_dedupe_and_order():
    assert tenant_policies("daegam-acme", []) == [GROUP]
    assert tenant_policies("daegam-acme", ["agent-1", "", "agent-1", "agent-2"]) == [
        GROUP,
        CID,
        CID.replace("agent-1", "agent-2"),
    ]
    # 액션은 명시하지 않는다 (call_tool / tools/call 표기 미확정)
    assert "Action::" not in GROUP and "Action::" not in CID


def test_forbid_names_one_action_for_everyone():
    """Gateway 표면(P50 13절)이 끈 기능을 막는 정책. 액션 이름은 ToolHive 의 것이다."""
    assert (
        forbid_action_policy("read_resource")
        == 'forbid(principal, action == Action::"read_resource", resource);'
    )


def test_quotes_are_escaped():
    p = group_policy('we"ird\\name')
    assert p == 'permit(principal in THVGroup::"we\\"ird\\\\name", action, resource);'


def test_the_client_id_permit_reads_the_configured_claim():
    """oidc 모드의 IdP 는 client id 를 자기 클레임에 싣는다(Keycloak 은 azp). 기본값 cid 의
    렌더는 그대로다."""
    assert tenant_policies("daegam-acme", ["agent-1"], "cid") == [GROUP, CID]
    assert tenant_policies("daegam-acme", ["agent-1"], "azp") == [
        GROUP,
        "permit(principal, action, resource) when "
        '{ principal has claim_azp && principal.claim_azp == "agent-1" };',
    ]
    # 점 경로는 레코드를 따라 내려가고 마디마다 has 로 지킨다
    assert tenant_policies("daegam-acme", ["agent-1"], "client.id")[1] == (
        "permit(principal, action, resource) when { principal has claim_client && "
        'principal.claim_client has id && principal.claim_client.id == "agent-1" };'
    )


@pytest.mark.parametrize(
    "name", ["", "client-id", "a..b", "1st", 'x" || true || "y', "app.in", "app.__cedar"]
)
def test_a_claim_name_that_is_not_a_cedar_identifier_path_is_refused(name):
    with pytest.raises(ValueError):
        claim_path(name)


def test_policy_text_checks_skip_comments_and_unescape_names():
    """발급자 전환의 상태가 쓰는 글자 검사 (P53b 6.9절). 주석은 보지 않고, 글 하나의 열린 주석이
    다음 글을 가리지 않는다."""
    code = policy_code(
        [
            '// permit(principal in THVGroup::"commented", action, resource);\n'
            'permit(principal in THVGroup::"a\\"b", action, resource);',
            "/* unclosed",
            'forbid(principal in THVGroup::"daegam-t", action, resource);',
            None,
        ]
    )
    assert named_groups(code) == {'a"b', "daegam-t"}
    forbid_only = policy_code(["forbid(principal, action, resource);"])
    assert holds_permit(code) and not holds_permit(forbid_only)


def test_claim_reads_and_compared_values_follow_the_claim_path():
    code = policy_code(
        [
            'permit(principal, action, resource) when { principal.claim_azp == "bot" };',
            "permit(principal, action, resource) when { principal has claim_groups && "
            'principal.claim_groups.contains("eng") };',
            "permit(principal, action, resource) when { "
            'principal.claim_realm_access.roles.containsAny(["ops", "daegam-x"]) };',
            'permit(principal, action, resource) when { principal.claim_cidr == "x" };',
        ]
    )
    assert reads_claim(code, ["azp"]) and not reads_claim(code, ["cid"])
    assert reads_claim(code, ["realm_access.roles"]) and not reads_claim(code, ["roles"])
    assert claim_values(code, "groups") == {"eng"}
    assert claim_values(code, "realm_access.roles") == {"ops", "daegam-x"}
    assert claim_values(code, "azp") == {"bot"}
