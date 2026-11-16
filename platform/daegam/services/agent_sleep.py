"""쓰지 않는 Agent 를 0 으로 줄이고 다음 턴에 깨운다 (P44, D103).

쉬는 상태는 Agent 문서의 `sleep` 이고 자기 조건부 갱신만 쓴다(`MongoAgentStore.move_sleep`). 상태는
awake, draining, asleep, waking 넷이다.

- **잠들 수 있는가** (6절, `ineligible`). memory 노드가 있고, 노출하지 않고, 런타임이
  `scale_to_zero` 를 알렸고, overrides 가 `spec.replicas` 를 정하지 않고, Agent 와 플랫폼의 스위치가
  켜졌을 때다. 아니면 그 첫 까닭의 코드다.
- **활동** (7절, `mark`). 턴, 결정, 다시 생성, 실행 시작, 깨우기가 런타임을 부르기 전에 앞으로만
  가는 쓰기로 적는다. 레플리카마다 그 Agent 에 마지막으로 쓴 지 창의 4분의 1(많아야 1분)이
  지나지 않았으면 건너뛴다. 읽기는 적지 않는다.
- **잠들기** (7절, `put_to_sleep`). awake 를 draining 으로 옮기고, 런타임의 `POST /drain` 이 공유
  색인에 표시를 두며 살아 있는 턴 리스를 센다. 리스가 있거나 Running 파드가 답하지 않으면 표시를
  지우고 awake 로 돌린다. 없으면 `/metrics` 의 세션 수와 함께 asleep 으로 옮기고 0 으로 줄인다.
- **깨우기** (8절, `admit`). 턴 라우트, 실행 시작, `POST /wake` 가 위임을 열기 전에 부르는 게이트다.
  draining 은 awake 로 쓰고 지나간다. asleep 은 waking 으로 옮기는 조건부 갱신에서 이긴 쪽이 활동을
  적고 레플리카를 되돌린다. waking 은 Deployment 의 준비된 파드 수(레플리카마다 2초 기억)를 보고,
  있으면 표시를 지우고 awake 로 옮겨 지나가며 없으면 503 `agent_waking` 과 `Retry-After: 5` 다.
- **레플리카** (9절, `apply_replicas`). 문서의 클레임 안에서 쉬는 상태를 다시 읽고 그 상태가 정하는
  수로 `spec.replicas` 하나만 패치한다. asleep 이면 적용됐다는 기록과 그 세대를 남긴다.
- **워커** (7절, 11절, `AgentSleepWorker`). 30초마다 잠들 Agent, 기한이 지난 draining, 남은 0 으로
  줄이기, 다시 뜬 파드, 끝나지 않는 깨우기, 매일 깨우기와 그 뒤의 잠들기를 다룬다.
"""

from __future__ import annotations

import hashlib
import logging
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import Enum
from typing import Any, Literal, get_args

from starlette.concurrency import run_in_threadpool

from daegam.config import Settings
from daegam.config.resolve import EffectiveConfig
from daegam.errors import ServiceError
from daegam.k8s.labels import LABEL_ID
from daegam.k8s.templates.agent import rendered_replicas
from daegam.k8s.templates.common import current_template_ready, deployment_counts
from daegam.services import agent_graph
from daegam.services.agent_features import SCALE_TO_ZERO
from daegam.services.agent_runtime import (
    AGENT_WAKING,
    SLEEP_SUB,
    AgentRuntimeProxy,
    waking_error,
)
from daegam.services.agent_status import session_counts
from daegam.services.claims import checkpoint
from daegam.services.instance_base import EDIT_STATES
from daegam.services.lease_worker import QueuedLeaseWorker
from daegam.store.agent_runs import MOVING_STATUSES, MongoAgentRunStore
from daegam.store.agents import (
    HOUSEKEEPING,
    Agent,
    AgentSleep,
    MongoAgentStore,
    SleepState,
)
from daegam.store.base import iso_utc, new_id, to_millis, utcnow
from daegam.store.leases import Lease
from daegam.ttl_cache import TTLCache

logger = logging.getLogger("daegam.agent_sleep")

# 잠들기의 오류 코드 (14절, `daegam/error_codes.py`). 읽기와 깨우는 중의 코드는 프록시
# (`services/agent_runtime.py`)에 있다
AGENT_BUSY = "agent_busy"
AGENT_SLEEP_INELIGIBLE = "agent_sleep_ineligible"

# 잠들 수 없는 까닭 (6절). 표의 차례이고 첫 것을 낸다. 상태 API 의 칸이 이 Literal 을 쓴다
SleepIneligible = Literal[
    "no_memory", "exposed", "runtime_image", "replicas_override", "agent_off", "setting_off"
]
NO_MEMORY, EXPOSED, RUNTIME_IMAGE, REPLICAS_OVERRIDE, AGENT_OFF, SETTING_OFF = get_args(
    SleepIneligible
)

