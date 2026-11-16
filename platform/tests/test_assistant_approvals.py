"""P14 F1b: 비서의 승인과 질문 (stub provider).

승인·거절·대체(superseded)와 결정 검사, ask_user 와 미뤄진(deferred) 승인, viewer 의 403(완료 조건
2)을 든다. 가져온 파일이 얹힌 쓰기의 승인 상향은 test_assistant_loop.py 가 DB 없이 본다.
"""

from __future__ import annotations

import json

from daegam.services.assistant.stub import LAST_ERROR_FORMAT
from tests.helpers.assistant import (
    approve_all,
    assistant_leases,
    decide,
    decided,
    enable,
    names,
    new_session,
    say,
    session_doc,
    steps,
    tool,
    turn,
)
from tests.helpers.auth import bearer, map_groups
from tests.helpers.consts import GROUP_MAPPINGS, A
from tests.helpers.http import detail_code


def test_approval_flow_reject_superseded_and_decision_validation(client, mongo):
    enable(client)
    sid = new_session(client)
    # 승인 대기 전에는 결정이 409
    r = decide(client, sid, [{"id": "x", "type": "approve"}])
    assert detail_code(r) == (409, "not_pending")
    script = steps(
        {
            "tools": [
                tool("list_clusters"),
                tool("deploy_mcp_server", server_id="p"),
            ]
        },
        say("결과 $1.code"),
    )
    events = turn(client, sid, script)
    assert names(events) == ["tool_call", "tool_result", "interrupt"]
    interrupt = events[2][1]
    assert interrupt["kind"] == "confirm" and interrupt["session_id"] == sid
    assert [a["name"] for a in interrupt["action_requests"]] == ["deploy_mcp_server"]
    body = client.get(f"{A}/sessions/{sid}").json()
    assert body["state"] == "pending" and body["pending"]["kind"] == "confirm"
    (call,) = body["pending"]["calls"]
    assert call["name"] == "deploy_mcp_server" and call["risk"] == "deploy"
    assert call["args"] == {"server_id": "p"}
    assert (
        body["transcript"][-1]["kind"] == "interrupt"
        and body["transcript"][-1]["resolved"] is False
    )
    # pending 은 interrupt 를 내기 전에 저장됐다. 리스는 풀렸다
    doc = session_doc(mongo, sid)
    assert doc["pending"]["calls"][0]["id"] == call["id"] and assistant_leases(mongo) == []
    # 결정 검사
    r = decide(client, sid, [{"id": call["id"], "type": "approve"}, {"id": "y", "type": "reject"}])
    assert detail_code(r) == (422, "decision_count_mismatch")
    assert r.json()["detail"]["expected"] == [call["id"]]
    r = decide(client, sid, [{"id": "wrong", "type": "approve"}])
    assert detail_code(r) == (422, "decision_mismatch")
    assert decide(client, sid, []).status_code == 422
    assert decide(client, sid, [{"id": call["id"], "type": "edit"}]).status_code == 422
    # 승인: 그 호출의 tool_call·tool_result 가 나고(프로필이 없어 실패) 모델이 이어 말한다
    events = decided(client, sid, [{"id": call["id"], "type": "approve"}])
    assert names(events) == ["tool_call", "tool_result", "token", "done"]
    assert events[0][1]["id"] == call["id"]
    failed = json.loads(events[1][1]["content"])
    assert events[1][1]["status"] == "error" and failed["status"] in (404, 422)
    assert events[3][1]["text"].startswith(f"결과 {failed['code']}")
    body = client.get(f"{A}/sessions/{sid}").json()
    assert body["state"] == "idle" and body["pending"] is None
    assert [it["resolved"] for it in body["transcript"] if it["kind"] == "interrupt"] == [True]
    # 거절: 사유가 ToolMessage 로 돌아간다
    events = turn(
        client,
        sid,
        steps(tool("deploy_mcp_server", server_id="p"), say("$0.code: $0.detail")),
    )
    assert names(events) == ["interrupt"]
    cid = client.get(f"{A}/sessions/{sid}").json()["pending"]["calls"][0]["id"]
    events = decided(client, sid, [{"id": cid, "type": "reject", "message": "다음에"}])
    assert names(events) == ["tool_call", "tool_result", "token", "done"]
    assert events[3][1]["text"].startswith("rejected: 다음에")
    # 대체: 승인 없이 새 메시지가 오면 대기가 rejected(superseded) 로 닫힌 뒤 새 턴이 돈다
    events = turn(client, sid, steps(tool("delete_mcp_server", server_id="i"), say("x")))
    assert names(events) == ["interrupt"]
    events = turn(client, sid, steps(say("새 말")))
    assert names(events) == ["tool_call", "tool_result", "token", "done"]
    assert events[0][1]["name"] == "delete_mcp_server"
    assert "superseded" in json.loads(events[1][1]["content"])["detail"]
    body = client.get(f"{A}/sessions/{sid}").json()
    assert body["pending"] is None and body["state"] == "idle"
    assert all(it["resolved"] for it in body["transcript"] if it["kind"] == "interrupt")
    doc = session_doc(mongo, sid)
    types = [m["type"] for m in doc["context"]]
    # 마지막 구간: ... ai(delete) tool(superseded) human(새 말) ai
    assert types[-4:] == ["ai", "tool", "human", "ai"]


