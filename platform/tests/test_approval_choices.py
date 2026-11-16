"""사람이 기억한 승인 선택이다(P43 5절, `services/approval_choices.py`). 규칙, 기억하기와 전달은
가짜 저장소로, 저장·전달·계정 화면의 라우트는 가짜 런타임과 MongoDB 로 본다."""

from __future__ import annotations

import asyncio
from datetime import timedelta
from typing import Any

import pytest

from daegam.errors import ServiceError
from daegam.services.agent_features import TOOL_SAFETY
from daegam.services.approval_choices import (
    REMEMBER_REFUSED,
    ApprovalChoiceService,
    ChoiceRule,
    ChoiceRules,
    strip_remember,
)
from daegam.services.turn_grants import GrantPerson
from daegam.services.turn_person import TurnPlan
from daegam.store.agent_session_owners import SessionOwner
from daegam.store.agents import Agent, AgentRuntime, AgentSpec, ToolPin
from daegam.store.approval_choices import ApprovalChoice
from daegam.store.base import utcnow
from tests.helpers.agent_runtime import deployed_agent, set_ready
from tests.helpers.agents import agent_body, node_of, template_for, template_spec
from tests.helpers.consts import AG
from tests.helpers.docs import agent_doc
from tests.helpers.http import api_client, make_app
from tests.helpers.runs import run_of, waiting_run_of

NODE = "tools-calculator"
IMAGE = "daegam-agent-runtime:test"
ALICE = GrantPerson(sub="alice", username="alice", tv=None)
# 실행 기록이 있는 세션이다
RUN_SESSION = "run-s1"


def _spec(**tools: Any) -> dict[str, Any]:
    """템플릿 spec 이다. 그 계산기 노드는 Add 와 Divide 를 주고 Divide 에 승인 규칙을 둔다."""
    spec = template_spec()
    node_of(spec, NODE)["data"].update(tools)
    return spec


def _rules(spec: dict[str, Any], pins: dict[str, ToolPin] | None = None) -> ChoiceRules:
    return ChoiceRules.of(AgentSpec.model_validate(spec), pins or {})


def _rule(rules: ChoiceRules, name: str) -> ChoiceRule:
    rule = rules.rule(name)
    assert rule is not None, name
    return rule


def _deployed(spec: dict[str, Any], *, features: tuple[str, ...] = (TOOL_SAFETY,)) -> Agent:
    return agent_doc(
        "ag-1",
        spec=spec,
        state="deployed",
        effective_config={"images": {"agent_image": IMAGE}},
        runtime=AgentRuntime(image=IMAGE, features=list(features)),
    )


class FakeChoiceStore:
    """`MongoApprovalChoiceStore` 의 `live` 만 흉내 낸다."""

    def __init__(self, *choices: ApprovalChoice) -> None:
        self.choices = list(choices)

    async def live(self, sub: str, agent_id: str, now: Any) -> list[ApprovalChoice]:
        return [c for c in self.choices if c.sub == sub and c.agent_id == agent_id]


class FakeRunStore:
    """`RUN_SESSION` 만 실행의 세션이다."""

    async def for_session(self, tenant: str, agent_id: str, session_id: str) -> object | None:
        return object() if session_id == RUN_SESSION else None


class FakeOwnerStore:
    """모든 세션의 소유자가 alice 다."""

    async def get(self, agent_id: str, session_id: str) -> SessionOwner:
        now = utcnow()
        return SessionOwner(
            agent_id=agent_id,
            session_id=session_id,
            sub="alice",
            confirmed_at=now,
            last_turn_at=now,
            expires_at=now + timedelta(days=1),
        )


def _service(*choices: ApprovalChoice) -> ApprovalChoiceService:
    return ApprovalChoiceService(
        FakeChoiceStore(*choices),  # type: ignore[arg-type]
        FakeRunStore(),  # type: ignore[arg-type]
        FakeOwnerStore(),  # type: ignore[arg-type]
        days=30,
    )


def _choice(tool: str, choice: str, digest: str, sub: str = "alice") -> ApprovalChoice:
    now = utcnow()
    return ApprovalChoice(
        tenant="default",
        sub=sub,
        agent_id="ag-1",
        tool=tool,
        choice=choice,  # type: ignore[arg-type]
        digest=digest,
        created_at=now,
        expires_at=now + timedelta(days=1),
    )


# --- 규칙 ------------------------------------------------------------------------------------


