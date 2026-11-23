"""P6 F5(b): MCP 프록시 로그 수집 (pods/log 폴링, 커서, 중복 제거, GET /audit/mcp).

Agent 런타임이 stdout 에 쓰는 관측 이벤트 줄의 파서와, 그 줄을 수집해 GET /agents/{id}/events 로
읽는 길도 든다.
"""

from __future__ import annotations

import json
from datetime import timedelta
from typing import Any

from daegam.services import audit_collector as collector_module
from daegam.services.audit_collector import (
    DRAIN_ATTEMPTS,
    DRAIN_CALL_TIMEOUT,
    DRAIN_NAMESPACED_MAX,
    LEASE_NAME,
    MAX_DELEGATION_CHAIN,
    MAX_DELEGATION_SUB,
    STARTUP_GRACE,
    parse_agent_line,
    parse_agent_log,
    parse_audit_line,
    parse_audit_log,
    parse_ts,
)
from daegam.store.base import utcnow
from daegam.store.leases import LEASES
from tests.helpers.agent_runtime import deployed_agent
from tests.helpers.consts import DEFAULT_NS, TENANT_HEADER
from tests.helpers.mcp_servers import clone_and_deploy, mcp_server_env
from tests.helpers.workers import audit_collector

PROXY_LABELS = {
    "toolhive": "true",
    "app.kubernetes.io/name": "mcpserver",
    "toolhive-name": "mcp-calc-1",
}

# spike(orbstack, ToolHive 0.46) 프록시 파드 stdout 에서 그대로 가져온 줄
TOOL_CALL = (
    '{"time":"2026-09-01T17:31:59.431608787Z","level":"AUDIT","msg":"audit_event",'
    '"audit_id":"7ae15617-d832-40a7-a661-695cdb9c455e","type":"mcp_tool_call",'
    '"logged_at":"2026-09-01T17:31:59.43159483Z","outcome":"success","component":"toolhive-api",'
    '"source":{"type":"network","value":"127.0.0.1","extra":{"user_agent":"python-httpx2/2.12.0"}},'
    '"subjects":{"user":"Local User: local","user_id":"local"},'
    '"target":{"endpoint":"/mcp","method":"tools/call","name":"Add","type":"tool"},'
    '"metadata":{"extra":{"duration_ms":42,"transport":"http"}}}'
)
TOOLS_LIST = (
    '{"time":"2026-09-01T17:31:59.385517544Z","level":"AUDIT","msg":"audit_event",'
    '"audit_id":"9485a2b6-ae38-4ea8-965e-95e6f5f45b2c","type":"mcp_tools_list",'
    '"logged_at":"2026-09-01T17:31:59.385509003Z","outcome":"success","component":"toolhive-api",'
    '"subjects":{"user":"Local User: local","user_id":"local"},'
    '"target":{"endpoint":"/mcp","method":"tools/list","type":"endpoint"}}'
)
NOISE = '{"time":"2026-09-01T17:10:15Z","level":"INFO","msg":"http: proxy error: context canceled"}'


def _event(audit_id: str, ts: str, *, tool: str = "Add", user_id: str = "u1", **extra) -> str:
    d = {
        "level": "AUDIT",
        "msg": "audit_event",
        "audit_id": audit_id,
        "type": "mcp_tool_call",
        "logged_at": ts,
        "outcome": "success",
        "component": "toolhive-api",
        "subjects": {"user": f"User: {user_id}", "user_id": user_id},
        "target": {"endpoint": "/mcp", "method": "tools/call", "name": tool, "type": "tool"},
        **extra,
    }
    return json.dumps(d)


def _iso(dt) -> str:
    return dt.isoformat(timespec="microseconds") + "Z"


# -- 파싱 ----------------------------------------------------------------------------


def test_parse_real_lines():
    doc = parse_audit_line(TOOL_CALL)
    assert doc["_id"] == "7ae15617-d832-40a7-a661-695cdb9c455e"
    assert doc["type"] == "mcp_tool_call" and doc["tool"] == "Add"
    assert doc["method"] == "tools/call" and doc["outcome"] == "success"
    assert doc["component"] == "toolhive-api"
    assert doc["sub"] == "local" and doc["user"] == "Local User: local"
    assert doc["ts"].isoformat() == "2026-09-01T17:31:59.431594"
    assert doc["metadata"]["extra"]["duration_ms"] == 42
    lst = parse_audit_line(TOOLS_LIST)
    assert lst["type"] == "mcp_tools_list" and lst["tool"] is None
    assert parse_audit_line(NOISE) is None
    assert parse_audit_line("not json") is None
    assert parse_audit_line('{"audit_id": ') is None
    assert parse_audit_line('{"audit_id": ""}') is None
    # kubectl --timestamps 처럼 앞에 시각이 붙어도, camelCase 초안 형식이어도 받는다
    prefixed = "2026-09-01T17:31:59.5Z " + TOOL_CALL
    assert parse_audit_line(prefixed)["_id"] == doc["_id"]
    camel = json.dumps(
        {
            "auditId": "x1",
            "type": "mcp_tool_call",
            "loggedAt": "2026-09-01T00:00:00Z",
            "subjects": {"userId": "svc", "clientName": "agent"},
            "target": {"method": "tools/call", "name": "Divide"},
        }
    )
    c = parse_audit_line(camel)
    assert (c["_id"], c["sub"], c["client_name"], c["tool"]) == ("x1", "svc", "agent", "Divide")
    docs, last = parse_audit_log("\n".join([NOISE, TOOLS_LIST, TOOL_CALL, "", NOISE]))
    assert [d["_id"] for d in docs] == [lst["_id"], doc["_id"]]
    assert last == doc["ts"]
    assert parse_audit_log("") == ([], None)


def _delegated(delegation: Any) -> dict[str, Any]:
    doc = parse_audit_line(json.dumps({**json.loads(TOOL_CALL), "delegation": delegation}))
    assert doc is not None
    return doc


def test_a_delegated_call_names_the_person_and_the_agent():
    """턴 토큰으로 부른 호출은 sub 가 사람이고, ToolHive 0.53 이 최상위 `delegation` 에 적은 사슬의
    맨 바깥 행위자가 act 다 (P53 F0 3번). 사슬에서는 행위자마다 sub 만 남긴다. 사슬이 없거나
    모양이 다르면 act 는 None 이다."""
    chain = {
        "chain": [{"sub": "svc:agent:helper", "iss": "x"}, {"sub": "svc:agent:inner"}],
        "truncated": False,
        "omitted": 0,
    }
    doc = _delegated(chain)
    assert (doc["sub"], doc["act"]) == ("local", "svc:agent:helper")
    kept = [{"sub": "svc:agent:helper"}, {"sub": "svc:agent:inner"}]
    assert doc["delegation"] == {"chain": kept, "truncated": False}
    assert parse_audit_line(TOOL_CALL)["act"] is None
    for bad in ({"chain": []}, {"chain": [{"iss": "x"}]}, {"chain": "x"}, "x"):
        assert _delegated(bad)["act"] is None
    # 맨 바깥 행위자의 sub 가 없으면 그 자리를 비워 둔다. 안쪽 행위자를 Agent 로 읽지 않는다
    assert _delegated({"chain": [{"iss": "x"}, {"sub": "svc:agent:inner"}]})["act"] is None


