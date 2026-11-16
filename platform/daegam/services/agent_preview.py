"""Agent 미리 보기 (P44b, D107). `agent:deploy` 를 가진 사람이 Agent 의 저장한 최신 리비전을
배포하지 않고 자기만의 짧은 미리 보기로 띄운다.

- **문서.** 미리 보기는 `agents` 의 문서이고 `preview` 표시를 가진다(`store/agents.py`). 이름은
  자기 id 라 Agent 이름(32자)과 겹치지 않는다. 정의는 원본의 최신 리비전을 같은 번호로 하되 memory
  노드와 그 간선을 뺀 것이다(`preview_spec`). 그래서 agent.json 의 `memory` 가 null 이고 세션,
  승인, 카운터, 전사가 파드 안에 살다 함께 사라진다. 그 리비전이 원본에서 메인에 memory 노드를
  가졌으면 agent.json 이 문서 기능 `in_pod_state` 를 실어 그것을 런타임에 알리고, 런타임은 memory
  노드 없이도 승인을 거절하지 않고 묻는다(`AgentService._preview_asks`). 원본에 memory 노드가
  없었으면 배포한 원본처럼 승인이 필요한 호출을 거절한다. 배포 검증은 공유 카운터를 요구하는
  `memory_required` 를 보지 않는다(`GraphContext.in_pod_state`).
- **시작과 옮기기** (`start`). 한 사람이 Agent 하나에 하나만 연다(부분 고유 인덱스). 이미 있으면
  그 자리에서 최신 리비전으로 옮기고(재배포라 파드가 굴러가 대화가 사라진다) 끝은 처음 시작 때의
  것 그대로다. 새로 열면 테넌트 자리 하나를 잡고(없으면 409 `preview_limit`) 원본이 배포된
  클러스터와 네임스페이스에, draft 면 배포 기본 자리에 파드 하나로 배포한다. 노출, 실행, 스케줄,
  트리거, 잠들기는 없다. 시작과 옮기기는 사람마다 테넌트마다 UTC 하루 20번이다(429
  `preview_start_limit`).
- **일일 예산** (`check_budget`). 리비전의 `daily_tokens` 가 있으면 원본의 오늘(UTC) 원장 합계가
  그에 닿을 때 채팅·결정·다시 생성을 429 `daily_budget_exceeded` 로 막는다. 원장은 미리 보기의
  행도 원본으로 센다. 원본의 런타임은 제 턴만 세므로 하루 합이 예산의 두 배까지 갈 수 있다.
- **평가 미리 보기** (`start_evaluation`, P45 9절). 평가 실행이 고른 리비전으로 연다. 표시의
  `run` 이 그 실행이고 시작한 사람이 주인이다. 사람의 미리 보기와 함께 열리고(인덱스의 열쇠에
  실행 id), `mine` 과 서랍의 라우트에 닿지 않으며 열린 미리 보기 목록에는 실행과 함께 든다. 자리,
  하루 20번의 시작, 자격의 끝, 끝나는 까닭은 사람의 미리 보기와 같다. 옮기지 않고 본문 기록
  (`record_bodies`)은 꺼 둔다. 끝나도 서랍이 읽는 끝 기록은 남지 않는다. 컴파일이 쓰기 Tool 을
  잡고 파드 안의 승인 기능을 요구하는 것은 `AgentService` 다.
- **끝** (`end`). 마지막 활동 30분 뒤, 처음 시작 2시간 뒤, 시작한 사람이나 `agent:deploy` 를 가진
  다른 사람이 끝낼 때, 시작한 사람이 그 테넌트의 `agent:deploy` 나 계정을 잃을 때, 원본을 지울 때,
  `DAEGAM_AGENT_PREVIEWS_ENABLED` 를 끌 때 끝난다. 끝은 Agent 삭제다(턴 멈춤, 토큰 회수, 객체와
  문서 지우기). 까닭은 그 사람의 서랍이 읽게 남기고 자리를 놓는다. 끝내기 워커
  (`AgentPreviewWorker`)가 때가 된 것과 원본이 없는 것을 끝내고, 미리 보기가 없거나 그 자격이 끝난
  자리를 놓는다.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Any

from pymongo.errors import DuplicateKeyError

from daegam.auth.principal import Principal
from daegam.config import Settings
from daegam.errors import ServiceError, retry_after_header
from daegam.services.agent_features import status_runtime
from daegam.services.agent_graph import without_nodes
from daegam.services.lease_worker import QueuedLeaseWorker
from daegam.store.agent_previews import MongoAgentPreviewStore, PreviewEnd, PreviewEndReason
from daegam.store.agents import Agent, AgentPreview, AgentRevision, AgentSpec, MongoAgentStore
from daegam.store.base import iso_utc, new_id, to_millis, utcnow
from daegam.store.leases import Lease
from daegam.store.memberships import MongoMembershipStore
from daegam.store.usage import MongoUsageStore, agent_records

if TYPE_CHECKING:
    from daegam.services.agents import AgentService

logger = logging.getLogger("daegam.agent_preview")

DEPLOY_SCOPE = "agent:deploy"
# 마지막 활동 뒤 이만큼 쓰지 않으면, 처음 시작 뒤 이만큼 지나면 끝난다 (10절)
PREVIEW_IDLE = timedelta(minutes=30)
PREVIEW_MAX_AGE = timedelta(hours=2)
# 사람마다 테넌트마다 UTC 하루의 시작과 옮기기 수 (7절)
STARTS_PER_DAY = 20
# 미리 보기 문서가 없는 자리를 놓기 전에 기다리는 시간. 시작이 자리를 잡고 문서를 넣는 사이다
SLOT_GRACE = timedelta(minutes=10)

PREVIEW_LIMIT = "preview_limit"
PREVIEW_START_LIMIT = "preview_start_limit"
PREVIEW_NOT_FOUND = "preview_not_found"
PREVIEW_DISABLED = "preview_disabled"
DAILY_BUDGET_EXCEEDED = "daily_budget_exceeded"


class PreviewError(ServiceError):
    pass


def preview_spec(revision: AgentRevision) -> AgentRevision:
    """미리 보기가 도는 리비전. 같은 번호이고 memory 노드와 그 간선이 빠져 상태가 파드 안에 산다
    (7절). 장기 기억이 없고 tenant 의 Database 에 아무 행도 남지 않는다. knowledge 노드는 그대로
    자기 Database 를 쓴다."""
    memory = {n.id for n in revision.graph.nodes if n.type == "memory"}
    return without_nodes(revision, memory)


def evaluation_spec(revision: AgentRevision) -> AgentRevision:
    """평가 미리 보기가 도는 리비전 (P45 9절). 미리 보기의 것에서 본문 기록을 끈다. 그래서 어떤
    메시지와 결과도 이벤트와 OTLP 내보내기에 닿지 않는다."""
    spec = preview_spec(revision)
    observability = spec.service.observability.model_copy(update={"record_bodies": False})
    service = spec.service.model_copy(update={"observability": observability})
    return spec.model_copy(update={"service": service})


def last_activity(preview: Agent) -> datetime:
    """마지막 활동. 턴, 결정, 다시 생성이 적는 활동(`AgentSleepControl.mark`)과 시작, 옮기기다."""
    assert preview.preview is not None
    activity = preview.sleep.activity
    return max(activity, preview.preview.started_at) if activity else preview.preview.started_at


def idle_end(preview: Agent) -> datetime:
    """쓰지 않아 끝나는 때. 처음 시작 2시간 뒤를 넘지 않는다."""
    assert preview.preview is not None
    return min(last_activity(preview) + PREVIEW_IDLE, preview.preview.ends_at)


def _source_of(preview: Agent) -> str:
    assert preview.preview is not None
    return preview.preview.source


def _due_at(preview: Agent, now: datetime) -> datetime:
    """끝낼 때가 된 때. 스위치를 끈 것은 지금이다."""
    assert preview.preview is not None
    return min(preview.preview.ends_at, idle_end(preview), now)


def due_reason(preview: Agent, now: datetime, *, enabled: bool) -> PreviewEndReason | None:
    """끝낼 때가 된 까닭. 아니면 None 이다."""
    assert preview.preview is not None
    if not enabled:
        return "disabled"
    if now >= preview.preview.ends_at:
        return "max_age"
    if now >= idle_end(preview):
        return "idle"
    return None


class AgentPreviews:
    """미리 보기의 시작, 옮기기, 끝, 일일 예산. 배포 일은 `AgentService` 에 맡긴다. 그 서비스가
    원본의 삭제에서 이것을 부르므로 이것을 먼저 만들고 서비스는 뒤에 붙인다."""

    late_hooks = frozenset({"service"})

    def __init__(
        self,
        agents: MongoAgentStore,
        store: MongoAgentPreviewStore,
        usage: MongoUsageStore,
        memberships: MongoMembershipStore,
        settings: Settings,
    ):
        self.agents = agents.with_previews()
        self.store = store
        self.usage = usage
        self.memberships = memberships
        self.settings = settings
        # 배포와 삭제를 하는 서비스. 배선이 만든 뒤에 붙인다
        self.service: AgentService | None = None

    @property
    def enabled(self) -> bool:
        return self.settings.agent_previews_enabled

    def _service(self) -> AgentService:
        assert self.service is not None, "AgentPreviews.service is not wired"
        return self.service

    # -- 찾기 ---------------------------------------------------------------------------------

    async def mine(self, source: Agent, sub: str) -> Agent | None:
        """그 사람이 그 Agent 에 연 미리 보기. 없으면 None 이다."""
        return await self.agents.preview_of(source.tenant, source.id, sub)

    async def require_mine(self, source: Agent, sub: str) -> Agent:
        """그 사람의 미리 보기. 없으면 404 `preview_not_found` 이고, 하루 안에 끝났으면 그 까닭을
        `ended` 로 싣는다. 끝날 때가 지났는데 워커가 아직 끝내지 않은 것도 그 까닭으로 404 다.
        그래서 워커가 멈춰도 끝 뒤에는 턴도 턴 토큰도 나지 않는다 (8절)."""
        found = await self.mine(source, sub)
        if found is not None:
            now = utcnow()
            reason = due_reason(found, now, enabled=self.enabled)
            if reason is None:
                return found
            ended: dict[str, Any] = {
                "reason": reason,
                "at": iso_utc(_due_at(found, now)),
                "by": None,
            }
        else:
            record = await self.store.end_of(source.tenant, source.id, sub)
            ended = _ended_out(record) if record is not None else {}
        extra = {"ended": ended} if ended else {}
        raise PreviewError(404, PREVIEW_NOT_FOUND, f"no open preview of agent {source.id}", **extra)

    async def of_source(self, source: Agent) -> list[Agent]:
        """그 Agent 에 열린 미리 보기 모두."""
        return await self.agents.previews_of(source.tenant, source.id)

    async def require_of_source(self, source: Agent, preview_id: str) -> Agent:
        """그 Agent 에 열린 미리 보기 하나. 다른 Agent 의 것이거나 없으면 404
        `preview_not_found` 다."""
        found = next((p for p in await self.of_source(source) if p.id == preview_id), None)
        if found is None:
            raise PreviewError(404, PREVIEW_NOT_FOUND, f"no open preview {preview_id}")
        return found

    async def view(self, preview: Agent) -> dict[str, Any]:
        """화면이 읽는 미리 보기 하나 (6절). phase 는 Deployment 를 한 번 읽은 것이다. warnings 는
        마지막 init 받기가 남긴 런타임 경고다(`runtime_feature_dropped` 등, `status_runtime`)."""
        info = preview.preview
        assert info is not None
        status = await self._service().status(preview)
        _runtime, warnings = status_runtime(preview)
        return {
            "id": preview.id,
            "source_id": info.source,
            "owner": info.owner,
            "revision": preview.deployed_revision or preview.revision,
            "state": preview.state,
            "phase": status["phase"] if status is not None else None,
            "last_error": preview.last_error,
            "warnings": warnings,
            "started_at": info.started_at,
            "ends_at": info.ends_at,
            "last_activity_at": last_activity(preview),
            "idle_ends_at": idle_end(preview),
            "idle_minutes": int(PREVIEW_IDLE.total_seconds() // 60),
            "max_hours": int(PREVIEW_MAX_AGE.total_seconds() // 3600),
            "run": info.run,
        }

    # -- 시작과 옮기기 ------------------------------------------------------------------------

    def _check_startable(self, source: Agent) -> None:
        """스위치가 꺼졌으면 409 `preview_disabled`, 원본이 미리 보기이거나 지우는 중이면 409
        `state_conflict` 다."""
        if not self.enabled:
            raise PreviewError(409, PREVIEW_DISABLED, "previews are turned off on this platform")
        if source.preview is not None or source.state == "deleting":
            raise PreviewError(409, "state_conflict", f"agent {source.id} cannot be previewed")

    async def _existing(self, find: Callable[[], Awaitable[Agent | None]]) -> Agent | None:
        """find 가 찾은 열린 미리 보기. 앞의 시작이 배포 전에 멈춰 남은 draft(요청 취소 등)는 지우고
        다시 찾는다. 지우는 중인 것은 409 `in_progress` 다."""
        found = await find()
        if found is not None and found.state == "draft":
            await self._discard(found)
            found = await find()
        if found is not None and found.state == "deleting":
            raise PreviewError(409, "in_progress", "the previous preview is still ending")
        return found

    async def start(self, source: Agent, principal: Principal) -> Agent:
        """그 사람의 미리 보기를 저장한 최신 리비전으로 띄운다. 이미 그 리비전이면 그대로 돌려준다.
        다른 리비전이면 그 자리에서 옮기고, 없으면 새로 연다."""
        self._check_startable(source)

        async def find() -> Agent | None:
            return await self.mine(source, principal.sub)

        existing = await self._existing(find)
        if existing is None:
            return await self._open(source, principal, preview_spec(source.current))
        # 옮기기는 리비전을 먼저 들이고 재배포한다. 재배포가 실패했으면 리비전만 새것이고 파드는
        # 앞의 리비전이라, 배포한 리비전까지 같아야 그대로 돌려주고 아니면 다시 옮긴다
        if existing.revision == source.revision and existing.deployed_revision == source.revision:
            return existing
        return await self._move(existing, source, principal)

    async def start_evaluation(
        self, source: Agent, principal: Principal, revision: int, run: str
    ) -> Agent:
        """평가 실행 run 의 미리 보기를 그 리비전으로 연다 (P45 9절). 스위치와 원본의 상태는
        `start` 와 같이 본다. 자리가 없으면 409 `preview_limit`, 그 사람의 하루 시작을 다 썼으면 429
        `preview_start_limit` 이다. 리비전이 없으면 404 다. 이미 그 실행의 미리 보기가 있으면 그것을
        돌려주고, 배포 전에 멈춰 남은 draft 는 지우고 새로 연다."""
        self._check_startable(source)
        chosen = source.get_revision(revision)
        if chosen is None:
            raise PreviewError(404, "not_found", f"revision {revision} of agent {source.id}")

        async def find() -> Agent | None:
            return await self.agents.preview_of_run(source.tenant, source.id, run)

        existing = await self._existing(find)
        if existing is not None:
            return existing
        return await self._open(source, principal, evaluation_spec(chosen), run=run)

    async def _open(
        self, source: Agent, principal: Principal, spec: AgentRevision, *, run: str | None = None
    ) -> Agent:
        """spec(`preview_spec` 이나 `evaluation_spec` 을 지난 리비전)으로 새 미리 보기를 연다. run
        은 평가 실행의 id 다. 평가 미리 보기는 그 사람의 앞선 끝 기록(`forget_end`)을 지우지 않는다.
        그 기록은 그 사람이 연 미리 보기의 것이라 서랍이 읽기 때문이다."""
        service = self._service()
        now = to_millis(utcnow())
        doc = _new_preview(source, principal.sub, now, spec, run=run)
        per_tenant = self.settings.agent_previews_per_tenant
        if await self.store.take_slot(source.tenant, doc.id, per_tenant, now) is None:
            raise PreviewError(
                409,
                PREVIEW_LIMIT,
                f"previews are limited to {per_tenant} per tenant and none is free",
                limit=per_tenant,
            )
        try:
            await self._count_start(source.tenant, principal.sub, now)
            try:
                await self.agents.create(doc)
            except DuplicateKeyError as e:
                raise PreviewError(409, "in_progress", "a preview of this agent is starting") from e
            if run is None:
                await self.store.forget_end(source.tenant, source.id, principal.sub)
            target = source.k8s
            doc, _issues = await service.deploy(
                doc,
                cluster_id=source.cluster_id if target is not None else None,
                namespace=target.namespace if target is not None else None,
                replicas=1,
                resources=source.resources,
                resources_sent=True,
                exposure=None,
                exposure_sent=True,
                scale_to_zero=False,
                principal=principal,
            )
        except BaseException:
            await self._discard(doc)
            raise
        return await service.provision(doc)

    async def _move(self, preview: Agent, source: Agent, principal: Principal) -> Agent:
        """원본의 최신 리비전으로 옮긴다. 재배포라 파드가 굴러가 지금 대화가 사라진다. 끝은 처음
        시작 때의 것 그대로이고 토큰도 같은 끝으로 난다 (7절, 8절)."""
        service = self._service()
        await self._count_start(source.tenant, principal.sub, to_millis(utcnow()))
        await service.adopt_revision(
            preview, preview_spec(source.current), display_name=source.display_name
        )
        moved, _warnings = await service.redeploy(preview, principal=principal)
        return moved

    async def _count_start(self, tenant: str, sub: str, now: datetime) -> None:
        count, day_ends = await self.store.count_start(tenant, sub, now)
        if count > STARTS_PER_DAY:
            raise PreviewError(
                429,
                PREVIEW_START_LIMIT,
                f"previews can be started or moved {STARTS_PER_DAY} times a day",
                headers=retry_after_header(day_ends - now),
                limit=STARTS_PER_DAY,
            )

    async def _discard(self, doc: Agent) -> None:
        """시작이 배포 전에 멈춘 미리 보기를 지우고, 문서가 없으면 자리를 놓는다. 배포로 넘어간
        문서는 그대로 두어 리컨실이 잇고 끝내기 워커가 끝낸다."""
        try:
            await self.agents.delete_draft(doc.id)
            if await self.agents.get(doc.id) is None:
                await self.store.release_slot(doc.id)
        except Exception as e:  # noqa: BLE001 - 남은 자리는 끝내기 워커가 놓는다
            logger.warning("preview %s: not discarded after a failed start: %s", doc.id, e)

    # -- 일일 예산 ----------------------------------------------------------------------------

    async def check_budget(self, preview: Agent) -> None:
        """핀한 리비전의 `daily_tokens` 에 원본의 오늘(UTC) 원장 합계가 닿았으면 429 다 (7절)."""
        info = preview.preview
        pinned = preview.deployed
        limit = pinned.service.budget.daily_tokens if pinned is not None else None
        if info is None or not limit:
            return
        now = utcnow()
        day_starts = now.replace(hour=0, minute=0, second=0, microsecond=0)
        day_ends = day_starts + timedelta(days=1)
        match = {**agent_records(preview.tenant, info.source), "ts": {"$gte": day_starts}}
        used = await self.usage.tokens_used(match)
        if used >= limit:
            raise PreviewError(
                429,
                DAILY_BUDGET_EXCEEDED,
                f"the agent has used its daily budget of {limit} tokens",
                headers=retry_after_header(day_ends - now),
                used=used,
                limit=limit,
            )

    # -- 끝 -------------------------------------------------------------------------------

    async def end(self, preview: Agent, reason: PreviewEndReason, *, by: str | None = None) -> bool:
        """미리 보기를 끝낸다. 까닭을 남기고 Agent 삭제를 부른다. 지웠으면 자리를 놓고 참이다. 남이
        쥔 클레임에 일을 남겼으면 거짓이고 문서는 `deleting` 이라 리컨실이 마저 지운다."""
        info = preview.preview
        assert info is not None
        if info.run is None:
            # 끝 기록은 그 사람이 연 미리 보기의 것이고 서랍이 읽는다. 평가 미리 보기는 남기지
            # 않는다 (P45 9절)
            await self.store.record_end(
                tenant=preview.tenant,
                source=info.source,
                owner=info.owner,
                preview_id=preview.id,
                reason=reason,
                by=by,
                now=utcnow(),
            )
        fresh = await self.agents.get(preview.id)
        if fresh is not None:
            _doc, revoked = await self._service().delete(fresh)
            if revoked is None:
                return False
        await self.store.release_slot(preview.id)
        logger.info("preview %s of agent %s ended: %s", preview.id, info.source, reason)
        return True

    async def end_for_source(self, source: Agent) -> int:
        """원본을 지운 뒤 그 미리 보기를 모두 끝낸다 (`AgentService.delete`)."""
        return await self._end_each(await self.of_source(source), "source_deleted")

    async def end_for_person(self, sub: str, tenant: str | None) -> int:
        """그 사람이 테넌트나 계정을 잃었을 때의 훅(`AccessRevoker.on_lost`). tenant 가 None 이면
        모든 테넌트의 것이다."""
        previews = await self.agents.previews_of_owner(sub, tenant=tenant)
        return await self._end_each(previews, "access_lost")

    async def end_if_scope_lost(self, sub: str, tenant: str) -> int:
        """역할이 낮아진 사람의 훅(`AccessRevoker.on_role_lowered`). 그 테넌트에서 `agent:deploy`
        를 잃었으면 그 테넌트의 미리 보기를 끝낸다. scope 는 요청의 주체와 같은 규칙으로 센다
        (`Principal.scopes_in`, platform-admin 의 `*` 줄 포함)."""
        person = Principal(sub=sub, memberships=await self.memberships.list_for_sub(sub))
        if DEPLOY_SCOPE in person.scopes_in(tenant):
            return 0
        return await self.end_for_person(sub, tenant)

    async def _end_each(self, previews: list[Agent], reason: PreviewEndReason) -> int:
        ended = 0
        for preview in previews:
            try:
                ended += await self.end(preview, reason)
            except Exception as e:  # noqa: BLE001 - 하나의 실패가 나머지를 막지 않는다. 워커가 잇는다
                logger.warning("preview %s not ended (%s): %s", preview.id, reason, e)
        return ended

    # -- 끝내기 워커의 일 ---------------------------------------------------------------------

    async def due(self, now: datetime) -> list[Agent]:
        """끝낼 미리 보기. 때가 됐거나 스위치가 꺼졌거나 원본이 없는 것이다. 지우는 중인 것은
        리컨실이 마저 지운다. 끝내기 전에 `end_if_due` 가 다시 본다."""
        previews = [p for p in await self.agents.all_previews() if p.state != "deleting"]
        sources = await self.agents.get_many({_source_of(p) for p in previews})
        return [
            p
            for p in previews
            if self._end_reason(p, now, source_found=_source_of(p) in sources) is not None
        ]

    async def end_if_due(self, preview: Agent, now: datetime) -> bool:
        """워커가 리스를 쥔 뒤 문서를 다시 읽고 아직 끝낼 때일 때만 끝낸다. 틱과 끝 사이에 턴이
        와 활동이 앞으로 갔으면 그대로 둔다."""
        fresh = await self.agents.get(preview.id)
        if fresh is None or fresh.state == "deleting":
            return False
        source = await self.agents.get(_source_of(fresh))
        reason = self._end_reason(fresh, now, source_found=source is not None)
        return reason is not None and await self.end(fresh, reason)

    def _end_reason(
        self, preview: Agent, now: datetime, *, source_found: bool
    ) -> PreviewEndReason | None:
        reason = due_reason(preview, now, enabled=self.enabled)
        if reason is None and not source_found:
            return "source_deleted"
        return reason

    async def free_slots(self, now: datetime) -> None:
        """미리 보기가 없거나 그 자격이 끝난 자리를 놓는다 (10절). 시작이 실패했거나 플랫폼이 끝
        전에 멈춰 남은 것이다. 놓은 자리는 로그 한 줄에 남긴다."""
        previews = {p.id: p for p in await self.agents.all_previews()}
        freed: list[str] = []
        for slot in await self.store.all_slots():
            found = previews.get(slot.preview_id)
            if found is None:
                stale = now - slot.taken_at >= SLOT_GRACE
            else:
                stale = found.preview is not None and now >= found.preview.credentials_end
            if stale and await self.store.release_slot(slot.preview_id):
                freed.append(slot.preview_id)
        if freed:
            logger.info("preview slots freed: %s", ", ".join(freed))


def _new_preview(
    source: Agent, sub: str, now: datetime, spec: AgentRevision, *, run: str | None = None
) -> Agent:
    """spec(`preview_spec` 이나 `evaluation_spec` 을 지난 리비전)을 도는 draft 미리 보기 문서.
    이름은 자기 id 다 (5절). run 은 평가 실행의 id 다 (P45 9절)."""
    preview_id = new_id()
    return Agent(
        id=preview_id,
        tenant=source.tenant,
        name=preview_id,
        display_name=source.display_name,
        spec=AgentSpec(graph=spec.graph, service=spec.service),
        revision=spec.revision,
        revisions=[spec],
        replicas=1,
        resources=source.resources,
        scale_to_zero=False,
        preview=AgentPreview(
            source=source.id,
            source_name=source.name,
            owner=sub,
            started_at=now,
            ends_at=now + PREVIEW_MAX_AGE,
            run=run,
        ),
        updated_by=sub,
    )


def _ended_out(end: PreviewEnd) -> dict[str, Any]:
    return {"reason": end.reason, "at": iso_utc(end.at), "by": end.by}


class AgentPreviewWorker(QueuedLeaseWorker[Agent]):
    """끝내기 워커 (10절). 틱마다 남은 자리를 놓고, 끝낼 미리 보기마다 리스 `agent-preview:<id>` 를
    걸어 한 레플리카만 끝낸다."""

    task_prefix = "agent-preview"
    tick_label = "agent preview tick"
    item_label = "agent preview"
    log = logger

    def __init__(
        self, previews: AgentPreviews, lease: Lease, *, owner: str | None = None, interval: float
    ):
        super().__init__(lease, owner=owner, interval=interval)
        self.previews = previews

    async def queue(self, now: datetime) -> list[Agent]:
        await self.previews.free_slots(now)
        return await self.previews.due(now)

    async def handle(self, item: Agent, now: datetime) -> None:
        await self.previews.end_if_due(item, now)


__all__ = [
    "DAILY_BUDGET_EXCEEDED",
    "PREVIEW_DISABLED",
    "PREVIEW_LIMIT",
    "PREVIEW_NOT_FOUND",
    "PREVIEW_START_LIMIT",
    "AgentPreviewWorker",
    "AgentPreviews",
    "PreviewError",
    "evaluation_spec",
    "preview_spec",
]
