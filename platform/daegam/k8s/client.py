"""Cluster 리소스 → kubernetes dynamic client 래퍼. 모든 쓰기는 SSA(field manager `daegam`)다.

kubernetes 클라이언트는 동기라 여기 메서드도 동기다. 비동기 핸들러는 run_in_threadpool 로 부른다.
예외는 데이터 플레인 Service 로 보내는 HTTP(`service_request`)로, httpx2 비동기 클라이언트를 쓴다.
"""

from __future__ import annotations

import base64
import contextlib
import json as jsonlib
import logging
import os
import re
import ssl
import tempfile
import threading
import time
import weakref
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Literal, Protocol, runtime_checkable
from urllib.parse import quote, urlsplit

import httpx2
import urllib3
from kubernetes import client as k8s
from kubernetes import config as k8s_config
from kubernetes.dynamic import DynamicClient
from kubernetes.dynamic.exceptions import (
    ConflictError,
    DynamicApiError,
    ForbiddenError,
    NotFoundError,
    ResourceNotFoundError,
    UnprocessibleEntityError,
)

from daegam.errors import ServiceError
from daegam.k8s.kubeconfig import KubeconfigRejected, load_kubeconfig
from daegam.k8s.labels import label_selector
from daegam.ttl_cache import TTLCache

if TYPE_CHECKING:
    from daegam.secrets import SecretStore
    from daegam.store.clusters import Cluster

logger = logging.getLogger("daegam.k8s")

FIELD_MANAGER = "daegam"
# ToolHive CRD 의 API 그룹과 판. 렌더(k8s/templates/common.py)·preflight·Strict 적용이 함께 쓴다
TOOLHIVE_GROUP = "toolhive.stacklok.dev"
TOOLHIVE_API = f"{TOOLHIVE_GROUP}/v1beta1"
RBAC_GROUP = "rbac.authorization.k8s.io"
RBAC_API = f"{RBAC_GROUP}/v1"
CLIENT_CACHE_TTL = 300.0  # 5분
SERVICE_REQUEST_TIMEOUT = 30.0  # service_request 기본 (connect·write·pool). read 는 호출부가 정한다
# `kubectl rollout restart` 가 쓰는 파드 템플릿 어노테이션이다. 값이 바뀌면 Deployment 가 굴러간다
RESTARTED_AT = "kubectl.kubernetes.io/restartedAt"
# 막 만든 RoleBinding 의 권한이 API 서버의 인가에 닿기까지 다시 해 보는 횟수와 간격
# (P50 2절, P63 2절)
RBAC_GRANT_RETRIES = 5
RBAC_GRANT_DELAY_SECONDS = 0.5


class ClusterUnreachable(Exception):
    """클라이언트를 만들 수 없거나 API 서버에 닿지 못했다."""


class ServiceTimeout(ClusterUnreachable):
    """데이터 플레인 Service 가 제한 시간 안에 답하지 않았다."""


class ClusterTimeout(ClusterUnreachable):
    """API 서버가 제한 시간 안에 연결을 받지 않았거나 답하지 않았다 (P65). 연결할 수 없는 클러스터와
    같게 502 cluster_unreachable 로 나간다."""


class ResourceNotServed(ClusterUnreachable):
    """API 서버가 그 apiVersion/kind 를 서빙하지 않는다(CRD 없음). 서버는 답했으므로 클러스터가
    내려간 것은 아니다(`cluster_down` 이 세지 않는다)."""


# API 서버로 가는 요청 하나의 기본 시한(초). (connect, read) 다 (P65 2절). 읽기는 30초로
# SERVICE_REQUEST_TIMEOUT 과 같은 값이다. 상태를 바꾸는 요청(POST·PUT·PATCH·DELETE)은 변경 웹훅이
# 차례로 붙을 수 있어 읽기 45초다. 시한을 직접 준 호출은 그 값을 쓴다
API_CONNECT_TIMEOUT = 10.0
API_READ_TIMEOUT = 30.0
API_WRITE_READ_TIMEOUT = 45.0
API_REQUEST_TIMEOUT = (API_CONNECT_TIMEOUT, API_READ_TIMEOUT)
API_WRITE_TIMEOUT = (API_CONNECT_TIMEOUT, API_WRITE_READ_TIMEOUT)
_READ_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})


class _ApiRetry(urllib3.util.Retry):
    """API 서버 요청의 재시도. 무엇이든 한 번까지이고 읽기 시한은 다시 하지 않는다. urllib3 의
    기본(세 번, GET·PUT·DELETE 의 읽기 오류 포함)이면 답하지 않는 서버를 시한의 네 배까지 기다린다.
    끊긴 풀 연결(ProtocolError)은 지금처럼 한 번 다시 한다."""

    def increment(self, *args: Any, **kwargs: Any) -> urllib3.util.Retry:
        error = kwargs.get("error")
        if isinstance(error, urllib3.exceptions.ReadTimeoutError):
            raise error
        return super().increment(*args, **kwargs)


API_RETRY = _ApiRetry(total=1)


def _default_timeout(method: str) -> tuple[float, float]:
    return API_REQUEST_TIMEOUT if method.upper() in _READ_METHODS else API_WRITE_TIMEOUT


def _timeout_of(e: BaseException) -> urllib3.exceptions.TimeoutError | None:
    """urllib3 의 연결·읽기 시한 오류. 연결 재시도를 다 쓰면 MaxRetryError 가 그것을 감싼다."""
    if isinstance(e, urllib3.exceptions.MaxRetryError):
        e = e.reason  # type: ignore[assignment]
    return e if isinstance(e, urllib3.exceptions.TimeoutError) else None


def exception_chain(e: BaseException) -> Iterator[BaseException]:
    """예외와 그것이 감싼 사슬(__cause__, 없으면 __context__). 라이브러리가 시한 오류를 다른 예외로
    감싸도 찾아낼 때 쓴다."""
    seen: set[int] = set()
    cur: BaseException | None = e
    while cur is not None and id(cur) not in seen:
        yield cur
        seen.add(id(cur))
        cur = cur.__cause__ or cur.__context__


def _cluster_timeout(
    what: str, timeout: urllib3.exceptions.TimeoutError, limit: Any
) -> ClusterTimeout:
    """연결 시한인지 읽기 시한인지와 그 시한을 적은 ClusterTimeout."""
    connect = isinstance(timeout, urllib3.exceptions.ConnectTimeoutError)
    if isinstance(limit, tuple):
        limit = limit[0] if connect else limit[1]
    phase = "connect" if connect else "read"
    return ClusterTimeout(f"{what}: {phase} timed out after {limit} s")