def test_an_approve_needs_a_rememberable_rule_on_every_node_offering_the_name() -> None:
    off = _rules(_spec())
    on = _rules(_spec(rememberable=["Divide"]))
    assert _rule(off, "Divide").approve is False and _rule(on, "Divide").approve is True
    # 규칙이나 rememberable 이나 소스가 바뀌면 요약이 바뀌어 앞의 선택이 무효다
    assert _rule(off, "Divide").digest != _rule(on, "Divide").digest
    changed = _rules(_spec(rememberable=["Divide"], approval={"Divide": ["approve"]}))
    assert _rule(changed, "Divide").digest != _rule(on, "Divide").digest
    moved = _rules(_spec(rememberable=["Divide"], source={"kind": "mcp_instance", "id": "ms-1"}))
    assert _rule(moved, "Divide").digest != _rule(on, "Divide").digest
    # 다른 Tool 의 규칙이 바뀌어도 그대로다
    assert _rule(off, "Add").digest == _rule(on, "Add").digest
    # 규칙이 없는 Tool 은 승인을 기억할 수 없고 거부는 기억한다
    assert _rule(on, "Add").approve is False
    # 소스가 주지 않는 이름과 늘 내장인 이름은 선택을 받지 않는다
    assert on.rule("Mul") is None
    any_name = _rules(_spec(allow_tools=[]), {NODE: ToolPin(names=None)})
    assert any_name.rule("write_file") is None and any_name.rule("execute") is None
    # Agent 에 따라 내장되는 이름은 소스 Tool 이면 받는다
    assert any_name.rule("task") is not None


def test_a_node_whose_names_are_unknown_may_offer_any_name() -> None:
    spec = _spec(rememberable=["Divide"])
    node_of(spec, NODE)["data"]["allow_tools"] = []
    rules = _rules(spec, {NODE: ToolPin(names=None)})
    assert _rule(rules, "Divide").approve is False
    assert rules.rule("Anything") is not None, "이름을 모르는 노드는 어떤 이름이든 줄 수 있다"
    pinned = _rules(spec, {NODE: ToolPin(names=["Divide"])})
    assert _rule(pinned, "Divide").approve is True and pinned.rule("Add") is None


def test_remember_is_stripped_before_the_runtime() -> None:
    decisions = [{"type": "approve", "remember": "approve"}, {"type": "reject"}]
    assert strip_remember(decisions) == [{"type": "approve"}, {"type": "reject"}]


def test_the_turn_owner_is_the_confirmed_person_or_the_extended_owner() -> None:
    doc = agent_doc("ag-1", spec=_spec())
    assert TurnPlan(doc, "s1", utcnow()).owner_sub is None
    assert TurnPlan(doc, "s1", utcnow(), confirms=ALICE).owner_sub == "alice"
    assert TurnPlan(doc, "s1", utcnow(), extends="bob").owner_sub == "bob"


# --- 기억하기 --------------------------------------------------------------------------------

ASKED = [{"name": "Divide", "args": {}, "reason": "rule"}]
APPROVE = [{"type": "approve", "remember": "approve"}]
REJECT = [{"type": "reject", "remember": "reject"}]


def _remember(
    doc: Agent,
    decisions: list[dict[str, Any]],
    actions: list[dict[str, Any]] = ASKED,
    *,
    owner: bool = True,
    session_id: str = "s1",
    pending_sub: str = "alice",
) -> list[ApprovalChoice]:
    plan = TurnPlan(doc, session_id, utcnow(), confirms=ALICE if owner else None)

    async def pending() -> dict[str, Any]:
        return {"user_sub": pending_sub, "action_requests": actions}

    return asyncio.run(_service().to_remember(plan, decisions, pending))


def _refused(doc: Agent, decisions: list[dict[str, Any]], **kw: Any) -> str:
    with pytest.raises(ServiceError) as e:
        _remember(doc, decisions, **kw)
    assert e.value.code == REMEMBER_REFUSED
    return str(e.value.detail)


def test_the_owner_remembers_an_allowed_approve_and_any_reject() -> None:
    doc = _deployed(_spec(rememberable=["Divide"]))
    [saved] = _remember(doc, APPROVE)
    assert (saved.tool, saved.choice, saved.sub) == ("Divide", "approve", "alice")
    assert saved.expires_at - saved.created_at == timedelta(days=30)
    plain = _deployed(_spec())
    [reject] = _remember(plain, REJECT)
    assert reject.choice == "reject"
    assert _remember(plain, [{"type": "approve"}]) == []
    # 실행 세션에서도 거부는 기억한다
    assert _remember(doc, REJECT, session_id=RUN_SESSION)


