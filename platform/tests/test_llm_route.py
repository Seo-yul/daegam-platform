"""Agent 의 LLM 경로와 옮겨 가기 (P38 6절, F7).

경로 결정 표, 게이트웨이 경로의 렌더와 컴파일, provision 과 재적용이 정하는 경로, 옮긴 뒤의 정리,
끈 뒤 되돌아가기, 상태 API 의 Agent 수, 그래프가 정하는 토큰의 provider 를 본다. 게이트웨이의 상태
문서는 테스트가 Mongo 에 넣고 home 클러스터는 Agent 의 클러스터로 둔다.
"""

from __future__ import annotations

import dataclasses
from types import SimpleNamespace
from typing import Any

import anyio
import pytest

from daegam.k8s.templates.agent import (
    CONFIG_URL_ENV,
    INIT_CONTAINER,
    LLM_TOKEN_KEY,
    render_agent_deployment,
)
from daegam.k8s.templates.llm_gateway import LLM_ROUTE_LABEL
from daegam.names import provider_slug
from daegam.services import agent_features
from daegam.services.agent_llm import (
    MOVING,
    PROVIDER_NOT_READY,
    REMOTE_CLUSTER,
    RUNTIME_FEATURES_UNKNOWN,
    RUNTIME_IMAGE_TOO_OLD,
    STUB_ONLY,
    AgentLlm,
    GatewayView,
    decide,
)
from daegam.services.llm_readiness import GATEWAY_NOT_READY, GATEWAY_OFF, NO_SIGNER
from daegam.services.redis_tls_consumers import RecentReads
from daegam.store.agents import AgentRuntime, LlmRoute
from daegam.store.base import utcnow
from tests.helpers.agent_pod_snapshot import ANTHROPIC, OPENAI, STUB
from tests.helpers.agent_runtime import (
    agent_deployment,
    deployed_agent,
    minimal_agent_doc,
    set_ready,
    token_of,
)
from tests.helpers.agents import (
    agent_body,
    anthropic_for,
    anthropic_with_openai_fallback,
)
from tests.helpers.consts import AG, DEFAULT_NS, IMAGE, LLM_KEY_SECRET, PLATFORM_NS
from tests.helpers.docs import effective_config
from tests.helpers.http import api_client, make_app
from tests.helpers.llm_gateway import IMAGES
from tests.helpers.runtime import fake_runtime
from tests.helpers.svc_tokens import claims_of

GW = "http://llm-gateway.daegam-llm.svc.cluster.local:8080"
CLAUDE = "local-anthropic"
COPY = "agent-support-llm-local-anthropic"
KEY_ENV = "DAEGAM_LLM_LOCAL_ANTHROPIC_API_KEY"
# init 이 알리는 이름. 플랫폼이 아는 이름 전부라 요구 기능이 빠지지 않는다
ALL_FEATURES = ",".join(sorted(agent_features.KNOWN))
OLD_FEATURES = ",".join(sorted(agent_features.KNOWN - {agent_features.LLM_GATEWAY}))


# -- 결정 표 ---------------------------------------------------------------------------------


def _doc(*, features: list[str] | None, cluster: str = "home-id") -> Any:
    snapshot = {"images": {"agent_image": IMAGE}}
    doc = minimal_agent_doc(cluster_id=cluster, effective_config=snapshot)
    if features is not None:
        doc.runtime = AgentRuntime(image=IMAGE, features=features, reported_at=utcnow())
    return doc


OFF = GatewayView(
    on=False,
    ready=True,
    signer=True,
    providers_ready=frozenset({"claude"}),
    carries_purpose=False,
    home_id="home-id",
)
READY = dataclasses.replace(OFF, on=True)
NOT_READY = dataclasses.replace(READY, ready=False)
NO_KEY = dataclasses.replace(READY, signer=False)
KNOWS = [agent_features.LLM_GATEWAY]


