"""LLM 게이트웨이의 동기화 (P38 4절·7절·10절·11절).

`LlmGateway.sync` 는 리컨실 리스 안의 단계 `llm_gateway` 다. 틱마다 설치 env 와 Platform Settings
에서 원하는 객체를 렌더해 home 클러스터의 게이트웨이 네임스페이스에 SSA 로 쓰고, 더는 원하지 않는
자기 객체(이 설치의 `platform-id` 라벨)를 지운다. 그다음 게이트웨이 파드마다 `GET /_daegam/rev` 로
반영된 리비전을 읽는다.

- 네임스페이스: 없으면(대감 차트를 `llmGateway.mode` agentgateway 로 설치하기 전), 테넌트 라벨이
  있으면, 다른 설치의 표시가 있으면 아무것도 렌더하지 않는다. 처음 렌더할 때 이 설치의 표시를 단다
- provider: 주소 검사(SSRF)를 지나고 키가 있는 것만 렌더한다. 키는 틱마다 원본(`api_key_secret`)에서
  다시 읽는다. 키 저장 경로는 `sync_provider` 로 곧바로 맞춘다
- 리비전: 설정과 키의 `keyed_hash` 이고 서명 키에서 나온 키로 건다. 모든 실행 중 파드가 그 값을
알리면 반영된 것이다. 지워지는 파드는 준비된 파드가 있고 지워진 지 30초가 지났을 때만 뺀다
- 회복: 한 파드만 2분 넘게 옛 리비전이면 그 파드를 10초 유예로 지운다. 모든 파드가 옛 리비전이면
  파드는 두고 설정 Secret 을 마지막 좋은 리비전으로 되돌린 뒤 `llm_gateway_config_rejected` 다
- 끄기: 스위치가 꺼지고 게이트웨이를 쥔 것(`holders`)이 없으면 만든 객체를 모두 지운다.
네임스페이스와 Role 은 설치의 것이라 둔다
"""

from __future__ import annotations

import ipaddress
import json
import logging
from collections.abc import Awaitable, Callable, Collection
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import urlsplit

import certifi
from pydantic import ValidationError
from starlette.concurrency import run_in_threadpool

from daegam.auth.service_token import InternalIssuer, service_sub
from daegam.auth.signer import LLM_AUDIENCE, TELEMETRY_AUDIENCE
from daegam.config import Settings
from daegam.errors import ServiceError
from daegam.k8s.client import KubeClient, decode_secret_data
from daegam.k8s.labels import LABEL_PLATFORM_ID, keyed_hash, stable_hash
from daegam.k8s.portforward import bearer_pod_client
from daegam.k8s.templates import llm_gateway as tpl
from daegam.k8s.templates.common import RenderError
from daegam.k8s.templates.llm_gateway_config import (
    CA_FILE,
    CONFIG_FILE,
    MAX_CONNECTION_SECONDS,
    RATELIMIT_PORT,
    REV_PATH,
    REV_PORT,
    REVOCATION_WARN,
    SECRET_MAX_BYTES,
    GatewayProvider,
    config_text,
    gateway_provider,
    render_config,
    tracing,
)
from daegam.services.llm_providers import ProviderError, resolve_api_key
from daegam.services.llm_tokens import LlmTokens
from daegam.services.tenancy import TenancyError, check_namespace_name
from daegam.store.base import utcnow
from daegam.store.llm_gateway import (
    LlmGatewayImages,
    LlmGatewayState,
    MongoLlmGatewayStore,
    PodRev,
    ProviderSync,
)
from daegam.store.llm_revocations import MongoLlmRevocationStore
from daegam.store.settings import LLMProvider, MongoPlatformSettingsStore, PlatformSettings
from daegam.store.telemetry import MongoTelemetryIngestStore
from daegam.urls import (
    API_PREFIX,
    IN_CLUSTER_SUFFIXES,
    TELEMETRY_OTLP_PATHS,
    export_url_problem,
    host_of,
)

logger = logging.getLogger("daegam.llm_gateway")