# 요청마다 먼저 부르는 훅 (P66 2.2절). 서비스 층이 등록하므로 K8s 층은 스토어를 모른다. 훅은 쥔
# 리스를 이어 갈 수 없으면 `LeaseStop` 을 올려 요청을 보내지 않는다
_request_hook: Callable[[], None] | None = None


def set_request_hook(hook: Callable[[], None] | None) -> None:
    """모든 클러스터 요청 앞에서 부를 훅을 건다. 하나뿐이고 다시 부르면 바꾼다."""
    global _request_hook
    _request_hook = hook


def bound_requests(api_client: k8s.ApiClient) -> None:
    """이 ApiClient 로 가는 모든 요청에 기본 시한과 재시도 정책을 건다 (P65 2절). 동적 클라이언트,
    타입 API, 디스커버리, pods/log 가 모두 REST 클라이언트의 request 하나를 지나므로 거기 한 번
    건다. 호출부는 시한을 기억하지 않아도 된다. 동적 클라이언트는 `_request_timeout=None` 을 늘
    넘기므로 None 도 기본을 쓴다는 뜻이다. ApiClient.request 가 아니라 REST 쪽인 까닭은 kubernetes
    stream 모듈이 터널을 여는 ApiClient 의 request 를 바꿔 끼우기 때문이다. 터널은 ApiClient
    사본으로 연다(`portforward._tunnel_api_client`). 시한 오류는 ClusterTimeout 으로 올린다.

    P66·P67 의 리스 훅도 따로 감싸지 않고 이 래퍼에 넣는다."""
    rest = getattr(api_client, "rest_client", None)
    if rest is None or getattr(rest, "_daegam_bounded", False):
        return
    # 풀은 첫 요청에 생기므로 그 전에 바꾼 kw 가 모든 연결에 든다
    rest.pool_manager.connection_pool_kw["retries"] = API_RETRY
    send = rest.request

    def request(method: str, url: str, *args: Any, **kwargs: Any) -> Any:
        hook = _request_hook
        if hook is not None:
            hook()
        if kwargs.get("_request_timeout") is None:
            kwargs["_request_timeout"] = _default_timeout(method)
        try:
            return send(method, url, *args, **kwargs)
        except Exception as e:
            timeout = _timeout_of(e)
            if timeout is None:
                raise
            raise _cluster_timeout(
                f"{method} {urlsplit(url).path}", timeout, kwargs["_request_timeout"]
            ) from e

    rest.request = request
    rest._daegam_bounded = True


class AccessDenied(Exception):
    """API 서버가 403 을 돌려줬다. `missing` 은 화면에 그대로 보여 줄
    `resource:verb[@namespace]` 다."""

    def __init__(self, verb: str, resource: str, namespace: str | None = None, detail: str = ""):
        self.verb = verb
        self.resource = resource
        self.namespace = namespace
        self.missing = f"{resource}:{verb}" + (f"@{namespace}" if namespace else "")
        super().__init__(detail or f"forbidden: {self.missing}")


def is_forbidden(e: BaseException) -> bool:
    """API 서버의 403 인가. 동적 클라이언트의 ForbiddenError(status 403)와 이 모듈의 AccessDenied
    둘 다다."""
    return isinstance(e, AccessDenied) or getattr(e, "status", None) == 403


def retry_forbidden[T](
    call: Callable[[], T],
    *,
    retries: int = RBAC_GRANT_RETRIES,
    delay: float = RBAC_GRANT_DELAY_SECONDS,
) -> T:
    """막 만든 RoleBinding 이 준 권한에 기대는 호출이다. 403 이면 API 서버의 인가가 그 권한을 받을
    때까지 잠깐씩 기다렸다 다시 하고, 끝내 403 이면 그 오류를 그대로 올린다. 다른 오류는 바로
    올린다. 바인딩을 어느 레플리카가 만들었든 같다."""
    for attempt in range(retries):
        try:
            return call()
        except Exception as e:
            if not is_forbidden(e) or attempt == retries - 1:
                raise
            time.sleep(delay)
    raise AssertionError("retries must be at least 1")


def _secret_call[T](kind: str, call: Callable[[], T]) -> T:
    """Secret 의 읽기와 쓰기는 403 을 잠깐 다시 해 본다 (P63 2절). 테넌트 네임스페이스의 Secret
    권한이 RoleBinding daegam-tenant-objects 로 오므로, 막 만든 바인딩이 인가에 닿기 전의 403 이
    있다. 다른 kind 는 한 번이다."""
    return retry_forbidden(call) if kind == "Secret" else call()


# PartialObjectMetadataList: 목록의 metadata 만 받는다. Secret 을 이 Accept 로 나열하면 data 가
# 응답에 실리지 않아 개인키가 플랫폼 프로세스에 들어오지 않는다 (D22)
PARTIAL_METADATA_ACCEPT = "application/json;as=PartialObjectMetadataList;g=meta.k8s.io;v=v1"
SELF_SUBJECT_REVIEW_PATH = "/apis/authentication.k8s.io/v1/selfsubjectreviews"


def rbac_subject(user_info: dict[str, Any]) -> dict[str, Any]:
    """SelfSubjectReview userInfo → RoleBinding subject. `system:serviceaccount:<ns>:<name>` 은
    ServiceAccount, 그 밖(kubecontext 의 kubeconfig User)은 User 다."""
    username = str(user_info.get("username") or "")
    parts = username.split(":")
    if len(parts) == 4 and parts[0] == "system" and parts[1] == "serviceaccount":
        return {"kind": "ServiceAccount", "name": parts[3], "namespace": parts[2]}
    return {"kind": "User", "name": username, "apiGroup": RBAC_GROUP}


def platform_subject(client: KubeClient) -> dict[str, Any] | None:
    """클러스터 클라이언트 자신의 identity(SelfSubjectReview)를 RoleBinding subject 로 바꾼 것이다.
    플랫폼이 테넌트 네임스페이스에 두는 RoleBinding(daegam-tls-reader, daegam-tenant-objects)이
    이것을 묶고, preflight 는 그 바인딩이 이것을 묶는지 본다. 얻지 못하면 None 이라 이번에는 그
    RoleBinding 을 만들지 않고, preflight 는 바인딩이 가리키는 역할만 본다."""
    try:
        return rbac_subject(client.whoami())
    except Exception as e:  # noqa: BLE001 - 권한·연결 문제는 노출 옵션 조회와 preflight 가 드러낸다
        logger.warning("selfsubjectreview failed; the platform's subject is unknown: %s", e)
        return None