@pytest.mark.parametrize(
    ("gw", "doc", "providers", "reason"),
    [
        (OFF, _doc(features=KNOWS), [ANTHROPIC], GATEWAY_OFF),
        (NOT_READY, _doc(features=KNOWS), [ANTHROPIC], GATEWAY_NOT_READY),
        (NO_KEY, _doc(features=KNOWS), [ANTHROPIC], NO_SIGNER),
        (READY, _doc(features=KNOWS, cluster="edge"), [ANTHROPIC], REMOTE_CLUSTER),
        (READY, _doc(features=KNOWS), [STUB], STUB_ONLY),
        # 반영되지 않은 provider 하나가 Agent 전체를 직접 경로로 둔다(SSRF 검사에 걸린 provider 도)
        (READY, _doc(features=KNOWS), [ANTHROPIC, OPENAI], PROVIDER_NOT_READY),
        (READY, _doc(features=None), [ANTHROPIC], RUNTIME_FEATURES_UNKNOWN),
        (READY, _doc(features=[]), [ANTHROPIC], RUNTIME_IMAGE_TOO_OLD),
        # 앞 행이 이긴다. 끈 게이트웨이는 다른 까닭보다 먼저다
        (OFF, _doc(features=None, cluster="edge"), [STUB], GATEWAY_OFF),
    ],
)
def test_the_first_matching_row_keeps_the_agent_direct(gw, doc, providers, reason) -> None:
    assert decide(doc, providers, gw) == LlmRoute(route="direct", reason=reason)


def test_otherwise_the_route_is_the_gateway_for_the_graph_providers() -> None:
    """토큰은 그래프가 쓰는 provider 가운데 stub 이 아닌 것의 slug 만 연다."""
    route = decide(_doc(features=KNOWS), [ANTHROPIC, STUB, ANTHROPIC], READY)
    assert route == LlmRoute(route="gateway", providers=[provider_slug("claude")])


def _llm(view: GatewayView) -> AgentLlm:
    """상태 읽기 없이 view 만 돌려주는 AgentLlm. provider 를 읽으면 실패한다."""
    llm = AgentLlm.__new__(AgentLlm)
    llm._recent = RecentReads()

    async def fixed() -> GatewayView:
        return view

    async def no_providers(*_: Any) -> Any:
        raise AssertionError("provider 를 읽지 않아야 한다")

    llm.view = fixed  # type: ignore[method-assign]
    llm.providers = no_providers  # type: ignore[method-assign]
    return llm


def test_an_off_gateway_decides_without_reading_providers() -> None:
    """재적용 틱이 Agent 마다 부르므로 끈 게이트웨이는 provider 를 해소하지 않는다."""
    doc = _doc(features=KNOWS)
    assert doc.deployed is not None
    route = anyio.run(_llm(OFF).decision, doc)
    assert route == LlmRoute(route="direct", reason=GATEWAY_OFF)


def test_a_lost_pinned_revision_stays_direct_as_provider_not_ready() -> None:
    """켠 게이트웨이에서 핀한 리비전이 없으면 provider 를 알 수 없어 반영되지 않은 것으로 본다."""
    doc = _doc(features=KNOWS)
    doc.deployed_revision = 99
    route = anyio.run(_llm(READY).decision, doc)
    assert route == LlmRoute(route="direct", reason=PROVIDER_NOT_READY)


# -- 렌더 -----------------------------------------------------------------------------------


def _render(inst: Any, **kw: Any) -> dict[str, Any]:
    return render_agent_deployment(
        inst,
        effective_config(),
        data_services={},
        agent_oauth=None,
        client_id_claim="cid",
        groups_claim=None,
        **kw,
    )


def _env(dep: dict[str, Any], container: str | None = None) -> dict[str, Any]:
    pod = dep["spec"]["template"]["spec"]
    found = pod["initContainers"] if container == INIT_CONTAINER else pod["containers"]
    return {e["name"]: e.get("valueFrom", e.get("value")) for e in found[0]["env"]}