def test_a_long_delegation_chain_is_cut_and_marked():
    long_sub = "svc:agent:" + "x" * (MAX_DELEGATION_SUB + 10)
    links = [{"sub": long_sub}] + [{"sub": f"a{i}"} for i in range(MAX_DELEGATION_CHAIN + 3)]
    stored = _delegated({"chain": links, "truncated": False})["delegation"]
    assert len(stored["chain"]) == MAX_DELEGATION_CHAIN and stored["truncated"] is True
    assert stored["chain"][0]["sub"] == long_sub[:MAX_DELEGATION_SUB]
    # ToolHive 가 이미 잘랐으면 그대로 truncated 다
    assert _delegated({"chain": [{"sub": "a"}], "truncated": True})["delegation"]["truncated"]


def test_parse_ts_variants():
    assert parse_ts("2026-09-01T17:31:59.43159483Z").isoformat() == "2026-09-01T17:31:59.431594"
    assert parse_ts("2026-09-01T17:31:59Z").isoformat() == "2026-09-01T17:31:59"
    assert parse_ts("2026-09-01T19:31:59+02:00").isoformat() == "2026-09-01T17:31:59"
    assert parse_ts("nope") is None and parse_ts(None) is None


# -- 수집 (fake 클러스터) ----------------------------------------------------------------


def _instance_with_proxy(client, fake_clusters):
    cid, bp, ds, mp = mcp_server_env(client)
    r = clone_and_deploy(client, mp)
    assert r.status_code == 201, r.text
    fake = fake_clusters.by_name["edge"]
    fake.add_pod(DEFAULT_NS, "mcp-calc-1-7f9-abc", PROXY_LABELS, uid="uid-1")
    return r.json(), fake


def test_collects_tool_calls_with_tenant_and_cursor(client, fake_clusters, mongo):
    inst, fake = _instance_with_proxy(client, fake_clusters)
    now = utcnow()
    fake.add_pod_log(DEFAULT_NS, "mcp-calc-1-7f9-abc", NOISE, ts=now - timedelta(seconds=30))
    fake.add_pod_log(
        DEFAULT_NS, "mcp-calc-1-7f9-abc", _event("e1", _iso(now - timedelta(seconds=20))), ts=now
    )
    fake.add_pod_log(
        DEFAULT_NS,
        "mcp-calc-1-7f9-abc",
        _event("e2", _iso(now - timedelta(seconds=10)), tool="Divide", user_id="u2"),
        ts=now,
    )
    # 프록시가 아닌 파드(MCP 서버 본체, 다른 앱)는 건드리지 않는다
    fake.add_pod(DEFAULT_NS, "mcp-calc-1-0", {"toolhive": "true", "toolhive-name": "mcp-calc-1"})
    fake.add_pod(DEFAULT_NS, "redis-1", {"app": "redis"})
    fake.add_pod_log(DEFAULT_NS, "mcp-calc-1-0", _event("ignored", _iso(now)))

    col = audit_collector(client)
    result = client.portal.call(col.run_once)
    assert result == {"namespaces": 1, "pods": 1, "events": 2, "errors": []}
    # 첫 폴링은 커서가 없어 전체 로그를 읽는다
    assert fake.log_reads == [(DEFAULT_NS, "mcp-calc-1-7f9-abc", None)]

    rows = list(mongo["mcp_audit_events"].find().sort("ts", 1))
    assert [r["_id"] for r in rows] == ["e1", "e2"]
    e1 = rows[0]
    assert e1["tenant"] == "default" and e1["instance_id"] == inst["id"]
    assert e1["cluster_id"] == inst["cluster_id"] and e1["namespace"] == DEFAULT_NS
    assert e1["pod"] == "mcp-calc-1-7f9-abc" and e1["proxy_kind"] == "mcpserver"
    assert e1["tool"] == "Add" and e1["sub"] == "u1"
    assert rows[1]["tool"] == "Divide" and rows[1]["sub"] == "u2"
    cur = mongo["audit_cursors"].find_one(
        {"_id": f"{inst['cluster_id']}/{DEFAULT_NS}/mcp-calc-1-7f9-abc"}
    )
    assert cur["pod_uid"] == "uid-1"
    assert abs((cur["since"] - (now - timedelta(seconds=10))).total_seconds()) < 0.01

    # 두 번째 폴링: 커서 이후(+겹침)만 읽고, 이미 있는 이벤트는 무시한다
    fake.add_pod_log(
        DEFAULT_NS, "mcp-calc-1-7f9-abc", _event("e3", _iso(now + timedelta(seconds=1))), ts=now
    )
    result = client.portal.call(col.run_once)
    assert result["events"] == 1
    since = fake.log_reads[-1][2]
    assert since is not None and 5 <= since <= 20
    assert mongo["mcp_audit_events"].count_documents({}) == 3
    # 아무것도 없으면 0
    assert client.portal.call(col.run_once)["events"] == 0
    assert mongo["mcp_audit_events"].count_documents({}) == 3


def test_remote_proxy_and_unmapped_proxy_pods_get_a_tenant(client, fake_clusters, mongo):
    """vMCP·원격 프록시도 감사를 낸다. 허브 원격 항목의 파드는 그 항목 id 로 적고, 맵에 없는 프록시
    파드는 네임스페이스의 테넌트로 적는다. 테넌트가 비면 테넌트 화면의 MCP 감사에 안 보인다."""
    _inst, fake = _instance_with_proxy(client, fake_clusters)
    r = client.post(
        "/hub", json={"name": "wiki", "kind": "remote", "url": "https://mcp.example.com/mcp"}
    )
    assert r.status_code == 201, r.text
    hub_id = r.json()["id"]
    now = utcnow()
    remote = {
        "toolhive": "true",
        "app.kubernetes.io/name": "mcpremoteproxy",
        "toolhive-name": "hub-wiki",
    }
    stray = {
        "toolhive": "true",
        "app.kubernetes.io/name": "virtualmcpserver",
        "toolhive-name": "gw-unknown",
    }
    fake.add_pod(DEFAULT_NS, "hub-wiki-5d8-xyz", remote, uid="uid-r")
    fake.add_pod(DEFAULT_NS, "gw-unknown-6c9-xyz", stray, uid="uid-g")
    at = _iso(now - timedelta(seconds=5))
    fake.add_pod_log(DEFAULT_NS, "hub-wiki-5d8-xyz", _event("r1", at, tool="ask_question"), ts=now)
    fake.add_pod_log(DEFAULT_NS, "gw-unknown-6c9-xyz", _event("g1", at), ts=now)

    col = audit_collector(client, hub=client.app.state.hub_items)
    assert client.portal.call(col.run_once)["errors"] == []
    r1 = mongo["mcp_audit_events"].find_one({"_id": "r1"})
    assert r1["tenant"] == "default" and r1["instance_id"] == hub_id
    assert r1["proxy_kind"] == "mcpremoteproxy" and r1["tool"] == "ask_question"
    g1 = mongo["mcp_audit_events"].find_one({"_id": "g1"})
    assert g1["tenant"] == "default" and g1["instance_id"] is None


