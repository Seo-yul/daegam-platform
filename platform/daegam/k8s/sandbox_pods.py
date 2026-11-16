"""샌드박스 파드의 상태 읽기와 그 판단 (P40 5절·8절).

정하는 것은 끝 코드뿐이다. 플랫폼이 파드 status 에서 읽는 끝 코드는 코드가 꾸밀 수 없고, 종료
메시지는 보이려고 읽을 뿐 아무것도 정하지 않는다(`sandbox/daegam_sandbox/selfcheck.py`).

- 87: 첫 자체 점검(준비 전, 제자리 재시작 뒤 준비 전 포함)이 uid map 이나 닿은 대상으로 실패했다.
  판정을 쓰기로 무효로 한다
- 86: 점검을 하지 못했다. 그 열기만 503 이다
- 88: 5분마다의 뒤 점검이 실패했다. 그 파드만 끝내고 사용자 코드 없는 프로브를 돌린다
- 그 밖에 준비되지 못한 것(당기기, 자리, 배치, OOM 재시작)은 그 열기만 503 이고 아무것도 무효로 하지
  않는다

열기는 파드가 배치되지 않았거나 컨테이너를 만드는 동안 120초, 컨테이너가 돈 뒤로는 30초를
기다린다(5절). 부르는 쪽의 남은 시간이 더 짧으면 그것이다.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Literal

from daegam.k8s.templates.sandbox import SANDBOX_CONTAINER, host_users_kept
from daegam.store.base import parse_iso_utc

EXIT_COULD_NOT_RUN = 86
EXIT_FIRST_FAILED = 87
EXIT_LATER_FAILED = 88
# 열기가 기다리는 시간 (5절)
CREATE_WAIT = timedelta(seconds=120)
READY_WAIT = timedelta(seconds=30)
# 기록을 넣은 레플리카가 아직 파드를 만들지 않았을 수 있다. 다른 열기와 스윕은 이만큼 기다린다
POD_CREATE_GRACE = timedelta(seconds=30)
# 이만큼 다시 뜬 컨테이너의 샌드박스는 스윕이 닫는다(5절). 뜰 때마다 RLIMIT_FSIZE 까지의 종료
# 로그가 노드 디스크에 남을 수 있다
MAX_RESTARTS = 3
PULL_WAITING = frozenset({"ErrImagePull", "ImagePullBackOff", "InvalidImageName"})
# 다시 해도 낫지 않는 컨테이너 만들기의 실패. user namespace 를 받지 못한 노드가 여기 든다
CONTAINER_ERRORS = frozenset(
    {"CreateContainerConfigError", "CreateContainerError", "RunContainerError"}
)
TERMINAL_PHASES = frozenset({"Succeeded", "Failed"})

# 열기의 503 `sandbox_open_failed` 가 싣는 까닭. 런타임이 모델에게 도구 오류로 옮긴다
FailReason = Literal[
    "self_check",
    "check_could_not_run",
    "oom",
    "restarted",
    "pod_ended",
    "pod_gone",
    "pull",
    "unscheduled",
    "container_error",
    "not_ready",
    "host_users",
    "refused",
]


@dataclass(frozen=True)
class PodView:
    """샌드박스 컨테이너 하나의 지금 모습. exit_code 는 지금 끝난 상태나 바로 앞에 끝난 상태의 끝
    코드이고, terminated_reason 은 그 까닭(`OOMKilled` 같은)이다."""

    exists: bool
    phase: str | None = None
    scheduled: bool = False
    ready: bool = False
    running_since: datetime | None = None
    restarts: int = 0
    exit_code: int | None = None
    terminated_reason: str | None = None
    waiting_reason: str | None = None
    image_id: str | None = None
    pod_ip: str | None = None
    host_users_kept: bool = True


def _container_status(status: dict[str, Any]) -> dict[str, Any]:
    for cs in status.get("containerStatuses") or []:
        if cs.get("name") == SANDBOX_CONTAINER:
            return cs
    return {}


def pod_view(pod: dict[str, Any] | None) -> PodView:
    if pod is None:
        return PodView(exists=False)
    status = pod.get("status") or {}
    cs = _container_status(status)
    state = cs.get("state") or {}
    terminated = state.get("terminated") or (cs.get("lastState") or {}).get("terminated") or {}
    running = state.get("running") or {}
    conditions = status.get("conditions") or []
    exit_code = terminated.get("exitCode")
    return PodView(
        exists=True,
        phase=status.get("phase"),
        scheduled=any(
            c.get("type") == "PodScheduled" and c.get("status") == "True" for c in conditions
        ),
        ready=cs.get("ready") is True,
        running_since=parse_iso_utc(running.get("startedAt")) if running else None,
        restarts=int(cs.get("restartCount") or 0),
        exit_code=exit_code if isinstance(exit_code, int) else None,
        terminated_reason=terminated.get("reason"),
        waiting_reason=(state.get("waiting") or {}).get("reason"),
        image_id=cs.get("imageID") or None,
        pod_ip=status.get("podIP") or None,
        host_users_kept=host_users_kept(pod),
    )


@dataclass(frozen=True)
class Judgement:
    """열기가 기다리는 파드의 판단. ready 면 쓸 수 있고, fail 이 있으면 그 까닭으로 실패이며, 둘 다
    아니면 더 기다린다. void 면 판정을 쓰기로 무효로 한다(87)."""

    ready: bool = False
    fail: FailReason | None = None
    void: bool = False


WAIT = Judgement()


def judge_open(view: PodView, *, opened_at: datetime, now: datetime) -> Judgement:
    """여는 중인 파드의 판단. 아직 명령이 하나도 돌지 않은 파드다. 파드가 없으면 기록을 넣은
    레플리카가 아직 만들지 않았을 수 있어 `POD_CREATE_GRACE` 까지 기다린다."""
    if not view.exists:
        return WAIT if now - opened_at <= POD_CREATE_GRACE else Judgement(fail="pod_gone")
    if not view.host_users_kept:
        return Judgement(fail="host_users")
    if view.exit_code == EXIT_FIRST_FAILED:
        return Judgement(fail="self_check", void=True)
    if view.exit_code == EXIT_COULD_NOT_RUN:
        return Judgement(fail="check_could_not_run")
    if view.exit_code is not None or view.restarts:
        return Judgement(fail="oom" if view.terminated_reason == "OOMKilled" else "restarted")
    if view.phase in TERMINAL_PHASES:
        return Judgement(fail="pod_ended")
    if view.ready:
        return Judgement(ready=True)
    if view.waiting_reason in CONTAINER_ERRORS:
        return Judgement(fail="container_error")
    if view.running_since is None:
        if now - opened_at <= CREATE_WAIT:
            return WAIT
        if view.waiting_reason in PULL_WAITING:
            return Judgement(fail="pull")
        return Judgement(fail="unscheduled" if not view.scheduled else "not_ready")
    if now - view.running_since <= READY_WAIT:
        return WAIT
    return Judgement(fail="not_ready")


# 스윕이 열린 샌드박스를 닫는 까닭
SweepAction = Literal["keep", "lost", "void", "later_failed", "restarts"]


def judge_open_record(view: PodView) -> SweepAction:
    """준비됐던 샌드박스의 파드를 스윕이 본다. 87 은 제자리 재시작 뒤의 첫 점검이라도 믿는다.
    재시작이 앞의 프로세스를 모두 끝냈고 서버는 남는 emptyDir 에서 아무것도 싣지 않기 때문이다.
    OOM 재시작 하나는 파일을 남긴 채 둔다."""
    if not view.exists or view.phase in TERMINAL_PHASES:
        return "lost"
    if not view.host_users_kept:
        return "lost"
    if view.exit_code == EXIT_FIRST_FAILED:
        return "void"
    if view.exit_code == EXIT_LATER_FAILED:
        return "later_failed"
    if view.restarts >= MAX_RESTARTS:
        return "restarts"
    return "keep"


def pod_address(pod_ip: str) -> str:
    """샌드박스 서버의 주소. IPv6 는 대괄호로 싼다."""
    host = f"[{pod_ip}]" if ":" in pod_ip else pod_ip
    return f"http://{host}:8080"
