"""세 인스턴스 서비스(MCP·Gateway·Agent)가 공통 부분을 다시 복사하지 않게 막는다.

같은 일을 하는 메서드가 세 곳에 복사돼 있었고 이미 갈라지기 시작했다(`_validate_exposure` 의
분기가 서로 달랐다). 글자 그대로 같던 것은 `BaseInstanceService` 로 모았고, 이 테스트는
누가 다시 복사해 넣으면 알려 준다. `test_oidc_copy.py`·`test_toolname_copy.py` 와 같은 취지다.

리소스마다 본문이 진짜 다른 것(`_provision`·`render`·`_delete_steps` 등)은 대상이 아니다.
"""

from __future__ import annotations

import ast
import asyncio
import hashlib
import inspect
from pathlib import Path

import pytest

from daegam.errors import ServiceError
from daegam.k8s.client import ClusterUnreachable
from daegam.k8s.templates import RenderError
from daegam.services.agents import AgentService
from daegam.services.gateways import GatewayService
from daegam.services.instance_base import KEPT_ON_UNDEPLOY, BaseInstanceService
from daegam.services.instance_status import (
    InstanceStatusMixin,
    apply_pod_waiting,
    unready_running_pod,
)
from daegam.services.mcp_servers import MCPServerService
from daegam.store.agents import Agent
from daegam.store.base import utcnow
from daegam.store.gateways import Gateway
from daegam.store.mcp_servers import MCPServer
from tests.helpers.consts import REPO
from tests.helpers.docs import gateway_doc
from tests.helpers.source import parse_python


class _Err(ServiceError):
    pass


SERVICES = {
    "mcp": REPO / "platform" / "daegam" / "services" / "mcp_servers.py",
    "gateway": REPO / "platform" / "daegam" / "services" / "gateways.py",
    "agent": REPO / "platform" / "daegam" / "services" / "agents.py",
}


def _method_bodies(path: Path) -> dict[str, str]:
    """클래스 메서드의 본문 해시. docstring 은 빼고 본다."""
    out: dict[str, str] = {}
    for node in ast.walk(parse_python(path)):
        if not isinstance(node, ast.ClassDef):
            continue
        for m in node.body:
            if not isinstance(m, ast.FunctionDef | ast.AsyncFunctionDef):
                continue
            body = m.body
            if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant):
                body = body[1:]
            digest = hashlib.sha256("\n".join(ast.dump(s) for s in body).encode()).hexdigest()
            out[m.name] = digest
    return out


BASE = REPO / "platform" / "daegam" / "services" / "instance_base.py"


def test_no_byte_identical_method_is_copied_between_services() -> None:
    bodies = {name: _method_bodies(path) for name, path in SERVICES.items()}
    copied: list[str] = []
    for method in sorted({m for b in bodies.values() for m in b}):
        # 같은 본문끼리 묶는다. 셋 중 둘만 같아도 복사다(나머지 하나가 다르다고 봐주면 안 된다)
        groups: dict[str, list[str]] = {}
        for name, b in bodies.items():
            if method in b:
                groups.setdefault(b[method], []).append(name)
        for same in groups.values():
            if len(same) > 1:
                copied.append(f"{method}: {sorted(same)} 가 서로 같다")
    # 기반에 이미 있는 것을 서비스가 그대로 다시 정의하는 것도 복사다 (한 곳만 고치면 갈라진다)
    base = _method_bodies(BASE)
    for name, methods in bodies.items():
        for method, digest in sorted(methods.items()):
            if base.get(method) == digest:
                copied.append(f"{method}: {name} 이 기반과 같은 본문을 다시 정의한다")
    assert not copied, (
        "글자 그대로 같은 메서드가 있다. 공통이면 BaseInstanceService 로 올리고 하위는 지운다:\n  "
        + "\n  ".join(copied)
    )