def test_the_gateway_render_points_the_key_envs_at_the_token() -> None:
    """게이트웨이 경로(6절)는 키 env 이름을 그대로 두고 토큰 Secret 의 llm_token 을 가리킨다.
    복사본 Secret 과 `_BASE_URL` env 는 없다. init 주소에 `?llm=gateway` 가 붙고 파드에만 경로
    라벨이 붙는다."""
    gateway = minimal_agent_doc(llm_route=LlmRoute(route="gateway", providers=["claude"]))
    based = OPENAI.model_copy(update={"base_url": "https://gw.example.com/v1"})
    dep = _render(gateway, providers=[ANTHROPIC, STUB], knowledge_providers=[(0, based)])
    env = _env(dep)
    token_ref = {"secretKeyRef": {"name": gateway.k8s.token_secret, "key": LLM_TOKEN_KEY}}
    assert env["DAEGAM_LLM_CLAUDE_API_KEY"] == token_ref
    assert env["DAEGAM_KNOWLEDGE_0_API_KEY"] == token_ref
    assert "DAEGAM_KNOWLEDGE_0_BASE_URL" not in env
    refs = [
        v["secretKeyRef"]["name"]
        for v in env.values()
        if isinstance(v, dict) and "secretKeyRef" in v
    ]
    assert not any("-llm-" in name for name in refs)
    assert _env(dep, INIT_CONTAINER)[CONFIG_URL_ENV].endswith("/agent.json?llm=gateway")
    assert dep["spec"]["template"]["metadata"]["labels"][LLM_ROUTE_LABEL] == "gateway"
    assert LLM_ROUTE_LABEL not in dep["metadata"]["labels"]
    direct = _render(minimal_agent_doc(), providers=[ANTHROPIC], knowledge_providers=[(0, based)])
    assert LLM_ROUTE_LABEL not in direct["spec"]["template"]["metadata"]["labels"]
    assert _env(direct)["DAEGAM_KNOWLEDGE_0_BASE_URL"] == "https://gw.example.com/v1"
    assert _env(direct, INIT_CONTAINER)[CONFIG_URL_ENV].endswith("/agent.json")


# -- provision 과 재적용 ----------------------------------------------------------------------


@pytest.fixture
def gw_client(settings, mongo, fake_clusters, fake_secrets):
    app = make_app(
        settings,
        fake_clusters,
        fake_secrets,
        llm_gateway="agentgateway",
        llm_gateway_images=IMAGES,
    )
    with api_client(app) as c:
        yield c


def _open(client, mongo, cluster_id: str, providers: tuple[str, ...] = (CLAUDE,)) -> None:
    """게이트웨이 리비전 하나가 반영됐고 그 provider 들이 열렸다. home 클러스터는 cluster_id 다."""
    mongo["settings"].update_one(
        {"_id": "llm_gateway"},
        {"$set": {"ready_at": utcnow(), "providers_ready": list(providers)}},
        upsert=True,
    )
    llm = client.app.state.agent_service.llm
    real = llm.cluster_service

    async def home() -> SimpleNamespace:
        return SimpleNamespace(id=cluster_id)

    llm.cluster_service = SimpleNamespace(home=home, secret_store=real.secret_store)
    llm._recent = RecentReads()


def _switch_off(client) -> None:
    llm = client.app.state.agent_service.llm
    llm.settings = dataclasses.replace(llm.settings, llm_gateway="off")
    llm._recent = RecentReads()


def _report(client, agent, features: str = ALL_FEATURES, llm: str | None = None) -> dict:
    """init 받기. 이 이미지가 features 를 알린다."""
    headers = {**agent.auth, "X-Daegam-Runtime-Image": IMAGE, "X-Daegam-Runtime-Features": features}
    url = f"{agent.url}/agent.json" + (f"?llm={llm}" if llm else "")
    r = client.get(url, headers=headers)
    assert r.status_code == 200, r.text
    return r.json()


def _doc_of(client, agent):
    return client.portal.call(lambda: client.app.state.agent_service.instances.get(agent.id))


def _reapply(client, agent) -> bool:
    service = client.app.state.agent_service
    doc = _doc_of(client, agent)
    return client.portal.call(lambda: service.reapply_auth(doc))


def _want_changed(client, agent) -> bool:
    service = client.app.state.agent_service
    doc = _doc_of(client, agent)

    async def want() -> str | None:
        return await service.want_rev(doc, await service.tenant_settings.get(doc.tenant))

    return client.portal.call(want) != doc.auth_rev


