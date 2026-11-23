"""Agent 의 허브 게시 (P46 6.1절). 게시한 Agent 만 다른 Agent 의 Tool 이 된다.

게시는 턴을 받는 Agent 의 배포된 리비전이 호출의 전제 조건을 채울 때만 된다. 호출 하나는 그
Agent 의 백그라운드 실행(P41)이므로 전제 조건은 실행의 것과 같다. memory 노드와 세션 예산, 이벤트
수집이고, 런타임 이미지는 `runs` 와 `turn_tokens` 를 알려야 한다. 전제 조건의 판정은 실행 서비스의
것(`RunService.check_revision`)을 그대로 부르고 코드만 호출의 것으로 옮긴다.

항목은 Agent 의 것이 아니라 허브의 것이다. 해제는 항목의 manage 로 하고, 그것을 Tool 로 쓰는
Agent 가 있어도 된다. 게시한 Agent 는 해제하기 전에는 내리지 못하고, 지우면 항목도 지워진다.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Protocol

from daegam.auth.hub_access import Access
from daegam.auth.principal import Principal
from daegam.config import Settings
from daegam.errors import ServiceError
from daegam.services.agent_features import RUNS, RUNTIME_FEATURE_MISSING, TURN_TOKENS
from daegam.services.hub import HubError, HubService
from daegam.store.agents import Agent, AgentRevision
from daegam.store.hub import HubItem

# 호출을 받는 Agent 의 런타임 이미지가 알려야 하는 능력
CALL_FEATURES: tuple[str, ...] = (RUNS, TURN_TOKENS)
# 배포된 리비전에 memory 노드나 세션 예산이 없다
RUNS_UNMET = "agent_source_runs_unmet"
# 실행의 전제 조건 가운데 `RUNS_UNMET` 으로 옮기는 것. 나머지(이벤트 수집)는 실행의 코드 그대로다
_UNMET_RUN_CODES = frozenset({"runs_need_memory", "run_budget_required"})
CALLS_DISABLED = "agent_calls_disabled"
# 게시한 Agent 의 배포가 전제 조건을 잃었다는 경고
CALLS_BROKEN_BY_DEPLOY = "agent_calls_broken_by_deploy"


class RevisionCheck(Protocol):
    """리비전 하나가 실행의 전제 조건을 채우는지 보고, 아니면 `ServiceError` 를 올린다
    (`RunService.check_revision`)."""

    def __call__(self, revision: AgentRevision, message: str | None) -> None: ...


def calls_enabled(settings: Settings) -> bool:
    """이 플랫폼에서 Agent 가 다른 Agent 를 부를 수 있는가 (P46 6.5절). 호출은 실행이라 실행이
    꺼지면 함께 꺼진다."""
    return settings.agent_calls_on


def revision_refusal(revision: AgentRevision, check: RevisionCheck) -> ServiceError | None:
    """리비전이 호출을 받지 못하는 까닭. 받을 수 있으면 None 이다. memory 노드와 세션 예산은
    `RUNS_UNMET` 하나로 답한다."""
    try:
        check(revision, None)
    except ServiceError as e:
        if e.code in _UNMET_RUN_CODES:
            return ServiceError(
                409,
                RUNS_UNMET,
                "an agent called as a tool needs a memory node and a session token budget",
            )
        return e
    return None


class AgentPublishing:
    """Agent 하나의 게시와 그 항목을 보는 가드. 상태 가드는 Agent 서비스의 것을 받는다."""

    def __init__(
        self,
        *,
        hub: HubService,
        settings: Settings,
        check_revision: RevisionCheck | None,
        require_serving: Callable[[Agent], None],
        error_cls: type[ServiceError],
    ):
        self.hub = hub
        self.settings = settings
        # 실행 서비스가 없으면(시험) 호출이 꺼진 것으로 본다
        self.check_revision = check_revision
        self.require_serving = require_serving
        self.error_cls = error_cls

    async def publish(
        self,
        doc: Agent,
        *,
        name: str | None = None,
        display_name: str | None = None,
        description: str,
        calls_trusted: bool = False,
        access: Access | None = None,
        principal: Principal,
    ) -> tuple[HubItem, bool]:
        """허브에 `agent` 항목으로 게시한다. (항목, 새로 만들었는지). 호출이 꺼져 있으면 409
        agent_calls_disabled, 턴을 받지 않는 Agent 면 409 not_deployed·in_progress·deleting 이다.
        이미 게시돼 있으면 전제 조건을 다시 보지 않고 그 항목을 돌려준다."""
        if not self.enabled:
            raise self.error_cls(409, CALLS_DISABLED, "agent calls are turned off on this platform")
        self.require_serving(doc)
        existing = await self.hub.store.get_by_agent(doc.id)
        if existing is not None:
            return existing, False
        refusal = self.call_refusal(doc)
        if refusal is not None:
            raise self.error_cls(refusal.status, refusal.code, refusal.detail, **refusal.extra)
        try:
            return await self.hub.publish_agent(
                doc,
                name=name,
                display_name=display_name,
                description=description,
                calls_trusted=calls_trusted,
                access=access,
                principal=principal,
            )
        except HubError as e:
            raise self.error_cls(e.status, e.code, e.detail, **e.extra) from e

    @property
    def enabled(self) -> bool:
        """이 플랫폼에서 호출이 켜져 있는가. 실행 서비스가 없으면 꺼진 것이다."""
        return self.check_revision is not None and calls_enabled(self.settings)

    def call_refusal(self, doc: Agent) -> ServiceError | None:
        """턴을 받는 Agent 가 호출을 받지 못하는 까닭. 받을 수 있으면 None 이다. 리비전은 409
        agent_source_runs_unmet·runs_need_events, 이미지는 409 runtime_feature_missing 이다. 게시와,
        그 Agent 를 Tool 로 쓰는 Agent 의 저장과 배포가 같은 것을 본다 (P46 6.2절)."""
        revision = doc.deployed
        assert revision is not None  # 턴을 받는 Agent 다
        if self.check_revision is not None:
            refusal = revision_refusal(revision, self.check_revision)
            if refusal is not None:
                return refusal
        for feature in CALL_FEATURES:
            if not doc.image_reports(feature):
                return ServiceError(
                    409,
                    RUNTIME_FEATURE_MISSING,
                    f"the deployed runtime image has not reported the {feature!r} capability",
                    feature=feature,
                )
        return None

    async def refuse_undeploy(self, doc: Agent) -> None:
        """게시한 Agent 는 내리지 못한다 (409 published). 먼저 게시를 해제한다."""
        if await self.hub.store.get_by_agent(doc.id) is not None:
            raise self.error_cls(
                409, "published", f"agent {doc.name} is published to the hub; unpublish first"
            )

    async def deploy_warning(self, doc: Agent) -> str | None:
        """게시한 Agent 의 새 배포가 memory 노드나 세션 예산을 잃었으면 그 경고의 문장이다. 그
        Agent 를 Tool 로 쓰는 Agent 의 호출이 거절된다. 이미지의 능력은 새 파드가 알리기 전이라
        보지 않는다."""
        revision = doc.deployed
        if revision is None or self.check_revision is None:
            return None
        refusal = revision_refusal(revision, self.check_revision)
        if refusal is None or refusal.code != RUNS_UNMET:
            return None
        if await self.hub.store.get_by_agent(doc.id) is None:
            return None
        return (
            "this deploy dropped the memory node or the session token budget, so agents that "
            "call this agent as a tool are refused"
        )

    async def forget(self, doc: Agent) -> None:
        """Agent 를 지울 때 그 게시 항목을 지운다."""
        await self.hub.unregister_agent(doc.id)
