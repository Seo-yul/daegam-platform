"""P56 F2: code mode 의 렌더 (6.3·6.4·6.6절). 판정이 받아들이면 vMCP 가 `codeMode` 와 고정한 필터,
맨 뒤의 힌트 forbid 를 싣고, 표시가 쓰기로 바뀌거나 ToolHive 가 낮으면 다시 렌더가 그것을 빼고
`code_mode_closed` 로 알린다. 정책 설정이 vMCP 보다 먼저 적용되므로, 닫을 때는 code mode 를 든
vMCP 가 도는 동안 힌트 forbid 가 남고 vMCP 적용이 성공한 뒤에 빠진다. 힌트 forbid 는 P52 의 토큰
꼴로 오류 없이 평가된다."""

from __future__ import annotations

from datetime import timedelta

import pytest

import daegam.services.authz_configs as authz_configs
import daegam.services.gateways as gateways_module
from daegam.auth.cedar import READ_ONLY_HINT_FORBID
from daegam.k8s.templates.vmcp import CODE_MODE_TOOL
from daegam.services.gateway_code_mode import CODE_MODE_CLOSED, CODE_MODE_TOOLS_HIDDEN
from daegam.services.gateway_timeouts import SCRIPT_MINUTE_SECONDS, call_timeout
from daegam.store.base import utcnow
from daegam.store.gateways import Gateway
from daegam.store.hour_marks import hour_of
from tests.helpers.cedar_eval import evaluate
from tests.helpers.clusters import record_toolhive
from tests.helpers.consts import DEFAULT_NS, GV, GW
from tests.helpers.docs import cluster_doc
from tests.helpers.gateways import backend_snapshot, cedar_policies, create_gateway, vmcp_of
from tests.helpers.mcp_metrics import mcp_call_event
from tests.helpers.workers import run_data_plane_auth
from tests.test_gateway_code_mode_api import _env, _mark
from tests.test_mcp_metrics_api import _Clusters, _gateway, _reports
from tests.test_rule_cedar import ACT_A, agent_own, delegated, person

PERMIT = "permit(principal, action, resource);"
TOKENS = [
    person("developer"),
    agent_own(),
    delegated(ACT_A),
    {"sub": "svc:platform", "tenant": "acme", "groups": ["daegam-acme"]},
]
# vMCP 적용을 거절하는 가짜 클러스터의 (group, resource, verb)
VMCP_APPLY = (GV.split("/")[0], "virtualmcpservers", "apply")


# -- 힌트 forbid (6.3절 둘째 층) -------------------------------------------------------------


@pytest.mark.parametrize("claims", TOKENS, ids=["person", "agent", "delegated", "platform"])
def test_the_hint_forbid_opens_only_tools_that_report_read_only(claims) -> None:
    policies = [PERMIT, READ_ONLY_HINT_FORBID]
    cases = {True: True, False: False, None: False}
    for hint, expected in cases.items():
        attrs = {} if hint is None else {"readOnlyHint": hint}
        verdict = evaluate(policies, claims, "get_order", tool_attrs=attrs)
        assert verdict.errors == ()
        assert verdict.allowed is expected, (claims["sub"], hint)


def test_the_call_timeout_follows_the_longest_backend_up_to_the_minute() -> None:
    short = backend_snapshot("a", "internal", ["x"], timeout_seconds=20)
    long = backend_snapshot("b", "internal", ["y"], timeout_seconds=45)
    assert call_timeout([short, long]) == "45s"
    slow = backend_snapshot("c", "internal", ["z"], timeout_seconds=300)
    assert call_timeout([short, slow]) == f"{SCRIPT_MINUTE_SECONDS}s"


# -- 렌더 ---------------------------------------------------------------------------------


def _deploy(client, mongo, cid, items, calc, *, code_mode=True, tools=("Add",)):
    record_toolhive(mongo, cid, "0.53.0")
    backends = [{"hub_item_id": calc, "tools": list(tools)}]
    r = create_gateway(client, items, backends=backends, code_mode=code_mode)
    assert r.status_code == 201, r.text
    d = client.post(f"{GW}/{r.json()['id']}/deploy", json={})
    assert d.status_code == 201, d.text
    return d.json()


def _vmcp(fake_clusters) -> dict:
    return vmcp_of(fake_clusters.by_name["edge"], "gw-a")


def _rendered(fake_clusters) -> tuple[bool, bool]:
    """(지금 도는 vMCP 가 code mode 를 실었는가, 그 정책에 힌트 forbid 가 있는가)."""
    fake = fake_clusters.by_name["edge"]
    cr = _vmcp(fake_clusters)
    return "codeMode" in cr["spec"]["config"], READ_ONLY_HINT_FORBID in cedar_policies(
        cr, fake=fake
    )


def _applied(mongo, gid: str) -> bool:
    return mongo["gateways"].find_one({"_id": gid})["code_mode_applied"]


def _status_warnings(client, gid: str) -> list[str]:
    r = client.get(f"{GW}/{gid}")
    assert r.status_code == 200, r.text
    return r.json()["status"]["warnings"]


