"""TLS 끝점을 가진 Redis 에 아직 평문 포트로 붙는 소비자 (P61 5절 4단계).

평문 포트를 닫기 전에 관리자가 소유자에게 알리고 다시 배포할 대상이다. 강제로 다시 배포하지
않는다. 그러면 고정한 이미지가 바뀌고 Agent 가 다시 컴파일되어 실패할 수 있기 때문이다. 세는 것은
렌더가 실제로 한 것이다.

- Agent: 바인딩한 Redis 에 TLS 끝점이 있는데 마지막 렌더가 그것을 TLS 로 붙이지 않았다
  (`Agent.redis_tls_bindings`). 런타임 이미지가 `redis_tls` 를 모르면 runtime_image, 알면 다음
  재적용을 기다리는 binding 이다.
- MCP Server: 배포 스냅샷의 바인딩이 TLS 끝점 없이 렌더됐는데 지금 그 Redis 에 끝점이 있다(데이터
  바인딩과 그 바인딩의 세션 저장소), 또는 세션 저장소가 평문으로 렌더됐는데 지금 그 Redis(바인딩이나
  같은 주소의 클러스터 기본 Redis, 또는 함께 올린 Redis)에 끝점이 있다. 배포 스냅샷이 없는 서버는
  TLS Redis 바인딩과 기본 Redis 세션 저장소마다 unknown 이다.

주소는 `redis_host_key` 로 견준다. 같은 Redis 를 ClusterIP 나 다른 별칭으로 적은 기록은 가리지
못한다.
"""

from __future__ import annotations

import dataclasses
import logging
import time
from collections.abc import Awaitable, Callable, Hashable, Iterable
from dataclasses import dataclass
from typing import Any, Literal

from pydantic import ValidationError

from daegam.config import ShippedRedisTls, redis_host_key
from daegam.config.resolve import EffectiveConfig
from daegam.k8s.templates.mcpserver import SESSION_REPLICAS
from daegam.services.agent_features import REDIS_TLS
from daegam.store.agents import Agent, MongoAgentStore
from daegam.store.clusters import Cluster, MongoClusterStore
from daegam.store.data_services import DataService, MongoDataServiceStore
from daegam.store.mcp_servers import MCPServer, MongoMCPServerStore

logger = logging.getLogger("daegam.redis_tls")

# 재적용 틱 안에서 같은 것을 다시 읽지 않는 시간(초) (`RecentReads`). 틱 간격보다 짧다
RECENT_READ_SECONDS = 5.0

PlainReason = Literal["runtime_image", "binding", "session_storage", "unknown"]
Lookup = Callable[[str], Awaitable[DataService | None]]


@dataclass(frozen=True)
class PlainConsumer:
    """평문 포트로 붙는 소비자 하나. data_service_id 가 None 이면 클러스터 기본 Redis 나 모르는
    바인딩이다."""

    tenant: str
    kind: Literal["agent", "mcp_server"]
    id: str
    name: str
    data_service_id: str | None
    reason: PlainReason


async def tls_redis_bindings(effective: EffectiveConfig, lookup: Lookup) -> list[DataService]:
    """이 배포가 바인딩한 Database 가운데 TLS 끝점을 가진 Redis."""
    out: list[DataService] = []
    for b in effective.bindings:
        ds = await lookup(b.data_service_id)
        if ds is not None and ds.has_redis_tls:
            out.append(ds)
    return out


class RecentReads:
    """재적용 틱은 문서마다 바인딩한 Database 와 네임스페이스의 CA Secret 을 보므로, 한 번 읽은
    것을 읽기가 끝난 때부터 `RECENT_READ_SECONDS` 동안 다시 쓴다. 읽기가 오래 걸려도 다음 문서가
    다시 읽지 않게 하려는 것이다. key 는 읽는 것을 가른다. load 가 예외로 끝나면 남기지 않으므로,
    실패도 남기려면 load 가 실패를 값으로 돌려준다."""

    def __init__(self) -> None:
        self._seen: dict[Hashable, tuple[float, Any]] = {}

    async def get[V](self, key: Hashable, load: Callable[[], Awaitable[V]]) -> V:
        cached = self._seen.get(key)
        if cached is not None and time.monotonic() - cached[0] < RECENT_READ_SECONDS:
            return cached[1]
        value = await load()
        self._seen[key] = (time.monotonic(), value)
        return value

    async def database(
        self, store: MongoDataServiceStore, ds_id: str, tenant: str
    ) -> DataService | None:
        return await self.get(("database", tenant, ds_id), lambda: store.get(ds_id, tenant=tenant))


class _Databases:
    """한 번의 계산 안에서 같은 Database 를 다시 읽지 않는다."""

    def __init__(self, store: MongoDataServiceStore, tenant: str | None):
        self.store = store
        self.tenant = tenant
        self._seen: dict[str, DataService | None] = {}

    async def get(self, ds_id: str) -> DataService | None:
        if ds_id not in self._seen:
            self._seen[ds_id] = await self.store.get(ds_id, tenant=self.tenant)
        return self._seen[ds_id]

    async def tls_redis(self, ds_id: str) -> DataService | None:
        ds = await self.get(ds_id)
        return ds if ds is not None and ds.has_redis_tls else None


