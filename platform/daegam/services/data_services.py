"""Data Service(화면의 Database): 쓰기 규칙, 비밀번호 읽기, 연결 확인, 바인딩 클러스터 일치 검사.

비밀번호는 테넌트 네임스페이스 Secret `ds-{id}` 에만 있고 `data_service_password` 하나가 그
Database 의 클러스터 client 로 읽는다. verify(redis·postgres)·Knowledge Base 생성·인덱서가 같은
함수를 쓴다(D30). postgres 는 psycopg 로 접속해 pgvector 확장까지 보고(D32), redis 는 redis-py
(`redis.asyncio`)로 PING 해 판정한 뒤 버전과 모듈(RediSearch·RedisJSON)을 봐서 memory 노드가
쓸 수 있는지(`capabilities.checkpointer`)를 남긴다(D33). 장기 기억을 지우는 자리도 여기의
`redis_client_kwargs` 로 같은 Redis 에 붙는다(D34, `services/agent_memory.py`).
"""

from __future__ import annotations

import logging
import ssl
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from pymongo.errors import DuplicateKeyError
from redis.asyncio import Redis as AsyncRedis
from redis.exceptions import AuthenticationError, RedisError, ResponseError
from redis.exceptions import ConnectionError as RedisConnectionError
from redis.exceptions import TimeoutError as RedisTimeoutError
from starlette.concurrency import run_in_threadpool

from daegam.errors import ServiceError, revision_conflict
from daegam.k8s.client import AccessDenied, ClusterUnreachable, exception_chain
from daegam.k8s.gates import require_tenant_secrets
from daegam.k8s.labels import daegam_labels
from daegam.pem import ca_sha256, is_ca_pem
from daegam.secrets import K8sSecretStore
from daegam.services.clusters import ClusterService
from daegam.services.knowledge.pg import verify_postgres
from daegam.services.references import DATA_SERVICE_DELETE, ReferenceService
from daegam.services.tenancy import TenancyError, TenancyService
from daegam.store.base import NothingWritten, save_with_edit_claim, utcnow
from daegam.store.clusters import Cluster
from daegam.store.data_services import (
    REDIS_CA_KEY,
    DataService,
    MongoDataServiceStore,
    RedisTls,
    ShippedTls,
    ca_secret_name_for,
    secret_name_for,
)

logger = logging.getLogger("daegam.data_services")

# postgres verify 의 문장 타임아웃. 생성의 DDL 은 30초, 인덱서는 120초다
VERIFY_STATEMENT_TIMEOUT = 5.0

# langgraph-checkpoint-redis 의 asetup() 이 요구하는 모듈. MODULE LIST 의 name 을 소문자로 비교한다
CHECKPOINTER_MODULES = frozenset({"search", "rejson"})
# MODULE LIST 가 거부됐을 때 대신 부르는 명령. 예외가 없으면 그 모듈이 있는 것으로 보고 반환값은
# 비교하지 않는다(FT._LIST 는 [], JSON.TYPE 은 키가 없을 때 [None] 을 돌려준다)
MODULE_PROBES: tuple[tuple[str, ...], ...] = (("FT._LIST",), ("JSON.TYPE", "__daegam_probe__"))


@dataclass(frozen=True)
class VerifyResult:
    ok: bool
    error: str | None = None
    detail: str | None = None
    # postgres 만. {vector: {installed: "0.8.6"|None, available: bool}}
    extensions: dict[str, Any] | None = None
    # redis 만. {version: "8.2.1"|None, modules: [name, ...]|None,
    #            checkpointer: "ready"|"modules_missing"|"unknown"}
    redis: dict[str, Any] | None = None


def pgvector_capability(extensions: dict[str, Any] | None) -> str | None:
    """verify 의 extensions 를 문서의 `capabilities.pgvector` 값으로. postgres 가 아니면 None."""
    if extensions is None:
        return None
    vector = extensions.get("vector") or {}
    if vector.get("installed"):
        return "installed"
    if vector.get("available"):
        return "available"
    return "missing"


def checkpointer_capability(redis: dict[str, Any] | None) -> str | None:
    """verify 의 redis 결과를 문서의 `capabilities.checkpointer` 값으로. redis 가 아니면 None."""
    if redis is None:
        return None
    return str(redis.get("checkpointer") or "unknown")


# -- 비밀번호 ---------------------------------------------------------------------