def test_a_code_mode_gateway_renders_code_mode_pins_and_the_hint_forbid_last(
    client, fake_clusters, mongo
):
    cid, items, calc, _server = _env(client, fake_clusters, mongo)
    gw = _deploy(client, mongo, cid, items, calc)
    fake = fake_clusters.by_name["edge"]
    cr = _vmcp(fake_clusters)
    code = cr["spec"]["config"]["codeMode"]
    assert code["enabled"] is True
    seconds = int(code["toolCallTimeout"].removesuffix("s"))
    assert 0 < seconds <= SCRIPT_MINUTE_SECONDS
    tools = cr["spec"]["config"]["aggregation"]["tools"]
    assert tools[0]["filter"] == ["Add"]
    # 힌트 forbid 는 광고 항목의 annotations 를 믿으므로 렌더는 이름 말고 아무것도 덮어쓰지 않는다
    assert all(set(o) <= {"name"} for t in tools for o in (t.get("overrides") or {}).values())
    assert cedar_policies(cr, fake=fake)[-1] == READ_ONLY_HINT_FORBID
    assert gw["code_mode"] is True and _applied(mongo, gw["id"]) is True


def test_a_gateway_without_code_mode_renders_as_a_document_from_before(
    client, fake_clusters, mongo
):
    """code mode 칸이 없던 판의 문서(칸 없음)와 이 판의 문서가 같은 묶음으로 렌더되고 판도 같다.
    codeMode 도 힌트 forbid 도 없다 (10절 셋째 항목)."""
    cid, items, calc, _server = _env(client, fake_clusters, mongo)
    gw = _deploy(client, mongo, cid, items, calc, code_mode=False)
    assert _rendered(fake_clusters) == (False, False)
    service = client.app.state.gateway_service
    now = mongo["gateways"].find_one({"_id": gw["id"]})
    old = {k: v for k, v in now.items() if k != "code_mode_applied"}
    old["spec"] = {k: v for k, v in now["spec"].items() if k != "code_mode"}
    old["revisions"] = [{k: v for k, v in r.items() if k != "code_mode"} for r in now["revisions"]]
    docs = {name: Gateway.from_doc(raw) for name, raw in (("now", now), ("before", old))}
    renders = {n: client.portal.call(service.render_docs, d) for n, d in docs.items()}
    assert renders["now"] == renders["before"]
    ts = client.portal.call(service.tenancy.tenant_settings.get, "default")
    revs = {n: client.portal.call(service.want_rev, d, ts) for n, d in docs.items()}
    assert revs["now"] == revs["before"]


def test_closing_keeps_the_hint_forbid_until_the_vmcp_without_code_mode_is_applied(
    client, fake_clusters, mongo
):
    """닫는 렌더는 code mode 를 빼되 forbid 를 둔다. vMCP 적용이 성공한 뒤의 렌더가 forbid 를
    뺀다. 다시 읽기로 켜면 둘 다 돌아온다."""
    cid, items, calc, server = _env(client, fake_clusters, mongo)
    gw = _deploy(client, mongo, cid, items, calc)
    _mark(mongo, server, Add=False)
    run_data_plane_auth(client)
    assert _rendered(fake_clusters) == (False, True)
    assert _applied(mongo, gw["id"]) is False
    closed = [w for w in _status_warnings(client, gw["id"]) if w.startswith(CODE_MODE_CLOSED)]
    assert closed and closed[0].endswith("(cause: tools; tools: Add)")
    run_data_plane_auth(client)
    assert _rendered(fake_clusters) == (False, False)
    _mark(mongo, server, Add=True)
    run_data_plane_auth(client)
    assert _rendered(fake_clusters) == (True, True)
    assert _applied(mongo, gw["id"]) is True
    assert not any(w.startswith(CODE_MODE_CLOSED) for w in _status_warnings(client, gw["id"]))


def test_a_config_wait_that_times_out_while_closing_keeps_the_forbid(
    client, fake_clusters, mongo, monkeypatch
):
    cid, items, calc, server = _env(client, fake_clusters, mongo)
    gw = _deploy(client, mongo, cid, items, calc)
    fake = fake_clusters.by_name["edge"]
    monkeypatch.setattr(authz_configs, "AUTHZ_POLL_SECONDS", 0.01)
    monkeypatch.setattr(gateways_module, "REAPPLY_AUTHZ_WAIT_SECONDS", 0.05)
    _mark(mongo, server, Add=False)
    fake.authz_pending = True
    run_data_plane_auth(client)
    # 설정은 적용됐지만 받아들여지지 않아 vMCP 는 그대로다. 설정은 forbid 를 둔다
    assert _rendered(fake_clusters) == (True, True)
    assert _applied(mongo, gw["id"]) is True
    fake.authz_pending = False
    # 실패한 문서는 다음 시도가 미뤄진다(`next_retry`). 그 뒤의 틱이다
    _later(client, 10)
    assert _rendered(fake_clusters) == (False, True)
    _later(client, 20)
    assert _rendered(fake_clusters) == (False, False)