def _settle(client) -> list[str]:
    return client.portal.call(client.app.state.agent_service.agent_tokens.llm.settle)


def _secret(agent) -> dict[str, Any]:
    return agent.fake.get("v1", "Secret", agent.doc["k8s"]["token_secret"], DEFAULT_NS)


def _on_gateway(client, mongo, fake_clusters):
    """처음 배포는 기록이 없어 직접 경로이고, init 이 기능을 알린 뒤 재적용 한 번에 옮긴다."""
    agent = deployed_agent(client, fake_clusters, mongo, spec=anthropic_for, runtime=False)
    assert agent.doc["llm_route"] == {"route": "direct", "reason": GATEWAY_NOT_READY}
    assert agent.fake.get("v1", "Secret", COPY, DEFAULT_NS) is not None
    _report(client, agent)
    _open(client, mongo, agent.cluster_id)
    assert _want_changed(client, agent)
    assert _reapply(client, agent)
    return agent


def test_a_new_agent_starts_direct_and_moves_with_one_reapply(gw_client, mongo, fake_clusters):
    """init 의 기록 뒤 한 번의 재적용으로 옮긴다. 그 파드는 provider 키 대신 그래프의 provider 만
    여는 90일 토큰을 받고, 롤아웃이 끝나면 복사본이 지워진다. 옮긴 뒤에는 판이 가라앉는다."""
    agent = _on_gateway(gw_client, mongo, fake_clusters)
    doc = mongo["agents"].find_one({"_id": agent.id})
    assert doc["llm_route"] == {"route": "gateway", "reason": None, "providers": [CLAUDE]}
    env = _env(agent_deployment(agent.fake))
    assert env[KEY_ENV] == {
        "secretKeyRef": {"name": doc["k8s"]["token_secret"], "key": LLM_TOKEN_KEY}
    }
    token = _secret(agent)["stringData"][LLM_TOKEN_KEY]
    claims = claims_of(token)
    assert (claims["aud"], claims["llm_providers"], claims["agent_id"]) == (
        "daegam-llm",
        [CLAUDE],
        agent.id,
    )
    # 게이트웨이 경로의 파드는 provider 키를 갖지 않는다. 토큰 Secret 에도 키가 없다
    assert "sk-test-key" not in str(_secret(agent)) and "sk-test-key" not in str(env)
    assert doc["llm_cleanup"][0]["drop_secrets"] == [COPY]
    assert not _want_changed(gw_client, agent)
    # 옛 템플릿의 파드가 다시 떠도 읽도록 롤아웃이 끝날 때까지 복사본을 둔다
    assert _settle(gw_client) == []
    assert agent.fake.get("v1", "Secret", COPY, DEFAULT_NS) is not None
    set_ready(agent.fake)
    assert _settle(gw_client) == [agent.id]
    assert agent.fake.get("v1", "Secret", COPY, DEFAULT_NS) is None
    assert mongo["agents"].find_one({"_id": agent.id})["llm_secrets"] == []


def test_the_gateway_document_carries_the_gateway_address_and_the_feature(
    gw_client, mongo, fake_clusters
):
    """`?llm=gateway` 의 문서는 provider 의 base_url 이 게이트웨이 경로이고 `gateway: true` 와
    `llm_gateway` 를 싣는다. 쿼리 없는 문서는 직접 경로 그대로다. 그 밖의 값은 422 다."""
    agent = _on_gateway(gw_client, mongo, fake_clusters)
    gateway = _report(gw_client, agent, llm="gateway")
    provider = gateway["main"]["model"]["provider"]
    assert provider == {
        "name": CLAUDE,
        "kind": "anthropic",
        "env_key": KEY_ENV,
        "base_url": f"{GW}/p/{CLAUDE}",
        "gateway": True,
    }
    assert agent_features.LLM_GATEWAY in gateway["features"]
    direct = _report(gw_client, agent)
    assert direct["main"]["model"]["provider"]["base_url"] is None
    assert "gateway" not in direct["main"]["model"]["provider"]
    assert agent_features.LLM_GATEWAY not in direct.get("features", [])
    headers = {**agent.auth, "X-Daegam-Runtime-Image": IMAGE}
    assert gw_client.get(f"{agent.url}/agent.json?llm=bogus", headers=headers).status_code == 422