def test_services_inherit_the_shared_base() -> None:
    """공통 부분은 상속으로 온다. 다시 각자 정의하면 위 테스트가 잡는다."""
    assert issubclass(GatewayService, BaseInstanceService)
    assert issubclass(MCPServerService, BaseInstanceService)
    assert issubclass(AgentService, BaseInstanceService)
    for shared in (
        "has_instances_on_cluster",
        "set_overrides",
        "drift_reset",
        "drift_absorb",
        "token_auth",
    ):
        assert hasattr(BaseInstanceService, shared), f"{shared} 가 기반에 없다"


SHARED_GUARDS = (
    "_require",
    "_transition",
    "_save_definition",
    "_save_deployment",
    "_record_error",
    "_k8s",
    "_pinned",
)


def test_state_guards_and_saves_live_only_in_the_base() -> None:
    """상태 가드와 조건부 저장은 기반 한 곳에만 있다. 위의 바이트 비교는 명사만 바꾼 사본을
    잡지 못한다. 세 벌이 명사·오류 클래스만 다른 채 갈라져 updated_at 을 찍는 자리와
    revision_conflict 문구가 서로 달랐다. 참조 검사는 삭제 가드 훅(`_guard_delete`)이 draft 갈래
    앞에서 하므로 `delete_draft` 를 덮어쓰는 하위는 없다."""
    for cls in (MCPServerService, GatewayService, AgentService):
        again = [m for m in (*SHARED_GUARDS, "delete_draft") if m in vars(cls)]
        assert again == [], f"{cls.__name__} 가 기반의 것을 다시 정의한다: {again}"
    for name in SHARED_GUARDS:
        assert name in vars(BaseInstanceService), f"{name} 가 기반에 없다"


# 배포·해제·삭제의 골격 (P71 2.1절). Agent 는 provision 의 token 인자와 delete 의 회수 수를 넘기는
# 한 줄 껍데기만 둔다
LIFECYCLE = ("provision", "undeploy", "delete", "_delete", "_clear_deployment")
THIN_WRAPPERS = {AgentService: {"provision", "delete"}}


def test_lifecycle_skeletons_live_only_in_the_base() -> None:
    """배포·해제·삭제의 골격은 기반 한 곳에만 있다. 세 벌이 명사만 다른 채 갈라지던 자리다.
    MRO 로 풀어 본다. 믹스인이나 중간 클래스가 기반의 것을 가려도 잡는다. 상태의 틀은 기반이 섞어
    쓰는 `InstanceStatusMixin` 의 것이다 (P71 2.4절)."""
    for name in LIFECYCLE:
        assert name in vars(BaseInstanceService), f"{name} 가 기반에 없다"
    assert issubclass(BaseInstanceService, InstanceStatusMixin)
    for cls in (MCPServerService, GatewayService, AgentService):
        assert cls.status is InstanceStatusMixin.status, f"{cls.__name__} 가 상태의 틀을 가린다"
        # 상태 조회가 읽을 주 객체를 구체 서비스마다 반드시 적는다
        assert cls.k8s_api and cls.k8s_kind, f"{cls.__name__} 가 k8s_api·k8s_kind 를 비웠다"
    for cls in (MCPServerService, GatewayService, AgentService):
        thin = THIN_WRAPPERS.get(cls, set())
        shadowed = {m for m in LIFECYCLE if getattr(cls, m) is not getattr(BaseInstanceService, m)}
        assert shadowed == thin, f"{cls.__name__} 가 골격을 다시 정의한다: {shadowed - thin}"


