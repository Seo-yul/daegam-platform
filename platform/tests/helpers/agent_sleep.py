"""잠들 수 있는 Agent 를 배포하고 그 쉬는 상태를 다루는 도우미 (P44, D103).

`sleepy_agent` 는 memory 노드가 있는 템플릿 Agent 를 배포하고, init 받기로 `scale_to_zero` 를 알린
뒤 Deployment 를 Ready 로 둔다. 서비스의 비동기 메서드는 앱의 루프에서 돈다(`in_app`).
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from functools import partial
from typing import Any

from daegam.services.agent_features import SCALE_TO_ZERO
from daegam.services.agents import AgentService
from daegam.store.agents import Agent
from tests.helpers.agent_runtime import AGENT_DEPLOYMENT, DeployedAgent, deployed_agent, set_ready
from tests.helpers.consts import DEFAULT_NS

# 기록에 둘 런타임 능력. 프록시의 읽기 라우트를 모두 열고 해제의 턴 멈추기를 부르게 한다
SLEEP_FEATURES = "cancel,detached_turns,session_admin,session_files,memory_files,citations"


def service(client) -> AgentService:
    return client.app.state.agent_service


def in_app[R](client, fn: Callable[..., Awaitable[R]], *args: Any, **kw: Any) -> R:
    """앱의 루프에서 `fn(*args, **kw)` 를 돌린다."""
    return client.portal.call(partial(fn, *args, **kw))


def sleepy_agent(client, fake_clusters, mongo, *, features: str = SLEEP_FEATURES) -> DeployedAgent:
    """잠들 수 있는 Agent 다. 런타임이 `scale_to_zero` 를 알렸고 파드 하나가 Ready 다."""
    agent = deployed_agent(client, fake_clusters, mongo, features=f"{features},{SCALE_TO_ZERO}")
    set_ready(agent.fake)
    return agent


def forget_deployments(client) -> None:
    """게이트가 레플리카마다 2초 기억하는 Deployment 를 잊는다. 시험이 가짜 클러스터의 상태를
    바꾼 뒤 바로 다시 읽게 한다."""
    service(client).sleep._deployments.clear()


def stored(client, agent_id: str) -> Agent:
    return in_app(client, service(client).agents.get, agent_id)


def set_sleep(mongo, agent_id: str, **fields: Any) -> None:
    """쉬는 상태의 칸을 바로 쓴다. 다른 레플리카가 옮긴 것을 흉내 낸다."""
    mongo.agents.update_one(
        {"_id": agent_id}, {"$set": {f"sleep.{k}": v for k, v in fields.items()}}
    )


def put_to_sleep(client, agent: DeployedAgent) -> None:
    """`POST /sleep` 과 같은 걸음을 서비스로 돌리고, 0 으로 줄인 Deployment 의 파드가 사라진 상태를
    둔다(가짜 클러스터는 상태를 스스로 고치지 않는다)."""

    async def go() -> None:
        doc = await service(client).agents.get(agent.id)
        await service(client).sleep.sleep_now(doc)

    in_app(client, go)
    pods_gone(agent)


def pods_gone(agent: DeployedAgent) -> None:
    """Deployment 의 파드가 모두 사라진 상태다."""
    agent.fake.set_status("apps/v1", "Deployment", AGENT_DEPLOYMENT, DEFAULT_NS, {})


def admit(client, agent_id: str, reason: str = "turn") -> Agent:
    """턴 앞의 게이트를 돌리고 그 뒤의 문서를 준다. 깨우는 중이면 AgentError 가 오른다."""

    async def go() -> Agent:
        doc = await service(client).agents.get(agent_id)
        await service(client).sleep.admit(doc, reason)
        return doc

    return in_app(client, go)


def deployment_replicas(agent: DeployedAgent) -> int:
    dep = agent.fake.get("apps/v1", "Deployment", AGENT_DEPLOYMENT, DEFAULT_NS)
    return dep["spec"]["replicas"]


def runtime_calls(agent: DeployedAgent, method: str, path: str) -> list[dict[str, Any]]:
    assert agent.runtime is not None
    return [c for c in agent.runtime.state.calls if c["method"] == method and c["path"] == path]