def test_the_cleanup_skips_an_agent_that_changed_after_it_was_listed(
    gw_client, mongo, fake_clusters
):
    """정리는 문서의 클레임 안에서 다시 읽은 문서로 한다. 목록을 읽은 뒤 재배포가 경로를 직접 경로로
    되돌렸거나(새 이미지라 runtime_features_unknown) 배포가 바뀌었으면, 그 배포가 다시 쓴 복사본을
    지우지 않고 목록에서 빼지도 않는다. 다음 틱이 지금 문서로 다시 본다."""
    agent = _on_gateway(gw_client, mongo, fake_clusters)
    set_ready(agent.fake)
    service = gw_client.app.state.agent_service
    tokens = service.agent_tokens.llm
    # provision 과 재적용이 쥐는 문서의 클레임이다
    assert tokens.claimed == service.claimed_llm_cleanup
    listed = _doc_of(gw_client, agent)
    assert listed.llm_route.gateway and listed.llm_cleanup[0].drop_secrets == [COPY]
    back = {"route": "direct", "reason": RUNTIME_FEATURES_UNKNOWN, "providers": []}
    mongo["agents"].update_one({"_id": agent.id}, {"$set": {"llm_route": back}})
    assert not gw_client.portal.call(tokens._settle_one, listed)
    assert agent.fake.get("v1", "Secret", COPY, DEFAULT_NS) is not None
    stored = mongo["agents"].find_one({"_id": agent.id})
    assert stored["llm_secrets"] == [COPY] and stored["llm_cleanup"]
    # 경로는 같아도 그 사이 다른 배포가 됐으면 건너뛴다
    gateway = {"route": "gateway", "reason": None, "providers": [CLAUDE]}
    mongo["agents"].update_one(
        {"_id": agent.id}, {"$set": {"llm_route": gateway, "deploying_since": utcnow()}}
    )
    assert not gw_client.portal.call(tokens._settle_one, listed)
    assert agent.fake.get("v1", "Secret", COPY, DEFAULT_NS) is not None
    # 바뀌지 않은 문서는 정리된다
    assert _settle(gw_client) == [agent.id]
    assert agent.fake.get("v1", "Secret", COPY, DEFAULT_NS) is None


def test_an_image_without_the_feature_stops_at_the_gateway_document(
    gw_client, mongo, fake_clusters
):
    """게이트웨이 경로의 문서는 `llm_gateway` 를 요구한다. 모르는 이미지의 init 은 409 로 멈추고
    그 기록이 다음 틱에 Agent 를 직접 경로로 되돌린다."""
    agent = _on_gateway(gw_client, mongo, fake_clusters)
    headers = {
        **agent.auth,
        "X-Daegam-Runtime-Image": IMAGE,
        "X-Daegam-Runtime-Features": OLD_FEATURES,
    }
    r = gw_client.get(f"{agent.url}/agent.json?llm=gateway", headers=headers)
    assert r.status_code == 409 and r.json()["detail"]["code"] == "runtime_image_too_old"
    gw_client.app.state.agent_service.llm._recent = RecentReads()
    assert _want_changed(gw_client, agent)
    assert _reapply(gw_client, agent)
    doc = mongo["agents"].find_one({"_id": agent.id})
    assert doc["llm_route"]["reason"] == RUNTIME_IMAGE_TOO_OLD