def test_lease_and_unreachable_cluster(client, fake_clusters, mongo):
    inst, fake = _instance_with_proxy(client, fake_clusters)
    a = audit_collector(client, "pod-a")
    b = audit_collector(client, "pod-b")
    assert "skipped" not in client.portal.call(a.run_once)
    assert client.portal.call(b.run_once) == {
        "namespaces": 0,
        "pods": 0,
        "events": 0,
        "errors": [],
        "skipped": "lease",
    }
    assert mongo["reconcile_leases"].find_one({"_id": LEASE_NAME})["holder"] == "pod-a"
    client.portal.call(a.stop)
    assert mongo["reconcile_leases"].find_one({"_id": LEASE_NAME}) is None
    assert "skipped" not in client.portal.call(b.run_once)
    # 클러스터가 죽으면 그 네임스페이스만 건너뛰고 루프는 계속
    fake.unreachable = True
    result = client.portal.call(b.run_once)
    assert result["namespaces"] == 0 and len(result["errors"]) == 1


# -- GET /audit/mcp --------------------------------------------------------------------


def test_query_mcp_events(client, fake_clusters, mongo):
    """수집기가 저장한 행을 GET /audit/mcp 가 걸러 낸다. 위임된 호출의 act 와 사슬도 그대로 낸다
    (P53 7절)."""
    inst, fake = _instance_with_proxy(client, fake_clusters)
    now = utcnow()
    chain = {"chain": [{"sub": "svc:agent:helper"}], "truncated": False}
    fake.add_pod_log(
        DEFAULT_NS, "mcp-calc-1-7f9-abc", _event("e1", _iso(now), delegation=chain), ts=now
    )
    fake.add_pod_log(
        DEFAULT_NS,
        "mcp-calc-1-7f9-abc",
        _event(
            "e2", _iso(now + timedelta(seconds=1)), tool="Divide", user_id="u2", outcome="denied"
        ),
        ts=now,
    )
    fake.add_pod_log(DEFAULT_NS, "mcp-calc-1-7f9-abc", TOOLS_LIST, ts=now)
    client.portal.call(audit_collector(client).run_once)

    r = client.get("/audit/mcp")
    assert r.status_code == 200, r.text
    assert [e["audit_id"] for e in r.json()] == [
        "e2",
        "e1",
        TOOLS_LIST.split('"audit_id":"')[1][:36],
    ]
    e2 = r.json()[0]
    assert e2["tool"] == "Divide" and e2["tenant"] == "default" and isinstance(e2["ts"], str)
    assert e2["act"] is None and e2["delegation"] is None
    e1 = r.json()[1]
    assert (e1["sub"], e1["act"], e1["delegation"]) == ("u1", "svc:agent:helper", chain)
    assert e2["instance_id"] == inst["id"] and e2["proxy_kind"] == "mcpserver"
    assert [e["audit_id"] for e in client.get("/audit/mcp", params={"tool": "Add"}).json()] == [
        "e1"
    ]
    assert [e["audit_id"] for e in client.get("/audit/mcp", params={"sub": "u2"}).json()] == ["e2"]
    assert len(client.get("/audit/mcp", params={"type": "mcp_tools_list"}).json()) == 1
    assert len(client.get("/audit/mcp", params={"outcome": "denied"}).json()) == 1
    assert len(client.get("/audit/mcp", params={"component": "toolhive-api"}).json()) == 3
    assert client.get("/audit/mcp", params={"tenant": "other"}).json() == []
    assert client.get("/audit/mcp", params={"tenant": "default"}).status_code == 200
    later = _iso(now + timedelta(minutes=1))
    assert client.get("/audit/mcp", params={"since": later}).json() == []
    assert len(client.get("/audit/mcp", params={"limit": 2}).json()) == 2


def test_identical_event_lines_are_not_collapsed():
    """같은 밀리초에 내용이 같은 줄이 둘 나와도 이벤트가 합쳐지면 안 된다.

    중복 제거 키가 (파드 uid + 줄 내용) 해시라, 런타임이 줄 번호를 싣지 않으면 두 이벤트가
    한 문서로 합쳐져 감사 기록이 실제보다 적게 남는다.
    """
    base = (
        '{"daegam_event": "guardrail", "ts": "2026-09-07T00:00:00.123+00:00", '
        '"session_id": "s1", "turn": 3, "position": "tool", "rule": "deny_pattern"'
    )
    text = f'{base}, "n": 7}}\n{base}, "n": 8}}\n'
    docs, _ = parse_agent_log(text, salt="pod-uid")
    assert len(docs) == 2
    assert len({d["_id"] for d in docs}) == 2, "내용이 같은 두 줄이 한 문서로 합쳐졌다"
    # 같은 줄을 다시 읽어도(로그를 겹쳐 읽는다) 같은 문서다
    again, _ = parse_agent_log(text, salt="pod-uid")
    assert {d["_id"] for d in again} == {d["_id"] for d in docs}


# -- Agent 관측 이벤트 -------------------------------------------------------------------

AGENT_TURN_END = (
    '{"daegam_event": "turn_end", "ts": "2026-09-06T01:02:03.456+00:00", '
    '"profile": {"id": "ap-1", "name": "support", "revision": 1}, '
    '"instance": {"id": "ai-1", "name": "support"}, "session_id": "s1", "user_sub": "u1", '
    '"turn": 2, "status": "done", "duration_ms": 12.5, "usage": {"input_tokens": 3}, '
    '"tool_calls": 1}'
)
AGENT_TOOL_CALL = (
    '{"daegam_event": "tool_call", "ts": "2026-09-06T01:02:02Z", "profile": {"id": "ap-1"}, '
    '"instance": {"id": "ai-1"}, "session_id": "s1", "user_sub": "u1", "turn": 2, '
    '"tool": "Add", "call_id": "c1", "args": "{\\"a\\": 3}"}'
)
AGENT_NOISE = "INFO:     Application startup complete."


