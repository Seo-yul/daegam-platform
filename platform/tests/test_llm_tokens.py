"""LLM 게이트웨이의 부르는 쪽 토큰과 회수 (P38 5절·6절).

토큰의 모양, Agent 토큰 Secret 의 세 번째 토큰과 그 판, 발급 기록과 회수 목록, 교체와 롤아웃 뒤의
정리, 해제의 회수, 스윕의 갱신 시점을 본다. 게이트웨이 경로로 옮겨 가는 흐름과 API 의 교체는
test_llm_route.py 가 본다.
"""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from daegam.auth.service_token import LLM_TOKEN_TTL, PLATFORM_CALL_TTL, claims_current
from daegam.k8s.templates.agent import LLM_TOKEN_KEY, render_agent_token_secret
from daegam.services.agent_tokens import stamp_svc_tokens
from daegam.services.llm_tokens import LlmTokenError, LlmTokens
from daegam.store.agents import LlmCleanup, LlmRoute, MongoAgentStore
from daegam.store.base import utcnow
from daegam.store.llm_revocations import MongoLlmRevocationStore
from tests.helpers.agent_runtime import deployed_agent, minimal_agent_doc
from tests.helpers.aio import run_db
from tests.helpers.http import detail_code
from tests.helpers.svc_tokens import claims_of, make_svc_tokens

LLM = "daegam-llm"


def test_an_agent_token_opens_only_its_providers_at_the_llm_audience():
    svc = make_svc_tokens()
    token = svc.mint_llm(
        name="agent-bot", tenant="acme", agent_id="ag-1", providers=["b", "a", "a"]
    )
    claims = claims_of(token)
    assert claims["aud"] == LLM and claims["iss"] == svc.issuer.issuer
    assert (claims["sub"], claims["tenant"], claims["agent_id"]) == (
        "svc:agent:agent-bot",
        "acme",
        "ag-1",
    )
    assert claims["llm_providers"] == ["a", "b"] and claims["groups"] == []
    assert claims["exp"] - claims["iat"] == LLM_TOKEN_TTL


def test_a_platform_token_is_minted_per_request_for_one_provider():
    claims = claims_of(make_svc_tokens().platform_llm_bearer("acme", "spec_generator", "openai"))
    assert (claims["sub"], claims["purpose"], claims["llm_providers"]) == (
        "svc:platform",
        "spec_generator",
        ["openai"],
    )
    assert claims["exp"] - claims["iat"] == PLATFORM_CALL_TTL


def test_an_llm_token_needs_a_provider_and_a_tenant():
    svc = make_svc_tokens()
    with pytest.raises(ValueError):
        svc.mint_llm(name="x", tenant="acme", agent_id="a", providers=[])
    with pytest.raises(ValueError):
        svc.mint_llm(name="x", tenant="", agent_id="a", providers=["p"])


def test_claims_current_also_compares_the_providers():
    svc = make_svc_tokens()
    token = svc.mint_llm(name="x", tenant="acme", agent_id="a", providers=["p", "q"])
    wanted = {"iss": svc.issuer.issuer, "aud": LLM, "groups": []}
    assert claims_current(token, **wanted, llm_providers=["q", "p"])
    assert not claims_current(token, **wanted, llm_providers=["p"])
    assert claims_current(token, **wanted)


def test_the_third_token_joins_the_secret_and_the_rev_only_when_present():
    """llm_token 이 없는 Agent 의 Secret 과 판은 이 단계 앞과 같다 (P38 Goals 1)."""
    svc = make_svc_tokens()
    inst = minimal_agent_doc()
    plain = render_agent_token_secret(inst, "t", mcp_token="m.c.p")
    assert LLM_TOKEN_KEY not in plain["stringData"]
    llm = svc.mint_llm(name="x", tenant="acme", agent_id=inst.id, providers=["p"])
    assert (
        render_agent_token_secret(inst, "t", mcp_token="m", llm_token=llm)["stringData"][
            LLM_TOKEN_KEY
        ]
        == llm
    )
    mcp = svc.issuer.mint(sub="svc:agent:x", tenant="acme", groups=[])
    stamp_svc_tokens(inst, mcp, None, {})
    before = inst.svc_token_rev
    stamp_svc_tokens(inst, mcp, None, {}, None)
    assert inst.svc_token_rev == before
    stamp_svc_tokens(inst, mcp, None, {}, llm)
    assert inst.svc_token_rev != before and inst.svc_token_rev.startswith(before)