# 한 파드가 옛 리비전에 이만큼 머물면 회복한다 (4절)
STUCK_SECONDS = 120
# 되돌린 뒤 원하는 리비전을 다시 써 보는 간격 (4절)
RETRY_SECONDS = 600
# 지워지는 파드를 셈에서 빼는 때. 연결 수명에 15초 여유를 더한 것이다 (4절)
TERMINATING_SECONDS = MAX_CONNECTION_SECONDS + 15
DELETE_GRACE_SECONDS = 10
REV_READ_TIMEOUT = 5.0
REV_LENGTH = 16
REV_SALT = b"daegam-llm-gateway"
COMPONENT_LABEL = "daegam.devops.ai.kr/component"
COMPONENT = "llm-gateway"
# 이 어노테이션이 네임스페이스에 있으면 게이트웨이 Deployment 를 두고 간다(e2e 8단계)
PAUSE_ANNOTATION = "daegam.devops.ai.kr/llm-gateway-paused"
# 상태의 까닭과 경고 (11절)
NAMESPACE_MISSING = "llm_gateway_namespace_missing"
NAMESPACE_TAKEN = "llm_gateway_namespace_taken"
FOREIGN_INSTALL = "llm_gateway_foreign_install"
NO_HOME = "llm_gateway_no_home_cluster"
NO_SIGNER = "no_signer"
CONFIG_REJECTED = "llm_gateway_config_rejected"
PROVIDER_REJECTED = "llm_gateway_provider_rejected"
PAUSED = "llm_gateway_paused"
RATELIMIT_UNAVAILABLE = "llm_ratelimit_unavailable"
IMAGES_MISSING = "llm_gateway_images_missing"
# 렌더한 것을 클러스터에 적용하거나 파드를 읽다가 실패했다. 다음 틱이 다시 한다
APPLY_FAILED = "llm_gateway_apply_failed"
REVOCATIONS_LARGE = "llm_gateway_revocations_large"
# 이 동기화가 게이트웨이 네임스페이스에서 부르는 동사 가운데 플랫폼 ClusterRole 에 없는 것. P63
# 뒤로 ClusterRole 에는 Secret 동사가 없다. 이름으로 읽고(get), SSA 로 쓰고(create·patch),
# 지운다(delete). 한도 계수기의 비밀번호는 없을 때만 만든다(create). 파드는 회복이 지우기만 한다.
# 대감 차트의 Role daegam-llm-gateway(charts/daegam/templates/llm-gateway.yaml)가 이것과 같아야 한다
# (test_namespaces.py)
NAMESPACE_VERBS: dict[tuple[str, str], frozenset[str]] = {
    ("", "secrets"): frozenset({"get", "create", "patch", "delete"}),
    ("", "configmaps"): frozenset({"get", "create", "patch", "delete"}),
    ("", "pods"): frozenset({"delete"}),
    ("policy", "poddisruptionbudgets"): frozenset({"get", "create", "patch", "delete"}),
}
# 이 설치가 지우는 자기 객체. 이름이 정해져 있어 목록을 읽지 않는다(Secret 에 list 권한이 없다)
OWNED: list[tuple[str, str, str]] = [
    ("apps/v1", "Deployment", tpl.GATEWAY),
    ("apps/v1", "Deployment", tpl.RATELIMIT),
    ("apps/v1", "Deployment", tpl.REDIS),
    ("v1", "Service", tpl.GATEWAY),
    ("v1", "Service", tpl.RATELIMIT),
    ("v1", "Service", tpl.REDIS),
    ("v1", "ConfigMap", tpl.RATELIMIT),
    ("v1", "Secret", tpl.CONFIG_SECRET),
    ("v1", "Secret", tpl.APPLIED_SECRET),
    ("v1", "Secret", tpl.REDIS),
    ("networking.k8s.io/v1", "NetworkPolicy", tpl.GATEWAY),
    ("networking.k8s.io/v1", "NetworkPolicy", tpl.RATELIMIT),
    ("networking.k8s.io/v1", "NetworkPolicy", tpl.REDIS),
    ("networking.k8s.io/v1", "NetworkPolicy", tpl.INTERNAL_UPSTREAMS),
    ("policy/v1", "PodDisruptionBudget", tpl.GATEWAY),
    ("apps/v1", "Deployment", tpl.COLLECTOR),
    ("v1", "Service", tpl.COLLECTOR),
    ("v1", "ConfigMap", tpl.COLLECTOR),
    ("v1", "Secret", tpl.COLLECTOR_TOKEN_SECRET),
    ("networking.k8s.io/v1", "NetworkPolicy", tpl.COLLECTOR),
]
# 수집 토큰 (8절). 30일로 내고 15일째에 바꾼다. tenant 는 service_claims 가 요구하는 자리 표시이고
# TENANT_RE 가 받지 않아 어느 테넌트와도 겹치지 않는다
INGEST_TTL = timedelta(days=30)
INGEST_RENEW_AFTER = timedelta(days=15)
INGEST_TENANT = "_llm-gateway"


class LlmGatewayError(ServiceError):
    pass


def dropped(obs: Observation, desired_rev: str) -> bool:
    """파드가 원하는 리비전을 버렸는가. 센 파드가 하나 이상이고 그 모두가 읽힌 다른 리비전이다."""
    revs = [s.rev for s in obs.counted]
    return bool(revs) and all(rev is not None and rev != desired_rev for rev in revs)


RevReader = Callable[[KubeClient, str, dict[str, Any]], Awaitable[str | None]]


def upstream_problem(p: LLMProvider, reserved: Collection[str]) -> str | None:
    """게이트웨이가 부를 주소의 검사(SSRF). 맞으면 None 이고 아니면 그 까닭이다. http(s)
    만이고 사용자 정보·쿼리·조각·`$` 가 없어야 하며, 평문 http 는 클러스터 안 `.svc` 이름만이다.
    `.svc` 이름의 네임스페이스는 예약된 것이 아니어야 하고 IP 글자는 공인 주소만이다. OpenAI 계열은
    경로가 있어야 접두로 쓸 수 있다. base_url 이 없으면 provider 의 기본 주소라 검사할 것이 없다."""
    url = p.effective_base_url
    if url is None:
        return None
    if "$" in url:
        return "the base_url contains '$'"
    if problem := export_url_problem(url):
        return problem
    parts = urlsplit(url.strip())
    host = host_of(parts.hostname or "")
    labels = host.split(".")
    if host.endswith(IN_CLUSTER_SUFFIXES) and len(labels) < 3:
        return "an in-cluster name must be <service>.<namespace>.svc"
    try:
        if not ipaddress.ip_address(host).is_global:
            return f"address {host} is not public"
    except ValueError:
        # 짧은 이름(`daegam-platform`, `daegam-platform.daegam-system`)도 클러스터 DNS 의 검색
        # 도메인으로 풀린다. 한 마디 이름은 받지 않고, 둘째 마디가 예약된 네임스페이스면 받지 않는다
        if len(labels) == 1:
            return f"host {host!r} is a single label"
        if _reserved(labels[1], reserved):
            return f"namespace {labels[1]} is reserved"
    if p.sdk_kind == "openai" and not parts.path.strip("/"):
        return "an OpenAI-compatible base_url needs a path such as /v1"
    return None