# -- 데이터 플레인 Service 로 가는 HTTP 경로 (D21) --------------------------------------

ServiceVia = Literal["direct", "apiserver"]


@dataclass
class ServiceRoute:
    """클러스터 안 Service 하나에 닿는 방법. `direct` 는 svc DNS(플랫폼이 그 클러스터 안에 있을 때),
    `apiserver` 는 K8s API 서버의 services/proxy 서브리소스(원격 클러스터, kubecontext 로컬 개발)다.
    호출부는 base_url 아래 경로만 붙이고 headers·verify·cert 를 그대로 실어 보내면 된다."""

    base_url: str
    via: ServiceVia
    headers: dict[str, str] = field(default_factory=dict)
    verify: bool | str = True  # CA 파일 경로 또는 True/False
    cert: tuple[str, str] | None = None  # 클라이언트 인증서 (kubeconfig 의 client-certificate)
    # 테스트가 가짜 데이터 플레인(ASGI 앱)을 꽂는 자리. 있으면 http_client 가 이 transport 를 쓴다
    transport: Any = None

    def url(self, path: str) -> str:
        return f"{self.base_url}/{path.lstrip('/')}" if path else self.base_url

    def _ssl(self) -> ssl.SSLContext | bool:
        if self.base_url.startswith("http://"):
            return True
        if self.verify is False:
            return False
        cafile = self.verify if isinstance(self.verify, str) else None
        ctx = ssl.create_default_context(cafile=cafile)
        if self.cert:
            ctx.load_cert_chain(self.cert[0], self.cert[1])
        return ctx

    def http_client(
        self, *, timeout: float = SERVICE_REQUEST_TIMEOUT, read_timeout: float | None = None
    ) -> httpx2.AsyncClient:
        """이 경로로 가는 httpx2 클라이언트. mcp 의 streamable_http_client(http_client=…) 에
        넘기거나 에이전트 SSE 프록시가 스트림으로 쓴다. read_timeout=None 은 읽기 무제한이 아니라
        timeout 과 같다. 무제한이 필요하면 read_timeout 에 큰 값을 준다."""
        kwargs: dict[str, Any] = {
            "headers": dict(self.headers),
            "timeout": httpx2.Timeout(timeout, read=read_timeout or timeout),
            "follow_redirects": True,
        }
        if self.transport is not None:
            kwargs["transport"] = self.transport
        else:
            kwargs["verify"] = self._ssl()
        return httpx2.AsyncClient(**kwargs)


@dataclass
class ServiceResponse:
    status: int
    headers: dict[str, str]
    body: bytes

    def json(self) -> Any:
        # 같은 파일 send_service_request 의 파라미터 이름 json 이 모듈을 가리므로 별칭으로 쓴다
        try:
            return jsonlib.loads(self.body.decode("utf-8")) if self.body else None
        except ValueError:
            return None


def parse_service_url(url: str) -> tuple[str, str, int, str] | None:
    """`http://<svc>.<ns>.svc[.cluster.local]:<port>/<path>` → (namespace, service, port, path).
    그 모양이 아니면 None (외부 URL 은 그대로 부른다)."""
    parts = urlsplit(url)
    host = (parts.hostname or "").lower()
    labels = host.split(".")
    if len(labels) == 3 and labels[2] == "svc":
        svc, ns = labels[0], labels[1]
    elif len(labels) == 5 and labels[2:] == ["svc", "cluster", "local"]:
        svc, ns = labels[0], labels[1]
    else:
        return None
    if not svc or not ns:
        return None
    port = parts.port or (443 if parts.scheme == "https" else 80)
    path = parts.path or "/"
    if parts.query:
        path = f"{path}?{parts.query}"
    return ns, svc, port, path


def apiserver_proxy_route(
    configuration: k8s.Configuration,
    namespace: str,
    service: str,
    port: int,
    *,
    scheme: str = "http",
) -> ServiceRoute:
    """kubernetes Configuration 의 host·인증·CA 로 services/proxy 경로를 만든다."""
    if configuration.refresh_api_key_hook is not None:
        # exec 플러그인·토큰 파일 갱신. kubecontext 로컬 개발 전용이고 등록한 kubeconfig 는
        # 둘 다 받지 않는다
        configuration.refresh_api_key_hook(configuration)
    headers: dict[str, str] = {}
    token = (configuration.api_key or {}).get("authorization")
    if token:
        prefix = (configuration.api_key_prefix or {}).get("authorization")
        if prefix and not token.startswith(f"{prefix} "):
            token = f"{prefix} {token}"
        headers["Authorization"] = token
    host = configuration.host.rstrip("/")
    base = (
        f"{host}/api/v1/namespaces/{quote(namespace)}/services/"
        f"{scheme}:{quote(service)}:{port}/proxy"
    )
    verify: bool | str
    if not configuration.verify_ssl:
        verify = False
    else:
        verify = configuration.ssl_ca_cert or True
    cert = (
        (configuration.cert_file, configuration.key_file)
        if configuration.cert_file and configuration.key_file
        else None
    )
    return ServiceRoute(base_url=base, via="apiserver", headers=headers, verify=verify, cert=cert)


async def send_service_request(
    route: ServiceRoute,
    method: str,
    path: str,
    *,
    json: Any = None,
    headers: dict[str, str] | None = None,
    timeout: float = SERVICE_REQUEST_TIMEOUT,
    read_timeout: float | None = None,
) -> ServiceResponse:
    """route 로 요청 하나를 보내고 응답을 통째로 받는다 (스트리밍 없음)."""
    async with route.http_client(timeout=timeout, read_timeout=read_timeout) as hc:
        try:
            r = await hc.request(method, route.url(path), json=json, headers=headers)
        except httpx2.TimeoutException as e:
            raise ServiceTimeout(f"{method} {route.url(path)}: {type(e).__name__}") from e
        except httpx2.HTTPError as e:
            raise ClusterUnreachable(f"{method} {route.url(path)}: {type(e).__name__}: {e}") from e
    return ServiceResponse(status=r.status_code, headers=dict(r.headers), body=r.content)


# SSA 의 fieldValidation. Strict 면 API 서버가 스키마에 없는 필드를 버리지 않고 거부한다
FieldValidation = Literal["Ignore", "Warn", "Strict"]


def is_toolhive(obj: dict[str, Any]) -> bool:
    """ToolHive CR 인가. apiVersion 의 그룹으로 본다."""
    return str(obj.get("apiVersion") or "").startswith(TOOLHIVE_GROUP + "/")


