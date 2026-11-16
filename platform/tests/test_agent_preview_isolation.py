"""미리 보기와 원본의 자격은 서로를 건드리지 않는다 (P44b 8절·9절, D107).

미리 보기의 토큰은 원본의 주체와 id 를 싣지만 기록과 Secret 은 미리 보기 문서의 것이다. 인스턴스
토큰은 미리 보기 id 로 찾고, LLM 토큰의 jti 는 미리 보기 문서에 적히며, 서비스·Gateway 토큰은 미리
보기의 Secret 에만 있다. 그래서 미리 보기를 끝내도 원본의 토큰은 하나도 회수되지 않고, 원본의 LLM
교체나 서비스 토큰 스윕은 미리 보기의 토큰을 회수하지도 늘리지도 않는다. 인스턴스 토큰은 제
문서에만 통한다.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

from daegam.k8s.templates.agent import LLM_TOKEN_KEY
from daegam.store.agents import Agent
from daegam.store.base import from_epoch
from tests.helpers.agent_runtime import DeployedAgent, deployed_agent, token_of
from tests.helpers.auth import bearer
from tests.helpers.consts import AG, DEFAULT_NS
from tests.helpers.previews import deployed_preview, plain_spec, start_preview
from tests.helpers.svc_tokens import claims_of, plant_key
from tests.test_llm_route import (  # noqa: F401 - gw_client 는 픽스처다
    _on_gateway,
    _report,
    gw_client,
)


def _secret(fake, name: str) -> dict[str, str]:
    return fake.get("v1", "Secret", name, DEFAULT_NS)["stringData"]


def _revoked(mongo, token: str) -> bool:
    return mongo["llm_revocations"].find_one({"_id": claims_of(token)["jti"]}) is not None


def _exp(token: str) -> int:
    return int(claims_of(token)["exp"])


def _preview_on_gateway(client, agent: DeployedAgent) -> tuple[Agent, Any]:
    """게이트웨이 경로의 원본에서 연 미리 보기를 init 의 기록과 재적용 한 번으로 게이트웨이 경로에
    옮긴다. 그 LLM 토큰은 미리 보기의 끝 + 10분까지다."""
    preview = deployed_preview(client, agent)
    assert preview.k8s is not None
    token = token_of(agent.fake, preview)
    _report(client, SimpleNamespace(auth=bearer(token), url=f"{AG}/{preview.id}"))
    service = client.app.state.agent_service
    fresh = client.portal.call(lambda: service.instances.get(preview.id))
    assert client.portal.call(lambda: service.reapply_auth(fresh))
    moved = client.portal.call(lambda: service.instances.get(preview.id))
    assert moved is not None and moved.llm_route.gateway
    return moved, token


def test_source_rotations_and_sweeps_leave_the_preview_tokens_alone(
    gw_client,  # noqa: F811
    mongo,
    fake_clusters,
):
    """원본의 곧바로 LLM 교체는 원본의 다른 토큰을 모두 회수하지만 미리 보기의 토큰은 회수하지
    않는다. 원본의 서비스 토큰 스윕도 미리 보기의 Secret 과 만료를 바꾸지 않는다."""
    client = gw_client
    agent = _on_gateway(client, mongo, fake_clusters)
    preview, _instance = _preview_on_gateway(client, agent)
    assert preview.k8s is not None and preview.preview is not None
    held = _secret(agent.fake, preview.k8s.token_secret)
    llm = held[LLM_TOKEN_KEY]
    assert claims_of(llm)["agent_id"] == agent.id
    assert from_epoch(_exp(llm)) <= preview.preview.credentials_end
    source_llm = _secret(agent.fake, agent.doc["k8s"]["token_secret"])[LLM_TOKEN_KEY]

    r = client.post(f"{agent.url}/llm-token/rotate", json={"immediate": True})
    assert r.status_code == 200, r.text
    assert _revoked(mongo, source_llm) and not _revoked(mongo, llm)
    # 원본의 스윕: 원본의 Secret 에 곧 끝나는 토큰을 심으면 원본만 새로 낸다
    issuer = client.app.state.internal_issuer
    stale = issuer.mint(sub="svc:agent:agent-support", tenant="default", groups=[], ttl=60)
    plant_key(agent.fake, agent.doc["k8s"]["token_secret"], "mcp_token", stale)
    sweep = client.portal.call(client.app.state.reconciler.svc_token_sweeper.run)
    assert sweep["renewed"] == [agent.id]
    after = _secret(agent.fake, preview.k8s.token_secret)
    assert {k: after[k] for k in held} == held
    assert _exp(after[LLM_TOKEN_KEY]) == _exp(llm)


def test_ending_a_preview_revokes_none_of_the_source_tokens(
    gw_client,  # noqa: F811
    mongo,
    fake_clusters,
):
    client = gw_client
    agent = _on_gateway(client, mongo, fake_clusters)
    preview, preview_token = _preview_on_gateway(client, agent)
    assert preview.k8s is not None
    source_secret = _secret(agent.fake, agent.doc["k8s"]["token_secret"])
    preview_llm = _secret(agent.fake, preview.k8s.token_secret)[LLM_TOKEN_KEY]
    r = client.delete(f"{AG}/{agent.id}/preview")
    assert r.status_code == 200 and r.json()["ended"] is True, r.text
    # 미리 보기의 것만 회수했다
    assert _revoked(mongo, preview_llm)
    assert not _revoked(mongo, source_secret[LLM_TOKEN_KEY])
    assert _secret(agent.fake, agent.doc["k8s"]["token_secret"]) == source_secret
    source_tokens = list(mongo["agent_instance_tokens"].find({"agent_instance_id": agent.id}))
    assert source_tokens and all(t["revoked_at"] is None for t in source_tokens)
    ok = client.get(f"{agent.url}/agent.json", headers=bearer(source_secret["token"]))
    assert ok.status_code == 200, ok.text
    gone = client.get(f"{AG}/{preview.id}/agent.json", headers=bearer(preview_token))
    assert gone.status_code == 401


def test_ending_a_preview_leaves_the_source_gateway_token(local_admin, fake_clusters, mongo):
    """local 모드의 Gateway 토큰은 기록 없는 JWT 라 미리 보기의 것은 그 Secret 과 함께 사라지고
    원본의 것은 그대로다."""
    client = local_admin
    agent = deployed_agent(client, fake_clusters, mongo, runtime=False, spec=plain_spec)
    preview = start_preview(client, agent.id, "admin-sub")
    assert preview.k8s is not None
    source_secret = _secret(agent.fake, agent.doc["k8s"]["token_secret"])
    preview_secret = _secret(agent.fake, preview.k8s.token_secret)
    assert source_secret["gateway_token"] != preview_secret["gateway_token"]
    ended = client.portal.call(
        lambda: client.app.state.agent_previews.end(preview, "ended", by="admin-sub")
    )
    assert ended
    assert agent.fake.get("v1", "Secret", preview.k8s.token_secret, DEFAULT_NS) is None
    assert _secret(agent.fake, agent.doc["k8s"]["token_secret"]) == source_secret
    ok = client.get(f"{agent.url}/agent.json", headers=bearer(source_secret["token"]))
    assert ok.status_code == 200, ok.text


def test_an_instance_token_opens_only_its_own_document(client, fake_clusters, mongo):
    """미리 보기의 인스턴스 토큰은 원본의 agent.json·Skill 을 열지 못하고, 원본의 토큰도 미리
    보기의 것을 열지 못한다 (`pinned_agent`)."""
    agent = deployed_agent(client, fake_clusters, mongo, runtime=False, spec=plain_spec)
    preview = deployed_preview(client, agent)
    assert preview.k8s is not None
    preview_token = token_of(agent.fake, preview)
    mine = client.get(f"{AG}/{preview.id}/agent.json", headers=bearer(preview_token))
    assert mine.status_code == 200, mine.text
    for url, token in (
        (f"{agent.url}/agent.json", preview_token),
        (f"{agent.url}/skills/any", preview_token),
        (f"{AG}/{preview.id}/agent.json", agent.token),
    ):
        assert client.get(url, headers=bearer(token)).status_code == 401, url
