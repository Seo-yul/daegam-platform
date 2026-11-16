"""배포한 Agent 와 그 런타임 프록시를 보는 테스트가 함께 쓰는 도우미 (D21, D26, D42, D61).

fake 클러스터의 기본 네임스페이스(`DEFAULT_NS`)에 배포한 Agent 에서 인스턴스 토큰·Deployment
(`agent_deployment`)·agent 컨테이너·init 이 받는 agent.json 을 읽고 Deployment 를 Ready 로 둔다.
`deployed_agent` 는 `agent_env` 를 깐 플랫폼에 Agent 를 배포하고 그 결과(`DeployedAgent`)를 준다.
spec 은 그 env 의 도구 소스와 Database 로 만드는 함수(tests/helpers/agents.py 의 `template_for`
등)로 고른다. 가짜 런타임(tests/helpers/runtime.py)은 기본으로 붙고 runtime=False 면 붙이지
않는다. 렌더만 보는 테스트는 최소 그래프의 배포된 문서(`minimal_agent_doc`)와 유효 설정
(tests/helpers/docs.py 의 `effective_config`)을 템플릿 함수에 바로 넘긴다.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from fastapi import FastAPI

from daegam.services.agent_features import FEATURES_HEADER, IMAGE_HEADER
from daegam.store.agents import Agent
from tests.helpers.agents import (
    MINIMAL_SPEC,
    agent_env,
    agent_env_with_openai,
    deploy_spec,
    template_for,
)
from tests.helpers.auth import bearer
from tests.helpers.consts import AG, DEFAULT_NS, IMAGE
from tests.helpers.docs import agent_doc
from tests.helpers.fake_cluster import FakeClusterClient
from tests.helpers.fake_secrets import FakeSecretStore
from tests.helpers.runtime import ATTACHMENT_FEATURES, fake_runtime

# 템플릿 Agent(이름 support)의 Deployment 이름
AGENT_DEPLOYMENT = "agent-support"


def token_of(fake, inst: Agent | dict[str, Any]) -> str:
    """배포가 토큰 Secret 에 넣은 인스턴스 토큰의 평문. inst 는 API 응답이나 Agent 문서다."""
    if isinstance(inst, Agent):
        assert inst.k8s is not None
        name = inst.k8s.token_secret
    else:
        name = inst["k8s"]["token_secret"]
    return fake.get("v1", "Secret", name, DEFAULT_NS)["stringData"]["token"]


def agent_deployment(
    fake, name: str = AGENT_DEPLOYMENT, namespace: str = DEFAULT_NS
) -> dict[str, Any] | None:
    """namespace(기본 네임스페이스)에 렌더된 Agent Deployment name. 없으면 None."""
    return fake.get("apps/v1", "Deployment", name, namespace)


def set_ready(fake, name: str = AGENT_DEPLOYMENT, *, replicas: int = 1) -> None:
    """Deployment name 의 상태를 replicas 개가 모두 Ready 인 롤아웃 끝으로 둔다."""
    dep = agent_deployment(fake, name)
    fake.set_status(
        "apps/v1",
        "Deployment",
        name,
        DEFAULT_NS,
        {
            "observedGeneration": dep["metadata"]["generation"],
            "replicas": replicas,
            "readyReplicas": replicas,
            "updatedReplicas": replicas,
            "availableReplicas": replicas,
        },
    )


def agent_container(fake, name: str = AGENT_DEPLOYMENT) -> dict[str, Any]:
    """Deployment name 의 첫 컨테이너(agent)."""
    return agent_deployment(fake, name)["spec"]["template"]["spec"]["containers"][0]


def agent_json(client, fake, inst) -> dict[str, Any]:
    """init 이 받는 문서. 토큰은 방금 provision 이 Secret 에 쓴 것이다."""
    r = client.get(f"{AG}/{inst['id']}/agent.json", headers=bearer(token_of(fake, inst)))
    assert r.status_code == 200, r.text
    return r.json()


def env_map(container: dict) -> dict:
    """env 이름 → 값(또는 valueFrom). 빈 문자열 값(비어 있는 목록 env)도 그대로 든다."""
    return {e["name"]: e["value"] if "value" in e else e["valueFrom"] for e in container["env"]}


# -- 렌더 입력 -----------------------------------------------------------------------------------


def minimal_agent_doc(**kw) -> Agent:
    """배포된 Agent 문서 하나. 렌더만 보므로 그래프는 최소 형태다."""
    return agent_doc(spec=MINIMAL_SPEC, name="bot", state="deployed", **kw)


# -- 배포한 템플릿 Agent -------------------------------------------------------------------------

# agent_env 가 깐 도구 소스 Gateway 문서와 Database 문서로 배포할 spec 을 만드는 함수
SpecBuilder = Callable[[dict[str, Any], dict[str, Any]], dict[str, Any]]


@dataclass
class DeployedAgent:
    """`deployed_agent` 가 배포한 Agent 다. `doc` 은 배포 응답이고 `fake` 는 배포한 fake 클러스터,
    `cluster_id`·`ds`·`gw` 는 `agent_env` 가 깐 플랫폼 기본 클러스터·Database·도구 소스 Gateway
    문서다. `runtime` 은 붙인 가짜 런타임이고 붙이지 않았으면 None 이다. `build` 는 배포한 spec 을
    만든 함수다."""

    doc: dict[str, Any]
    fake: FakeClusterClient
    cluster_id: str
    ds: dict[str, Any]
    gw: dict[str, Any]
    runtime: FastAPI | None = None
    build: SpecBuilder = template_for

    @property
    def id(self) -> str:
        return self.doc["id"]

    @property
    def url(self) -> str:
        return f"{AG}/{self.id}"

    @property
    def token(self) -> str:
        """토큰 Secret 의 지금 값이다. 재배포가 토큰을 새로 내면 새 값을 읽는다."""
        return token_of(self.fake, self.doc)

    @property
    def auth(self) -> dict[str, str]:
        """인스턴스 토큰을 단 Authorization 헤더다. init 과 런타임이 플랫폼을 부를 때 쓴다."""
        return bearer(self.token)

    def spec(self) -> dict[str, Any]:
        """배포한 정의와 같은 spec 을 새로 만든다. 고쳐 보내도 배포한 것은 그대로다."""
        return self.build(self.gw, self.ds)


def deployed_agent(
    client,
    fake_clusters,
    mongo,
    *,
    spec: SpecBuilder = template_for,
    openai_secrets: FakeSecretStore | None = None,
    runtime: bool = True,
    features: str | None = None,
) -> DeployedAgent:
    """agent_env 를 깔고 spec 이 만든 Agent 를 배포한다. openai_secrets(fake_secrets)를 주면 키
    Secret 이 따로인 openai provider 까지 깐다(`agent_env_with_openai`). runtime 이면 가짜 런타임을
    붙이고, features 를 주면 init 이 그 이름을 알린 기록을 남긴다."""
    if openai_secrets is None:
        cluster_id, ds, gw = agent_env(client, mongo)
    else:
        cluster_id, ds, gw = agent_env_with_openai(client, mongo, openai_secrets)
    _p, doc = deploy_spec(client, spec(gw, ds))
    agent = DeployedAgent(doc, fake_clusters.by_name["edge"], cluster_id, ds, gw, build=spec)
    if runtime:
        agent.runtime = fake_runtime(agent.token)
        agent.fake.service_app = agent.runtime
    if features is not None:
        headers = {**agent.auth, IMAGE_HEADER: IMAGE, FEATURES_HEADER: features}
        assert client.get(f"{agent.url}/agent.json", headers=headers).status_code == 200
    return agent


def ready_agent(client, fake_clusters, mongo, features: str = ATTACHMENT_FEATURES) -> DeployedAgent:
    """첨부를 아는 런타임이 Ready 인 Agent 다."""
    agent = deployed_agent(client, fake_clusters, mongo, features=features)
    set_ready(agent.fake)
    assert client.get(agent.url).json()["status"]["phase"] == "Ready"
    return agent


def chat_upload(client, agent_id: str, name: str, body: bytes, media_type: str = "text/plain"):
    """채팅 첨부 하나를 올린 응답."""
    return client.post(f"{AG}/{agent_id}/uploads", files={"file": (name, body, media_type)})


def skip_start_check(app) -> None:
    """재적용이 Agent 를 굴리기 전에 하는 검사(`AgentService._check_start`, P50 1절)를 건너뛴다.
    Gateway 소스를 가짜 문서로 깔았다가 지우는 테스트가 쓴다. 지운 소스로는 새 파드의 컴파일이
    어긋나 재적용이 막히기 때문이다. 그 검사 자체는 tests/test_render_epoch_reapply.py 가 본다."""

    async def passes(_doc, _services=None) -> None:
        return None

    app.state.agent_service._check_start = passes