def test_the_sweep_renews_an_llm_token_thirty_days_before_it_ends():
    from daegam.services.svc_token_sweep import LLM_RENEW_BEFORE, SvcTokenSweeper

    svc = make_svc_tokens()
    sweeper = SvcTokenSweeper(
        svc, gateways=(None, None), mcp_instances=(None, None), agents=(None, None)
    )  # type: ignore[arg-type]
    token = svc.mint_llm(name="x", tenant="acme", agent_id="a", providers=["p"])
    now = utcnow()
    assert not sweeper.needs_renewal(token, now + timedelta(days=59), LLM_RENEW_BEFORE)
    assert sweeper.needs_renewal(token, now + timedelta(days=61), LLM_RENEW_BEFORE)


# -- 기록과 회수 (Mongo) ---------------------------------------------------------------------


class _Client:
    """Deployment 하나, 그 파드, 토큰 Secret 키 빼기만 흉내 낸다."""

    def __init__(self, deployment: dict | None, pods: list[dict] | None = None):
        self.deployment = deployment
        self.pods = pods or []
        self.dropped: list[list[str]] = []

    def get(self, *_args):
        return self.deployment

    def list(self, *_args):
        return self.pods

    def drop_secret_keys(self, _name, _ns, keys):
        self.dropped.append(keys)
        return True


def _pod(template: str, *, born: str, deleting: bool = False) -> dict:
    meta = {"labels": {"pod-template-hash": template}, "creationTimestamp": born}
    if deleting:
        meta["deletionTimestamp"] = "2026-11-09T10:00:00Z"
    return {"metadata": meta, "status": {"phase": "Running"}}


def _rolled(generation: int, *, done: bool) -> dict:
    status = {"observedGeneration": generation, "replicas": 1, "updatedReplicas": 1}
    status["availableReplicas"] = 1 if done else 0
    return {"metadata": {"generation": generation}, "spec": {"replicas": 1}, "status": status}


async def _tokens(
    db, client: _Client
) -> tuple[LlmTokens, MongoAgentStore, MongoLlmRevocationStore]:
    agents, revocations = MongoAgentStore(db), MongoLlmRevocationStore(db)
    await revocations.init_schema()

    async def client_for(_doc):
        return client

    return (
        LlmTokens(agents, revocations, make_svc_tokens(), client_for_doc=client_for),
        agents,
        revocations,
    )


def test_minting_records_before_the_secret_and_undeploy_revokes_every_live_token(settings):
    async def go(db):
        tokens, agents, revocations = await _tokens(db, _Client(None))
        doc = minimal_agent_doc()
        await agents.create(doc)
        old = await tokens.mint(doc, ["p"])
        new = await tokens.mint(doc, ["p", "q"])
        stored = await agents.get(doc.id)
        assert [r.jti for r in stored.llm_tokens] == [claims_of(old)["jti"], claims_of(new)["jti"]]
        synced: list[bool] = []

        async def on_revoked():
            synced.append(True)

        tokens.on_revoked = on_revoked
        assert await tokens.revoke_all(stored) == 2
        assert (await agents.get(doc.id)).llm_tokens == []
        listed = await revocations.listed(datetime(2000, 1, 1))
        assert sorted(listed) == sorted([claims_of(old)["jti"], claims_of(new)["jti"]])
        assert synced == [True]
        # 켜기 전에 낸 토큰은 목록에 싣지 않는다. 리스너의 iat 규칙이 막는다
        assert await revocations.listed(utcnow() + timedelta(seconds=5)) == []

    run_db(settings, go)


async def _revoke_after(agents: MongoAgentStore, doc, jti: str, *, generation: int) -> None:
    """그 세대의 롤아웃 뒤에 jti 를 회수하는 정리를 남긴다."""
    assert await agents.add_llm_cleanup(
        doc.id, LlmCleanup(revoke=[jti], generation=generation), states={"deployed"}
    )


