"""Agent 되돌리기와 배포 기록 (P45 6절, D109). `AgentService` 가 섞어 쓴다.

- **내용의 지문** (`spec_digest`). 리비전의 graph 와 service 를 기본값인 칸을 뺀 정렬 JSON 으로
  해시하고 판을 붙인다. 뒤 판이 기본값을 가진 칸을 더해도 지문이 바뀌지 않는다. 같은 지문은 같은
  내용이라 되돌리기와 배포 게이트(12절)가 리비전 번호 대신 이것으로 견준다.
- **배포 기록.** Agent 의 배포, 재배포, 되돌리기가 `deploying` 으로 옮기는 같은 조건부 갱신에 기록
  한 줄을 넣는다(`deploy_record_push`). 미리 보기에는 넣지 않는다. 그 배포가 끝나는 자리(provision
  의 끝 전이, 배포 경로의 실패 기록, 기동 뒤 리컨실이 마치는 배포)가 그 줄의 id 로 결과를 적는다
  (`record_deploy_end`). 마지막 20줄만 두고, 배포 쪽 필드가 아니므로 해제해도 남는다.
- **되돌리기** (`rollback`). `agent:write` 와 `agent:deploy` 로, 배포됐거나 실패한 Agent 를 리비전
  `n` 의 내용으로 돌린다. 문서의 클레임을 쥐고 `n` 의 graph 와 service 를 다음 리비전으로 저장한 뒤
  (`AgentService.save`, 지금 플랫폼으로 검증한다) 그 리비전을 그대로 재배포한다
  (`AgentService.redeploy`). `n` 이 현재 리비전과 같은 내용이면 리비전을 만들지 않고, 배포된 내용과
  같으면 409 `nothing_to_roll_back` 이다. 검증을 지나지 못하면 422 `invalid_graph` 이고 아무것도
  바뀌지 않는다. 저장 뒤 재배포가 실패하면 새 리비전은 남고 Agent 는 실패한 재배포가 남기는 꼴이다.
  재배포가 하는 일(Tool 핀, 보류 세션, 스케줄, 기억한 선택)은 모두 그대로 일어나고, 런타임 이미지,
  provider 설정, Tool 목록은 오늘의 것이다.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable, Collection
from contextlib import AbstractAsyncContextManager
from typing import Any

from pymongo.errors import PyMongoError

from daegam.auth.principal import Principal
from daegam.errors import ServiceError
from daegam.k8s.labels import content_digest
from daegam.services.evaluation.outcome import GateOutcome
from daegam.services.instance_base import EDIT_STATES
from daegam.store.agents import (
    Agent,
    AgentSpec,
    DeployAction,
    DeployRecord,
    MongoAgentStore,
)
from daegam.store.instances import DeployEnd

logger = logging.getLogger(__name__)

NOTHING_TO_ROLL_BACK = "nothing_to_roll_back"
# 지문을 짓는 규칙의 판. 규칙을 바꾸면 올린다. 판이 다른 지문은 같은 내용이어도 같지 않다
SPEC_DIGEST_VERSION = "v1"


def spec_digest(spec: AgentSpec) -> str:
    """리비전 내용(graph·service)의 지문 (6절). 리비전 번호와 만든 때는 들지 않는다.

    기본값인 칸이 빠지므로 리비전 모델(Graph, GraphNode, GraphEdge, ServiceSettings, Budget,
    Observability)의 기본값은 바꾸지 않는다. 새 칸은 옛 동작을 뜻하는 기본값으로 더한다. 기본값을
    바꾸면 새로 지은 지문이 저장된 지문과 소리 없이 달라져 배포 기록의 지문과 맞지 않는다
    (tests/test_agent_rollback.py 의 고정 지문이 지킨다)."""
    return content_digest(spec, SPEC_DIGEST_VERSION, include={"graph", "service"})


def deploy_record_push(
    doc: Agent,
    action: DeployAction,
    principal: Principal,
    *,
    restored_from: int | None = None,
    gate: GateOutcome | None = None,
) -> dict[str, Any] | None:
    """`deploying` 으로 옮기는 전이에 실을 기록 한 줄의 `$push`. 배포 쪽 스냅숏(핀한 리비전,
    이미지)을 얹은 뒤에 부른다. 미리 보기는 None 이다. gate 는 배포 게이트의 판정이고, 받아들인
    실행이나 건너뛴 이유를 줄에 남긴다 (P45 12절)."""
    if doc.preview is not None:
        return None
    pinned = doc.deployed
    assert pinned is not None, "the snapshot pins a revision before the transition"
    record = DeployRecord(
        action=action,
        revision=pinned.revision,
        restored_from=restored_from,
        spec_digest=spec_digest(pinned),
        image=doc.snapshot_image,
        by=principal.sub,
        evaluation_run=gate.run if gate is not None else None,
        skipped_reason=gate.skipped if gate is not None else None,
    )
    return doc.push_deploy_record(record)


async def record_deploy_end(store: MongoAgentStore, doc: Agent, outcome: DeployEnd) -> None:
    """그 배포의 기록 줄에 결과를 적는다. 줄은 결과를 기다리는 마지막 줄이고 핀한 리비전이
    같아야 한다. 앞 판이 시작한 재배포를 이 판이 마칠 때 앞 배포의 줄에 다른 배포의 결과를 적지
    않기 위해서다. 문서는 이미 옮겨 갔으므로 저장 실패는 경고로 남긴다."""
    record = doc.open_deploy_record
    if record is None or record.revision != doc.deployed_revision:
        return
    record.outcome = outcome
    try:
        await store.end_deploy_record(doc.id, record.id, outcome)
    except PyMongoError as e:
        logger.warning("agent %s: deploy record %s not ended: %s", doc.id, record.id, e)


class AgentRollbackMixin:
    """`AgentService` 의 되돌리기. 상태 가드, 클레임, 저장, 재배포는 서비스의 것을 쓴다."""

    # 섞는 서비스가 주는 것. 값 없는 선언이라 기반 클래스의 정의를 가리지 않는다
    _require: Callable[..., None]
    _error: Callable[..., ServiceError]
    _claim_scope: Callable[..., AbstractAsyncContextManager[None]]
    save: Callable[..., Awaitable[tuple[Agent, Collection[Any], bool]]]
    redeploy: Callable[..., Awaitable[tuple[Agent, list[str]]]]

    _judge_gate: Callable[..., Awaitable[GateOutcome]]

    async def rollback(
        self,
        doc: Agent,
        *,
        revision: int,
        principal: Principal,
        skip_evaluation: str | None = None,
    ) -> tuple[Agent, list[str]]:
        """리비전 `revision` 의 내용으로 되돌린다. (문서, 경고). 경고는 재배포의 것이다. 재배포가
        저장 검증과 같은 그래프 검증을 다시 하므로 저장의 경고를 따로 싣지 않는다. 미리 보기와 없는
        리비전은 404 다. 미리 보기는 라우트의 404 와 같은 답이다. 배포 게이트는 클레임 안에서 저장
        앞에 리비전 `revision` 의 내용을 판정하므로, 게이트가 막으면 아무것도 저장하지 않는다
        (P45 12절)."""
        if doc.preview is not None:
            raise self._error(404, "not_found", f"agent {doc.id} not found")
        self._require(doc, EDIT_STATES, action="rollback")
        if doc.get_revision(revision) is None:
            raise self._error(404, "not_found", f"revision {revision} not found")
        async with self._claim_scope(doc, EDIT_STATES, action="rollback"):
            # 클레임이 문서를 다시 읽었다. 리비전은 지우지 않으므로 그대로 있다
            source = doc.get_revision(revision)
            assert source is not None
            deployed = doc.deployed
            if deployed is not None and spec_digest(source) == spec_digest(deployed):
                raise self._error(
                    409,
                    NOTHING_TO_ROLL_BACK,
                    f"revision {revision} has the deployed content",
                    revision=revision,
                )
            gate = await self._judge_gate(doc, source, skip_evaluation)
            doc, _issues, _created = await self.save(
                doc,
                display_name=doc.display_name,
                spec=AgentSpec(graph=source.graph, service=source.service),
                layout=doc.layout,
                principal=principal,
            )
            return await self.redeploy(doc, principal=principal, restored_from=revision, gate=gate)