def test_lines_with_numbers_that_are_not_json_are_skipped():
    assert parse_agent_line('{"daegam_event": "budget", "id": "n1", "used": NaN}') is None
    assert parse_agent_line('{"daegam_event": "budget", "id": "n2", "used": 1e400}') is None


def test_parse_agent_lines():
    doc = parse_agent_line(AGENT_TURN_END, salt="uid-1")
    assert doc["event"] == "turn_end" and doc["ts"].isoformat() == "2026-09-06T01:02:03.456000"
    assert doc["session_id"] == "s1" and doc["user_sub"] == "u1" and doc["turn"] == 2
    assert doc["status"] == "done" and doc["tool"] is None and doc["decided_by"] is None
    assert doc["profile"] == {"id": "ap-1", "name": "support", "revision": 1}
    assert doc["instance"] == {"id": "ai-1", "name": "support"}
    assert doc["data"]["duration_ms"] == 12.5 and doc["data"]["usage"] == {"input_tokens": 3}
    assert "daegam_event" not in doc["data"] and "profile" not in doc["data"]
    assert len(doc["_id"]) == 32
    # 같은 파드의 같은 줄은 같은 id, 다른 파드면 다른 id. 접두어(kubectl --timestamps)는 무시
    assert parse_agent_line(AGENT_TURN_END, salt="uid-1")["_id"] == doc["_id"]
    assert parse_agent_line(AGENT_TURN_END, salt="uid-2")["_id"] != doc["_id"]
    assert (
        parse_agent_line("2026-09-06T01:02:03Z " + AGENT_TURN_END, salt="uid-1")["_id"]
        == doc["_id"]
    )
    call = parse_agent_line(AGENT_TOOL_CALL)
    # 본문은 data 가 아니라 body 하나에 있다(P37 5절)
    assert call["tool"] == "Add" and call["body"] == {"args": '{"a": 3}'}
    assert "args" not in call["data"] and call["source"] == "log" and call["seq"] is None
    assert parse_agent_line(AGENT_NOISE) is None
    assert parse_agent_line('{"daegam_event": ""}') is None
    assert parse_agent_line('{"daegam_event": ') is None
    assert parse_agent_line('{"audit_id": "x", "type": "mcp_tool_call"}') is None
    docs, last = parse_agent_log(
        "\n".join([AGENT_NOISE, AGENT_TOOL_CALL, "", AGENT_TURN_END]), salt="u"
    )
    assert [d["event"] for d in docs] == ["tool_call", "turn_end"]
    assert last == doc["ts"]
    assert parse_agent_log("") == ([], None)


def test_collector_stores_agent_events_and_api_reads_them(client, fake_clusters, mongo):
    agent = deployed_agent(client, fake_clusters, mongo, runtime=False)
    fake = agent.fake
    labels = {
        "daegam.devops.ai.kr/resource": "agent-instance",
        "daegam.devops.ai.kr/id": agent.id,
        "daegam.devops.ai.kr/tenant": "default",
        "app.kubernetes.io/managed-by": "daegam",
    }
    fake.add_pod(DEFAULT_NS, "agent-support-7f9-abc", labels, uid="uid-1")
    now = utcnow()
    for line in (AGENT_NOISE, AGENT_TOOL_CALL, AGENT_TURN_END):
        fake.add_pod_log(DEFAULT_NS, "agent-support-7f9-abc", line, ts=now)
    # 에이전트가 아닌 파드는 건드리지 않는다
    fake.add_pod(DEFAULT_NS, "redis-1", {"app": "redis"})
    fake.add_pod_log(DEFAULT_NS, "redis-1", AGENT_TURN_END)
    col = audit_collector(client, agents=client.app.state.agents)
    result = client.portal.call(col.run_once)
    assert result == {"namespaces": 1, "pods": 1, "events": 2, "errors": []}
    assert fake.log_reads == [(DEFAULT_NS, "agent-support-7f9-abc", None)]
    rows = list(mongo["agent_events"].find().sort("ts", 1))
    assert [r["event"] for r in rows] == ["tool_call", "turn_end"]
    assert rows[0]["tenant"] == "default" and rows[0]["instance_id"] == agent.id
    assert rows[0]["cluster_id"] == agent.cluster_id and rows[0]["namespace"] == DEFAULT_NS
    assert rows[0]["pod"] == "agent-support-7f9-abc" and rows[0]["tool"] == "Add"
    cur = mongo["audit_cursors"].find_one(
        {"_id": f"{agent.cluster_id}/{DEFAULT_NS}/agent-support-7f9-abc"}
    )
    assert cur["pod_uid"] == "uid-1"
    # 다시 읽어도 겹친 줄은 id 로 걸러진다
    assert client.portal.call(col.run_once)["events"] == 0
    assert mongo["agent_events"].count_documents({}) == 2

    r = client.get(f"{agent.url}/events")
    assert r.status_code == 200, r.text
    assert [e["event"] for e in r.json()] == ["turn_end", "tool_call"]
    e = r.json()[0]
    # 저장은 naive UTC 이고 응답에만 시간대가 붙는다. 없으면 브라우저가 로컬 시각으로 읽는다
    assert e["ts"] == "2026-09-06T01:02:03.456000+00:00" and e["session_id"] == "s1"
    assert e["status"] == "done" and e["data"]["tool_calls"] == 1 and e["instance_id"] == agent.id
    instance, _, digest = e["id"].partition(":")
    assert e["collected_at"].endswith("+00:00") and (instance, len(digest)) == (agent.id, 32)
    assert [e["event"] for e in client.get(f"{agent.url}/events?event=tool_call").json()] == [
        "tool_call"
    ]
    assert client.get(f"{agent.url}/events?session_id=nope").json() == []
    assert len(client.get(f"{agent.url}/events?limit=1").json()) == 1
    assert client.get(f"{agent.url}/events?since=2027-01-01T00:00:00Z").json() == []
    assert client.get(f"{agent.url}/events?since=nope").status_code == 422
    assert client.get(f"{agent.url}/events", headers={TENANT_HEADER: "o"}).status_code == 404