def test_a_vmcp_apply_that_fails_while_closing_keeps_the_forbid(client, fake_clusters, mongo):
    cid, items, calc, server = _env(client, fake_clusters, mongo)
    gw = _deploy(client, mongo, cid, items, calc)
    fake = fake_clusters.by_name["edge"]
    _mark(mongo, server, Add=False)
    fake.denied.add(VMCP_APPLY)
    run_data_plane_auth(client)
    assert _rendered(fake_clusters) == (True, True)
    assert _applied(mongo, gw["id"]) is True
    fake.denied.discard(VMCP_APPLY)
    _later(client, 10)
    assert _rendered(fake_clusters) == (False, True)
    _later(client, 20)
    assert _rendered(fake_clusters) == (False, False)


def _later(client, minutes: int) -> None:
    """minutes 분 뒤의 재적용 틱이다."""
    run_data_plane_auth(client, now=utcnow() + timedelta(minutes=minutes))


def test_a_running_code_mode_gateway_stays_open_through_ticks_and_redeploys(
    client, fake_clusters, mongo
):
    """도는 렌더가 실은 스크립트(Agent 쪽 소스에는 든다, P56 6.5절)를 판정은 보지 않는다. 그래서
    다음 틱과 재배포가 그것을 예약 이름으로 거절해 code mode 를 닫지 않는다."""
    cid, items, calc, _server = _env(client, fake_clusters, mongo)
    gw = _deploy(client, mongo, cid, items, calc)
    assert _applied(mongo, gw["id"]) is True
    for minutes in (10, 20):
        _later(client, minutes)
        assert _rendered(fake_clusters) == (True, True)
        assert _applied(mongo, gw["id"]) is True
    assert not any(w.startswith(CODE_MODE_CLOSED) for w in _status_warnings(client, gw["id"]))
    r = client.post(f"{GW}/{gw['id']}/redeploy", json={})
    assert r.status_code < 300, r.text
    assert _rendered(fake_clusters) == (True, True)


def test_an_old_toolhive_closes_code_mode_with_its_cause(client, fake_clusters, mongo):
    cid, items, calc, _server = _env(client, fake_clusters, mongo)
    gw = _deploy(client, mongo, cid, items, calc)
    record_toolhive(mongo, cid, "0.52.3")
    run_data_plane_auth(client)
    assert "codeMode" not in _vmcp(fake_clusters)["spec"]["config"]
    warnings = _status_warnings(client, gw["id"])
    assert any(w.startswith(CODE_MODE_CLOSED) and w.endswith("(cause: toolhive)") for w in warnings)


def test_tools_the_platform_cannot_list_are_warned_as_hidden(client, fake_clusters, mongo):
    cid, items, calc, _server = _env(client, fake_clusters, mongo)
    gw = _deploy(client, mongo, cid, items, calc)
    fake = fake_clusters.by_name["edge"]
    fake.set_status(
        GV, "VirtualMCPServer", "gw-a", DEFAULT_NS, {"phase": "Ready", "observedGeneration": 1}
    )
    seen: list[str] = []

    async def lister(_client, url, headers=None):
        # 시험 앱은 header 모드라 vMCP 가 토큰 없이 받는다
        seen.append(url)
        assert not headers
        return [CODE_MODE_TOOL]

    service = client.app.state.gateway_service
    client.app.state.hub_service.tool_lister = lister
    # 배포의 답이 이미 상태를 읽어 나열하지 못한 것을 기억했다. 새로 읽게 한다
    service._listings = type(service._listings)(60.0)
    for _ in range(2):
        hidden = [
            w for w in _status_warnings(client, gw["id"]) if w.startswith(CODE_MODE_TOOLS_HIDDEN)
        ]
        assert hidden and hidden[0].endswith("(Add)")
    # 나열은 Gateway 마다 잠깐 기억하므로 상태 읽기 둘에 한 번이다
    assert len(seen) == 1 and "vmcp-gw-a" in seen[0]

    async def every(_client, url, headers=None):
        return [CODE_MODE_TOOL, "Add"]

    client.app.state.hub_service.tool_lister = every
    service._listings = type(service._listings)(60.0)
    assert not any(w.startswith(CODE_MODE_TOOLS_HIDDEN) for w in _status_warnings(client, gw["id"]))


@pytest.mark.parametrize("applied", [True, False])
def test_the_metrics_report_names_the_script_tool_while_code_mode_runs(
    settings, mongo, applied: bool
) -> None:
    doc = _gateway(spec={"code_mode": True}, code_mode_applied=applied)
    now = utcnow()
    events = [
        mcp_call_event(1, now, doc.id, proxy_kind="virtualmcpserver", backend="gw-calc"),
        mcp_call_event(2, now, doc.id, proxy_kind="virtualmcpserver", tool=CODE_MODE_TOOL),
    ]
    clusters = _Clusters(c1=cluster_doc(recorded_toolhive_version="0.53.0"))

    async def body(db, reports, roll):
        await db["mcp_audit_events"].insert_many(events)
        await roll.recount(hour_of(now))
        return await reports.gateway(doc, window="24h", tz="UTC")

    out = _reports(settings, clusters, body)
    named = CODE_MODE_TOOL if applied else None
    assert [t["tool"] for t in out["by_tool"]] == ["Add", named]