# 상태를 옮긴 까닭. 잠들기는 창(idle), `POST /sleep`(request), 매일 깨우기의 끝(housekeeping)이다.
# 깨우기는 턴(turn), 실행 시작(run), `POST /wake`(wake), 배포(deploy), 잠들 수 없게 됨(ineligible),
# 매일 깨우기(housekeeping)다. waking 에서 awake 로 옮길 때는 깨운 까닭을 그대로 둔다
IDLE = "idle"
REQUEST = "request"
TURN = "turn"
RUN = "run"
WAKE = "wake"
DEPLOY = "deploy"
INELIGIBLE = "ineligible"
# waking 을 거쳐 awake 로 온 까닭. waking 에서 awake 로 옮길 때 깨운 까닭을 그대로 둔다
WOKEN: frozenset[str] = frozenset({TURN, RUN, WAKE, HOUSEKEEPING})
# 잠들기를 그만둔 까닭과 워커가 awake 로 돌린 까닭이다. active 는 매일 깨우기의 hold 안에 활동이
# 있어 창에 넘긴 것이다
ACTIVE = "active"
BUSY = "busy"
NO_ANSWER = "no_answer"
STALE_DRAIN = "stale_drain"
STRAY_PODS = "stray_pods"
WAKE_TIMEOUT = "wake_timeout"

# 게이트가 Deployment 를 다시 읽지 않는 시간(초). 레플리카마다다
READY_CACHE_SECONDS = 2.0
# 드레인 표시의 수명. 표시는 이만큼 지나면 풀린다(Postgres 는 행을 두고 만료 시각으로 거른다).
# 그래서 이보다 오래된 draining 은 멈춘 워커가 남긴 것이다
DRAIN_MARK = timedelta(minutes=2)
# 이만큼 준비된 파드가 없는 깨우기는 awake 로 끝낸다
WAKE_LIMIT = timedelta(minutes=5)
# 깨우기가 awake 로 끝난 뒤 Service 의 엔드포인트를 기다리는 시간. 그 사이의 연결 거부는
# 깨우는 중이다
JUST_WOKEN = timedelta(seconds=10)
# 활동 쓰기를 건너뛰는 시간의 상한(초)
ACTIVITY_THROTTLE_MAX = 60.0
# asleep 을 떠날 때 지우는 0 으로 줄이기의 기록
UNSCALED: dict[str, Any] = {"scaled": None, "generation": None}


def housekeeping_hold(settings: Settings) -> timedelta:
    """매일 깨우기가 Agent 를 깨워 두는 시간(hold, 11절). 첫 정리가 이 안에 끝난다 (F0.3).
    `DAEGAM_AGENT_HOUSEKEEPING_HOLD_SECONDS` 이고 기본 10분이다."""
    return timedelta(seconds=settings.agent_housekeeping_hold_seconds)


def housekeeping_span(settings: Settings) -> timedelta:
    """매일 깨우기 하나가 클러스터의 몫을 쥘 수 있는 가장 긴 시간. 깨우기의 한도, hold, 드레인
    표시의 수명을 더한 것이고, 이보다 오래 그 까닭으로 남은 문서는 몫을 쥐지 않는다."""
    return WAKE_LIMIT + housekeeping_hold(settings) + DRAIN_MARK


def activity_throttle(window: int) -> float:
    """활동 쓰기를 건너뛰는 시간(초, 7절). 창의 4분의 1 이고 많아야 1분이다. 창이 0 이면 1분이다."""
    return min(window / 4, ACTIVITY_THROTTLE_MAX) if window else ACTIVITY_THROTTLE_MAX


def _spec_overrides(doc: Agent) -> dict[str, Any]:
    """저장된 overrides 의 `spec` 이다. 없으면 빈 dict 다."""
    spec = doc.overrides.get("spec")
    return spec if isinstance(spec, dict) else {}


def sets_replicas(doc: Agent) -> bool:
    """저장된 overrides 가 `spec.replicas` 를 정하는가 (6절)."""
    return "replicas" in _spec_overrides(doc)


def ineligible_all(doc: Agent, settings: Settings) -> list[SleepIneligible]:
    """잠들 수 없는 까닭의 코드를 표의 차례로 모두 준다 (6절). 잠들 수 있으면 빈 목록이다. 배포
    대화상자는 자기 입력으로 앞의 까닭(memory 노드, 노출, 이미지)을 다시 가르므로 뒤의 까닭도 알아야
    한다."""
    rev = doc.deployed
    failed = [
        (NO_MEMORY, rev is None or not agent_graph.analyze_structure(rev).main_has_memory),
        (EXPOSED, doc.exposure.enabled),
        (RUNTIME_IMAGE, not doc.image_reports(SCALE_TO_ZERO)),
        (REPLICAS_OVERRIDE, sets_replicas(doc)),
        (AGENT_OFF, not doc.scale_to_zero),
        (SETTING_OFF, not settings.agent_scale_to_zero),
    ]
    return [code for code, fails in failed if fails]


def ineligible(doc: Agent, settings: Settings) -> SleepIneligible | None:
    """잠들 수 없는 까닭의 코드이고 잠들 수 있으면 None 이다 (6절). 표의 차례로 첫 것이다."""
    codes = ineligible_all(doc, settings)
    return codes[0] if codes else None


