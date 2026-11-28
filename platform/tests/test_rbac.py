"""P6 F2: 테넌트 소속·role·scope. oidc 모드 플랫폼(conftest.oidc_client) + mock OIDC 토큰."""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import pytest

from daegam.auth import rbac
from daegam.auth.principal import Membership, Principal, header_principal
from daegam.urls import (
    AGENT_CALL_ROUTES,
    AGENT_PROTOCOL_ROUTES,
    HOOK_ROUTE,
    SANDBOX_ROUTES,
    TEAMS_MESSAGES_ROUTE,
    TELEMETRY_EVENTS_PATH,
    TELEMETRY_OTLP_PATHS,
    TURN_GRANT_ROUTES,
)
from tests.helpers.auth import bearer
from tests.helpers.clusters import post_cluster
from tests.helpers.consts import GROUP_MAPPINGS
from tests.helpers.docs import INSTANCE_KINDS, InstanceKind, insert_docs
from tests.helpers.http import detail_code, make_app


def memberships(mongo, sub: str) -> dict[str, tuple[str, str]]:
    return {
        d["tenant"]: (d["role"], d["source"])
        for d in mongo["memberships"].find({"sub": sub}, {"_id": 0})
    }


# -- role → scope 표 ----------------------------------------------------------------


def test_role_scope_table():
    reads = {
        "backend:read",
        "mcp:read",
        "hub:read",
        "gateway:read",
        "agent:read",
        "knowledge:read",
        "skill:read",
        "cluster:read",
        "settings:read",
    }
    # viewer 는 읽기에 더해 agent:chat 하나를 갖는다. 홈 챗에서 배포된 에이전트와 대화하고 자기
    # 세션의 결정을 하게 하려는 것이다 (D35). 배포·남의 세션 승인은 그대로 developer 위다
    assert rbac.scopes_for_role("viewer") == reads | {"agent:chat"}
    dev = rbac.scopes_for_role("viewer") | {
        "backend:write",
        "mcp:write",
        "mcp:deploy",
        "hub:write",
        "gateway:write",
        "gateway:deploy",
        "agent:write",
        "agent:deploy",
        "agent:approve",
        "knowledge:write",
        "skill:write",
        "audit:read",
    }
    assert rbac.scopes_for_role("developer") == dev
    # 사용량과 사람별 비용은 tenant-admin 부터다 (P37 14절)
    assert rbac.scopes_for_role("tenant-admin") == dev | {"members:write", "usage:read"}
    assert rbac.scopes_for_role("platform-admin") == rbac.SCOPES
    # platform-admin 전용
    for s in ("cluster:write", "hub:share", "skill:share", "settings:write"):
        assert s in rbac.SCOPES
        for r in ("tenant-admin", "developer", "viewer"):
            assert s not in rbac.scopes_for_role(r)
    assert "audit:read" not in rbac.scopes_for_role("viewer")
    for s in ("agent:deploy", "agent:approve", "agent:write"):
        assert s not in rbac.scopes_for_role("viewer")
    assert "members:write" not in rbac.scopes_for_role("developer")
    assert "usage:read" not in rbac.scopes_for_role("developer")
    assert rbac.scopes_for_role("nope") == frozenset()
    assert rbac.highest_role(["viewer", "tenant-admin", "developer"]) == "tenant-admin"
    assert rbac.role_at_least("developer", "viewer") and not rbac.role_at_least(
        "viewer", "developer"
    )


def test_principal_helpers():
    p = Principal(sub="u", memberships=[Membership("a", "developer"), Membership("b", "viewer")])
    assert p.tenants == ["a", "b"] and not p.is_platform_admin
    assert p.is_member("a") and not p.is_member("zzz")
    assert "mcp:deploy" in p.scopes_in("a") and "mcp:deploy" not in p.scopes_in("b")
    assert "cluster:read" in p.all_scopes and "cluster:write" not in p.all_scopes
    admin = header_principal()
    assert admin.is_platform_admin and admin.is_member("anything") and admin.tenants == []
    assert admin.scopes_in("x") == rbac.SCOPES
    with pytest.raises(ValueError):
        from daegam.api.deps import require_scope

        require_scope("nope:read")


