"""P14 F1b: 비서 API 의 흐름 (stub provider, fake 클러스터).

config 논리곱과 assistant_disabled, 세션 CRUD 와 소유·테넌트, fetch_url 참조가 verify 로
이어지고 파일로 남는 흐름, navigate·resource 이벤트와 감사, 도구 인자 비밀 마스킹, backend
부터 agent 배포까지의 전체 흐름을 든다.
"""

from __future__ import annotations

import dataclasses
import json

import httpx

from daegam.urlfetch import BlockedURL
from tests.helpers.assistant import (
    STUB,
    WSDL,
    decide,
    enable,
    names,
    new_session,
    say,
    session_doc,
    steps,
    tool,
    turn,
)
from tests.helpers.consts import TENANT_HEADER, A
from tests.helpers.http import detail_code

# -- config · 비활성 ----------------------------------------------------------------------------


def test_config_is_the_conjunction_of_flag_and_provider_and_disabled_is_404(client):
    r = client.get(f"{A}/config")
    assert r.status_code == 200
    assert r.json() == {
        "enabled": False,
        "provider": None,
        "providers": [],
        "confirm_deploy": True,
        "limits": {"max_tool_calls_per_turn": 24, "turn_timeout_seconds": 300},
    }
    for call in (
        lambda: client.get(f"{A}/sessions"),
        lambda: client.post(f"{A}/sessions", json={}),
        lambda: client.get(f"{A}/sessions/x"),
        lambda: client.delete(f"{A}/sessions/x"),
        lambda: client.post(f"{A}/sessions/x/turns", json={"message": "hi"}),
        lambda: client.post(
            f"{A}/sessions/x/decisions", json={"decisions": [{"id": "a", "type": "approve"}]}
        ),
    ):
        r = call()
        assert detail_code(r) == (404, "assistant_disabled"), r.text
    # 켜졌지만 provider 가 없으면 enabled=false 이고 세션 API 는 열리되 턴이 422
    enable(client, providers=(), turn_timeout_seconds=60)
    cfg = client.get(f"{A}/config").json()
    assert cfg["enabled"] is False and cfg["provider"] is None
    assert cfg["limits"]["turn_timeout_seconds"] == 60
    sid = new_session(client)
    r = client.post(f"{A}/sessions/{sid}/turns", json={"message": "hi"})
    assert detail_code(r) == (422, "provider_unavailable")
    # provider 해소: defaults.assistant → defaults.spec_generator → 첫 항목
    other = {"name": "ant", "kind": "anthropic", "api_key_secret": "llm-keys"}
    enable(client, providers=(STUB, other))
    # assistant 절은 통째로 바뀌므로 앞서 준 60 초는 기본값으로 돌아간다
    assert client.get(f"{A}/config").json() == {
        "enabled": True,
        "provider": "local-stub",
        # 고를 수 있는 것만 싣는다. 키 값도 Secret 이름도 없다 (D45)
        "providers": [
            {"name": "local-stub", "kind": "stub", "model": "stub"},
            {"name": "ant", "kind": "anthropic", "model": "claude-opus-5"},
        ],
        "confirm_deploy": True,
        "limits": {"max_tool_calls_per_turn": 24, "turn_timeout_seconds": 300},
    }
    enable(client, providers=(STUB, other), defaults={"spec_generator": "ant"})
    assert client.get(f"{A}/config").json()["provider"] == "ant"
    enable(
        client,
        providers=(STUB, other),
        defaults={"spec_generator": "ant", "assistant": "local-stub"},
    )
    assert client.get(f"{A}/config").json()["provider"] == "local-stub"
    # stub 은 배포본이 허용할 때만 해소된다
    svc = client.app.state.assistant
    svc.settings = dataclasses.replace(svc.settings, allow_stub_provider=False)
    cfg = client.get(f"{A}/config").json()
    assert cfg["enabled"] is False and cfg["provider"] is None
    # 비서가 꺼진 셈이라 고를 목록도 비어 있다
    assert cfg["providers"] == []
    svc.settings = client.app.state.settings
    # Secret 이 없는 provider 는 이름은 해소되지만 턴에서 422 provider_unavailable
    other = {**other, "api_key_secret": "missing"}
    enable(client, providers=(other,), defaults={"assistant": "ant"})
    assert client.get(f"{A}/config").json()["provider"] == "ant"
    r = client.post(f"{A}/sessions/{sid}/turns", json={"message": "hi"})
    assert detail_code(r) == (422, "provider_unavailable")