async def secret_store_for(
    tenancy: TenancyService, clusters: ClusterService, cluster_id: str
) -> tuple[Cluster, K8sSecretStore]:
    """그 클러스터의 Secret 저장소. 없는 클러스터는 TenancyError(422 unknown_cluster), 닿지 않으면
    ClusterUnreachable 이 그대로 올라간다. K8s 호출은 스레드풀에서 한다."""
    cluster = await tenancy.clusters.get(cluster_id)
    if cluster is None:
        raise TenancyError(422, "unknown_cluster", f"cluster {cluster_id} not found")
    client = await run_in_threadpool(clusters.client_for, cluster)
    return cluster, K8sSecretStore(client)


@dataclass(frozen=True)
class DataServiceSecrets:
    """Database 의 비밀 둘. password 는 Secret `ds-{id}` 의 것(무인증 Redis 는 None)이고 ca 는 TLS
    끝점이 있을 때 Secret `ds-{id}-ca` 의 CA PEM 이다(없거나 읽지 못하면 None, P61 2절)."""

    password: str | None
    ca: str | None = None


async def data_service_secrets(
    ds: DataService, tenancy: TenancyService, clusters: ClusterService
) -> DataServiceSecrets:
    """테넌트 네임스페이스에서 그 Database 의 비밀번호와 CA 를 읽는다. 클러스터 client 와
    네임스페이스는 한 번만 찾는다. CA 는 TLS 끝점이 있을 때만 읽는다."""
    secrets, namespace = await _secret_store_and_namespace(ds, tenancy, clusters)
    data = await run_in_threadpool(secrets.get, ds.secret_name, namespace=namespace)
    ca = None
    if ds.tls is not None:
        ca_data = await run_in_threadpool(secrets.get, ds.tls.ca_secret, namespace=namespace)
        ca = (ca_data or {}).get(REDIS_CA_KEY)
    return DataServiceSecrets((data or {}).get("password"), ca)


async def data_service_password(
    ds: DataService, tenancy: TenancyService, clusters: ClusterService
) -> str | None:
    """테넌트 네임스페이스의 Secret `ds-{id}` 에서 password 만 읽는다. 없으면 None
    (무인증 Redis). Postgres 의 접속과 Knowledge 인덱서가 쓴다."""
    secrets, namespace = await _secret_store_and_namespace(ds, tenancy, clusters)
    data = await run_in_threadpool(secrets.get, ds.secret_name, namespace=namespace)
    return (data or {}).get("password")


async def data_service_ca(
    ds: DataService, tenancy: TenancyService, clusters: ClusterService
) -> str | None:
    """테넌트 네임스페이스의 CA Secret `ds-{id}-ca` 에서 CA PEM 만 읽는다. TLS 끝점이 없거나 그
    Secret 이 없으면 None 이다. 비밀번호는 읽지 않는다."""
    if ds.tls is None:
        return None
    secrets, namespace = await _secret_store_and_namespace(ds, tenancy, clusters)
    data = await run_in_threadpool(secrets.get, ds.tls.ca_secret, namespace=namespace)
    return (data or {}).get(REDIS_CA_KEY)


async def _secret_store_and_namespace(
    ds: DataService, tenancy: TenancyService, clusters: ClusterService
) -> tuple[K8sSecretStore, str]:
    cluster, secrets = await secret_store_for(tenancy, clusters, ds.cluster_id)
    return secrets, (await tenancy.namespace_for(ds.tenant, cluster)).name


async def resolve_password(
    ds: DataService,
    tenancy: TenancyService,
    clusters: ClusterService,
    *,
    err: type[ServiceError],
) -> str | None:
    """`data_service_password` 의 실패를 호출자의 오류 종류(err)로 올린다. 테넌시 오류는 상태와
    코드를 그대로 싣고, 클러스터에 닿지 못하면 502 cluster_unreachable 이다. 호출자가 자기 오류만
    좁게 잡을 수 있도록 종류를 받는다(`pg.connect_or_raise` 와 같은 모양)."""
    return await _as_service_error(data_service_password(ds, tenancy, clusters), err)


async def resolve_secrets(
    ds: DataService,
    tenancy: TenancyService,
    clusters: ClusterService,
    *,
    err: type[ServiceError],
) -> DataServiceSecrets:
    """`data_service_secrets` 를 `resolve_password` 와 같은 오류 규칙으로 읽는다. TLS 끝점이 있는데
    CA 를 읽지 못했으면 평문으로 물러서지 않고 409 secret_missing 이다 (P61 3절)."""
    found = await _as_service_error(data_service_secrets(ds, tenancy, clusters), err)
    if ds.tls is not None and not found.ca:
        raise err(409, "secret_missing", f"CA secret {ds.tls.ca_secret} missing")
    return found