# -- viewer 쓰기 403 --------------------------------------------------------------


def test_viewer_cannot_write(oidc, token):
    v = bearer(token("viewer1", "daegam-a-view"))
    assert oidc.get("/backend-apis", headers=v).status_code == 200
    assert oidc.get("/mcp-servers", headers=v).status_code == 200
    assert oidc.get("/clusters", headers=v).status_code == 200
    assert oidc.get("/settings", headers=v).status_code == 200
    assert oidc.get("/tenants/a/settings", headers=v).status_code == 200
    r = oidc.post("/mcp-servers/p/deploy", headers=v, json={})
    assert r.status_code == 403, r.text
    assert r.json()["detail"] == {
        "code": "forbidden",
        "detail": "scope mcp:deploy required",
        "scope": "mcp:deploy",
        "tenant": "a",
    }
    # 만들기·저장·복제·삭제는 mcp:write 다
    r = oidc.post("/mcp-servers", headers=v, json={"name": "x"})
    assert r.status_code == 403 and r.json()["detail"]["scope"] == "mcp:write"
    r = oidc.delete("/mcp-servers/nope", headers=v)
    assert r.status_code == 403 and r.json()["detail"]["scope"] == "mcp:write"
    r = oidc.post("/mcp-servers/nope/clone", headers=v, json={"name": "y"})
    assert r.status_code == 403 and r.json()["detail"]["scope"] == "mcp:write"
    # Advanced 편집·Basic 편집·드리프트·재배포·내리기는 클러스터에 쓴다. 상세 화면이 이 버튼을
    # 권한으로 감추지 않으므로 라우터가 유일한 방어다
    r = oidc.put("/mcp-servers/nope/overrides", headers=v, json={"patch": {}})
    assert r.status_code == 403 and r.json()["detail"]["scope"] == "mcp:deploy"
    r = oidc.patch("/mcp-servers/nope", headers=v, json={"replicas": 2})
    assert r.status_code == 403 and r.json()["detail"]["scope"] == "mcp:deploy"
    for path in (
        "/mcp-servers/nope/drift/reset",
        "/mcp-servers/nope/redeploy",
        "/mcp-servers/nope/undeploy",
    ):
        r = oidc.post(path, headers=v, json={})
        assert r.status_code == 403 and r.json()["detail"]["scope"] == "mcp:deploy", path
    r = oidc.post("/backend-apis", headers=v, files={"file": ("x.yaml", b"openapi: 3")})
    assert r.status_code == 403 and r.json()["detail"]["scope"] == "backend:write"
    # 스펙 다듬기·오버라이드 제안 job 과 검증·변환은 테넌트 예산의 LLM 을 태운다
    r = oidc.post("/spec-generator/jobs", headers=v, json={"mode": "generate", "provider": "p"})
    assert r.status_code == 403 and r.json()["detail"]["scope"] == "backend:write"
    r = oidc.delete("/spec-generator/jobs/nope", headers=v)
    assert r.status_code == 403 and r.json()["detail"]["scope"] == "backend:write"
    for path in ("/backend-apis/verify", "/backend-apis/convert"):
        r = oidc.post(path, headers=v, files={"file": ("x.yaml", b"openapi: 3")})
        assert r.status_code == 403 and r.json()["detail"]["scope"] == "backend:write", path
    r = oidc.post("/data-services", headers=v, json={})
    assert r.status_code == 403 and r.json()["detail"]["scope"] == "mcp:write"
    r = oidc.patch("/tenants/a/settings", headers=v, json={"quota": {"pods": "1"}})
    assert r.status_code == 403 and r.json()["detail"]["scope"] == "members:write"
    r = oidc.patch("/settings", headers=v, json={"default_model": "m"})
    assert r.status_code == 403 and r.json()["detail"]["scope"] == "settings:write"
    r = oidc.post("/clusters", headers=v, json={})
    assert r.status_code == 403 and r.json()["detail"]["scope"] == "cluster:write"
    assert oidc.get("/tenants/a/members", headers=v).status_code == 403
    assert oidc.get("/group-mappings", headers=v).status_code == 403
    # 허브·Gateway 라우터 (D23): viewer 는 GET 만
    assert oidc.get("/hub", headers=v).status_code == 200
    assert oidc.get("/gateways", headers=v).status_code == 200
    r = oidc.post("/hub", headers=v, json={"name": "x", "kind": "entry"})
    assert r.status_code == 403 and r.json()["detail"]["scope"] == "hub:write"
    r = oidc.post("/gateways", headers=v, json={"name": "x"})
    assert r.status_code == 403 and r.json()["detail"]["scope"] == "gateway:write"
    r = oidc.post("/gateways/nope/deploy", headers=v, json={})
    assert r.status_code == 403 and r.json()["detail"]["scope"] == "gateway:deploy"
    # DELETE 는 write 로 열리고 배포된 것이면 핸들러가 deploy 를 한 번 더 본다
    # (test_deleting_a_deployed_resource_needs_deploy_scope)
    r = oidc.delete("/gateways/nope", headers=v)
    assert r.status_code == 403 and r.json()["detail"]["scope"] == "gateway:write"
    r = oidc.post("/gateways/nope/publish", headers=v, json={})
    assert r.status_code == 403 and r.json()["detail"]["scope"] == "gateway:deploy"
    r = oidc.post("/mcp-servers/nope/publish", headers=v, json={})
    assert r.status_code == 403 and r.json()["detail"]["scope"] == "hub:write"
    # Agent 라우터 (D26, D42: 정의와 배포가 한 리소스다): viewer 는 GET 만
    assert oidc.get("/agents", headers=v).status_code == 200
    assert oidc.get("/agents/templates", headers=v).status_code == 200
    assert oidc.get("/agents/summary", headers=v).status_code == 200
    r = oidc.post("/agents", headers=v, json={"name": "x"})
    assert r.status_code == 403 and r.json()["detail"]["scope"] == "agent:write"
    r = oidc.post("/agents/validate", headers=v, json={})
    assert r.status_code == 403 and r.json()["detail"]["scope"] == "agent:write"
    r = oidc.post("/agents/nope/deploy", headers=v, json={})
    assert r.status_code == 403 and r.json()["detail"]["scope"] == "agent:deploy"
    # 지금 재우기는 배포 scope 이고 깨우기는 채팅 scope 다 (P44 14절)
    r = oidc.post("/agents/nope/sleep", headers=v)
    assert r.status_code == 403 and r.json()["detail"]["scope"] == "agent:deploy"
    assert oidc.post("/agents/nope/wake", headers=v).status_code == 404
    # DELETE 는 write 로 열리고 배포된 것이면 핸들러가 deploy 를 한 번 더 본다
    # (test_deleting_a_deployed_resource_needs_deploy_scope)
    r = oidc.delete("/agents/nope", headers=v)
    assert r.status_code == 403 and r.json()["detail"]["scope"] == "agent:write"
    # 다만 agent:chat 은 viewer 도 갖는다 (D35). scope 를 지나 없는 문서에서 404 로 걸린다
    r = oidc.post("/agents/nope/chat", headers=v, json={"message": "hi"})
    assert r.status_code == 404, r.text
    r = oidc.post("/agents/nope/sessions/s1/decisions", headers=v, json={"decisions": [{}]})
    assert r.status_code == 404, r.text
    # 중지도 채팅과 같은 scope 다(D65). 소유자 판정은 런타임이 한다
    r = oidc.post("/agents/nope/sessions/s1/cancel", headers=v)
    assert r.status_code == 404, r.text
    # 첨부 업로드와 지우기(P36)도 채팅과 같은 scope 다
    note = {"file": ("a.txt", b"a", "text/plain")}
    assert oidc.post("/agents/nope/uploads", headers=v, files=note).status_code == 404
    assert oidc.delete("/agents/nope/uploads/x", headers=v).status_code == 404
    # 세션 파일의 목록과 내려받기(P36)는 세션 상세와 같은 agent:read 이고 소유자 판정은 런타임이
    # 한다
    assert oidc.get("/agents/nope/sessions/s1/files", headers=v).status_code == 404
    assert oidc.get("/agents/nope/sessions/s1/files/a.md", headers=v).status_code == 404