def test_collector_skips_agent_pods_that_pushed_recently(client, fake_clusters, mongo):
    """최근에 이벤트를 밀어 보낸 파드의 로그는 읽지 않는다. 옛 이미지와 밀어 보내기가 막힌 파드는
    예전처럼 읽는다 (P37 4절)."""
    agent = deployed_agent(client, fake_clusters, mongo, runtime=False)
    labels = {
        "daegam.devops.ai.kr/resource": "agent-instance",
        "daegam.devops.ai.kr/id": agent.id,
        "daegam.devops.ai.kr/tenant": "default",
        "app.kubernetes.io/managed-by": "daegam",
    }
    now = utcnow()
    for name, uid in (("agent-pushing", "uid-push"), ("agent-old", "uid-old")):
        agent.fake.add_pod(DEFAULT_NS, name, labels, uid=uid)
        agent.fake.add_pod_log(DEFAULT_NS, name, AGENT_TURN_END, ts=now)
    mongo["agent_telemetry"].insert_one(
        {
            "_id": f"{agent.id}:uid-push",
            "instance_id": agent.id,
            "pod_uid": "uid-push",
            "last_at": now,
        }
    )
    col = audit_collector(client, agents=client.app.state.agents)
    result = client.portal.call(col.run_once)
    assert result["events"] == 1
    assert [p for _ns, p, _c in agent.fake.log_reads] == ["agent-old"]
    # 밀어 보내는 파드의 커서는 지금이라, 밀어 보내기가 멈춰도 컨테이너가 뜬 때부터 다시 읽지 않는다
    cursor = mongo["audit_cursors"].find_one({"_id": {"$regex": "/agent-pushing$"}})
    assert cursor["pod_uid"] == "uid-push" and cursor["since"] >= now - timedelta(seconds=1)


def _agent_container(pod: dict, started_at, *, pushes: bool = True) -> None:
    """가짜 파드에 agent 컨테이너의 env 와 기동 시각을 싣는다."""
    env = [{"name": "DAEGAM_EVENTS_URL", "value": "http://p/api/x"}] if pushes else []
    pod["spec"] = {"containers": [{"name": "agent", "env": env}]}
    pod["status"]["containerStatuses"] = [
        {"name": "agent", "state": {"running": {"startedAt": started_at.isoformat() + "Z"}}}
    ]


def test_collector_waits_for_the_first_push_of_a_new_agent_pod(client, fake_clusters, mongo):
    """agent 컨테이너가 뜬 지 STARTUP_GRACE 가 안 된 파드는 첫 push 를 기다린다. 로그도 커서도
    건드리지 않아서, 끝내 push 하지 않으면 유예가 지난 뒤 처음부터 읽는다. push 할 주소가 없는
    파드는 기다리지 않는다."""
    agent = deployed_agent(client, fake_clusters, mongo, runtime=False)
    labels = {
        "daegam.devops.ai.kr/resource": "agent-instance",
        "daegam.devops.ai.kr/id": agent.id,
        "daegam.devops.ai.kr/tenant": "default",
        "app.kubernetes.io/managed-by": "daegam",
    }
    now = utcnow()
    young = agent.fake.add_pod(DEFAULT_NS, "agent-young", labels, uid="uid-young")
    settled = agent.fake.add_pod(DEFAULT_NS, "agent-settled", labels, uid="uid-settled")
    no_ingest = agent.fake.add_pod(DEFAULT_NS, "agent-no-ingest", labels, uid="uid-no-ingest")
    _agent_container(young, now - timedelta(seconds=10))
    _agent_container(settled, now - STARTUP_GRACE - timedelta(seconds=10))
    _agent_container(no_ingest, now - timedelta(seconds=10), pushes=False)
    for name in ("agent-young", "agent-settled", "agent-no-ingest"):
        agent.fake.add_pod_log(DEFAULT_NS, name, AGENT_TURN_END, ts=now)
    col = audit_collector(client, agents=client.app.state.agents)
    client.portal.call(col.run_once)
    assert sorted(p for _ns, p, _c in agent.fake.log_reads) == ["agent-no-ingest", "agent-settled"]
    assert mongo["audit_cursors"].find_one({"_id": {"$regex": "/agent-young$"}}) is None

    _agent_container(young, now - STARTUP_GRACE - timedelta(seconds=1))
    client.portal.call(col.run_once)
    assert "agent-young" in [p for _ns, p, _c in agent.fake.log_reads]
    assert mongo["agent_events"].count_documents({"pod": "agent-young"}) == 1


def test_a_vmcp_front_pod_is_written_to_its_mcp_server(client, fake_clusters, mongo):
    """노출한 MCP Server 의 vMCP 앞단 파드는 그 서버의 id 라벨과 앞단의 리소스 값을 단다. 수집기는
    그 서버의 이벤트로 적는다. 같은 vMCP 종류라도 리소스 값이 다른 파드는 그렇게 적지 않는다 (P54
    11절)."""
    inst, fake = _instance_with_proxy(client, fake_clusters)
    now = utcnow()
    front = {
        "toolhive": "true",
        "app.kubernetes.io/name": "virtualmcpserver",
        "toolhive-name": f"front-{inst['k8s']['name']}",
        "daegam.devops.ai.kr/resource": "mcp-instance-front",
        "daegam.devops.ai.kr/id": inst["id"],
        "daegam.devops.ai.kr/tenant": "default",
    }
    other = {**front, "toolhive-name": "gw-x", "daegam.devops.ai.kr/resource": "gateway-instance"}
    fake.add_pod(DEFAULT_NS, "front-pod", front, uid="uid-f")
    fake.add_pod(DEFAULT_NS, "gw-pod", other, uid="uid-o")
    at = _iso(now - timedelta(seconds=5))
    fake.add_pod_log(DEFAULT_NS, "front-pod", _event("f1", at), ts=now)
    fake.add_pod_log(DEFAULT_NS, "gw-pod", _event("o1", at), ts=now)
    col = audit_collector(client)
    assert client.portal.call(col.run_once)["errors"] == []
    f1 = mongo["mcp_audit_events"].find_one({"_id": "f1"})
    assert (f1["tenant"], f1["instance_id"]) == ("default", inst["id"])
    assert f1["proxy_kind"] == "virtualmcpserver"
    assert mongo["mcp_audit_events"].find_one({"_id": "o1"})["instance_id"] is None


# -- 사라지는 파드와 다시 뜬 컨테이너 ----------------------------------------------------


def _terminate(fake, name: str) -> None:
    fake.objects[("v1", "Pod", DEFAULT_NS, name)]["metadata"]["deletionTimestamp"] = _iso(utcnow())