def test_each_refusal_of_remember() -> None:
    doc = _deployed(_spec(rememberable=["Divide"]))
    assert "owner" in _refused(doc, APPROVE, owner=False)
    assert "owner's" in _refused(doc, APPROVE, pending_sub="mallory")
    assert "match" in _refused(doc, [{"type": "edit", "remember": "approve"}])
    assert "match" in _refused(doc, [{"type": "approve", "remember": "reject"}])
    assert "no pending request" in _refused(doc, APPROVE, actions=[])
    assert "background" in _refused(doc, APPROVE, session_id=RUN_SESSION)
    outside = [{**ASKED[0], "reason": "outside"}]
    assert "outside content" in _refused(doc, APPROVE, actions=outside)
    assert "may not be remembered" in _refused(_deployed(_spec()), APPROVE)
    builtin = [{"name": "write_file", "reason": "rule"}]
    assert "tool source" in _refused(doc, APPROVE, actions=builtin)
    old = _deployed(_spec(rememberable=["Divide"]), features=())
    assert "image" in _refused(old, APPROVE)
    both = [*APPROVE, *REJECT]
    assert "both" in _refused(doc, both, actions=[*ASKED, *ASKED])


def test_a_non_owner_is_refused_before_the_pending_request_is_read() -> None:
    plan = TurnPlan(_deployed(_spec()), "s1", utcnow())

    async def pending() -> dict[str, Any]:
        raise AssertionError("the runtime must not be asked")

    with pytest.raises(ServiceError):
        asyncio.run(_service().to_remember(plan, APPROVE, pending))


CARD_ACTIONS = [
    ASKED[0],
    {**ASKED[0], "reason": "outside"},
    {"name": "Add", "reason": "rule"},
    {"name": "write_file", "reason": "rule"},
]


def _card(doc: Agent, caller: str = "alice", session_id: str = "s1", **pending: Any):
    body = {"user_sub": "alice", "action_requests": CARD_ACTIONS, **pending}
    return asyncio.run(_service().card_options(doc, session_id, caller, body))


@pytest.mark.parametrize("session_id", ["s1", RUN_SESSION])
def test_the_card_offers_exactly_the_choices_that_remembering_accepts(session_id: str) -> None:
    doc = _deployed(_spec(rememberable=["Divide"]))
    offered = _card(doc, session_id=session_id)
    assert offered is not None and len(offered) == len(CARD_ACTIONS)
    for action, choices in zip(CARD_ACTIONS, offered, strict=True):
        for choice in ("approve", "reject"):
            decision = [{"type": choice, "remember": choice}]
            accepted = True
            try:
                _remember(doc, decision, actions=[action], session_id=session_id)
            except ServiceError:
                accepted = False
            assert accepted is (choice in choices), (action, choice)
    assert offered[0] == (["reject"] if session_id == RUN_SESSION else ["approve", "reject"])


def test_the_card_offers_nothing_to_others_or_old_images() -> None:
    doc = _deployed(_spec(rememberable=["Divide"]))
    assert _card(doc, caller="bob") is None, "기록된 소유자가 아니다"
    assert _card(doc, user_sub="bob") is None, "대기 요청이 소유자의 것이 아니다"
    assert _card(_deployed(_spec(rememberable=["Divide"]), features=())) is None
    # 그 요청이 받는 결정만 낸다
    configs = [{"action_name": "Divide", "allowed_decisions": ["approve"]}]
    assert _card(doc, review_configs=configs)[0] == ["approve"]  # type: ignore[index]


# --- 전달 ------------------------------------------------------------------------------------


def test_only_choices_that_match_the_deployed_rules_travel() -> None:
    doc = _deployed(_spec(rememberable=["Divide"]))
    rules = ApprovalChoiceService.rules(doc)
    assert rules is not None
    digest = _rule(rules, "Divide").digest
    service = _service(
        _choice("Divide", "approve", digest),
        _choice("Add", "reject", _rule(rules, "Add").digest),
        _choice("Divide", "reject", "voided-by-a-redeploy", sub="bob"),
        _choice("Gone", "reject", "a-tool-no-source-offers"),
    )

    def turn(d: Agent, sub: str | None, session_id: str = "s1", **kw: Any):
        plan = TurnPlan(d, session_id, utcnow(), extends=sub)
        return asyncio.run(service.for_turn(plan, **kw))

    both = [{"tool": "Divide", "choice": "approve"}, {"tool": "Add", "choice": "reject"}]
    rejects = [{"tool": "Add", "choice": "reject"}]
    assert turn(doc, "alice") == both
    # 실행 세션에는 거부만 간다. 라우트가 안 실행이든 실행 기록으로 가린 세션이든 같다
    assert turn(doc, "alice", run=True) == rejects
    assert turn(doc, "alice", RUN_SESSION) == rejects
    assert asyncio.run(service.for_run_start(doc, "alice")) == rejects
    assert turn(doc, "bob") is None
    assert turn(doc, None) is None
    old = _deployed(_spec(rememberable=["Divide"]), features=())
    assert turn(old, "alice") is None