def test_long_term_memory_is_deleted_by_its_owner_or_a_tenant_admin(oidc, token):
    """D34. 대상이 경로에 있으므로 감사에 무엇을 지웠는지 남는다. 문서가 없어 404 로 끝난다."""
    v = bearer(token("viewer1", "daegam-a-view"))
    r = oidc.delete("/agents/nope/memories/someone-else", headers=v)
    assert detail_code(r) == (403, "not_memory_owner")
    # 자기 것은 viewer 도 지운다 (기억을 남기는 것이 대화이기 때문이다)
    assert oidc.delete("/agents/nope/memories/viewer1", headers=v).status_code == 404
    # tenant admin 은 남의 것도 지운다
    ta = bearer(token("admin-a", "daegam-a-admin"))
    assert oidc.delete("/agents/nope/memories/viewer1", headers=ta).status_code == 404
    # developer 는 자기 것만이다
    d = bearer(token("dev1", "daegam-a-dev"))
    r = oidc.delete("/agents/nope/memories/viewer1", headers=d)
    assert detail_code(r) == (403, "not_memory_owner")
    # 내 기억 보기와 하나 지우기(P36)도 agent:chat 이다. viewer 가 scope 를 지나 없는 문서에서
    # 404 다
    assert oidc.get("/agents/nope/memories/me", headers=v).status_code == 404
    assert oidc.get("/agents/nope/memories/me/files/a.md", headers=v).status_code == 404
    assert oidc.delete("/agents/nope/memories/me/files/a.md", headers=v).status_code == 404