def stored_effective(doc: Agent | MCPServer) -> EffectiveConfig | None:
    """저장된 배포 스냅샷. 없거나 읽지 못하면 None 이다. 그 문서 하나 때문에 재적용 틱이나 init
    받기가 멈추지 않게 읽지 못한 것은 로그만 남긴다."""
    if not doc.effective_config:
        return None
    try:
        return EffectiveConfig.model_validate(doc.effective_config)
    except ValidationError as e:
        logger.warning("%s %s: effective_config unreadable: %s", type(doc).__name__, doc.id, e)
        return None


async def _agent_rows(agent: Agent, dbs: _Databases) -> list[PlainConsumer]:
    effective = stored_effective(agent)
    if effective is None:
        return []
    rendered = set(agent.redis_tls_bindings or ())
    reason: PlainReason = "binding" if agent.image_reports(REDIS_TLS) else "runtime_image"
    return [
        PlainConsumer(agent.tenant, "agent", agent.id, agent.name, ds.id, reason)
        for ds in await tls_redis_bindings(effective, dbs.get)
        if ds.id not in rendered
    ]


@dataclass(frozen=True)
class ShippedAt:
    """배포가 함께 올린 Redis 와 그것이 도는 home 클러스터 (`DAEGAM_HOME_REDIS_TLS`). 기본 Redis 를
    세션 저장소로 쓰는 서버의 스냅샷 주소가 그 Redis 이면 클러스터의 지금 기본 Redis 와 상관없이
    TLS 끝점이 있는 Redis 다."""

    redis: ShippedRedisTls
    home_id: str


def shipped_at(redis: ShippedRedisTls | None, home: Cluster | None) -> ShippedAt | None:
    """설정의 함께 올린 Redis 와 home 클러스터. 어느 하나가 없으면 None 이다."""
    return ShippedAt(redis, home.id) if redis is not None and home is not None else None


def _default_has_tls(
    snapshot: EffectiveConfig, cluster: Cluster | None, shipped: ShippedAt | None
) -> bool:
    """스냅샷의 기본 Redis 에 지금 TLS 끝점이 있는가. 지금 클러스터의 기본 Redis 가 같은 곳을
    가리키고 TLS 끝점을 가졌거나, 그 주소가 함께 올린 Redis 이다. 주소는 `redis_host_key` 로
    견준다."""
    default = snapshot.default_redis
    if default is None:
        return False
    host, port = default.host_port()
    if shipped is not None and cluster is not None and cluster.id == shipped.home_id:
        if shipped.redis.serves(host, port):
            return True
    current = cluster.default_redis if cluster is not None else None
    if current is None or current.tls is None:
        return False
    now_host, now_port = current.host_port()
    return redis_host_key(host) == redis_host_key(now_host) and port == now_port


async def _unknown_rows(
    server: MCPServer, effective: EffectiveConfig, dbs: _Databases, default_tls: bool
) -> list[PlainConsumer]:
    """배포 스냅샷이 없는 서버의 줄. 렌더가 무엇을 했는지 모르므로 TLS Redis 바인딩마다, 그리고
    Redis 바인딩 없이 TLS 끝점이 있는 기본 Redis 를 세션 저장소로 쓰는 복제 서버면 그것도 unknown
    이다."""
    tls_bound = await tls_redis_bindings(effective, dbs.get)
    rows = [
        PlainConsumer(server.tenant, "mcp_server", server.id, server.name, ds.id, "unknown")
        for ds in tls_bound
    ]
    if effective.replicas >= SESSION_REPLICAS and default_tls:
        redis_bound = False
        for b in effective.bindings:
            ds = await dbs.get(b.data_service_id)
            redis_bound = redis_bound or (ds is not None and ds.kind == "redis")
        if not redis_bound:
            rows.append(
                PlainConsumer(server.tenant, "mcp_server", server.id, server.name, None, "unknown")
            )
    return rows


async def _mcp_rows(
    server: MCPServer, dbs: _Databases, clusters: dict[str, Cluster], shipped: ShippedAt | None
) -> list[PlainConsumer]:
    def row(ds_id: str | None, reason: PlainReason) -> PlainConsumer:
        return PlainConsumer(server.tenant, "mcp_server", server.id, server.name, ds_id, reason)

    inputs = server.deployed_inputs
    effective = stored_effective(server)
    if effective is None:
        return []
    default_tls = _default_has_tls(effective, clusters.get(server.cluster_id), shipped)
    if inputs is None:
        return await _unknown_rows(server, effective, dbs, default_tls)
    rows: list[PlainConsumer] = []
    session_redis = None
    for b in inputs.bindings:
        if b.kind != "redis":
            continue
        session_redis = session_redis or b
        if b.tls is None and await dbs.tls_redis(b.data_service_id) is not None:
            rows.append(row(b.data_service_id, "binding"))
    if effective.replicas < SESSION_REPLICAS or inputs.session_tls:
        return rows
    if session_redis is not None:
        # 세션 저장소는 첫 Redis 바인딩이다. 그 바인딩이 평문이면 위에서 이미 셌다
        if session_redis.tls is not None and await dbs.tls_redis(session_redis.data_service_id):
            rows.append(row(session_redis.data_service_id, "session_storage"))
    elif default_tls:
        rows.append(row(None, "session_storage"))
    return rows