def test_a_rotation_waits_for_the_rollout_unless_immediate(settings):
    client = _Client(_rolled(3, done=False))

    async def go(db):
        tokens, agents, revocations = await _tokens(db, client)
        doc = minimal_agent_doc()
        await agents.create(doc)
        await tokens.mint(doc, ["p"])
        old = doc.llm_tokens[0]
        await tokens.mint(doc, ["p"])
        # 다시 렌더한 적용이 옛 토큰의 회수를 롤아웃 뒤로 남겼다(`AgentLlm.after_apply`)
        await _revoke_after(agents, doc, old.jti, generation=3)
        await tokens.after_rotation(doc, doc.llm_tokens[1].jti, immediate=False)
        assert await tokens.holding()
        assert await tokens.settle() == []
        assert await revocations.listed(datetime(2000, 1, 1)) == []
        client.deployment = _rolled(3, done=True)
        assert await tokens.settle() == [doc.id]
        assert await revocations.listed(datetime(2000, 1, 1)) == [old.jti]
        assert (await agents.get(doc.id)).llm_cleanup == [] and not await tokens.holding()
        # 곧바로 회수는 새 토큰 말고 살아 있는 토큰을 모두 회수한다. 앞서 갱신이 바꾼 토큰도다
        other = minimal_agent_doc(id="ag-2", tenant="beta")
        await agents.create(other)
        for _ in range(3):
            await tokens.mint(other, ["p"])
        first, renewed, new = (r.jti for r in other.llm_tokens)
        await tokens.after_rotation(other, new, immediate=True)
        listed = await revocations.listed(datetime(2000, 1, 1))
        assert first in listed and renewed in listed and new not in listed

    run_db(settings, go)


def test_a_cleanup_drops_secret_keys_after_the_rollout(settings):
    client = _Client(_rolled(2, done=True))

    async def go(db):
        tokens, agents, _ = await _tokens(db, client)
        doc = minimal_agent_doc()
        doc.llm_cleanup = [LlmCleanup(drop_keys=[LLM_TOKEN_KEY], generation=2)]
        await agents.create(doc)
        assert await tokens.settle() == [doc.id]

    run_db(settings, go)
    assert client.dropped == [[LLM_TOKEN_KEY]]


# -- API ------------------------------------------------------------------------------------


def test_rotating_a_direct_agent_is_a_conflict(client, fake_clusters, mongo):
    agent = deployed_agent(client, fake_clusters, mongo, runtime=False)
    r = client.post(f"{agent.url}/llm-token/rotate", json={"immediate": True})
    assert detail_code(r) == (409, "llm_token_missing")


def test_undeploy_revokes_the_agent_llm_tokens(client, fake_clusters, mongo):
    agent = deployed_agent(client, fake_clusters, mongo, runtime=False)
    exp = utcnow() + timedelta(days=90)
    record = {"jti": "jti-live", "iat": utcnow(), "exp": exp}
    mongo["agents"].update_one({"_id": agent.id}, {"$set": {"llm_tokens": [record]}})
    assert client.post(f"{agent.url}/undeploy").status_code == 200
    assert mongo["llm_revocations"].find_one({"_id": "jti-live"}) is not None
    assert mongo["agents"].find_one({"_id": agent.id})["llm_tokens"] == []


def test_cleanup_waits_while_an_old_pod_still_drains(settings):
    """`rolled_out` 은 지워지는 옛 파드를 세지 않는다. 그 파드는 preStop 동안 옛 토큰으로 부르므로
    옛 ReplicaSet 의 파드가 다 없어진 뒤에 회수한다."""
    old = _pod("old", born="2026-11-09T09:00:00Z", deleting=True)
    new = _pod("new", born="2026-11-09T09:30:00Z")
    client = _Client(_rolled(2, done=True), [old, new])

    async def go(db):
        tokens, agents, revocations = await _tokens(db, client)
        doc = minimal_agent_doc()
        await agents.create(doc)
        await tokens.mint(doc, ["p"])
        await _revoke_after(agents, doc, doc.llm_tokens[0].jti, generation=2)
        assert await tokens.settle() == []
        client.pods = [new]
        assert await tokens.settle() == [doc.id]
        assert await revocations.listed(datetime(2000, 1, 1)) == [doc.llm_tokens[0].jti]

    run_db(settings, go)