@pytest.mark.parametrize("cls", [MCPServerService, GatewayService, AgentService])
def test_undeploy_clears_every_deployment_field_it_does_not_keep(cls) -> None:
    """해제는 남기는 것(`KEPT_ON_UNDEPLOY`, `kept_settings`) 밖의 배포 쪽 필드를 모두 모델 기본값
    으로 되돌린다. 나중에 더한 배포 쪽 필드도 남긴다고 적지 않으면 비워진다."""
    model = {
        MCPServerService: MCPServer,
        GatewayService: Gateway,
        AgentService: Agent,
    }[cls]
    assert cls.deployment_fields, f"{cls.__name__} 가 deployment_fields 를 채우지 않았다"
    cleared = cls.deployment_fields - KEPT_ON_UNDEPLOY - cls.kept_settings
    marks = {name: object() for name in cls.deployment_fields}
    doc = model.model_construct(**marks)
    BaseInstanceService._clear_deployment(cls.__new__(cls), doc)
    for name in cleared:
        want = model.model_fields[name].get_default(call_default_factory=True)
        assert getattr(doc, name) == want, f"{cls.__name__}.{name} 가 비워지지 않았다"
    for name in cls.deployment_fields - cleared:
        assert getattr(doc, name) is marks[name], f"{cls.__name__}.{name} 는 남아야 한다"
    # 오늘 남기는 것과 같다: 상태·시각·재적용 칸과 각자의 설정
    kept = cls.deployment_fields - cleared
    assert {"state", "exposure", "last_spec_change", "updated_at"} <= kept


class _CondStore:
    """조건부 저장이 부르는 자리만 흉내 낸다. ok 가 거짓이면 0건(조건 불일치)이다. latest 는 어긋난
    뒤 다시 읽을 때 돌려줄 문서다(없으면 그사이 지워진 것)."""

    def __init__(self, ok: bool) -> None:
        self.ok = ok
        self.calls: list[tuple[str, dict, dict]] = []
        self.latest: object = None

    async def get(self, doc_id: str, tenant: str | None = None) -> object:
        return self.latest

    async def set_fields(self, doc_id: str, fields: dict, **cond: object) -> bool:
        self.calls.append((doc_id, fields, cond))
        return self.ok

    async def transition(
        self, doc: object, *, expect: object, extra: object = (), push: object = None
    ) -> bool:
        self.calls.append((doc.id, {}, {"expect": expect}))  # type: ignore[attr-defined]
        return self.ok


class _GatewayLike(BaseInstanceService):  # type: ignore[type-arg]
    error_cls = _Err
    profile_kind = "gateway"
    definition_fields = frozenset({"spec", "revision", "updated_at"})

    def __init__(self, *, ok: bool) -> None:
        self.instances: _CondStore = _CondStore(ok)  # type: ignore[assignment]


def test_shared_saves_stamp_updated_at_and_write_only_their_side() -> None:
    """세 저장이 모두 updated_at 을 찍고 자기 쪽 필드만 조건과 함께 쓴다 (D42)."""
    svc = _GatewayLike(ok=True)
    doc = gateway_doc(name="calc", state="deployed")
    doc.updated_at = utcnow().replace(year=2000)
    asyncio.run(svc._save_definition(doc, expect_revision=1, action="save"))
    _id, fields, cond = svc.instances.calls[-1]
    assert set(fields) == {"spec", "revision", "updated_at"} and cond == {"expect_revision": 1}
    assert doc.updated_at.year > 2000

    doc.updated_at = utcnow().replace(year=2000)
    asyncio.run(
        svc._save_deployment(doc, {"exposure"}, expect_state={"deployed"}, action="edit exposure")
    )
    _id, fields, cond = svc.instances.calls[-1]
    assert set(fields) == {"exposure", "updated_at"} and cond == {"expect_state": {"deployed"}}
    assert doc.updated_at.year > 2000

    # 배포 경로의 실패만 failed 로 옮기고, 옮기기 전 상태를 조건으로 건다
    deploying = gateway_doc(name="calc", state="deploying")
    asyncio.run(svc._record_error(deploying, "k8s_error: boom"))
    _id, fields, cond = svc.instances.calls[-1]
    assert fields["state"] == "failed" and fields["last_error"] == "k8s_error: boom"
    assert cond == {"expect_state": {"deploying"}}