# -- 테넌트 결정 -------------------------------------------------------------------


def test_other_tenant_is_403_not_empty_list(oidc, token, mongo):
    a = token("dev1", "daegam-a-dev")
    assert oidc.get("/backend-apis", headers=bearer(a)).status_code == 200  # 소속 하나 → 자동
    assert oidc.get("/backend-apis", headers=bearer(a, tenant="a")).status_code == 200
    r = oidc.get("/backend-apis", headers=bearer(a, tenant="b"))
    assert r.status_code == 403, r.text
    assert r.json()["detail"] == {
        "code": "not_member",
        "detail": "not a member of tenant b",
        "tenant": "b",
    }
    for path in ("/mcp-servers", "/data-services"):
        assert oidc.get(path, headers=bearer(a, tenant="b")).status_code == 403, path
    # 잘못된 테넌트 이름은 400
    assert oidc.get("/backend-apis", headers=bearer(a, tenant="bad name")).status_code == 400
    # 소속이 전혀 없으면 403
    r = oidc.get("/backend-apis", headers=bearer(token("nobody")))
    assert detail_code(r) == (403, "not_member")
    assert oidc.get("/backend-apis", headers=bearer(token("nobody"), tenant="a")).status_code == 403


def test_no_header_with_multiple_memberships_is_400(oidc, token):
    both = token("dev2", "daegam-a-dev", "daegam-b-dev")
    r = oidc.get("/backend-apis", headers=bearer(both))
    assert r.status_code == 400, r.text
    assert r.json()["detail"]["code"] == "tenant_required"
    assert r.json()["detail"]["tenants"] == ["a", "b"]
    assert oidc.get("/backend-apis", headers=bearer(both, tenant="a")).status_code == 200
    assert oidc.get("/backend-apis", headers=bearer(both, tenant="b")).status_code == 200
    assert oidc.get("/backend-apis", headers=bearer(both, tenant="c")).status_code == 403


# -- 그룹 동기화 -------------------------------------------------------------------


