"""배포가 함께 올린 Redis 의 TLS 끝점을 home 의 기록에 더한다 (P61 5절 3단계).

그 Redis 를 두는 배포는 같은 클러스터의 Redis 에 TLS 포트를 열고 그 공개 인증서를 플랫폼
네임스페이스의 ConfigMap 에 둔다(`DAEGAM_HOME_REDIS_TLS`, `services/shipped_redis_ca.py`). 기동 때
그 Redis 의 호스트와 평문 포트를 가리키는 home 의 Redis Database 와 클러스터 기본 Redis 가 TLS
끝점과 그 CA 를 얻는다. 평문 포트는 그대로라 아직 TLS 를 쓰지 못하는 소비자는 평문으로 붙고,
플랫폼은 곧바로, Agent 는 이미지가 `redis_tls` 를 알리면, MCP Server 는 다음 배포 때 옮겨 간다.

그 배포가 평문 포트를 닫으면(설정의 `plain` 이 거짓) 기동이 그 Redis 를 가리키는 기록에 닫힌
표시(`plain_closed`)를 적고, 다시 열면 지운다(5절 5단계). 닫기 전에 그 배포가 끊길 것이 없는지
본다(`redis_tls_consumers.plain_close_blockers`,
`daegam.shipped_redis_check`).

한 기록에 한 번만 더하고 뒤의 고침을 따른다. 더한 기록에는 그때의 CA 지문을 적는다
(`ShippedTls`). 관리자가 TLS 끝점을 지웠으면 다시 더하지 않고, 저장된 CA 가 그때 더한 것과
다르면(관리자가 바꾼 묶음) 건드리지 않는다. 저장된 CA 가 그때 더한 것과 같고 함께 올린 CA 가
바뀌었으면 새 CA 로 고친다. 고친 주체는 `updated_by` 에 남는다. 한 기록이 실패해도 다른 기록과
기동은 계속된다.
"""

from __future__ import annotations

import logging

from daegam.config import Settings, ShippedRedisTls
from daegam.pem import ca_sha256
from daegam.services.clusters import ClusterService
from daegam.services.data_services import DataServiceService, TlsInput, data_service_secrets
from daegam.services.shipped_redis_ca import read_shipped_ca
from daegam.store.base import utcnow
from daegam.store.clusters import (
    Cluster,
    DefaultRedis,
    DefaultRedisTls,
)
from daegam.store.data_services import DataService, ShippedTls

logger = logging.getLogger("daegam.shipped_redis")

# 기록을 고친 주체. updated_by 에 남는다
SHIPPED_REDIS_SUB = "svc:platform"


async def adopt_shipped_redis_tls(
    settings: Settings, clusters: ClusterService, data_services: DataServiceService
) -> int:
    """고친 기록의 수. 설정이 없거나 home·CA 를 읽지 못하면 0 이다. 예외를 던지지 않는다."""
    shipped = settings.home_redis_tls
    if shipped is None:
        return 0
    try:
        found = await read_shipped_ca(clusters.home_client(), settings.namespace, shipped)
        home = await clusters.home()
        if found is None or home is None:
            return 0
        records = await data_services.store.redis_at(home.id, shipped)
    except Exception as e:  # noqa: BLE001
        logger.warning("the shipped Redis TLS endpoint is not adopted: %s", e)
        return 0
    marker = ShippedTls(at=utcnow(), ca_sha256=ca_sha256(found.ca) or "")
    changed = 0
    for ds in records:
        try:
            changed += await _adopt_database(ds, found.ca, marker, shipped, data_services)
        except Exception as e:  # noqa: BLE001
            logger.warning("data service %s/%s keeps its record: %s", ds.tenant, ds.name, e)
    try:
        changed += await _adopt_default_redis(home, found.ca, marker, shipped, clusters)
    except Exception as e:  # noqa: BLE001
        logger.warning("cluster %s keeps its default_redis: %s", home.name, e)
    if changed:
        logger.info("the shipped Redis TLS endpoint reached %d record(s)", changed)
    return changed


def _wanted(
    stored_ca_sha: str | None, has_tls: bool, previous: ShippedTls | None, marker: ShippedTls
) -> bool:
    """이 기록에 함께 올린 CA 를 (다시) 둘 것인가."""
    if previous is None:
        # 처음 보는 기록은 TLS 끝점이 없을 때만 더한다. 관리자가 둔 끝점은 그대로다
        return not has_tls
    if not has_tls:
        # 더했는데 지금 없으면 관리자가 지운 것이다
        return False
    # 그때 더한 CA 가 그대로 있고 함께 올린 CA 가 바뀌었을 때만 고친다
    return stored_ca_sha == previous.ca_sha256 and previous.ca_sha256 != marker.ca_sha256


