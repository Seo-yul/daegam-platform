"""인스턴스 상태 조회의 틀과 그 도우미 (P71 2.4절).

MCP Server·Gateway·Agent 의 상태 응답이 같은 틀을 지난다. draft 는 None, 클러스터 조회, 주 객체
읽기, 읽기 실패의 오류 줄, 노출 상태, 객체가 없을 때의 Pending·Missing 이 한 곳이고, 종류마다
다른 것은 `InstanceStatusMixin` 의 훅이 채운다. 파드·조건을 화면의 말로 옮기는 순수 함수도 여기
있다. P72 의 agent_status·gateway_status 가 이 훅을 구현한다.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import Any, Protocol

from starlette.concurrency import run_in_threadpool

from daegam.k8s.client import AccessDenied, ClusterUnreachable, KubeClient
from daegam.k8s.labels import LABEL_ID
from daegam.services.exposure import ExposureReading, read_exposure
from daegam.store.base import utcnow

logger = logging.getLogger(__name__)

# 스펙 변경 직후의 일시적 Failed 는 Rolling 으로 본다. 세 리소스가 같은 값을 쓴다
ROLLING_GRACE = timedelta(seconds=60)
# 스펙 변경 뒤 이 시간 안에는 상태가 Ready 여도 롤아웃이 끝났는지 한 번 더 본다. Deployment 의
# progressDeadlineSeconds 기본값과 같다. 롤아웃이 이보다 길면 Kubernetes 도 진행이 멈췄다고 본다
ROLLOUT_WATCH = timedelta(seconds=600)


def changed_within(
    last_spec_change: datetime | None, observed_at: datetime, window: timedelta = ROLLING_GRACE
) -> bool:
    """스펙을 바꾼 지 window 가 지나지 않았는가. 바꾼 적이 없으면 거짓이다."""
    return last_spec_change is not None and observed_at - last_spec_change < window


async def read_deployment(client: KubeClient, name: str, namespace: str) -> dict[str, Any] | None:
    """Deployment name. 스펙을 바꾼 직후의 Ready 가 옛 파드의 것인지 보려고 읽는다(MCP Server 의
    프록시, Gateway 의 vMCP). 못 읽으면 None 이고 부르는 쪽은 CR phase 를 그대로 믿는다."""
    try:
        return await run_in_threadpool(client.get, "apps/v1", "Deployment", name, namespace)
    except Exception:  # noqa: BLE001 - 읽지 못하면 CR phase 를 그대로 믿는다
        return None


# 파드의 대기 이유를 보는 phase (`apply_pod_waiting`)
WAITING_PHASES = frozenset({"Pending", "Rolling"})
# 기다려도 저절로 낫지 않는 컨테이너 대기 이유 (D43 둘째). ToolHive 와 Deployment 의 phase 는
# 이미지를 못 받거나 컨테이너가 반복해서 죽어도 Pending 이라 화면이 "곧 됩니다" 를 10분 동안 보인다
POD_FAILED_REASONS: frozenset[str] = frozenset(
    {"ImagePullBackOff", "ErrImagePull", "CrashLoopBackOff", "CreateContainerConfigError"}
)


def condition_rows(status: dict[str, Any]) -> list[dict[str, Any]]:
    """CR·Deployment status 의 conditions 를 화면 표의 행으로 옮긴다. 순수 함수.

    세 리소스의 상태 응답이 같은 표를 쓴다. `lastTransitionTime` 만 `last_transition` 이 된다."""
    return [
        {
            "type": c.get("type"),
            "status": c.get("status"),
            "reason": c.get("reason"),
            "message": c.get("message"),
            "last_transition": c.get("lastTransitionTime"),
        }
        for c in (status.get("conditions") or [])
    ]


def failed_condition_message(conditions: list[dict[str, Any]]) -> str | None:
    """False 인 조건의 message 를 "; " 로 잇는다. 없으면 None 이다. 순수 함수.

    컨트롤러가 Failed 를 내면서 message 를 비워 둘 때 이유를 조건에서 읽는다. 원본 conditions 와
    `condition_rows` 의 행이 같은 키를 쓰므로 어느 쪽이든 받는다."""
    return (
        "; ".join(
            c["message"] for c in conditions if c.get("status") == "False" and c.get("message")
        )
        or None
    )


def newest_template_pods(pods: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """가장 최근 파드 템플릿의 파드만. 순수 함수.

    롤아웃 중에는 옛 ReplicaSet 의 파드가 함께 온다. 그것이 죽어 가며 내는 CrashLoopBackOff 로
    새 파드를 판정하면 멀쩡한 롤아웃이 실패가 된다. `pod-template-hash` 로 묶어 가장 늦게 만들어진
    묶음만 남긴다. 라벨이 없는 파드는 한 묶음으로 본다."""
    groups: dict[str, list[dict[str, Any]]] = {}
    born: dict[str, str] = {}
    for pod in pods:
        meta = pod.get("metadata") or {}
        key = str(((meta.get("labels") or {}).get("pod-template-hash")) or "")
        groups.setdefault(key, []).append(pod)
        stamp = str(meta.get("creationTimestamp") or "")
        # RFC3339 는 사전순이 곧 시간순이다
        if stamp > born.get(key, ""):
            born[key] = stamp
    if not groups:
        return []
    newest = max(groups, key=lambda k: born.get(k, ""))
    return groups[newest]


def live_newest_pods(pods: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """가장 최근 템플릿의 파드 가운데 지워지는 중이 아닌 것. 순수 함수. 롤아웃 중이면 옛 파드가
    지워지는 중으로 남는다."""
    return [
        pod
        for pod in newest_template_pods(pods)
        if not (pod.get("metadata") or {}).get("deletionTimestamp")
    ]


def pod_waiting_reason(pods: list[dict[str, Any]]) -> tuple[str, str | None] | None:
    """파드 목록에서 못 뜨는 이유 하나를 고른다. 순수 함수. 없으면 None 이다.

    컨테이너와 initContainer 를 함께 본다. 스펙은 initContainer 로 주입하므로(D4) 이미지를 못 받는
    자리가 그쪽일 수 있다. 다시 뜨기를 기다리는 컨테이너(CrashLoopBackOff)는 마지막 종료의
    termination message 가 있으면 그것이 이유다. Agent 의 init 이 받기 실패의 `code: detail` 을
    거기 쓰고(D65), 런타임도 기동이 설정 오류로 실패하면 `code: message` 를 쓴다(T23). 지워지는
    중인 파드는 건너뛴다. 롤아웃 중이면 옛 파드가 그렇게 남는다."""
    for pod in live_newest_pods(pods):
        st = pod.get("status") or {}
        statuses = [
            *(st.get("initContainerStatuses") or []),
            *(st.get("containerStatuses") or []),
        ]
        for cs in statuses:
            waiting = ((cs or {}).get("state") or {}).get("waiting") or {}
            reason = waiting.get("reason")
            if reason in POD_FAILED_REASONS:
                terminated = ((cs or {}).get("lastState") or {}).get("terminated") or {}
                return str(reason), terminated.get("message") or waiting.get("message")
    return None


def unready_running_pod(pods: list[dict[str, Any]]) -> dict[str, Any] | None:
    """가장 최근 템플릿의 파드 가운데 컨테이너가 모두 돌고 있는데 준비되지 않은 것 하나. 순수 함수.

    의존(저장소·도구 서버)을 기다리는 Agent 런타임이 이렇게 남는다(T21). 이유는 런타임이 안다.
    다시 뜨기를 기다리는 파드(waiting)는 `pod_waiting_reason` 이 맡으므로 고르지 않는다. 지워지는
    중인 파드는 건너뛴다."""
    for pod in live_newest_pods(pods):
        statuses = [cs or {} for cs in (pod.get("status") or {}).get("containerStatuses") or []]
        running = bool(statuses) and all("running" in (cs.get("state") or {}) for cs in statuses)
        if running and not all(cs.get("ready") for cs in statuses):
            return pod
    return None


def with_unready_reason(status: dict[str, Any], reason: str | None) -> dict[str, Any]:
    """준비되지 않은 파드의 이유(T21)를 상태 message 에 얹는다. 순수 함수. message 가 없으면 이유가
    message 가 되고, 있으면 `; ` 로 뒤에 붙는다(Pending 은 대개 Available 조건의 문장이 이미
    있다)."""
    if not reason:
        return status
    message = status.get("message")
    return {**status, "message": f"{message}; {reason}" if message else reason}


def apply_pod_waiting(status: dict[str, Any], pods: list[dict[str, Any]]) -> dict[str, Any]:
    """컨트롤러가 낸 상태 위에 파드의 컨테이너 상태를 얹는다. 순수 함수 (D43 둘째).

    가장 최근 템플릿의 파드에 못 뜨는 이유가 있으면 `Pending` 은 phase 를 `Failed` 로 내리고
    message 에 reason 과 그 message 를 싣는다. `Rolling` 은 phase 를 두고 message 뒤에 그 이유를
    붙인다. 새 파드가 이미지를 못 받거나 반복해서 죽으면 롤아웃이 끝나지 않으므로 기다리는 까닭을
    보인다. Ready·Failed 는 컨트롤러가 이미 판정한 것이라 보지 않는다. 파드를 못 읽었으면(403·
    클러스터 흔들림) 빈 목록이 오고 상태는 그대로다."""
    phase = status.get("phase")
    if phase not in WAITING_PHASES:
        return status
    found = pod_waiting_reason(pods)
    if found is None:
        return status
    reason, message = found
    text = f"{reason}: {message}" if message else reason
    if phase == "Rolling":
        return with_unready_reason(status, text)
    return {**status, "phase": "Failed", "message": text}


class StatusView(Protocol):
    """상태 훅을 구현하는 협력자의 약속 (P72 2.3절). `fields` 와 `map` 은 반드시 있고, 어느 길로
    끝나든 마지막에 더할 것이 있으면 `finish` 도 둔다(Agent 의 Skill 경고와 phase 기록)."""

    async def fields(self, inst: Any, observed_at: datetime) -> dict[str, Any]: ...

    async def map(
        self,
        inst: Any,
        client: KubeClient,
        obj: dict[str, Any],
        out: dict[str, Any],
        observed_at: datetime,
        *,
        listing: bool,
    ) -> dict[str, Any]: ...


class InstanceStatusMixin:
    """상태 조회의 틀. `BaseInstanceService` 가 섞어 쓴다.

    훅과 도는 자리:
    - `_status_fields(inst, observed_at)`: 맨 앞. 응답의 모든 키와 읽기 전에 아는 경고
    - `terminating_states`: 이 상태면 클러스터를 읽지 않고 Terminating 이다
    - `_status_exposure(inst, client, out)`: 노출 상태를 읽는 자리(with_exposure 일 때)
    - `_status_map(inst, client, obj, out, observed_at, listing=)`: 주 객체를 읽은 뒤
    - `_status_finish(inst, out)`: 어느 길로 끝나든 마지막
    """

    tenancy: Any  # TenancyService
    cluster_service: Any  # ClusterService

    # 이 종류가 만드는 주 객체의 apiVersion 과 K8s 종류. 상태 조회가 이것을 읽고, 이름 충돌 문구가
    # 종류를 쓴다. 구체 서비스는 반드시 채운다
    k8s_api: str = ""
    k8s_kind: str = ""
    # 노출 앞단이 따로 있는가(MCP Server 의 MCPRemoteProxy 나 vMCP 앞단). 노출 상태가 Ingress 가
    # 가리키는 앞단을 본다
    front_proxy: bool = False
    # 이 상태면 클러스터를 읽지 않고 Terminating 으로 답한다. Agent 만 채운다
    terminating_states: frozenset[str] = frozenset()
    # 상태 훅을 구현하는 협력자 (P72 2.1·2.2절). 있으면 `fields`·`map`(그리고 `finish`)이 훅에
    # 답한다. MCP Server 처럼 없으면 서비스가 훅을 직접 채운다
    status_view: StatusView | None = None

    def _k8s(self, inst: Any) -> Any:
        raise NotImplementedError

    async def status(
        self, inst: Any, *, listing: bool = False, with_exposure: bool = True
    ) -> dict[str, Any] | None:
        """주 객체를 매 요청 읽어 화면용 dict 로 옮긴다. 예외를 던지지 않는다(error 필드로).
        draft 는 클러스터에 아무것도 없으므로 None 이다. listing 은 목록 조회다(Agent 만 쓴다).
        with_exposure 가 거짓이면 노출 상태(앞단·Ingress·TLS 조회)를 읽지 않고 `exposure` 는
        None 이다. phase 만 쓰는 허브 상태가 그렇게 부른다 (T55).

        읽지 못한 이유는 `코드: 설명` 한 줄이다. 클러스터 문서가 없으면 unknown_cluster, 연결할 수
        없으면 cluster_unreachable, 클러스터가 플랫폼 계정의 읽기를 거절하면 cluster_forbidden 과
        빠진 권한이다. 마지막은 사용자의 권한이 아니라 플랫폼 계정의 RBAC 문제다."""
        if inst.k8s is None or inst.cluster_id is None:
            return None
        k8s = inst.k8s
        observed_at = utcnow()
        out = await self._status_fields(inst, observed_at)
        if inst.state in self.terminating_states:
            out["phase"] = "Terminating"
            return await self._status_finish(inst, out)
        cluster = await self.tenancy.clusters.get(inst.cluster_id)
        if cluster is None:
            out["error"] = f"unknown_cluster: cluster {inst.cluster_id} missing"
            return await self._status_finish(inst, out)
        try:
            client = await run_in_threadpool(self.cluster_service.client_for, cluster)
            obj = await run_in_threadpool(
                client.get, self.k8s_api, self.k8s_kind, k8s.name, k8s.namespace
            )
        except ClusterUnreachable as e:
            out["error"] = f"cluster_unreachable: {e}"
            return await self._status_finish(inst, out)
        except AccessDenied as e:
            # 권한이 모자란 원격 클러스터 하나가 목록 전체를 500 으로 만들면 안 된다
            out["error"] = f"cluster_forbidden: {e.missing}"
            return await self._status_finish(inst, out)
        except Exception as e:  # noqa: BLE001 - 상태 조회는 예외를 던지지 않는다
            out["error"] = f"{type(e).__name__}: {e}"
            return await self._status_finish(inst, out)
        if with_exposure:
            await self._status_exposure(inst, client, out)
        if obj is None:
            out["phase"] = "Pending" if inst.state == "deploying" else "Missing"
            out["message"] = f"{self.k8s_kind} {k8s.namespace}/{k8s.name} not found"
            return await self._status_finish(inst, out)
        out = await self._status_map(inst, client, obj, out, observed_at, listing=listing)
        return await self._status_finish(inst, out)

    async def _status_fields(self, inst: Any, observed_at: datetime) -> dict[str, Any]:
        """status 응답의 모든 키와 클러스터를 읽기 전에 아는 경고. 상태 협력자(`status_view`)가
        있으면 그것이 답하고, 없으면 하위가 채운다."""
        if self.status_view is None:
            raise NotImplementedError
        return await self.status_view.fields(inst, observed_at)

    async def _status_exposure(
        self, inst: Any, client: KubeClient, out: dict[str, Any]
    ) -> ExposureReading | None:
        """노출 상태를 `exposure` 에 싣고 읽은 것을 돌려준다. 노출하지 않았으면 None 이다. 앞단의
        경고를 더하는 종류(MCP Server)가 이것을 넓힌다."""
        k8s = inst.k8s
        reading = await run_in_threadpool(
            read_exposure,
            client,
            k8s_name=k8s.name,
            namespace=k8s.namespace,
            exposure=inst.exposure,
            front_proxy=self.front_proxy,
        )
        out["exposure"] = reading.fields if reading is not None else None
        return reading

    async def _status_map(
        self,
        inst: Any,
        client: KubeClient,
        obj: dict[str, Any],
        out: dict[str, Any],
        observed_at: datetime,
        *,
        listing: bool,
    ) -> dict[str, Any]:
        """읽은 주 객체를 phase·message 와 종류의 칸으로 옮긴다. 상태 협력자가 있으면 그것이 답하고,
        없으면 하위가 채운다."""
        if self.status_view is None:
            raise NotImplementedError
        return await self.status_view.map(inst, client, obj, out, observed_at, listing=listing)

    async def _status_finish(self, inst: Any, out: dict[str, Any]) -> dict[str, Any]:
        """어느 길로 끝나든 마지막에 더할 것. 상태 협력자가 `finish` 를 가지면 그것이고, 기본은
        그대로다."""
        finish = getattr(self.status_view, "finish", None)
        return out if finish is None else await finish(inst, out)

    async def _with_pod_waiting(
        self, inst: Any, client: KubeClient, out: dict[str, Any], *, when: bool | None = None
    ) -> tuple[dict[str, Any], list[dict[str, Any]]]:
        """파드의 대기 이유를 싣는다. (상태, 읽은 파드). 컨트롤러 phase 는 이미지를 못 받는
        파드도 Pending 으로 내고 롤아웃은 새 파드가 못 떠도 Rolling 이므로 기본은 그 둘일 때
        파드를 본다 (D43 둘째). when 을 주면 그것이 읽을지를 정한다(Agent 는 상세에서 준비되지
        않은 phase 도 본다). 읽지 않았으면 빈 목록이다."""
        if not (out["phase"] in WAITING_PHASES if when is None else when):
            return out, []
        k8s = self._k8s(inst)
        pods = await self.instance_pods(client, instance_id=inst.id, namespace=k8s.namespace)
        return apply_pod_waiting(out, pods), pods

    async def instance_pods(
        self, client: KubeClient, *, instance_id: str, namespace: str
    ) -> list[dict[str, Any]]:
        """이 인스턴스의 파드. 세 리소스의 파드가 모두 `daegam.devops.ai.kr/id` 를 달고 있다.

        상태 조회가 컨트롤러 phase 너머를 보려고 부른다. 못 읽으면 빈 목록이고 phase 는 컨트롤러가
        낸 그대로 남는다. 파드 목록 권한이 없는 원격 클러스터(403 AccessDenied)에서도 상태 조회
        자체는 살아야 한다."""
        try:
            return await run_in_threadpool(
                client.list, "v1", "Pod", namespace, {LABEL_ID: instance_id}
            )
        except AccessDenied as e:
            logger.debug("instance %s: pods forbidden: %s", instance_id, e)
            return []
        except Exception as e:  # noqa: BLE001 - 클러스터가 흔들려도 상태를 가리면 안 된다
            logger.debug("instance %s: cannot list pods: %s", instance_id, e)
            return []