# --- 저장과 라우트 ---------------------------------------------------------------------------


def _rememberable(gw: dict[str, Any], ds: dict[str, Any]) -> dict[str, Any]:
    """템플릿의 Divide 승인 규칙을 기억할 수 있게 한 spec 이다."""
    spec = template_for(gw, ds)
    node_of(spec, NODE)["data"]["rememberable"] = ["Divide"]
    return spec


def _last(agent, suffix: str) -> dict[str, Any]:
    return next(c for c in reversed(agent.runtime.state.calls) if c["path"].endswith(suffix))


def _seed_owner(client, agent_id: str, session_id: str, sub: str) -> None:
    """그 세션의 소유자 기록이다. 소유자 자신의 턴이 받아들여진 것과 같다."""
    now = utcnow()
    owner = SessionOwner(
        agent_id=agent_id,
        session_id=session_id,
        sub=sub,
        confirmed_at=now,
        last_turn_at=now,
        expires_at=now + timedelta(days=1),
    )
    assert client.portal.call(client.app.state.agent_session_owners.confirm, owner)


def test_a_remembered_choice_is_saved_listed_delivered_and_deleted(
    client, fake_clusters, mongo
) -> None:
    agent = deployed_agent(
        client, fake_clusters, mongo, spec=_rememberable, features=f"cancel,{TOOL_SAFETY}"
    )
    _seed_owner(client, agent.id, "s1", "header")
    body = {"decisions": [{"type": "approve", "remember": "approve"}]}
    r = client.post(f"{AG}/{agent.id}/sessions/s1/decisions", json=body)
    assert r.status_code == 200, r.text
    assert _last(agent, "/decisions")["body"]["decisions"] == [{"type": "approve"}]
    [stored] = list(mongo["approval_choices"].find())
    assert (stored["tool"], stored["choice"], stored["sub"]) == ("Divide", "approve", "header")

    [mine] = client.get("/me/approval-choices").json()["choices"]
    assert mine["tool"] == "Divide" and mine["agent_name"] == agent.doc["name"]
    assert "digest" not in mine and "sub" not in mine

    r = client.post(f"{AG}/{agent.id}/chat", json={"message": "hi", "session_id": "s1"})
    assert r.status_code == 200, r.text
    delivered = _last(agent, "/chat")["body"]["approval_choices"]
    assert delivered == [{"tool": "Divide", "choice": "approve"}]

    assert client.delete(f"/me/approval-choices/{mine['id']}").status_code == 204
    assert client.get("/me/approval-choices").json()["choices"] == []
    assert client.delete(f"/me/approval-choices/{mine['id']}").status_code == 404
    r = client.post(f"{AG}/{agent.id}/chat", json={"message": "hi", "session_id": "s1"})
    assert r.status_code == 200, r.text
    assert "approval_choices" not in _last(agent, "/chat")["body"]

    # 저장소는 도구마다 하나를 두고, 테넌트를 주면 그 안의 것만 잊고, 남의 것은 지우지 않는다
    store = client.app.state.approval_choices.store
    first = _choice("Divide", "approve", "d1")
    client.portal.call(store.put, first)
    again = client.portal.call(store.put, _choice("Divide", "reject", "d2"))
    assert again.id == first.id and again.choice == "reject"
    other = _choice("Add", "reject", "d3").model_copy(update={"tenant": "acme"})
    client.portal.call(store.put, other)
    assert client.portal.call(store.delete_for_person, "alice", "acme") == 1
    [left] = client.portal.call(store.list_mine, "alice", utcnow())
    assert left.tool == "Divide"
    assert client.portal.call(store.delete_mine, "bob", left.id) is None
    assert client.portal.call(store.delete_for_person, "alice") == 1