def test_turning_off_moves_the_agent_back_and_revokes_after_the_rollout(
    gw_client, mongo, fake_clusters
):
    """끄면 결정이 gateway_off 가 되어 재적용이 직접 경로로 되돌린다. 복사본이 다시 생기고, 그
    롤아웃이 끝나면 살아 있던 jti 를 회수하고 llm_token 을 뺀다. 그 사이 다시 뜨는 옛 파드는 여전히
    게이트웨이 주소의 문서를 받는다(주소는 늘 있다)."""
    agent = _on_gateway(gw_client, mongo, fake_clusters)
    set_ready(agent.fake)
    _settle(gw_client)
    jti = claims_of(_secret(agent)["stringData"][LLM_TOKEN_KEY])["jti"]
    _switch_off(gw_client)
    assert _want_changed(gw_client, agent)
    assert _reapply(gw_client, agent)
    doc = mongo["agents"].find_one({"_id": agent.id})
    assert doc["llm_route"] == {"route": "direct", "reason": GATEWAY_OFF, "providers": []}
    assert agent.fake.get("v1", "Secret", COPY, DEFAULT_NS) is not None
    assert _env(agent_deployment(agent.fake))[KEY_ENV]["secretKeyRef"]["name"] == COPY
    cleanup = doc["llm_cleanup"][-1]
    assert cleanup["revoke"] == [jti] and cleanup["drop_keys"] == [LLM_TOKEN_KEY]
    assert _report(gw_client, agent, llm="gateway")["main"]["model"]["provider"]["gateway"]
    assert mongo["llm_revocations"].find_one({"_id": jti}) is None
    set_ready(agent.fake)
    assert _settle(gw_client) == [agent.id]
    assert mongo["llm_revocations"].find_one({"_id": jti}) is not None
    assert LLM_TOKEN_KEY not in (_secret(agent).get("stringData") or {})
    # 직접 경로로 돌아온 Agent 는 끈 채로 다시 렌더되지 않는다
    assert not _want_changed(gw_client, agent)


def test_an_unready_provider_keeps_the_agent_direct(gw_client, mongo, fake_clusters):
    """게이트웨이가 렌더하지 않은 provider(SSRF 검사에 걸린 provider)를 쓰는 Agent 는 직접 경로에
    `provider_not_ready` 로 남고 다시 렌더되지 않는다. 상태 API 가 그 까닭을 센다."""
    agent = deployed_agent(gw_client, fake_clusters, mongo, spec=anthropic_for, runtime=False)
    _report(gw_client, agent)
    _open(gw_client, mongo, agent.cluster_id, providers=())
    assert not _want_changed(gw_client, agent)
    agents = gw_client.get("/settings/llm-gateway").json()["agents"]
    assert agents == {
        "gateway": 0,
        "direct": {PROVIDER_NOT_READY: 1},
        MOVING: 0,
        "cleanups_pending": 0,
    }
    _open(gw_client, mongo, agent.cluster_id)
    assert gw_client.get("/settings/llm-gateway").json()["agents"][MOVING] == 1
    assert _reapply(gw_client, agent)
    counts = gw_client.get("/settings/llm-gateway").json()["agents"]
    assert (counts["gateway"], counts["cleanups_pending"]) == (1, 1)


def test_a_gateway_off_install_rerenders_no_agent(client, mongo, fake_clusters):
    """끈 설치의 직접 경로 Agent 는 판에 표시가 없어 재적용이 다시 렌더하지 않는다."""
    agent = deployed_agent(client, fake_clusters, mongo, spec=anthropic_for, runtime=False)
    _report(client, agent)
    assert not _want_changed(client, agent)
    assert agent.doc["llm_route"] == {"route": "direct", "reason": GATEWAY_OFF}


