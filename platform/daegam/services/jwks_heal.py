"""플랫폼보다 먼저 떠 JWKS 를 받지 못한 검증기를 다시 띄운다 (T24, D41).

ToolHive 검증기(내부 MCPServer 의 프록시, 데이터 플레인이 플랫폼을 믿는 모드의 vMCP 와 노출
앞단)는 플랫폼 JWKS 를 첫 토큰이 올 때 받는다. 노드가 다시 뜨면 검증기가 플랫폼보다 먼저 뜰 수
있고, 플랫폼이 준비되기 전에 첫 토큰(Agent 의 tools/list, vMCP 의 백엔드 health check)이 오면 처음
키를 받지 못한다. ToolHive 0.51.2 가 쓰는 httprc 는 실패한 조회를 최소 간격(15분) 뒤에야 다시
하므로 그동안 그 검증기는 모든 토큰을 401 로 거절한다.

리컨실 단계 `sweep` 이 이 프로세스가 뜬 뒤 15분 동안 틱마다 검증기에 새 서비스 토큰을 실어 한 번
불러 본다. 401 의 이유가 키 조회 실패(`failed to lookup JWKS`)면 그 Deployment 를 다시
띄운다(`kubectl rollout restart` 와 같다). 새 파드는 플랫폼이 준비된 뒤 뜨므로 처음부터 키를
받는다. 데이터 플레인 발급자를 바꾸면(P53b 6.2절) ConfigMap 이 바뀌어 플랫폼이 다시 뜨므로 바꾼
뒤에도 15분 동안 돈다. 그때 daegam-oidc 를 참조하는 vMCP 와 노출 앞단이 대상에 들어, 플랫폼
JWKS 를 처음 받지 못한 검증기(T76)를 다시 띄운다.

- 틱마다 먼저 플랫폼 자신의 JWKS 를 Service 로 받아 본다. 받지 못하면(플랫폼이 아직 준비되지
  않았거나 JWKS 가 503) 그 틱은 아무것도 부르지 않는다. 그때 부르면 프로브가 첫 토큰이 되어 멀쩡한
  검증기를 잠그고, 다시 띄워도 풀리지 않기 때문이다.
- 검증기는 서명 키부터 찾으므로 잠겼으면 audience 를 보기 전에 실패한다. 그래서 잠김의 판정은
  audience 와 상관없다. 그래도 토큰은 검증기가 받는 audience 를 싣는다. 자기 audience 로 옮겨 간
  내부 서버(P50 9절)는 CR 의 `oidcConfigRef.audience` 에서 서버 id 를 읽어 그 서버의 audience 를
  함께 싣고, 잠기지 않은 검증기는 옮겨 가기 전처럼 토큰을 받는다. 키 묶음에 없는 kid 로 거절된
  것(`key ID … not found in JWKS`, IdP 를 쓰는 검증기)은 잠긴 것이 아니므로 다시 띄우지 않는다.
- 다시 띄운 검증기는 `RESTART_COOLDOWN` 동안 부르지 않는다. 롤아웃 중에는 잠긴 옛 파드가 Service
  뒤에 남아 있어 곧바로 다시 부르면 또 굴리게 된다.
- 레플리카가 하나인 검증기는 한 번 정상으로 확인하면 다시 부르지 않는다. 플랫폼이 떠 있는 동안에는
  새로 잠길 수 없기 때문이다. 레플리카가 여럿이면 프로브가 Service 뒤의 파드 하나에만 가므로 창이
  닫힐 때까지 틱마다 부른다. 15분이 지나면 잠김이 스스로 풀리므로 단계도 멈춘다.
- 이 설치의 MCPOIDCConfig 가 있는 네임스페이스(`data_plane_auth.classify` 의 ours)만 본다.
  검증기는 같은 네임스페이스의 MCPOIDCConfig 만 참조할 수 있다.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Literal

import httpx2
from starlette.concurrency import run_in_threadpool

from daegam.auth.signer import audience_server_id
from daegam.config import Settings
from daegam.k8s.client import KubeClient, parse_service_url
from daegam.k8s.portforward import bearer_header, bearer_service_client
from daegam.k8s.templates.common import INTERNAL_OIDC_CONFIG_NAME, OIDC_CONFIG_NAME, TOOLHIVE_API
from daegam.k8s.templates.vmcp import vmcp_url
from daegam.services.claims import tick_item
from daegam.services.data_plane_auth import classify, live_oidc_configs
from daegam.services.svc_tokens import SvcTokens
from daegam.services.tenancy import TenancyService
from daegam.store.base import utcnow

logger = logging.getLogger("daegam.jwks_heal")

# 잠김이 스스로 풀리는 시간(httprc 의 최소 간격)이다. 그 뒤에는 다시 띄울 까닭이 없다
HEAL_WINDOW = timedelta(minutes=15)
# 다시 띄운 검증기를 다시 부르기까지 기다리는 시간. ToolHive 프록시의 startupProbe 가 90초까지 둔다
RESTART_COOLDOWN = timedelta(minutes=3)
# 검증기 하나를 불러 보는 상한(초)과 한 클러스터에서 함께 부르는 수
PROBE_TIMEOUT = 5.0
PROBE_CONCURRENCY = 8
# vMCP 는 인증보다 앞에서 Accept 를 본다. MCP streamable HTTP 클라이언트가 내는 값이다
MCP_ACCEPT = "application/json, text/event-stream"
# 잠긴 검증기의 401 이유(ToolHive `getKeyFromJWKS`)
LOCKED_MARKER = "failed to lookup JWKS"
# 검증기 CR 의 종류와 그 MCPOIDCConfig 참조 자리. ToolHive 는 셋 다 CR 이름으로 Deployment 를 만든다
VERIFIER_REFS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("MCPServer", ("spec", "oidcConfigRef", "name")),
    ("VirtualMCPServer", ("spec", "incomingAuth", "oidcConfigRef", "name")),
    ("MCPRemoteProxy", ("spec", "oidcConfigRef", "name")),
)

Probe = Literal["locked", "ok", "unknown"]


@dataclass(frozen=True)
class Verifier:
    """플랫폼 JWKS 로 토큰을 검증하는 CR 하나다. key 는 클러스터 안에서 CR 을 가르는 이름이다.
    server_id 는 자기 audience 로 옮겨 간 내부 서버의 id 이고, 테넌트 audience 로 검증하면 None
    이다."""

    kind: str
    name: str
    namespace: str
    tenant: str
    key: str
    # 부를 주소. 아직 없는 CR 은 `pick_verifiers(with_unaddressed=True)` 만 내고 그때 None 이다
    url: str | None
    replicas: int
    server_id: str | None = None


def platform_config_names(settings: Settings) -> frozenset[str]:
    """플랫폼 JWKS 를 가리키는 MCPOIDCConfig 이름. 서비스 토큰용은 언제나이고, 사람 토큰용은
    데이터 플레인이 플랫폼을 믿는 모드(local, platform 인 oidc)뿐이다. IdP 를 믿는 oidc 모드는 IdP
    를 본다 (P53b 6.2절)."""
    names = {INTERNAL_OIDC_CONFIG_NAME}
    if settings.human_jwks_url:
        names.add(OIDC_CONFIG_NAME)
    return frozenset(names)


def _dig(obj: dict[str, Any], path: tuple[str, ...]) -> Any:
    for key in path:
        obj = obj.get(key) if isinstance(obj, dict) else None
    return obj


def _probe_url(kind: str, obj: dict[str, Any]) -> str | None:
    """검증기를 부를 주소. vMCP 의 status.url 에는 경로가 없어 `/mcp` 를 붙인다."""
    url = (obj.get("status") or {}).get("url")
    if kind == "VirtualMCPServer" and url:
        meta = obj["metadata"]
        return vmcp_url(url, meta["name"], meta["namespace"])
    return str(url) if url else None


def pick_verifiers(
    kind: str,
    ref_path: tuple[str, ...],
    objs: Iterable[dict[str, Any]],
    config_names: frozenset[str],
    tenants: dict[str, str],
    *,
    with_unaddressed: bool = False,
) -> list[Verifier]:
    """CR 목록에서 tenants 의 네임스페이스에 있고 플랫폼 JWKS 를 쓰는 검증기를 고른다. 순수 함수.
    아직 주소가 없는 CR 은 부를 수 없으므로 뺀다. with_unaddressed 면 그것도 url 없이 낸다(발급자
    전환이 기다리는 것으로 센다)."""
    out = []
    for obj in objs:
        meta = obj.get("metadata") or {}
        namespace = meta.get("namespace")
        url = _probe_url(kind, obj)
        if namespace not in tenants or _dig(obj, ref_path) not in config_names:
            continue
        if not url and not with_unaddressed:
            continue
        tenant = tenants[namespace]
        audience = _dig(obj, (*ref_path[:-1], "audience"))
        out.append(
            Verifier(
                kind=kind,
                name=meta["name"],
                namespace=namespace,
                tenant=tenant,
                key=f"{kind}/{namespace}/{meta['name']}/{meta.get('uid')}",
                url=url,
                replicas=int((obj.get("spec") or {}).get("replicas") or 1),
                server_id=audience_server_id(
                    tenant, audience if isinstance(audience, str) else None
                ),
            )
        )
    return out


def jwks_locked(status: int, challenge: str) -> bool:
    """검증기가 키를 받지 못해 잠겼는가. 순수 함수. 401 의 WWW-Authenticate 이유로 가른다."""
    return status == 401 and LOCKED_MARKER in challenge


class JwksHeal:
    """리컨실 단계다. 프로세스가 뜬 뒤 `HEAL_WINDOW` 동안만 돈다."""

    def __init__(
        self,
        tenancy: TenancyService,
        svc_tokens: SvcTokens,
        settings: Settings,
        *,
        started_at: datetime | None = None,
    ) -> None:
        self.tenancy = tenancy
        self.svc_tokens = svc_tokens
        self.settings = settings
        self.started_at = started_at or utcnow()
        # 잠기지 않은 것으로 확인한 검증기(클러스터 id 와 key). 다시 부르지 않는다
        self._healthy: set[tuple[str, str]] = set()
        # 다시 띄운 검증기와 그 시각. 쿨다운 동안 부르지 않는다
        self._restarted: dict[tuple[str, str], datetime] = {}

    async def sweep(self, now: datetime) -> dict[str, list[str]]:
        """잠긴 검증기를 찾아 다시 띄운다. 결과는 다시 띄운 것(`restarted`)과 보지 못한 클러스터
        (`skipped`)다. 플랫폼 JWKS 를 받지 못한 틱은 `skipped` 가 `jwks` 하나다."""
        if now - self.started_at > HEAL_WINDOW or not self.svc_tokens.available:
            return {}
        if not await self._jwks_served():
            return {"restarted": [], "skipped": ["jwks"]}
        install_id = await self.tenancy.platform_settings.install_id()
        names = platform_config_names(self.settings)
        restarted: list[str] = []
        skipped: list[str] = []
        for cluster in await self.tenancy.clusters.list():
            try:
                client = await run_in_threadpool(self.tenancy.cluster_service.client_for, cluster)
                found = await run_in_threadpool(platform_verifiers, client, install_id, names)
                due = [v for v in found if self._due(cluster.id, v, now)]
                results = await self._probe_all(client, due)
                for v, result in zip(due, results, strict=True):
                    if result == "ok" and v.replicas == 1:
                        self._healthy.add((cluster.id, v.key))
                    elif result == "locked" and await self._restart(client, v):
                        self._restarted[(cluster.id, v.key)] = now
                        restarted.append(f"{cluster.name}/{v.namespace}/{v.kind}/{v.name}")
            except Exception as e:  # noqa: BLE001 - 한 클러스터의 실패가 다른 클러스터를 막지 않는다
                logger.warning("jwks heal skipped cluster %s: %s", cluster.name, e)
                skipped.append(cluster.name)
        return {"restarted": restarted, "skipped": skipped}

    def _due(self, cluster_id: str, v: Verifier, now: datetime) -> bool:
        """이번 틱에 부를 검증기인가. 정상으로 확인했거나 쿨다운 중이면 아니다."""
        key = (cluster_id, v.key)
        if key in self._healthy:
            return False
        restarted = self._restarted.get(key)
        return restarted is None or now - restarted >= RESTART_COOLDOWN

    async def _jwks_served(self) -> bool:
        return await jwks_served(self.settings.service_jwks_url)

    async def _probe_all(self, client: KubeClient, verifiers: list[Verifier]) -> list[Probe]:
        gate = asyncio.Semaphore(PROBE_CONCURRENCY)

        async def one(v: Verifier) -> Probe:
            async with gate:
                # 터널을 여는 요청은 훅을 지나지 않으므로 탐침마다 틱의 리스를 확인한다.
                # 늘리기는 1초에 한 번까지다 (P67 2절)
                await tick_item()
                return await self._probe(client, v)

        tasks = [asyncio.create_task(one(v)) for v in verifiers]
        try:
            return list(await asyncio.gather(*tasks))
        except BaseException:
            # 하나가 멈추면(리스를 잃음 등) 남은 탐침이 이 틱 밖에서 돌지 않게 거둔다
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            raise

    async def _probe(self, client: KubeClient, v: Verifier) -> Probe:
        """새 서비스 토큰으로 검증기를 한 번 불러 본다(`probe_verifier`). 연결하지 못하거나(파드가
        뜨는 중) 주소를 풀지 못하면 unknown 이라 다음 틱에 다시 본다."""
        token = await self.svc_tokens.platform_token(v.tenant, v.server_id)
        answer = await probe_verifier(client, v.url, token) if v.url else None
        if answer is None:
            return "unknown"
        return "locked" if jwks_locked(*answer) else "ok"

    async def _restart(self, client: KubeClient, v: Verifier) -> bool:
        if not await run_in_threadpool(client.rollout_restart, v.name, v.namespace):
            return False
        logger.warning(
            "%s %s/%s could not fetch the platform JWKS; restarted it", v.kind, v.namespace, v.name
        )
        return True


async def jwks_served(url: str) -> bool:
    """플랫폼 자신의 JWKS 를 Service 로 받을 수 있는가. 검증기가 받는 주소 그대로다. 받지 못하는
    틱에 검증기를 부르면 그 검증기가 JWKS 를 못 받아 잠길 수 있으므로(T24, T76) 그 틱은 부르지
    않는다. JWKS 치유와 발급자 전환의 확인이 함께 쓴다."""
    try:
        async with httpx2.AsyncClient(timeout=PROBE_TIMEOUT) as hc:
            return (await hc.get(url)).status_code == 200
    except httpx2.HTTPError:
        return False


async def probe_verifier(client: KubeClient, url: str, bearer: str) -> tuple[int, str] | None:
    """클러스터 안 주소의 검증기를 bearer 로 한 번 GET 해 (상태 코드, WWW-Authenticate) 를 준다.
    본문은 읽지 않는다. MCP 엔드포인트가 SSE 를 열어도 머리만 보고 닫는다. 주소를 풀지 못하거나
    부르지 못하면(파드가 뜨는 중) None 이다. JWKS 치유와 발급자 전환의
    확인(`services/issuer_switch.py`)이 함께 쓴다."""
    target = parse_service_url(url)
    if target is None:
        return None
    namespace, service, port, path = target
    try:
        async with bearer_service_client(
            client, namespace, service, port, headers=bearer_header(bearer), timeout=PROBE_TIMEOUT
        ) as (hc, base_url):
            headers = {"Accept": MCP_ACCEPT}
            async with hc.stream("GET", f"{base_url}{path}", headers=headers) as r:
                return r.status_code, r.headers.get("www-authenticate", "")
    except Exception as e:  # noqa: BLE001 - 부르지 못한 것은 다음 틱에 다시 본다
        logger.debug("probe of %s failed: %s", url, e)
        return None


def platform_verifiers(
    client: KubeClient, install_id: str, names: frozenset[str], *, with_unaddressed: bool = False
) -> list[Verifier]:
    """이 설치의 네임스페이스에서 플랫폼 JWKS 를 쓰는 검증기를 모은다. 종류마다 클러스터 전체를 한
    번 읽는다. threadpool 에서 돈다. with_unaddressed 는 `pick_verifiers` 의 것이다."""
    ours, _unlabeled, _foreign = classify(live_oidc_configs(client), install_id)
    if not ours:
        return []
    out: list[Verifier] = []
    for kind, ref_path in VERIFIER_REFS:
        objs = client.list(TOOLHIVE_API, kind)
        out.extend(
            pick_verifiers(kind, ref_path, objs, names, ours, with_unaddressed=with_unaddressed)
        )
    return out