def test_a_stale_transition_drops_no_token_record_or_cleanup(settings):
    """상태 전이가 읽어 둔 옛 문서로 저장해도 그 사이 더한 기록과 정리는 남는다."""

    async def go(db):
        tokens, agents, _ = await _tokens(db, _Client(None))
        doc = minimal_agent_doc()
        await agents.create(doc)
        stale = await agents.get(doc.id)
        await tokens.mint(doc, ["p"])
        await _revoke_after(agents, doc, doc.llm_tokens[0].jti, generation=1)
        assert await agents.transition(stale, expect={"deployed"})
        fresh = await agents.get(doc.id)
        assert [r.jti for r in fresh.llm_tokens] == [doc.llm_tokens[0].jti]
        assert len(fresh.llm_cleanup) == 1

    run_db(settings, go)


def test_a_mint_without_a_live_deployment_is_refused(settings):
    """기록을 남기지 못하는 토큰은 회수할 수 없으므로 내지 않는다."""

    async def go(db):
        tokens, agents, _ = await _tokens(db, _Client(None))
        doc = minimal_agent_doc()
        doc.state = "undeploying"
        await agents.create(doc)
        with pytest.raises(LlmTokenError) as e:
            await tokens.mint(doc, ["p"])
        assert e.value.code == "state_conflict"

    run_db(settings, go)


def _token_secret(token: str) -> dict:
    import base64

    return {"data": {LLM_TOKEN_KEY: base64.b64encode(token.encode()).decode()}}


@pytest.mark.parametrize("outcome", ["claim_held", "secret_unchanged"])
def test_a_rotation_that_did_not_rerender_revokes_nothing(settings, outcome):
    """다른 작업이 클레임을 쥐어 다시 렌더가 돌지 않았거나 Secret 의 토큰이 그대로면 409 이고
    아무것도 회수하지 않는다. 그 토큰은 아직 파드가 쓰는 것이다."""
    client = _Client(None)

    async def go(db):
        tokens, agents, revocations = await _tokens(db, client)
        doc = minimal_agent_doc(llm_route=LlmRoute(route="gateway", providers=["p"]))
        await agents.create(doc)
        token = await tokens.mint(doc, ["p"])
        client.deployment = _token_secret(token)

        async def rerender(_doc):
            return outcome == "secret_unchanged"

        with pytest.raises(LlmTokenError) as e:
            await tokens.rotate(doc, immediate=True, rerender=rerender)
        assert e.value.code == "in_progress"
        assert await revocations.listed(datetime(2000, 1, 1)) == []

    run_db(settings, go)


def test_a_held_token_is_reissued_when_the_graph_providers_change():
    """뺀 provider 는 재배포·교체·갱신을 지나 열린 채 남지 않는다."""
    from daegam.services.agent_tokens import AgentTokens

    svc = make_svc_tokens()
    tokens = AgentTokens.__new__(AgentTokens)
    tokens.svc_tokens = svc
    held = svc.mint_llm(name="x", tenant="acme", agent_id="a", providers=["p", "q"])
    secret = {"stringData": {LLM_TOKEN_KEY: held}}
    doc = minimal_agent_doc()
    assert tokens.kept_llm_token(doc, secret, wanted=None) == (held, ["p", "q"])
    assert tokens.kept_llm_token(doc, secret, wanted=["q", "p"]) == (held, ["q", "p"])
    assert tokens.kept_llm_token(doc, secret, wanted=["p"]) == (None, ["p"])
    assert tokens.kept_llm_token(doc, {}, wanted=None) == (None, None)


def test_the_sweep_reissues_an_llm_token_whose_providers_differ():
    import asyncio

    from daegam.services.svc_token_sweep import (
        LLM_RENEW_BEFORE,
        HeldToken,
        HeldTokens,
        SvcTokenSweeper,
    )

    svc = make_svc_tokens()
    sweeper = SvcTokenSweeper(
        svc, gateways=(None, None), mcp_instances=(None, None), agents=(None, None)
    )  # type: ignore[arg-type]
    token = svc.mint_llm(name="x", tenant="acme", agent_id="a", providers=["p", "q"])

    def due(wanted):
        held = HeldTokens(
            (HeldToken(token, kind="llm", renew_before=LLM_RENEW_BEFORE, wanted=wanted),)
        )
        return asyncio.run(sweeper._due(held, "acme", utcnow()))

    assert not due(None) and not due(("q", "p")) and due(("p",))
