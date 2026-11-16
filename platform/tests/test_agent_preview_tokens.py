"""미리 보기의 자격은 그 끝 + 10분에 끝나고 아무것도 갱신하지 않는다 (P44b 8절·9절, D107).

미리 보기의 서비스·Gateway·LLM 토큰은 문서가 정한 수명(`token_lifetime`)으로 나고 그 끝 전에 다시
낼 때가 오지 않는다(`renew_before`). 그래서 재적용, 렌더 번호, 회전, 서비스 토큰 스윕, LLM 교체가
열린 미리 보기의 토큰도 `svc_token_rev` 도 파드 템플릿도 바꾸지 않는다. 인스턴스 토큰은 만료가
없어 `pinned_agent` 가 끝 + 10분부터 거절한다. 토큰은 원본의 id 와 서비스 주체를 실어 Tool 규칙이
원본으로 대한다.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

from daegam.auth.service_token import InternalIssuer
from daegam.k8s.templates.agent import LLM_TOKEN_KEY
from daegam.k8s.templates.svc_token import SVC_TOKEN_REV_ANNOTATION
from daegam.services.agent_tokens import AgentTokens
from daegam.services.llm_tokens import LlmTokens
from daegam.store.agents import Agent, MongoAgentStore
from daegam.store.base import from_epoch, utcnow
from daegam.store.llm_revocations import MongoLlmRevocationStore
from daegam.store.turn_grants import GrantTarget, TurnGrant
from tests.helpers.agent_runtime import agent_deployment, deployed_agent, token_of
from tests.helpers.aio import run_db
from tests.helpers.auth import bearer
from tests.helpers.consts import AG, DEFAULT_NS
from tests.helpers.previews import deployed_preview, plain_spec
from tests.helpers.svc_tokens import claims_of, make_svc_tokens
from tests.helpers.workers import run_data_plane_auth


def _secret(fake, doc: Agent) -> dict[str, str]:
    assert doc.k8s is not None
    return fake.get("v1", "Secret", doc.k8s.token_secret, DEFAULT_NS)["stringData"]


def _template(fake, doc: Agent) -> dict[str, Any]:
    assert doc.k8s is not None
    return agent_deployment(fake, doc.k8s.name)["spec"]["template"]


def _expires(token: str) -> datetime:
    peek = InternalIssuer.peek(token)
    assert peek is not None and peek.exp is not None
    return from_epoch(peek.exp)


def test_a_preview_holds_tokens_that_end_with_it_and_nothing_renews_them(
    local_admin, fake_clusters, mongo
):
    """서비스·Gateway 토큰은 끝 + 10분 전에 만료하고 원본의 id 와 주체를 싣는다. 재적용(렌더 번호의
    다시 렌더도 같은 길이다), 회전, 서비스 토큰 스윕이 토큰도 판도 파드 템플릿도 바꾸지 않는다."""
    client = local_admin
    agent = deployed_agent(client, fake_clusters, mongo, runtime=False, spec=plain_spec)
    preview = deployed_preview(client, agent)
    assert preview.preview is not None and preview.state == "deployed"
    end = preview.preview.credentials_end
    secret = _secret(agent.fake, preview)
    tokens = {k: secret[k] for k in ("mcp_token", "gateway_token")}
    for token in tokens.values():
        claims = claims_of(token)
        assert claims["agent_id"] == agent.id and claims["sub"] == "svc:agent:agent-support"
        assert utcnow() < _expires(token) <= end
    template = _template(agent.fake, preview)
    rev = template["metadata"]["annotations"][SVC_TOKEN_REV_ANNOTATION]

    service = client.app.state.agent_service

    def unchanged() -> None:
        now = _secret(agent.fake, preview)
        assert {k: now[k] for k in tokens} == tokens
        assert _template(agent.fake, preview) == template
        stored = mongo["agents"].find_one({"_id": preview.id})
        assert stored["svc_token_rev"] == rev

    def fresh() -> Agent:
        doc = client.portal.call(service.instances.get, preview.id)
        assert doc is not None
        return doc

    assert client.portal.call(service.reapply_auth, fresh())
    unchanged()
    assert client.portal.call(service.rotate_svc_token, fresh())
    unchanged()
    sweep = client.portal.call(client.app.state.reconciler.svc_token_sweeper.run)
    assert preview.id not in sweep["renewed"] + sweep["failed"]
    unchanged()
    reapplied = run_data_plane_auth(client)
    assert preview.id not in reapplied.get("failed", [])
    unchanged()


def test_the_instance_token_of_a_preview_is_refused_once_its_credentials_end(
    client, fake_clusters, mongo
):
    """init 은 끝 전에 agent.json 을 받는다. 끝 + 10분이 지나면 지우기 전이라도 401 이다."""
    agent = deployed_agent(client, fake_clusters, mongo, runtime=False, spec=plain_spec)
    preview = deployed_preview(client, agent)
    token = token_of(agent.fake, preview)
    url = f"{AG}/{preview.id}/agent.json"
    r = client.get(url, headers=bearer(token))
    assert r.status_code == 200, r.text
    assert r.json()["instance"] == {"id": preview.id, "name": preview.id}
    started = utcnow() - timedelta(hours=2, minutes=11)
    mongo["agents"].update_one(
        {"_id": preview.id},
        {"$set": {"preview.started_at": started, "preview.ends_at": started + timedelta(hours=2)}},
    )
    assert client.get(url, headers=bearer(token)).status_code == 401
    # 원본의 토큰은 그대로 받는다
    assert client.get(f"{agent.url}/agent.json", headers=bearer(agent.token)).status_code == 200


def test_turn_tokens_of_a_preview_act_as_the_source(client, fake_clusters, mongo):
    """턴 토큰의 행위자는 원본의 주체와 id 다. 위임은 미리 보기에 묶인다 (9절)."""
    agent = deployed_agent(client, fake_clusters, mongo, runtime=False, spec=plain_spec)
    preview = deployed_preview(client, agent)
    now = utcnow()
    target = GrantTarget(kind="gateway_instance", id=agent.gw["id"])
    grant = TurnGrant(
        id="g-1",
        secret_sha256="x",
        agent_id=preview.id,
        tenant="default",
        sub="person-1",
        trigger="chat",
        targets=[target],
        opened_at=now,
        deadline=now + timedelta(minutes=5),
        purge_at=now + timedelta(days=1),
    )
    token = client.app.state.turn_grants._mint(preview, grant, target, 60)
    assert claims_of(token)["act"] == {"sub": "svc:agent:agent-support", "agent_id": agent.id}


def _preview_doc(ends_in: timedelta) -> Agent:
    from tests.helpers.agent_runtime import minimal_agent_doc
    from tests.helpers.docs import agent_doc

    now = utcnow()
    source = minimal_agent_doc()
    return agent_doc(
        "p-1",
        spec=source.spec.model_dump(),
        state="deployed",
        preview={
            "source": source.id,
            "source_name": source.name,
            "owner": "dev-1",
            "started_at": now,
            "ends_at": now + ends_in,
        },
    )


def test_a_preview_llm_token_ends_with_it_and_is_never_due_before(settings):
    """LLM 토큰도 끝 + 10분에 만료하고 원본의 id 와 주체를 싣는다. 만료가 30일 안이어도 끝 전에는
    다시 낼 때가 아니다. 교체는 미리 보기를 건너뛴다."""

    async def go(db) -> dict[str, Any]:
        agents = MongoAgentStore(db)
        svc = make_svc_tokens()
        tokens = LlmTokens(agents, MongoLlmRevocationStore(db), svc, client_for_doc=_no_client)
        preview = _preview_doc(timedelta(hours=2))
        await agents.create(preview)
        minted = await tokens.mint(preview, ["p"])
        kept = AgentTokens.__new__(AgentTokens)
        kept.svc_tokens = svc
        secret = {"stringData": {LLM_TOKEN_KEY: minted}}
        held = kept.kept_llm_token(preview, secret, wanted=["p"])
        normal = kept.kept_llm_token(
            preview.model_copy(update={"preview": None}), secret, wanted=["p"]
        )

        async def rerender(_doc: Agent) -> bool:
            raise AssertionError("a preview is never rotated")

        await tokens.rotate(preview, immediate=True, rerender=rerender)
        return {
            "claims": claims_of(minted),
            "record": (await agents.with_previews().get(preview.id)).llm_tokens,
            "held": held,
            "normal": normal,
            "token": minted,
            "end": preview.preview.credentials_end,
        }

    out = run_db(settings, go)
    assert out["claims"]["agent_id"] == "ag-1" and out["claims"]["sub"] == "svc:agent:agent-bot"
    assert out["record"][0].exp <= out["end"] and _expires(out["token"]) <= out["end"]
    assert out["held"] == (out["token"], ["p"])
    # 같은 토큰을 미리 보기가 아닌 문서가 들었다면 만료 30일 안이라 다시 낸다
    assert out["normal"] == (None, ["p"])


async def _no_client(_doc: Agent) -> Any:
    raise AssertionError("no cluster call is expected")