async def _as_service_error[T](read: Awaitable[T], err: type[ServiceError]) -> T:
    """비밀 읽기의 테넌시·연결 실패를 호출자의 오류 종류로 바꾼다."""
    try:
        return await read
    except TenancyError as e:
        raise err(e.status, e.code, e.detail) from e
    except ClusterUnreachable as e:
        raise err(502, "cluster_unreachable", str(e)) from e


# -- 연결 확인 -------------------------------------------------------------------


def redis_target(ds: DataService) -> str:
    """오류 문장에 싣는 대상. `host:port/db` 이고 값은 여기까지만 싣는다(D14). TLS 끝점이 있으면
    플랫폼은 늘 그것으로 붙으므로 그 포트에 `(tls)` 를 붙인다."""
    if ds.tls is not None:
        return f"{ds.host}:{ds.tls.port}/{ds.db or 0} (tls)"
    return f"{ds.host}:{ds.port}/{ds.db or 0}"


def redis_client_kwargs(
    ds: DataService,
    password: str | None,
    *,
    ca: str | None = None,
    connect_timeout: float = 3.0,
    socket_timeout: float = 3.0,
) -> dict[str, Any]:
    """`redis.asyncio.Redis` 인자. verify 와 장기 기억 삭제(D34)가 같은 접속을 쓴다. TLS 끝점이
    있으면 늘 그것으로 붙고 서버 인증서를 CA(ca, PEM)로, 이름을 호스트로 확인한다 (P61 3절)."""
    kwargs: dict[str, Any] = {
        "host": ds.host,
        "port": ds.port,
        "db": ds.db or 0,
        "socket_connect_timeout": connect_timeout,
        "socket_timeout": socket_timeout,
        "decode_responses": True,
    }
    if ds.tls is not None:
        kwargs.update(
            port=ds.tls.port,
            ssl=True,
            ssl_cert_reqs="required",
            ssl_check_hostname=True,
            ssl_ca_data=ca,
        )
    if ds.username:
        kwargs["username"] = ds.username
    if password:
        kwargs["password"] = password
    return kwargs


def tls_failure(e: BaseException) -> bool:
    """redis-py 가 감싼 연결 실패의 사슬에 TLS 오류(인증서·호스트 이름 확인 실패)가 있는가."""
    return any(isinstance(cause, ssl.SSLError) for cause in exception_chain(e))


async def verify_connection_async(
    ds: DataService, password: str | None, *, ca: str | None = None
) -> VerifyResult:
    """kind 별 연결 확인. postgres 는 psycopg, redis 는 redis-py 를 그대로 await 한다(스레드풀
    없음). ca 는 Redis 의 TLS 끝점을 확인할 CA PEM 이다."""
    if ds.kind == "postgres":
        r = await verify_postgres(ds, password, statement_timeout_seconds=VERIFY_STATEMENT_TIMEOUT)
        return VerifyResult(r.ok, r.error, r.detail, r.extensions)
    return await verify_redis(ds, password, ca=ca)