def test_a_gateway_off_reapply_and_rotation_leave_the_key_copies_alone(
    client, mongo, fake_clusters, fake_secrets, monkeypatch
):
    """끈 설치의 재적용과 서비스 토큰 회전은 복사하려고 플랫폼 키를 읽지 않고 복사본을 쓰지
    않는다. 바꾼 플랫폼 키는 설정 지문을 바꾸는 재배포가 싣는다(D56). 그래서 한 템플릿의
    레플리카가 같은 키로 돈다. 검증의 키 유무 검사는 따로이고 복사본에 아무것도 쓰지 않는다."""
    from daegam.services import agent_pod_inputs

    agent = deployed_agent(client, fake_clusters, mongo, spec=anthropic_for, runtime=False)
    copy_before = agent.fake.get("v1", "Secret", COPY, DEFAULT_NS)
    assert copy_before is not None
    fake_secrets.put(LLM_KEY_SECRET, {"api_key": "sk-changed"}, namespace=PLATFORM_NS)
    reads: list[str] = []
    real = agent_pod_inputs.resolve_api_key

    def counting(provider: Any, *args: Any, **kwargs: Any) -> str:
        reads.append(provider.name)
        return real(provider, *args, **kwargs)

    monkeypatch.setattr(agent_pod_inputs, "resolve_api_key", counting)
    applied = len(agent.fake.applied)
    service = client.app.state.agent_service
    assert _reapply(client, agent)
    doc = _doc_of(client, agent)
    assert client.portal.call(lambda: service.rotate_svc_token(doc))
    assert reads == []
    written = [o["metadata"]["name"] for o in agent.fake.applied[applied:] if o["kind"] == "Secret"]
    assert COPY not in written
    assert agent.fake.get("v1", "Secret", COPY, DEFAULT_NS) == copy_before
    # 문서의 목록이 모자라면(옛 문서) 다시 쓴다. 파드가 읽을 Secret 이 없는 것보다 낫다
    mongo["agents"].update_one({"_id": agent.id}, {"$set": {"llm_secrets": []}})
    assert _reapply(client, agent)
    assert reads == [CLAUDE]
    assert mongo["agents"].find_one({"_id": agent.id})["llm_secrets"] == [COPY]


def test_a_removed_provider_is_no_longer_granted_and_the_old_token_is_revoked_after_the_rollout(
    gw_client, mongo, fake_clusters, fake_secrets
):
    """토큰이 여는 provider 는 그래프에서 온다(`AgentTokens.llm_wanted`). 폴백을 뺀 재배포는 그
    provider 를 열지 않는 토큰을 새로 내고, 뺀 provider 를 연 옛 토큰은 그 롤아웃 뒤에 회수한다.
    스윕도 뺀 provider 를 연 토큰을 갱신 대상으로 본다."""
    agent = deployed_agent(
        gw_client,
        fake_clusters,
        mongo,
        spec=anthropic_with_openai_fallback,
        openai_secrets=fake_secrets,
        runtime=False,
    )
    _report(gw_client, agent)
    _open(gw_client, mongo, agent.cluster_id, providers=(CLAUDE, "local-openai"))
    assert _reapply(gw_client, agent)
    old = claims_of(_secret(agent)["stringData"][LLM_TOKEN_KEY])
    assert old["llm_providers"] == sorted([CLAUDE, provider_slug("local-openai")])
    changed = anthropic_for(agent.gw, agent.ds)
    assert gw_client.put(agent.url, json=agent_body(changed)).json()["revision"] == 2
    set_ready(agent.fake)
    agent.fake.service_app = fake_runtime(token_of(agent.fake, agent.doc))
    r = gw_client.post(f"{AG}/{agent.id}/redeploy")
    assert r.status_code == 200, r.text
    assert r.json()["llm_route"] == {"route": "gateway", "reason": None}
    new = claims_of(_secret(agent)["stringData"][LLM_TOKEN_KEY])
    assert new["llm_providers"] == [CLAUDE]
    tokens = gw_client.app.state.agent_service.agent_tokens
    assert tokens.wanted_llm(_doc_of(gw_client, agent)) == [CLAUDE]
    assert mongo["agents"].find_one({"_id": agent.id})["llm_cleanup"][-1]["revoke"] == [old["jti"]]
    assert mongo["llm_revocations"].find_one({"_id": old["jti"]}) is None
    set_ready(agent.fake)
    assert _settle(gw_client) == [agent.id]
    assert mongo["llm_revocations"].find_one({"_id": old["jti"]}) is not None
    assert mongo["llm_revocations"].find_one({"_id": new["jti"]}) is None


