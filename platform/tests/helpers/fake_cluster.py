"""테스트용 인메모리 클러스터. apply/get/delete/list 에 status 주입과 오류 주입을 지원한다.
데이터 플레인 Service HTTP(D21)는 `service_handler` 로 응답을 꾸미고 `service_calls` 에 남는다.
`service_app` 에 ASGI 앱을 주면 `service_route().http_client()` 와 (핸들러가 없을 때의)
`service_request` 가 그 앱을 부른다. 에이전트 런타임처럼 스트리밍 응답을 흉내 낼 때 쓴다.
적용한 객체에는 API 서버처럼 플랫폼 field manager 의 Apply 기록(managedFields)을 남긴다."""

from __future__ import annotations

import base64
import copy
import json as jsonlib
from collections.abc import Callable
from typing import Any

import httpx2

from daegam.k8s.client import (
    FIELD_MANAGER,
    RESTARTED_AT,
    SERVICE_REQUEST_TIMEOUT,
    AccessDenied,
    ClusterUnreachable,
    FieldValidation,
    ServiceResponse,
    ServiceRoute,
    ServiceVia,
    decode_secret_data,
    encode_secret_data,
    send_service_request,
)
from daegam.k8s.preflight import (
    RBAC_API,
    SANDBOX_POD_VERBS,
    SANDBOX_PRIORITY_CLASS,
    SECRET_VERBS,
    TENANT_OBJECTS_CLUSTER_ROLE,
    TOOLHIVE_GV,
)
from tests.helpers.loop_guard import guard_methods

# 지금의 RBAC 파일에서 ClusterRole 이 아니라 네임스페이스마다의 RoleBinding
# daegam-tenant-objects 로만 받는 리소스 (P63)
BINDING_ONLY_RESOURCES = frozenset({"secrets", "mcpauthzconfigs", "serviceaccounts"})

Key = tuple[str, str, str | None, str]  # (apiVersion, kind, namespace, name)

# (method, namespace, service, port, path, json) -> (status, body) 또는 (status, body, headers)
ServiceHandler = Callable[[str, str, str, int, str, Any], tuple[Any, ...]]


def _resource_name(kind: str) -> str:
    """Kind → 리소스 이름 (복수형 소문자). IngressClass → ingressclasses, Secret → secrets,
    NetworkPolicy → networkpolicies, Gateway → gateways."""
    k = kind.lower()
    if k.endswith("s"):
        return k + "es"
    if k.endswith("y") and k[-2:-1] not in "aeiou":
        return k[:-1] + "ies"
    return k + "s"


def _key(obj: dict[str, Any]) -> Key:
    meta = obj.get("metadata") or {}
    return (obj["apiVersion"], obj["kind"], meta.get("namespace"), meta["name"])


# 진짜 클라이언트에서 API 서버로 가는 동기 메서드. 루프 위에서 부르면 거부한다 (P69). I/O 가 없는
# service_route, async 인 service_request, 대역의 준비·조회 도우미는 빠진다
_BLOCKING_METHODS = (
    "apply",
    "create",
    "dry_run_apply",
    "get",
    "delete",
    "rollout_restart",
    "scale_deployment",
    "drop_secret_keys",
    "list",
    "list_metadata",
    "version",
    "can_i",
    "can_user",
    "whoami",
    "discover",
    "pod_log",
)


def operator_image(version: str) -> str:
    """그 ToolHive 판의 오퍼레이터 이미지. preflight 가 태그에서 판을 읽는다 (P50 2절)."""
    return f"ghcr.io/stacklok/toolhive/operator:v{version}"


class _NoDataPlane(httpx2.AsyncBaseTransport):
    """`service_app` 이 없는 가짜 클러스터의 데이터 플레인이다. Service 이름
    (`*.svc.cluster.local`)은 풀지 않고 곧 연결 실패다. 실제 네트워크로 가면 macOS 는 `.local`
    이름을 mDNS 로 5초 동안 찾은 뒤에야 실패한다. 파드 IP 처럼 그 밖의 주소(`bearer_pod_client`)는
    실제 네트워크로 간다."""

    def __init__(self) -> None:
        self._network = httpx2.AsyncHTTPTransport()

    async def handle_async_request(self, request: httpx2.Request) -> httpx2.Response:
        if request.url.host.endswith(".svc.cluster.local"):
            raise httpx2.ConnectError("fake cluster has no data plane", request=request)
        return await self._network.handle_async_request(request)

    async def aclose(self) -> None:
        await self._network.aclose()