def test_shared_guards_name_the_resource_in_one_wording() -> None:
    """오류 문구의 명사는 profile_kind 가 채운다. 세 리소스가 같은 문장을 쓴다."""
    svc = _GatewayLike(ok=False)
    doc = gateway_doc(name="calc", state="deployed")
    # 조건부 저장이 어긋나면 다시 읽는다. 남아 있으면 지금 번호를 싣는다 (P48 6.7절)
    svc.instances.latest = gateway_doc(name="calc", state="deployed", revision=4)
    with pytest.raises(_Err) as ei:
        asyncio.run(svc._save_definition(doc, expect_revision=1, action="save"))
    assert (ei.value.status, ei.value.code) == (409, "revision_conflict")
    assert ei.value.detail == "gateway calc was saved by another request; reload and retry save"
    assert ei.value.extra == {"revision": 4}
    # 그사이 지워졌거나 삭제 중이면 그 답이다 (`save_missed`)
    svc.instances.latest = None
    with pytest.raises(_Err) as ei:
        asyncio.run(svc._save_definition(doc, expect_revision=1, action="save"))
    assert (ei.value.status, ei.value.code, ei.value.detail) == (
        404,
        "not_found",
        "gateway calc not found",
    )
    svc.instances.latest = gateway_doc(name="calc", state="deleting", revision=4)
    with pytest.raises(_Err) as ei:
        asyncio.run(svc._save_definition(doc, expect_revision=1, action="save"))
    assert (ei.value.status, ei.value.code) == (409, "deleting")
    with pytest.raises(_Err) as ei:
        asyncio.run(svc._transition(doc, {"deployed"}, action="redeploy"))
    assert (ei.value.code, ei.value.detail) == (
        "state_conflict",
        "gateway calc changed state; retry redeploy",
    )
    with pytest.raises(_Err) as ei:
        svc._require(gateway_doc(name="calc", state="deploying"), {"deployed"}, action="publish")
    assert (ei.value.code, ei.value.detail) == (
        "in_progress",
        "gateway calc is deploying; cannot publish",
    )
    with pytest.raises(_Err) as ei:
        svc._require(gateway_doc(name="calc", state="draft"), {"deployed"}, action="publish")
    assert ei.value.code == "not_deployed"
    with pytest.raises(_Err) as ei:
        svc._k8s(gateway_doc(name="calc", state="draft"))
    assert (ei.value.code, ei.value.detail) == ("not_deployed", "gateway calc is not deployed")


class _Doc:
    """record_failure 가 건드리는 필드만 가진 인스턴스 문서."""

    def __init__(self) -> None:
        self.last_error: str | None = None
        self.updated_at = utcnow()


class _Store:
    def __init__(self) -> None:
        self.saved: list[_Doc] = []

    async def update(self, doc: _Doc) -> None:
        self.saved.append(doc)


class _Svc(BaseInstanceService):  # type: ignore[type-arg]
    error_cls = _Err

    def __init__(self) -> None:
        self.instances: _Store = _Store()  # type: ignore[assignment]

    async def _record_error(self, inst: _Doc, message: str) -> None:
        """합친 리소스 셋은 문서 전체를 쓰는 저장이 없어 기반이 자리만 잡고 세 서비스가 각자
        자기 상태 조건으로 쓴다 (D42). 여기서는 그 셋이 공통으로 하는 일만 흉내 낸다."""
        inst.last_error = message
        inst.updated_at = utcnow()
        await self.instances.update(inst)


def _record_failure(svc: _Svc, doc: _Doc, raised: Exception) -> None:
    async def body() -> None:
        async with svc.record_failure(doc, code_only=(RenderError,)):
            raise raised

    asyncio.run(body())