async def verify_redis(
    ds: DataService,
    password: str | None,
    *,
    ca: str | None = None,
    connect_timeout: float = 3.0,
    socket_timeout: float = 3.0,
    client_factory: Callable[..., Any] | None = None,
) -> VerifyResult:
    """redis 의 연결 확인. 판정은 PING 하나다. 그 뒤 INFO server(버전)와 MODULE LIST(모듈)는 권한이
    없으면 물러나고(관리형 Redis 와 `-@dangerous` ACL 사용자), MODULE LIST 가 거부되면 FT._LIST 와
    JSON.TYPE 를 대신 불러 예외가 없으면 모듈이 있는 것으로 본다. 예외를 던지지 않고 detail 에는
    host:port/db 와 코드만 싣는다(서버 원문은 싣지 않는다, D14). `client_factory` 는 테스트가 가짜
    클라이언트를 끼우는 자리이고 기본은 `redis.asyncio.Redis` 다.

    TLS 끝점이 있으면 그것만 본다(P61 3절). CA 를 읽지 못했으면 붙지 않고 `ca_missing` 이고,
    인증서나 호스트 이름 확인이 실패하면 `tls_failed` 다."""
    target = redis_target(ds)
    if ds.tls is not None and not ca:
        return _redis_failure(target, "ca_missing")
    client = (client_factory or AsyncRedis)(
        **redis_client_kwargs(
            ds, password, ca=ca, connect_timeout=connect_timeout, socket_timeout=socket_timeout
        )
    )
    try:
        # 예외 계층이 겹치므로(AuthenticationError 는 ConnectionError 의 하위, NoPermissionError 는
        # ResponseError 의 하위) 순서가 곧 결과다
        await client.ping()
        version = await _redis_version(client)
        modules, checkpointer = await _redis_modules(client)
    except AuthenticationError:
        return _redis_failure(target, "auth_failed")
    except RedisTimeoutError:
        return _redis_failure(target, "connect_failed")
    except RedisConnectionError as e:
        return _redis_failure(target, "tls_failed" if tls_failure(e) else "connect_failed")
    except ResponseError as e:
        return _redis_failure(target, "select_failed" if "DB index" in str(e) else "ping_failed")
    except RedisError:
        return _redis_failure(target, "io_error")
    finally:
        await aclose_quietly(client)
    return VerifyResult(
        True,
        detail=f"{target}: connected",
        redis={"version": version, "modules": modules, "checkpointer": checkpointer},
    )


def _redis_failure(target: str, code: str) -> VerifyResult:
    return VerifyResult(False, code, f"{target}: {code}")


async def aclose_quietly(client: Any) -> None:
    try:
        await client.aclose()
    except (RedisError, OSError):
        pass


async def _redis_version(client: Any) -> str | None:
    """INFO server 의 redis_version. 권한이 없거나 서버가 거부하면(관리형 Redis 가 INFO 를 막는
    경우) None. PING 이 됐으니 여기서 실패해도 verify 는 통과다."""
    try:
        info = await client.info("server")
    except ResponseError:
        return None
    version = info.get("redis_version") if isinstance(info, dict) else None
    return str(version) if version is not None else None


async def _redis_modules(client: Any) -> tuple[list[str] | None, str]:
    """(MODULE LIST 의 모듈 이름, capabilities.checkpointer 값). MODULE LIST 가 거부되면(NOPERM,
    또는 관리형 Redis 가 명령 자체를 막은 경우) 폴백 명령 둘로 보고 모듈 목록은 None 이다."""
    try:
        raw = await client.execute_command("MODULE LIST")
    except ResponseError:
        return None, await _probe_modules(client)
    names = _module_names(raw)
    ready = CHECKPOINTER_MODULES <= {n.lower() for n in names}
    return names, "ready" if ready else "modules_missing"


async def _probe_modules(client: Any) -> str:
    """FT._LIST 와 JSON.TYPE 로 모듈을 본다. 하나라도 unknown command 면 modules_missing, 그 밖의
    거부(NOPERM)가 있으면 unknown, 둘 다 되면 ready."""
    status = "ready"
    for command in MODULE_PROBES:
        try:
            await client.execute_command(*command)
        except ResponseError as e:
            if "unknown command" in str(e).lower():
                return "modules_missing"
            status = "unknown"
    return status


def _module_names(raw: Any) -> list[str]:
    """MODULE LIST 응답의 name 들. redis-py 가 RESP2 에서는 항목을 dict 로 바꿔 주고 RESP3 은 map
    그대로이지만, 콜백이 없는 경로의 [key, value, ...] 쌍 목록도 받는다."""
    names: list[str] = []
    for item in raw or []:
        if isinstance(item, dict):
            name = item.get("name")
        elif isinstance(item, list | tuple):
            name = dict(zip(item[::2], item[1::2], strict=False)).get("name")
        else:
            name = None
        if name:
            names.append(str(name))
    return names


class ClusterMismatch(Exception):
    def __init__(self, detail: str):
        super().__init__(detail)
        self.detail = detail


def check_same_cluster(
    services: list[DataService], *, tenant: str, cluster_id: str | None = None
) -> str | None:
    """바인딩된 Data Service 들이 같은 테넌트·같은 클러스터인지. 공통 cluster_id 를 돌려준다.
    cluster_id 를 주면 그것과도 같아야 한다 (인스턴스 생성 시)."""
    expected = cluster_id
    for ds in services:
        if ds.tenant != tenant:
            raise ClusterMismatch(f"data service {ds.name} belongs to another tenant")
        if expected is None:
            expected = ds.cluster_id
        elif ds.cluster_id != expected:
            raise ClusterMismatch(
                f"data service {ds.name} is on cluster {ds.cluster_id}, expected {expected}"
            )
    return expected


