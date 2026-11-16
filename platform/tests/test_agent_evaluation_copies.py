"""P45 B2: 대화에서 복사한 평가 사례와 그 동의 (8절).

복사는 대화한 사람이 평가에 동의를 켰을 때만 되고 평가를 받은 턴의 질문과 답만 담으며, 개인정보로
보이는 것은 자리표로 바뀐다. 복사본은 저장할 때마다 동의를 다시 보고, 어느 칸에든 개인정보로 보이는
것이 남으면 거절된다. 손으로 쓴 사례는 확인을 받고 저장한다. 평가를 고치면 복사본의 TTL 이 따라
옮기고, 동의를 끄거나 평가를 거두면 복사본이 지워진다. 가짜 런타임의 세션 상세가 전사를 싣고
Mongo 는 진짜다.
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any

import pytest

from daegam.store.base import utcnow
from tests.helpers.agent_runtime import DeployedAgent, deployed_agent
from tests.helpers.agents import agent_fixture, node_of, template_for
from tests.helpers.http import detail_code
from tests.helpers.runtime import with_transcript

TURN_ID = agent_fixture("chat/turn.json")["events"]["done"]["turn_id"]
SID = "s1"
QUESTION = "제 번호 010-1234-5678 로 환불 결과를 알려 줄 수 있어?"
ANSWER = "네, 등록하신 kim@corp.kr 과 사번 EMP-12345 로 안내하겠습니다."
TRANSCRIPT: list[dict[str, Any]] = [
    {"kind": "assistant", "text": "안녕하세요.", "streaming": False, "greeting": True},
    {"kind": "user", "text": "먼저 물어본 질문"},
    {"kind": "assistant", "text": "먼저 한 답", "streaming": False},
    {"kind": "done", "turn": 1, "tool_calls": 0, "turn_id": "earlier", "usage": {}},
    {"kind": "user", "text": QUESTION},
    {"kind": "assistant", "text": "하위 에이전트의 말", "agent": "helper", "streaming": False},
    {"kind": "assistant", "text": ANSWER, "streaming": False},
    {"kind": "done", "turn": 2, "tool_calls": 0, "turn_id": TURN_ID, "usage": {}},
]
GRADER = {"provider": "local-stub", "model": "stub"}


def _with_rule(gw: dict[str, Any], ds: dict[str, Any]) -> dict[str, Any]:
    """입력 가드레일에 사번 정규식을 더한 템플릿이다."""
    spec = template_for(gw, ds)
    rules = node_of(spec, "guardrail-input")["data"]["rules"]
    rules.append({"kind": "pii_mask", "value": {"types": ["regex"], "regex": r"EMP-\d{5}"}})
    return spec


@pytest.fixture
def agent(client, fake_clusters, mongo) -> DeployedAgent:
    deployed = deployed_agent(client, fake_clusters, mongo, spec=_with_rule)
    with_transcript(deployed.runtime, TRANSCRIPT)
    return deployed


def _rate(client, agent: DeployedAgent, **body: Any):
    url = f"{agent.url}/sessions/{SID}/turns/{TURN_ID}/feedback"
    return client.put(url, json={"rating": "up", **body})


def _set(client, agent: DeployedAgent) -> str:
    r = client.post(f"{agent.url}/evaluation-sets", json={"name": "복사본", "grader": GRADER})
    assert r.status_code == 201, r.text
    return r.json()["id"]


def _draft(client, agent: DeployedAgent, set_id: str, turn_id: str = TURN_ID):
    url = f"{agent.url}/evaluation-sets/{set_id}/cases/from-turn"
    return client.post(url, json={"session_id": SID, "turn_id": turn_id})


def _save(client, agent: DeployedAgent, set_id: str, body: dict[str, Any]):
    return client.post(f"{agent.url}/evaluation-sets/{set_id}/cases", json=body)


def _count(client, agent: DeployedAgent, set_id: str) -> int:
    return client.get(f"{agent.url}/evaluation-sets/{set_id}").json()["case_count"]


def test_a_copy_needs_the_chatting_persons_consent(client, agent, mongo):
    set_id = _set(client, agent)
    assert detail_code(_draft(client, agent, set_id)) == (409, "evaluation_consent_missing")
    assert _rate(client, agent).status_code == 200
    assert detail_code(_draft(client, agent, set_id)) == (409, "evaluation_consent_missing")
    r = _rate(client, agent, rating="down", evaluation_use=True)
    assert r.status_code == 200 and r.json()["evaluation_use"] is True

    r = _draft(client, agent, set_id)
    assert r.status_code == 200, r.text
    draft = r.json()
    # 그 턴의 질문과 메인 에이전트의 답만, 개인정보는 자리표로 바뀐다
    assert draft["content"]["messages"] == ["제 번호 [phone] 로 환불 결과를 알려 줄 수 있어?"]
    assert draft["content"]["reference"] == {
        "text": "네, 등록하신 [email] 과 사번 [masked] 로 안내하겠습니다.",
        "good": False,
    }
    assert draft["replaced"] == {"phone": 1, "email": 1, "masked": 1}
    rating_id = mongo["agent_feedback"].find_one({"turn_id": TURN_ID})["_id"]
    assert draft["origin"] == {"rating_id": rating_id, "session_id": SID, "turn_id": TURN_ID}
    # 아직 저장되지 않았다
    assert _count(client, agent, set_id) == 0
    # 전사에는 있지만 평가가 없는 턴. 전사에 없는 턴은 아래 시험이 본다
    assert detail_code(_draft(client, agent, set_id, turn_id="earlier")) == (
        409,
        "evaluation_consent_missing",
    )


def test_a_saved_copy_lives_with_its_rating(client, agent, mongo):
    set_id = _set(client, agent)
    _rate(client, agent, evaluation_use=True)
    draft = _draft(client, agent, set_id).json()
    body = {"content": draft["content"], "origin": draft["origin"]}
    r = _save(client, agent, set_id, body)
    assert r.status_code == 201, r.text
    case = r.json()
    rating = mongo["agent_feedback"].find_one({"turn_id": TURN_ID})
    stored = mongo["evaluation_cases"].find_one({"_id": case["id"]})
    assert stored["expires_at"] == rating["expires_at"] and stored["origin"] == draft["origin"]
    assert "user_sub" not in stored and stored["pii_ack"] is None

    # 평가를 고치면 복사본의 기한이 따라 옮긴다
    mongo["agent_feedback"].update_one(
        {"_id": rating["_id"]}, {"$set": {"expires_at": utcnow() - timedelta(days=1)}}
    )
    assert _rate(client, agent, evaluation_use=True, comment="다시").status_code == 200
    moved = mongo["agent_feedback"].find_one({"_id": rating["_id"]})["expires_at"]
    assert mongo["evaluation_cases"].find_one({"_id": case["id"]})["expires_at"] == moved

    # 동의를 끄면 복사본이 지워지고 세트의 자리가 돌아온다
    assert _rate(client, agent, evaluation_use=False).status_code == 200
    assert mongo["evaluation_cases"].find_one({"_id": case["id"]}) is None
    assert _count(client, agent, set_id) == 0


def test_withdrawing_the_rating_deletes_its_copies(client, agent, mongo):
    set_id = _set(client, agent)
    _rate(client, agent, evaluation_use=True)
    draft = _draft(client, agent, set_id).json()
    case = _save(client, agent, set_id, {"content": draft["content"], "origin": draft["origin"]})
    url = f"{agent.url}/sessions/{SID}/turns/{TURN_ID}/feedback"
    assert client.delete(url).status_code == 204
    assert mongo["evaluation_cases"].find_one({"_id": case.json()["id"]}) is None
    # 평가가 없으면 그 출처로 저장하지 못한다
    r = _save(client, agent, set_id, {"content": draft["content"], "origin": draft["origin"]})
    assert detail_code(r) == (409, "evaluation_consent_missing")
    assert _count(client, agent, set_id) == 0


@pytest.mark.parametrize(
    "edit",
    [
        lambda c: c["messages"].append("제 카드는 4111-1111-1111-1111"),
        lambda c: c.update(checks=[{"kind": "answer_contains", "text": "010-1234-5678"}]),
        lambda c: c.update(tool_results={"Notify": "kim@corp.kr 로 보냄"}),
        lambda c: c.update(criteria=["EMP-12345 를 말하지 않는다"]),
    ],
)
def test_a_copy_refuses_personal_information_in_any_field(client, agent, edit):
    """복사한 글에서 지운 것을 다른 칸으로 옮겨도 거절한다. 확인으로는 저장되지 않는다."""
    set_id = _set(client, agent)
    _rate(client, agent, evaluation_use=True)
    draft = _draft(client, agent, set_id).json()
    edit(draft["content"])
    body = {"content": draft["content"], "origin": draft["origin"], "pii_acknowledged": True}
    r = _save(client, agent, set_id, body)
    assert detail_code(r) == (422, "evaluation_case_pii")
    detail = r.json()["detail"]
    assert detail["acknowledgeable"] is False and len(detail["matches"]) == 1
    assert _count(client, agent, set_id) == 0


def test_a_superseded_rating_is_not_copied_and_stops_saves(client, agent, mongo):
    set_id = _set(client, agent)
    _rate(client, agent, evaluation_use=True)
    draft = _draft(client, agent, set_id).json()
    case = _save(client, agent, set_id, {"content": draft["content"], "origin": draft["origin"]})
    mongo["agent_feedback"].update_one({"turn_id": TURN_ID}, {"$set": {"superseded": True}})
    assert detail_code(_draft(client, agent, set_id)) == (404, "turn_not_found")
    url = f"{agent.url}/evaluation-sets/{set_id}/cases/{case.json()['id']}"
    r = client.put(url, json={"content": draft["content"]})
    assert detail_code(r) == (409, "evaluation_consent_missing")


def test_hand_written_cases_save_after_the_acknowledgement(client, agent, mongo):
    set_id = _set(client, agent)
    content = {"messages": ["주문번호 2026111412345 의 배송 상태를 알려 주세요"]}
    r = _save(client, agent, set_id, {"content": content})
    assert detail_code(r) == (422, "evaluation_case_pii")
    assert r.json()["detail"]["acknowledgeable"] is True
    assert r.json()["detail"]["matches"] == [
        {"kind": "id_number", "place": "messages[0]", "start": 5, "end": 18}
    ]
    r = _save(client, agent, set_id, {"content": content, "pii_acknowledged": True})
    assert r.status_code == 201, r.text
    assert r.json()["pii_ack"]["by"] == "header" and r.json()["expires_at"] is None
    # 개인정보로 보이는 것이 없으면 확인도 남지 않는다
    url = f"{agent.url}/evaluation-sets/{set_id}/cases/{r.json()['id']}"
    mail = {"messages": ["test@example.com 으로 회의 초대를 보내 주세요"]}
    r = client.put(url, json={"content": mail})
    assert r.status_code == 200 and r.json()["pii_ack"] is None


def test_withdraw_person_ends_the_consents_of_one_tenant(client, agent, mongo):
    """계정을 지우거나 소속을 잃은 사람의 동의를 거두고 복사본을 지운다."""
    set_id = _set(client, agent)
    _rate(client, agent, evaluation_use=True)
    draft = _draft(client, agent, set_id).json()
    _save(client, agent, set_id, {"content": draft["content"], "origin": draft["origin"]})
    copies = client.app.state.evaluation_copies
    assert client.portal.call(copies.withdraw_person, "header", "other-tenant") == 0
    assert client.portal.call(copies.withdraw_person, "header", "default") == 1
    assert mongo["agent_feedback"].find_one({"turn_id": TURN_ID})["evaluation_use"] is False
    assert mongo["evaluation_cases"].count_documents({"origin": {"$exists": True}}) == 0


def test_a_rated_turn_missing_from_the_transcript_is_404(client, agent, mongo):
    """동의한 평가가 있어도 전사에 그 턴이 없으면(보존 기간이 지났다) 복사할 것이 없다."""
    set_id = _set(client, agent)
    _rate(client, agent, evaluation_use=True)
    ghost = mongo["agent_feedback"].find_one({"turn_id": TURN_ID})
    mongo["agent_feedback"].insert_one({**ghost, "_id": "r-ghost", "turn_id": "ghost"})
    assert detail_code(_draft(client, agent, set_id, turn_id="ghost")) == (404, "turn_not_found")


def test_a_turn_longer_than_a_case_holds_is_422(client, agent):
    set_id = _set(client, agent)
    with_transcript(
        agent.runtime,
        [
            {"kind": "user", "text": "q" * 8001},
            {"kind": "assistant", "text": "a", "streaming": False},
            {"kind": "done", "turn": 1, "tool_calls": 0, "turn_id": TURN_ID, "usage": {}},
        ],
    )
    _rate(client, agent, evaluation_use=True)
    r = _draft(client, agent, set_id)
    assert detail_code(r) == (422, "invalid_value")
    assert (r.json()["detail"]["field"], r.json()["detail"]["max"]) == ("messages[0]", 8000)


def test_a_consent_withdrawn_during_the_save_leaves_no_copy(client, agent, mongo, monkeypatch):
    """넣은 뒤 동의를 다시 본다. 넣는 사이 동의가 끝났으면 넣은 것을 지우고 409 다."""
    set_id = _set(client, agent)
    _rate(client, agent, evaluation_use=True)
    draft = _draft(client, agent, set_id).json()
    store = client.app.state.evaluation_sets.store
    insert = store.insert_case

    async def insert_then_withdraw(case):
        stored = await insert(case)
        mongo["agent_feedback"].update_one(
            {"turn_id": TURN_ID}, {"$set": {"evaluation_use": False}}
        )
        return stored

    monkeypatch.setattr(store, "insert_case", insert_then_withdraw)
    r = _save(client, agent, set_id, {"content": draft["content"], "origin": draft["origin"]})
    assert detail_code(r) == (409, "evaluation_consent_missing")
    assert mongo["evaluation_cases"].count_documents({}) == 0
    assert _count(client, agent, set_id) == 0


def test_the_list_hides_copies_whose_rating_no_longer_consents(client, agent, mongo):
    set_id = _set(client, agent)
    _rate(client, agent, evaluation_use=True)
    draft = _draft(client, agent, set_id).json()
    _save(client, agent, set_id, {"content": draft["content"], "origin": draft["origin"]})
    url = f"{agent.url}/evaluation-sets/{set_id}/cases"
    before = client.get(url).json()
    assert len(before["items"]) == 1
    # 훅을 거치지 않고 동의가 꺼졌다. 정리가 지우기 전에도 목록에 없다
    mongo["agent_feedback"].update_one({"turn_id": TURN_ID}, {"$set": {"evaluation_use": False}})
    after = client.get(url).json()
    assert after["items"] == [] and after["set_digest"] == before["set_digest"]


def test_a_rating_flip_moves_the_reference_and_its_digest(client, agent, mongo):
    set_id = _set(client, agent)
    _rate(client, agent, evaluation_use=True)
    draft = _draft(client, agent, set_id).json()
    case = _save(client, agent, set_id, {"content": draft["content"], "origin": draft["origin"]})
    assert case.json()["content"]["reference"]["good"] is True
    _rate(client, agent, rating="down", evaluation_use=True)
    stored = mongo["evaluation_cases"].find_one({"_id": case.json()["id"]})
    assert stored["content"]["reference"]["good"] is False
    assert stored["digest"] != case.json()["digest"]