def test_ask_user_and_deferred_approvals(client, mongo):
    enable(client)
    sid = new_session(client)
    script = steps(
        {
            "tools": [
                tool("deploy_mcp_server", server_id="p"),
                tool("ask_user", question="어느 클러스터?", options=["edge"], field="cluster_id"),
            ]
        },
        say("답 $1.answer, 배포 $0.code"),
    )
    events = turn(client, sid, script)
    assert names(events) == ["tool_call", "tool_result", "interrupt"]
    assert events[0][1]["name"] == "deploy_mcp_server"
    assert json.loads(events[1][1]["content"])["code"] == "deferred"
    interrupt = events[2][1]
    assert interrupt["kind"] == "question" and interrupt["action_requests"] == []
    assert interrupt["question"] == {
        "text": "어느 클러스터?",
        "options": ["edge"],
        "field": "cluster_id",
    }
    body = client.get(f"{A}/sessions/{sid}").json()
    assert body["state"] == "pending" and body["pending"]["kind"] == "question"
    assert body["pending"]["calls"][0]["name"] == "ask_user"
    r = decide(client, sid, [{"id": body["pending"]["calls"][0]["id"], "type": "approve"}])
    assert detail_code(r) == (409, "not_pending")
    events = turn(client, sid, "edge")
    assert names(events) == ["token", "done"] and events[1][1]["text"] == "답 edge, 배포 deferred"
    doc = session_doc(mongo, sid)
    assert [m["type"] for m in doc["context"]] == ["human", "ai", "tool", "tool", "ai"]
    assert doc["context"][3]["data"]["name"] == "ask_user"
    assert json.loads(doc["context"][3]["data"]["content"]) == {"answer": "edge"}
    assert doc["pending"] is None and doc["state"] == "idle"
    assert [it["kind"] for it in doc["transcript"]] == [
        "user",
        "tool_call",
        "interrupt",
        "user",
        "assistant",
        "done",
    ]


# -- 완료 조건 2: viewer --------------------------------------------------------------------------


def test_viewer_gets_403_from_deploy_tool_and_daegam_says_so(oidc_client, token, mongo):
    map_groups(mongo, GROUP_MAPPINGS)
    admin = bearer(token("root", "daegam-admins"))
    enable(oidc_client, headers=admin)
    viewer = bearer(token("viewer1", "daegam-a-view"), tenant="a")
    assert oidc_client.get(f"{A}/config", headers=viewer).json()["enabled"] is True
    assert oidc_client.get(f"{A}/sessions").status_code == 401
    sid = new_session(oidc_client, headers=viewer)
    script = steps(
        tool("list_mcp_servers"),
        tool("deploy_mcp_server", server_id="p"),
        say("배포 결과: $1.code"),
    )
    events = turn(oidc_client, sid, script, headers=viewer)
    assert names(events) == ["tool_call", "tool_result", "interrupt"]
    assert events[1][1]["status"] == "success"
    events = approve_all(oidc_client, sid, viewer)
    assert names(events) == ["tool_call", "tool_result", "token", "done"]
    result_ = json.loads(events[1][1]["content"])
    assert events[1][1]["status"] == "error"
    assert result_["status"] == 403 and result_["code"] == "forbidden"
    assert result_["scope"] == "mcp:deploy" and result_["tenant"] == "a"
    text = events[-1][1]["text"]
    assert text == "배포 결과: forbidden" + LAST_ERROR_FORMAT.format(code="forbidden")
    doc = session_doc(mongo, sid)
    assert doc["sub"] == "viewer1" and doc["tenant"] == "a"
    # 세션은 만든 사람만 본다
    other = bearer(token("dev1", "daegam-a-dev"), tenant="a")
    assert oidc_client.get(f"{A}/sessions/{sid}", headers=other).status_code == 404
    assert oidc_client.get(f"{A}/sessions", headers=other).json() == {"items": []}
    # 감사: 403 도 via 로 남는다 (쓰기 요청이므로)
    rows = oidc_client.get("/audit", params={"via": f"assistant/{sid}"}, headers=admin).json()
    assert [(r["action"], r["status"], r["sub"]) for r in rows] == [
        ("deploy_server", 403, "viewer1")
    ]