def test_group_sync_add_remove_and_highest_role(oidc, token, mongo):
    sub = "sync1"
    assert oidc.get("/clusters", headers=bearer(token(sub, "daegam-a-dev"))).status_code == 200
    assert memberships(mongo, sub) == {"a": ("developer", "okta_group")}
    # 그룹 둘 → 높은 role
    oidc.get("/clusters", headers=bearer(token(sub, "daegam-a-dev", "daegam-a-admin")))
    assert memberships(mongo, sub) == {"a": ("tenant-admin", "okta_group")}
    # a 에서 빠지고 b 로
    oidc.get("/clusters", headers=bearer(token(sub, "daegam-b-dev")))
    assert memberships(mongo, sub) == {"b": ("developer", "okta_group")}
    # 매핑 없는 그룹은 무시, 그룹이 비면 okta_group 소속 전부 삭제
    oidc.get("/clusters", headers=bearer(token(sub, "unmapped")))
    assert memberships(mongo, sub) == {}
    # platform-admin 그룹은 tenant "*"
    oidc.get("/clusters", headers=bearer(token(sub, "daegam-admins")))
    assert memberships(mongo, sub) == {"*": ("platform-admin", "okta_group")}


def test_manual_membership_survives_group_sync(oidc, token, mongo):
    ta = bearer(token("admin-a", "daegam-a-admin"))
    r = oidc.put("/tenants/a/members", headers=ta, json=[{"sub": "m1", "role": "viewer"}])
    assert r.status_code == 200, r.text
    assert [(m["sub"], m["role"], m["source"]) for m in r.json()] == [
        ("admin-a", "tenant-admin", "okta_group"),
        ("m1", "viewer", "manual"),
    ]
    # 그룹이 developer 를 줘도 manual viewer 가 이긴다
    m1_dev = token("m1", "daegam-a-dev")
    r = oidc.post("/mcp-servers/p/deploy", headers=bearer(m1_dev), json={})
    assert r.status_code == 403
    assert memberships(mongo, "m1") == {"a": ("viewer", "manual")}
    # 그룹에서 빠져도 manual 은 남는다
    assert oidc.get("/backend-apis", headers=bearer(token("m1"))).status_code == 200
    assert memberships(mongo, "m1") == {"a": ("viewer", "manual")}
    # 다른 테넌트 그룹은 okta_group 으로 따로 붙는다
    oidc.get("/clusters", headers=bearer(token("m1", "daegam-b-dev")))
    assert memberships(mongo, "m1") == {"a": ("viewer", "manual"), "b": ("developer", "okta_group")}
    # PUT 으로 manual 을 비우면 okta_group 은 그대로
    r = oidc.put("/tenants/a/members", headers=ta, json=[])
    assert [m["sub"] for m in r.json()] == ["admin-a"]
    assert memberships(mongo, "m1") == {"b": ("developer", "okta_group")}
    assert oidc.get("/backend-apis", headers=bearer(token("m1"), tenant="a")).status_code == 403


def test_members_api_validation(oidc, token):
    ta = bearer(token("admin-a", "daegam-a-admin"))
    # 다른 테넌트의 설정·멤버는 tenant-admin 이라도 403
    assert oidc.get("/tenants/b/settings", headers=ta).status_code == 403
    assert oidc.get("/tenants/b/members", headers=ta).status_code == 403
    r = oidc.put("/tenants/a/members", headers=ta, json=[{"sub": "x", "role": "platform-admin"}])
    assert detail_code(r) == (422, "invalid_role")
    r = oidc.put(
        "/tenants/a/members",
        headers=ta,
        json=[{"sub": "x", "role": "viewer"}, {"sub": "x", "role": "developer"}],
    )
    assert detail_code(r) == (422, "duplicate_sub")
    assert oidc.put("/tenants/a/members", headers=ta, json=[{"sub": ""}]).status_code == 422
    # manual 로 승격: okta_group 소속이 manual 로 바뀐다
    r = oidc.put("/tenants/a/members", headers=ta, json=[{"sub": "admin-a", "role": "developer"}])
    assert r.json() == [{**r.json()[0], "sub": "admin-a", "role": "developer", "source": "manual"}]