@pytest.mark.parametrize(
    ("raised", "status", "code"),
    [
        (_Err(409, "state_conflict", "no"), 409, "state_conflict"),
        (ClusterUnreachable("no route"), 502, "cluster_unreachable"),
        (RenderError("redis_required", "needs redis"), 422, "redis_required"),
        (RuntimeError("boom"), 502, "k8s_error"),
    ],
)
def test_record_failure_maps_every_arm(raised: Exception, status: int, code: str) -> None:
    """실패의 (status, code) 대응과 last_error 형식이 계약이다. 세 서비스가 이 한 자리를 쓴다.

    안 감싸면 라우터가 못 잡아 500 이 나가고, `last_error` 가 비어 화면에 이유가 없으며,
    reconcile 이 60초마다 스택 트레이스를 찍는다.
    """
    svc = _Svc()
    doc = _Doc()
    with pytest.raises(_Err) as ei:
        _record_failure(svc, doc, raised)
    assert (ei.value.status, ei.value.code) == (status, code)
    assert doc.last_error is not None and doc.last_error.startswith(f"{code}: ")
    assert svc.instances.saved == [doc]


def test_all_three_services_route_failures_through_record_failure() -> None:
    """provision·해제·삭제는 실패 기록을 손으로 쓰지 않고 기반의 record_failure 로 넘긴다.

    손으로 쓰면 위 대응표에서 한 팔씩 빠지고(실제로 MCP 의 redeploy 에 k8s_error 팔이
    없었다) 세 리소스의 last_error 형식이 갈라진다. 골격이 기반 한 곳에만 있는 것은
    `test_lifecycle_skeletons_live_only_in_the_base` 가 보므로 여기서는 기반의 골격을 본다.
    """
    methods = {
        m.name: ast.unparse(m)
        for node in ast.walk(parse_python(BASE))
        if isinstance(node, ast.ClassDef) and node.name == "BaseInstanceService"
        for m in node.body
        if isinstance(m, ast.FunctionDef | ast.AsyncFunctionDef)
    }
    # 골격은 클레임을 쥔 뒤의 본문(P66)에서 클러스터 일을 한다
    skeleton = ("_provision_claimed", "_undeploy_claimed", "_delete_claimed")
    missing = [m for m in skeleton if "record_failure" not in methods[m]]
    assert not missing, f"실패 기록을 기반으로 넘기지 않는 곳: {missing}"
    # 세 서비스가 MRO 로 그 골격에 닿는다. Agent 의 껍데기는 기반의 것을 그대로 부른다
    for cls in (MCPServerService, GatewayService, AgentService):
        for name in skeleton:
            resolved = getattr(cls, name)
            if resolved is getattr(BaseInstanceService, name):
                continue
            assert name in THIN_WRAPPERS.get(cls, set()), f"{cls.__name__}.{name} 가 골격을 가린다"
            source = inspect.getsource(resolved)
            assert f"super().{name}(" in source or "self._delete(" in source, (
                f"{cls.__name__}.{name} 가 기반의 골격을 부르지 않는다"
            )


# -- 파드 컨테이너 상태 (D43 둘째) -----------------------------------------------------


def _pod(
    reason: str | None, *, message: str | None = None, init: bool = False, **meta: object
) -> dict:
    """컨테이너 하나짜리 파드. reason 이 None 이면 돌고 있는 것이다."""
    state = {"waiting": {"reason": reason, "message": message}} if reason else {"running": {}}
    key = "initContainerStatuses" if init else "containerStatuses"
    return {"metadata": dict(meta), "status": {key: [{"name": "mcp", "state": state}]}}


@pytest.mark.parametrize(
    "reason",
    ["ImagePullBackOff", "ErrImagePull", "CrashLoopBackOff", "CreateContainerConfigError"],
)
def test_pod_waiting_turns_pending_into_failed(reason: str) -> None:
    """뜰 수 없는 파드는 Failed 다. 컨트롤러 phase 는 이 넷을 Pending 으로 낸다."""
    out = apply_pod_waiting(
        {"phase": "Pending", "message": None}, [_pod(reason, message="back-off pulling")]
    )
    assert out["phase"] == "Failed"
    assert out["message"] == f"{reason}: back-off pulling"
    # 컨테이너의 message 가 없으면 reason 만 싣는다
    assert apply_pod_waiting({"phase": "Pending"}, [_pod(reason)])["message"] == reason
    # 스펙은 initContainer 로 주입한다(D4). 이미지를 못 받는 자리가 그쪽일 수 있다
    assert apply_pod_waiting({"phase": "Pending"}, [_pod(reason, init=True)])["phase"] == "Failed"