async def _adopt_database(
    ds: DataService,
    ca: str,
    marker: ShippedTls,
    shipped: ShippedRedisTls,
    service: DataServiceService,
) -> int:
    stored = None
    if ds.tls is not None and ds.shipped_tls is not None:
        stored = ca_sha256((await data_service_secrets(ds, service.tenancy, service.clusters)).ca)
    if not _wanted(stored, ds.tls is not None, ds.shipped_tls, marker):
        return 0
    await service.update(
        ds, {"tls": TlsInput(shipped.tls_port, ca)}, user_sub=SHIPPED_REDIS_SUB, shipped_tls=marker
    )
    logger.info(
        "data service %s/%s gained the TLS endpoint :%d", ds.tenant, ds.name, shipped.tls_port
    )
    # 플랫폼은 TLS 끝점이 있으면 그것으로만 붙으므로 바로 확인해 둔다. 확인이 실패하거나 그 결과를
    # 남기지 못해도 기록은 바뀌었다
    try:
        result = await service.verify(ds)
    except Exception as e:  # noqa: BLE001
        logger.warning("data service %s/%s: verify after adopt failed: %s", ds.tenant, ds.name, e)
    else:
        if not result.ok:
            logger.warning(
                "data service %s/%s does not answer on its TLS endpoint: %s",
                ds.tenant,
                ds.name,
                result.detail,
            )
    return 1


async def _adopt_default_redis(
    home: Cluster,
    ca: str,
    marker: ShippedTls,
    shipped: ShippedRedisTls,
    clusters: ClusterService,
) -> int:
    default = home.default_redis
    if default is None or not _is_shipped(default, shipped):
        return 0
    stored = ca_sha256(default.tls.ca) if default.tls is not None else None
    if not _wanted(stored, default.tls is not None, home.shipped_redis_tls, marker):
        return 0
    tls = (
        default.tls.model_copy(update={"port": shipped.tls_port, "ca": ca})
        if default.tls is not None
        else DefaultRedisTls(port=shipped.tls_port, ca=ca)
    )
    home.shipped_redis_tls = marker
    if not await _save_default_redis(
        home, default.model_copy(update={"tls": tls}), clusters, "shipped_redis_tls"
    ):
        return 0
    logger.info("cluster %s default_redis gained the TLS endpoint :%d", home.name, shipped.tls_port)
    return 1


async def _save_default_redis(
    home: Cluster, default: DefaultRedis, clusters: ClusterService, *names: str
) -> bool:
    """home 의 기본 Redis 를 플랫폼의 이름으로 저장한다. names 는 함께 저장할 칸이다. 다른
    레플리카의 기동이나 사람의 저장이 먼저 지나갔으면 저장하지 않고 거짓이다."""
    home.default_redis = default
    home.updated_at = utcnow()
    home.updated_by = SHIPPED_REDIS_SUB
    if await clusters.store.save_edit(home, {"default_redis", "updated_at", "updated_by", *names}):
        return True
    logger.info(
        "cluster %s was saved by another writer first; its default_redis is left", home.name
    )
    return False


def _is_shipped(default: DefaultRedis, shipped: ShippedRedisTls) -> bool:
    """클러스터 기본 Redis 가 함께 올린 Redis 의 평문 주소를 가리키는가."""
    return shipped.serves(*default.host_port())


async def mark_shipped_plain(
    settings: Settings, clusters: ClusterService, data_services: DataServiceService
) -> int:
    """함께 올린 Redis 의 평문 포트가 닫혔는지(`ShippedRedisTls.plain`)를 home 의 기록에 적는다.
    닫혔으면 그 주소를 가리키고 TLS 끝점을 가진 Database 와 기본 Redis 가 표시를 얻고, 그 밖의
    기록의 표시는 지워진다. 열렸으면 어느 주소의 표시도 지운다. 닫을 수 있는 것은 그 Redis
    하나라서다. 주소는 Database·클러스터 저장과 같은 판정이다(`ShippedRedisTls.serves`). 바뀐 기록의
    수이고 예외를 던지지 않는다. 닫혔는데 TLS 끝점이 없는 Database 는 경고만 남긴다."""
    shipped = settings.home_redis_tls
    if shipped is None:
        return 0
    closed = not shipped.plain
    try:
        home = await clusters.home()
        if home is None:
            return 0
        changed = await _mark_databases(home, shipped, closed, data_services)
        changed += await _mark_default_redis(shipped, closed, clusters)
    except Exception as e:  # noqa: BLE001
        logger.warning("the shipped Redis plain port state is not recorded: %s", e)
        return 0
    if changed:
        state = "closed" if closed else "open"
        logger.info("%d record(s) now follow the shipped Redis plain port: %s", changed, state)
    return changed


async def _mark_databases(
    home: Cluster, shipped: ShippedRedisTls, closed: bool, data_services: DataServiceService
) -> int:
    records = await data_services.store.redis_at(home.id, shipped) if closed else []
    for ds in records:
        if ds.tls is None:
            logger.warning(
                "data service %s/%s points at the closed plain port without a TLS endpoint",
                ds.tenant,
                ds.name,
            )
    ids = [ds.id for ds in records if ds.tls is not None]
    changed = await data_services.store.clear_plain_closed(keep=ids)
    return changed + (await data_services.store.set_plain_closed(ids) if ids else 0)


async def _mark_default_redis(
    shipped: ShippedRedisTls, closed: bool, clusters: ClusterService
) -> int:
    """home 의 기본 Redis 표시를 맞춘다. 다른 저장에 지면 한 번 다시 읽어 다시 한다."""
    for _ in range(2):
        home = await clusters.home()
        default = home.default_redis if home is not None else None
        if home is None or default is None:
            return 0
        want = closed and default.tls is not None and _is_shipped(default, shipped)
        if default.plain_closed == want:
            return 0
        marked = default.model_copy(update={"plain_closed": want})
        if await _save_default_redis(home, marked, clusters):
            return 1
    return 0