def test_group_mappings_api(oidc, token, mongo):
    pa = bearer(token("root", "daegam-admins"))
    got = oidc.get("/group-mappings", headers=pa).json()
    assert {(m["group"], m["tenant"], m["role"]) for m in got} == {
        (m["group"], m["tenant"], m["role"]) for m in GROUP_MAPPINGS
    }
    bad = [
        [{"group": "g", "tenant": "a", "role": "platform-admin"}],
        [{"group": "g", "tenant": "*", "role": "developer"}],
        [{"group": "g", "tenant": "a", "role": "owner"}],
        [
            {"group": "g", "tenant": "a", "role": "viewer"},
            {"group": "g", "tenant": "a", "role": "viewer"},
        ],
    ]
    for body in bad:
        assert oidc.put("/group-mappings", headers=pa, json=body).status_code == 422, body
    assert (
        oidc.put(
            "/group-mappings",
            headers=pa,
            json=[{"group": "g", "tenant": "bad name", "role": "viewer"}],
        ).status_code
        == 400
    )
    new = [
        {"group": "daegam-admins", "tenant": "*", "role": "platform-admin"},
        {"group": "eng", "tenant": "c", "role": "developer"},
    ]
    r = oidc.put("/group-mappings", headers=pa, json=new)
    assert r.status_code == 200 and r.json() == sorted(new, key=lambda m: m["group"])
    # 새 매핑이 바로 반영된다. 예전 매핑은 사라졌다
    assert oidc.get("/backend-apis", headers=bearer(token("e1", "eng"))).status_code == 200
    assert memberships(mongo, "e1") == {"c": ("developer", "okta_group")}
    assert oidc.get("/backend-apis", headers=bearer(token("e2", "daegam-a-dev"))).status_code == 403


# -- 라우트 표와 거절 기록 ----------------------------------------------------------


def test_data_plane_auth_status_is_for_platform_admins(oidc, token):
    """인증 재적용의 상태는 모든 테넌트의 막힌 소비자와 클러스터 상태를 싣는다. settings:write 라
    viewer 와 tenant-admin 은 403 이고 platform-admin 만 부른다 (P32 8절)."""
    for tok in (token("viewer1", "daegam-a-view"), token("admin-a", "daegam-a-admin")):
        r = oidc.get("/settings/data-plane-auth", headers=bearer(tok, tenant="a"))
        assert r.status_code == 403 and r.json()["detail"]["scope"] == "settings:write", r.text
    r = oidc.get("/settings/data-plane-auth", headers=bearer(token("root", "daegam-admins")))
    assert r.status_code == 200 and r.json()["install_id"], r.text