def _reserved(namespace: str, reserved: Collection[str]) -> bool:
    try:
        check_namespace_name(namespace, reserved=reserved)
    except TenancyError as e:
        return e.code == "namespace_reserved"
    return False


def internal_upstream(p: LLMProvider) -> tuple[str, int] | None:
    """클러스터 안 provider 의 (네임스페이스, 포트). `llm-internal-upstreams` 가 그리로 나가는 길을
    연다."""
    url = p.effective_base_url
    if not url:
        return None
    parts = urlsplit(url)
    host = host_of(parts.hostname or "")
    if not host.endswith(IN_CLUSTER_SUFFIXES):
        return None
    return host.split(".")[1], parts.port or (443 if parts.scheme == "https" else 80)


@dataclass
class Desired:
    """한 틱의 렌더 결과."""

    rev: str
    config_rev: str
    files: dict[str, str]
    providers: list[GatewayProvider]
    rejected: dict[str, str]
    internal: list[tuple[str, int]]
    revoked: list[str]


@dataclass
class PodSeen:
    name: str
    pod: dict[str, Any]
    ready: bool
    deleted_for: float | None  # 지워진 뒤 흐른 초. 지워지지 않았으면 None
    rev: str | None = None


@dataclass
class Observation:
    counted: list[PodSeen] = field(default_factory=list)
    applied: bool = False


async def read_pod_rev(client: KubeClient, namespace: str, pod: dict[str, Any]) -> str | None:
    """파드 하나가 지금 쓰는 설정 리비전. 파드 IP 로 닿지 못하면 pods/portforward 로 간다. 읽지
    못하면 None 이다."""
    try:
        async with bearer_pod_client(
            client, namespace, tpl.GATEWAY, pod, REV_PORT, bearer=None, timeout=REV_READ_TIMEOUT
        ) as (hc, base_url):
            resp = await hc.get(f"{base_url}{REV_PATH}")
    except Exception as e:  # noqa: BLE001 - 읽지 못한 파드는 반영하지 않은 것으로 센다
        logger.info("llm gateway: rev of %s unreadable: %s", pod["metadata"]["name"], e)
        return None
    return resp.text.strip() if resp.status_code == 200 else None


def _config_rev(pod: dict[str, Any]) -> str | None:
    """파드가 뜰 때의 설정 판(시작할 때만 읽는 칸의 지문)이다."""
    return ((pod.get("metadata") or {}).get("annotations") or {}).get(tpl.CONFIG_REV_ANNOTATION)


def _ready(pod: dict[str, Any]) -> bool:
    conditions = (pod.get("status") or {}).get("conditions") or []
    return any(c.get("type") == "Ready" and c.get("status") == "True" for c in conditions)


def seen_pods(pods: list[dict[str, Any]], now: datetime) -> list[PodSeen]:
    """실행 중인 게이트웨이 파드. 지워지는 파드는 준비된 파드가 있고 지워진 지 30초가 지났을 때만
    뺀다. 준비된 파드가 없으면 kube-proxy 가 새 연결을 지워지는 파드에 보내기 때문이다 (F0 10번)."""
    running = []
    for pod in pods:
        meta = pod.get("metadata") or {}
        if (pod.get("status") or {}).get("phase") != "Running":
            continue
        deleted = meta.get("deletionTimestamp")
        deleted_for = None
        if deleted:
            at = datetime.fromisoformat(str(deleted).replace("Z", "+00:00")).replace(tzinfo=None)
            deleted_for = (now - at).total_seconds()
        running.append(PodSeen(meta["name"], pod, _ready(pod) and not deleted, deleted_for))
    any_ready = any(p.ready for p in running)
    return [
        p
        for p in running
        if p.deleted_for is None or not any_ready or p.deleted_for <= TERMINATING_SECONDS
    ]