def final_replicas(doc: Agent, state: SleepState) -> int:
    """쉬는 상태 state 에서 렌더에 overrides 를 얹은 최종본이 주는 레플리카 수. overrides 가
    `spec.replicas` 를 정하면 그 값이고 아니면 렌더의 수다(`rendered_replicas`)."""
    override = _spec_overrides(doc).get("replicas")
    if isinstance(override, int):
        return override
    return rendered_replicas(state, EffectiveConfig.model_validate(doc.effective_config))


def housekeeping_offset(agent_id: str, period: timedelta) -> timedelta:
    """매일 깨우기를 Agent 마다 당기는 몫 (11절). id 에서 나오고 주기의 절반 안이다. 함께 잠든
    Agent 가 함께 깨지 않는다."""
    digest = int(hashlib.sha256(agent_id.encode()).hexdigest()[:8], 16)
    return period * (digest / 0x1_0000_0000) / 2


def housekeeping_at(doc: Agent, period: timedelta) -> datetime | None:
    """잠든 Agent 의 매일 깨우기 때 (11절). 잠든 때에서 주기를 지나되 id 의 몫만큼 당긴다. 그래서
    한 주기보다 오래 자지 않는다. 잠들지 않았으면 None 이다."""
    since = doc.sleep.since
    if doc.sleep.state != "asleep" or since is None:
        return None
    return since + period - housekeeping_offset(doc.id, period)


class Slept(Enum):
    """`put_to_sleep` 의 끝."""

    # 잠들었고 0 으로 줄이기를 적용했다
    ASLEEP = "asleep"
    # 잠들었지만 남이 클레임을 쥐어 0 으로 줄이기가 남았다. 워커의 다음 틱이 한다
    PENDING = "pending"
    # 턴이 리스를 쥐고 있었다
    BUSY = "busy"
    # Running 파드가 답하지 않았다
    NO_ANSWER = "no_answer"
    # 드레인 사이에 턴이 와서 awake 를 썼다
    TURN = "turn"
    # 처음부터 awake 가 아니었거나 그 사이 활동이 바뀌었다
    CHANGED = "changed"


ClaimedWork = Callable[[Agent, Callable[[], Awaitable[bool]]], Awaitable[bool]]


@dataclass
class _Read:
    """한 번 읽은 Deployment."""

    dep: dict[str, Any] | None