# -- Database 의 쓰기 규칙 (P75 2절) ------------------------------------------------------------


class DataServiceError(ServiceError):
    """Database 규칙 위반. 앱의 전역 핸들러가 응답으로 낸다."""


@dataclass(frozen=True)
class TlsInput:
    """Redis 의 TLS 끝점 본문 (P61 2절). ca 는 PEM 이고, 고칠 때 비우면 지금 CA 를 그대로 둔다."""

    port: int
    ca: str | None = None


@dataclass(frozen=True)
class DataServiceInput:
    """만들기 본문에서 라우터가 이름을 검사하고 넘기는 값이다."""

    name: str
    display_name: str
    cluster_id: str | None
    kind: str
    host: str
    port: int
    database: str | None
    db: int | None
    username: str | None
    password: str | None
    tls: TlsInput | None = None


def check_kind_fields(kind: str, database: str | None, db: int | None) -> None:
    """postgres 는 database, redis 는 db 번호를 쓴다. 다른 종류의 칸이 오면 422 다."""
    if kind == "postgres" and db is not None:
        raise DataServiceError(422, "invalid_value", "db is for redis; postgres uses database")
    if kind == "redis" and database is not None:
        raise DataServiceError(422, "invalid_value", "database is for postgres; redis uses db")


def tls_ca(kind: str, tls: TlsInput) -> str | None:
    """TLS 끝점 본문을 검사하고 쓸 CA PEM 을 돌려준다. Redis 만 TLS 끝점을 갖는다. CA 는 PEM
    인증서만 든 값이어야 한다(`is_ca_pem`, 개인 키가 섞이면 거절한다). 비운 CA 는 None 이다.
    처음 둘 때 CA 가 꼭 있어야 하는지는 부르는 쪽이 본다(`_ca_required`)."""
    if kind != "redis":
        raise DataServiceError(422, "invalid_value", "tls is for redis")
    if not (tls.ca or "").strip():
        return None
    ca = is_ca_pem(tls.ca or "")
    if ca is None:
        raise DataServiceError(
            422, "invalid_value", "tls.ca must hold PEM certificates only, without a private key"
        )
    return ca


def _ca_required() -> DataServiceError:
    return DataServiceError(422, "invalid_value", "tls.ca is required")


def _plain_closed_without_tls() -> DataServiceError:
    return DataServiceError(
        422, "invalid_value", "this Redis serves only its TLS endpoint, so tls is required"
    )


def _apply_tls(ds: DataService, tls: TlsInput | None) -> str | None:
    """고치기 본문의 `tls` 를 문서에 얹고 Secret 에 쓸 새 CA PEM 을 돌려준다(없으면 None). null 은
    끝점을 지운다."""
    if tls is None:
        ds.tls = None
        return None
    ca = tls_ca(ds.kind, tls)
    if ds.tls is not None:
        # 이 판이 모르는 칸이 있어도 그대로 둔다. CA 를 보내지 않으면 지문도 그대로다
        fields: dict[str, Any] = {"port": tls.port}
        if ca is not None:
            fields["ca_sha256"] = ca_sha256(ca)
        ds.tls = ds.tls.model_copy(update=fields)
    elif ca is None:
        raise _ca_required()
    else:
        ds.tls = RedisTls(
            port=tls.port, ca_secret=ca_secret_name_for(ds.id), ca_sha256=ca_sha256(ca)
        )
    return ca


def _secret_data(username: str | None, password: str | None) -> dict[str, str]:
    data: dict[str, str] = {}
    if password:
        data["password"] = password
    if username:
        data["username"] = username
    return data


# (저장소, 네임스페이스, 쓸 Secret 이름과 값의 목록)
type _SecretWrites = tuple[K8sSecretStore, str, list[tuple[str, dict[str, str]]]]