@runtime_checkable
class KubeClient(Protocol):
    """ClusterClient 와 테스트 대역(tests/helpers/fake_cluster.py)이 공유하는 표면."""

    def apply(
        self,
        obj: dict[str, Any],
        *,
        dry_run: bool = False,
        field_validation: FieldValidation | None = None,
    ) -> dict[str, Any]: ...
    def create(self, obj: dict[str, Any]) -> bool: ...
    def dry_run_apply(self, obj: dict[str, Any]) -> dict[str, Any]: ...
    def get(
        self, api_version: str, kind: str, name: str, namespace: str | None = None
    ) -> dict[str, Any] | None: ...
    def delete(
        self,
        api_version: str,
        kind: str,
        name: str,
        namespace: str | None = None,
        *,
        grace_seconds: int | None = None,
        uid: str | None = None,
    ) -> bool: ...
    def rollout_restart(self, name: str, namespace: str) -> bool: ...
    def scale_deployment(self, name: str, namespace: str, replicas: int) -> int | None: ...
    def drop_secret_keys(self, name: str, namespace: str, keys: list[str]) -> bool: ...
    def merge_secret_keys(self, name: str, namespace: str, data: dict[str, str | None]) -> bool: ...
    def swap_secret_keys(
        self,
        name: str,
        namespace: str,
        expect: dict[str, str],
        data: dict[str, str | None],
    ) -> bool: ...
    def list(
        self,
        api_version: str,
        kind: str,
        namespace: str | None = None,
        labels: dict[str, str] | None = None,
    ) -> list[dict[str, Any]]: ...
    def list_metadata(
        self,
        api_version: str,
        kind: str,
        namespace: str | None = None,
        *,
        field_selector: str | None = None,
        labels: dict[str, str] | None = None,
    ) -> list[dict[str, Any]]: ...
    def version(self) -> dict[str, Any]: ...
    def can_i(
        self,
        verb: str,
        resource: str,
        group: str = "",
        namespace: str | None = None,
        name: str | None = None,
    ) -> bool: ...
    def can_user(
        self, user: str, verb: str, resource: str, namespace: str, group: str = ""
    ) -> bool: ...
    def whoami(self) -> dict[str, Any]: ...
    def discover(self, group_version: str) -> list[str]: ...
    def pod_log(
        self,
        name: str,
        namespace: str,
        *,
        since_seconds: int | None = None,
        container: str | None = None,
        limit_bytes: int | None = None,
    ) -> str: ...
    def service_route(
        self, namespace: str, service: str, port: int, *, scheme: str = "http"
    ) -> ServiceRoute: ...
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
    ) -> ServiceResponse: ...


def _to_dict(obj: Any) -> dict[str, Any]:
    if obj is None:
        return {}
    if isinstance(obj, dict):
        return obj
    if hasattr(obj, "to_dict"):
        return obj.to_dict()
    return dict(obj)


def _unlink_quietly(path: str | None) -> None:
    """임시 파일을 지운다. 없거나 지우지 못해도 조용히 끝낸다."""
    if path:
        with contextlib.suppress(OSError):
            os.unlink(path)


def _release_client(api_client: k8s.ApiClient, ca_file: str | None) -> None:
    """커넥션 풀을 닫고 임시 CA 파일을 지운다. weakref.finalize 가 부른다."""
    try:
        api_client.close()
    except Exception:  # noqa: BLE001 - 정리는 실패해도 조용히 끝낸다
        pass
    _unlink_quietly(ca_file)


