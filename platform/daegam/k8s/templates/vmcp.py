"""P4 허브 항목과 Gateway 그룹·Entry·VirtualMCPServer 렌더.

MCPGroup·MCPServerEntry(Gateway 백엔드 래퍼), 허브 image 항목의 MCPServer·MCPToolConfig,
vMCP incomingAuth 와 VirtualMCPServer, vMCP Service 이름·주소 규칙. 허브 entry 항목은 CR 을
만들지 않고 source.url 을 그대로 쓴다."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from daegam.auth.cedar import (
    render_authz_inline,
    split_statements,
    tenant_policies,
)
from daegam.config.resolve import EffectiveConfig
from daegam.k8s.labels import LABEL_PROFILE_REVISION, daegam_labels, ensure_labels
from daegam.k8s.templates.common import (
    FOREIGN_CONTAINER_SECURITY,
    FOREIGN_POD_SECURITY,
    PROXY_PORT,
    TOOLHIVE_API,
    hub_labels,
    internal_oidc_config_ref,
    is_private_endpoint,
    mark_mcp_pods,
    owner_ref,
    pod_placement,
    proxy_resources,
    proxy_service_url,
    secret_env,
    with_owner,
)

VMCP_PORT = 4483
# image 항목의 Secret env 판 (P55 5절). 값을 바꾸면 판이 올라 백엔드 파드가 다시 뜬다. 값에서 짓지
# 않는다. 매니페스트에 보이기 때문이다
SECRET_ENV_ANNOTATION = "daegam.devops.ai.kr/secret-env-rev"
# stdio·카탈로그 image 항목의 쓸 수 있는 `/tmp` (P55 5절). 노드 디스크에 두어 메모리 한도에 들지
# 않고, 넘치면 kubelet 이 파드를 내보낸다
TMP_VOLUME = "tmp"
TMP_SIZE_LIMIT = "64Mi"
PERMIT_ALL = "permit(principal, action, resource);"
LABEL_GATEWAY_PROFILE = "daegam.devops.ai.kr/gateway-profile"
# vMCP 감사가 남기는 이벤트 (P50 4절). 요청·응답 본문은 싣지 않는다(includeRequestData·
# includeResponseData 는 기본 false). 수집기는 vMCP 파드 stdout 을 다른 프록시처럼 읽는다
VMCP_AUDIT_EVENT_TYPES: tuple[str, ...] = ("mcp_tool_call", "mcp_resource_read", "mcp_prompt_get")
# ToolHive 의 code mode 가 내는 가상 Tool 이름 (P56 4절). 게시된 Gateway 가 code mode 로 돌면 그
# 목록에 이 이름이 있다
CODE_MODE_TOOL = "execute_tool_script"
# 라벨 daegam.devops.ai.kr/id 로 지우는 vMCP 묶음의 (apiVersion, kind). Gateway 의 것은 이름으로
# 지우고 남은 것을 거두고, 노출한 MCP Server 의 vMCP 앞단의 것은 이름이 남의 것과 겹칠 수 있어
# 라벨로만 지운다(`services/instance_base.py` `delete_labeled`, P54 5절)
GROUP_ENTRY_KINDS: tuple[tuple[str, str], ...] = (
    (TOOLHIVE_API, "MCPServerEntry"),
    (TOOLHIVE_API, "MCPGroup"),
)


def published_pins(backend: Any) -> list[str]:
    """토큰 인증에서 게시된 백엔드의 Entry 가 고정하는 이름 (P57 6.2절). 배포 스냅샷의 선택(비면
    항목이 아는 이름 전부)에서 `execute_tool_script` 를 뺀 것이다. 그 스크립트 안의 호출은 게시된
    Gateway 가 소비자의 토큰으로 돌려 소비자의 규칙을 지나지 않는다. 게시된 Gateway 가 나중에 더한
    Tool 은 소비자를 다시 배포할 때 들어온다. backend 는 `tools`·`tool_names` 를 가진 스냅샷이다."""
    return [n for n in (backend.tools or backend.tool_names) if n != CODE_MODE_TOOL]


def vmcp_owner_ref(name: str, uid: str) -> dict[str, Any]:
    """그룹·Entry 를 vMCP 에 종속시킨다 (vMCP 삭제만으로 GC)."""
    return owner_ref("VirtualMCPServer", name, uid)


def render_group(
    *,
    tenant: str,
    instance_id: str,
    name: str,
    namespace: str,
    description: str,
    owner: dict[str, Any] | None = None,
    resource: str = "gateway-group",
) -> dict[str, Any]:
    """Gateway 마다 MCPGroup 하나 (D5). 멤버는 파드 없는 MCPServerEntry 래퍼다.
    daegam.devops.ai.kr/id 가 인스턴스 id 라 정리·조회는 그 라벨로 한다. 노출한 MCP Server 의 vMCP
    앞단은 resource 를 자기 값으로 준다 (P54 5절)."""
    doc = ensure_labels(
        {
            "apiVersion": TOOLHIVE_API,
            "kind": "MCPGroup",
            "metadata": {"name": name, "namespace": namespace},
            "spec": {"description": description},
        },
        tenant=tenant,
        resource=resource,
        id=instance_id,
    )
    return with_owner(doc, owner)


def render_entry(
    *,
    tenant: str,
    instance_id: str,
    name: str,
    namespace: str,
    group: str,
    url: str,
    transport: str,
    hub_item_id: str | None,
    owner: dict[str, Any] | None = None,
    external_auth_config: str | None = None,
    resource: str = "gateway-backend",
) -> dict[str, Any]:
    """Gateway 그룹의 백엔드 래퍼. 이름 `<instance>-<item>` = vMCP workload. 노출한 MCP Server 의
    vMCP 앞단은 허브 항목 없이(hub_item_id None, 라벨 없음) 자기 resource 로 그 서버를 감싼다 (P54
    5절).

    external_auth_config 는 우리 MCPServer(internal·image 항목)와 플랫폼 웹 Tool(platform 항목)을
    가리키는 래퍼(`ENTRY_TOKEN_KINDS`), 묶인 토큰을 받는 게시된 vMCP(P57), 고정 헤더를 싣는 entry
    항목(그 헤더의 사본, P86 6.2절)에 준다. `static_backends` 게이트를 지나지 못한 클러스터의 vMCP
    는 `outgoingAuth {source: discovered}` 로 이것을 발견한다(D41). 그 밖의 사용자가 넣은
    주소(remote, 인증 없는 entry)에는 주지 않는다. 플랫폼의 토큰이 그리로 나가면 안 된다."""
    spec: dict[str, Any] = {
        "remoteUrl": url,
        "transport": transport,
        "allowPrivateEndpoint": is_private_endpoint(url),
        "groupRef": {"name": group},
    }
    if external_auth_config:
        spec["externalAuthConfigRef"] = {"name": external_auth_config}
    doc = ensure_labels(
        {
            "apiVersion": TOOLHIVE_API,
            "kind": "MCPServerEntry",
            "metadata": {"name": name, "namespace": namespace},
            "spec": spec,
        },
        tenant=tenant,
        resource=resource,
        id=instance_id,
    )
    if hub_item_id is not None:
        doc["metadata"]["labels"]["daegam.devops.ai.kr/hub-item"] = hub_item_id
    return with_owner(doc, owner)


def render_tool_config(
    *, tenant: str, item_id: str, name: str, namespace: str, tools_filter: list[str]
) -> dict[str, Any]:
    return {
        "apiVersion": TOOLHIVE_API,
        "kind": "MCPToolConfig",
        "metadata": {
            "name": name,
            "namespace": namespace,
            "labels": hub_labels(tenant, item_id, "image"),
        },
        "spec": {"toolsFilter": list(tools_filter)},
    }


@dataclass(frozen=True)
class ImageContainer:
    """image 항목의 컨테이너 입력 (P55 5절). env 는 (이름, 값)이고 secret_env 는 값이 든 이름이다.
    secret_name 은 그 값을 든 Secret 이다."""

    stdio: bool = False
    args: tuple[str, ...] = ()
    env: tuple[tuple[str, str], ...] = ()
    secret_env: tuple[str, ...] = ()
    secret_name: str | None = None
    secret_env_rev: int | None = None
    writable_tmp: bool = False


def render_image_server(
    *,
    tenant: str,
    item_id: str,
    name: str,
    namespace: str,
    group: str,
    image: str,
    transport: str,
    port: int | None,
    tool_config: str | None,
    effective: EffectiveConfig | None = None,
    server_id: str | None = None,
    fence: dict[str, Any] | None = None,
    container: ImageContainer | None = None,
) -> dict[str, Any]:
    """허브 image 항목과 그 공유 복제본의 MCPServer. fence 는 테넌트 울타리의 spec 칸이다
    (`server_fence`, P50 11절). 울타리 게이트를 지나지 못한 클러스터는 None 이다. container 는
    컨테이너 입력(P55 5절)이고, 없으면 그 칸 전의 렌더와 같다. stdio 컨테이너는 transport 로
    프록시가 내고(`proxyMode`), Secret env 는 백엔드 컨테이너의 secretKeyRef 다. 그 자리는 어느
    ToolHive 판이든 받으므로 렌더가 게이트를 따르지 않는다."""
    container = container or ImageContainer()
    spec: dict[str, Any] = {
        "image": image,
        "transport": "stdio" if container.stdio else transport,
        "proxyPort": PROXY_PORT,
        "groupRef": {"name": group},
        "replicas": 1,
        # 내부 홉 인증 (D41). MCP Server 의 MCPServer 와 같다. tenant 는 이 CR 이 사는
        # 네임스페이스의 테넌트(공유 복제본이면 소비하는 테넌트)라 그 테넌트의 토큰만 통한다.
        # server_id 는 자기 audience 로 옮겨 간 서버(항목이나 복제본 기록)의 id 이고, 그러면 그 서버
        # 하나의 토큰만 통한다 (P50 9절)
        "oidcConfigRef": internal_oidc_config_ref(
            tenant, proxy_service_url(name, namespace), server_id
        ),
    }
    if container.stdio:
        # 소비자가 말하는 전송은 그대로다 (P55 5절). Entry 의 CRD 는 stdio 를 받지 않는다
        spec["proxyMode"] = transport
    if fence:
        spec.update(fence)
    if port:
        spec["mcpPort"] = port
    if tool_config:
        spec["toolConfigRef"] = {"name": tool_config}
    if container.args:
        spec["args"] = list(container.args)
    if container.env:
        spec["env"] = [{"name": n, "value": v} for n, v in container.env]
    # 허브의 외부 이미지는 uid 를 알 수 없어 사용자·그룹은 정하지 않는다. 그래도 권한 상승·
    # capability·seccomp 는 어느 이미지에나 닫을 수 있다
    mcp: dict[str, Any] = {"name": "mcp", "securityContext": FOREIGN_CONTAINER_SECURITY}
    pod: dict[str, Any] = {"securityContext": FOREIGN_POD_SECURITY, "containers": [mcp]}
    if effective is not None:
        pod.update(pod_placement(effective))
        if effective.resources:
            mcp["resources"] = effective.resources
    if container.secret_name and container.secret_env:
        mcp["env"] = [secret_env(n, container.secret_name, n) for n in container.secret_env]
    if container.writable_tmp:
        pod["volumes"] = [{"name": TMP_VOLUME, "emptyDir": {"sizeLimit": TMP_SIZE_LIMIT}}]
        mcp["volumeMounts"] = [{"name": TMP_VOLUME, "mountPath": "/tmp"}]
    # ToolHive 는 spec.resources 를 MCP 컨테이너가 아니라 프록시 Deployment 에 넣는다 (D54)
    spec["resources"] = proxy_resources()
    spec["podTemplateSpec"] = {"spec": pod}
    mark_mcp_pods(spec, name)
    if container.secret_env_rev is not None:
        meta = spec["podTemplateSpec"]["metadata"]
        meta["annotations"] = {SECRET_ENV_ANNOTATION: str(container.secret_env_rev)}
    return {
        "apiVersion": TOOLHIVE_API,
        "kind": "MCPServer",
        "metadata": {
            "name": name,
            "namespace": namespace,
            "labels": hub_labels(tenant, item_id, "image"),
        },
        "spec": spec,
    }


def vmcp_policies(
    *,
    tenant_group: str,
    agent_client_ids: list[str],
    client_id_claim: str,
    extra_policy: str | None = None,
    forbids: Sequence[str] = (),
    delegation: str | None = None,
) -> list[str]:
    """토큰 모드 vMCP 의 Cedar. 테넌트 그룹(THVGroup) + 에이전트 client id(client_id_claim) +
    이 Gateway 에 묶인 위임 토큰(delegation, local 모드, P53 3.1절) + authz_policy + forbid
    (끈 표면 P50 13절, 묶이지 않은 위임 토큰 P53)다. 사람이 쓴 authz_policy 는 문장마다 원소
    하나다 (T63). 렌더는 파싱하지 않으므로 틀린 문장도 자기 원소가 되고 오퍼레이터가 거절한다."""
    policies = tenant_policies(tenant_group, agent_client_ids, client_id_claim)
    if delegation is not None:
        policies.append(delegation)
    policies += split_statements(extra_policy)
    return [*policies, *forbids]


def anonymous_policies(forbids: Sequence[str] = ()) -> list[str]:
    """header 모드 vMCP 의 Cedar. 누구나 부르되(permit-all) 끈 표면은 forbid 로 막는다."""
    return [PERMIT_ALL, *forbids]


def _authz(
    policies: list[str],
    authz_ref: dict[str, Any] | None,
    groups_claim: str | None = None,
    roles_claim: str | None = None,
) -> dict[str, Any]:
    """참조가 있으면(G8 게이트를 지난 클러스터) `authzConfigRef`, 없으면 인라인이다. CRD 는 둘을
    함께 받지 않는다. 참조일 때 그룹·역할 클레임은 참조한 MCPAuthzConfig 에 있다."""
    if authz_ref is not None:
        return {"authzConfigRef": dict(authz_ref)}
    return {"authzConfig": render_authz_inline(policies, groups_claim, roles_claim)}


def vmcp_incoming_auth(
    *,
    oidc_ref: dict[str, Any],
    policies: list[str],
    groups_claim: str | None,
    authz_ref: dict[str, Any] | None = None,
    roles_claim: str | None = None,
) -> dict[str, Any]:
    """vMCP OIDC incomingAuth (P6 F4a 2부, D16). MCPOIDCConfig 는 ensure_namespace_objects 가
    테넌트 네임스페이스에 이미 만들었고 여기서는 oidc_ref(`oidc_config_ref`)로 참조만 한다. audience
    는 참조 쪽에 있다. Cedar 는 `vmcp_policies` 의 것이고, authz_ref 가 있으면 그 정책을 든
    MCPAuthzConfig 를 참조한다(P50 8절). 노출된 Gateway 는 참조에 resourceUrl 만 더한다
    (VirtualMCPServer 에는 trustProxyHeaders 가 없다, D22). groups_claim 은 ToolHive 가 그룹을 읽을
    클레임이다(`ConsumerAuth.groups_claim`)."""
    return {
        "type": "oidc",
        "oidcConfigRef": oidc_ref,
        **_authz(policies, authz_ref, groups_claim, roles_claim),
    }


def anonymous_incoming_auth(
    policies: list[str] | None = None, *, authz_ref: dict[str, Any] | None = None
) -> dict[str, Any]:
    """header 모드의 vMCP incomingAuth. 정책을 주지 않으면 permit-all 이다(`anonymous_policies`).
    anonymous 에서 authzConfig 는 필수가 아니지만 명시한다."""
    return {"type": "anonymous", **_authz(policies or anonymous_policies(), authz_ref)}


def render_vmcp(
    *,
    tenant: str,
    instance_id: str,
    profile_id: str | None,
    revision: int,
    name: str,
    namespace: str,
    group: str,
    backends: list[dict[str, Any]],  # {workload, filter, overrides{tool: {name}}}
    failure_mode: str,
    effective: EffectiveConfig,
    conflict: str = "prefix",  # prefix | priority
    incoming_auth: dict[str, Any] | None = None,  # 없으면 anonymous + permit-all (header 모드)
    replicas: int = 1,
    resources: dict[str, Any] | None = None,
    pod_annotations: dict[str, str] | None = None,  # 서비스 토큰 판 (D41). 바뀌면 굴러간다
    audit: bool = False,  # 감사 이벤트를 남긴다 (P50 4절). 게이트를 지난 클러스터에서만 켠다
    timeouts: dict[str, str] | None = None,  # Entry 이름 → 호출 타임아웃 (P50 7절)
    static_backends: dict[str, str] | None = None,  # Entry 이름 → 서비스 토큰 MCPExternalAuthConfig
    code_mode: dict[str, Any] | None = None,  # spec.config.codeMode (P56 6.4절). 없으면 끈다
    resource: str = "gateway-instance",  # 노출한 MCP Server 의 vMCP 앞단은 자기 값이다 (P54 5절)
    template_revision: bool = True,  # 리비전 라벨을 파드 템플릿에도 단다. vMCP 앞단은 달지 않는다
) -> dict[str, Any]:
    """VirtualMCPServer.
    priority 모드는 접두사 없이 backends 순으로 우선순위를 주고
    충돌 툴만 overrides 로 이름을 바꾼다.
    세션은 소비자가 요청마다 여닫으므로 sessionAffinity·sessionStorage 는 두지 않는다.
    timeouts 가 있으면 백엔드마다의 호출 타임아웃을 `operational.timeouts.perWorkload` 에 적는다.
    없으면(타임아웃 게이트를 지나지 못한 클러스터) vMCP 기본값(30초)이다. static_backends 가 있으면
    (G5 게이트를 지난 클러스터) 나가는 인증을 `outgoingAuth.source: inline` 으로 Entry 마다 적는다.
    vMCP 가 백엔드의 인증 설정을 발견하러 네임스페이스의 Secret·ConfigMap 을 읽지 않으므로 그 역할이
    줄어든다(F0). default 는 두지 않는다. profile_id 가 None 이면(vMCP 앞단) Gateway 정의의 라벨을
    달지 않는다. revision 은 라벨 daegam.devops.ai.kr/profile-revision 이다(`vmcp_caught_up`).
    template_revision 이 거짓이면(vMCP 앞단) 그 라벨을 CR 에만 달아, 서버를 새 리비전으로 다시
    배포해도 앞단의 파드가 굴러가지 않는다 (P54 5절)."""
    labels = daegam_labels(tenant=tenant, resource=resource, id=instance_id)
    if profile_id is not None:
        labels[LABEL_GATEWAY_PROFILE] = profile_id
    labels[LABEL_PROFILE_REVISION] = str(revision)
    tools: list[dict[str, Any]] = []
    for b in backends:
        entry: dict[str, Any] = {"workload": b["workload"]}
        if b.get("filter"):
            entry["filter"] = list(b["filter"])
        if b.get("overrides"):
            entry["overrides"] = {k: dict(v) for k, v in b["overrides"].items()}
        tools.append(entry)
    if conflict == "priority":
        resolution: dict[str, Any] = {
            "conflictResolution": "priority",
            "conflictResolutionConfig": {"priorityOrder": [b["workload"] for b in backends]},
        }
    else:
        resolution = {
            "conflictResolution": "prefix",
            "conflictResolutionConfig": {"prefixFormat": "{workload}_"},
        }
    pod = pod_placement(effective)
    # ToolHive 는 failureHandling 을 검증만 하고 동작에 쓰지 않는다
    # (0.51.2 pkg/vmcp/config/validator.go:521). vMCP 는 늘 남은 백엔드로 답하고, 이 값이 가르는
    # 것은 대감 화면 배지뿐이다. timeouts 는 호출을 끊는다(0.51.2 실측). 나머지 operational 값은
    # 오퍼레이터가 빈 자리만 기본값으로 채운다(defaults.go 의 mergo.Merge)
    operational: dict[str, Any] = {"failureHandling": {"partialFailureMode": failure_mode}}
    if timeouts:
        operational["timeouts"] = {"perWorkload": dict(timeouts)}
    spec: dict[str, Any] = {
        "groupRef": {"name": group},
        "incomingAuth": incoming_auth or anonymous_incoming_auth(),
        "outgoingAuth": static_outgoing_auth(static_backends)
        if static_backends is not None
        else {"source": "discovered"},
        "config": {
            "aggregation": {
                **resolution,
                "defaultToolVisibility": "deny",
                "tools": tools,
            },
            "operational": operational,
        },
        "replicas": replicas,
    }
    if audit:
        spec["config"]["audit"] = {"enabled": True, "eventTypes": list(VMCP_AUDIT_EVENT_TYPES)}
    if code_mode is not None:
        # 걸음 수와 동시 호출 수는 ToolHive 의 기본값을 두고 toolCallTimeout 만 적는다 (P56 6.4절)
        spec["config"]["codeMode"] = dict(code_mode)
    # vMCP 이미지는 ToolHive 의 것이라 uid 를 정하지 않되 seccomp 는 파드 수준에서 닫는다. ToolHive
    # 는 spec.resources 를 읽지 않고 늘 기본값을 넣으므로 자원은 podTemplateSpec 의 vmcp 컨테이너에
    # 적는다. 리비전 값이 없으면 고정값이다(실측 33MiB 에 ToolHive 기본 128Mi 요청은 크다, D54)
    pod = {
        "securityContext": FOREIGN_POD_SECURITY,
        **pod,
        "containers": [{"name": "vmcp", "resources": resources or proxy_resources()}],
    }
    template_labels = dict(labels)
    if not template_revision:
        del template_labels[LABEL_PROFILE_REVISION]
    template_meta: dict[str, Any] = {"labels": template_labels}
    if pod_annotations:
        template_meta["annotations"] = dict(pod_annotations)
    spec["podTemplateSpec"] = {"metadata": template_meta, "spec": pod}
    return {
        "apiVersion": TOOLHIVE_API,
        "kind": "VirtualMCPServer",
        "metadata": {"name": name, "namespace": namespace, "labels": labels},
        "spec": spec,
    }


def static_outgoing_auth(backends: dict[str, str]) -> dict[str, Any]:
    """`outgoingAuth.source: inline` (P50 12절, G5). 키는 Entry 이름이고 값은 그 Entry 에 서비스
    토큰이나 묶인 토큰(P57), 고정 헤더의 사본(P86 6.2절)을 붙이는 MCPExternalAuthConfig 다. 붙일
    것이 없는 Entry(remote, 인증 없는 entry, 토큰 없는 published)는 적지 않는다."""
    out: dict[str, Any] = {"source": "inline"}
    if backends:
        out["backends"] = {
            workload: {"type": "externalAuthConfigRef", "externalAuthConfigRef": {"name": name}}
            for workload, name in sorted(backends.items())
        }
    return out


def vmcp_service_name(name: str) -> str:
    """ToolHive 가 VirtualMCPServer 마다 만드는 Service 이름 `vmcp-<name>`."""
    return f"vmcp-{name}"


def vmcp_cluster_url(name: str, namespace: str) -> str:
    return f"http://{vmcp_service_name(name)}.{namespace}.svc.cluster.local:{VMCP_PORT}/mcp"


def vmcp_url(status_url: str | None, name: str, namespace: str) -> str:
    """vMCP status.url 에는 경로가 없다. `/mcp` 를 붙인다. status 가 없으면 규칙으로 만든다."""
    fallback = f"http://{vmcp_service_name(name)}.{namespace}.svc:{VMCP_PORT}"
    base = (status_url or fallback).rstrip("/")
    return base if base.endswith("/mcp") else f"{base}/mcp"