class AgentSleepControl:
    """잠들기와 깨우기의 상태 이동과 그 둘레의 클러스터·런타임 호출. AgentService 가 생성자에서
    만들어 프록시·상태 조회·배포 경로가 함께 쓴다."""

    def __init__(
        self,
        *,
        agents: MongoAgentStore,
        settings: Settings,
        proxy: AgentRuntimeProxy,
        claimed: ClaimedWork,
        error_cls: type[ServiceError],
        runs: MongoAgentRunStore | None = None,
    ):
        self.agents = agents
        self.settings = settings
        # 런타임의 드레인 표시와 `/metrics` 를 부르고 문서의 클러스터 클라이언트를 준다
        self.proxy = proxy
        # 문서의 클레임 안에서 일을 돌린다. 남이 쥐고 있으면 건너뛰고 False 다
        self.claimed = claimed
        self.error = error_cls
        # 잠들기 앞에 열린 실행을 센다. 백그라운드 실행을 끈 배포본은 None 이다
        self.runs = runs
        self._marked: TTLCache[str, bool] = TTLCache(
            activity_throttle(settings.agent_sleep_after_seconds), max_entries=10_000
        )
        self._deployments: TTLCache[str, _Read] = TTLCache(READY_CACHE_SECONDS, max_entries=10_000)

    # -- 판정과 상태 보기 ----------------------------------------------------------------

    def ineligible(self, doc: Agent) -> str | None:
        return ineligible(doc, self.settings)

    def view(self, doc: Agent) -> dict[str, Any]:
        """상태 응답의 `sleep` (10절, 14절). 창은 화면의 안내문이 읽는다."""
        sleep = doc.sleep
        codes = ineligible_all(doc, self.settings)
        return {
            "state": sleep.state,
            "since": iso_utc(sleep.since) if sleep.since is not None else None,
            "reason": sleep.reason,
            "ineligible": codes[0] if codes else None,
            "ineligible_all": codes,
            "window_seconds": self.settings.agent_sleep_after_seconds,
        }

    @staticmethod
    def just_woken(doc: Agent) -> bool:
        """깨우기가 끝나 awake 로 옮긴 직후인가 (8절). 게이트와 상태 읽기는 Deployment 의 준비된
        파드로 awake 로 옮기지만 Service 의 엔드포인트는 kube-proxy 가 조금 늦게 쓴다. 그 사이의
        연결 거부는 프록시가 503 `agent_waking` 으로 답해 화면과 실행 시작이 Retry-After 뒤에 다시
        보낸다. draining 에서 awake 로 옮긴 턴도 까닭이 turn 이라 같이 들지만 파드가 이미 떠 있어
        거부가 드물고, 그래도 `JUST_WOKEN` 뒤에는 502 로 돌아온다."""
        sleep = doc.sleep
        if sleep.state != "awake" or sleep.reason not in WOKEN or sleep.since is None:
            return False
        return utcnow() - sleep.since < JUST_WOKEN

    @property
    def window(self) -> timedelta | None:
        """스스로 잠드는 창. 0 이거나 플랫폼 스위치가 꺼졌으면 None 이다."""
        seconds = self.settings.agent_sleep_after_seconds
        if not seconds or not self.settings.agent_scale_to_zero:
            return None
        return timedelta(seconds=seconds)

    # -- 활동 ---------------------------------------------------------------------------

    async def mark(self, doc: Agent) -> None:
        """활동을 적는다 (7절). awake 로 읽은 문서는 이 레플리카가 그 Agent 에 마지막으로 쓴 지
        `activity_throttle` 이 지나지 않았으면 건너뛴다."""
        if doc.sleep.state == "awake" and self._marked.get(doc.id):
            return
        await self.agents.mark_activity(doc.id, to_millis(utcnow()))
        self._marked.put(doc.id, True)

    # -- 깨우기 (8절) ----------------------------------------------------------------------

    async def admit(self, doc: Agent, reason: str) -> None:
        """턴 앞의 게이트. 지나가면 문서의 `sleep` 은 awake 이고 활동이 적혔다. 깨우는 중이면 503
        `agent_waking` 이다. 런타임을 부르지 않는 상태(draft·배포 중 등)는 보지 않는다. 그 답은 뒤의
        상태 가드가 낸다."""
        if doc.state not in EDIT_STATES:
            return
        for _ in range(3):
            if await self._admitted(doc, reason):
                return
            doc.sleep = await self.agents.sleep_of(doc.id) or AgentSleep()
        raise waking_error(self.error, doc)

    async def _admitted(self, doc: Agent, reason: str) -> bool:
        """지나가면 참이다. 다른 레플리카와의 조건부 갱신에 졌으면 거짓이라 다시 읽고 다시 본다.
        깨우는 중이면 503 이다."""
        state = doc.sleep.state
        if state == "awake":
            await self.mark(doc)
            return True
        now = to_millis(utcnow())
        if state == "draining":
            fields = {"state": "awake", "since": now, "reason": reason}
            if not await self.agents.move_sleep(doc.id, ("draining",), fields, touch=now):
                return False
            doc.sleep = doc.sleep.model_copy(update=fields)
            self._marked.put(doc.id, True)
            return True
        if state == "asleep":
            if not await self._wake(doc, reason, now):
                return False
            raise waking_error(self.error, doc)
        if await self.settle_waking(doc):
            await self.mark(doc)
            return True
        raise waking_error(self.error, doc)

    async def wake(self, doc: Agent) -> None:
        """`POST /wake` (14절). 게이트와 같고 깨우는 중이어도 오류가 아니다."""
        try:
            await self.admit(doc, WAKE)
        except self.error as e:
            if e.code != AGENT_WAKING:
                raise

    async def _wake(self, doc: Agent, reason: str, now: datetime) -> bool:
        """asleep 을 waking 으로 옮긴다. 이긴 쪽만 참이고, 활동을 적고 레플리카를 되돌린다."""
        fields = {"state": "waking", "since": now, "reason": reason, **UNSCALED}
        if not await self.agents.move_sleep(doc.id, ("asleep",), fields, touch=now):
            return False
        doc.sleep = doc.sleep.model_copy(update=fields)
        await self._scale_quietly(doc)
        return True

    async def settle_waking(self, doc: Agent, dep: Mapping[str, Any] | None = None) -> bool:
        """waking 문서의 Deployment 를 본다. 준비된 파드가 있으면 드레인 표시를 지우고 awake 로
        옮기며 참이다. 없는데 원하는 수가 0 이면(레플리카 패치가 늦게 닿았거나 남이 쥐어 건너뛰었다)
        레플리카를 다시 되돌린다. dep 을 주면 그것을 보고, 아니면 기억한 것을 읽는다."""
        read = dict(dep) if dep is not None else await self._deployment(doc)
        wanted, ready = deployment_counts(read)
        if read is not None and ready == 0 and wanted == 0:
            await self._scale_quietly(doc)
        if ready == 0:
            return False
        await self._undrain(doc)
        fields = {"state": "awake", "since": to_millis(utcnow())}
        if not await self.agents.move_sleep(doc.id, ("waking",), fields):
            fresh = await self.agents.sleep_of(doc.id)
            if fresh is None or fresh.state != "awake":
                return False
            doc.sleep = fresh
            return True
        doc.sleep = doc.sleep.model_copy(update=fields)
        return True

    async def wake_for_deploy(self, doc: Agent) -> None:
        """재배포가 먼저 깨운다 (12절). 새 리비전의 파드가 떠 상태를 보이게 awake 로 바로 옮기고,
        뒤의 provision 렌더가 레플리카를 되돌린다. 깨어 있던 문서도 까닭을 deploy 로 바꾸고 활동을
        적는다. 그래서 매일 깨우기의 몫을 놓고 창이 재배포에서 다시 시작한다. 남은 드레인 표시는 첫
        턴이 지운다."""
        now = to_millis(utcnow())
        fields = {"state": "awake", "since": now, "reason": DEPLOY, **UNSCALED}
        states: tuple[SleepState, ...] = ("awake", "draining", "asleep", "waking")
        await self.agents.move_sleep(doc.id, states, fields, touch=now)
        doc.sleep = await self.agents.sleep_of(doc.id) or AgentSleep()

    def awake_after_override(self, doc: Agent) -> frozenset[str]:
        """overrides 를 저장해 잠들 수 없게 된 asleep·draining 문서를 같은 저장에서 awake 로
        옮긴다 (6절). 문서의 `sleep` 을 고치고 저장이 함께 쓸 칸 이름을 준다. 옮길 것이 없으면 빈
        집합이다."""
        if doc.sleep.state not in ("asleep", "draining") or not sets_replicas(doc):
            return frozenset()
        update = {"state": "awake", "since": to_millis(utcnow()), "reason": INELIGIBLE, **UNSCALED}
        doc.sleep = doc.sleep.model_copy(update=update)
        return frozenset(f"sleep.{name}" for name in update)

    # -- 프록시의 훅 ------------------------------------------------------------------------

    async def met_draining(self, doc: Agent) -> bool:
        """턴이 드레인 표시에 거절됐다 (7절). 문서가 awake 면 표시를 지우고 한 번 다시 보내도 된다는
        참이다. 아니면 거짓이라 프록시가 깨우는 중으로 답하고 다음 시도의 게이트가 깨운다."""
        sleep = await self.agents.sleep_of(doc.id)
        if sleep is None or sleep.state != "awake":
            return False
        return await self._undrain(doc, sleep.mark)

    async def pod_running(self, doc: Agent) -> bool:
        """이 Agent 의 Running 파드가 있는가. 준비되지 않았어도 떼어 낸 턴을 쥐고 있을 수 있어 센다.
        읽지 못하면 있다고 본다. 배포 쪽이 비었으면 없다."""
        if doc.k8s is None:
            return False
        try:
            client = await self.proxy.client(doc)
            pods = await run_in_threadpool(
                client.list, "v1", "Pod", doc.k8s.namespace, {LABEL_ID: doc.id}
            )
        except Exception as e:  # noqa: BLE001 - 모르면 파드가 있다고 본다
            logger.info("agent %s: pods not read: %s", doc.id, e)
            return True
        return any((p.get("status") or {}).get("phase") == "Running" for p in pods)

    async def podless(self, doc: Agent) -> bool:
        """잠들었거나 깨우는 중이고 Running 파드가 없는가. 드레인 표시가 어떤 턴도 시작하지 못하게
        했고 아직 뜬 파드도 없으므로 중지와 해제의 턴 멈추기가 런타임을 부르지 않는다 (8절)."""
        return doc.sleep.state in ("asleep", "waking") and not await self.pod_running(doc)

    async def ready_now(self, doc: Agent) -> bool:
        """모든 파드가 지금 템플릿이고 하나는 준비됐는가 (`current_template_ready`)."""
        return current_template_ready(await self._deployment(doc))

    # -- 잠들기 (7절) ----------------------------------------------------------------------

    async def put_to_sleep(self, doc: Agent, reason: str, *, check_activity: bool) -> Slept:
        """잠들기의 걸음. check_activity 면 읽은 활동이 그대로일 때만 draining 으로 옮긴다. 이
        잠들기의 드레인 표시 id 를 같은 갱신에 적고 런타임에 그 id 로 표시를 세운다."""
        now = to_millis(utcnow())
        also = {"activity": doc.sleep.activity} if check_activity else None
        mark = new_id()
        draining = {"state": "draining", "since": now, "reason": reason, "mark": mark}
        if not await self.agents.move_sleep(doc.id, ("awake",), draining, also=also):
            return Slept.CHANGED
        doc.sleep = doc.sleep.model_copy(update=draining)
        try:
            leases: int | None = await self.proxy.drain(doc, user_sub=SLEEP_SUB, mark=mark)
        except Exception as e:  # noqa: BLE001 - 답이 없는 것은 파드로 가른다
            logger.info("agent %s: drain not answered: %s", doc.id, e)
            leases = None
        if leases:
            await self._give_up(doc, BUSY)
            return Slept.BUSY
        if leases is None and await self.pod_running(doc):
            await self._give_up(doc, NO_ANSWER)
            return Slept.NO_ANSWER
        active, pending, chat = await self._saved_metrics(doc)
        asleep = {
            "state": "asleep",
            "since": to_millis(utcnow()),
            "active_sessions": active,
            "pending_approvals": pending,
            # 읽지 못했으면 앞 잠들기의 받는 첨부를 둔다. 재배포는 깨우므로 쉬는 동안 바뀌지 않는다
            "chat": chat if chat is not None else doc.sleep.chat,
            "scaled": "pending",
            "generation": None,
        }
        if not await self.agents.move_sleep(doc.id, ("draining",), asleep, also={"since": now}):
            # 그 사이 턴의 게이트가 awake 를 썼다. 그 턴이 표시를 만나지 않게 지운다
            await self._undrain(doc)
            return Slept.TURN
        doc.sleep = doc.sleep.model_copy(update=asleep)
        return Slept.ASLEEP if await self.apply_replicas(doc) else Slept.PENDING

    async def _give_up(self, doc: Agent, reason: str) -> None:
        """잠들기를 그만둔다. 표시를 지우고 새 활동과 함께 awake 로 돌린다. 매일 깨우기의 끝에서
        턴이 리스를 쥐었으면 까닭을 그대로 두어 hold 가 다시 시작한다. 답이 없어 그만둔 것은 까닭을
        바꿔 클러스터의 몫을 놓는다. 준비되지 않은 파드가 남은 동안 매번 그만두며 몫을 쥐지 않게
        하려는 것이다."""
        await self._undrain(doc)
        now = to_millis(utcnow())
        held = doc.sleep.reason == HOUSEKEEPING and reason == BUSY
        kept = HOUSEKEEPING if held else reason
        fields = {"state": "awake", "since": now, "reason": kept}
        if await self.agents.move_sleep(
            doc.id, ("draining",), fields, also={"since": doc.sleep.since}, touch=now
        ):
            doc.sleep = doc.sleep.model_copy(update=fields)

    async def _saved_metrics(
        self, doc: Agent
    ) -> tuple[int | None, int | None, dict[str, Any] | None]:
        """잠들 때 남길 런타임 `/metrics` 의 (활성 세션, 보류 승인, 받는 첨부). 읽지 못하면 모두
        None 이다. draining 문서는 프록시의 쉬기 검사를 지난다."""
        try:
            status, metrics = await self.proxy.metrics(doc)
        except Exception as e:  # noqa: BLE001 - 수를 모르는 채로 잠든다
            logger.info("agent %s: metrics not read before sleeping: %s", doc.id, e)
            return None, None, None
        if status != 200 or not isinstance(metrics, dict):
            return None, None, None
        active, pending = session_counts(metrics)
        chat = metrics.get("chat")
        return active, pending, chat if isinstance(chat, dict) else None

    async def sleep_now(self, doc: Agent) -> None:
        """`POST /sleep` (7절). 창과 상관없이 잠들기의 걸음을 요청 안에서 돌린다. 잠들 수 없으면
        409 `agent_sleep_ineligible`, 턴이 리스를 쥐고 있거나 드레인 사이에 턴이 왔으면 409
        `agent_busy` 다. 0 으로 줄이기를 남이 쥔 클레임 때문에 하지 못했으면 409 in_progress 이고
        워커가 마친다."""
        self.proxy.require(doc, {"deployed"}, action="sleep", outside="not_deployed")
        code = self.ineligible(doc)
        if code is not None:
            raise self.error(
                409,
                AGENT_SLEEP_INELIGIBLE,
                f"agent {doc.name} cannot scale to zero: {code}",
                ineligible=code,
            )
        if doc.sleep.state == "asleep":
            if doc.sleep.scaled == "applied" or await self.apply_replicas(doc):
                return
            raise self._held(doc)
        if doc.sleep.state != "awake":
            raise self.error(
                409,
                "state_conflict",
                f"agent {doc.name} is {doc.sleep.state}; retry the sleep",
            )
        outcome = await self.put_to_sleep(doc, REQUEST, check_activity=False)
        if outcome in (Slept.BUSY, Slept.TURN):
            raise self.error(409, AGENT_BUSY, f"agent {doc.name} is answering a turn")
        if outcome is Slept.NO_ANSWER:
            raise self.error(
                502, "runtime_unreachable", f"agent {doc.name}: the runtime did not answer"
            )
        if outcome is Slept.CHANGED:
            raise self.error(409, "state_conflict", f"agent {doc.name} changed; retry the sleep")
        if outcome is Slept.PENDING:
            raise self._held(doc)

    def _held(self, doc: Agent) -> Exception:
        return self.error(
            409,
            "in_progress",
            f"agent {doc.name} is asleep but another action holds it; "
            "its pods stop on the next sleep worker tick",
        )

    # -- 레플리카 (9절) --------------------------------------------------------------------

    async def apply_replicas(self, doc: Agent) -> bool:
        """문서의 클레임 안에서 쉬는 상태를 다시 읽고 그 상태가 정하는 수로 `spec.replicas` 만
        패치한다. asleep 이면 적용됐다는 기록과 패치가 만든 세대를 남긴다. 남이 클레임을 쥐었거나
        Deployment 가 없으면 거짓이다."""

        async def work() -> bool:
            await checkpoint()
            sleep = await self.agents.sleep_of(doc.id)
            if sleep is None or doc.k8s is None:
                return False
            count = final_replicas(doc, sleep.state)
            client = await self.proxy.client(doc)
            generation = await run_in_threadpool(
                client.scale_deployment, doc.k8s.name, doc.k8s.namespace, count
            )
            self._deployments.pop(doc.id)
            if generation is None:
                return False
            if sleep.state != "asleep" or count:
                return True
            applied = {"scaled": "applied", "generation": generation}
            return await self.agents.move_sleep(
                doc.id, ("asleep",), applied, also={"since": sleep.since}
            )

        return await self.claimed(doc, work)

    async def _scale_quietly(self, doc: Agent) -> None:
        """깨우기의 레플리카 되돌리기. 실패는 로그만 남긴다. 다음 시도의 게이트와 워커가 원하는 수가
        0 인 것을 보고 다시 한다."""
        try:
            await self.apply_replicas(doc)
        except Exception as e:  # noqa: BLE001 - 다시 보는 쪽이 있다
            logger.warning("agent %s: replicas not restored on wake: %s", doc.id, e)

    async def _deployment(self, doc: Agent, *, fresh: bool = False) -> dict[str, Any] | None:
        """이 Agent 의 Deployment. 레플리카마다 `READY_CACHE_SECONDS` 기억한다. 읽지 못하면 None
        이다."""
        hit = None if fresh else self._deployments.get(doc.id)
        if hit is not None:
            return hit.dep
        if doc.k8s is None:
            return None
        try:
            client = await self.proxy.client(doc)
            dep = await run_in_threadpool(
                client.get, "apps/v1", "Deployment", doc.k8s.name, doc.k8s.namespace
            )
        except Exception as e:  # noqa: BLE001 - 읽지 못하면 깨우는 중으로 답한다
            logger.info("agent %s: deployment not read: %s", doc.id, e)
            return None
        self._deployments.put(doc.id, _Read(dep))
        return dep

    async def _undrain(self, doc: Agent, mark: str | None = None) -> bool:
        """런타임의 드레인 표시를 지운다(`DELETE /drain`). mark 를 주지 않으면 문서의 마지막
        잠들기의 표시다. 런타임은 그 id 의 표시만 지우므로 그 사이 다른 잠들기가 세운 표시는 남는다.
        표시가 없는 문서는 지울 것이 없다. 실패는 로그만 남긴다. 표시는 2분 뒤 스스로 풀리고, 그
        사이 턴은 프록시가 다시 지우고 다시 보낸다."""
        mark = mark or doc.sleep.mark
        if mark is None:
            return False
        try:
            await self.proxy.undrain(doc, mark=mark)
        except Exception as e:  # noqa: BLE001 - 표시의 수명이 받친다
            logger.info("agent %s: drain mark not cleared: %s", doc.id, e)
            return False
        return True

    # -- 워커가 쓰는 걸음 (7절, 11절) -------------------------------------------------------

    async def candidates(self, now: datetime) -> list[Agent]:
        window = self.window
        return await self.agents.sleep_candidates(
            idle_before=now - window if window is not None else None,
            stale_before=now - DRAIN_MARK,
            capability=SCALE_TO_ZERO,
        )

    async def open_runs(self, doc: Agent) -> bool:
        """starting 이나 running 인 실행이 있는가. 실행을 끈 배포본은 거짓이다."""
        if self.runs is None:
            return False
        return bool(await self.runs.count_in_statuses(doc.tenant, doc.id, MOVING_STATUSES))

    async def retag(self, doc: Agent, reason: str) -> bool:
        """awake 문서의 까닭만 바꾼다. 읽은 since 가 그대로일 때만이다."""
        return await self.agents.move_sleep(
            doc.id, ("awake",), {"reason": reason}, also={"since": doc.sleep.since}
        )

    async def wake_for(self, doc: Agent, reason: str) -> bool:
        """워커의 깨우기(잠들 수 없게 됨, 매일 깨우기). 이긴 쪽만 참이다."""
        return await self._wake(doc, reason, to_millis(utcnow()))

    async def back_to_awake(
        self, doc: Agent, state: SleepState, reason: str, *, touch: bool = False
    ) -> bool:
        """워커가 문서를 awake 로 돌린다. 읽은 상태와 since 가 그대로일 때만이다. draining 을
        돌렸으면 그 잠들기의 표시를 지운다."""
        now = to_millis(utcnow())
        fields = {"state": "awake", "since": now, "reason": reason, **UNSCALED}
        moved = await self.agents.move_sleep(
            doc.id,
            (state,),
            fields,
            also={"since": doc.sleep.since},
            touch=now if touch else None,
        )
        if moved and state == "draining":
            await self._undrain(doc)
        return moved

    async def stray(self, doc: Agent) -> bool:
        """0 으로 줄이기를 적용했는데 Deployment 가 다시 파드를 원하는가 (옛 레플리카의 렌더, 손으로
        한 scale). 그렇다면 0 으로 바로 줄이지 않고 awake 로 돌려 걸음을 다시 밟는다 (7절)."""
        dep = await self._deployment(doc, fresh=True)
        if dep is None:
            return False
        wanted, _ready = deployment_counts(dep)
        return wanted > 0