def test_pod_waiting_looks_at_pending_and_rolling() -> None:
    """Pending 은 Failed 가 되고, Rolling 은 phase 를 두고 기다리는 까닭을 message 에 싣는다.
    나머지는 컨트롤러가 이미 판정한 것이다."""
    for phase in ("Ready", "Failed", "Missing", "Terminating"):
        before = {"phase": phase, "message": None}
        assert apply_pod_waiting(before, [_pod("ImagePullBackOff")]) == before
    rolling = {"phase": "Rolling", "message": "vMCP Deployment not yet rolled out to revision 2"}
    out = apply_pod_waiting(rolling, [_pod("CrashLoopBackOff", message="exit 1")])
    assert out == {**rolling, "message": f"{rolling['message']}; CrashLoopBackOff: exit 1"}
    assert apply_pod_waiting(rolling, [_pod(None)]) == rolling
    pending = {"phase": "Pending", "message": None}
    assert apply_pod_waiting(pending, [_pod(None)]) == pending
    # 파드를 못 읽었을 때(403·클러스터 흔들림)도 phase 는 그대로다
    assert apply_pod_waiting(pending, []) == pending


def test_pod_waiting_skips_pods_on_their_way_out() -> None:
    """지워지는 중인 파드로 멀쩡한 배포를 실패로 뒤집지 않는다."""
    dying = _pod("CrashLoopBackOff", deletionTimestamp="2026-09-16T00:00:00Z")
    assert apply_pod_waiting({"phase": "Pending"}, [dying])["phase"] == "Pending"


def test_pod_waiting_ignores_pods_of_an_older_template() -> None:
    """옛 ReplicaSet 파드의 CrashLoopBackOff 가 새 롤아웃을 Failed 로 만들지 않는다."""
    old = _pod(
        "CrashLoopBackOff",
        creationTimestamp="2026-09-16T00:00:00Z",
        labels={"pod-template-hash": "aaa"},
    )
    new = _pod(
        None,
        creationTimestamp="2026-09-16T01:00:00Z",
        labels={"pod-template-hash": "bbb"},
    )
    assert apply_pod_waiting({"phase": "Pending"}, [old, new])["phase"] == "Pending"
    # 반대로 새 템플릿 파드가 못 뜨면 옛 파드가 멀쩡해도 Failed 다
    new["status"]["containerStatuses"][0]["state"] = {"waiting": {"reason": "ErrImagePull"}}
    assert apply_pod_waiting({"phase": "Pending"}, [old, new])["phase"] == "Failed"


def test_pod_waiting_ignores_reasons_that_pass_on_their_own() -> None:
    """ContainerCreating·PodInitializing 은 기다리면 낫는다."""
    for reason in ("ContainerCreating", "PodInitializing"):
        assert apply_pod_waiting({"phase": "Pending"}, [_pod(reason)])["phase"] == "Pending"


def _running(*, ready: bool, **meta: object) -> dict:
    """컨테이너 하나가 돌고 있는 파드. ready 가 준비 검사 결과다."""
    pod = _pod(None, **meta)
    pod["status"]["containerStatuses"][0]["ready"] = ready
    return pod


def test_unready_running_pod_picks_a_running_pod_that_is_not_ready() -> None:
    """돌고 있는데 준비되지 않은 최신 파드가 의존을 기다리는 런타임이다(T21). 준비된 파드, 다시
    뜨기를 기다리는 파드(waiting), 지워지는 중인 파드, 옛 템플릿의 파드는 고르지 않는다."""
    waiting_one = _running(ready=False)
    assert unready_running_pod([waiting_one]) is waiting_one
    assert unready_running_pod([_running(ready=True)]) is None
    assert unready_running_pod([_pod("CrashLoopBackOff")]) is None
    assert (
        unready_running_pod([_running(ready=False, deletionTimestamp="2026-10-02T00:00:00Z")])
        is None
    )
    assert unready_running_pod([{"metadata": {}, "status": {}}]) is None
    old = _running(
        ready=False,
        labels={"pod-template-hash": "old"},
        creationTimestamp="2026-10-01T00:00:00Z",
    )
    new = _running(
        ready=True, labels={"pod-template-hash": "new"}, creationTimestamp="2026-10-02T00:00:00Z"
    )
    assert unready_running_pod([old, new]) is None


