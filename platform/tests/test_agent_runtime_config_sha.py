"""P35 13절: Agent 파드 템플릿의 설정 지문 어노테이션 (`daegam.devops.ai.kr/config-sha`).

지문이 남긴 키 없는 digest 를 인스턴스 토큰으로 건 값인 것, 재배포가 파드 입력이 바뀔 때만 파드를
굴리는 것, 토큰을 다시 낸 인증 재적용이 컴파일 없이 다시 거는 것, 컴파일 실패가 파드 전에
provision 을 멈추는 것을 든다. 적용하지 않는 렌더(manifest·Advanced 편집)도 같은 값을 내는 것은
test_agent_runtime_advanced_edit.py 가 본다.
"""

from __future__ import annotations

import pytest

from daegam.k8s.labels import keyed_hash
from daegam.k8s.templates.agent import CONFIG_SHA_ANNOTATION, config_digest, render_agent_deployment
from daegam.services import agent_graph
from daegam.services.agent_graph import GraphError, Issue
from daegam.store.agents import Agent
from daegam.store.base import hash_token, utcnow
from tests.helpers.agent_runtime import (
    agent_deployment,
    deployed_agent,
    minimal_agent_doc,
)
from tests.helpers.agents import (
    agent_env,
    create_agent,
    deploy_agent,
    template_for,
)
from tests.helpers.docs import effective_config
from tests.helpers.http import detail_code


def _config_sha(fake, name: str = "agent-support") -> str:
    dep = agent_deployment(fake, name)
    return dep["spec"]["template"]["metadata"]["annotations"][CONFIG_SHA_ANNOTATION]


def _stored_digest(mongo, agent_id: str) -> str:
    return mongo["agents"].find_one({"_id": agent_id})["config_digest"]


def test_config_sha_is_the_stored_digest_keyed_by_the_instance_token():
    """어노테이션은 남긴 키 없는 지문을 인스턴스 토큰으로 건 HMAC 앞 16자다. 같은 입력이면 같고
    토큰이 바뀌면 바뀌며 지문의 앞자리가 아니다. 토큰이 없으면 "0" 이다."""
    document = {"schema_version": "1"}
    digest = config_digest(document, {"agent-bot-llm-a": hash_token("sk-a")})
    assert len(digest) == 64 and digest == config_digest(
        document, {"agent-bot-llm-a": hash_token("sk-a")}
    )
    assert digest != config_digest(document, {"agent-bot-llm-a": hash_token("sk-b")})
    assert digest != config_digest({"schema_version": "2"}, {"agent-bot-llm-a": hash_token("sk-a")})

    def sha(inst: Agent, **kw) -> str:
        dep = render_agent_deployment(
            inst,
            effective_config(),
            providers=[],
            data_services={},
            agent_oauth=None,
            **kw,
            client_id_claim="cid",
            groups_claim=None,
        )
        return dep["spec"]["template"]["metadata"]["annotations"][CONFIG_SHA_ANNOTATION]

    inst = minimal_agent_doc(config_digest=digest)
    value = sha(inst, instance_token="tok-a")
    assert value == keyed_hash("tok-a", digest, 16) and len(value) == 16
    assert value == sha(inst, instance_token="tok-a")
    assert value != sha(inst, instance_token="tok-b")
    assert not digest.startswith(value)
    assert sha(inst) == "0"


def test_reapply_that_reissues_the_token_rehashes_without_recomputing_the_digest(
    client, fake_clusters, mongo, monkeypatch
):
    """인증 재적용이 토큰을 다시 내면 지문을 다시 계산하지 않고 남긴 지문을 새 토큰으로 다시 건다.
    어노테이션이 바뀌어 파드가 새 토큰을 받고 지문은 그대로다. 토큰이 살아 있으면 값도 그대로다.
    재적용은 새 파드가 받을 컴파일을 검사만 하고(P50 1절) 그 문서로 지문을 바꾸지 않는다."""
    agent = deployed_agent(client, fake_clusters, mongo, runtime=False)
    fake = agent.fake
    first, digest = _config_sha(fake), _stored_digest(mongo, agent.id)
    svc, store = client.app.state.agent_service, client.app.state.agents

    def reapply() -> None:
        assert client.portal.call(svc.reapply_auth, client.portal.call(store.get, agent.id))

    monkeypatch.setattr(svc, "_config_digest", lambda *a, **k: pytest.fail("digest recomputed"))
    reapply()
    assert _config_sha(fake) == first
    mongo["agent_instance_tokens"].update_one(
        {"_id": agent.doc["token_id"]}, {"$set": {"revoked_at": utcnow()}}
    )
    reapply()
    second = _config_sha(fake)
    assert second != first and second == keyed_hash(agent.token, digest, 16)
    assert _stored_digest(mongo, agent.id) == digest


def test_a_compile_failure_fails_provision_before_the_pod(
    client, fake_clusters, mongo, monkeypatch
):
    """지금 상태에서 어긋난 그래프는 provision 이 422 invalid_graph 로 실패한다. init 이 받지 못해
    크래시 루프에 드는 대신이다. Deployment 는 적용되지 않는다."""
    cid, ds, gw = agent_env(client, mongo)

    def broken(*args, **kwargs):
        raise GraphError([Issue(code="unknown_provider", message="provider gone")])

    monkeypatch.setattr(agent_graph, "compile", broken)
    p = create_agent(client, template_for(gw, ds)).json()
    r = deploy_agent(client, p["id"])
    assert detail_code(r) == (422, "invalid_graph")
    saved = mongo["agents"].find_one({"_id": p["id"]})
    assert saved["state"] == "failed" and saved["last_error"].startswith("invalid_graph")
    assert saved["config_digest"] == ""
    assert agent_deployment(fake_clusters.by_name["edge"]) is None