async def plain_redis_consumers(
    agents: MongoAgentStore,
    mcp_servers: MongoMCPServerStore,
    data_services: MongoDataServiceStore,
    clusters: MongoClusterStore,
    *,
    tenant: str | None = None,
    data_service_id: str | None = None,
    shipped: ShippedAt | None = None,
) -> list[PlainConsumer]:
    """살아 있는 Agent 와 MCP Server 가운데 평문 포트로 붙는 것. data_service_id 를 주면 그 Database
    를 바인딩한 것만 읽고 그 Database 의 줄만 낸다(기본 Redis 줄은 빠진다). tenant 를 주면 그
    테넌트만이다. shipped 는 함께 올린 Redis 이고, 기본 Redis 줄을 셀 때 스냅샷 주소를 그것과도
    견준다(`_default_has_tls`)."""
    dbs = _Databases(data_services, tenant)
    if data_service_id is not None:
        agent_docs: Iterable[Agent] = await agents.live_binding_data_service(
            data_service_id, tenant=tenant, deployed_only=True
        )
        server_docs: Iterable[MCPServer] = await mcp_servers.live_binding_data_service(
            data_service_id, tenant=tenant, deployed_only=True
        )
        known: dict[str, Cluster] = {}
    else:
        agent_docs, server_docs = await agents.live(), await mcp_servers.live()
        # 세션 저장소가 클러스터 기본 Redis 인 서버를 보려고 클러스터는 한 번에 읽는다
        known = {c.id: c for c in await clusters.list()}
    rows: list[PlainConsumer] = []
    for agent in _of_tenant(agent_docs, tenant):
        rows.extend(await _agent_rows(agent, dbs))
    for server in _of_tenant(server_docs, tenant):
        rows.extend(await _mcp_rows(server, dbs, known, shipped))
    if data_service_id is not None:
        rows = [r for r in rows if r.data_service_id == data_service_id]
    return rows


async def plain_close_blockers(
    shipped: ShippedRedisTls | None,
    home: Cluster | None,
    clusters: MongoClusterStore,
    data_services: MongoDataServiceStore,
    agents: MongoAgentStore,
    mcp_servers: MongoMCPServerStore,
) -> list[str]:
    """함께 올린 Redis 의 평문 포트를 닫으면 끊길 것. 비면 닫아도 된다 (P61 5절 5단계). 그 Redis 를
    가리키는데 TLS 끝점이 없는 Database 와 home 의 기본 Redis, 그리고 아직 평문 포트로 붙는
    소비자다. 기본 Redis 를 세션 저장소로 쓰는 소비자 줄은 어느 클러스터의 것인지 가르지 않고 모두
    센다. 덜 닫는 쪽으로 틀린다. 읽기만 한다. shipped 는 설정의 `home_redis_tls`, home 은 home
    클러스터다. `daegam.shipped_redis_check` 가 플랫폼 파드에서 부른다."""
    if shipped is None:
        return ["DAEGAM_HOME_REDIS_TLS is not set, so this platform has no shipped Redis"]
    if home is None:
        return ["there is no home cluster"]
    records = await data_services.redis_at(home.id, shipped)
    out = [
        f"database {ds.tenant}/{ds.name} has no TLS endpoint" for ds in records if ds.tls is None
    ]
    default = home.default_redis
    if default is not None and default.tls is None and shipped.serves(*default.host_port()):
        out.append(f"cluster {home.name} default_redis has no TLS endpoint")
    ids = {ds.id for ds in records}
    rows = await plain_redis_consumers(
        agents, mcp_servers, data_services, clusters, shipped=shipped_at(shipped, home)
    )
    out.extend(
        f"{r.kind} {r.tenant}/{r.name} still connects to the plain port ({r.reason})"
        for r in rows
        if r.data_service_id is None or r.data_service_id in ids
    )
    return out


def _of_tenant[D: (Agent, MCPServer)](docs: Iterable[D], tenant: str | None) -> list[D]:
    return [d for d in docs if tenant is None or d.tenant == tenant]


def by_tenant(rows: Iterable[PlainConsumer]) -> dict[str, list[PlainConsumer]]:
    """상태 API 의 모양: 테넌트마다의 줄."""
    out: dict[str, list[PlainConsumer]] = {}
    for r in rows:
        out.setdefault(r.tenant, []).append(r)
    return out


def plain_row(row: PlainConsumer) -> dict[str, str | None]:
    """API 의 한 줄 (`PlainRedisConsumerOut`). 테넌트는 상태 API 가 묶는 키라 빠진다."""
    out = dataclasses.asdict(row)
    del out["tenant"]
    return out