def test_every_write_route_declares_a_scope(offline_settings, fake_clusters, fake_secrets):
    """GET 아닌 라우트는 전부 scope 의존성을 가진다.

    라우트를 하나씩 짚는 위 테스트는 새로 생긴 라우트를 놓친다. 실제로 MCP Instance 의 편집 셋이
    Gateway·Agent 의 같은 라우트가 막혀 있는 동안 검사 없이 열려 있었다. 표를 통째로 돌아 빠진 것이
    생기는 순간 여기서 걸리게 한다. 비서 라우터만 예외다. 그쪽은 호출자의 principal 을 그대로 물고
    같은 앱을 다시 부르므로 대상 라우터의 검사가 걸린다
    (`services/assistant/tools.py` 의 via_app)."""
    app = make_app(offline_settings, fake_clusters, fake_secrets)

    def api_routes(routes: Any) -> Iterator[Any]:
        """include_router 로 붙인 라우터는 이 FastAPI 판에서 `_IncludedRouter` 래퍼로 남는다.
        래퍼 자신에는 path 도 methods 도 없으므로 `original_router.routes` 로 한 겹 들어가야 실제
        라우트가 나온다. 평면으로 훑으면 빈 목록을 돌게 되고, 그러면 이 검사가 언제나 통과한다."""
        for r in routes:
            inner = getattr(r, "original_router", None)
            if inner is not None:
                yield from api_routes(inner.routes)
            elif getattr(r, "methods", None):
                yield r

    writes = [r for r in api_routes(app.routes) if set(r.methods) - {"GET", "HEAD", "OPTIONS"}]
    # 빈 목록을 도는 검사는 있으나 마나다. 표를 실제로 잡았는지 먼저 못 박는다
    assert len(writes) > 50, f"쓰기 라우트를 {len(writes)}개만 찾았다. 표를 잘못 돌고 있다"

    # local 모드의 자기 계정 라우트 (D40). 로그인은 정의상 익명이고, 나머지는 로그인한 사람이
    # 자기 것을 부른다(require_user 만). 개인 MCP 토큰의 발급·회수도 요청한 사람 자신의 토큰만
    # 다루고 테넌트는 소속 존재로 본다 (P32 6절). OAuth 인가 서버의 인증 없는 쓰기(DCR 등록, 토큰,
    # 회수)는 로그인처럼 정의상 익명이고 코드·갱신 토큰과 client_id 가 자격이다. 동의 셋은 로그인한
    # 사람이 자기 이름으로 허용·거절하고 허용은 역할이 아니라 소속으로 가른다. 내 연결 끊기는 요청한
    # 사람 자신의 연결만 다룬다 (P33 1절·7절·12절). 나를 대신하는 일정과 트리거 멈추기는 지금
    # role 과 상관없이 나에게 묶인 것만 멈춘다 (P41 2절). 기억한 승인 선택 지우기도 내 것만 지운다
    # (P43 5절). Teams 연결과 그 코드도 내 것만 다룬다 (P47 6.3절). 이것들은 scope 로 가르는 자리가
    # 아니다
    self_service = {
        "/auth/login",
        "/auth/token",
        "/auth/password",
        "/auth/logout",
        "/me/tokens",
        "/me/tokens/{token_id}",
        "/oauth/register",
        "/oauth/token",
        "/oauth/revoke",
        "/me/oauth-grants/{grant_id}",
        "/oauth/consent/inspect",
        "/oauth/consent/approve",
        "/oauth/consent/deny",
        "/me/runs-as-me/schedules/{schedule_id}/pause",
        "/me/runs-as-me/triggers/{trigger_id}/pause",
        "/me/approval-choices/{choice_id}",
        "/me/channels/teams",
        "/me/channels/teams/link-code",
        "/me/channels/teams/confirm",
    }
    # 기계의 쓰기다. 사람의 scope 가 아니라 토큰이 자격이고 그 토큰이 인스턴스나 클러스터를
    # 정한다(런타임 이벤트 수집과 게이트웨이 사용량 수집, P37 3절·8절, 턴 위임의 갱신과 끝, P53
    # 4절). 웹훅 호출은 트리거의 서명이 자격이다 (P41 4절). 샌드박스의 열기·쓰기 표시·닫기도
    # 인스턴스 토큰이 자격이다 (P40 5절). Teams 의 활동은 Bot Connector 의 토큰이 자격이다
    # (P47 6.2절). Agent 호출과 맡긴 작업은 부르는 턴의 호출 토큰이나 header 모드의
    # 인스턴스 토큰이 자격이다 (P46 6.3·6.7절)
    machine = {
        *AGENT_CALL_ROUTES.values(),
        *AGENT_PROTOCOL_ROUTES.values(),
        TELEMETRY_EVENTS_PATH,
        *TELEMETRY_OTLP_PATHS.values(),
        *TURN_GRANT_ROUTES.values(),
        HOOK_ROUTE,
        *SANDBOX_ROUTES.values(),
        TEAMS_MESSAGES_ROUTE,
    }
    missing: list[str] = []
    for route in writes:
        if route.path.startswith("/assistant") or route.path in self_service | machine:
            continue
        names = " ".join(getattr(d.call, "__qualname__", "") for d in route.dependant.dependencies)
        # scope 하나를 요구하거나, 플랫폼 관리자만 부를 수 있으면 된다 (그룹 매핑이 후자다)
        if "require_scope" not in names and "require_platform_admin" not in names:
            missing.append(f"{sorted(set(route.methods) - {'HEAD', 'OPTIONS'})[0]} {route.path}")
    assert sorted(missing) == []