@guard_methods(_BLOCKING_METHODS)
class FakeClusterClient:
    def __init__(
        self,
        *,
        k8s_version: str = "v1.34.0-fake",
        toolhive_resources: list[str] | None = None,
        operator_image: str | None = operator_image("0.53.0"),
        operator_available: bool = True,
        denied: set[tuple[str, ...]] | None = None,  # (group, resource, verb[, name])
        unreachable: bool = False,
        # 예: "calico-node" 를 주면 kube-system DaemonSet 을 심는다
        policy_controller: str | None = None,
        identity: str = "system:serviceaccount:daegam-system:daegam-platform",
        # deploy/*/rbac.yaml 이 까는 ClusterRole daegam-tenant-objects 를 심는다 (P50 2절). 옛 RBAC
        # 로 등록한 클러스터를 흉내 낼 때 False 로 둔다. 규칙은 preflight 가 읽는 Secret 규칙만이다
        # (P63). Secret 동사가 없는 P50 의 역할은 테스트가 rules 를 비워 흉내 낸다
        tenant_objects_role: bool = True,
        # 플랫폼 계정이 클러스터 전체에서 Secret 을 다루는가. 지금의 RBAC 파일은 주지 않으므로
        # 기본은 거짓이고, 옛 RBAC 파일(rc3·rc4)의 클러스터를 흉내 낼 때 참으로 둔다 (P63)
        cluster_wide_secrets: bool = False,
        # P40 의 RBAC 파일이 까는 것: daegam-tenant-objects 의 파드 create·delete 와 PriorityClass
        # daegam-sandbox. 그 파일을 다시 적용하지 않은 클러스터를 흉내 낼 때 False 로 둔다
        sandbox_rights: bool = True,
    ):
        self.objects: dict[Key, dict[str, Any]] = {}
        # 참이면 적용한 Secret 의 stringData 를 쿠버네티스처럼 data(base64)로 옮긴다. 많은 테스트가
        # 렌더한 stringData 를 그대로 읽으므로 기본은 거짓이다
        self.secret_string_data_to_data = False
        self.cluster_wide_secrets = cluster_wide_secrets
        # 밖에서 지운 네임스페이스 (drop_namespace). daegam-tenant-objects 로만 받는 리소스는
        # 그 네임스페이스의 RoleBinding 과 함께 권한도 사라졌으므로 쓰기와 목록은 403 이고,
        # 읽기·지우기는 클라이언트처럼 없음으로 답한다. ClusterRole 이 주는 다른 리소스는 비어
        # 있을 뿐이다 (P63)
        self.gone_namespaces: set[str] = set()
        self.applied: list[dict[str, Any]] = []
        self.deleted: list[Key] = []
        # rollout_restart 로 다시 띄운 Deployment (namespace, name)
        self.restarts: list[tuple[str, str]] = []
        # scale_deployment 의 (namespace, name, replicas). 레플리카 패치의 차례를 본다 (P44 9절)
        self.scales: list[tuple[str, str, int]] = []
        self.pod_logs: dict[tuple[str, str], list[tuple[Any, str]]] = {}
        self.log_reads: list[tuple[str, str, int | None]] = []
        self.k8s_version = k8s_version
        self.toolhive_resources = (
            toolhive_resources
            if toolhive_resources is not None
            else [
                "mcpservers",
                "mcpgroups",
                "virtualmcpservers",
                "mcpremoteproxies",
                "mcpserverentries",
                "mcptoolconfigs",
            ]
        )
        self.denied = denied or set()
        self.unreachable = unreachable
        # SelfSubjectReview 가 돌려줄 username (RoleBinding subject 의 재료)
        self.identity = identity
        # LocalSubjectAccessReview 가 허락하는 (user, group, resource, verb, namespace)
        self.user_grants: set[tuple[str, str, str, str, str]] = set()
        # MCPAuthzConfig 를 적용하면 오퍼레이터처럼 그 세대의 Valid 를 적는다. 글을 두면 거부한다
        self.authz_invalid: str | None = None
        # 참이면 오퍼레이터가 MCPAuthzConfig 를 아직 보지 않은 것처럼 status 를 적지 않는다
        self.authz_pending = False
        # 서빙하는 group/version 별 리소스 (discover). cert-manager 는 기본으로 없다
        self.extra_discovery: dict[str, list[str]] = {}
        # dry-run 거부 시뮬레이션: obj -> 오류 메시지(None 이면 통과)
        self.reject_dry_run = None
        self.dry_runs: list[dict[str, Any]] = []
        # apply 가 받은 fieldValidation. (Key, 값) 이고 값을 준 호출만 남는다
        self.field_validations: list[tuple[Key, FieldValidation]] = []
        # D21: Service HTTP. 핸들러가 없으면 502 (데이터 플레인이 없는 fake)
        self.service_handler: ServiceHandler | None = None
        self.service_calls: list[dict[str, Any]] = []
        # 가짜 데이터 플레인 ASGI 앱 (에이전트 런타임 등). 핸들러보다 뒤, 502 보다 앞이다
        self.service_app: Any = None
        # delete 에 준 종료 유예 (P38 4절의 회복)
        self.delete_graces: dict[tuple[str, str, str | None, str], int] = {}
        # service_route 의 via. "apiserver" 로 두면 Bearer 를 실은 호출이 pods/portforward 로
        # 가는 경로(D41)를 테스트가 흉내 낼 수 있다. 터널은 테스트가 portforward_client 나
        # open_tunnel 자리에 fake 를 넣는다
        self.service_via: ServiceVia = "direct"
        # create 가 새로 만든 객체를 받는 훅. kubelet 처럼 파드의 status 를 적을 때 쓴다 (P40)
        self.create_hooks: list[Callable[[dict[str, Any]], None]] = []
        # 참이면 Pod 를 다시 읽을 때 hostUsers 를 버린 API 서버처럼 답한다 (P40 2절)
        self.drop_host_users = False
        # 있으면 Pod 의 create 가 이 예외를 던진다. ResourceQuota 거절 같은 API 서버의 답이다
        # (P40 7절)
        self.pod_create_error: Exception | None = None
        self._generation = 0
        if operator_image is not None:
            self.objects[("apps/v1", "Deployment", "toolhive-system", "toolhive-operator")] = {
                "apiVersion": "apps/v1",
                "kind": "Deployment",
                "metadata": {"name": "toolhive-operator", "namespace": "toolhive-system"},
                "spec": {"template": {"spec": {"containers": [{"image": operator_image}]}}},
                "status": {
                    "availableReplicas": 1 if operator_available else 0,
                    "conditions": [
                        {"type": "Available", "status": "True" if operator_available else "False"}
                    ],
                },
            }
        if tenant_objects_role:
            self.objects[(RBAC_API, "ClusterRole", None, TENANT_OBJECTS_CLUSTER_ROLE)] = {
                "apiVersion": RBAC_API,
                "kind": "ClusterRole",
                "metadata": {"name": TENANT_OBJECTS_CLUSTER_ROLE},
                "rules": [
                    {"apiGroups": [""], "resources": ["secrets"], "verbs": list(SECRET_VERBS)}
                ],
            }
            if sandbox_rights:
                self.objects[(RBAC_API, "ClusterRole", None, TENANT_OBJECTS_CLUSTER_ROLE)][
                    "rules"
                ].append(
                    {"apiGroups": [""], "resources": ["pods"], "verbs": list(SANDBOX_POD_VERBS)}
                )
        if sandbox_rights:
            key = ("scheduling.k8s.io/v1", "PriorityClass", None, SANDBOX_PRIORITY_CLASS)
            self.objects[key] = {
                "apiVersion": "scheduling.k8s.io/v1",
                "kind": "PriorityClass",
                "metadata": {"name": SANDBOX_PRIORITY_CLASS},
                "value": -10,
            }
        if policy_controller:
            self.objects[("apps/v1", "DaemonSet", "kube-system", policy_controller)] = {
                "apiVersion": "apps/v1",
                "kind": "DaemonSet",
                "metadata": {"name": policy_controller, "namespace": "kube-system"},
                "status": {"numberReady": 1},
            }

    def _check(self) -> None:
        if self.unreachable:
            raise ClusterUnreachable("fake cluster is unreachable")

    def _binding_gone(self, resource: str, namespace: str | None) -> bool:
        """밖에서 지운 네임스페이스의, RoleBinding daegam-tenant-objects 로만 받는 리소스인가."""
        return namespace in self.gone_namespaces and resource in BINDING_ONLY_RESOURCES

    def _deny(self, api_version: str, kind: str, verb: str, namespace: str | None) -> None:
        """denied 에 (group, resource, verb) 가 있거나 밖에서 지운 네임스페이스면 AccessDenied
        (403 시뮬레이션)."""
        group = "" if api_version == "v1" else api_version.split("/")[0]
        resource = _resource_name(kind)
        if self._binding_gone(resource, namespace):
            raise AccessDenied(verb, resource, namespace, "fake: namespace gone, no binding")
        if (group, resource, verb) in self.denied:
            raise AccessDenied(verb, resource, namespace, "fake: forbidden")

    # -- KubeClient --------------------------------------------------------

    def apply(
        self,
        obj: dict[str, Any],
        *,
        dry_run: bool = False,
        field_validation: FieldValidation | None = None,
    ) -> dict[str, Any]:
        self._check()
        self._deny(
            obj["apiVersion"], obj["kind"], "apply", (obj.get("metadata") or {}).get("namespace")
        )
        if field_validation is not None:
            self.field_validations.append((_key(obj), field_validation))
        if dry_run:
            self.dry_runs.append(copy.deepcopy(obj))
            if self.reject_dry_run is not None:
                msg = self.reject_dry_run(obj)
                if msg:
                    raise ValueError(msg)
        key = _key(obj)
        new = copy.deepcopy(obj)
        existing = self.objects.get(key)
        self._generation += 1
        meta = new.setdefault("metadata", {})
        if existing:
            # status 는 클라이언트가 덮지 않는다 (서버 소유)
            if "status" in existing and "status" not in new:
                new["status"] = copy.deepcopy(existing["status"])
            meta["uid"] = existing["metadata"].get("uid")
            meta["generation"] = existing["metadata"].get("generation", 0) + 1
        else:
            meta["uid"] = f"uid-{len(self.objects) + 1}"
            meta["generation"] = 1
        meta["resourceVersion"] = str(self._generation)
        if self.secret_string_data_to_data and new.get("kind") == "Secret" and "stringData" in new:
            plain = new.pop("stringData")
            encoded = {k: base64.b64encode(v.encode()).decode() for k, v in plain.items()}
            new["data"] = {**(new.get("data") or {}), **encoded}
        if not dry_run:
            if new.get("kind") == "MCPAuthzConfig" and not self.authz_pending:
                valid = "False" if self.authz_invalid else "True"
                new["status"] = {
                    "observedGeneration": meta["generation"],
                    "conditions": [
                        {"type": "Valid", "status": valid, "message": self.authz_invalid or ""}
                    ],
                }
            self.applied.append(copy.deepcopy(new))
            meta["managedFields"] = _managed_fields(existing, new)
            self.objects[key] = new
        return copy.deepcopy(new)

    def create(self, obj: dict[str, Any]) -> bool:
        self._check()
        if obj.get("kind") == "Pod":
            # 샌드박스 파드의 create 권한(P40)만 본다. 다른 create 는 preflight 의 SAR 거부와 섞인다
            self._deny("v1", "Pod", "create", (obj.get("metadata") or {}).get("namespace"))
            if self.pod_create_error is not None:
                raise self.pod_create_error
        if _key(obj) in self.objects:
            return False
        self.apply(obj)
        if self.drop_host_users and obj.get("kind") == "Pod":
            self.objects[_key(obj)].get("spec", {}).pop("hostUsers", None)
        for hook in self.create_hooks:
            hook(self.objects[_key(obj)])
        return True

    def dry_run_apply(self, obj: dict[str, Any]) -> dict[str, Any]:
        return self.apply(obj, dry_run=True)

    def get(
        self, api_version: str, kind: str, name: str, namespace: str | None = None
    ) -> dict[str, Any] | None:
        self._check()
        if self._binding_gone(_resource_name(kind), namespace):
            return None
        self._deny(api_version, kind, "get", namespace)
        obj = self.objects.get((api_version, kind, namespace, name))
        return copy.deepcopy(obj) if obj else None

    def delete(
        self,
        api_version: str,
        kind: str,
        name: str,
        namespace: str | None = None,
        *,
        grace_seconds: int | None = None,
        uid: str | None = None,
    ) -> bool:
        self._check()
        key = (api_version, kind, namespace, name)
        if key not in self.objects or self._binding_gone(_resource_name(kind), namespace):
            return False
        if uid is not None and (self.objects[key].get("metadata") or {}).get("uid") != uid:
            # 전제 조건이 맞지 않는 지우기는 409 이고 실제 클라이언트는 False 를 돌려준다
            return False
        del self.objects[key]
        self.deleted.append(key)
        if grace_seconds is not None:
            self.delete_graces[key] = grace_seconds
        return True

    def rollout_restart(self, name: str, namespace: str) -> bool:
        self._check()
        self._deny("apps/v1", "Deployment", "patch", namespace)
        obj = self.objects.get(("apps/v1", "Deployment", namespace, name))
        if obj is None:
            return False
        template = obj.setdefault("spec", {}).setdefault("template", {})
        annotations = template.setdefault("metadata", {}).setdefault("annotations", {})
        annotations[RESTARTED_AT] = f"restart-{len(self.restarts) + 1}"
        self._generation += 1
        meta = obj["metadata"]
        meta["generation"] = meta.get("generation", 0) + 1
        meta["resourceVersion"] = str(self._generation)
        self.restarts.append((namespace, name))
        return True

    def scale_deployment(self, name: str, namespace: str, replicas: int) -> int | None:
        """`spec.replicas` 만 바꾼다. 값이 바뀌면 세대가 오른다. 패치마다 `scales` 에 남는다."""
        self._check()
        self._deny("apps/v1", "Deployment", "patch", namespace)
        obj = self.objects.get(("apps/v1", "Deployment", namespace, name))
        if obj is None:
            return None
        meta = obj["metadata"]
        if obj.setdefault("spec", {}).get("replicas") != replicas:
            obj["spec"]["replicas"] = replicas
            self._generation += 1
            meta["generation"] = meta.get("generation", 0) + 1
            meta["resourceVersion"] = str(self._generation)
        self.scales.append((namespace, name, replicas))
        return int(meta.get("generation", 0))

    def drop_secret_keys(self, name: str, namespace: str, keys: list[str]) -> bool:
        return self.merge_secret_keys(name, namespace, dict.fromkeys(keys))

    def swap_secret_keys(
        self,
        name: str,
        namespace: str,
        expect: dict[str, str],
        data: dict[str, str | None],
    ) -> bool:
        """진짜처럼 test 가 하나라도 어긋나거나 뺄 키가 없으면 아무것도 바꾸지 않는다."""
        self._check()
        self._deny("v1", "Secret", "patch", namespace)
        obj = self.objects.get(("v1", "Secret", namespace, name))
        if obj is None:
            return False
        stored = decode_secret_data(obj.get("data"))
        if any(stored.get(k) != v for k, v in expect.items()):
            return False
        if any(v is None and k not in stored for k, v in data.items()):
            return False
        return self.merge_secret_keys(name, namespace, data)

    def merge_secret_keys(self, name: str, namespace: str, data: dict[str, str | None]) -> bool:
        self._check()
        self._deny("v1", "Secret", "patch", namespace)
        obj = self.objects.get(("v1", "Secret", namespace, name))
        if obj is None:
            return False
        for key, value in data.items():
            (obj.get("stringData") or {}).pop(key, None)
            stored = obj.setdefault("data", {})
            if value is None:
                stored.pop(key, None)
            else:
                stored[key] = encode_secret_data({key: value})[key]
        return True

    def list(
        self,
        api_version: str,
        kind: str,
        namespace: str | None = None,
        labels: dict[str, str] | None = None,
    ) -> list[dict[str, Any]]:
        self._check()
        if self._binding_gone(_resource_name(kind), namespace):
            raise AccessDenied("list", _resource_name(kind), namespace, "fake: namespace gone")
        if kind == "Node":
            # 노드 목록은 P40 의 RBAC 가 준다. 그 파일을 적용하지 않은 클러스터를 흉내 낸다
            self._deny(api_version, kind, "list", namespace)
        out = []
        for (av, k, ns, _name), obj in self.objects.items():
            if av != api_version or k != kind:
                continue
            if namespace is not None and ns != namespace:
                continue
            if labels:
                have = (obj.get("metadata") or {}).get("labels") or {}
                if any(have.get(lk) != lv for lk, lv in labels.items()):
                    continue
            out.append(copy.deepcopy(obj))
        return out

    def list_metadata(
        self,
        api_version: str,
        kind: str,
        namespace: str | None = None,
        *,
        field_selector: str | None = None,
        labels: dict[str, str] | None = None,
    ) -> list[dict[str, Any]]:
        """metadata 만. field selector 는 `type=`·`metadata.name=` 을 지원한다. denied 에
        ("", "secrets", "list") 처럼 list 가 있으면 AccessDenied 다 (403 시뮬레이션)."""
        self._check()
        self._deny(api_version, kind, "list", namespace)
        wanted: dict[str, str] = {}
        for part in (field_selector or "").split(","):
            if "=" in part:
                k, v = part.split("=", 1)
                wanted[k.strip()] = v.strip()
        out = []
        for obj in self.list(api_version, kind, namespace, labels):
            if "type" in wanted and obj.get("type") != wanted["type"]:
                continue
            if "metadata.name" in wanted and obj["metadata"].get("name") != wanted["metadata.name"]:
                continue
            out.append({"metadata": copy.deepcopy(obj.get("metadata") or {})})
        return out

    def version(self) -> dict[str, Any]:
        self._check()
        return {"gitVersion": self.k8s_version, "major": "1", "minor": "34"}

    def can_i(
        self,
        verb: str,
        resource: str,
        group: str = "",
        namespace: str | None = None,
        name: str | None = None,
    ) -> bool:
        self._check()
        if (group, resource, verb) in self.denied:
            return False
        if group == "" and resource == "secrets" and namespace is None:
            return self.cluster_wide_secrets
        return name is None or (group, resource, verb, name) not in self.denied

    def can_user(
        self, user: str, verb: str, resource: str, namespace: str, group: str = ""
    ) -> bool:
        self._check()
        return (user, group, resource, verb, namespace) in self.user_grants

    def whoami(self) -> dict[str, Any]:
        self._check()
        if ("authentication.k8s.io", "selfsubjectreviews", "create") in self.denied:
            raise AccessDenied("create", "selfsubjectreviews", None, "fake: forbidden")
        return {"username": self.identity, "groups": ["system:authenticated"]}

    def discover(self, group_version: str) -> list[str]:
        self._check()
        if group_version == TOOLHIVE_GV:
            return list(self.toolhive_resources)
        if group_version == "v1":
            return ["configmaps", "secrets", "namespaces", "pods"]
        return list(self.extra_discovery.get(group_version, []))

    def pod_log(
        self,
        name: str,
        namespace: str,
        *,
        since_seconds: int | None = None,
        container: str | None = None,
        limit_bytes: int | None = None,
    ) -> str:
        """add_pod_log 로 넣은 (ts, line) 중 since_seconds 안의 줄. 파드가 없으면 빈 문자열."""
        self._check()
        lines = self.pod_logs.get((namespace, name), [])
        if since_seconds is not None:
            from datetime import timedelta

            from daegam.store.base import utcnow

            cutoff = utcnow() - timedelta(seconds=since_seconds)
            lines = [(ts, line) for ts, line in lines if ts >= cutoff]
        text = "\n".join(line for _ts, line in lines)
        if limit_bytes is not None:
            text = text[:limit_bytes]
        self.log_reads.append((namespace, name, since_seconds))
        return text

    def service_route(
        self, namespace: str, service: str, port: int, *, scheme: str = "http"
    ) -> ServiceRoute:
        self._check()
        return ServiceRoute(
            base_url=f"{scheme}://{service}.{namespace}.svc.cluster.local:{port}",
            via=self.service_via,
            transport=(
                httpx2.ASGITransport(app=self.service_app) if self.service_app else _NoDataPlane()
            ),
        )

    async def service_request(
        self,
        method: str,
        namespace: str,
        service: str,
        port: int,
        path: str,
        *,
        json: Any = None,
        headers: dict[str, str] | None = None,
        timeout: float = SERVICE_REQUEST_TIMEOUT,
        read_timeout: float | None = None,
    ) -> ServiceResponse:
        self._check()
        self.service_calls.append(
            {
                "method": method,
                "namespace": namespace,
                "service": service,
                "port": port,
                "path": path,
                "json": copy.deepcopy(json),
                "headers": dict(headers or {}),
            }
        )
        if self.service_handler is None:
            if self.service_app is not None:
                return await send_service_request(
                    self.service_route(namespace, service, port),
                    method,
                    path,
                    json=json,
                    headers=headers,
                    timeout=timeout,
                    read_timeout=read_timeout,
                )
            return ServiceResponse(
                502, {}, jsonlib.dumps({"detail": "fake cluster has no data plane"}).encode()
            )
        out = self.service_handler(method, namespace, service, port, path, json)
        status, body = out[0], out[1]
        resp_headers = dict(out[2]) if len(out) > 2 else {}
        raw = body if isinstance(body, bytes) else jsonlib.dumps(body).encode("utf-8")
        return ServiceResponse(int(status), resp_headers, raw)

    # -- 테스트 도우미 ------------------------------------------------------

    def add_pod(
        self,
        namespace: str,
        name: str,
        labels: dict[str, str],
        *,
        uid: str | None = None,
        phase: str = "Running",
    ) -> dict[str, Any]:
        obj = {
            "apiVersion": "v1",
            "kind": "Pod",
            "metadata": {
                "name": name,
                "namespace": namespace,
                "labels": dict(labels),
                "uid": uid or f"uid-{name}",
            },
            "status": {"phase": phase},
        }
        self.objects[("v1", "Pod", namespace, name)] = obj
        return obj

    def add_pod_log(self, namespace: str, name: str, line: str, *, ts: Any = None) -> None:
        from daegam.store.base import utcnow

        self.pod_logs.setdefault((namespace, name), []).append((ts or utcnow(), line))

    def set_status(
        self, api_version: str, kind: str, name: str, namespace: str | None, status: dict[str, Any]
    ) -> None:
        obj = self.objects[(api_version, kind, namespace, name)]
        obj["status"] = copy.deepcopy(status)

    def edit_live(
        self,
        api_version: str,
        kind: str,
        name: str,
        namespace: str | None,
        mutate,
        *,
        manager: str = "kubectl-edit",
        fields: dict[str, Any] | None = None,
    ) -> None:
        """kubectl edit 흉내: 저장된 객체를 직접 고치고 managedFields 에 manager 를 남긴다.
        fields 는 그 manager 가 쥔 자리(fieldsV1, 예 `{"f:spec": {"f:env": {}}}`)이고, 없으면
        fieldsV1 없는 기록이라 무엇을 쥐었는지 모르는 manager 다."""
        obj = self.objects[(api_version, kind, namespace, name)]
        mutate(obj)
        meta = obj.setdefault("metadata", {})
        mf = meta.setdefault("managedFields", [{"manager": FIELD_MANAGER, "operation": "Apply"}])
        if not any(m.get("manager") == manager for m in mf):
            entry: dict[str, Any] = {"manager": manager, "operation": "Update"}
            if fields is not None:
                entry["fieldsV1"] = copy.deepcopy(fields)
            mf.append(entry)
        self._generation += 1
        meta["resourceVersion"] = str(self._generation)
        meta["generation"] = meta.get("generation", 0) + 1

    def drop_namespace(self, namespace: str) -> None:
        """클러스터 관리자가 네임스페이스를 밖에서 지운 것처럼 그 안의 객체를 모두 없앤다."""
        for key in [
            k for k in self.objects if k[2] == namespace or k[1:] == ("Namespace", None, namespace)
        ]:
            del self.objects[key]
        self.gone_namespaces.add(namespace)

    def find(self, kind: str, namespace: str | None = None) -> list[dict[str, Any]]:
        return [
            copy.deepcopy(o)
            for (_, k, ns, _), o in self.objects.items()
            if k == kind and (namespace is None or ns == namespace)
        ]


def _managed_fields(
    existing: dict[str, Any] | None, applied: dict[str, Any]
) -> list[dict[str, Any]]:
    """적용 뒤의 managedFields. 다른 manager 의 기록은 그대로 두고 플랫폼 field manager 의 Apply
    기록을 적용한 객체의 spec 바로 아래 자리로 새로 쓴다. API 서버는 강제 적용으로 넘겨받은 자리를
    다른 manager 의 기록에서 지우지만, 이 가짜는 지우지 않아 소유를 넓게 본다."""
    kept = [
        copy.deepcopy(m)
        for m in ((existing or {}).get("metadata") or {}).get("managedFields") or []
        if m.get("manager") != FIELD_MANAGER
    ]
    spec = applied.get("spec")
    fields: dict[str, Any] = {}
    if isinstance(spec, dict):
        fields["f:spec"] = {f"f:{key}": {} for key in spec}
    ours = {"manager": FIELD_MANAGER, "operation": "Apply", "fieldsV1": fields}
    return [ours, *kept]