class LlmGateway:
    """게이트웨이 한 벌의 렌더·적용·반영 확인. 리컨실 단계와 키 저장 경로와 상태 API 가 쓴다."""

    def __init__(
        self,
        settings: Settings,
        platform_settings: MongoPlatformSettingsStore,
        store: MongoLlmGatewayStore,
        cluster_service: Any,
        issuer: InternalIssuer,
        *,
        read_rev: RevReader = read_pod_rev,
        revocations: MongoLlmRevocationStore | None = None,
        tokens: LlmTokens | None = None,
        telemetry: MongoTelemetryIngestStore | None = None,
        clock: Callable[[], datetime] = utcnow,
    ):
        self.settings = settings
        self.platform_settings = platform_settings
        self.store = store
        self.clusters = cluster_service
        self.issuer = issuer
        self.read_rev = read_rev
        self.revocations = revocations
        # Agent 의 LLM 토큰과 롤아웃 뒤의 정리 (5절·6절). 정리가 남으면 끈 뒤에도 게이트웨이를 둔다
        self.tokens = tokens
        # 수집 토큰의 클러스터별 회수 시각 (P37 8절)
        self.telemetry = telemetry
        self.clock = clock

    @property
    def namespace(self) -> str:
        return self.settings.llm_gateway_namespace

    # -- 틱 ------------------------------------------------------------------------------------

    async def sync(self, _now: datetime | None = None) -> dict[str, list[str]]:
        """게이트웨이 한 벌을 맞추고, 그와 따로 롤아웃이 끝난 Agent 의 토큰 정리를 한다. 정리는
        네임스페이스를 거둔 뒤에도, 렌더가 막힌 틱에도 돈다 (6절)."""
        out = await self._sync_gateway()
        if self.tokens is not None and (settled := await self.tokens.settle()):
            out = {**out, "llm_cleanup": settled}
        return out

    async def _sync_gateway(self) -> dict[str, list[str]]:
        state = await self.store.get()
        if not self.settings.llm_gateway_on and not state.rendered:
            # 끄고 한 번도 렌더하지 않았으면 클러스터에 묻지 않는다. 게이트웨이 경로 파드도 없다
            return {}
        client = self.clusters.home_client()
        if not await self._wanted(client):
            if state.rendered and client is not None:
                await self._clear(client)
                await self.store.put(LlmGatewayState(synced_at=self.clock()))
                return {"cleared": [COMPONENT]}
            return {}
        now = self.clock()
        state.warnings = []
        reason = await self._prepare(client, state)
        if reason is not None:
            state.reason, state.synced_at = reason, now
            await self.store.put(state)
            return {"blocked": [reason]}
        assert client is not None
        ps = await self.platform_settings.get()
        try:
            images = self._images()
            desired = await self._desired(ps, state, now)
        except RenderError as e:
            state.reason, state.synced_at = e.code, now
            await self.store.put(state)
            return {"failed": [e.code]}
        try:
            paused = await self._paused(client)
            await self._apply(client, ps, images, desired, state, paused=paused)
            await self._settle(client, ps, desired, state, now, paused=paused)
        except Exception:
            await self._store_apply_failure(state, now)
            raise
        if state.reason == APPLY_FAILED:
            # 실패가 덮어쓴 까닭이 아직 서 있으면(파드가 버린 리비전) 그것으로 되돌린다
            state.reason = CONFIG_REJECTED if state.rejected_rev else None
        state.synced_at = now
        await self.store.put(state)
        return {"synced": [state.desired_rev or ""]}

    async def _store_apply_failure(self, state: LlmGatewayState, now: datetime) -> None:
        """적용이 실패한 틱의 까닭과 시각을 남긴다. 까닭이 없으면 상태 API 가 한 번도 맞추지 않은
        것처럼 읽히고, 원인은 플랫폼 로그에만 남는다.

        저장된 문서에 까닭과 시각, 그리고 `_prepare` 가 이미 클러스터에 해 둔 것(네임스페이스
        표시)만 옮긴다. 이 틱이 메모리에서 바꾼 나머지는 적용이 끝나지 않았으므로 버린다. 저장이
        실패해도 원래 오류를 가리지 않는다."""
        try:
            stored = await self.store.get()
            stored.rendered, stored.activated_at = state.rendered, state.activated_at
            stored.reason, stored.synced_at = APPLY_FAILED, now
            await self.store.put(stored)
        except Exception:  # noqa: BLE001  원래 오류가 올라가야 한다
            logger.exception("llm gateway: could not store the apply failure")

    def _images(self) -> LlmGatewayImages:
        """설치 env 의 이미지 넷. 끈 채 옮겨 가는 동안 배포가 이미지를 비웠으면 렌더하지 않는다."""
        try:
            return LlmGatewayImages.model_validate(self.settings.llm_gateway_images)
        except ValidationError as e:
            raise RenderError(
                IMAGES_MISSING, "DAEGAM_LLM_GATEWAY_IMAGES is empty or invalid"
            ) from e

    async def _wanted(self, client: KubeClient | None) -> bool:
        """렌더할 때인가. 켰거나 아직 게이트웨이를 쥔 것이 있다(끈 뒤 옮겨 가는 동안, 6절)."""
        if self.settings.llm_gateway_on:
            return True
        if self.tokens is not None and await self.tokens.holding():
            return True
        return client is not None and await self._route_pods(client)

    async def _route_pods(self, client: KubeClient) -> bool:
        pods = await run_in_threadpool(
            client.list_metadata, "v1", "Pod", None, labels={tpl.LLM_ROUTE_LABEL: "gateway"}
        )
        return bool(pods)

    async def _prepare(self, client: KubeClient | None, state: LlmGatewayState) -> str | None:
        """렌더할 수 있는가. 아니면 그 까닭이다. 처음 렌더할 때 네임스페이스에 이 설치의 표시를
        달고 `activated_at` 을 정한다."""
        if client is None:
            return NO_HOME
        if not self.issuer.loaded:
            return NO_SIGNER
        ns = await run_in_threadpool(client.get, "v1", "Namespace", self.namespace)
        if ns is None or ((ns.get("metadata") or {}).get("labels") or {}).get(COMPONENT_LABEL) != (
            COMPONENT
        ):
            return NAMESPACE_MISSING
        labels = (ns.get("metadata") or {}).get("labels") or {}
        if tpl.TENANT_LABEL in labels:
            return NAMESPACE_TAKEN
        install_id = await self.platform_settings.install_id()
        mark = labels.get(LABEL_PLATFORM_ID)
        if mark and mark != install_id:
            return FOREIGN_INSTALL
        if not mark:
            # 설치가 단 라벨을 그대로 함께 싣는다. SSA 가 그 값을 바꾸지 않는다
            body = {
                "apiVersion": "v1",
                "kind": "Namespace",
                "metadata": {
                    "name": self.namespace,
                    "labels": {**labels, LABEL_PLATFORM_ID: install_id},
                },
            }
            await run_in_threadpool(client.apply, body)
        if not state.rendered:
            state.activated_at = self.clock().replace(microsecond=0)
            state.rendered = True
        return None

    async def _paused(self, client: KubeClient) -> bool:
        ns = await run_in_threadpool(client.get, "v1", "Namespace", self.namespace)
        return PAUSE_ANNOTATION in (((ns or {}).get("metadata") or {}).get("annotations") or {})

    # -- 렌더 ----------------------------------------------------------------------------------

    def _rendered_providers(
        self, ps: PlatformSettings
    ) -> tuple[list[GatewayProvider], dict[str, str], dict[str, str], list[tuple[str, int]]]:
        """(렌더할 provider, 거절한 것 slug→까닭, 키 파일, 클러스터 안 주소)."""
        store = self.clusters.secret_store()
        rendered, rejected, keys, internal = [], {}, {}, []
        for p in ps.llm_providers:
            if p.is_stub:
                continue
            gp = gateway_provider(p)
            problem = upstream_problem(p, self.settings.reserved_namespaces)
            if problem is None:
                try:
                    keys[gp.key_file] = resolve_api_key(p, store, namespace=self.settings.namespace)
                except ProviderError as e:
                    problem = e.code
            if problem is not None:
                rejected[gp.slug] = problem
                continue
            rendered.append(gp)
            if (upstream := internal_upstream(p)) is not None:
                internal.append(upstream)
        return rendered, rejected, keys, internal

    async def _revoked(self, state: LlmGatewayState) -> list[str]:
        """리스너가 막는 jti. 켠 뒤에 낸 토큰의 것만이다. 그 앞의 토큰은 `jwt.iat` 규칙이
        막는다."""
        if self.revocations is None or state.activated_at is None:
            return []
        return await self.revocations.listed(state.activated_at)

    async def _desired(
        self, ps: PlatformSettings, state: LlmGatewayState, now: datetime
    ) -> Desired:
        providers, rejected, keys, internal = await run_in_threadpool(self._rendered_providers, ps)
        files = dict(keys)
        if ps.network.ca_bundle:
            files[CA_FILE] = _ca_file(ps.network.ca_bundle)
        assert state.activated_at is not None
        revoked = await self._revoked(state)
        args: dict[str, Any] = {
            "issuer": self.settings.platform_issuer_url,
            "audience": LLM_AUDIENCE,
            "jwks": self.issuer.jwks(),
            "activated_at": int(state.activated_at.timestamp()),
            "revoked": revoked,
            "providers": providers,
            "ratelimit_host": self._ratelimit_host(),
            "ca": CA_FILE in files,
            "tracing": self._tracing(),
        }
        unsigned = config_text(render_config(rev="", **args))
        key = self.issuer.signer.derive(REV_SALT, b"rev", 32).hex()
        rev = keyed_hash(
            key, json.dumps({"config": unsigned, "files": files}, sort_keys=True), REV_LENGTH
        )
        config = render_config(rev=rev, **args)
        files[CONFIG_FILE] = config_text(config)
        if sum(len(v.encode()) for v in files.values()) > SECRET_MAX_BYTES:
            raise RenderError("llm_gateway_config_too_large", "the gateway config exceeds 1 MiB")
        state.revocations = len(revoked)
        if len(revoked) > REVOCATION_WARN:
            state.warnings.append(REVOCATIONS_LARGE)
        return Desired(
            rev=rev,
            config_rev=stable_hash(config["config"], REV_LENGTH),
            files=files,
            providers=providers,
            rejected=rejected,
            internal=internal,
            revoked=revoked,
        )

    def _ratelimit_host(self) -> str:
        return f"{tpl.RATELIMIT}.{self.namespace}.svc.cluster.local:{RATELIMIT_PORT}"

    def _tracing(self) -> dict[str, Any]:
        host = f"{tpl.COLLECTOR}.{self.namespace}.svc.cluster.local"
        return tracing(f"http://{host}:{tpl.OTLP_GRPC_PORT}")

    # -- 적용 ----------------------------------------------------------------------------------

    async def _apply(
        self,
        client: KubeClient,
        ps: PlatformSettings,
        images: LlmGatewayImages,
        desired: Desired,
        state: LlmGatewayState,
        *,
        paused: bool,
    ) -> None:
        install_id = await self.platform_settings.install_id()
        docs = self._workloads(images, ps, desired, install_id)
        if paused:
            # e2e 8단계가 게이트웨이 파드를 지운 채 둔다. 상태는 경고한다
            docs = [
                d for d in docs if (d["kind"], d["metadata"]["name"]) != ("Deployment", tpl.GATEWAY)
            ]
            state.warnings.append(PAUSED)
        if state.rejected_rev != desired.rev:
            # 파드가 버린 리비전이 그대로면 되돌린 좋은 설정을 다시 덮지 않는다 (4절)
            docs.append(
                tpl.render_secret(
                    tpl.CONFIG_SECRET, self.namespace, desired.files, install_id=install_id
                )
            )
        for doc in docs:
            await run_in_threadpool(client.apply, doc)
        await self._ensure_redis_password(client, install_id)
        state.ingest_expires_at = await self._ensure_ingest_token(client, install_id)
        wanted = {(d["kind"], d["metadata"]["name"]) for d in docs}
        wanted |= {
            ("Secret", tpl.CONFIG_SECRET),
            ("Secret", tpl.APPLIED_SECRET),
            ("Secret", tpl.REDIS),
            ("Secret", tpl.COLLECTOR_TOKEN_SECRET),
        }
        if paused:
            wanted.add(("Deployment", tpl.GATEWAY))
        await self._delete_owned(client, install_id, keep=wanted)
        if state.desired_rev != desired.rev and state.rejected_rev != desired.rev:
            state.desired_rev, state.desired_at = desired.rev, self.clock()
        state.desired_providers = [gp.name for gp in desired.providers]
        state.limits_rev = stable_hash(ps.llm_gateway.limits.model_dump(), REV_LENGTH)
        state.providers = {
            gp.slug: state.providers.get(gp.slug, ProviderSync()) for gp in desired.providers
        } | {slug: ProviderSync(rejected=why) for slug, why in desired.rejected.items()}
        if desired.rejected:
            state.warnings.append(PROVIDER_REJECTED)

    def _workloads(
        self, images: LlmGatewayImages, ps: PlatformSettings, desired: Desired, install_id: str
    ) -> list[dict[str, Any]]:
        ns = self.namespace
        slugs = {gp.name: gp.slug for gp in desired.providers}
        limits = tpl.ratelimit_config(ps.llm_gateway.limits, slugs)
        docs = [
            *tpl.render_policies(
                namespace=ns,
                platform_namespace=self.settings.namespace,
                install_id=install_id,
                gateway_egress=self._gateway_egress(),
                internal_upstreams=desired.internal,
            ),
            *tpl.render_redis(images, namespace=ns, install_id=install_id),
            *tpl.render_ratelimit(images, namespace=ns, install_id=install_id, config=limits),
            tpl.render_gateway(
                images, namespace=ns, install_id=install_id, config_rev=desired.config_rev
            ),
            tpl.render_service(tpl.GATEWAY, ns, tpl.GATEWAY_PORT, install_id=install_id),
        ]
        traces = f"{API_PREFIX}{TELEMETRY_OTLP_PATHS['traces']}"
        ingest = f"{self.settings.home_platform_internal_url()}{traces}"
        docs += [
            *tpl.render_collector(
                images, namespace=ns, install_id=install_id, config=tpl.collector_config(ingest)
            ),
            tpl.collector_policy(
                namespace=ns,
                install_id=install_id,
                ingest_namespace=self.settings.namespace,
                ingest_port=self.settings.platform_port,
            ),
        ]
        if self.settings.llm_gateway_pdb:
            docs.append(tpl.render_pdb(ns, install_id=install_id))
        return docs

    def _gateway_egress(self) -> list[dict[str, Any]]:
        """게이트웨이가 더 나가는 곳. 수집기(8절)다."""
        return [tpl.collector_egress()]

    async def _ensure_redis_password(self, client: KubeClient, install_id: str) -> None:
        """한도 계수기의 비밀번호. 없을 때만 무작위로 만들고 다시 쓰지 않는다."""
        import secrets

        doc = tpl.render_secret(
            tpl.REDIS,
            self.namespace,
            {tpl.REDIS_PASSWORD_KEY: secrets.token_hex(24)},
            install_id=install_id,
        )
        await run_in_threadpool(client.create, doc)

    async def _ensure_ingest_token(self, client: KubeClient, install_id: str) -> datetime | None:
        """수집기의 토큰 (8절). P37 의 `aud: daegam-telemetry` 이고 `sub` 는 home 클러스터의
        `svc:llm-gateway:<id>` 다. 없거나, 15일이 지났거나, 서명 키가 바뀌었거나, 클러스터의
        `not_before` 보다 앞서 냈으면 새로 낸다. 수집기는 파일을 다시 읽으므로 구르지 않는다. 든
        토큰의 만료다."""
        home = await self.clusters.home()
        if home is None:
            return None
        secret = await run_in_threadpool(
            client.get, "v1", "Secret", tpl.COLLECTOR_TOKEN_SECRET, self.namespace
        )
        held = decode_secret_data((secret or {}).get("data")).get(tpl.COLLECTOR_TOKEN_KEY)
        if not await self._ingest_due(held, home.id):
            return _expires(held)
        token = self.issuer.mint(
            sub=service_sub("llm-gateway", home.id),
            tenant=INGEST_TENANT,
            groups=[],
            ttl=int(INGEST_TTL.total_seconds()),
            audiences=[TELEMETRY_AUDIENCE],
        )
        doc = tpl.render_secret(
            tpl.COLLECTOR_TOKEN_SECRET,
            self.namespace,
            {tpl.COLLECTOR_TOKEN_KEY: token},
            install_id=install_id,
        )
        await run_in_threadpool(client.apply, doc)
        return _expires(token)

    async def _ingest_due(self, token: str | None, cluster_id: str) -> bool:
        peek = InternalIssuer.peek(token) if token else None
        if peek is None or peek.iat is None or peek.kid != self.issuer.kid:
            return True
        issued = datetime.fromtimestamp(peek.iat, tz=UTC).replace(tzinfo=None)
        if self.clock() - issued >= INGEST_RENEW_AFTER:
            return True
        not_before = await self.telemetry.not_before(cluster_id) if self.telemetry else None
        return not_before is not None and issued < not_before.replace(microsecond=0)

    async def _delete_owned(
        self, client: KubeClient, install_id: str, *, keep: set[tuple[str, str]]
    ) -> None:
        """이 설치가 만든 객체 가운데 keep 에 없는 것을 지운다. 라벨로 자기 것인지 본다."""
        for api_version, kind, name in OWNED:
            if (kind, name) in keep:
                continue
            obj = await run_in_threadpool(client.get, api_version, kind, name, self.namespace)
            labels = ((obj or {}).get("metadata") or {}).get("labels") or {}
            if obj is not None and labels.get(LABEL_PLATFORM_ID) == install_id:
                await run_in_threadpool(client.delete, api_version, kind, name, self.namespace)

    async def _clear(self, client: KubeClient) -> None:
        install_id = await self.platform_settings.install_id()
        await self._delete_owned(client, install_id, keep=set())
        logger.info("llm gateway: turned off and cleared %s", self.namespace)

    # -- 반영 확인과 회복 --------------------------------------------------------------------

    async def _settle(
        self,
        client: KubeClient,
        ps: PlatformSettings,
        desired: Desired,
        state: LlmGatewayState,
        now: datetime,
        *,
        paused: bool,
    ) -> None:
        obs = await self._observe(client, state, now)
        configured = {p.name for p in ps.llm_providers}
        rendered = {gp.name for gp in desired.providers}
        ready = [p for p in state.providers_ready if p in configured and p in rendered]
        if obs.applied and obs.counted[0].rev == desired.rev:
            if state.applied_rev != desired.rev:
                await self._keep_applied(client, desired)
            state.applied_rev = desired.rev
            started = all(_config_rev(s.pod) == desired.config_rev for s in obs.counted)
            state.purpose_rev = desired.rev if started else None
            state.applied_revoked = list(desired.revoked)
            state.ready_at = state.ready_at or now
            state.rejected_rev = state.rejected_at = None
            state.reason = None
            ready = sorted(set(ready) | rendered)
        elif obs.applied and state.rejected_rev:
            self._retry_rejected(state, now)
        elif not paused:
            await self._recover(client, desired, state, obs, now)
        state.providers_ready = ready
        state.revocations_pending = len(set(desired.revoked) - set(state.applied_revoked))
        await self._limit_health(client, state)

    async def _observe(
        self, client: KubeClient, state: LlmGatewayState, now: datetime
    ) -> Observation:
        pods = await run_in_threadpool(
            client.list, "v1", "Pod", self.namespace, {tpl.APP_LABEL: tpl.GATEWAY}
        )
        counted = seen_pods(pods, now)
        known: dict[str, PodRev] = {}
        for seen in counted:
            seen.rev = await self.read_rev(client, self.namespace, seen.pod)
            before = state.pods.get(seen.name)
            same = before is not None and before.rev == seen.rev
            known[seen.name] = before if same and before else PodRev(rev=seen.rev, since=now)
        state.pods = known
        revs = {s.rev for s in counted}
        return Observation(counted, applied=bool(counted) and len(revs) == 1 and None not in revs)

    async def _recover(
        self,
        client: KubeClient,
        desired: Desired,
        state: LlmGatewayState,
        obs: Observation,
        now: datetime,
    ) -> None:
        """원하는 리비전이 2분 넘게 반영되지 않았다. 새 리비전을 읽은 준비된 파드가 있으면 옛 파드
        하나를 지운다. 모든 파드가 읽히고 그 모두가 다른 리비전을 알릴 때만 파드는 두고 마지막 좋은
        설정으로 되돌린다 (4절). 파드가 없거나 리비전을 읽지 못하는 것은 버린 것이 아니다. 그때
        되돌리면 회수한 토큰과 바꾼 키가 되살아난다."""
        waited = state.desired_at is not None and (now - state.desired_at).total_seconds()
        if not waited or waited <= STUCK_SECONDS:
            return
        good = [s for s in obs.counted if s.rev == desired.rev and s.ready]
        stuck = [
            s
            for s in obs.counted
            if s.rev != desired.rev
            and s.deleted_for is None
            and (now - state.pods[s.name].since).total_seconds() > STUCK_SECONDS
        ]
        ready = [s for s in obs.counted if s.ready]
        if good and stuck and len(ready) - (1 if stuck[0].ready else 0) >= 1:
            name = stuck[0].name
            logger.warning("llm gateway: pod %s stuck on an old revision; deleting it", name)
            await run_in_threadpool(
                client.delete, "v1", "Pod", name, self.namespace, grace_seconds=DELETE_GRACE_SECONDS
            )
            return
        if dropped(obs, desired.rev) and state.rejected_rev != desired.rev:
            logger.warning("llm gateway: every pod dropped revision %s; restoring", desired.rev)
            state.rejected_rev, state.rejected_at = desired.rev, now
            state.reason = CONFIG_REJECTED
            await self._restore(client)

    def _retry_rejected(self, state: LlmGatewayState, now: datetime) -> None:
        """되돌린 좋은 설정이 모든 파드에 있다. 원하는 리비전이 바뀔 때까지 까닭을 두되, 10분마다
        원하는 리비전을 다시 쓴다. 느린 kubelet 을 버린 것으로 잘못 보았어도 회수와 키 교체가
        결국 닿는다. 정말 틀린 설정이면 파드가 다시 버리고 2분 뒤 다시 되돌린다."""
        state.reason = CONFIG_REJECTED
        if state.rejected_at is None or (now - state.rejected_at).total_seconds() > RETRY_SECONDS:
            state.rejected_rev = state.rejected_at = None
            state.desired_at = now

    async def _keep_applied(self, client: KubeClient, desired: Desired) -> None:
        """반영된 리비전을 붙이지 않는 사본에 둔다. 키를 Mongo 에 두지 않고 되돌릴 수 있다."""
        install_id = await self.platform_settings.install_id()
        doc = tpl.render_secret(
            tpl.APPLIED_SECRET, self.namespace, desired.files, install_id=install_id
        )
        await run_in_threadpool(client.apply, doc)

    async def _restore(self, client: KubeClient) -> None:
        applied = await run_in_threadpool(
            client.get, "v1", "Secret", tpl.APPLIED_SECRET, self.namespace
        )
        if applied is None:
            return
        install_id = await self.platform_settings.install_id()
        files = decode_secret_data(applied.get("data"))
        doc = tpl.render_secret(tpl.CONFIG_SECRET, self.namespace, files, install_id=install_id)
        await run_in_threadpool(client.apply, doc)

    async def _limit_health(self, client: KubeClient, state: LlmGatewayState) -> None:
        ready = {
            name: await self._deployment_ready(client, name) for name in (tpl.RATELIMIT, tpl.REDIS)
        }
        if not all(ready.values()):
            state.warnings.append(RATELIMIT_UNAVAILABLE)

    async def _deployment_ready(self, client: KubeClient, name: str) -> bool:
        dep = await run_in_threadpool(client.get, "apps/v1", "Deployment", name, self.namespace)
        return int(((dep or {}).get("status") or {}).get("readyReplicas") or 0) > 0

    # -- 키 저장 경로와 상태 ------------------------------------------------------------------

    async def sync_config(self) -> PlatformSettings | None:
        """렌더하고 있으면 설정 Secret 만 곧바로 맞춘다. 키 저장 경로와 회수가 부른다. 실패는 다음
        틱이 잇는다. 맞춘 설정의 Platform Settings 이고, 렌더하지 않으면 None 이다."""
        state = await self.store.get()
        client = self.clusters.home_client()
        if not state.rendered or client is None or not self.issuer.loaded:
            return None
        ps = await self.platform_settings.get()
        desired = await self._desired(ps, state, self.clock())
        if state.rejected_rev == desired.rev:
            return None
        install_id = await self.platform_settings.install_id()
        doc = tpl.render_secret(
            tpl.CONFIG_SECRET, self.namespace, desired.files, install_id=install_id
        )
        await run_in_threadpool(client.apply, doc)
        return ps

    async def sync_provider(self, name: str) -> None:
        """키 저장 경로가 부른다. 바꾼 키를 사본에 곧바로 넣고 그 시각을 적는다."""
        ps = await self.sync_config()
        p = ps.providers_by_name.get(name) if ps is not None else None
        if p is not None:
            await self.store.key_synced(gateway_provider(p).slug, self.clock())

    async def status(self) -> dict[str, Any]:
        state = await self.store.get()
        ps = await self.platform_settings.get()
        return {
            "switch": self.settings.llm_gateway,
            "url": self.settings.llm_gateway_url,
            "namespace": self.namespace,
            "activated_at": state.activated_at,
            "ready_at": state.ready_at,
            "desired_rev": state.desired_rev,
            "applied_rev": state.applied_rev,
            "reason": state.reason,
            "warnings": list(state.warnings),
            "pods": [{"name": n, "rev": p.rev} for n, p in sorted(state.pods.items())],
            "providers": [
                self._provider_status(p, state) for p in ps.llm_providers if not p.is_stub
            ],
            "limits_rev": state.limits_rev,
            "revocations": state.revocations,
            "revocations_pending": state.revocations_pending,
            "ingest_token_expires_at": state.ingest_expires_at,
            "ingest_received_at": await self._received_at(),
            "synced_at": state.synced_at,
        }

    async def _received_at(self) -> datetime | None:
        """P37 수집 경로가 이 home 게이트웨이의 요청을 마지막으로 받은 때."""
        home = await self.clusters.home()
        if home is None or self.telemetry is None:
            return None
        return await self.telemetry.received_at(home.id)

    def _provider_status(self, p: LLMProvider, state: LlmGatewayState) -> dict[str, Any]:
        gp = gateway_provider(p)
        sync = state.providers.get(gp.slug) or ProviderSync()
        synced = state.key_synced.get(gp.slug)
        route = gp.base_url(self.settings.llm_gateway_url)
        return {
            "name": p.name,
            "slug": gp.slug,
            "ready": p.name in state.providers_ready,
            "key_synced_at": synced,
            "route_url": route,
            "rejected": sync.rejected,
        }