def test_viewer_sees_clusters_without_connection_details(oidc, token):
    """cluster:read 는 viewer 도 갖는다. 인스턴스를 어느 클러스터에 둘지 고르는 데는 이름과 상태만
    있으면 되고, API 서버 주소·kubecontext·Secret 이름은 클러스터를 고치는 사람만 본다."""
    admin = bearer(token("root", "daegam-admins"))
    r = post_cluster(oidc, "edge-view", headers=admin)
    assert r.status_code == 201, r.text
    cid = r.json()["id"]
    assert r.json()["connection"]["secret_name"], "관리자는 접속 정보를 본다"
    viewer = bearer(token("viewer1", "daegam-a-view"))
    for path in ("/clusters", f"/clusters/{cid}"):
        r = oidc.get(path, headers=viewer)
        assert r.status_code == 200, (path, r.text)
        items = r.json() if isinstance(r.json(), list) else [r.json()]
        for c in items:
            conn = c["connection"]
            assert conn["mode"] and conn["secret_name"] is None
            assert conn["api_url"] is None and conn["kubecontext"] is None
    full = oidc.get(f"/clusters/{cid}", headers=admin).json()["connection"]
    assert full["secret_name"] and full["mode"] == "kubeconfig"


def test_denied_reads_are_audited(oidc, token, mongo):
    """쓰기는 늘 남고 읽기는 거절만 남는다. 누가 무엇을 열어 보려다 막혔는지가 보안 기록이다."""
    mongo["audit_events"].delete_many({})
    viewer = bearer(token("viewer1", "daegam-a-view"))
    assert oidc.get("/clusters", headers=viewer).status_code == 200
    assert oidc.get("/group-mappings", headers=viewer).status_code == 403
    assert oidc.get("/clusters").status_code == 401
    events = list(mongo["audit_events"].find({"method": "GET"}, {"_id": 0}))
    by_path = {(e["path"], e["status"]) for e in events}
    assert ("/api/group-mappings", 403) in by_path
    assert ("/api/clusters", 401) in by_path
    # 성공한 읽기는 남지 않는다
    assert ("/api/clusters", 200) not in by_path
    denied = next(e for e in events if e["status"] == 403)
    assert denied["sub"] == "viewer1" and denied["result"] == "error"


# -- 배포된 것 지우기 ------------------------------------------------------------------------------

# 배포한 것을 지울 수 있는 리소스 셋. tests/helpers/docs.py 의 표다
DELETABLE = [pytest.param(kind, id=kind.name) for kind in INSTANCE_KINDS.values()]


@pytest.mark.parametrize("kind", DELETABLE)
def test_deleting_a_deployed_resource_needs_deploy_scope(
    oidc, token, mongo, monkeypatch, kind: InstanceKind
):
    """DELETE 는 <scope>:write 로 열리고, 배포 쪽이 있으면 핸들러가 <scope>:deploy 를 한 번 더
    본다(정의만 지우는 것과 클러스터의 것을 지우는 것은 권한이 다르다). 기본 역할에는 write 만
    있고 deploy 가 없는 조합이 없으므로 viewer 에 write 를 얹어 만든다."""
    deploy = f"{kind.scope}:deploy"
    viewer = rbac.ROLE_SCOPES[rbac.VIEWER]
    monkeypatch.setitem(rbac.ROLE_SCOPES, rbac.VIEWER, viewer | {f"{kind.scope}:write"})
    live = kind.build("x1", tenant="a", state="deployed", cluster="gone")
    insert_docs(mongo, **{kind.collection: [kind.build("d1", tenant="a"), live]})
    w = bearer(token("writer1", "daegam-a-view"))
    r = oidc.delete(f"{kind.prefix}/x1", headers=w)
    assert r.status_code == 403, r.text
    assert r.json()["detail"] == {
        "code": "forbidden",
        "detail": f"scope {deploy} required",
        "scope": deploy,
        "tenant": "a",
    }
    assert mongo[kind.collection].find_one({"_id": "x1"})["state"] == "deployed"
    # draft 는 write 만으로 지운다
    r = oidc.delete(f"{kind.prefix}/d1", headers=w)
    assert r.status_code == 200, r.text
    assert r.json()["state_before"] == "draft"
    # deploy 를 가진 developer 는 배포된 것도 지운다 (클러스터가 사라진 뒤의 정리 경로)
    r = oidc.delete(f"{kind.prefix}/x1", headers=bearer(token("dev1", "daegam-a-dev")))
    assert r.status_code == 200, r.text
    assert r.json()["state_before"] == "deployed"
    assert mongo[kind.collection].count_documents({}) == 0