def test_pending_offers_remember_choices_only_to_the_owner(client, fake_clusters, mongo) -> None:
    agent = deployed_agent(
        client, fake_clusters, mongo, spec=_rememberable, features=f"cancel,{TOOL_SAFETY}"
    )
    r = client.get(f"{AG}/{agent.id}/sessions/s1/pending")
    assert r.status_code == 200, r.text
    assert r.json().get("remember_options") is None, "소유자 기록이 없으면 고를 것이 없다"
    # 기억하겠다는 결정도 런타임에 가기 전에 거절한다. 소유자를 확인하기 전에는 대기 요청도 읽지
    # 않는다
    before = len(agent.runtime.state.calls)
    remember = {"decisions": [{"type": "approve", "remember": "approve"}]}
    r = client.post(f"{AG}/{agent.id}/sessions/s1/decisions", json=remember)
    assert r.status_code == 422, r.text
    detail = r.json()["detail"]
    assert detail["code"] == REMEMBER_REFUSED and "owner" in detail["detail"]
    assert agent.runtime.state.calls[before:] == []
    assert mongo["approval_choices"].count_documents({}) == 0
    _seed_owner(client, agent.id, "s1", "header")
    body = client.get(f"{AG}/{agent.id}/sessions/s1/pending").json()
    # 가짜 런타임의 요청은 승인만 받으므로(review_configs) 기억한 승인만 낸다
    assert body["remember_options"] == [["approve"]] and body["remember_days"] == 30


@pytest.mark.parametrize("runs_enabled", [True, False])
def test_a_run_session_gets_no_remembered_approvals(
    settings, mongo, fake_clusters, fake_secrets, runs_enabled
) -> None:
    """실행을 켰든 껐든 실행 기록이 있는 세션에는 기억한 승인이 가지 않고 기억되지도 않는다.
    켠 배포본의 결정은 기다리는 실행만 옮기므로 그때는 기다리는 실행을 둔다(P41 7절)."""
    app = make_app(settings, fake_clusters, fake_secrets, runs_enabled=runs_enabled)
    with api_client(app) as client:
        agent = deployed_agent(
            client, fake_clusters, mongo, spec=_rememberable, features=f"cancel,{TOOL_SAFETY}"
        )
        _seed_owner(client, agent.id, "s1", "header")
        _seed_owner(client, agent.id, "s2", "header")
        runs = client.app.state.approval_choices.runs
        seeded = (waiting_run_of if runs_enabled else run_of)(agent_id=agent.id, session_id="s2")
        assert client.portal.call(runs.insert, seeded)
        body = {"decisions": [{"type": "approve", "remember": "approve"}]}
        r = client.post(f"{AG}/{agent.id}/sessions/s2/decisions", json=body)
        assert r.status_code == 422 and "background" in r.json()["detail"]["detail"], r.text
        assert client.post(f"{AG}/{agent.id}/sessions/s1/decisions", json=body).status_code == 200
        for session_id, sent in (("s1", [{"tool": "Divide", "choice": "approve"}]), ("s2", None)):
            message = {"message": "hi", "session_id": session_id}
            assert client.post(f"{AG}/{agent.id}/chat", json=message).status_code == 200
            assert _last(agent, "/chat")["body"].get("approval_choices") == sent
        if runs_enabled:
            # 받지 못한 결정은 실행을 다시 기다리게 둔다
            run = client.portal.call(runs.for_session, seeded.tenant, agent.id, "s2")
            assert run is not None and run.status == "waiting_approval"


def test_a_redeploy_that_changes_the_rule_sweeps_the_choice(client, fake_clusters, mongo) -> None:
    agent = deployed_agent(
        client, fake_clusters, mongo, spec=_rememberable, features=f"cancel,{TOOL_SAFETY}"
    )
    _seed_owner(client, agent.id, "s1", "header")
    body = {"decisions": [{"type": "reject", "remember": "reject"}]}
    assert client.post(f"{AG}/{agent.id}/sessions/s1/decisions", json=body).status_code == 200
    assert mongo["approval_choices"].count_documents({"tool": "Divide"}) == 1
    plain = agent.spec()
    node_of(plain, NODE)["data"].pop("rememberable")
    assert client.put(f"{AG}/{agent.id}", json=agent_body(plain)).status_code == 200
    set_ready(agent.fake)
    r = client.post(f"{AG}/{agent.id}/redeploy")
    assert r.status_code == 200, r.text
    assert mongo["approval_choices"].count_documents({}) == 0