def _expires(token: str | None) -> datetime | None:
    peek = InternalIssuer.peek(token) if token else None
    if peek is None or peek.exp is None:
        return None
    return datetime.fromtimestamp(peek.exp, tz=UTC).replace(tzinfo=None)


def _ca_file(ca_bundle: str) -> str:
    """AI backend 가 믿을 묶음. 주어진 root 는 시스템 묶음을 갈음하므로 certifi 묶음에 관리자 CA 를
    더한다 (10절)."""
    with open(certifi.where(), encoding="utf-8") as f:
        system = f.read()
    return f"{system.rstrip()}\n{ca_bundle.strip()}\n"


def check_limits(limits: Any, ps: PlatformSettings, *, tenant_ok: Callable[[str], bool]) -> None:
    """한도 저장의 검사 (7절). provider 는 등록된 이름이어야 하고 테넌트 이름은 테넌트 꼴이어야
    한다. 아니면 LlmGatewayError 다."""
    unknown = sorted(set(limits.providers) - {p.name for p in ps.llm_providers})
    if unknown:
        raise LlmGatewayError(422, "unknown_provider", f"unknown providers {unknown}")
    bad = sorted(t for t in limits.tenants if not tenant_ok(t))
    if bad:
        raise LlmGatewayError(422, "invalid_value", f"invalid tenant names {bad}")


def check_upstreams(ps: PlatformSettings, settings: Settings) -> None:
    """켠 게이트웨이의 provider 주소 검사. 저장할 때 본다. 렌더도 틱마다 다시 본다."""
    if not settings.llm_gateway_on:
        return
    for p in ps.llm_providers:
        if not p.is_stub and (problem := upstream_problem(p, settings.reserved_namespaces)):
            raise LlmGatewayError(
                422, "invalid_value", f"llm provider {p.name!r}: {problem}", provider=p.name
            )