# -- 세션 ------------------------------------------------------------------------------------


def test_session_crud_ownership_and_tenant(client, mongo):
    enable(client)
    assert client.get(f"{A}/sessions").json() == {"items": []}
    sid = new_session(client, "첫 대화")
    other = new_session(client, headers={TENANT_HEADER: "acme"})
    items = client.get(f"{A}/sessions").json()["items"]
    assert [i["id"] for i in items] == [sid]
    assert items[0]["title"] == "첫 대화" and items[0]["state"] == "idle" and items[0]["updated_at"]
    assert [
        i["id"]
        for i in client.get(f"{A}/sessions", headers={TENANT_HEADER: "acme"}).json()["items"]
    ] == [other]
    body = client.get(f"{A}/sessions/{sid}").json()
    assert body == {
        "id": sid,
        "title": "첫 대화",
        "state": "idle",
        "provider": None,
        "pending": None,
        "transcript": [],
        "turns": 0,
        "usage": {
            "input_tokens": 0,
            "output_tokens": 0,
            "total_tokens": 0,
            "cached_input_tokens": 0,
        },
        "updated_at": body["updated_at"],
    }
    r = client.get(f"{A}/sessions/{sid}", headers={TENANT_HEADER: "acme"})
    assert detail_code(r) == (409, "tenant_mismatch")
    assert r.json()["detail"]["session_tenant"] == "default"
    r = client.get(f"{A}/sessions/nope")
    assert detail_code(r) == (404, "session_not_found")
    doc = session_doc(mongo, sid)
    assert doc["tenant"] == "default" and doc["sub"] == "header" and doc["files"] == []
    # 색인: (tenant, sub, updated_at) 과 TTL 7일
    info = mongo["assistant_sessions"].index_information()
    assert info["ix_tenant_sub_updated"]["key"] == [("tenant", 1), ("sub", 1), ("updated_at", -1)]
    assert info["ttl_updated_at"]["expireAfterSeconds"] == 7 * 24 * 3600
    assert client.delete(f"{A}/sessions/{sid}").json() == {"id": sid, "deleted": True}
    assert client.get(f"{A}/sessions/{sid}").status_code == 404
    assert client.post(f"{A}/sessions", json={"title": "x" * 201}).status_code == 422


def test_tenant_mismatch_on_turn_and_decision(client):
    enable(client)
    sid = new_session(client)
    r = client.post(
        f"{A}/sessions/{sid}/turns", json={"message": "hi"}, headers={TENANT_HEADER: "acme"}
    )
    assert detail_code(r) == (409, "tenant_mismatch")
    r = decide(client, sid, [{"id": "x", "type": "approve"}], headers={TENANT_HEADER: "acme"})
    assert detail_code(r) == (409, "tenant_mismatch")


# -- 파일 참조·화면 이벤트·감사 ----------------------------------------------------------------


def test_fetch_url_reference_flows_into_verify_and_files_persist(client, mongo):
    enable(client)
    svc = client.app.state.assistant
    hosts = {"api.example.com": ["93.184.216.34"]}

    def resolver(host: str) -> list[str]:
        if host not in hosts:
            raise BlockedURL("url_unresolvable", host)
        return hosts[host]

    svc.executor.resolver = resolver
    svc.executor.fetch_transport = httpx.MockTransport(
        lambda req: httpx.Response(200, content=WSDL.encode(), headers={"content-type": "text/xml"})
    )
    sid = new_session(client)
    script = steps(
        tool("fetch_url", url="https://api.example.com/calc.wsdl"),
        tool("verify_spec", text="$0.ref", filename="calc.wsdl"),
        say("파일 $0.ref"),
    )
    events = turn(client, sid, script)
    assert names(events) == [
        "tool_call",
        "tool_result",
        "tool_call",
        "tool_result",
        "token",
        "done",
    ]
    fetched = json.loads(events[1][1]["content"])
    # 모델이 보는 것은 우리가 만든 값뿐이다. 본문은 한 글자도 없다(D31)
    assert fetched == {
        "ref": "$file:fetched-1.xml",
        "url": "https://api.example.com/calc.wsdl",
        "content_type": "text/xml",
        "bytes": len(WSDL.encode()),
        "kind": "wsdl",
        "format": "xml",
        "operation_count": 2,
    }
    assert "CalculatorPortType" not in events[1][1]["content"]
    # 모델에는 참조가 갔고 실행기가 본문으로 바꿔 verify 가 통과했다
    assert events[2][1]["args"] == {"text": "$file:fetched-1.xml", "filename": "calc.wsdl"}
    assert events[3][1]["status"] == "success"
    assert events[5][1]["text"] == "파일 $file:fetched-1.xml"
    doc = session_doc(mongo, sid)
    assert doc["files"] == [
        {"name": "fetched-1.xml", "text": WSDL, "url": "https://api.example.com/calc.wsdl"}
    ]
    # 다음 턴에서도 파일이 살아 있다
    events = turn(
        client,
        sid,
        steps(tool("verify_spec", text="$file:fetched-1.xml", filename="c.wsdl"), say("검증 끝")),
    )
    assert events[1][1]["status"] == "success" and events[-1][1]["text"] == "검증 끝"