def test_a_terminating_proxy_pod_is_read_between_polls(client, fake_clusters, mongo):
    """폴링 사이에 지워지기 시작한 파드는 짧은 회차가 커서부터 한 번 더 읽는다. 살아 있는 파드는
    읽지 않고, 같은 줄을 두 번 넣지 않는다."""
    inst, fake = _instance_with_proxy(client, fake_clusters)
    pod = "mcp-calc-1-7f9-abc"
    now = utcnow()
    fake.add_pod_log(DEFAULT_NS, pod, _event("e1", _iso(now - timedelta(seconds=20))), ts=now)
    col = audit_collector(client)
    assert client.portal.call(col.drain_once) == {"pods": 0, "events": 0, "errors": 0}
    assert fake.log_reads == []  # 폴링이 리스를 쥐기 전에는 돌지 않는다
    assert client.portal.call(col.run_once)["events"] == 1

    # 마지막 폴링 뒤에 쓴 줄. 파드가 살아 있는 동안은 다음 폴링의 몫이다
    fake.add_pod_log(DEFAULT_NS, pod, _event("e2", _iso(now + timedelta(seconds=1))), ts=now)
    assert client.portal.call(col.drain_once) == {"pods": 0, "events": 0, "errors": 0}
    assert len(fake.log_reads) == 1

    fake.add_pod(DEFAULT_NS, "mcp-calc-1-8a1-new", PROXY_LABELS, uid="uid-2")
    _terminate(fake, pod)
    assert client.portal.call(col.drain_once) == {"pods": 1, "events": 1, "errors": 0}
    # 커서부터만 읽었고 새 파드는 건드리지 않았다
    assert fake.log_reads[-1][1] == pod and fake.log_reads[-1][2] is not None
    assert len(fake.log_reads) == 2
    row = mongo["mcp_audit_events"].find_one({"_id": "e2"})
    assert row["tenant"] == "default" and row["instance_id"] == inst["id"] and row["pod"] == pod

    # 파드가 남아 있으면 다음 회차와 폴링이 다시 읽지만 줄은 늘지 않는다
    fake.add_pod_log(DEFAULT_NS, pod, _event("e3", _iso(now + timedelta(seconds=2))), ts=now)
    assert client.portal.call(col.drain_once)["events"] == 1
    assert client.portal.call(col.drain_once)["events"] == 0
    assert client.portal.call(col.run_once)["events"] == 0
    assert mongo["mcp_audit_events"].count_documents({}) == 3
    # 짧은 회차는 커서를 지우지 않는다. 사라진 파드의 커서는 폴링이 지운다
    key = f"{inst['cluster_id']}/{DEFAULT_NS}/{pod}"
    assert mongo["audit_cursors"].find_one({"_id": key}) is not None
    del fake.objects[("v1", "Pod", DEFAULT_NS, pod)]
    client.portal.call(col.drain_once)
    assert mongo["audit_cursors"].find_one({"_id": key}) is not None
    client.portal.call(col.run_once)
    assert mongo["audit_cursors"].find_one({"_id": key}) is None


def test_the_short_pass_stops_without_the_lease_and_survives_a_failure(
    client, fake_clusters, mongo
):
    _inst, fake = _instance_with_proxy(client, fake_clusters)
    pod = "mcp-calc-1-7f9-abc"
    fake.add_pod_log(DEFAULT_NS, pod, _event("e1", _iso(utcnow())))
    holder = audit_collector(client, holder="pod-a")
    other = audit_collector(client, holder="pod-b")
    assert client.portal.call(holder.run_once)["events"] == 1
    assert client.portal.call(other.run_once) == {
        "namespaces": 0,
        "pods": 0,
        "events": 0,
        "errors": [],
        "skipped": "lease",
    }
    fake.add_pod_log(DEFAULT_NS, pod, _event("e2", _iso(utcnow())))
    _terminate(fake, pod)
    assert client.portal.call(other.drain_once) == {"pods": 0, "events": 0, "errors": 0}
    fake.unreachable = True
    assert client.portal.call(holder.drain_once) == {"pods": 0, "events": 0, "errors": 1}
    # 실패한 클러스터는 다음 폴링까지 건너뛴다. 폴링이 그 줄을 읽는다
    fake.unreachable = False
    lists = len(fake.pod_metadata_lists)
    assert client.portal.call(holder.drain_once) == {"pods": 0, "events": 0, "errors": 0}
    assert len(fake.pod_metadata_lists) == lists
    assert client.portal.call(holder.run_once)["events"] == 1
    assert client.portal.call(holder.drain_once) == {"pods": 1, "events": 0, "errors": 0}


def test_a_restarted_container_is_read_from_its_previous_log(client, fake_clusters, mongo):
    """컨테이너만 다시 뜬 파드는 uid 가 같다. 앞 컨테이너가 마지막 폴링 뒤에 쓴 줄을 그 로그에서
    커서부터 읽고, 이미 넣은 줄은 다시 넣지 않는다."""
    inst, fake = _instance_with_proxy(client, fake_clusters)
    pod = "mcp-calc-1-7f9-abc"
    fake.set_status(
        "v1",
        "Pod",
        pod,
        DEFAULT_NS,
        {"phase": "Running", "containerStatuses": [{"name": "mcp", "restartCount": 0}]},
    )
    now = utcnow()
    fake.add_pod_log(DEFAULT_NS, pod, _event("e1", _iso(now - timedelta(seconds=20))), ts=now)
    col = audit_collector(client)
    assert client.portal.call(col.run_once)["events"] == 1
    key = f"{inst['cluster_id']}/{DEFAULT_NS}/{pod}"
    assert mongo["audit_cursors"].find_one({"_id": key})["restarts"] == 0
    assert fake.previous_log_reads == []

    fake.add_pod_log(DEFAULT_NS, pod, _event("e2", _iso(now + timedelta(seconds=1))), ts=now)
    fake.restart_pod_container(DEFAULT_NS, pod)
    fake.add_pod_log(DEFAULT_NS, pod, _event("e3", _iso(now + timedelta(seconds=2))), ts=now)
    assert client.portal.call(col.run_once)["events"] == 2
    assert [r["_id"] for r in mongo["mcp_audit_events"].find().sort("ts", 1)] == ["e1", "e2", "e3"]
    assert len(fake.previous_log_reads) == 1 and fake.previous_log_reads[0][2] is not None
    assert mongo["audit_cursors"].find_one({"_id": key})["restarts"] == 1

    # 재시작 수가 그대로면 앞 로그를 다시 읽지 않는다
    assert client.portal.call(col.run_once)["events"] == 0
    assert len(fake.previous_log_reads) == 1
    # kubelet 이 앞 로그를 버렸어도 지금 컨테이너의 줄은 들어간다
    fake.restart_pod_container(DEFAULT_NS, pod)
    del fake.previous_pod_logs[(DEFAULT_NS, pod)]
    fake.add_pod_log(DEFAULT_NS, pod, _event("e4", _iso(now + timedelta(seconds=3))), ts=now)
    assert client.portal.call(col.run_once)["events"] == 1
    assert mongo["mcp_audit_events"].count_documents({}) == 4