class DataServiceService:
    """Database 문서와 그 자격증명 Secret 을 함께 다룬다. 비밀번호는 대상 클러스터의 테넌트
    네임스페이스 Secret 에만 두고, 그 Secret 을 쓸 수 없다고 확인된 클러스터에서는 만들기·자격증명
    바꾸기·지우기가 아무것도 쓰기 전에 422 다 (P63 2절)."""

    def __init__(
        self,
        store: MongoDataServiceStore,
        tenancy: TenancyService,
        clusters: ClusterService,
        references: ReferenceService,
    ):
        self.store = store
        self.tenancy = tenancy
        self.clusters = clusters
        self.references = references

    async def create(
        self, tenant: str, body: DataServiceInput, *, user_sub: str
    ) -> tuple[DataService, str]:
        """Secret 을 먼저 쓰고 문서를 만든다. 문서가 이름 충돌로 실패하면 Secret 을 지운다.
        만든 문서와 그 네임스페이스를 돌려준다."""
        check_kind_fields(body.kind, body.database, body.db)
        if body.kind == "postgres" and not body.password:
            raise DataServiceError(422, "invalid_value", "password is required for postgres")
        ca = None
        if body.tls is not None:
            ca = tls_ca(body.kind, body.tls)
            if ca is None:
                raise _ca_required()
        if await self.store.get_by_name(tenant, body.name) is not None:
            raise _name_conflict(body.name)
        cluster = await self.tenancy.resolve_cluster(tenant, body.cluster_id)
        require_tenant_secrets(cluster, DataServiceError)
        namespace = await self.tenancy.ensure_tenant_namespace(tenant, cluster)

        ds = DataService(
            updated_by=user_sub,
            tenant=tenant,
            name=body.name,
            display_name=body.display_name,
            cluster_id=cluster.id,
            kind=body.kind,
            host=body.host.strip(),
            port=body.port,
            database=body.database,
            db=body.db,
            username=body.username,
            secret_name="",
            has_password=bool(body.password),
        )
        ds.secret_name = secret_name_for(ds.id)
        writes = [(ds.secret_name, _secret_data(body.username, body.password))]
        if body.tls is not None and ca is not None:
            tls = RedisTls(
                port=body.tls.port, ca_secret=ca_secret_name_for(ds.id), ca_sha256=ca_sha256(ca)
            )
            ds.tls = tls
            writes.append((tls.ca_secret, {REDIS_CA_KEY: ca}))
        if ds.kind == "redis":
            await self._mark_plain_closed(ds)
        _, secrets = await secret_store_for(self.tenancy, self.clusters, cluster.id)
        await self._write_secret(ds, (secrets, namespace, writes))
        try:
            await self.store.create(ds)
        except DuplicateKeyError as e:
            for name, _ in writes:
                await run_in_threadpool(secrets.delete, name, namespace=namespace)
            raise _name_conflict(body.name) from e
        return ds, namespace

    async def _mark_plain_closed(self, ds: DataService) -> set[str]:
        """Redis Database 의 닫힌 표시를 지금 주소로 정한다 (P61 5절 5단계). 평문 포트를 닫은 함께
        올린 Redis 를 가리키면(`ClusterService.plain_closed_at`) 참이고, TLS 끝점이 없으면 붙을 길이
        없어 422 다. 저장할 칸의 이름을 돌려준다(바뀐 표시만)."""
        closed = await self.clusters.plain_closed_at(ds.cluster_id, ds.host, ds.port)
        if closed and ds.tls is None:
            raise _plain_closed_without_tls()
        if ds.plain_closed == closed:
            return set()
        ds.plain_closed = closed
        return {"plain_closed"}

    async def sync_ca_fingerprints(self) -> int:
        """TLS 끝점을 가진 Redis Database 의 CA 지문(`tls.ca_sha256`)을 CA Secret 의 `ca.crt` 와
        맞춘다 (P61). 지문이 없거나(이 칸 전에 저장한 CA) Secret 과 다르면 고친다. 플랫폼이 뜬 뒤
        뒤에서 한 번 돈다(`Services.start`). 한 클러스터에서 읽기가 한 번 실패하면 그 클러스터의
        나머지는 건너뛴다. 닿지 않는 클러스터는 읽기마다 시한까지 기다리기 때문이다. 고친 수를
        돌려주고 예외를 던지지 않는다."""
        try:
            records = await self.store.redis_with_tls()
        except Exception as e:  # noqa: BLE001 - 지문 맞추기가 플랫폼을 멈추지 않는다
            logger.warning("Redis CA fingerprints are not checked: %s", e)
            return 0
        changed = 0
        failed: set[str] = set()
        for ds in records:
            if ds.cluster_id in failed or ds.tls is None:
                continue
            try:
                fingerprint = ca_sha256(await data_service_ca(ds, self.tenancy, self.clusters))
                if fingerprint is not None and fingerprint != ds.tls.ca_sha256:
                    changed += await self.store.set_ca_sha256(ds, fingerprint)
            except Exception as e:  # noqa: BLE001 - 다음 기동이나 CA 저장이 다시 맞춘다
                failed.add(ds.cluster_id)
                logger.warning(
                    "cluster %s: CA fingerprints unchecked (data service %s/%s): %s",
                    ds.cluster_id,
                    ds.tenant,
                    ds.name,
                    e,
                )
        if changed:
            logger.info("corrected the CA fingerprint of %d data service(s)", changed)
        return changed

    async def update(
        self,
        ds: DataService,
        changes: dict[str, Any],
        *,
        user_sub: str,
        shipped_tls: ShippedTls | None = None,
    ) -> DataService:
        """보낸 칸만 바꾼다. `changes` 는 보낸 칸과 그 값이고 이름·표시 이름은 라우터가 이미
        검사했다. 나머지 칸은 예전 라우터와 같은 차례로 검사한다(host, port, 종류별 칸).

        저장은 보낸 칸만, 서버가 읽은 `seq` 를 조건으로 쓴다 (P68 2절). 주소나 접속 정보가 바뀌면
        verify 를 지운다. 사용자 이름이나 비밀번호, 새 CA 를 보내면 Secret 을 먼저 쓰므로 편집
        클레임을 잡고 쓴다(`save_with_edit_claim`).

        `tls` 는 Redis 의 TLS 끝점이다 (P61 2절). null 이면 끝점을 지우되 CA Secret 은 Database 를
        지울 때까지 둔다. 그 끝점으로 배포한 MCP Server 스냅샷이 다시 배포할 때까지 그 Secret 을
        붙이기 때문이다. CA 를 비우고 보내면 지금 CA 를 그대로 둔다. host·port·tls 를 보내면 닫힌
        표시(`plain_closed`, 5절 5단계)를 그 주소로 다시 정하고(`_mark_plain_closed`), 평문 포트를
        닫은 주소에서 TLS 끝점이 없으면 422 다.

        shipped_tls 는 배포가 함께 올린 Redis 의 끝점을 받는 저장이 같은 저장에 적는 표시다
        (`services/shipped_redis.py`). API 의 칸이 아니다."""
        was_verified = ds.verified_at
        names = {"updated_at", "updated_by"}
        if shipped_tls is not None:
            ds.shipped_tls = shipped_tls
            names.add("shipped_tls")
        if "display_name" in changes:
            ds.display_name = changes["display_name"]
            names.add("display_name")
        if "host" in changes:
            host = changes["host"]
            if not host or not host.strip():
                raise DataServiceError(422, "invalid_value", "host cannot be empty")
            ds.host = host.strip()
        if "port" in changes:
            ds.port = DataServiceError.not_null(changes["port"], "port")
        if "database" in changes:
            ds.database = changes["database"]
        if "db" in changes:
            ds.db = changes["db"]
        check_kind_fields(ds.kind, ds.database, ds.db)
        if "username" in changes:
            ds.username = changes["username"]
            names.add("username")
        if "password" in changes and ds.kind == "postgres" and not changes["password"]:
            raise DataServiceError(422, "invalid_value", "password is required for postgres")
        ca = _apply_tls(ds, changes["tls"]) if "tls" in changes else None
        names |= changes.keys() & _ADDRESS_FIELDS
        if ds.kind == "redis" and changes.keys() & {"host", "port", "tls"}:
            names |= await self._mark_plain_closed(ds)
        if names & _ADDRESS_FIELDS or changes.keys() & _SECRET_FIELDS:
            # 확인은 확인한 주소와 접속 정보에만 해당한다 (P68 3절)
            ds.verified_at = None
            names.add("verified_at")
        ds.updated_at = utcnow()
        ds.updated_by = user_sub
        if not changes.keys() & _SECRET_FIELDS and ca is None:
            if not await self.store.save_edit(ds, names):
                raise _revision_conflict(ds)
        else:
            await save_with_edit_claim(
                self.store,
                ds,
                names | {"has_password"},
                prepare=lambda: self._secret_to_write(ds, changes, ca),
                write=lambda target: self._write_secret(ds, target),
                conflict=lambda: _revision_conflict(ds),
                claim_fields={"verified_at": None},
                restore={"verified_at": was_verified},
            )
        return ds

    async def _secret_to_write(
        self, ds: DataService, changes: dict[str, Any], ca: str | None
    ) -> _SecretWrites:
        """Secret 을 쓰기 전의 일: 저장소와 네임스페이스를 찾고 쓸 값을 만든다. 자격증명 Secret
        `ds-{id}` 와 새 CA 가 있으면 CA Secret 이다. 여기서 실패하면 Secret 은 그대로이므로 편집
        클레임을 푼다."""
        cluster, secrets = await secret_store_for(self.tenancy, self.clusters, ds.cluster_id)
        require_tenant_secrets(cluster, DataServiceError)
        namespace = (await self.tenancy.namespace_for(ds.tenant, cluster)).name
        writes: list[tuple[str, dict[str, str]]] = []
        if "password" in changes:
            password = changes["password"]
            ds.has_password = bool(password)
            writes.append((ds.secret_name, _secret_data(ds.username, password)))
        elif changes.keys() & _SECRET_FIELDS:
            current = await run_in_threadpool(secrets.get, ds.secret_name, namespace=namespace)
            if current is None:
                raise DataServiceError(409, "secret_missing", f"secret {ds.secret_name} missing")
            writes.append((ds.secret_name, _secret_data(ds.username, current.get("password"))))
        if ca is not None and ds.tls is not None:
            writes.append((ds.tls.ca_secret, {REDIS_CA_KEY: ca}))
        return secrets, namespace, writes

    async def _write_secret(self, ds: DataService, target: _SecretWrites) -> None:
        """첫 Secret 이 403 이면 아무것도 쓰지 않은 것이라 편집 클레임을 바로 푼다
        (`NothingWritten`)."""
        secrets, namespace, writes = target
        for i, (name, data) in enumerate(writes):
            try:
                await run_in_threadpool(
                    secrets.put,
                    name,
                    data,
                    namespace=namespace,
                    labels=daegam_labels(tenant=ds.tenant, resource="data-service", id=ds.id),
                )
            except AccessDenied as e:
                if i == 0:
                    raise NothingWritten() from e
                raise

    async def delete(self, ds: DataService) -> None:
        """참조 가드, Secret, 문서 순이다. 가드는 테넌트를 거르지 않는다. 다른 테넌트의 참조도
        삭제를 막아야 해서다."""
        await self.references.refuse_first(
            "data-service", ds.id, DATA_SERVICE_DELETE, DataServiceError
        )
        cluster, secrets = await secret_store_for(self.tenancy, self.clusters, ds.cluster_id)
        require_tenant_secrets(cluster, DataServiceError)
        namespace = (await self.tenancy.namespace_for(ds.tenant, cluster)).name
        await run_in_threadpool(secrets.delete, ds.secret_name, namespace=namespace)
        # TLS 끝점을 지운 뒤에도 CA Secret 은 남아 있을 수 있다 (update 의 tls 설명)
        await run_in_threadpool(secrets.delete, ca_secret_name_for(ds.id), namespace=namespace)
        await self.store.delete(ds.id)

    async def verify(self, ds: DataService) -> VerifyResult:
        """접속을 확인한다. 되면 확인 시각과 능력(pgvector·checkpointer)을 문서에 남긴다."""
        secrets = await data_service_secrets(ds, self.tenancy, self.clusters)
        result = await verify_connection_async(ds, secrets.password, ca=secrets.ca)
        if result.ok:
            ds.verified_at = utcnow()
            capability = pgvector_capability(result.extensions)
            if capability is not None:
                ds.capabilities["pgvector"] = capability
            checkpointer = checkpointer_capability(result.redis)
            if checkpointer is not None:
                ds.capabilities["checkpointer"] = checkpointer
            # 읽은 뒤 주소·접속 정보가 바뀌었거나 클레임이 살아 있으면 이 결과는 지금의 접속
            # 정보를 확인한 것이 아니다. 남기지 않는다 (P68 2절)
            stored = await self.store.store_result(
                ds, {"verified_at": ds.verified_at, "capabilities": ds.capabilities}
            )
            if not stored:
                raise _revision_conflict(ds)
        return result


# 바뀌면 verify 를 지우는 주소 칸과, 바뀌면 Secret 을 먼저 쓰는 칸 (P68 2절). TLS 끝점도 주소다
_ADDRESS_FIELDS = frozenset({"host", "port", "database", "db", "tls"})
_SECRET_FIELDS = frozenset({"username", "password"})


def _revision_conflict(ds: DataService) -> Exception:
    return revision_conflict(DataServiceError, "data service", ds.name)


def _name_conflict(name: str) -> DataServiceError:
    return DataServiceError(409, "name_conflict", f"data service {name!r} already exists")
