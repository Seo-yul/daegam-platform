"""MCP Server 의 MCPServer CR 과 initContainer 토큰 Secret 렌더.

입력은 문서·effective_config 스냅샷·핀된 리비전·Backend API 뿐이다. 배포 쪽(`k8s`·
`deployed_revision`)이 채워진 문서만 받는다.
P1 스파이크(deploy/dev/lab/manifests/40-mcpserver.yaml)와 ToolHive 제약을 따른다:
- podTemplateSpec 의 컨테이너 이름은 반드시 `mcp` (proxyrunner 가 이 이름으로 image 를 덮는다)
- MCPServer.spec.env 는 name/value 만 받는다
- spec.secrets 는 0.52 부터 list-map 키가 (name, key)라 한 Secret 의 여러 키를 넣는다. 그 앞의 판은
  키가 name 하나라 SSA 에서 같은 Secret 이름을 두 번 넣을 수 없다(orbstack 에서 확인, 500). 그런
  클러스터(`multi_key_secrets` 게이트를 지나지 못한 것)에는 Secret 당 한 키(password)만
  spec.secrets 로 넣고, 나머지 비밀 키는 podTemplateSpec 의 mcp 컨테이너 env(secretKeyRef)로
  넣는다. proxyrunner 는 그 env 를 지우지 않고 자기 env 와 병합한다 (0.46.0 실측, 0.51.2 도 같다)
- 0.52 부터 내부 MCPServer 는 테넌트 울타리 Cedar 를 건다 (`tenant_fence`, P50 11절)
- imagePullSecrets 는 MCPServer spec 에 없다. podTemplateSpec.spec.imagePullSecrets 에 넣는다
- mcpPort 8000 (mcp-runtime 이미지 포트), proxyPort 8080, transport streamable-http
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from daegam.config.resolve import EffectiveConfig
from daegam.k8s.gates import Feature
from daegam.k8s.labels import LABEL_PROFILE_REVISION, daegam_labels
from daegam.k8s.templates.common import (
    DEFAULT_REDIS_SOURCE,
    PROXY_PORT,
    REDIS_CA_ANNOTATION,
    TOOLHIVE_API,
    RenderError,
    binding_ca_volume,
    binding_env_and_secrets,
    ca_env,
    config_fetch_pod,
    curl_fetch,
    internal_oidc_config_ref,
    mark_mcp_pods,
    opaque_secret,
    proxy_resources,
    proxy_service_url,
    redis_ca_mark,
    secret_env,
    tls_endpoint,
)
from daegam.store.auth_reapply import switched_server_id
from daegam.store.backend_apis import BackendAPI
from daegam.store.clusters import DEFAULT_REDIS_CA_SECRET, Cluster
from daegam.store.data_services import REDIS_CA_KEY, DataService
from daegam.store.mcp_servers import (
    MCP_DEFAULT_TIMEOUT_SECONDS,
    InstanceK8s,
    MCPServer,
    MCPServerRevision,
)
from daegam.urls import API_PREFIX

MCP_PORT = 8000
# 이 수 이상의 복제는 세션을 나눠 가져야 해서 Redis 세션 저장소를 둔다(`_session_storage`)
SESSION_REPLICAS = 2
TOKEN_KEY = "token"
# backend 하나의 렌더가 내는 env 이름(`_backend_env`). MCP Server 의 배포 스냅샷을 살아 있는
# MCPServer 에서 채우는 쪽(`services/mcp_inputs.py`)도 이 이름으로 읽는다
SINGLE_ENDPOINT_ENV = "SPEC2OPENAPI_ENDPOINT"
SINGLE_AUTH_ENV = "SPEC2OPENAPI_AUTH"
SINGLE_PASSWORD_ENV = "SPEC2OPENAPI_PASSWORD"
SINGLE_USERNAME_ENV = "SPEC2OPENAPI_USERNAME"
# backend 하나의 호출 타임아웃(초). 런타임이 지금 이미지부터 읽는다 (P50 7절)
SINGLE_TIMEOUT_ENV = "SPEC2OPENAPI_TIMEOUT"
# 배포 스냅샷의 CA 묶음(`EffectiveConfig.ca_bundle`)을 싣는 env. mcp-runtime 이 같은 이름으로 읽어
# 백엔드 TLS 검증의 기본 루트에 더한다 (D88)
CA_ENV = "DAEGAM_CA_B64"
# WSDL 이 선언한 soap:header 의 값(JSON 객체). Backend API 의 `soap_headers_secret` 이 가리키는
# Secret 의 이 키에서 온다 (T60). backend 가 여럿이면 `backend_env(i, SOAP_HEADERS_KEY)` 다
SINGLE_SOAP_HEADERS_ENV = "SPEC2OPENAPI_SOAP_HEADERS"
SOAP_HEADERS_KEY = "soap_headers"
# 스펙을 받는 init 컨테이너와 스펙 볼륨. merge.LOCKED_PATHS 가 이 이름으로 잠근다
FETCH_INIT = "fetch-spec"
SPEC_VOLUME = "spec"


def api_base(platform_internal_url: str) -> str:
    """init 컨테이너가 부르는 플랫폼 API 의 뿌리 (MCPServer·Agent 가 같다)."""
    return platform_internal_url.rstrip("/") + API_PREFIX


def openapi_url(platform_internal_url: str, backend_profile_id: str, version: int) -> str:
    base = api_base(platform_internal_url)
    return f"{base}/backend-apis/{backend_profile_id}/versions/{version}/openapi.yaml"


def profile_json_url(platform_internal_url: str, server_id: str, revision: int) -> str:
    base = api_base(platform_internal_url)
    return f"{base}/mcp-servers/{server_id}/revisions/{revision}/profile.json"


def _suffix(i: int, n: int, sep: str) -> str:
    """backend 가 하나면 예전 이름 그대로 두고(빈 접미사), 둘 이상이면 `<sep><i>` 를 붙인다."""
    return "" if n == 1 else f"{sep}{i}"


def token_key(i: int, n: int) -> str:
    """n 개 backend 중 i 번째의 토큰 Secret 키. 하나면 `token`, 둘 이상이면 `token-<i>` 다."""
    return f"{TOKEN_KEY}{_suffix(i, n, '-')}"


def _k8s(inst: MCPServer) -> InstanceK8s:
    if inst.k8s is None:
        raise RenderError("not_deployed", f"mcp server {inst.name} has no deployment side")
    return inst.k8s


def render_token_secret(inst: MCPServer, tokens: list[str]) -> dict[str, Any]:
    """initContainer 가 쓰는 Backend API 읽기 토큰. 테넌트 네임스페이스에 둔다.
    backend 가 하나면 예전처럼 `token` 키 하나, 둘 이상이면 backends 순서대로 `token-<i>`."""
    k8s = _k8s(inst)
    return opaque_secret(
        k8s.token_secret,
        k8s.namespace,
        {token_key(i, len(tokens)): t for i, t in enumerate(tokens)},
        tenant=inst.tenant,
        resource="mcp-instance-token",
        id=inst.id,
    )


def _session_storage(
    inst: MCPServer,
    effective: EffectiveConfig,
    bound: list[tuple[str, DataService]],
    cluster: Cluster,
    *,
    session_tls: bool,
    default_redis_ca: bool,
) -> dict[str, Any]:
    """replicas ≥ 2 는 세션 공유가 필요하다. Redis 바인딩 > Cluster.default_redis > 422.
    session_tls 는 서비스가 정한 것이다(배포 때의 `session_tls` 게이트, 다시 렌더는 배포 스냅샷).
    참이고 Redis 가 TLS 끝점을 가지면 그 끝점과 CA Secret 으로 붙는다 (P61 3절). 기본 Redis 는 그
    CA 가 테넌트 네임스페이스에 놓일 때(default_redis_ca)만이다. 아니면 평문 포트다."""
    for _prefix, ds in bound:
        if ds.kind == "redis":
            tls = tls_endpoint(ds, session_tls)
            storage: dict[str, Any] = {
                "provider": "redis",
                "address": f"{ds.host}:{tls.port if tls is not None else ds.port}",
                "db": ds.db or 0,
                "keyPrefix": f"daegam:{inst.id}:",
            }
            if ds.has_password:
                storage["passwordRef"] = {"name": ds.secret_name, "key": "password"}
            if tls is not None:
                storage["tls"] = _session_tls(tls.ca_secret)
            return storage
    if effective.default_redis is not None:
        default = effective.default_redis
        tls_address = default.tls_address() if session_tls and default_redis_ca else None
        storage = {
            "provider": "redis",
            "address": tls_address or default.address,
            "keyPrefix": f"daegam:{inst.id}:",
        }
        if default.password_secret:
            storage["passwordRef"] = {
                "name": default.password_secret,
                "key": "password",
            }
        if tls_address is not None:
            storage["tls"] = _session_tls(DEFAULT_REDIS_CA_SECRET)
        return storage
    raise RenderError(
        "redis_required",
        f"replicas={effective.replicas} needs a Redis binding or cluster default_redis "
        f"for session storage (cluster {cluster.name})",
    )


def _session_tls(secret: str) -> dict[str, Any]:
    """`sessionStorage.tls`. 사설 CA 를 그 Secret 의 `ca.crt` 에서 읽어 서버 인증서를 확인한다.
    핸드셰이크가 실패해도 평문으로 물러서지 않는다(ToolHive 0.52 CRD 설명)."""
    return {"caCertSecretRef": {"name": secret, "key": REDIS_CA_KEY}}


def backend_env(index: int, key: str) -> str:
    """backend 가 여럿인 렌더에서 index 번째 backend 의 env 이름이다. key 는 endpoint·auth 이거나
    자격증명 Secret 의 키(username·password)거나 soap:header 값의 키(`SOAP_HEADERS_KEY`)다.
    런타임이 같은 이름으로 읽는다(`mcp-runtime` `_backend_options`)."""
    return f"DAEGAM_BACKEND_{index}_{key.upper()}"


class _SecretRefs:
    """mcp 컨테이너에 Secret 키를 넣는 두 자리. spec.secrets 가 먼저이고, 그 자리에 넣을 수 없는
    키는 mcp 컨테이너 env(secretKeyRef)로 간다. multi_key 가 거짓이면(0.52 앞의 CRD) Secret 마다
    한 키만, 참이면 (Secret, 키) 마다 하나를 spec.secrets 에 넣는다. CRD 의 list-map 키가
    그것이다."""

    def __init__(self, *, multi_key: bool):
        self.multi_key = multi_key
        self.secrets: list[dict[str, str]] = []
        self.pod_env: list[dict[str, Any]] = []

    def add(self, env_name: str, secret: str, key: str, *, prefer_secrets: bool = True) -> None:
        if prefer_secrets and self._free(secret, key):
            self.secrets.append({"name": secret, "key": key, "targetEnvName": env_name})
        else:
            self.pod_env.append(secret_env(env_name, secret, key))

    def _free(self, secret: str, key: str) -> bool:
        if self.multi_key:
            return all((s["name"], s["key"]) != (secret, key) for s in self.secrets)
        return all(s["name"] != secret for s in self.secrets)


def _connection_env(bp: BackendAPI, endpoint_env: str, auth_env: str) -> list[dict[str, str]]:
    """backend 하나의 endpoint·auth env. 런타임은 env 를 profile.json 보다 먼저 읽는다.
    profile.json 은 파드가 뜰 때마다 지금 Backend API 로 만들어지므로, 배포가 읽은 값(재렌더는 배포
    스냅샷)을 env 로 내야 파드가 다시 떠도 배포 때의 주소와 인증으로 부른다. endpoint 가 없거나
    auth 가 none 이면 내지 않는다."""
    env: list[dict[str, str]] = []
    if bp.endpoint:
        env.append({"name": endpoint_env, "value": bp.endpoint})
    if bp.auth != "none":
        env.append({"name": auth_env, "value": bp.auth})
    return env


def _backend_env(backend_apis: list[BackendAPI], refs: _SecretRefs) -> list[dict[str, str]]:
    """spec.env 를 돌려주고 자격증명 키는 refs 에 넣는다. backend 가 하나면 SPEC2OPENAPI_* 이고, 둘
    이상이면 backend 순서대로 DAEGAM_BACKEND_<i>_* 다. 한 Secret 의 여러 키를 받는 클러스터면
    자격증명 키를 모두 spec.secrets 로 넣는다. 아니면 예전 렌더링 그대로다: backend 하나의
    비밀번호만 spec.secrets 로 가고, 사용자 이름과 여러 backend 의 자격증명은 mcp 컨테이너 env 로
    간다. soap:header 값의 Secret 키는 자격증명 뒤에 같은 규칙으로 간다(T60). 그 칸이 없는
    backend 의 렌더는 그 칸이 생기기 전과 같다."""
    env: list[dict[str, str]] = []
    if len(backend_apis) > 1:
        for i, bp in enumerate(backend_apis):
            env.extend(_connection_env(bp, backend_env(i, "endpoint"), backend_env(i, "auth")))
            if bp.has_credentials:
                for cred_key in ("username", "password"):
                    refs.add(
                        backend_env(i, cred_key),
                        bp.credential_secret,
                        cred_key,
                        prefer_secrets=refs.multi_key,
                    )
            if bp.soap_headers_secret:
                refs.add(
                    backend_env(i, SOAP_HEADERS_KEY),
                    bp.soap_headers_secret,
                    SOAP_HEADERS_KEY,
                    prefer_secrets=refs.multi_key,
                )
        return env
    backend_api = backend_apis[0]
    env.extend(_connection_env(backend_api, SINGLE_ENDPOINT_ENV, SINGLE_AUTH_ENV))
    if backend_api.has_credentials:
        # 백엔드 자격증명 Secret 의 키는 username / password 다 (Backend API 문서 규약)
        refs.add(SINGLE_PASSWORD_ENV, backend_api.credential_secret, "password")
        refs.add(SINGLE_USERNAME_ENV, backend_api.credential_secret, "username")
    if backend_api.soap_headers_secret:
        refs.add(SINGLE_SOAP_HEADERS_ENV, backend_api.soap_headers_secret, SOAP_HEADERS_KEY)
    return env


def _timeout_env(profile_rev: MCPServerRevision) -> list[dict[str, str]]:
    """backend 가 하나면 그 타임아웃을 `SPEC2OPENAPI_TIMEOUT` 으로 낸다. 런타임의 기본값과 같으면
    내지 않아, 기본값을 쓰는 서버의 CR 은 이 필드가 생기기 전과 같다. 둘 이상이면 profile.json 의
    backends 항목이 나른다 (`services/mcp_spec.py` `render_profile_json`)."""
    if len(profile_rev.backends) != 1:
        return []
    seconds = profile_rev.backends[0].timeout_seconds
    if seconds == MCP_DEFAULT_TIMEOUT_SECONDS:
        return []
    return [{"name": SINGLE_TIMEOUT_ENV, "value": str(seconds)}]


def _fetch_spec_init(
    inst: MCPServer, profile_rev: MCPServerRevision, base: str
) -> tuple[str, list[dict[str, Any]]]:
    """(initContainer 스크립트, 그 env). backend 가 하나면 openapi.yaml 하나와 토큰 env
    SPEC_TOKEN(키 token), 둘 이상이면 backend 순서대로 openapi-<i>.yaml 과 SPEC_TOKEN_<i>(키
    token-<i>) 다. profile.json 은 항상 하나이고 첫 backend 의 토큰으로 받는다."""
    k8s = _k8s(inst)
    n = len(profile_rev.backends)
    token_envs = [f"SPEC_TOKEN{_suffix(i, n, '_')}" for i in range(n)]
    parts = ["set -e"]
    for i, sel in enumerate(profile_rev.backends):
        parts.append(
            curl_fetch(
                token_envs[i],
                openapi_url(base, sel.backend_profile_id, sel.spec_version),
                f"openapi{_suffix(i, n, '-')}.yaml",
            )
        )
    parts.append(
        curl_fetch(
            token_envs[0], profile_json_url(base, inst.id, profile_rev.revision), "profile.json"
        )
    )
    init_env = [
        secret_env(env, k8s.token_secret, token_key(i, n)) for i, env in enumerate(token_envs)
    ]
    return "; ".join(parts), init_env


def render_mcpserver(
    inst: MCPServer,
    effective: EffectiveConfig,
    profile_rev: MCPServerRevision,
    backend_apis: list[BackendAPI],
    cluster: Cluster,
    data_services: dict[str, DataService],
    *,
    features: frozenset[Feature],
    fence: dict[str, Any] | None = None,
    server_id: str | None = None,
    session_tls: bool = False,
    default_redis_ca: bool = False,
    redis_ca: Mapping[str, str | None] | None = None,
) -> dict[str, Any]:
    """ToolHive MCPServer CR. backend_apis 는 리비전 backends 순서와 정렬,
    data_services 는 리비전 바인딩의 data_service_id → DataService. profile_rev 가 핀된 리비전이고
    라벨 `daegam.devops.ai.kr/profile-revision` 과 profile.json URL 이 그 번호를 담는다. 자기
    audience 로 옮겨 간 서버(`inst.audience_switch`, P50 9절)는 그 서버 하나의 audience 만 받는다.
    server_id 는 그 audience 의 id 이고 기본은 `inst.id` 다(미리보기는 다른 id 의 견본으로
    렌더한다).

    backend 가 하나면 SPEC2OPENAPI_* env 와 Secret 키 token 이다. 둘 이상이면
    endpoint·auth·자격증명을 backend 순서대로 DAEGAM_BACKEND_<i>_* 로 주입한다. 배포 스냅샷에 CA
    묶음이 있으면 `CA_ENV` 로 싣고, 없으면 그 env 가 없다. features 는 서비스가
    한 번 계산한, 이 클러스터가 지나는 게이트다(`passing_features`). 자격증명과 바인딩 비밀번호가
    spec.secrets 로 가는지 mcp 컨테이너 env 로 가는지는 `multi_key_secrets` 게이트가
    정한다(`_SecretRefs`). fence 는 테넌트 울타리의 spec 칸이고(`tenant_fence`, P50 11절) 게이트를
    지나지 못한 클러스터는 None 이다. session_tls·default_redis_ca 는 세션 저장소의 TLS 를 서비스가
    정한 값이다(`_session_storage`, P61). redis_ca 는 TLS 로 붙을 수 있는 Redis 의 지금 CA
    지문이고(출처는 Database id 나 `DEFAULT_REDIS_SOURCE`), TLS 로 붙은 출처만 파드 템플릿의
    `REDIS_CA_ANNOTATION` 이 된다. 다시 렌더는 바인딩을 스냅샷에서 읽지만 CA 는 지금 것을 믿어야
    하기 때문이다."""
    if len(backend_apis) != len(profile_rev.backends):
        raise RenderError("integrity_error", "backend_apis must match revision backends")
    k8s = _k8s(inst)
    labels = daegam_labels(tenant=inst.tenant, resource="mcp-instance", id=inst.id)
    labels[LABEL_PROFILE_REVISION] = str(profile_rev.revision)

    refs = _SecretRefs(multi_key=Feature.MULTI_KEY_SECRETS in features)
    env = _backend_env(backend_apis, refs)
    env.extend(_timeout_env(profile_rev))
    if effective.ca_bundle:
        env.append(ca_env(CA_ENV, effective.ca_bundle))

    bound: list[tuple[str, DataService]] = []
    ca_volumes: list[tuple[dict[str, Any], dict[str, Any]]] = []
    ca_fingerprints: dict[str, str | None] = {}
    current_ca = redis_ca or {}
    for b in profile_rev.bindings:
        ds = data_services.get(b.data_service_id)
        if ds is None:
            raise RenderError("unknown_data_service", f"data service {b.data_service_id} missing")
        bound.append((b.env_prefix, ds))
        # 바인딩은 Database 가 TLS 끝점을 가지면 그것을 쓴다 (P61 3절). 스냅샷을 다시 렌더하는
        # 배포는 그 배포가 읽은 끝점 그대로다
        b_env, b_secrets = binding_env_and_secrets(b.env_prefix, ds, allow_tls=True)
        env.extend(b_env)
        # 한 Database 의 둘째 바인딩은 같은 (Secret, 키)라 mcp 컨테이너 env 로 간다
        for s in b_secrets:
            refs.add(s["targetEnvName"], s["name"], s["key"])
        ca = binding_ca_volume(b.env_prefix, ds, allow_tls=True)
        if ca is not None:
            ca_volumes.append(ca)
            ca_fingerprints[b.data_service_id] = current_ca.get(b.data_service_id)

    fetch_script, init_env = _fetch_spec_init(inst, profile_rev, effective.platform_internal_url)
    mcp_container: dict[str, Any] = {
        "name": "mcp",
        "imagePullPolicy": effective.images.pull_policy,
    }
    if refs.pod_env:
        mcp_container["env"] = refs.pod_env
    if effective.resources:
        mcp_container["resources"] = effective.resources
    pod_spec = config_fetch_pod(
        init_name=FETCH_INIT,
        volume=SPEC_VOLUME,
        init_env=init_env,
        script=fetch_script,
        main=mcp_container,
        effective=effective,
        extra_volumes=tuple(ca_volumes),
    )

    spec: dict[str, Any] = {
        "image": effective.images.runtime_image,
        "transport": "streamable-http",
        "proxyPort": PROXY_PORT,
        "mcpPort": MCP_PORT,
        "groupRef": {"name": k8s.group},
        "replicas": effective.replicas,
        "audit": {"enabled": True},  # 프록시 stdout 으로 감사 이벤트 (P6 F5)
        # 내부 홉 인증 (D41). 프록시가 플랫폼이 낸 서비스 토큰을 검증하고 audience 로 테넌트를
        # 가른다. Cedar 는 울타리(fence)뿐이다
        "oidcConfigRef": internal_oidc_config_ref(
            inst.tenant,
            proxy_service_url(k8s.name, k8s.namespace),
            switched_server_id(inst.audience_switch, server_id or inst.id),
        ),
        "podTemplateSpec": {"metadata": {"labels": dict(labels)}, "spec": pod_spec},
    }
    if fence:
        spec.update(fence)
    if env:
        spec["env"] = env
    if refs.secrets:
        spec["secrets"] = refs.secrets
    # ToolHive 는 spec.resources 를 MCP 컨테이너가 아니라 프록시 Deployment 에 넣는다. MCP
    # 컨테이너의 자원은 위 podTemplateSpec 에 있다 (D54)
    spec["resources"] = proxy_resources()
    if effective.replicas >= SESSION_REPLICAS:
        spec["sessionStorage"] = _session_storage(
            inst,
            effective,
            bound,
            cluster,
            session_tls=session_tls,
            default_redis_ca=default_redis_ca,
        )
        session_ca = (spec["sessionStorage"].get("tls") or {}).get("caCertSecretRef") or {}
        if session_ca.get("name") == DEFAULT_REDIS_CA_SECRET:
            ca_fingerprints[DEFAULT_REDIS_SOURCE] = current_ca.get(DEFAULT_REDIS_SOURCE)
    # 바인딩과 세션 저장소가 믿는 CA 가 바뀌면 MCP 파드가 다시 뜬다. 스펙이 바뀌므로 프록시도
    # 다시 뜬다(오퍼레이터가 CR 세대를 프록시 파드 템플릿에 남긴다)
    if (ca_mark := redis_ca_mark(ca_fingerprints)) is not None:
        spec["podTemplateSpec"]["metadata"]["annotations"] = {REDIS_CA_ANNOTATION: ca_mark}
    mark_mcp_pods(spec, k8s.name)

    return {
        "apiVersion": TOOLHIVE_API,
        "kind": "MCPServer",
        "metadata": {"name": k8s.name, "namespace": k8s.namespace, "labels": labels},
        "spec": spec,
    }
