"""서버마다의 audience (P50 9절, G9 2단계).

내부 서버(MCP Server, image 허브 항목, image 복제본 기록)는 만들 때부터 자기 audience
`daegam-internal:<tenant>:<서버 id>` 로 검증한다. 옮겨 간 표시는 판의 첫 절반에 들어간다
(`own_audience_rev`).
"""

from __future__ import annotations

from types import SimpleNamespace

from daegam.auth.signer import audience_server_id, internal_audience, server_audience
from daegam.k8s.templates.common import INTERNAL_OIDC_CONFIG_NAME
from daegam.services import jwks_heal
from daegam.services.data_plane_auth import KIND_CLONE, KIND_MCP_SERVER, _Target


def test_an_auth_change_is_urgent_except_for_a_clone_record():
    """판의 첫 절반이 다르면 먼저 다시 렌더한다. 공유 복제본 기록의 첫 절반은 원본 정의의 판이라
    렌더 번호처럼 천천히 간다."""
    service = SimpleNamespace(auth_kind=KIND_MCP_SERVER)
    doc = SimpleNamespace(auth_rev="older", audience_switch=None, render_epoch=1)
    assert _Target(service, doc, "x", 1).urgent  # type: ignore[arg-type]
    clone = _Target(SimpleNamespace(auth_kind=KIND_CLONE), doc, "x", 1)  # type: ignore[arg-type]
    assert not clone.urgent


def test_the_jwks_probe_carries_the_audience_its_verifier_checks():
    """검증기 점검의 토큰은 그 검증기가 받는 audience 를 싣는다. 자기 audience 로 옮겨 간 MCPServer
    는 CR 의 audience 에서 서버 id 를 읽고, 테넌트 audience 로 검증하면 서버 id 가 없다."""
    assert audience_server_id("t", server_audience("t", "s1")) == "s1"
    assert audience_server_id("t", internal_audience("t")) is None
    assert audience_server_id("t", server_audience("u", "s1")) is None
    objs = [
        {
            "metadata": {"name": name, "namespace": "ns", "uid": name},
            "spec": {"oidcConfigRef": {"name": INTERNAL_OIDC_CONFIG_NAME, "audience": audience}},
            "status": {"url": f"http://mcp-{name}-proxy.ns.svc.cluster.local:8080/mcp"},
        }
        for name, audience in (
            ("own", server_audience("t", "s1")),
            ("tenant", internal_audience("t")),
        )
    ]
    found = jwks_heal.pick_verifiers(
        "MCPServer",
        ("spec", "oidcConfigRef", "name"),
        objs,
        frozenset({INTERNAL_OIDC_CONFIG_NAME}),
        {"ns": "t"},
    )
    assert {v.name: v.server_id for v in found} == {"own": "s1", "tenant": None}