class AgentSleepWorker(QueuedLeaseWorker[Agent]):
    """쉬기 워커 (7절, 11절). 30초마다 볼 문서를 고르고(`AgentSleepControl.candidates`) 문서마다
    리스 `agent-sleep:<id>` 를 걸어 한 레플리카만 다룬다. 매일 깨우기의 상한은 클러스터마다의 리스
    `agent-housekeeping:<cluster>` 안에서 센다. 문서마다의 리스와 매일 깨우기 리스를 같은 홀더로
    쥔다."""

    task_prefix = "agent-sleep"
    tick_label = "agent sleep tick"
    item_label = "agent"
    log = logger

    def __init__(
        self,
        sleep: AgentSleepControl,
        lease: Lease,
        *,
        owner: str | None = None,
        interval: float,
    ):
        super().__init__(lease, owner=owner, interval=interval)
        self.sleep = sleep

    @property
    def settings(self) -> Settings:
        return self.sleep.settings

    # -- QueuedLeaseWorker ----------------------------------------------------------------

    async def queue(self, now: datetime) -> list[Agent]:
        """볼 문서를 고른다. 스스로 잠들 수 없는 awake 문서는 리스를 걸기 전에 뺀다."""
        candidates = await self.sleep.candidates(now)
        return [doc for doc in candidates if not self._never_sleeps(doc)]

    async def handle(self, item: Agent, now: datetime) -> None:
        await self.tend(item, now)

    def _never_sleeps(self, doc: Agent) -> bool:
        """할 걸음이 없는 문서다. 매일 깨우기로 깨어 있지 않은 awake 문서가 잠들 수 없으면
        그렇다."""
        sleep = doc.sleep
        awake = sleep.state == "awake" and sleep.reason != HOUSEKEEPING
        return awake and self.sleep.ineligible(doc) is not None

    # -- 문서 하나 ----------------------------------------------------------------------------

    async def tend(self, doc: Agent, now: datetime) -> None:
        """문서의 상태에 맞는 걸음 하나."""
        state = doc.sleep.state
        if state == "awake":
            await self._awake(doc, now)
        elif state == "draining":
            # 멈춘 워커가 남긴 것이다. 표시는 이미 스스로 사라졌다
            await self.sleep.back_to_awake(doc, "draining", STALE_DRAIN, touch=True)
        elif state == "asleep":
            await self._asleep(doc, now)
        elif await self.sleep.settle_waking(doc):
            return
        elif doc.sleep.since is not None and now - doc.sleep.since >= WAKE_LIMIT:
            # 준비된 파드 없이 5분이다. 파드는 그대로 원하고 상태가 그 까닭을 보인다
            await self.sleep.back_to_awake(doc, "waking", WAKE_TIMEOUT)

    async def _awake(self, doc: Agent, now: datetime) -> None:
        sleep = doc.sleep
        if sleep.reason == HOUSEKEEPING:
            await self._held(doc, now)
            return
        if self.sleep.ineligible(doc) is not None or await self.sleep.open_runs(doc):
            return
        window = self.sleep.window
        if window is None:
            return
        if sleep.activity is None:
            await self.sleep.agents.start_window(doc.id, now)
            return
        if now - sleep.activity >= window:
            await self.sleep.put_to_sleep(doc, IDLE, check_activity=True)

    async def _held(self, doc: Agent, now: datetime) -> None:
        """매일 깨우기로 깨어 있는 문서 (11절). 잠들 수 없게 됐으면 까닭을 바꿔 클러스터의 몫을
        놓는다. hold 가 지나면 다시 재운다. 다만 창이 있고 hold 안에 활동이 있었으면 까닭을 바꿔
        몫을 놓고 창에 넘긴다. 사람이 쓰기 시작한 Agent 를 창과 상관없이 재우지 않는다. 창이 0 이면
        명세대로 hold 끝에 재운다."""
        sleep = doc.sleep
        if self.sleep.ineligible(doc) is not None:
            await self.sleep.retag(doc, INELIGIBLE)
            return
        if sleep.since is None or now - sleep.since < housekeeping_hold(self.settings):
            return
        used = sleep.activity is not None and sleep.activity > sleep.since
        if used and self.sleep.window is not None:
            await self.sleep.retag(doc, ACTIVE)
            return
        if not await self.sleep.open_runs(doc):
            await self.sleep.put_to_sleep(doc, HOUSEKEEPING, check_activity=True)

    async def _asleep(self, doc: Agent, now: datetime) -> None:
        if self.sleep.ineligible(doc) is not None:
            await self.sleep.wake_for(doc, INELIGIBLE)
            return
        if doc.sleep.scaled != "applied":
            await self.sleep.apply_replicas(doc)
            return
        if await self.sleep.stray(doc):
            await self.sleep.back_to_awake(doc, "asleep", STRAY_PODS, touch=True)
            return
        period = timedelta(hours=self.settings.agent_housekeeping_hours)
        due = housekeeping_at(doc, period)
        if due is not None and now >= due:
            await self._housekeeping(doc, now)

    async def _housekeeping(self, doc: Agent, now: datetime) -> None:
        """매일 깨우기 (11절). 클러스터의 리스 안에서 지금 매일 깨우기로 깨어 있는 수를 세고 상한
        아래일 때만 깨운다. 리스를 쥐지 못했거나 상한이면 다음 틱이 다시 본다."""
        if doc.cluster_id is None:
            return
        name = f"agent-housekeeping:{doc.cluster_id}"
        if not await self.lease.acquire(name):
            return
        try:
            held = await self.sleep.agents.housekeeping_wakes(
                doc.cluster_id, since_after=now - housekeeping_span(self.settings)
            )
            if held < self.settings.agent_housekeeping_parallel:
                await self.sleep.wake_for(doc, HOUSEKEEPING)
        finally:
            await self.lease.release(name)


__all__ = [
    "AGENT_BUSY",
    "AGENT_SLEEP_INELIGIBLE",
    "AgentSleepControl",
    "AgentSleepWorker",
    "SleepIneligible",
    "Slept",
    "final_replicas",
    "housekeeping_at",
    "housekeeping_hold",
    "housekeeping_offset",
    "housekeeping_span",
    "ineligible",
    "ineligible_all",
]
