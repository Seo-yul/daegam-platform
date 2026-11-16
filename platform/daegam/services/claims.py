"""문서 하나의 클러스터 작업을 묶는 클레임 (P66, D78).

MCP Server·Gateway·Agent 문서 하나에 클러스터 작업을 하는 실행(provision·해제·삭제)은 그
문서의 클레임을 쥔다. 클레임은 `reconcile_leases` 의 리스 `instance:<kind>:<id>` 이고 홀더는
실행마다 다르다(파드 id 와 난수). 같은 요청 안에서 안쪽 동작은 바깥이 쥔 클레임을 물려받는다.

실행은 체크포인트에서 클레임을 늘리고 문서를 다시 읽는다. 명시 체크포인트는 쓰기 단계·끝
저장·토큰 발급과 회수·기억 저장소 앞이고(`checkpoint`), 훅 체크포인트는 마지막 체크포인트가
`HOOK_INTERVAL` 을 넘겼을 때 클러스터 요청 앞이다(`request_hook`, P65 의 `rest_client.request`
래퍼가 부른다). 이어 갈 수 없으면 `LeaseStop` 을 올리고 클레임을 잡은 자리(`BaseInstanceService`)가
결과를 고른다.

실행의 상태는 컨텍스트 변수가 가리키는 `ClaimRun` 하나에 산다. 클레임 범위가 들어갈 때 변수에 새
실행을 놓고 나갈 때 되돌린다. anyio 의 워커 스레드는 컨텍스트 사본에서 돌므로 변수는 바꾸지 않고 그
객체의 필드만 바꾼다.

리컨실 틱이 쥔 리스(`mcp-instances`, P67)도 같은 훅과 체크포인트에서 늘린다(`TickHold`). 틱은
반복의 항목마다 `tick_item` 으로 늘리고, 잃으면 `LeaseStop` 이 단계의 처리기를 지나 틱으로 간다.
문서를 몰아가는 중(`driving`)에는 잃었다고 적기만 하고 그 문서는 자기 클레임으로 마친다. 리스
수명보다 길 수 있는 한 걸음(MCP 메트릭 롤업의 집계 하나)은 `renewing` 으로 그동안에도 늘리되, 한도를
두어 멈춘 걸음이 리스를 끝없이 쥐지 않게 한다.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from collections.abc import AsyncIterator, Awaitable, Callable, Collection, Iterator
from contextvars import ContextVar
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import Enum
from typing import Any

import anyio.from_thread
from pymongo.asynchronous.database import AsyncDatabase

from daegam.services.periodic import renewing_every
from daegam.store.base import utcnow
from daegam.store.leases import Lease, LeaseStop, run_lease

logger = logging.getLogger(__name__)

# 클레임의 수명. 두 갱신 사이의 가장 긴 틈은 HOOK_INTERVAL 과 P65 가 묶은 요청 하나(최악 65초)라
# 그 안에 든다 (P66 2.4절)
CLAIM_TTL = timedelta(seconds=90)
# 마지막 체크포인트 뒤 이만큼 지난 클러스터 요청 앞에서 훅이 체크포인트를 한다
HOOK_INTERVAL = 10.0
# 삭제·해제가 남이 쥔 클레임을 기다리는 시간과 다시 보는 간격 (P66 2.6절)
CLAIM_WAIT_SECONDS = 30.0
CLAIM_POLL_SECONDS = 0.5


class Outcome(Enum):
    """체크포인트가 본 문서의 자리. CONTINUE 밖은 실행을 멈추고 클레임 범위가 결과를 고른다."""

    CONTINUE = "continue"
    # 클레임을 잃었다. 새 홀더가 문서를 맡는다
    LOST = "lost"
    # 그 사이 삭제·해제가 상태만 옮기고 일을 홀더에게 남겼다. 같은 클레임으로 그 동작을 마친다
    DELETING = "deleting"
    UNDEPLOYING = "undeploying"
    # 문서가 없거나(또는 삭제 중인데 클레임을 잃었거나) 이 실행이 남기지 않은 draft 다
    GONE = "gone"
    # 문서가 다른 클러스터나 네임스페이스로 옮겨 갔다
    MOVED = "moved"
    # 같은 이름으로 다시 배포됐다. 새 실행이 그 이름을 맡는다
    REDEPLOYED = "redeployed"


def claim_name(kind: str, doc_id: str) -> str:
    return f"instance:{kind}:{doc_id}"


@dataclass(eq=False)
class ClaimRun:
    """클레임 하나를 쥔 실행. 판정(`judge`)은 문서 종류를 아는 서비스가 넘긴다."""

    lease: Lease
    name: str
    doc_id: str
    judge: Callable[[ClaimRun], Awaitable[Outcome]]
    # 이 실행이 이어 가도 되는 문서 상태
    expect: frozenset[str]
    # 실행이 맡은 배포의 자리. 다른 자리로 옮겨 갔으면 MOVED 다
    cluster_id: str | None = None
    namespace: str | None = None
    deploying_since: datetime | None = None
    last: float = field(default_factory=lambda: time.monotonic())
    stopped: Outcome | None = None
    released: bool = False
    # 이 실행이 낸 토큰 id. 옮겨 간 문서에는 이것만 회수한다 (P66 2.5절)
    issued: list[str] = field(default_factory=list)

    def place(self, doc: object) -> None:
        """문서의 배포 자리를 이 실행의 자리로 적는다."""
        k8s = getattr(doc, "k8s", None)
        self.cluster_id = getattr(doc, "cluster_id", None)
        self.namespace = getattr(k8s, "namespace", None) if k8s is not None else None
        self.deploying_since = getattr(doc, "deploying_since", None)

    async def verdict(self) -> Outcome:
        """판정. 판정 자체가 실패하면(스토어가 잠깐 흔들림) 이어 간다. 다음 체크포인트가 다시
        본다."""
        try:
            outcome = await self.judge(self)
        except Exception as e:  # noqa: BLE001 - 판정 실패가 실행을 멈추면 안 된다
            logger.warning("claim %s: checkpoint failed: %s", self.name, e)
            outcome = Outcome.CONTINUE
        self.last = time.monotonic()
        return outcome

    def _stop_unless_continue(self, outcome: Outcome) -> None:
        if outcome is not Outcome.CONTINUE:
            self.stopped = outcome
            raise LeaseStop(self.name, outcome)

    async def checkpoint(self) -> None:
        """명시 체크포인트. 이어 갈 수 없으면 LeaseStop 이다."""
        if self.stopped is not None:
            raise LeaseStop(self.name, self.stopped)
        self._stop_unless_continue(await self.verdict())

    def hook(self) -> None:
        """클러스터 요청 앞의 훅 체크포인트. 이미 멈춘 실행은 다시 갱신하지 않고 바로 멈춘다. anyio
        워커 스레드가 아니면(이벤트 루프 스레드 포함) 아무것도 하지 않는다. 판정 코루틴은 루프에서
        돌고 워커 스레드를 쓰지 않으므로 훅에 묶인 스레드가 많아도 막히지 않는다."""
        if self.stopped is not None:
            raise LeaseStop(self.name, self.stopped)
        if self.released or time.monotonic() - self.last < HOOK_INTERVAL:
            return
        outcome = _from_worker(self.verdict)
        if outcome is not _NOT_WORKER:
            self._stop_unless_continue(outcome)  # type: ignore[arg-type]


_current: ContextVar[ClaimRun | None] = ContextVar("daegam_claim_run", default=None)

# 워커 스레드가 아니라 루프로 부르지 못했다는 표시
_NOT_WORKER = object()


def _from_worker[T](fn: Callable[[], Awaitable[T]]) -> T | object:
    """워커 스레드에서 루프의 코루틴을 돌려 그 답을 받는다. anyio 워커 스레드가 아니면(이벤트 루프
    스레드 포함) 돌리지 않고 `_NOT_WORKER` 다. 코루틴은 워커 스레드를 쓰지 않으므로 훅에 묶인
    스레드가 많아도 막히지 않는다."""
    try:
        return anyio.from_thread.run(fn)
    except RuntimeError:
        return _NOT_WORKER


def current_run() -> ClaimRun | None:
    return _current.get()


def enter_run(run: ClaimRun) -> object:
    """클레임 범위에 들어가며 이 태스크의 실행을 놓는다. 돌려주는 것은 `leave_run` 의 표다."""
    return _current.set(run)


def leave_run(token: object) -> None:
    _current.reset(token)  # type: ignore[arg-type]


# -- 리컨실 틱의 리스 (P67) ------------------------------------------------------------------
# 틱이 쥔 `mcp-instances` 리스도 같은 훅과 체크포인트에서 늘린다. 홀더는 파드 id 라 늘리기는
# `Lease.renew`(그 사이 남이 잡지 않았을 때만)다. 잃으면 틱 밖으로 `LeaseStop` 을 올리되, 문서를
# 몰아가는 중(`driving`)에는 잃었다고 적기만 하고 그 문서는 자기 클레임으로 마친다 (P67 2절)

# 항목 확인이 리스를 늘리는 가장 짧은 간격(초). 항목이 많은 반복이 항목마다 쓰지 않게 한다
TICK_RENEW_FLOOR = 1.0


@dataclass(eq=False)
class TickHold:
    """틱 하나가 쥔 리스. 워커 스레드는 컨텍스트 사본에서 돌므로 필드만 바꾼다."""

    lease: Lease
    name: str
    last: float = field(default_factory=lambda: time.monotonic())
    lost: bool = False
    # 문서를 몰아가는 중이면 0 보다 크다. 그동안은 잃어도 멈추지 않는다
    driving: int = 0

    async def renew(self) -> None:
        """리스를 늘린다. 스토어가 잠깐 흔들려 늘리지 못한 것은 잃은 것이 아니다. 한 번 잃으면 다시
        늘리지 않는다. 그 사이 이 파드가 리스를 다시 잡았어도 남은 태스크가 그것을 늘리면 안
        된다."""
        if self.lost:
            return
        try:
            ok = await self.lease.renew(self.name)
        except Exception as e:  # noqa: BLE001 - 늘리기의 실패가 틱을 멈추면 안 된다
            logger.warning("lease %s: renew failed: %s", self.name, e)
            return
        self.last = time.monotonic()
        if not ok:
            self.lost = True
            logger.warning("lease %s: lost during the tick; stopping", self.name)

    def raise_if_lost(self) -> None:
        if self.lost and self.driving == 0:
            raise LeaseStop(self.name)

    async def item(self) -> None:
        """반복의 항목 확인. 늘리고, 잃었으면(문서를 몰아가는 중이 아니면) 멈춘다."""
        if time.monotonic() - self.last >= TICK_RENEW_FLOOR:
            await self.renew()
        self.raise_if_lost()

    def hook(self) -> None:
        """클러스터 요청 앞의 훅. 잃었으면 늘리지 않고 바로 멈춘다. anyio 워커 스레드에서만
        늘린다."""
        self.raise_if_lost()
        if self.lost or time.monotonic() - self.last < HOOK_INTERVAL:
            return
        if _from_worker(self.renew) is not _NOT_WORKER:
            self.raise_if_lost()


_tick: ContextVar[TickHold | None] = ContextVar("daegam_tick_hold", default=None)


def enter_tick(hold: TickHold) -> object:
    return _tick.set(hold)


def leave_tick(token: object) -> None:
    _tick.reset(token)  # type: ignore[arg-type]


async def leased_tick(
    lease: Lease,
    name: str,
    run: Callable[[datetime, TickHold], Awaitable[None]],
    *,
    log: logging.Logger,
    label: str,
) -> None:
    """한 레플리카만 도는 루프의 틱 하나 (P67). 리스를 잡지 못하면 아무것도 하지 않는다. 잡으면
    `TickHold` 를 세우고 `run(지금, hold)` 를 돌리며, 그 리스를 잃어 멈춘 것은 경고로 남긴다."""
    now = utcnow()
    if not await lease.acquire(name, now=now):
        return
    hold = TickHold(lease, name)
    token = enter_tick(hold)
    try:
        await run(now, hold)
    except LeaseStop as stop:
        if stop.name != name:
            raise
        log.warning("%s: lease lost during the tick; stopping", label)
    finally:
        leave_tick(token)


async def tick_item() -> None:
    """틱 안 반복의 항목 확인 (P67 2절). 틱 밖이면 아무것도 하지 않는다."""
    hold = _tick.get()
    if hold is not None:
        await hold.item()


async def tick_fence() -> None:
    """틱 안에서 쓰기 바로 앞의 확인. 늘 리스를 늘리고, 리스 수명 안에 늘린 적이 없으면 잃은
    것으로 보고 멈춘다. 늘리기가 오류로 계속 실패하는 사이 리스가 끝나 다른 레플리카가 잡았을 수
    있기 때문이다. 틱 밖이면 아무것도 하지 않는다."""
    hold = _tick.get()
    if hold is None:
        return
    await hold.renew()
    if time.monotonic() - hold.last >= hold.lease.ttl.total_seconds():
        hold.lost = True
    hold.raise_if_lost()


@contextlib.asynccontextmanager
async def renewing(limit: float) -> AsyncIterator[None]:
    """틱 안의 긴 한 걸음(Mongo 집계 하나)이 도는 동안 틱의 리스를 늘린다. 항목 확인은
    걸음 사이에서만 늘리므로, 한 걸음이 리스 수명보다 길면 그 사이 다른 레플리카가 리스를
    가져갈 수 있다. 늘리기가 거절되면 멈추고, 걸음 뒤의 항목 확인이 틱을 멈춘다. limit 초가 지나면
    더 늘리지 않으므로 멈춘 걸음이 리스를 끝없이 쥐지 않는다(D79). 틱 밖이면 아무것도 하지
    않는다."""
    hold = _tick.get()
    if hold is None:
        yield
        return

    async def renew() -> bool:
        await hold.renew()
        return not hold.lost

    async with renewing_every(hold.lease.ttl.total_seconds(), renew, limit=limit):
        yield


@contextlib.contextmanager
def driving() -> Iterator[None]:
    """리컨실이 문서 하나를 몰아가는 동안. 틱의 리스를 잃어도 그 문서는 자기 클레임으로 마친다."""
    hold = _tick.get()
    if hold is None:
        yield
        return
    hold.driving += 1
    try:
        yield
    finally:
        hold.driving -= 1


def request_hook() -> None:
    """P65 래퍼가 모든 클러스터 요청 앞에서 부르는 훅. 틱의 리스와 지금 실행의 클레임을 늘린다.
    둘 다 없으면 아무것도 하지 않는다."""
    hold = _tick.get()
    if hold is not None:
        hold.hook()
    run = _current.get()
    if run is not None:
        run.hook()


async def checkpoint() -> None:
    """지금 실행의 명시 체크포인트. 틱 안이면 틱의 리스도 늘린다. 실행 밖이면 아무것도 하지
    않는다."""
    run = _current.get()
    if run is not None:
        hold = _tick.get()
        if hold is not None:
            await hold.renew()
        await run.checkpoint()


def note_issued(token_id: str) -> None:
    """지금 실행이 낸 토큰 id 를 적는다."""
    run = _current.get()
    if run is not None:
        run.issued.append(token_id)


@contextlib.contextmanager
def detached() -> Iterator[None]:
    """실행 밖처럼 돈다. 클레임을 잃은 뒤의 정리가 훅에 다시 걸리지 않게 한다."""
    token = _current.set(None)
    try:
        yield
    finally:
        _current.reset(token)


class Claims:
    """클레임 리스를 만드는 곳. 서비스 셋이 나눠 쓴다."""

    def __init__(
        self,
        db: AsyncDatabase,
        pod_id: str,
        *,
        ttl: timedelta = CLAIM_TTL,
        wait: float = CLAIM_WAIT_SECONDS,
        poll: float = CLAIM_POLL_SECONDS,
    ):
        self.db = db
        self.pod_id = pod_id
        self.ttl = ttl
        self.wait = wait
        self.poll = poll

    def lease(self) -> Lease:
        """실행 하나의 리스. 같은 파드의 두 실행이 홀더를 나눠 쓰지 않는다."""
        return run_lease(self.db, self.pod_id, ttl=self.ttl)


# -- 클레임 범위 ------------------------------------------------------------------------------
# 문서를 읽는 함수(`read`)만 받으므로 문서 종류를 모른다. 결과를 고르는 일(`_settle`)은 종류를
# 아는 서비스(`BaseInstanceService`)에 남는다

Read = Callable[[str], Awaitable[Any]]


class Taken(Enum):
    """`take_claim` 이 실행을 내주지 않은 까닭."""

    # 기다리는 사이 홀더가 그 동작을 마쳤다(done 이 참)
    DONE = "done"
    # 기다려도 남이 쥐고 있어 일을 홀더에게 남긴다
    DEFERRED = "deferred"
    # 기다리지 않는 자리에서 남이 쥐고 있다
    HELD = "held"
    # 쥐고 보니 문서가 없거나 상태가 기대 밖이다
    CHANGED = "changed"


def same_ms(a: datetime | None, b: datetime | None) -> bool:
    """같은 배포 표시인가. Mongo 는 시각을 밀리초까지 저장하므로 메모리의 값과 그 자리까지만
    견준다."""
    if a is None or b is None:
        return a is b

    def ms(d: datetime) -> tuple[Any, ...]:
        return (*d.replace(tzinfo=None).timetuple()[:6], d.microsecond // 1000)

    return ms(a) == ms(b)


def copy_into(inst: Any, fresh: Any) -> None:
    """다시 읽은 문서를 부른 쪽의 객체에 덮는다. 부른 쪽이 들고 있는 참조가 그대로 새 값을 본다."""
    for name in type(inst).model_fields:
        setattr(inst, name, getattr(fresh, name))


def judge_with(read: Read) -> Callable[[ClaimRun], Awaitable[Outcome]]:
    """체크포인트의 판정 (P66 2.2절). 클레임을 늘리고 문서를 다시 읽어 자리를 고른다."""

    async def judge(run: ClaimRun) -> Outcome:
        renewed = await run.lease.renew(run.name)
        fresh = await read(run.doc_id)
        if fresh is None:
            return Outcome.GONE
        if fresh.state == "draft" and "draft" not in run.expect:
            return Outcome.GONE
        namespace = fresh.k8s.namespace if fresh.k8s is not None else None
        if (fresh.cluster_id, namespace) != (run.cluster_id, run.namespace):
            return Outcome.MOVED
        if not renewed:
            # 삭제 중인 문서의 클레임을 잃었으면 늦은 쓰기를 문서 단위로 거둔다
            if fresh.state == "deleting" and "deleting" not in run.expect:
                return Outcome.GONE
            return Outcome.LOST
        if fresh.state == "deleting" and "deleting" not in run.expect:
            return Outcome.DELETING
        if fresh.state == "undeploying" and "undeploying" not in run.expect:
            return Outcome.UNDEPLOYING
        if fresh.state in run.expect and same_ms(fresh.deploying_since, run.deploying_since):
            return Outcome.CONTINUE
        return Outcome.REDEPLOYED

    return judge


async def take_claim(
    claims: Claims,
    kind: str,
    inst: Any,
    expect: Collection[str],
    read: Read,
    *,
    wait: bool = False,
    done: Callable[[Any], bool] | None = None,
) -> ClaimRun | Taken:
    """문서 inst 의 클레임을 쥔다 (P66 2.1절).

    - 같은 문서의 클레임을 이 태스크가 이미 쥐고 있으면 그것을 물려받는다
    - 남이 쥐고 있으면 wait 일 때만 `claims.wait` 까지 기다린다. 그 사이 done(지금 문서)이 참이면
      DONE, 시간이 다하면 DEFERRED, 기다리지 않으면 HELD 다
    - 쥐면 문서를 다시 읽어 inst 에 덮는다. 문서가 없거나 상태가 expect 밖이면 놓고 done 이 참이면
      DONE, 아니면 CHANGED 다"""
    current = current_run()
    if current is not None and current.doc_id == inst.id and not current.released:
        return current
    name = claim_name(kind, inst.id)
    lease = claims.lease()
    deadline = time.monotonic() + (claims.wait if wait else 0.0)
    while not await lease.acquire(name):
        if done is not None and done(await read(inst.id)):
            return Taken.DONE
        if time.monotonic() >= deadline:
            return Taken.DEFERRED if wait else Taken.HELD
        await asyncio.sleep(claims.poll)
    fresh = await read(inst.id)
    if fresh is None or fresh.state not in expect:
        await lease.release(name)
        return Taken.DONE if done is not None and done(fresh) else Taken.CHANGED
    copy_into(inst, fresh)
    run = ClaimRun(lease, name, inst.id, judge_with(read), frozenset(expect))
    run.place(inst)
    return run


@contextlib.asynccontextmanager
async def holding(run: ClaimRun | None, expect: Collection[str]) -> AsyncIterator[None]:
    """쥔 클레임의 범위. 새 실행이면 이 태스크의 실행으로 놓고 나갈 때 되돌리며 리스를 놓는다.
    물려받은 실행이면 기대 상태만 잠깐 바꾸고 놓지 않는다."""
    if run is None:
        yield
        return
    if run is current_run():
        saved = (run.expect, run.stopped)
        run.expect, run.stopped = frozenset(expect), None
        try:
            yield
        finally:
            run.expect, run.stopped = saved
        return
    token = enter_run(run)
    try:
        yield
    finally:
        run.released = True
        leave_run(token)
        try:
            await run.lease.release(run.name)
        except Exception as e:  # noqa: BLE001 - 놓지 못한 클레임은 만료가 거둔다
            logger.warning("claim %s: release failed: %s", run.name, e)


@dataclass
class Stopped:
    """`stopping` 이 받은 멈춤의 결과. 멈추지 않았으면 None 이다."""

    outcome: Outcome | None = None


@contextlib.asynccontextmanager
async def stopping(run: ClaimRun | None) -> AsyncIterator[Stopped]:
    """이 실행의 `LeaseStop` 만 받아 결과를 적고 삼킨다. 다른 리스의 멈춤은 그대로 올린다."""
    box = Stopped()
    try:
        yield box
    except LeaseStop as stop:
        if run is None or stop.name != run.name:
            raise
        box.outcome = stop.outcome  # type: ignore[assignment]


__all__ = [
    "CLAIM_TTL",
    "CLAIM_WAIT_SECONDS",
    "HOOK_INTERVAL",
    "ClaimRun",
    "Claims",
    "Outcome",
    "Stopped",
    "Taken",
    "TickHold",
    "driving",
    "enter_tick",
    "leave_tick",
    "tick_item",
    "tick_fence",
    "renewing",
    "copy_into",
    "holding",
    "judge_with",
    "same_ms",
    "stopping",
    "take_claim",
    "checkpoint",
    "claim_name",
    "current_run",
    "detached",
    "enter_run",
    "leave_run",
    "note_issued",
    "request_hook",
]
