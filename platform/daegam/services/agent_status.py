"""Agent 상태 (P72 2.1절). 상태의 틀은 `instance_status.InstanceStatusMixin` 이고 여기는 Agent 의
훅이다. 읽은 Deployment 를 phase 로 옮기고, 파드가 준비됐으면 런타임 /metrics 로 세션·보류 수와
경고를 읽는다. `AgentStatusView` 는 서비스가 생성자에서 만들어 가드·클라이언트를 콜러블로 넘긴다
(`agent_runtime.AgentRuntimeProxy` 와 같은 꼴).
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping
from datetime import datetime
from typing import TYPE_CHECKING, Any

from daegam.config import Settings
from daegam.errors import ServiceError
from daegam.k8s.client import KubeClient
from daegam.k8s.templates.common import deployment_counts
from daegam.services import agent_features, agent_graph
from daegam.services.agent_citations import masks_sources
from daegam.services.agent_runtime import AgentRuntimeProxy
from daegam.services.agent_skills import AgentSkillStore
from daegam.services.exposure import status_base
from daegam.services.instance_status import (
    changed_within,
    condition_rows,
    unready_running_pod,
    with_unready_reason,
)
from daegam.services.tool_effects import (
    UNKNOWN_SOURCE,
    AgentGate,
    SourceTools,
    node_names,
    tools_nodes,
)
from daegam.services.tool_rules import coded_warning
from daegam.store.agents import Agent, AgentSpec, MongoAgentStore

if TYPE_CHECKING:
    from daegam.services.agent_sleep import AgentSleepControl

CHAT_PATH = "/api/agents/{id}/chat"
# 모든 파드가 최신 스펙으로 준비돼 채팅을 받는 Deployment phase 다(`map_deployment_status`)
READY_PHASE = "Ready"
# 쓰지 않아 파드를 0 으로 줄인 Agent 와 그 파드를 다시 띄우는 중의 phase (P44 10절). 둘 다 런타임을
# 읽지 않는다
SLEEPING_PHASE = "Sleeping"
WAKING_PHASE = "Waking"
# 준비되지 않은 파드의 이유(T21)를 상태 message 에 싣는 phase 다. Ready 는 볼 것이 없고 Terminating·
# Missing 은 파드를 기다리는 것이 아니다
UNREADY_PHASES: frozenset[str] = frozenset({"Pending", "Rolling", "Failed"})

# 런타임(/metrics)이 코드만 보내는 경고. 플랫폼이 만드는 경고(pending_sessions_revision_changed
# 등)와 같은 `code: 설명` 꼴로 맞춰 화면과 API 소비자가 왜 뜨는지 바로 읽게 한다. 모르는 코드는
# 그대로 둔다
RUNTIME_WARNINGS: dict[str, str] = {
    "no_shared_state": (
        "the graph has no memory node, so session state (checkpoints, session index, "
        "rate-limit counters) lives only inside the pod: sessions are lost on restart and are "
        "not shared across replicas; add a memory node bound to a Database to persist them"
    ),
}


# 턴 위임 토큰(P53 7절)의 Agent 경고. 위임이 켜진 토큰 인증 모드에서 토큰을 받을 수 있는
# 소스(MCP Server, local 모드면 Gateway 도)를 쓰는데, 이미지가 기능을 알린 기록에 `turn_tokens` 가
# 없다. 기록이 없으면(첫 배포, 이미지를 바꾼 직후) 아직 모르는 것이라 내지 않는다. 상태 API 의
# `agents_without_turn_tokens` 는 기록이 없는 것도 센다
TURN_TOKENS_UNSUPPORTED = "turn_tokens_unsupported"

TURN_TOKENS_UNSUPPORTED_WARNING = (
    f"{TURN_TOKENS_UNSUPPORTED}: the runtime image does not report turn_tokens, so this Agent "
    "calls every tool under its own name until it is redeployed on a current image"
)


# 뒤 판이 더한 종류의 소스를 가리키는 tools 노드를 컴파일이 뺐다 (P46 A0,
# `agent_graph.without_newer_sources`). 끝의 괄호는 `노드 id: 종류` 들이다
TOOLS_SOURCE_NEWER = "tools_source_newer"
TOOLS_SOURCE_NEWER_TEXT = (
    "tools nodes whose source kind this platform version does not know are left out of "
    "agent.json, so the pod starts without their Tools"
)


def newer_source_warning(rev: AgentSpec | None) -> str | None:
    """배포된 리비전에서 컴파일이 뺀 노드의 상태 경고다. 뺀 것이 없으면 None 이다."""
    newer = agent_graph.newer_sources(rev) if rev is not None else {}
    if not newer:
        return None
    nodes = ", ".join(f"{node_id}: {kind}" for node_id, kind in newer.items())
    return coded_warning(TOOLS_SOURCE_NEWER, TOOLS_SOURCE_NEWER_TEXT, nodes)


def lacks_turn_tokens(doc: Agent, settings: Settings) -> bool:
    """이 Agent 가 `turn_tokens_unsupported` 를 내는가 (P53 6·7절). 그런 Agent 는 토큰을 받을 수
    있는 소스도 자기 이름으로 부른다. 순수 함수다."""
    if not (settings.turn_tokens_enabled and settings.token_auth):
        return False
    rev = doc.deployed
    if rev is None or doc.runtime_for_image is None:
        return False
    if doc.image_reports(agent_features.TURN_TOKENS):
        return False
    return bool(
        agent_graph.turn_token_sources(rev, trusts_platform=settings.data_plane_trusts_platform)
    )


# 거절된 Tool 호출(P52 5절)의 Agent 경고. 이미지가 기능을 알린 기록에 `tool_refusals` 가 없는데,
# 누가 부를지의 규칙이나 인자 조건이 있는 Tool 을 쓴다. 그런 이미지는 규칙이 거절한 호출에서 턴이
# 실패한다. 기록이 없으면 아직 모르는 것이라 내지 않는다
RUNTIME_TOOL_REFUSALS = "runtime_tool_refusals"

RUNTIME_TOOL_REFUSALS_WARNING = (
    f"{RUNTIME_TOOL_REFUSALS}: the runtime image does not report tool_refusals, so a chat fails "
    "when a Tool with rules refuses a call; redeploy this Agent on a current image"
)


def refusals_unknown(doc: Agent) -> bool:
    """이 Agent 의 이미지가 거절된 Tool 호출을 견디지 못한다고 아는가 (P52 5절). 기록이 없으면
    모르는 것이다. 순수 함수다."""
    return doc.runtime_for_image is not None and not doc.image_reports(agent_features.TOOL_REFUSALS)


def uses_ruled_tools(rev: AgentSpec, sources: Mapping[str, SourceTools]) -> bool:
    """리비전의 tools 노드가 누가 부를지의 규칙이나 인자 조건이 있는 Tool 을 쓰는가. 고른 Tool 이
    없거나 이름을 모르는 노드는 소스에 그런 Tool 이 있으면 쓰는 것이다. 순수 함수다."""
    for _node_id, data in tools_nodes(rev):
        tools = sources.get(data.source.id, UNKNOWN_SOURCE)
        names = node_names(data, tools)
        if (names and tools.ruled & set(names)) or (not names and tools.ruled):
            return True
    return False


# Agent 가 고른 Tool 을 누가 부를지의 규칙이 숨긴다(P52 2·3·7절). 원인마다 한 줄이다. 서버나
# Gateway 의 규칙이 이 Agent 를 이름 대지 않았다, 허브 항목의 규칙이다(Agent 를 받지 않는다), 아직
# IdP 토큰만 받는 oidc 모드의 Gateway 다(Agent 를 규칙에 넣을 수 없다, P53b 6.9절), 규칙이 이름 댄
# Agent 의 토큰을 새로 내는 중이다. header 모드는 규칙을 집행하지 않으므로 내지 않는다
AGENT_TOOL_HIDDEN = "agent_tool_hidden"
HIDDEN_NOT_NAMED = "not_named"
HIDDEN_ITEM = "item"
HIDDEN_OIDC = "oidc"
HIDDEN_REISSUE = "reissue"
HIDDEN_CAUSES: tuple[str, ...] = (HIDDEN_NOT_NAMED, HIDDEN_ITEM, HIDDEN_OIDC, HIDDEN_REISSUE)


def gate_cause(gate: AgentGate, agent_id: str, *, trusts_idp: bool) -> str | None:
    """규칙 하나가 이 Agent 에게 그 Tool 을 숨기는 원인이다. 열면 None 이다."""
    if gate.item:
        return HIDDEN_ITEM
    if gate.gateway and trusts_idp:
        return HIDDEN_OIDC
    return None if agent_id in gate.agents else HIDDEN_NOT_NAMED


def hidden_tools(
    agent_id: str, rev: AgentSpec, sources: Mapping[str, SourceTools], *, trusts_idp: bool
) -> tuple[dict[str, list[str]], list[str]]:
    """리비전의 tools 노드가 쓰는 Tool 가운데 규칙이 이 Agent 에게 숨기는 것을 원인마다, 그리고
    규칙이 있고 모든 규칙이 이 Agent 를 받는 Tool 을 낸다. 뒤의 것은 토큰을 새로 내는 동안 숨는다.
    고른 Tool 이 없고 이름을 모르는 노드는 규칙이 있는 이름 모두를 쓰는 것이다. 순수 함수다."""
    hidden: dict[str, list[str]] = {}
    admitted: list[str] = []
    for _node_id, data in tools_nodes(rev):
        tools = sources.get(data.source.id, UNKNOWN_SOURCE)
        names = node_names(data, tools) or list(tools.gates)
        for name in names:
            gates = tools.gates.get(name, ())
            found = (gate_cause(g, agent_id, trusts_idp=trusts_idp) for g in gates)
            causes = [c for c in found if c is not None]
            for cause in dict.fromkeys(causes):
                hidden.setdefault(cause, []).append(name)
            if gates and not causes:
                admitted.append(name)
    return hidden, admitted


def hidden_tool_warnings(hidden: Mapping[str, list[str]]) -> list[str]:
    """원인마다 `agent_tool_hidden` 한 줄이다. 화면이 원인으로 문장을 고르고 Tool 이름을 그 아래에
    보인다(`ui/src/lib/warningText.ts`)."""
    return [
        f"{AGENT_TOOL_HIDDEN}: a Tool rule hides Tools this Agent selected "
        f"(cause: {cause}; tools: {', '.join(dict.fromkeys(hidden[cause]))})"
        for cause in HIDDEN_CAUSES
        if hidden.get(cause)
    ]


def session_counts(metrics: Mapping[str, Any]) -> tuple[int, int]:
    """런타임 `/metrics` 의 (활성 세션, 보류 승인). 칸이 없으면 0 이다. 상태 조회와 잠들기가 남길
    수(P44 7절)가 함께 읽는다."""
    sessions = metrics.get("sessions") if isinstance(metrics.get("sessions"), dict) else {}
    return int(sessions.get("active") or 0), int(sessions.get("pending") or 0)


def describe_runtime_warning(warning: str) -> str:
    """코드만 온 런타임 경고를 `code: 설명` 으로 편다. 이미 설명이 붙었거나 모르는 코드면 그대로."""
    code = warning.split(":", 1)[0].strip()
    if ":" in warning or code not in RUNTIME_WARNINGS:
        return warning
    return f"{code}: {RUNTIME_WARNINGS[code]}"


class AgentStatusView:
    """Agent 의 상태 훅 셋(fields·map·finish)과 재배포 경고가 쓰는 보류 수."""

    def __init__(
        self,
        *,
        settings: Settings,
        skills: AgentSkillStore,
        agents: MongoAgentStore,
        proxy: AgentRuntimeProxy,
        client_for_doc: Callable[[Agent], Awaitable[KubeClient]],
        with_pod_waiting: Callable[..., Awaitable[tuple[dict[str, Any], list[dict[str, Any]]]]],
        pin_warnings: Callable[[Agent], Awaitable[list[str]]],
        error_cls: type[ServiceError],
        sandbox_warnings: Callable[[Agent], Awaitable[list[str]]] | None = None,
        sleep: AgentSleepControl | None = None,
    ):
        self.settings = settings
        self.skills = skills
        self.agents = agents
        self.proxy = proxy
        self.client_for_doc = client_for_doc
        self.with_pod_waiting = with_pod_waiting
        # 배포 때의 Tool 핀과 소스가 갈린 경고 (P39b 14절 5부, `tools_behind`·`tools_missing`)
        self.pin_warnings = pin_warnings
        # 코드 실행의 게이트와 개발용 우회 경고 (P40 8절, `SandboxService.gate_warnings`)
        self.sandbox_warnings = sandbox_warnings
        self.error_cls = error_cls
        # 쉬는 상태 (P44 10절). 잠든 Agent 의 phase 와 남긴 수, waking 의 끝을 본다
        self.sleep = sleep

    async def fields(self, doc: Agent, observed_at: datetime) -> dict[str, Any]:
        """status 응답의 모든 키. 어느 경로로 빠져나가도 이 모양이다. 잠든 Agent 의 세션·보류 수는
        잠들 때 남긴 값이다 (P44 10절). 쉬는 동안 바꾸는 것이 없으므로 목록과 개요가 보류 승인을
        계속 보인다."""
        saved = doc.sleep if doc.sleep.state == "asleep" else None
        # 받는 첨부는 깨우는 동안에도 잠들 때의 값이다. 화면이 첨부를 쥔 채 다시 보낸다 (P44 8절)
        resting = doc.sleep.state in ("asleep", "waking")
        return {
            # Agent 는 노출이 oidc 전용이라 미인증 경고가 나올 자리가 없다.
            # error 는 문서의 것을 먼저 담는다
            **status_base(doc, observed_at, warnings=[], error=doc.last_error),
            "active_sessions": saved.active_sessions if saved is not None else None,
            "pending_approvals": saved.pending_approvals if saved is not None else None,
            "chat_url": CHAT_PATH.format(id=doc.id),
            "ready_replicas": None,
            "greeting": self._greeting(doc),
            **self._deployed_chat_facts(doc),
            # 런타임 이미지가 init 받기 때 알린 기록 (D65). 같은 이미지의 것만 보인다
            "runtime": None,
            # 런타임 /metrics 의 `chat` 이다(D61). 받는 첨부의 형식과 상한이고 옛 런타임은
            # null 이다. 잠들었거나 깨우는 중이면 잠들 때 남긴 값이다
            "chat": doc.sleep.chat if resting else None,
            # 이 파드에 내려간 기록 보존 기간이다 (D46). 화면의 안내문이 세션 TTL 과 함께 읽어
            # "닫히면 이어 쓸 수 없고 기록은 며칠 남는다" 를 사실대로 적는다
            "transcript_retention_days": self.settings.transcript_retention_days,
            # 쉬는 상태와 잠들 수 없는 까닭 (P44 10절, 14절)
            "sleep": self.sleep.view(doc) if self.sleep is not None else None,
        }

    @staticmethod
    def _greeting(doc: Agent) -> str | None:
        """핀된 리비전 입구의 인사말이다(D65). 저장만 하고 재배포하지 않은 정의가 아니라 파드가 도는
        리비전의 값이라, 화면이 첫 말 전에 보이는 말풍선과 런타임이 새 세션의 첫 이벤트로 내는 글이
        같다. 비어 있으면 None 이다."""
        rev = doc.deployed
        entry = rev.graph.node("entry") if rev is not None else None
        return (entry.data.get("greeting") or None) if entry is not None else None

    @staticmethod
    def _deployed_chat_facts(doc: Agent) -> dict[str, bool]:
        """핀된 리비전의 채팅 사실이다(P36). 장기 기억을 쓰는가와 인용 원문을 가리는가다. 인사말처럼
        저장만 한 정의가 아니라 도는 리비전의 값이라 홈 챗의 기억 서랍 단추와 인용 창의 원문
        다운로드가 런타임·플랫폼 라우트와 같은 판정을 본다."""
        rev = doc.deployed
        return {
            "long_term_memory": rev is not None and agent_graph.long_term_memory(rev) is not None,
            "sources_masked": rev is not None and masks_sources(rev),
        }

    async def _runtime_stats(self, doc: Agent, client: KubeClient) -> dict[str, Any]:
        """런타임 /metrics 의 세션 수·보류 수·경고·받는 첨부(`chat`). 런타임에 연결할 수 없으면
        `error` 만 담아 돌려준다."""
        try:
            status, metrics, _headers = await self.proxy.request(doc, client, "GET", "/metrics")
        except self.error_cls as e:
            return {"error": f"{e.code}: {e.detail}"}
        if status != 200 or not isinstance(metrics, dict):
            return {"error": f"runtime_metrics: HTTP {status}"}
        active, pending = session_counts(metrics)
        return {
            "active_sessions": active,
            "pending_approvals": pending,
            "warnings": [describe_runtime_warning(str(w)) for w in metrics.get("warnings") or []],
            "chat": metrics["chat"] if isinstance(metrics.get("chat"), dict) else None,
        }

    def _revision_warning(self, doc: Agent, pending: int) -> str | None:
        """보류 세션이 있는데 정의가 그 뒤로 나아갔으면 재배포하라고 알린다."""
        if not pending or doc.deployed_revision is None or doc.revision == doc.deployed_revision:
            return None
        return (
            f"pending_sessions_revision_changed: {pending} session(s) await approval on revision "
            f"{doc.deployed_revision}; redeploying to revision {doc.revision} resumes them "
            "on the new graph"
        )

    async def finish(self, doc: Agent, out: dict[str, Any]) -> dict[str, Any]:
        """런타임 수치가 경고 목록을 통째로 바꾸므로(`map` 의 `out.update(stats)`) 플랫폼이 아는
        경고(Skill, Tool 핀, 코드 실행의 게이트, 턴 위임, 뒤 판의 소스)는 그 뒤에 더한다.
        본 phase 는 값이 바뀔 때만 문서에 적어 둔다. 상태를 안 붙이는 가벼운 목록(D35 F2)이 읽는
        "마지막으로 알려진" 값이 그것이다."""
        out["warnings"].extend(await self.skills.access_revoked(doc.tenant, doc.deployed))
        out["warnings"].extend(await self.pin_warnings(doc))
        if self.sandbox_warnings is not None:
            out["warnings"].extend(await self.sandbox_warnings(doc))
        if lacks_turn_tokens(doc, self.settings):
            out["warnings"].append(TURN_TOKENS_UNSUPPORTED_WARNING)
        # 뒤 판이 배포한 리비전의 모르는 소스는 컴파일이 뺀다 (P46 A0)
        if (newer := newer_source_warning(doc.deployed)) is not None:
            out["warnings"].append(newer)
        await self._remember_phase(doc, out["phase"])
        return out

    async def _remember_phase(self, doc: Agent, phase: str | None) -> None:
        if phase == doc.last_phase:
            return
        doc.last_phase = phase
        await self.agents.set_phase(doc.id, phase)

    async def map(
        self,
        doc: Agent,
        client: KubeClient,
        obj: dict[str, Any],
        out: dict[str, Any],
        observed_at: datetime,
        *,
        listing: bool,
    ) -> dict[str, Any]:
        """읽은 Deployment 를 phase 로 옮긴다. 파드가 준비됐으면 런타임 /metrics 로 세션·보류 수와
        경고를 읽는다.

        listing 은 목록 조회다. 직행이 아닌 경로(원격 클러스터, kubecontext 로컬 개발)에서는 런타임
        수치를 싣지 않는다. 런타임 호출이 pods/portforward 터널이라(D41) 목록 요청 하나가 배포된
        에이전트 수만큼 터널을 열게 되기 때문이다. 상세와 메트릭 탭은 싣는다. 돌고 있는데 준비되지
        않은 파드가 있으면 그 런타임이 아는 이유를 message 에 싣는 것도 상세만 한다(T21)."""
        out.update(map_deployment_status(obj, doc.last_spec_change, observed_at))
        out["runtime"], runtime_warnings = agent_features.status_runtime(doc)
        out["warnings"].extend(runtime_warnings)
        if await self._sleep_phase(doc, obj, out):
            return out
        # Deployment phase 는 이미지를 못 받는 파드도 Pending 으로 낸다 (D43 둘째). 의존을 기다리는
        # 파드의 이유는 런타임이 갖는다(T21). 목록은 파드마다 런타임을 부르지 않으므로 준비되지 않은
        # phase 의 파드는 상세만 본다
        read = out["phase"] == "Pending" or (not listing and out["phase"] in UNREADY_PHASES)
        out, pods = await self.with_pod_waiting(doc, client, out, when=read)
        if read and not listing:
            out = with_unready_reason(out, await self._unready_reason(doc, client, pods))
        if not out["ready_replicas"] or (listing and not self.proxy.is_direct(doc, client)):
            return out
        stats = await self._runtime_stats(doc, client)
        out.update(stats)
        if "error" in stats:
            return out
        warning = self._revision_warning(doc, out["pending_approvals"])
        if warning:
            out["warnings"].append(warning)
        return out

    async def _sleep_phase(self, doc: Agent, obj: dict[str, Any], out: dict[str, Any]) -> bool:
        """쉬는 상태의 phase 를 얹는다 (P44 10절). 문서가 asleep 이고 Deployment 가 0 을 원하면
        Sleeping, waking 인데 준비된 파드가 없으면 Waking 이고 둘 다 런타임을 읽지 않는다(참).
        waking 에 준비된 파드가 있으면 게이트처럼 awake 로 옮기고 평소처럼 읽는다. Deployment 가
        문서와 다르면 워커가 맞출 때까지 Deployment 의 phase 를 보인다."""
        if self.sleep is None:
            return False
        state = doc.sleep.state
        wanted, ready = deployment_counts(obj)
        if state == "asleep" and wanted == 0:
            out.update(phase=SLEEPING_PHASE, message=None)
            return True
        if state != "waking":
            return False
        if ready and await self.sleep.settle_waking(doc, obj):
            out["sleep"] = self.sleep.view(doc)
            return False
        out["phase"] = WAKING_PHASE
        return True

    async def _unready_reason(
        self, doc: Agent, client: KubeClient, pods: list[dict[str, Any]]
    ) -> str | None:
        """돌고 있는데 준비되지 않은 최신 파드가 있으면 그 런타임이 아는 이유다(T21). 없거나 읽지
        못하면 None 이다."""
        pod = unready_running_pod(pods)
        return None if pod is None else await self.proxy.unready_reason(doc, client, pod)

    async def pending_count(self, doc: Agent) -> int | None:
        """런타임 색인의 보류 세션 수. 런타임에 연결할 수 없으면 None. /metrics 는 상태 조회와 같게
        읽는다. 잠든 Agent 는 잠들 때 남긴 수다 (P44 10절)."""
        if doc.sleep.state == "asleep":
            return doc.sleep.pending_approvals
        try:
            client = await self.client_for_doc(doc)
        except self.error_cls:
            return None
        stats = await self._runtime_stats(doc, client)
        return None if "error" in stats else stats["pending_approvals"]


def map_deployment_status(
    dep: dict[str, Any], last_spec_change: datetime | None, observed_at: datetime
) -> dict[str, Any]:
    """apps/v1 Deployment status → 플랫폼 phase. 순수 함수.
    Ready: 롤아웃이 끝났다. 원하는 수만큼 최신 파드가 준비됐고 옛 ReplicaSet 의 파드는 모두 종료에
    들어갔다. `status.replicas` 는 모든 ReplicaSet 의 종료 중이 아닌 파드 수라 원하는 수를 넘으면 옛
    파드가 아직 돈다. 그 파드는 내려갈 때까지 옛 설정으로 답한다. 종료에 들어간 파드도 preStop
    (`PRESTOP_SECONDS`) 동안은 이미 맺은 연결로 온 요청에 답하므로, Ready 뒤 몇 초는 옛 설정이 답할
    수 있다. Rolling: 세대가 아직 관찰되지 않았거나 일부만 준비됐거나 옛 파드가 남았다(스펙 변경
    직후의 Failed 도 grace 동안 Rolling). Failed: ReplicaFailure 또는 진행 기한 초과
    (Progressing=False). Pending: 그 밖(파드가 아직 없다).

    conditions 는 Deployment 가 낸 것을 그대로 옮긴다. 화면이 MCP Server 와 같은 표로 보인다."""
    meta = dep.get("metadata") or {}
    spec = dep.get("spec") or {}
    st = dep.get("status") or {}
    wanted = int(spec.get("replicas") if spec.get("replicas") is not None else 1)
    ready = int(st.get("readyReplicas") or 0)
    updated = int(st.get("updatedReplicas") or 0)
    running = int(st.get("replicas") or 0)
    generation = meta.get("generation") or 0
    observed = st.get("observedGeneration")
    conditions = {c.get("type"): c for c in st.get("conditions") or [] if c.get("type")}
    message: str | None = None
    recently_changed = changed_within(last_spec_change, observed_at)
    # 원하는 수만큼 최신 파드가 있고 준비된 파드도 그만큼이다. 준비된 파드에는 옛 파드도 들어 있다
    caught_up = ready >= wanted and updated >= wanted

    def _msg(cond: dict[str, Any] | None) -> str | None:
        if not cond:
            return None
        return cond.get("message") or cond.get("reason")

    if observed is not None and generation > observed:
        phase = "Rolling"
        message = f"spec generation {generation} not yet observed"
    elif (conditions.get("ReplicaFailure") or {}).get("status") == "True":
        phase = "Failed"
        message = _msg(conditions.get("ReplicaFailure"))
    elif caught_up and running <= wanted:
        phase = READY_PHASE
    # 진행 기한 초과는 옛 파드가 남은 것보다 먼저 본다. 새 파드가 이미지를 못 받거나 계속 죽으면
    # 준비된 옛 파드가 남은 채 멈추는데, 그것을 교체 중으로 두면 실패 까닭이 끝내 가려진다
    elif (conditions.get("Progressing") or {}).get("status") == "False":
        phase = "Rolling" if recently_changed else "Failed"
        message = _msg(conditions.get("Progressing"))
    elif caught_up:
        phase = "Rolling"
        message = f"{running - wanted} old pod(s) still running"
    elif ready > 0:
        phase = "Rolling"
        message = f"{ready}/{wanted} ready"
    else:
        phase = "Pending"
        message = _msg(conditions.get("Available")) if conditions.get("Available") else None
    return {
        "phase": phase,
        "message": message,
        "ready_replicas": ready,
        "conditions": condition_rows(st),
    }