class ClusterClient:
    def __init__(
        self,
        api_client: k8s.ApiClient,
        *,
        cluster_id: str = "",
        name: str = "",
        direct_services: bool = False,
        ca_file: str | None = None,
        strict_kinds: frozenset[str] = frozenset(),
    ):
        """direct_services: 이 클러스터의 Service 에 svc DNS 로 바로 닿는다 (플랫폼이 그 클러스터
        안에서 도는 in_cluster home). 아니면 API 서버 services/proxy 를 지난다 (D21).

        strict_kinds: fieldValidation=Strict 로 적용하는 ToolHive kind (P50 2절). 옛 CRD 가 모르는
        필드를 조용히 버리지 않고 거부하게 한다. 그 클러스터의 마지막 Strict dry run 에서 그 kind 의
        문서가 모두 통과했을 때만 든다(`strict_kinds`)."""
        self.api_client = api_client
        self.cluster_id = cluster_id
        self.name = name
        self.direct_services = direct_services
        self.strict_kinds = strict_kinds
        # 디스커버리(DynamicClient 생성)보다 먼저 건다. 생성도 API 서버에 묻는다
        bound_requests(api_client)
        self.dyn = DynamicClient(api_client)
        # 임시 CA 파일과 커넥션 풀은 이 객체가 사라질 때 정리한다. 캐시 축출 시점에 바로 닫으면
        # 그 클라이언트로 아직 호출 중인 쪽이 끊긴다. GC 는 아무도 안 들고 있을 때만 온다
        self._cleanup = weakref.finalize(self, _release_client, api_client, ca_file)

    # -- 생성 ------------------------------------------------------------

    @classmethod
    def from_cluster(
        cls,
        cluster: Cluster,
        secret_store: SecretStore | None,
        *,
        platform_namespace: str = "daegam-system",
    ) -> ClusterClient:
        conn = cluster.connection
        ca_file: str | None = None
        try:
            if conn.mode == "in_cluster":
                k8s_config.load_incluster_config()
                api_client = k8s.ApiClient()
            elif conn.mode == "kubecontext":
                api_client = k8s_config.new_client_from_config(
                    context=conn.kubecontext, persist_config=False
                )
            elif conn.mode == "kubeconfig":
                data = _read_secret(secret_store, conn.secret_name, platform_namespace)
                cfg = _kubeconfig_dict(data.get("kubeconfig"))
                api_client = k8s_config.new_client_from_config_dict(cfg, persist_config=False)
            elif conn.mode == "token":
                data = _read_secret(secret_store, conn.secret_name, platform_namespace)
                token = data.get("token")
                if not token or not conn.api_url:
                    raise ClusterUnreachable("token mode needs api_url and 'token' in secret")
                configuration = k8s.Configuration()
                configuration.host = conn.api_url
                configuration.api_key = {"authorization": f"Bearer {token}"}
                ca = data.get("ca_bundle")
                if ca:
                    # Configuration 은 파일 경로만 받는다. 파드에서는 /tmp 가 emptyDir 이어야 한다.
                    # urllib3 가 연결할 때마다 읽을 수 있으므로 클라이언트가 사는 동안 남겨 두고,
                    # 아래 finalizer(성공) 또는 except 절(실패)이 지운다
                    f = tempfile.NamedTemporaryFile("w", suffix=".crt", delete=False)
                    f.write(ca)
                    f.close()
                    ca_file = f.name
                    configuration.ssl_ca_cert = ca_file
                else:
                    # 검증을 끄고 붙으면 API 서버 자리에 선 누구에게나 SA 토큰을 준다. 등록 API 가
                    # ca_bundle 을 요구하므로 여기 오는 것은 손으로 만든 Secret 뿐이다
                    raise ClusterUnreachable(
                        "token mode needs 'ca_bundle' in the secret; "
                        "the platform never skips TLS verification"
                    )
                api_client = k8s.ApiClient(configuration)
            else:  # pragma: no cover - pydantic Literal 이 막는다
                raise ClusterUnreachable(f"unknown connection mode {conn.mode}")
            # DynamicClient 는 생성 시 /version 디스커버리를 하므로 여기서 연결 실패가 드러난다
            return cls(
                api_client,
                ca_file=ca_file,
                cluster_id=cluster.id,
                name=cluster.name,
                # svc DNS 는 파드 안에서만 풀린다. in_cluster 만 직행이고 나머지(kubecontext 로컬
                # 개발, kubeconfig·token 원격)는 API 서버 services/proxy 다
                direct_services=conn.mode == "in_cluster",
                strict_kinds=strict_kinds(cluster),
            )
        except Exception as e:  # noqa: BLE001 - kubeconfig 파싱·로딩·연결 오류 전부
            # 클라이언트를 못 만들었으니 finalizer 도 없다. 임시 CA 파일은 여기서 지운다
            _unlink_quietly(ca_file)
            if isinstance(e, ClusterUnreachable):
                raise
            raise ClusterUnreachable(f"{type(e).__name__}: {e}") from e

    # -- 리소스 접근 --------------------------------------------------------

    def _resource(self, api_version: str, kind: str):
        try:
            return self.dyn.resources.get(api_version=api_version, kind=kind)
        except ResourceNotFoundError as e:
            raise ResourceNotServed(f"resource {api_version}/{kind} not served") from e

    def apply(
        self,
        obj: dict[str, Any],
        *,
        dry_run: bool = False,
        field_validation: FieldValidation | None = None,
    ) -> dict[str, Any]:
        """SSA. field_validation 을 주지 않으면 이 클러스터의 Strict dry run 을 통과한 kind 의
        ToolHive CR 만 Strict 다. 동적 클라이언트는 fieldValidation 을 모르므로 query_params 로
        싣는다."""
        meta = obj.get("metadata") or {}
        name = meta.get("name")
        if not obj.get("apiVersion") or not obj.get("kind") or not name:
            raise ValueError("apply needs apiVersion, kind and metadata.name")
        resource = self._resource(obj["apiVersion"], obj["kind"])
        kwargs: dict[str, Any] = {}
        if dry_run:
            kwargs["dry_run"] = "All"
        if field_validation is None and is_toolhive(obj) and obj["kind"] in self.strict_kinds:
            field_validation = "Strict"
        if field_validation is not None:
            kwargs["query_params"] = [("fieldValidation", field_validation)]
        res = _secret_call(
            obj["kind"],
            lambda: resource.server_side_apply(
                body=obj,
                name=name,
                namespace=meta.get("namespace"),
                field_manager=FIELD_MANAGER,
                force_conflicts=True,
                **kwargs,
            ),
        )
        return _to_dict(res)

    def create(self, obj: dict[str, Any]) -> bool:
        """있으면 만들지 않는다(create-if-absent). 이미 있으면 False 다.

        SSA 는 무조건 덮어쓰므로 "먼저 쓴 쪽이 이긴다" 가 필요한 자리(local 서명 키, D40)는 이것을
        쓴다."""
        meta = obj.get("metadata") or {}
        resource = self._resource(obj["apiVersion"], obj["kind"])
        try:
            _secret_call(
                obj["kind"], lambda: resource.create(body=obj, namespace=meta.get("namespace"))
            )
        except k8s.ApiException as e:
            if e.status == 409:
                return False
            raise
        return True

    def dry_run_apply(self, obj: dict[str, Any]) -> dict[str, Any]:
        return self.apply(obj, dry_run=True)

    def get(
        self, api_version: str, kind: str, name: str, namespace: str | None = None
    ) -> dict[str, Any] | None:
        resource = self._resource(api_version, kind)
        try:
            found = _secret_call(kind, lambda: resource.get(name=name, namespace=namespace))
            return _to_dict(found)
        except NotFoundError:
            return None
        except ForbiddenError as e:
            if self._namespace_gone(namespace):
                return None
            # list·list_metadata 와 같게 403 은 AccessDenied 다 (호출부가 403 forbidden 으로 낸다)
            raise AccessDenied("get", resource.name, namespace, str(e)) from e

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
        """지운다. 없으면 False 다. grace_seconds 는 파드의 종료 유예를 이 값으로 줄인다. uid 를
        주면 그 uid 의 객체일 때만 지운다(전제 조건). 그사이 같은 이름의 다른 객체로 바뀌었으면
        API 서버가 409 로 답하고 False 다 (P55 5절, stdio 항목의 프록시 파드)."""
        resource = self._resource(api_version, kind)
        opts: dict[str, Any] = {}
        if grace_seconds is not None:
            opts["gracePeriodSeconds"] = grace_seconds
        if uid is not None:
            opts["preconditions"] = {"uid": uid}
        body = opts or None

        def call() -> None:
            _secret_call(kind, lambda: resource.delete(name=name, namespace=namespace, body=body))

        try:
            try:
                call()
            except DynamicApiError as e:
                if e.status < 500:
                    raise
                # API 서버가 지우고도 5xx 로 답하는 때가 있다. 오퍼레이터가 같은 순간에 status 를
                # 쓰면 `unexpected end of JSON input` 500 이 오고, 바로 읽으면 이미 404 다. 지우기는
                # 멱등이므로 한 번 다시 지워 본다. 그때 404 면 앞의 지우기가 지운 것이다
                try:
                    call()
                except NotFoundError:
                    return True
        except NotFoundError:
            return False
        except ConflictError:
            if uid is None:
                raise
            return False
        except ForbiddenError:
            if self._namespace_gone(namespace):
                return False
            raise
        return True

    def _namespace_gone(self, namespace: str | None) -> bool:
        """네임스페이스 안 객체의 403 이 네임스페이스가 없어서인가. 테넌트 권한은 그 네임스페이스의
        RoleBinding 으로 오므로(P63), 밖에서 지운 네임스페이스의 객체는 404 가 아니라 403 이다.
        네임스페이스 읽기는 클러스터 전체에 있다. 읽기가 실패하면 모르는 것이라 False 다."""
        if namespace is None:
            return False
        try:
            self._resource("v1", "Namespace").get(name=namespace)
        except NotFoundError:
            return True
        except Exception:  # noqa: BLE001 - 403·연결 오류는 판정하지 않는다
            return False
        return False

    def rollout_restart(self, name: str, namespace: str) -> bool:
        """Deployment 를 다시 띄운다. `kubectl rollout restart` 처럼 파드 템플릿의 `restartedAt`
        어노테이션만 패치하므로 오퍼레이터가 쓴 다른 필드는 건드리지 않는다. 없으면 False 다."""
        stamp = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        body = {"spec": {"template": {"metadata": {"annotations": {RESTARTED_AT: stamp}}}}}
        return self._patch_deployment(name, namespace, body) is not None

    def scale_deployment(self, name: str, namespace: str, replicas: int) -> int | None:
        """Deployment 의 `spec.replicas` 하나만 바꾸고 그 세대를 돌려준다 (P44 9절). 부분 SSA 는
        같은 manager 가 빼놓은 필드를 지우므로 쓰지 않는다. 없으면 None 이다."""
        patched = self._patch_deployment(name, namespace, {"spec": {"replicas": replicas}})
        if patched is None:
            return None
        generation = (patched.get("metadata") or {}).get("generation")
        return int(generation) if generation is not None else None

    def _patch_deployment(
        self, name: str, namespace: str, body: dict[str, Any]
    ) -> dict[str, Any] | None:
        """Deployment 에 플랫폼 field manager 의 strategic merge patch 하나를 보낸다. body 에 든
        필드만 바뀐다. 패치한 객체이고, 없으면 None 이다."""
        resource = self._resource("apps/v1", "Deployment")
        try:
            res = resource.patch(
                body=body,
                name=name,
                namespace=namespace,
                content_type="application/strategic-merge-patch+json",
                field_manager=FIELD_MANAGER,
            )
        except NotFoundError:
            return None
        except ForbiddenError as e:
            raise AccessDenied("patch", resource.name, namespace, str(e)) from e
        return _to_dict(res)

    def drop_secret_keys(self, name: str, namespace: str, keys: list[str]) -> bool:
        """Secret 에서 키를 이름으로 뺀다. SSA 는 stringData 로 넣었던 키를 다음 적용에서 빼도
        data 에 남기므로 merge patch 의 null 로 지운다. Secret 이 없으면 False 다."""
        return self.merge_secret_keys(name, namespace, dict.fromkeys(keys))

    def merge_secret_keys(self, name: str, namespace: str, data: dict[str, str | None]) -> bool:
        """Secret 의 키를 merge patch 하나로 더하거나 바꾸고(값) 뺀다(None). 다른 키는 두므로 여러
        요청이 같은 Secret 의 서로 다른 키를 함께 고쳐도 잃는 것이 없다. Secret 이 없으면
        False 다."""
        resource = self._resource("v1", "Secret")
        body = {k: (None if v is None else encode_secret_data({k: v})[k]) for k, v in data.items()}
        try:
            _secret_call(
                "Secret",
                lambda: resource.patch(
                    body={"data": body},
                    name=name,
                    namespace=namespace,
                    content_type="application/merge-patch+json",
                    field_manager=FIELD_MANAGER,
                ),
            )
        except NotFoundError:
            return False
        except ForbiddenError as e:
            raise AccessDenied("patch", resource.name, namespace, str(e)) from e
        return True

    def swap_secret_keys(
        self,
        name: str,
        namespace: str,
        expect: dict[str, str],
        data: dict[str, str | None],
    ) -> bool:
        """`expect` 의 키가 모두 그 값일 때만 키를 바꾸고(값) 뺀다(None). JSON Patch 하나의 test 라
        그 사이에 다른 쓰기가 끼지 못한다. 값이 다르거나 뺄 키가 없거나 Secret 이 없으면 False
        다."""
        resource = self._resource("v1", "Secret")
        ops: list[dict[str, Any]] = [
            {"op": "test", "path": _data_path(k), "value": _encoded(v)} for k, v in expect.items()
        ]
        for key, value in data.items():
            if value is None:
                ops.append({"op": "remove", "path": _data_path(key)})
            else:
                ops.append({"op": "add", "path": _data_path(key), "value": _encoded(value)})
        try:
            _secret_call(
                "Secret",
                lambda: resource.patch(
                    body=ops,
                    name=name,
                    namespace=namespace,
                    content_type="application/json-patch+json",
                    field_manager=FIELD_MANAGER,
                ),
            )
        except (NotFoundError, UnprocessibleEntityError):
            # 실패한 test 와 없는 키의 remove 는 422 다
            return False
        except ForbiddenError as e:
            raise AccessDenied("patch", resource.name, namespace, str(e)) from e
        return True

    def list(
        self,
        api_version: str,
        kind: str,
        namespace: str | None = None,
        labels: dict[str, str] | None = None,
    ) -> list[dict[str, Any]]:
        resource = self._resource(api_version, kind)
        kwargs: dict[str, Any] = {}
        if labels:
            kwargs["label_selector"] = label_selector(labels)
        try:
            res = _to_dict(resource.get(namespace=namespace, **kwargs))
        except ForbiddenError as e:
            raise AccessDenied("list", resource.name, namespace, str(e)) from e
        return [_to_dict(i) for i in res.get("items") or []]

    def list_metadata(
        self,
        api_version: str,
        kind: str,
        namespace: str | None = None,
        *,
        field_selector: str | None = None,
        labels: dict[str, str] | None = None,
    ) -> list[dict[str, Any]]:
        """`Accept: PartialObjectMetadataList` 로 metadata 만 받는다 (본문 없음). 항목은
        `{"metadata": {...}}` 다. 403 은 AccessDenied 로 올린다 (화면에 빠진 verb 를 보여 준다)."""
        resource = self._resource(api_version, kind)
        params: dict[str, Any] = {
            "header_params": {"Accept": PARTIAL_METADATA_ACCEPT},
            "serializer": lambda _c, d: d,
        }
        if field_selector:
            params["field_selector"] = field_selector
        if labels:
            params["label_selector"] = label_selector(labels)
        try:
            data = self.dyn.request("get", resource.path(namespace=namespace), **params)
        except ForbiddenError as e:
            raise AccessDenied("list", resource.name, namespace, str(e)) from e
        items = (data or {}).get("items") if isinstance(data, dict) else None
        return [{"metadata": dict((i or {}).get("metadata") or {})} for i in items or []]

    def pod_log(
        self,
        name: str,
        namespace: str,
        *,
        since_seconds: int | None = None,
        container: str | None = None,
        limit_bytes: int | None = None,
    ) -> str:
        """pods/log. python 클라이언트에는 sinceTime 이 없어 since_seconds 로 자른다(초 단위)."""
        kwargs: dict[str, Any] = {}
        if since_seconds is not None:
            kwargs["since_seconds"] = max(1, int(since_seconds))
        if container:
            kwargs["container"] = container
        if limit_bytes is not None:
            kwargs["limit_bytes"] = int(limit_bytes)
        try:
            # 기본(_preload_content=True)은 응답을 'str' 로 역직렬화하면서 bytes 의 repr 을 돌려준다
            # (b'...\\n...'). 원문 bytes 를 직접 받아 디코드한다. orbstack 에서 확인.
            resp = k8s.CoreV1Api(self.api_client).read_namespaced_pod_log(
                name, namespace, _preload_content=False, **kwargs
            )
        except k8s.ApiException as e:
            if e.status == 404:
                return ""
            raise ClusterUnreachable(f"pods/log {namespace}/{name}: HTTP {e.status}") from e
        try:
            data = resp.data
        except Exception as e:
            # 머리를 받은 뒤 본문 도중에 멈춘 서버. 읽기 시한은 urllib3 의 원래 오류로 올라온다
            timeout = _timeout_of(e)
            if timeout is None:
                raise
            raise _cluster_timeout(f"pods/log {namespace}/{name}", timeout, API_READ_TIMEOUT) from e
        if isinstance(data, bytes):
            return data.decode("utf-8", errors="replace")
        return str(data or "")

    # -- 데이터 플레인 Service HTTP (D21) ------------------------------------------

    def service_route(
        self, namespace: str, service: str, port: int, *, scheme: str = "http"
    ) -> ServiceRoute:
        if self.direct_services:
            return ServiceRoute(
                base_url=f"{scheme}://{service}.{namespace}.svc.cluster.local:{port}", via="direct"
            )
        return apiserver_proxy_route(
            self.api_client.configuration, namespace, service, port, scheme=scheme
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
        """이 클러스터의 Service 로 HTTP 요청 하나. 호출부는 direct/apiserver 를 구분하지 않는다."""
        route = self.service_route(namespace, service, port)
        return await send_service_request(
            route,
            method,
            path,
            json=json,
            headers=headers,
            timeout=timeout,
            read_timeout=read_timeout,
        )

    # -- 진단 -------------------------------------------------------------

    def version(self) -> dict[str, Any]:
        try:
            info = k8s.VersionApi(self.api_client).get_code()
        except ClusterUnreachable:
            raise  # ClusterTimeout 은 그 이름 그대로 올린다
        except Exception as e:  # noqa: BLE001
            raise ClusterUnreachable(f"{type(e).__name__}: {e}") from e
        # to_dict 는 파이썬 속성 이름(git_version)을 준다. API 서버와 fake 가 쓰는 camelCase
        # (gitVersion)로 맞춘다
        return self.api_client.sanitize_for_serialization(info)

    def can_i(
        self,
        verb: str,
        resource: str,
        group: str = "",
        namespace: str | None = None,
        name: str | None = None,
    ) -> bool:
        """`services/proxy` 처럼 `/` 가 있으면 서브리소스로 나눠 묻는다. name 은 resourceNames 한정
        규칙(예: clusterroles bind daegam-tls-reader)을 묻는다."""
        resource, _, subresource = resource.partition("/")
        body = k8s.V1SelfSubjectAccessReview(
            spec=k8s.V1SelfSubjectAccessReviewSpec(
                resource_attributes=k8s.V1ResourceAttributes(
                    verb=verb,
                    resource=resource,
                    subresource=subresource or None,
                    group=group,
                    namespace=namespace,
                    name=name,
                )
            )
        )
        res = k8s.AuthorizationV1Api(self.api_client).create_self_subject_access_review(body)
        return bool(res.status and res.status.allowed)

    def can_user(
        self, user: str, verb: str, resource: str, namespace: str, group: str = ""
    ) -> bool:
        """다른 주체(user)가 네임스페이스에서 그 일을 할 수 있는가. LocalSubjectAccessReview 다
        (P50 10절, 원격 프록시 계정이 Secret 을 읽지 못하는지 본다)."""
        body = k8s.V1LocalSubjectAccessReview(
            metadata=k8s.V1ObjectMeta(namespace=namespace),
            spec=k8s.V1SubjectAccessReviewSpec(
                user=user,
                resource_attributes=k8s.V1ResourceAttributes(
                    verb=verb, resource=resource, group=group, namespace=namespace
                ),
            ),
        )
        res = k8s.AuthorizationV1Api(self.api_client).create_namespaced_local_subject_access_review(
            namespace, body
        )
        return bool(res.status and res.status.allowed)

    def whoami(self) -> dict[str, Any]:
        """`authentication.k8s.io/v1 SelfSubjectReview` 의 status.userInfo. RoleBinding subject 의
        재료다 (home SA, 원격 SA, kubecontext 의 User)."""
        body = {"apiVersion": "authentication.k8s.io/v1", "kind": "SelfSubjectReview"}
        try:
            data = self.dyn.request(
                "post", SELF_SUBJECT_REVIEW_PATH, body=body, serializer=lambda _c, d: d
            )
        except ForbiddenError as e:
            raise AccessDenied("create", "selfsubjectreviews", None, str(e)) from e
        except Exception as e:
            # 연결 실패만 ClusterUnreachable 이다. 1.27 의 404 나 인증 프록시의 400 은 그대로 둔다
            if _connection_failure(e):
                raise ClusterUnreachable(f"selfsubjectreview: {type(e).__name__}: {e}") from e
            raise
        status = (data or {}).get("status") if isinstance(data, dict) else None
        return dict((status or {}).get("userInfo") or {})

    def discover(self, group_version: str) -> list[str]:
        """`GET /apis/{gv}` 로 서빙되는 리소스 이름(서브리소스 제외). CRD 읽기 권한이 필요 없다."""
        path = f"/apis/{group_version}" if group_version != "v1" else "/api/v1"
        try:
            data = self.dyn.request("get", path, serializer=lambda _c, d: d)
        except NotFoundError:
            return []
        except Exception as e:
            if _connection_failure(e):
                raise ClusterUnreachable(f"discovery {path}: {type(e).__name__}: {e}") from e
            raise
        if not isinstance(data, dict):
            return []
        return [r["name"] for r in data.get("resources", []) if "/" not in r["name"]]


def strict_kinds(cluster: Cluster) -> frozenset[str]:
    """이 클러스터에서 Strict 로 적용하는 ToolHive kind (P50 2절). 마지막 Strict dry run 에서 문서가
    모두 통과한 kind 이고, dry run 이 없었거나 아무 문서도 보내지 않았으면 빈 집합이다."""
    check = cluster.gates.strict_apply
    if check is None or check.checked == 0:
        return frozenset()
    return frozenset(check.kinds)


def is_unreachable_error(e: Exception) -> bool:
    """클러스터에 연결할 수 없다는 오류인가. 연결 오류와 API 서버의 5xx 이고, 그 클러스터로 가는
    다음 요청도 같은 꼴로 실패하리라 본다. 4xx 는 그 요청 하나의 판정이다."""
    if isinstance(e, ClusterUnreachable | OSError | urllib3.exceptions.HTTPError):
        return True
    status = getattr(e, "status", None)
    return isinstance(status, int) and (status == 0 or status >= 500)


def _connection_failure(e: BaseException) -> bool:
    """서버가 답하지 못한 실패인가. 시한, 소켓·urllib3 오류, 상태 0(TLS 오류를 감싼
    ApiException)이다. ClusterUnreachable 은 이미 그렇게 판정된 것이다. 5xx 나 4xx 는 서버가 답한
    것이다."""
    if isinstance(e, ClusterUnreachable):
        return not isinstance(e, ResourceNotServed | ServiceTimeout)
    if isinstance(e, OSError | urllib3.exceptions.HTTPError):
        return True
    return getattr(e, "status", None) == 0


def cluster_down(e: BaseException) -> bool:
    """이 실패가 클러스터 자체에 연결할 수 없다는 뜻인가(시한 포함). preflight 와 서비스 토큰 스윕이
    그 클러스터의 남은 일을 건너뛸지 이것으로 정한다 (P65 2절).

    예외 사슬을 따라 읽는다. 서비스 오류는 코드로 본다(502 k8s_error 는 아니다). ClusterUnreachable
    은 그것이 감싼 원인까지 보고, 원인이 서버가 답한 오류(상태가 있는 ApiException)면 아니다.
    서빙하지 않는 리소스와 데이터 플레인 Service 의 시한은 API 서버가 내려간 것이 아니다."""
    down = False
    for link in exception_chain(e):
        if isinstance(link, ResourceNotServed | ServiceTimeout):
            return False
        if isinstance(link, ServiceError):
            if link.code != "cluster_unreachable":
                return False
            down = True
        elif isinstance(link, ClusterUnreachable):
            down = True
        elif isinstance(link, OSError | urllib3.exceptions.HTTPError):
            return True
        elif isinstance(getattr(link, "status", None), int):
            return link.status == 0  # type: ignore[attr-defined]
    return down


def _read_secret(
    secret_store: SecretStore | None, secret_name: str | None, namespace: str
) -> dict[str, str]:
    if secret_store is None or not secret_name:
        raise ClusterUnreachable("no secret store / secret name for cluster credentials")
    data = secret_store.get(secret_name, namespace=namespace)
    if data is None:
        raise ClusterUnreachable(f"secret {namespace}/{secret_name} not found")
    return data


def _kubeconfig_dict(raw: str | None) -> dict[str, Any]:
    """Secret 의 kubeconfig 를 등록 API 와 같은 `load_kubeconfig` 로 연다. 등록 API 가 먼저 거르지만
    그 전에 저장된 Secret 에서도 명령이 돌지 않고 문법 오류의 글이 preflight·로그로 나가지 않게
    클라이언트를 만들기 전에 다시 본다."""
    if not raw:
        raise ClusterUnreachable("secret has no 'kubeconfig' key")
    try:
        return load_kubeconfig(raw)
    except KubeconfigRejected as e:
        raise ClusterUnreachable(e.detail) from None


def _encoded(value: str) -> str:
    return base64.b64encode(value.encode("utf-8")).decode("ascii")


def _data_path(key: str) -> str:
    """Secret 의 data 안 키 하나의 JSON Pointer (RFC 6901)."""
    return "/data/" + key.replace("~", "~0").replace("/", "~1")


def decode_secret_data(data: dict[str, str] | None) -> dict[str, str]:
    return {k: base64.b64decode(v).decode("utf-8") for k, v in (data or {}).items()}


def encode_secret_data(data: dict[str, str]) -> dict[str, str]:
    return {k: base64.b64encode(v.encode("utf-8")).decode("ascii") for k, v in data.items()}


# -- 캐시 ------------------------------------------------------------------


ClientFactory = Callable[["Cluster"], KubeClient]


class ClusterClientCache:
    """파드 로컬 캐시. TTL 5분, PATCH /clusters 에서 invalidate. 파드 간 공유 상태는 아니다."""

    def __init__(
        self,
        secret_store_getter: Callable[[], SecretStore | None],
        *,
        platform_namespace: str = "daegam-system",
        ttl: float = CLIENT_CACHE_TTL,
    ):
        self._secret_store_getter = secret_store_getter
        self._ns = platform_namespace
        self._lock = threading.Lock()
        self._entries: TTLCache[str, ClusterClient] = TTLCache(ttl)

    def get(self, cluster: Cluster) -> ClusterClient:
        with self._lock:
            hit = self._entries.get(cluster.id)
        if hit is not None:
            return hit
        cc = ClusterClient.from_cluster(
            cluster, self._secret_store_getter(), platform_namespace=self._ns
        )
        with self._lock:
            self._entries.put(cluster.id, cc)
        return cc

    def invalidate(self, cluster_id: str) -> None:
        with self._lock:
            self._entries.pop(cluster_id)

    def clear(self) -> None:
        with self._lock:
            self._entries.clear()


_IMAGE_TAG_RE = re.compile(r":v?([0-9][0-9A-Za-z.\-+]*)$")


def image_tag(image: str) -> str | None:
    """`ghcr.io/x/operator:v0.51.2` → `0.51.2`."""
    m = _IMAGE_TAG_RE.search(image.split("@", 1)[0])
    return m.group(1) if m else None