def test_deploy_prologue_lives_in_the_base() -> None:
    """배포의 앞부분(대상 클러스터·네임스페이스, 이름 충돌, 쓸 수 있는 클러스터 확인)은 기반 한 곳에
    있다 (P71 2.2절). 테넌시 오류를 종류의 클래스로 다시 싸는 자리도 없다. record_failure 가 서비스
    오류를 그대로 지나보내고 오류의 모양은 하나이기 때문이다(D30). 문자열이 아니라 AST 로 본다."""
    calls = {"check_namespace", "live_by_k8s_name", "require_tenant_secrets"}
    leftovers: list[str] = []
    for name, path in SERVICES.items():
        for node in ast.walk(parse_python(path)):
            if isinstance(node, ast.Call):
                fn = node.func
                called = fn.attr if isinstance(fn, ast.Attribute) else getattr(fn, "id", None)
                if called in calls:
                    leftovers.append(f"{name}: {called}()")
            elif isinstance(node, ast.ExceptHandler) and "TenancyError" in ast.unparse(
                node.type or ast.Constant(None)
            ):
                leftovers.append(f"{name}: except TenancyError")
            elif isinstance(node, ast.Name) and node.id == "TenancyError":
                leftovers.append(f"{name}: TenancyError")
    assert not leftovers, f"기반에 둔 배포 앞부분이 서비스에 남았다: {sorted(set(leftovers))}"


def test_instance_stores_share_one_set_of_queries() -> None:
    """세 스토어가 함께 쓰는 질의는 `MongoInstanceStore` 한 곳에 있다 (P71 2.3절). MRO 로 풀어
    하위가 공통 질의를 다시 정의하지 않았는지 보고, 하위의 메서드 가운데 본문이 공통 것과 같은
    것도 없는지 본다. 셋이 모델 클래스만 바꿔 줄마다 같게 갖던 자리다."""
    from daegam.store.agents import MongoAgentStore
    from daegam.store.gateways import MongoGatewayStore
    from daegam.store.instances import MongoInstanceStore
    from daegam.store.mcp_servers import MongoMCPServerStore

    shared = {
        name
        for name, value in vars(MongoInstanceStore).items()
        if callable(value) and not name.startswith("__") and name != "init_schema"
    }
    generic = _method_bodies(REPO / "platform" / "daegam" / "store" / "instances.py")
    for cls, path in (
        (MongoMCPServerStore, "mcp_servers.py"),
        (MongoGatewayStore, "gateways.py"),
        (MongoAgentStore, "agents.py"),
    ):
        assert issubclass(cls, MongoInstanceStore)
        shadowed = {n for n in shared if getattr(cls, n) is not getattr(MongoInstanceStore, n)}
        assert not shadowed, f"{cls.__name__} 가 공통 질의를 다시 정의한다: {shadowed}"
        # 이름과 상관없이 본문으로 견준다. 이름만 바꾼 사본도 잡힌다. 다만 공통 본문은
        # `self.model` 로 문서를 만들므로, 모델 클래스를 직접 적은 사본(`Agent.from_doc`)은 본문이
        # 달라 여기서 잡히지 않는다. 그런 사본은 리뷰가 본다
        own = _method_bodies(REPO / "platform" / "daegam" / "store" / path)
        shared_bodies = set(generic.values())
        copied = {m for m, body in own.items() if body in shared_bodies}
        assert not copied, f"{cls.__name__} 가 공통 질의를 그대로 베꼈다: {copied}"
        assert cls.model and cls.collection and cls.transition_fields
