"""미리 보기를 쓰는 테스트의 도우미 (P44b, D107).

`plain_spec` 은 memory 노드도 승인도 분당 한도도 없는 템플릿 spec 이다. `start_preview` 는 미리 보기
서비스로 그 사람의 미리 보기를 시작하거나 옮기고, `deployed_preview` 는 배포한 원본의 미리 보기를
시작한다. `start_evaluation_preview` 는 평가 실행의 미리 보기를 그 리비전으로 연다(P45 9절).
`source_doc` 은 스토어가 읽은 원본이고 `preview_service` 는 앱의 미리 보기 서비스다. `agent_json`
은 파드가 기능 이름을 알리며 받은 agent.json 의 응답이다.
"""

from __future__ import annotations

from typing import Any

import httpx

from daegam.auth.principal import Principal
from daegam.services.agent_features import FEATURES_HEADER, IMAGE_HEADER
from daegam.services.agent_preview import AgentPreviews
from daegam.store.agents import Agent
from tests.helpers.agent_runtime import DeployedAgent, token_of
from tests.helpers.agents import node_of, template_for, without_memory
from tests.helpers.auth import bearer
from tests.helpers.consts import AG, IMAGE

# 헤더 모드 테스트 클라이언트의 사람
HEADER_SUB = "header"


def plain_spec(gw: dict[str, Any], ds: dict[str, Any]) -> dict[str, Any]:
    """`template_for` 에서 memory 노드, 분당 한도, 승인을 뺀 spec 이다."""
    spec = without_memory(template_for(gw, ds))
    node_of(spec, "entry")["data"].pop("rate_limit", None)
    node_of(spec, "tools-calculator")["data"].pop("approval", None)
    return spec


def preview_service(client) -> AgentPreviews:
    return client.app.state.agent_previews


def agent_json(client, fake, doc: Agent, names: frozenset[str]) -> httpx.Response:
    """doc 의 파드가 IMAGE 와 기능 names 를 알리며 받은 agent.json 의 응답이다."""
    headers = {
        **bearer(token_of(fake, doc)),
        IMAGE_HEADER: IMAGE,
        FEATURES_HEADER: ",".join(sorted(names)),
    }
    return client.get(f"{AG}/{doc.id}/agent.json", headers=headers)


def source_doc(client, agent_id: str) -> Agent:
    doc = client.portal.call(client.app.state.agents.get, agent_id)
    assert doc is not None
    return doc


def start_preview(client, agent_id: str, sub: str = HEADER_SUB) -> Agent:
    """sub 의 미리 보기를 원본의 최신 리비전으로 시작하거나 옮긴 문서다."""
    source = source_doc(client, agent_id)
    return client.portal.call(preview_service(client).start, source, Principal(sub=sub))


def start_evaluation_preview(
    client, agent_id: str, revision: int, run: str, sub: str = HEADER_SUB
) -> Agent:
    """sub 가 시작한 평가 실행 run 의 미리 보기를 그 리비전으로 연 문서다."""
    source = source_doc(client, agent_id)
    return client.portal.call(
        preview_service(client).start_evaluation, source, Principal(sub=sub), revision, run
    )


def deployed_preview(client, agent: DeployedAgent, *, sub: str = HEADER_SUB) -> Agent:
    """배포한 원본 agent 의 미리 보기. 원본의 클러스터와 네임스페이스에 뜬다."""
    preview = start_preview(client, agent.id, sub)
    assert preview.state == "deployed", preview.last_error
    return preview