def test_ui_context_navigate_and_resource_events(client, mongo):
    enable(client)
    sid = new_session(client)
    script = steps(
        tool("navigate", to="/data-services/new"),
        tool(
            "create_backend_api",
            name="calc",
            display_name="계산기",
            endpoint="http://mock-soap.svc:18080/calc",
            spec_text=WSDL,
            filename="calculator.wsdl",
        ),
        say("만듦 $1.id"),
    )
    events = turn(client, sid, script, context={"route": "/backend-apis"})
    assert names(events) == [
        "tool_call",
        "tool_result",
        "ui",
        "tool_call",
        "tool_result",
        "resource",
        "token",
        "done",
    ]
    assert events[2][1] == {"type": "navigate", "to": "/data-services/new"}
    resource = events[5][1]
    assert resource["kind"] == "backend-api" and resource["name"] == "calc"
    # 카드는 사람이 붙인 이름을 앞세운다. K8s 이름과 다를 때만 실린다
    assert resource["display_name"] == "계산기"
    assert resource["url"] == f"/backend-apis/{resource['id']}"
    assert events[-1][1]["text"] == f"만듦 {resource['id']}"
    body = client.get(f"{A}/sessions/{sid}").json()
    kinds = [it["kind"] for it in body["transcript"]]
    # ui 는 전사에 없고 resource 는 있다
    assert kinds == ["user", "tool_call", "tool_call", "resource", "assistant", "done"]
    assert body["transcript"][3]["resource"] == resource
    # 감사: 비서의 쓰기 호출이 via 로 남는다. 세션을 지워도 남는다
    rows = client.get("/audit", params={"via": f"assistant/{sid}"}).json()
    assert [r["action"] for r in rows] == ["upload_spec"]
    assert rows[0]["request_id"].endswith(".2") and rows[0]["sub"] == "header"
    assert client.delete(f"{A}/sessions/{sid}").status_code == 200
    assert len(client.get("/audit", params={"via": f"assistant/{sid}"}).json()) == 1


# -- 비밀 마스킹 ------------------------------------------------------------------------------


def test_tool_argument_secrets_are_masked_everywhere_but_the_user_message(client, mongo):
    enable(client)
    sid = new_session(client)
    secret = "hunter2-top-secret"
    script = steps(
        tool(
            "create_data_service",
            name="db",
            kind="postgres",
            host="pg.svc",
            port=5432,
            password=secret,
        ),
        say("결과 $0.code"),
    )
    events = turn(client, sid, script)
    assert names(events) == ["tool_call", "tool_result", "token", "done"]
    assert events[0][1]["args"]["password"] == "***" and events[0][1]["args"]["name"] == "db"
    assert json.loads(events[1][1]["content"])["code"] == "secret_in_chat"
    assert events[3][1]["text"].startswith("결과 secret_in_chat")
    assert secret not in json.dumps(events[1:], ensure_ascii=False)
    doc = session_doc(mongo, sid)
    for item in doc["transcript"]:
        if item["kind"] != "user":
            assert secret not in json.dumps(item, default=str), item
    for m in doc["context"]:
        if m["type"] != "human":
            assert secret not in json.dumps(m, default=str), m
    # 실행기는 원본으로 판정해 API 를 부르지 않았다
    assert client.get("/data-services").json() == []
    # 한계: 사용자 메시지 자체(전사 user 항목·HumanMessage·제목)에는 남는다
    assert secret in doc["transcript"][0]["text"]
    assert secret in doc["context"][0]["data"]["content"]


# -- 전체 흐름 ----------------------------------------------------------------------------------
