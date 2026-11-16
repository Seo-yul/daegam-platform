"""비서 서비스 (D31, P14 F1b). 세션 CRUD, 턴 태스크 시작(Lease), 결정 재개, SSE 생성기, 종료.

턴은 요청과 분리된 백그라운드 태스크다. `asyncio.create_task` 로 돌며 리스·저장·이벤트 큐를 갖고,
SSE 생성기는 큐를 읽기만 한다. 클라이언트가 끊기면 생성기만 끝나고 태스크는 다음 멈춤(interrupt·
done·error·timeout)까지 간다. 생성기가 끝나면 큐를 닫고 태스크는 닫힌 큐에 넣지 않는다(전사는
문서에 있다). 태스크 참조는 집합으로 붙들어 GC 되지 않게 한다.

턴은 `store/leases.py` 의 `Lease` 로 잡는다. 이름은 `assistant-session:{sid}`(세션당 하나)와
`assistant-turn:{sub}`(사용자당 하나), holder 는 `{pod_id}/{sid}/{turn_uuid}` 라 턴마다 다르고 리스
자체가 배타적이다(도구 호출마다 같은 holder 로 다시 acquire 해 갱신한다). ttl 은
`turn_timeout_seconds + 30` 이다. 둘 중 하나라도 못 잡으면 409 `turn_in_progress`. 같은 파드 안의
경합은 리스보다 앞에서 막는다. `running` 검사와 자리표시 등록 사이에 await 가 없어 두 요청이 같이
지나가지 못하고, 자리표시는 실패 경로에서 걷는다. 턴이 끝나면 두 리스를 release 한다.

`Services.stop()` 이 살아 있는 턴 태스크를 취소하고, 태스크의 `finally` 는 `CancelledError` 에서도
`anyio.CancelScope(shield=True)` 안에서 전사·문맥을 저장하고 두 리스를 release 한다.

비밀의 한계. 도구 인자 안의 비밀(표의 `secret_fields`)은 루프가 이벤트·전사·문맥·pending 에 넣기
전에 `***` 로 마스킹하고 실행기가 `secret_in_chat` 으로 거부한다. 그러나 사용자가 메시지 본문에
직접 적은 비밀은 HumanMessage·전사의 user 항목·첫 메시지에서 온 제목에 그대로 남는다. 그것을 걷을
규칙이 없기 때문이다. 시스템 프롬프트가 비밀을 묻지 말라고 못 박는 이유가 이것이다.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass
from datetime import timedelta
from functools import partial
from typing import Any

import anyio
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import message_to_dict, messages_from_dict
from pymongo.asynchronous.database import AsyncDatabase
from starlette.types import ASGIApp

from daegam.auth.principal import Principal
from daegam.config import Settings
from daegam.errors import ServiceError
from daegam.secrets import SecretStore
from daegam.services.assistant.loop import Decision, Limits, Outcome, TurnContext, TurnLoop
from daegam.services.assistant.model import build_chat_model
from daegam.services.assistant.proposals import Proposals, Review, proposal_of
from daegam.services.assistant.schema import ToolDef
from daegam.services.assistant.tools import Refused, ToolExecutor, dumps
from daegam.services.llm_endpoint import Endpoint, LlmEndpoints
from daegam.services.llm_providers import ProviderError, provider_named
from daegam.services.usage.ledger import CallMeter, CallSite, UsageLedger, metered
from daegam.store.assistant_sessions import (
    DEFAULT_TITLE,
    TRANSCRIPT_LIMIT,
    AssistantSession,
    MongoAssistantSessionStore,
    PendingCall,
    Usage,
)
from daegam.store.leases import Lease, lease_held
from daegam.store.settings import LLMProvider, MongoPlatformSettingsStore, PlatformSettings
from daegam.store.usage import UsageSource

logger = logging.getLogger("daegam.assistant")

SSE_KEEPALIVE_SECONDS = 15.0
LEASE_GRACE_SECONDS = 30
# 원장 행의 부른 쪽 이름 (P37 7절)
ASSISTANT_SOURCE = "daegam"
TITLE_MAX = 60

ModelFactory = Callable[[LLMProvider, str], BaseChatModel | Awaitable[BaseChatModel]]


class AssistantError(ServiceError):
    """비서 서비스 오류. main.py 의 ServiceError 핸들러가 `{detail: {code, detail}}` 로 낸다."""


def session_lease_name(session_id: str) -> str:
    return f"assistant-session:{session_id}"


def turn_lease_name(sub: str) -> str:
    return f"assistant-turn:{sub}"


def sse(name: str, data: dict[str, Any]) -> bytes:
    return f"event: {name}\ndata: {dumps(data)}\n\n".encode()


class EventQueue:
    """태스크 → SSE 생성기. 생성기가 끝나면 닫히고 그 뒤의 put 은 버려진다. None 이 끝 표시다."""

    def __init__(self) -> None:
        self._queue: asyncio.Queue[tuple[str, dict[str, Any]] | None] = asyncio.Queue()
        self.closed = False

    def put(self, item: tuple[str, dict[str, Any]] | None) -> None:
        if not self.closed:
            self._queue.put_nowait(item)

    async def get(self) -> tuple[str, dict[str, Any]] | None:
        return await self._queue.get()

    def close(self) -> None:
        self.closed = True


@dataclass(frozen=True)
class TurnLeases:
    """턴 하나가 쥐는 리스 두 개의 이름. 세션당 하나(`assistant-session:{sid}`)와 사용자당
    하나(`assistant-turn:{sub}`)이고 둘 다 잡아야 턴이 시작된다."""

    session: str
    user: str

    @property
    def both(self) -> tuple[str, str]:
        """푸는 순서. 잡은 순서와 같아도 되므로 그대로 돈다."""
        return (self.session, self.user)


@dataclass
class TurnHandle:
    session_id: str
    queue: EventQueue
    task: asyncio.Task[Outcome | None]


class AssistantService:
    def __init__(
        self,
        sessions: MongoAssistantSessionStore,
        platform_settings: MongoPlatformSettingsStore,
        settings: Settings,
        *,
        asgi_app: ASGIApp,
        tool_defs: list[ToolDef],
        db: AsyncDatabase,
        secret_store: Callable[[], SecretStore | None],
        usage_ledger: UsageLedger | None = None,
        endpoints: LlmEndpoints | None = None,
    ) -> None:
        self.sessions = sessions
        self.platform_settings = platform_settings
        self.settings = settings
        self.tool_defs = tool_defs
        self.executor = ToolExecutor(asgi_app, tool_defs, settings=settings)
        # 기다리는 제안의 다시 읽기 (P48 6.1절). 턴 루프는 같은 실행기로 제 것을 만든다
        self.proposals = Proposals(self.executor)
        self.db = db
        # 턴마다 모델의 길을 고른다 (P38 6절). 없으면(테스트) 늘 직접 경로다
        self.endpoints = endpoints or LlmEndpoints(settings, secret_store)
        # 모델 호출을 원장에 남긴다 (P37 7절). 없으면(테스트) 남기지 않는다
        self.usage_ledger = usage_ledger
        self.tasks: set[asyncio.Task[Any]] = set()
        # 이 파드에서 도는(또는 막 시작하는) 턴. 값이 None 이면 시작 중인 자리표시다
        self.running: dict[str, TurnHandle | None] = {}
        # 테스트가 가짜 모델·백오프 대기를 꽂는 자리
        self.model_factory: ModelFactory | None = None
        self.sleep: Callable[[float], Awaitable[None]] = asyncio.sleep

    # -- 설정 -------------------------------------------------------------------------------

    def resolve_provider(self, ps: PlatformSettings) -> LLMProvider | None:
        """defaults.assistant, 없으면 defaults.spec_generator, 그것도 없으면 첫 provider. stub 은
        배포본이 허용할 때만."""
        provider = ps.assistant_provider()
        if provider is None:
            return None
        if provider.is_stub and not self.settings.allow_stub_provider:
            return None
        return provider

    def _selectable(self, provider: LLMProvider) -> bool:
        """홈의 모델 목록에 실을 provider 인가 (D45). stub 은 배포본이 허용할 때만이다. 키 유무는
        K8s Secret 을 읽어야 알 수 있으므로 여기서 거르지 않는다. 키가 없는 것을 고르면 첫 턴이
        422 provider_no_key 로 그 자리에서 말한다."""
        return self.settings.allow_stub_provider if provider.is_stub else True

    def choose_provider(self, ps: PlatformSettings, name: str | None) -> LLMProvider:
        """대화가 쓸 provider 를 정한다 (D45). 이름을 준 자리에는 폴백이 없다. 어느 모델과
        이야기하는지가 이 결정의 요지라, 세션에 붙어 있던 provider 가 설정에서 사라졌어도 조용히
        기본으로 넘어가지 않고 422 unknown_provider 로 멈춘다. 키 Secret 이름이 비어 있으면
        provider_no_key, stub 은 배포본이 허용할 때만이다. 이름이 없으면 설정의 기본이다."""
        if not name:
            provider = self.resolve_provider(ps)
            if provider is None:
                raise AssistantError(
                    422, "provider_unavailable", "no LLM provider is configured for the assistant"
                )
            return provider
        provider = provider_named(ps.llm_providers, name)
        if provider is None:
            raise AssistantError(
                422, "unknown_provider", f"llm provider {name!r} is not configured", provider=name
            )
        if not self._selectable(provider):
            raise AssistantError(
                422,
                "stub_provider_disallowed",
                f"llm provider {name!r} is a stub and this deployment does not allow stubs",
                provider=name,
            )
        if not provider.is_stub and not provider.api_key_secret:
            raise AssistantError(
                422,
                "provider_no_key",
                f"llm provider {name!r} has no api_key_secret",
                provider=name,
            )
        return provider

    async def config(self) -> dict[str, Any]:
        ps = await self.platform_settings.get()
        provider = self.resolve_provider(ps)
        enabled = bool(ps.assistant.enabled and provider is not None)
        return {
            "enabled": enabled,
            "provider": provider.name if provider is not None else None,
            # 비서가 꺼져 있으면 고를 자리가 없으니 빈 목록이다. 이름·종류·모델만 싣는다.
            # Secret 이름과 base_url 은 설정 읽기 권한이 있어야 볼 것이라 여기로 새지 않는다
            "providers": [
                {"name": p.name, "kind": p.kind, "model": p.effective_model}
                for p in (ps.llm_providers if enabled else [])
                if self._selectable(p)
            ],
            "confirm_deploy": ps.assistant.confirm_deploy,
            "limits": {
                "max_tool_calls_per_turn": ps.assistant.max_tool_calls_per_turn,
                "turn_timeout_seconds": ps.assistant.turn_timeout_seconds,
            },
        }

    async def enabled(self) -> bool:
        """`assistant.enabled` 플래그만. provider 가 없으면 config 는 enabled=false 이지만 세션 API
        는 열려 있고 턴 시작이 422 로 알린다."""
        return (await self.platform_settings.get()).assistant.enabled

    # -- 세션 --------------------------------------------------------------------------------

    async def list_sessions(self, tenant: str, sub: str) -> list[dict[str, Any]]:
        return await self.sessions.list(tenant, sub)

    async def create_session(self, tenant: str, sub: str, title: str | None) -> AssistantSession:
        session = AssistantSession(
            tenant=tenant, sub=sub, title=(title or "").strip() or DEFAULT_TITLE
        )
        return await self.sessions.create(session)

    async def get_session(self, session_id: str, *, tenant: str, sub: str) -> AssistantSession:
        """만든 사람만 본다. 다른 사람 것은 없는 것과 같고, 테넌트가 다르면 409 tenant_mismatch."""
        session = await self.sessions.get(session_id)
        if session is None or session.sub != sub:
            raise AssistantError(404, "session_not_found", f"session {session_id} not found")
        if session.tenant != tenant:
            raise AssistantError(
                409,
                "tenant_mismatch",
                f"session belongs to tenant {session.tenant!r}, request is for {tenant!r}",
                session_tenant=session.tenant,
            )
        return session

    async def turn_in_progress(self, session_id: str) -> bool:
        """이 파드의 태스크 목록과 `assistant-session:{sid}` 리스(다른 파드의 턴)를 함께 본다."""
        if session_id in self.running:
            return True
        return await lease_held(self.db, session_lease_name(session_id))

    async def delete_session(self, session_id: str, *, tenant: str, sub: str) -> None:
        """삭제는 감사 행의 via 를 지우지 않는다. 어느 파드에서든 도는 턴이 있으면 409."""
        session = await self.get_session(session_id, tenant=tenant, sub=sub)
        if await self.turn_in_progress(session.id):
            raise AssistantError(409, "turn_in_progress", "a turn is still running")
        await self.sessions.delete(session.id)

    # -- 턴 ----------------------------------------------------------------------------------

    async def start_turn(
        self,
        session_id: str,
        *,
        tenant: str,
        principal: Principal,
        message: str,
        ui_context: dict[str, Any] | None,
        request_id: str,
        provider: str | None = None,
    ) -> TurnHandle:
        session = await self.get_session(session_id, tenant=tenant, sub=principal.sub)

        def prepare() -> None:
            session.turns += 1
            session.turn_tool_calls = 0
            session.turn_usage = Usage()
            if session.title == DEFAULT_TITLE and session.turns == 1:
                session.title = " ".join(message.split())[:TITLE_MAX] or DEFAULT_TITLE
            session.ui_context = ui_context

        async def run(loop: TurnLoop, ctx: TurnContext) -> Outcome:
            return await loop.run_message(ctx, message, ui_context)

        return await self._launch(
            session, principal, tenant, request_id, prepare, run, requested=provider
        )

    async def decide(
        self,
        session_id: str,
        *,
        tenant: str,
        principal: Principal,
        decisions: list[Decision],
        request_id: str,
    ) -> TurnHandle:
        session = await self.get_session(session_id, tenant=tenant, sub=principal.sub)
        pending = session.pending
        if session.state != "pending" or pending is None:
            raise AssistantError(409, "not_pending", "session has no pending approval")
        if pending.kind != "confirm":
            raise AssistantError(
                409, "not_pending", "session is waiting for an answer; send a message instead"
            )
        if len(decisions) != len(pending.calls):
            raise AssistantError(
                422,
                "decision_count_mismatch",
                f"{len(pending.calls)} pending call(s) need {len(pending.calls)} decision(s), "
                f"got {len(decisions)}",
                expected=[c.id for c in pending.calls],
            )
        expected = {c.id for c in pending.calls}
        given = [d.id for d in decisions]
        if set(given) != expected or len(set(given)) != len(given):
            raise AssistantError(
                422,
                "decision_mismatch",
                "decision ids must match pending.calls ids exactly",
                expected=[c.id for c in pending.calls],
            )
        self._check_digests(pending.calls, decisions)

        async def run(loop: TurnLoop, ctx: TurnContext) -> Outcome:
            return await loop.run_decisions(ctx, decisions)

        return await self._launch(session, principal, tenant, request_id, lambda: None, run)

    def _check_digests(self, calls: list[PendingCall], decisions: list[Decision]) -> None:
        """제안 호출의 결정은 승인이든 거절이든 그 제안의 지문을 싣는다 (P48 6.1절). 없거나 다르면
        409 proposal_changed 다. 오래된 탭, 지문을 빼는 클라이언트, 호출 id 를 되풀이하는 provider
        가 사람이 보지 않은 내용을 결정하지 못하게 한다. 지문을 실은 결정이 제안이 아닌 호출에
        닿아도 409 다(제안 카드의 결정이 같은 id 의 다른 호출을 결정하지 못하게). 예약 칸 없이
        기다리는 제안 도구(앞 판이 만든 호출)는 지문 없이 거절만 된다(10절). 화면은 이 409 를
        읽는다. 같은 코드의 다른 두 길은 `proposals.py` 의 도구 결과와 `proposal` 의 409 다."""
        proposal_tools = {d.name for d in self.tool_defs if d.spec.proposal}
        by_id = {c.id: c for c in calls}
        for decision in decisions:
            call = by_id[decision.id]
            reserved = proposal_of(call.args)
            if reserved is None:
                old_proposal = call.name in proposal_tools and decision.type == "approve"
                if decision.proposal_digest is None and not old_proposal:
                    continue
            elif decision.proposal_digest == reserved.get("digest"):
                continue
            raise AssistantError(
                409,
                "proposal_changed",
                "the proposal is not the one this decision was made on; read the session again "
                "and decide on the current proposal",
                proposal_id=(reserved or {}).get("id"),
            )

    async def proposal(
        self,
        session_id: str,
        *,
        tenant: str,
        principal: Principal,
        request_id: str,
        review: Review,
    ) -> dict[str, Any]:
        """기다리는 제안을 기준 리비전 위에 다시 짜 맞춰 돌려준다 (P48 6.1절). 세션을 만든 사람만
        보고, 기다리는 제안이 없으면 404 proposal_not_found 다. 대상 Agent 를 읽지 못하면 그
        상태(404·403)를 그대로 낸다. 그 기준 위에 다시 짜이지 않으면 409 proposal_changed 다.
        캔버스가 이것으로 제안을 열고 오래된 기준을 알린다. review 는 감사 행을 남기지 않는 검증이다
        (화면의 읽기가 청지기의 일로 남지 않게)."""
        session = await self.get_session(session_id, tenant=tenant, sub=principal.sub)
        pending = session.pending
        call = pending.calls[0] if pending is not None and pending.kind == "confirm" else None
        if call is None or proposal_of(call.args) is None:
            raise AssistantError(404, "proposal_not_found", "no proposal waits in this session")
        ctx = TurnContext(
            principal=principal,
            tenant=tenant,
            session_id=session.id,
            request_id=request_id,
            turn=session.turns,
        )
        try:
            return await self.proposals.view(ctx, 0, call.name, call.args, review)
        except Refused as e:
            content = e.result.content if isinstance(e.result.content, dict) else {}
            status = e.result.http_status
            code, detail = str(content.get("code") or ""), str(content.get("detail") or "")
            if status is None:
                # 검사를 지난 제안이 그 기준 위에 다시 짜이지 않는다. 화면은 새 제안을 기다린다
                status, code = 409, "proposal_changed"
            raise AssistantError(status, code, detail) from e

    async def _model(
        self, ps: PlatformSettings, session: AssistantSession, requested: str | None, tenant: str
    ) -> tuple[LLMProvider, Endpoint, BaseChatModel]:
        """이 턴이 쓸 (provider, 길, 모델). 요청이 provider 를 주면 그것이 이 대화에 붙고(D45),
        주지 않으면 세션이 기억한 것, 그것도 없으면 설정의 기본이다. 승인 재개는 요청에 provider 가
        없으므로 세션의 것으로 이어져 승인 한 번에 모델이 바뀌지 않는다. 길은 턴마다 고른다
        (P38 6절)."""
        provider = self.choose_provider(ps, requested or session.provider)
        if requested:
            session.provider = requested
        try:
            endpoint = await self.endpoints.resolve_endpoint(
                provider, tenant=tenant, purpose="assistant"
            )
        except ProviderError as e:
            raise AssistantError(
                422, "provider_unavailable", e.detail, provider=provider.name
            ) from e
        if self.model_factory is not None:
            made = self.model_factory(provider, ps.assistant.effort)
            model = await made if asyncio.iscoroutine(made) else made
            return provider, endpoint, model  # type: ignore[return-value]
        chat = build_chat_model(provider, effort=ps.assistant.effort, endpoint=endpoint)
        return provider, endpoint, chat

    async def _acquire_leases(
        self, session: AssistantSession, principal: Principal, ttl: timedelta
    ) -> tuple[Lease, TurnLeases]:
        """턴의 리스 둘을 잡는다. 세션당 하나와 사용자당 하나이고 holder 가 턴마다 달라 리스
        자체가 배타적이다. 둘째를 못 잡으면 첫째를 풀고 409 라, 반쯤 잡힌 상태로 나가지 않는다."""
        holder = f"{self.settings.pod_id}/{session.id}/{uuid.uuid4().hex}"
        lease = Lease(self.db, holder, ttl=ttl)
        leases = TurnLeases(session_lease_name(session.id), turn_lease_name(principal.sub))
        if not await lease.acquire(leases.session):
            raise AssistantError(409, "turn_in_progress", "a turn is running for this session")
        if not await lease.acquire(leases.user):
            await lease.release(leases.session)
            raise AssistantError(409, "turn_in_progress", "a turn is running in another session")
        return lease, leases

    def _build_loop(
        self,
        session: AssistantSession,
        ps: PlatformSettings,
        model: BaseChatModel,
        lease: Lease,
        leases: TurnLeases,
        queue: EventQueue,
        meter: Callable[[], AbstractAsyncContextManager[CallMeter]],
    ) -> TurnLoop:
        """세션 문서의 상태와 설정으로 턴 루프를 조립한다. 이벤트 싱크·리스 갱신·체크포인트·모델
        호출의 계량이 서비스가 넣는 콜백이고, 루프 자신은 저장을 모른다."""

        async def emit(name: str, data: dict[str, Any]) -> None:
            queue.put((name, data))

        async def renew() -> bool:
            return await lease.acquire(leases.session) and await lease.acquire(leases.user)

        loop = TurnLoop(
            model=model,
            tool_defs=self.tool_defs,
            executor=self.executor,
            emit=emit,
            renew=renew,
            checkpoint=lambda: self._persist(session, loop, "pending"),
            limits=Limits(
                max_tool_calls=ps.assistant.max_tool_calls_per_turn,
                timeout=float(ps.assistant.turn_timeout_seconds),
            ),
            confirm_deploy=ps.assistant.confirm_deploy,
            messages=messages_from_dict(session.context),
            transcript=list(session.transcript),
            files=dict(session.files),
            usage=session.usage.model_copy(),
            turn_usage=session.turn_usage.model_copy(),
            tool_calls=session.turn_tool_calls,
            pending=session.pending,
            ui_context=session.ui_context,
            tool_groups=set(session.tool_groups),
            sleep=self.sleep,
            meter=meter,
        )
        return loop

    def _meter(
        self,
        session: AssistantSession,
        principal: Principal,
        tenant: str,
        provider: LLMProvider,
        endpoint: Endpoint,
    ) -> Callable[[], AbstractAsyncContextManager[CallMeter]]:
        """이 턴의 모델 호출 계량. 턴 하나가 trace 하나이고 호출마다 행이 하나다 (P37 7절)."""
        site = CallSite(
            tenant=tenant,
            source=UsageSource(kind="assistant", id=ASSISTANT_SOURCE, name=ASSISTANT_SOURCE),
            provider=provider,
            model=provider.effective_model,
            ref=session.id,
            user_sub=principal.sub,
            base_url=endpoint.base_url,
        )
        return partial(metered, self.usage_ledger, site)

    async def _launch(
        self,
        session: AssistantSession,
        principal: Principal,
        tenant: str,
        request_id: str,
        prepare: Callable[[], None],
        run: Callable[[TurnLoop, TurnContext], Awaitable[Outcome]],
        requested: str | None = None,
    ) -> TurnHandle:
        """턴 태스크를 띄운다. `running` 검사와 자리표시 등록 사이에 await 가 없어야 한다. 그래야
        같은 세션의 두 요청이 같이 지나가지 못한다. prepare 는 자리를 잡은 뒤 세션 문서를 고치는
        부분이다.

        정리 경로가 둘이고 되돌리는 것이 다르다. 안쪽 `try` 는 저장이 실패했을 때 잡은 리스를
        풀고, 바깥 `except BaseException` 은 `running` 의 자리표시를 걷는다. 태스크가 뜬 뒤의
        정리는 `_run_task` 의 `finally` 가 맡는다."""
        if session.id in self.running:
            raise AssistantError(409, "turn_in_progress", "a turn is already running")
        self.running[session.id] = None
        try:
            prepare()
            ps = await self.platform_settings.get()
            provider, endpoint, model = await self._model(ps, session, requested, tenant)
            ttl = timedelta(seconds=ps.assistant.turn_timeout_seconds + LEASE_GRACE_SECONDS)
            lease, leases = await self._acquire_leases(session, principal, ttl)
            queue = EventQueue()
            meter = self._meter(session, principal, tenant, provider, endpoint)
            loop = self._build_loop(session, ps, model, lease, leases, queue, meter)
            loop.gateway = endpoint.gateway
            ctx = TurnContext(
                principal=principal,
                tenant=tenant,
                session_id=session.id,
                request_id=request_id,
                turn=session.turns,
            )
            try:
                await self._persist(session, loop, "running")
            except Exception:
                # 저장이 안 되면 턴을 띄우지 않는다. 잡은 리스를 TTL 까지 남기지 않는다
                for name in leases.both:
                    await lease.release(name)
                raise
        except BaseException:
            self.running.pop(session.id, None)
            raise
        task = asyncio.create_task(
            self._run_task(session, loop, ctx, lease, leases, queue, run),
            name=f"assistant-turn:{session.id}",
        )
        handle = TurnHandle(session_id=session.id, queue=queue, task=task)
        self.tasks.add(task)
        self.running[session.id] = handle
        task.add_done_callback(self.tasks.discard)
        return handle

    async def _run_task(
        self,
        session: AssistantSession,
        loop: TurnLoop,
        ctx: TurnContext,
        lease: Lease,
        leases: TurnLeases,
        queue: EventQueue,
        run: Callable[[TurnLoop, TurnContext], Awaitable[Outcome]],
    ) -> Outcome | None:
        outcome: Outcome | None = None
        try:
            outcome = await run(loop, ctx)
            return outcome
        except asyncio.CancelledError:
            queue.put(
                (
                    "error",
                    {"session_id": session.id, "code": "turn_failed", "message": "turn cancelled"},
                )
            )
            raise
        except Exception as e:  # noqa: BLE001 - 루프의 버그가 리스를 쥔 채 죽으면 안 된다
            logger.exception("assistant turn %s crashed", session.id)
            # 원인 문장은 로그에만 남긴다. 루프의 `_run` 처럼 화면에는 예외의 종류만 준다
            data = loop.record_error(
                session.id,
                "turn_failed",
                f"turn crashed ({type(e).__name__}); details are in the server log",
            )
            queue.put(("error", data))
            return Outcome("error", "turn_failed")
        finally:
            # 취소(롤링 업데이트)나 예외에서도 전사·문맥·리스 정리는 끝까지 간다
            with anyio.CancelScope(shield=True):
                if outcome is None:
                    # 도구 실행 도중 끊겼으면 그 tool_call 을 닫아 다음 턴의 문맥을 지킨다
                    loop.close_open_calls("turn_failed", "turn ended before this call finished")
                # 상태는 루프의 pending 이 정한다. 체크포인트 도중 끊겨도 pending 이 있으면
                # pending 이다
                state = "pending" if loop.pending is not None else "idle"
                try:
                    await self._persist(session, loop, state)
                except Exception:  # noqa: BLE001
                    logger.exception("assistant session %s save failed", session.id)
                for name in leases.both:
                    try:
                        await lease.release(name)
                    except Exception:  # noqa: BLE001
                        logger.exception("assistant lease %s release failed", name)
                self.running.pop(session.id, None)
                queue.put(None)
                queue.close()

    async def _persist(self, session: AssistantSession, loop: TurnLoop, state: str) -> None:
        session.context = [message_to_dict(m) for m in loop.messages]
        session.transcript = loop.transcript[-TRANSCRIPT_LIMIT:]
        session.pending = loop.pending
        session.files = loop.files
        session.usage = loop.usage
        session.turn_usage = loop.turn_usage
        session.turn_tool_calls = loop.tool_calls
        session.tool_groups = sorted(loop.tool_groups)
        session.state = state  # type: ignore[assignment]
        await self.sessions.save(session)

    # -- SSE ----------------------------------------------------------------------------------

    async def events(self, handle: TurnHandle) -> AsyncIterator[bytes]:
        """큐를 읽어 SSE 로 낸다. 15초 동안 이벤트가 없으면 `: keepalive` 주석을 내 프록시가 연결을
        끊지 않게 한다. 어떻게 끝나든 큐를 닫아 태스크가 더 넣지 않게 한다."""
        queue = handle.queue
        try:
            while True:
                try:
                    item = await asyncio.wait_for(queue.get(), timeout=SSE_KEEPALIVE_SECONDS)
                except TimeoutError:
                    yield b": keepalive\n\n"
                    continue
                if item is None:
                    return
                yield sse(*item)
        finally:
            queue.close()

    # -- 종료 ---------------------------------------------------------------------------------

    async def stop(self) -> None:
        """살아 있는 턴 태스크를 취소하고 finally 의 저장·release 가 끝나기를 기다린다."""
        tasks = [t for t in self.tasks if not t.done()]
        for t in tasks:
            t.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self.running.clear()


__all__ = [
    "LEASE_GRACE_SECONDS",
    "SSE_KEEPALIVE_SECONDS",
    "AssistantError",
    "AssistantService",
    "EventQueue",
    "TurnHandle",
    "TurnLeases",
    "session_lease_name",
    "sse",
    "turn_lease_name",
]