def test_a_terminating_agent_pod_is_read_before_its_first_push(client, fake_clusters, mongo):
    """첫 push 를 기다리던 Agent 파드가 지워지기 시작하면 더 기다리지 않고 로그를 읽는다."""
    agent = deployed_agent(client, fake_clusters, mongo, runtime=False)
    fake = agent.fake
    labels = {
        "daegam.devops.ai.kr/resource": "agent-instance",
        "daegam.devops.ai.kr/id": agent.id,
        "daegam.devops.ai.kr/tenant": "default",
    }
    pod = fake.add_pod(DEFAULT_NS, "agent-support-7f9-abc", labels, uid="uid-1")
    pod["spec"] = {"containers": [{"name": "agent", "env": [{"name": "DAEGAM_EVENTS_URL"}]}]}
    pod["status"]["containerStatuses"] = [
        {"name": "agent", "restartCount": 0, "state": {"running": {"startedAt": _iso(utcnow())}}}
    ]
    fake.add_pod_log(DEFAULT_NS, "agent-support-7f9-abc", AGENT_TOOL_CALL)
    col = audit_collector(client, agents=client.app.state.agents)
    assert client.portal.call(col.run_once)["pods"] == 0  # 첫 push 를 기다린다
    _terminate(fake, "agent-support-7f9-abc")
    assert client.portal.call(col.drain_once) == {"pods": 1, "events": 1, "errors": 0}
    assert client.portal.call(col.drain_once)["events"] == 0
    assert mongo["agent_events"].count_documents({}) == 1


def test_the_loop_polls_at_its_interval_and_runs_short_passes_between(client, monkeypatch):
    col = audit_collector(client, interval=60.0)
    calls: list[str] = []

    async def poll():
        calls.append("poll")

    async def drain():
        calls.append("drain")

    monkeypatch.setattr(col, "run_once", poll)
    monkeypatch.setattr(col, "drain_once", drain)
    client.portal.call(col._tick)
    client.portal.call(col._tick)
    col._last_poll -= 61  # 폴링 간격이 지났다
    client.portal.call(col._tick)
    client.portal.call(col._tick)
    assert calls == ["poll", "drain", "poll", "drain"]


def test_the_short_pass_lists_cached_metadata_in_the_target_namespaces(
    client, fake_clusters, mongo
):
    """짧은 회차의 나열은 대상 네임스페이스의 metadata 를 캐시에서 받는다. 대상이 많으면 클러스터
    전체를 한 번 나열해 호출 수가 네임스페이스 수를 따라 늘지 않는다."""
    _inst, fake = _instance_with_proxy(client, fake_clusters)
    col = audit_collector(client)
    client.portal.call(col.run_once)
    assert fake.pod_metadata_lists == []  # 폴링은 파드를 통째로 나열한다
    client.portal.call(col.drain_once)
    assert fake.pod_metadata_lists == [
        (DEFAULT_NS, {"toolhive": "true"}, True, DRAIN_CALL_TIMEOUT),
        (DEFAULT_NS, {"daegam.devops.ai.kr/resource": "agent-instance"}, True, DRAIN_CALL_TIMEOUT),
    ]

    fake.pod_metadata_lists.clear()
    many = {f"ns-{i}": {} for i in range(DRAIN_NAMESPACED_MAX + 1)}
    client.portal.call(col._terminating, fake, {"toolhive": "true"}, many)
    assert [scope for scope, *_ in fake.pod_metadata_lists] == [None]
    fake.pod_metadata_lists.clear()
    many.popitem()
    client.portal.call(col._terminating, fake, {"toolhive": "true"}, many)
    assert [scope for scope, *_ in fake.pod_metadata_lists] == sorted(many)


def _terminate_at(fake, name: str, ends) -> None:
    fake.objects[("v1", "Pod", DEFAULT_NS, name)]["metadata"]["deletionTimestamp"] = _iso(ends)


def test_a_terminating_pod_is_read_on_every_short_pass_until_it_should_be_gone(
    client, fake_clusters, mongo
):
    """지우기 시작할 때 처리하던 호출의 줄은 몇 초 뒤에 쓰인다. 새 줄이 없던 회차 뒤에도 계속 읽고,
    끝나야 할 때에서 여유 시간이 지나도 남은 파드(finalizer, 죽은 노드)만 폴링에 맡긴다."""
    _inst, fake = _instance_with_proxy(client, fake_clusters)
    pod = "mcp-calc-1-7f9-abc"
    col = audit_collector(client)
    client.portal.call(col.run_once)
    now = utcnow()
    _terminate_at(fake, pod, now + timedelta(seconds=30))
    fake.log_reads.clear()
    for _ in range(3):
        assert client.portal.call(col.drain_once) == {"pods": 1, "events": 0, "errors": 0}
    fake.add_pod_log(DEFAULT_NS, pod, _event("in-flight", _iso(utcnow())))
    assert client.portal.call(col.drain_once) == {"pods": 1, "events": 1, "errors": 0}
    assert len(fake.log_reads) == 4

    _terminate_at(fake, pod, now - collector_module.DRAIN_GRACE_MARGIN - timedelta(seconds=1))
    fake.add_pod_log(DEFAULT_NS, pod, _event("stuck", _iso(utcnow())))
    assert client.portal.call(col.drain_once) == {"pods": 0, "events": 0, "errors": 0}
    assert len(fake.log_reads) == 4
    assert client.portal.call(col.run_once)["events"] == 1  # 폴링은 읽는다


def test_pods_that_cannot_be_read_do_not_stop_the_others_or_the_cluster(
    client, fake_clusters, mongo, monkeypatch
):
    """죽은 노드의 파드는 읽기가 시한까지 걸려 실패한다. 그런 파드가 클러스터의 시간을 다 써도 늦게
    지우기 시작한 성한 파드는 읽히고, 클러스터는 건너뛰지 않는다. 읽지 못한 파드는
    `DRAIN_ATTEMPTS` 번까지만 읽는다."""
    import time

    _inst, fake = _instance_with_proxy(client, fake_clusters)
    healthy = "mcp-calc-1-7f9-abc"
    dead = [f"mcp-calc-1-dead-{i:02d}" for i in range(12)]
    for name in dead:
        fake.add_pod(DEFAULT_NS, name, PROXY_LABELS)
    col = audit_collector(client)
    client.portal.call(col.run_once)
    now = utcnow()
    for name in dead:
        _terminate_at(fake, name, now - timedelta(seconds=5))
    _terminate_at(fake, healthy, now + timedelta(seconds=30))
    fake.failing_pod_logs.update(dead)
    fake.pod_log_failure_delay = 0.3
    fake.add_pod_log(DEFAULT_NS, healthy, _event("healthy", _iso(utcnow())))
    # 12 개를 4 개씩 0.3 초면 0.9 초라 0.5 초의 시간을 넘긴다
    monkeypatch.setattr(collector_module, "DRAIN_BUDGET", 0.5)
    fake.log_reads.clear()
    started = time.monotonic()
    assert client.portal.call(col.drain_once) == {"pods": 1, "events": 1, "errors": 0}
    assert time.monotonic() - started < 0.85
    assert len(fake.log_reads) < 1 + len(dead)  # 시간이 다 돼 남은 읽기를 버렸다

    lists = len(fake.pod_metadata_lists)
    for _ in range(DRAIN_ATTEMPTS + 4):
        assert client.portal.call(col.drain_once)["errors"] == 0
    assert len(fake.pod_metadata_lists) > lists  # 클러스터는 계속 본다
    reads = [name for _ns, name, _since in fake.log_reads]
    assert all(reads.count(name) <= DRAIN_ATTEMPTS for name in dead)
    settled = len(reads)
    client.portal.call(col.drain_once)
    assert len(fake.log_reads) == settled + 1  # 성한 파드만 읽는다