def test_rotation_mints_a_new_token_and_keeps_the_old_until_the_rollout(
    gw_client, mongo, fake_clusters
):
    """명시적 교체(5절)는 새 토큰으로 파드를 굴리고 옛 jti 는 롤아웃 뒤에 회수한다. 곧바로 교체는
    새 토큰 말고 살아 있는 토큰을 모두 그 자리에서 회수한다. 직접 경로로 돌아간 Agent 의 교체는
    정리를 기다리는 옛 토큰이 남아 있어도 409 llm_token_missing 이다."""
    agent = _on_gateway(gw_client, mongo, fake_clusters)
    old = _secret(agent)["stringData"][LLM_TOKEN_KEY]
    r = gw_client.post(f"{agent.url}/llm-token/rotate", json={})
    assert r.status_code == 200, r.text
    new = _secret(agent)["stringData"][LLM_TOKEN_KEY]
    doc = mongo["agents"].find_one({"_id": agent.id})
    assert new != old and claims_of(new)["llm_providers"] == [CLAUDE]
    assert doc["llm_cleanup"][-1]["revoke"] == [claims_of(old)["jti"]]
    assert mongo["llm_revocations"].find_one({"_id": claims_of(old)["jti"]}) is None
    r = gw_client.post(f"{agent.url}/llm-token/rotate", json={"immediate": True})
    assert r.status_code == 200, r.text
    third = _secret(agent)["stringData"][LLM_TOKEN_KEY]
    for gone in (old, new):
        assert mongo["llm_revocations"].find_one({"_id": claims_of(gone)["jti"]}) is not None
    assert mongo["llm_revocations"].find_one({"_id": claims_of(third)["jti"]}) is None
    _switch_off(gw_client)
    assert _reapply(gw_client, agent)
    r = gw_client.post(f"{agent.url}/llm-token/rotate", json={"immediate": True})
    assert r.status_code == 409 and r.json()["detail"]["code"] == "llm_token_missing"


def test_a_quick_return_to_the_gateway_mints_a_token_that_survives_the_cleanup(
    gw_client, mongo, fake_clusters
):
    """직접 경로로 옮긴 정리가 끝나기 전에 다시 게이트웨이로 돌아오면 회수를 기다리는 옛 토큰을
    다시 쓰지 않고 새로 낸다. 정리는 옛 jti 만 회수하고 지금 템플릿이 읽는 llm_token 은 두며, 다시
    만든 복사본은 지금 경로가 게이트웨이라 그 정리가 지운다."""
    agent = _on_gateway(gw_client, mongo, fake_clusters)
    first = claims_of(_secret(agent)["stringData"][LLM_TOKEN_KEY])["jti"]
    _switch_off(gw_client)
    assert _reapply(gw_client, agent)
    llm = gw_client.app.state.agent_service.llm
    llm.settings = dataclasses.replace(llm.settings, llm_gateway="agentgateway")
    llm._recent = RecentReads()
    assert _reapply(gw_client, agent)
    second = claims_of(_secret(agent)["stringData"][LLM_TOKEN_KEY])["jti"]
    assert second != first
    set_ready(agent.fake)
    assert _settle(gw_client) == [agent.id]
    assert mongo["llm_revocations"].find_one({"_id": first}) is not None
    assert mongo["llm_revocations"].find_one({"_id": second}) is None
    assert claims_of(_secret(agent)["stringData"][LLM_TOKEN_KEY])["jti"] == second
    assert agent.fake.get("v1", "Secret", COPY, DEFAULT_NS) is None


def test_the_sweep_leaves_a_direct_agents_leftover_token_to_the_cleanup() -> None:
    """직접 경로로 옮긴 Agent 의 남은 llm_token 은 정리가 뺄 때까지 스윕이 갱신하지 않는다."""
    from daegam.services.svc_token_sweep import _agent_tokens

    class _Fake:
        def get(self, *_a):
            return {"stringData": {LLM_TOKEN_KEY: "leftover", "mcp_token": "m"}}

    ai = SimpleNamespace(
        k8s=SimpleNamespace(token_secret="s", namespace="n"), current_token_table=None
    )
    kinds = [t.kind for t in _agent_tokens(_Fake(), ai, wanted=[]).tokens]  # type: ignore[arg-type]
    assert "llm" not in kinds
    kinds = [t.kind for t in _agent_tokens(_Fake(), ai, wanted=["p"]).tokens]  # type: ignore[arg-type]
    assert "llm" in kinds