def test_a_failed_lease_check_is_logged_once_per_poll_period(
    client, fake_clusters, mongo, monkeypatch, caplog
):
    _inst, _fake = _instance_with_proxy(client, fake_clusters)
    col = audit_collector(client)
    client.portal.call(col.run_once)

    async def broken(*_a, **_kw):
        raise RuntimeError("store down")

    held = col.lease.held
    monkeypatch.setattr(col.lease, "held", broken)
    with caplog.at_level("WARNING", logger="daegam.audit.collector"):
        for _ in range(3):
            assert client.portal.call(col.drain_once) == {"pods": 0, "events": 0, "errors": 1}
        assert len([r for r in caplog.records if "lease check failed" in r.message]) == 1
        monkeypatch.setattr(col.lease, "held", held)
        client.portal.call(col.run_once)
        monkeypatch.setattr(col.lease, "held", broken)
        client.portal.call(col.drain_once)
        assert len([r for r in caplog.records if "lease check failed" in r.message]) == 2


def test_a_slow_cluster_is_cut_off_and_does_not_hold_the_others(
    client, fake_clusters, mongo, monkeypatch
):
    """클러스터마다 한 회차에 쓸 시간이 정해져 있다. 넘긴 클러스터는 다음 폴링까지 건너뛰고, 그동안
    다른 클러스터의 지워지는 파드는 읽힌다."""
    import time
    from types import SimpleNamespace

    class Stub:
        def __init__(self, delay: float):
            self.delay = delay
            self.lists = 0

        def list_metadata(self, _av, _kind, namespace, **_kw):
            self.lists += 1
            time.sleep(self.delay)
            meta = {
                "name": "mcp-x-1",
                "namespace": "ns-x",
                "uid": "uid-x",
                "labels": PROXY_LABELS,
                "deletionTimestamp": _iso(utcnow()),
            }
            return [{"metadata": meta}] if _kw["labels"] == {"toolhive": "true"} else []

        def pod_log(self, *_a, **_kw):
            return _event("from-ok", _iso(utcnow()))

    stubs = {"c-slow": Stub(0.6), "c-ok": Stub(0.0)}
    col = audit_collector(client)
    client.portal.call(col.run_once)  # 리스를 쥔다
    monkeypatch.setattr(collector_module, "DRAIN_BUDGET", 0.2)
    monkeypatch.setattr(col, "cluster_service", SimpleNamespace(client_for=lambda c: stubs[c.id]))
    col._targets = [(SimpleNamespace(id=cid, name=cid), "ns-x", {}) for cid in ("c-slow", "c-ok")]
    started = time.monotonic()
    assert client.portal.call(col.drain_once) == {"pods": 1, "events": 1, "errors": 1}
    assert time.monotonic() - started < 0.5
    assert mongo["mcp_audit_events"].find_one({"_id": "from-ok"})["cluster_id"] == "c-ok"
    # 느린 클러스터는 다음 폴링까지 부르지 않는다
    time.sleep(0.6)
    lists = stubs["c-slow"].lists
    assert client.portal.call(col.drain_once)["errors"] == 0
    assert stubs["c-slow"].lists == lists


def test_the_short_pass_stops_when_another_replica_took_the_lease(client, fake_clusters, mongo):
    _inst, fake = _instance_with_proxy(client, fake_clusters)
    col = audit_collector(client, holder="pod-a")
    client.portal.call(col.run_once)
    _terminate(fake, "mcp-calc-1-7f9-abc")
    mongo[LEASES].update_one({"_id": LEASE_NAME}, {"$set": {"holder": "pod-b"}})
    assert client.portal.call(col.drain_once) == {"pods": 0, "events": 0, "errors": 0}
    assert fake.pod_metadata_lists == [] and fake.log_reads[1:] == []


def test_a_failed_previous_log_read_is_retried(client, fake_clusters, mongo):
    """앞 컨테이너의 로그를 읽지 못하면(없다는 답이 아닌 실패) 재시작 수를 적지 않는다. 다음 읽기가
    그 로그를 다시 읽는다."""
    inst, fake = _instance_with_proxy(client, fake_clusters)
    pod = "mcp-calc-1-7f9-abc"
    fake.set_status(
        "v1",
        "Pod",
        pod,
        DEFAULT_NS,
        {"phase": "Running", "containerStatuses": [{"name": "mcp", "restartCount": 0}]},
    )
    now = utcnow()
    col = audit_collector(client)
    client.portal.call(col.run_once)
    fake.add_pod_log(DEFAULT_NS, pod, _event("old", _iso(now + timedelta(seconds=1))), ts=now)
    fake.restart_pod_container(DEFAULT_NS, pod)
    fake.add_pod_log(DEFAULT_NS, pod, _event("new", _iso(now + timedelta(seconds=2))), ts=now)
    key = f"{inst['cluster_id']}/{DEFAULT_NS}/{pod}"

    fake.previous_log_fails = True
    assert client.portal.call(col.run_once) == {
        "namespaces": 1,
        "pods": 0,
        "events": 0,
        "errors": [],
    }
    assert mongo["audit_cursors"].find_one({"_id": key})["restarts"] == 0
    fake.previous_log_fails = False
    assert client.portal.call(col.run_once)["events"] == 2
    assert mongo["audit_cursors"].find_one({"_id": key})["restarts"] == 1
    assert mongo["mcp_audit_events"].count_documents({"_id": {"$in": ["old", "new"]}}) == 2


def test_restart_count_of_the_container_that_is_read():
    restarts = collector_module._restarts
    pod = {
        "metadata": {},
        "status": {
            "containerStatuses": [
                {"name": "agent", "restartCount": 2},
                {"name": "sidecar", "restartCount": 7},
            ]
        },
    }
    assert restarts(pod, "agent") == 2
    assert restarts(pod, "sidecar") == 7
    assert restarts(pod, None) is None  # 어느 컨테이너를 읽는지 모른다
    assert restarts({"metadata": {"name": "p"}}, "agent") is None
