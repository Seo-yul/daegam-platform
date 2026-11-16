"""장기 기억 저장소에 플랫폼이 직접 붙는 자리다 (D34, P17 F1·F2b).

두 가지를 한다. 하나는 Postgres 전용 스키마 `daegam_store` 를 미리 만드는 것이고, 다른 하나는
기억을 지우는 것이다. 둘 다 Knowledge Base 가 Database 에 붙어 확장·테이블을 만들고 지우는 것과
같은 방식이다(D32, `services/knowledge/pg.py`).

**스키마를 플랫폼이 만드는 이유.** langgraph 의 store 마이그레이션은 `CREATE TABLE IF NOT EXISTS
store` 다음에 `ALTER TABLE store ADD COLUMN` 을 한다. `store` 는 테넌트가 등록한 Database 에서 흔한
이름이라 남의 업무 테이블을 고칠 수 있어서 전용 스키마에 둔다. 런타임은 스키마를 만들지 않고
없으면 `long_term_schema_missing` 으로 기동을 막는다. 파드에 스키마 생성 권한을 주지 않으려는
것이고, 이름을 잘못 준 인스턴스가 남의 DB 에 스키마를 하나 더 만드는 일도 막는다.

**지우는 것을 플랫폼이 하는 이유.** 인스턴스 삭제 순서가 토큰 회수 다음 파드 삭제라 런타임에
프록시할 자리가 없다. 기억은 사용자 개인의 것이라 인스턴스보다 오래 남으면 안 된다(체크포인터를
안 지우는 D33 과 여기서 갈린다).

네임스페이스는 `(tenant, instance_id, sha256(세션 소유자 sub))` 이고 저장 모양은 저장소마다 다르다.

- Postgres: `daegam_store.store` 의 `prefix` 열이 네임스페이스를 점으로 이은 문자열이다.
  `(tenant, instance_id)` 접두어로 지우면 그 인스턴스의 사용자 전부가 걷힌다.
- Redis: 키가 `daegam-agent:<instance_id>:store:<ULID>` 이고 네임스페이스는 키가 아니라 JSON
  문서의 `prefix` 필드에 있다. 그래서 인스턴스 접두어로 SCAN 한 뒤 필드를 보고 고른다. 필드 값은
  RediSearch 가 점과 하이픈 같은 문장 부호 앞에 역슬래시를 붙인 꼴(`tenant\\.ag\\-1\\.…`)이다.

런타임과 같은 값을 써야 하므로 상수와 해시는 `agent-runtime/runtime/long_term.py`·`dsn.py` 의
복사본이다. 어긋나면 `tests/test_long_term_copy.py` 가 잡는다.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
from collections.abc import Awaitable, Callable
from typing import Any

import psycopg
from psycopg import AsyncConnection, sql
from redis.asyncio import Redis as AsyncRedis
from redis.exceptions import AuthenticationError, RedisError
from redis.exceptions import ConnectionError as RedisConnectionError
from redis.exceptions import TimeoutError as RedisTimeoutError

from daegam.errors import ServiceError
from daegam.k8s.client import ClusterUnreachable
from daegam.services import agent_graph
from daegam.services.claims import checkpoint
from daegam.services.clusters import ClusterService
from daegam.services.data_services import (
    DataServiceSecrets,
    aclose_quietly,
    redis_client_kwargs,
    redis_target,
    resolve_password,
    resolve_secrets,
)
from daegam.services.knowledge import pg
from daegam.services.tenancy import TenancyService
from daegam.store.agents import Agent, AgentRevision
from daegam.store.data_services import DataService, MongoDataServiceStore

logger = logging.getLogger("daegam.agents")

# 전용 스키마 이름. agent-runtime/runtime/long_term.py 의 POSTGRES_SCHEMA 와 같아야 한다
LONG_TERM_SCHEMA = "daegam_store"
# langgraph 가 그 스키마 안에 만드는 테이블. store_migrations 는 마이그레이션 기록이다
STORE_TABLE = "store"
# 네임스페이스 라벨을 잇는 글자. langgraph 의 _namespace_to_text 가 이것으로 잇는다
NAMESPACE_SEPARATOR = "."
# Redis 키 접두어. agent-runtime/runtime/dsn.py 의 redis_prefix() 와 같아야 한다
REDIS_INSTANCE_PREFIX = "daegam-agent:"
# 그 아래 store 가 쓰는 접두어. 런타임이 store_prefix 로 넘기는 값이다
REDIS_STORE_NAME = "store"
REDIS_KEY_SEPARATOR = ":"
# SCAN 한 번에 훑는 키 수. 지우는 것은 사람이 기다리는 동작이 아니라 처리량보다 부담이 중요하다
SCAN_COUNT = 500
# 스키마 생성과 삭제의 문장 타임아웃(초). Knowledge Base 의 DDL 과 같은 값이다
STATEMENT_TIMEOUT = 30.0
# RediSearch 가 붙인 역슬래시 하나와 그 뒤 글자. langgraph 의 TokenUnescaper 와 같은 규칙이다
_REDIS_ESCAPED = re.compile(r"\\(.)")
# LIKE 의 메타문자. 테넌트 이름에 밑줄이 올 수 있어 그대로 두면 남의 네임스페이스까지 걷는다
_LIKE_ESCAPE = "\\"


def user_label(user_sub: str) -> str:
    """네임스페이스의 사용자 라벨이다. sha256 hex 다.

    langgraph 가 라벨의 점을 금지해서 sub 를 그대로 못 쓴다(이메일이나 `auth0|user.name` 꼴 sub 가
    흔하다). 플랫폼은 sub 를 알므로 같은 라벨을 다시 만들어 지울 수 있다.
    """
    return hashlib.sha256(user_sub.encode("utf-8")).hexdigest()


def namespace_prefix(tenant: str, instance_id: str, user_sub: str | None = None) -> str:
    """지울 대상의 네임스페이스 접두어다. `user_sub` 가 없으면 인스턴스 전체다."""
    labels = [tenant, instance_id]
    if user_sub is not None:
        labels.append(user_label(user_sub))
    return NAMESPACE_SEPARATOR.join(labels)


def redis_store_prefix(instance_id: str) -> str:
    """그 인스턴스의 store 키 접두어다. 같은 Redis 를 나눠 쓰는 인스턴스는 이것으로 갈린다."""
    return f"{REDIS_INSTANCE_PREFIX}{instance_id}{REDIS_KEY_SEPARATOR}{REDIS_STORE_NAME}"


def _escape_like(text: str) -> str:
    """LIKE 패턴에 값을 그대로 넣을 때 쓰는 이스케이프. `ESCAPE '\\'` 와 함께 쓴다."""
    for ch in (_LIKE_ESCAPE, "%", "_"):
        text = text.replace(ch, _LIKE_ESCAPE + ch)
    return text


# -- Postgres -------------------------------------------------------------------------


async def ensure_schema(conn: AsyncConnection) -> None:
    """전용 스키마를 만든다. 이미 있으면 아무 일도 없다.

    테이블은 만들지 않는다. `store`·`store_migrations` 는 런타임이 기동 때 langgraph 마이그레이션
    으로 만들고, 그 마이그레이션이 어느 스키마에 놓일지만 우리가 정해 주는 것이다.
    """
    await conn.execute(
        sql.SQL("CREATE SCHEMA IF NOT EXISTS {s}").format(s=sql.Identifier(LONG_TERM_SCHEMA))
    )


async def _store_table_exists(conn: AsyncConnection) -> bool:
    """스키마도 테이블도 아직 없을 수 있다. 한 번도 안 돈 인스턴스를 지우는 경우다."""
    qualified = f"{LONG_TERM_SCHEMA}.{STORE_TABLE}"
    row = await (
        await conn.execute("SELECT to_regclass(%s) IS NOT NULL AS ok", (qualified,))
    ).fetchone()
    return bool(row and row[0])


async def delete_postgres(
    conn: AsyncConnection, *, tenant: str, instance_id: str, user_sub: str | None = None
) -> int:
    """그 네임스페이스와 그 아래 전부를 지운다. 지운 행 수를 돌려준다.

    `prefix` 가 정확히 같은 행과 그 아래로 더 깊은 네임스페이스(`<접두어>.…`)를 함께 지운다.
    `StoreBackend` 는 지금 한 단계만 쓰지만 접두어 검색이 더 깊은 것까지 걷어 오므로 삭제도 같은
    범위여야 남는 것이 없다.
    """
    if not await _store_table_exists(conn):
        return 0
    prefix = namespace_prefix(tenant, instance_id, user_sub)
    cur = await conn.execute(
        sql.SQL("DELETE FROM {s}.{t} WHERE prefix = %s OR prefix LIKE %s ESCAPE {e}").format(
            s=sql.Identifier(LONG_TERM_SCHEMA),
            t=sql.Identifier(STORE_TABLE),
            e=sql.Literal(_LIKE_ESCAPE),
        ),
        (prefix, f"{_escape_like(prefix + NAMESPACE_SEPARATOR)}%"),
    )
    return cur.rowcount


# -- Redis ----------------------------------------------------------------------------


def _matches(raw: Any, prefix: str) -> bool:
    """JSON.GET 이 준 `$.prefix` 가 지울 네임스페이스에 드는가.

    RedisJSON 의 JSONPath 응답은 값 하나여도 배열 문자열이다(`["a\\.b\\.c"]`). 값은 RediSearch 가
    문장 부호 앞에 역슬래시를 붙인 꼴이라 풀어서 견준다. 하이픈이 든 인스턴스 id(uuid)를 풀지 않고
    견주면 아무것도 맞지 않는다. 모양이 다르면 지우지 않는다. 남의 것을 지우느니 남기는 쪽이 낫다.
    """
    if isinstance(raw, bytes):
        raw = raw.decode("utf-8", "replace")
    if not isinstance(raw, str):
        return False
    try:
        found = json.loads(raw)
    except ValueError:
        return False
    if not isinstance(found, list):
        found = [found]
    return any(isinstance(v, str) and _within(_REDIS_ESCAPED.sub(r"\1", v), prefix) for v in found)


def _within(namespace: str, prefix: str) -> bool:
    """네임스페이스가 prefix 와 같거나 그 아래인가. 값은 역슬래시를 푼 꼴이다."""
    return namespace == prefix or namespace.startswith(prefix + NAMESPACE_SEPARATOR)


async def delete_redis(
    client: Any,
    *,
    tenant: str,
    instance_id: str,
    user_sub: str | None = None,
    between: Callable[[], Awaitable[None]] | None = None,
) -> int:
    """그 인스턴스(또는 그 안의 한 사용자)의 store 키를 지운다. 지운 키 수를 돌려준다.

    키에는 네임스페이스가 없고 JSON 문서의 `prefix` 필드에만 있다. 인스턴스 전체를 지울 때는
    접두어 SCAN 만으로 충분하고, 한 사람만 지울 때만 문서를 하나씩 읽어 고른다. RediSearch 인덱스는
    지운 키를 스스로 잊으므로 따로 손댈 것이 없다. `between` 은 SCAN 묶음 사이마다 부른다(삭제의
    클레임 체크포인트, P66 2.4절).
    """
    match = f"{redis_store_prefix(instance_id)}{REDIS_KEY_SEPARATOR}*"
    prefix = namespace_prefix(tenant, instance_id, user_sub)
    deleted = 0
    cursor = 0
    while True:
        cursor, keys = await client.scan(cursor=cursor, match=match, count=SCAN_COUNT)
        if keys:
            targets = keys if user_sub is None else await _owned_by(client, keys, prefix)
            if targets:
                deleted += int(await client.delete(*targets) or 0)
        if not cursor or str(cursor) == "0":
            return deleted
        if between is not None:
            await between()


async def _owned_by(client: Any, keys: list[Any], prefix: str) -> list[Any]:
    """그 네임스페이스에 드는 키만. 한 번에 파이프라인으로 읽는다."""
    pipe = client.pipeline(transaction=False)
    for key in keys:
        pipe.execute_command("JSON.GET", key, "$.prefix")
    values = await pipe.execute()
    return [key for key, raw in zip(keys, values, strict=True) if _matches(raw, prefix)]


class AgentMemories:
    """Agent 서비스가 장기 기억 저장소에 붙는 자리 (P72 2.1절). 저장소 Database 를 고르고,
    비밀번호를 풀고, 스키마를 만들고, 지운다. 서비스가 생성자에서 만들고 오류 클래스를 넘긴다."""

    def __init__(
        self,
        data_services: MongoDataServiceStore,
        tenancy: TenancyService,
        cluster_service: ClusterService,
        *,
        error_cls: type[ServiceError],
    ):
        self.data_services = data_services
        self.tenancy = tenancy
        self.cluster_service = cluster_service
        self.error_cls = error_cls

    async def store_of(
        self, rev: AgentRevision, tenant: str, known: dict[str, DataService] | None = None
    ) -> DataService | None:
        """장기 기억이 켜져 있으면 그 저장소 Database, 아니면 None.

        저장소는 memory 노드가 고른 Database 그대로다(체크포인터와 같은 곳). `known` 은 이미 읽어
        둔 바인딩 Database 이고 없으면 스토어에서 읽는다."""
        found = agent_graph.long_term_memory(rev)
        if found is None:
            return None
        ds_id, _settings = found
        ds = (known or {}).get(ds_id)
        if ds is None:
            ds = await self.data_services.get(ds_id, tenant=tenant)
        if ds is None:
            raise self.error_cls(
                422, "unknown_data_service", f"data service {ds_id} of the memory node not found"
            )
        return ds

    async def _password(self, ds: DataService) -> str | None:
        return await resolve_password(ds, self.tenancy, self.cluster_service, err=self.error_cls)

    async def _secrets(self, ds: DataService) -> DataServiceSecrets:
        """비밀번호와, TLS 끝점이 있으면 그 CA 다. 클러스터와 네임스페이스는 한 번 찾는다."""
        return await resolve_secrets(ds, self.tenancy, self.cluster_service, err=self.error_cls)

    async def _connect(self, ds: DataService, password: str | None) -> psycopg.AsyncConnection:
        return await pg.connect_or_raise(
            ds, password, statement_timeout_seconds=STATEMENT_TIMEOUT, err=self.error_cls
        )

    async def ensure_schema(
        self, doc: Agent, rev: AgentRevision, known: dict[str, DataService]
    ) -> None:
        """장기 기억이 켜져 있고 저장소가 Postgres 면 전용 스키마 `daegam_store` 를 미리 만든다.

        런타임은 스키마를 만들지 않고 없으면 `long_term_schema_missing` 으로 기동을 막으므로
        (D34) 파드가 뜨기 전에 있어야 한다. 자리를 배포로 잡은 것은 배포·재배포·리컨실이 전부
        여기를 지나기 때문이다. `CREATE SCHEMA IF NOT EXISTS` 라 다시 돌아도 안전하고, 실패하면
        배포가 422 로 멈춰 사용자가 이유를 읽는다. Redis 는 스키마 개념이 없어 해당 없다.
        """
        ds = await self.store_of(rev, doc.tenant, known)
        if ds is None or ds.kind != "postgres":
            return
        password = await self._password(ds)
        conn = await self._connect(ds, password)
        async with conn:
            try:
                await ensure_schema(conn)
            except (psycopg.Error, OSError) as e:
                raise self.error_cls(
                    422,
                    "long_term_schema_failed",
                    f"{ds.host}:{ds.port}: CREATE SCHEMA {LONG_TERM_SCHEMA} failed "
                    f"({type(e).__name__}); the account needs CREATE on the database",
                ) from e

    async def delete(self, doc: Agent, rev: AgentRevision, *, user_sub: str | None) -> int:
        """rev 의 memory 노드가 고른 저장소에서 그 문서의 기억을 지운다. `user_sub` 가 있으면 그
        사람 것만이다. 네임스페이스는 `(tenant, 문서 id, sha256(user_sub))` 라 플랫폼이 sub 로 같은
        라벨을 다시 만들어 찾는다. 장기 기억이 꺼져 있으면 0 이다."""
        ds = await self.store_of(rev, doc.tenant)
        if ds is None:
            return 0
        secrets = await self._secrets(ds)
        if ds.kind == "redis":
            return await self._delete_redis(ds, secrets, doc, user_sub)
        return await self._delete_postgres(ds, secrets.password, doc, user_sub)

    async def _delete_postgres(
        self, ds: DataService, password: str | None, doc: Agent, user_sub: str | None
    ) -> int:
        conn = await self._connect(ds, password)
        async with conn:
            try:
                return await delete_postgres(
                    conn, tenant=doc.tenant, instance_id=doc.id, user_sub=user_sub
                )
            except (psycopg.Error, OSError) as e:
                raise self.error_cls(
                    422, "query_failed", f"{ds.host}:{ds.port}: DELETE failed ({type(e).__name__})"
                ) from e

    async def _delete_redis(
        self, ds: DataService, secrets: DataServiceSecrets, doc: Agent, user_sub: str | None
    ) -> int:
        target = redis_target(ds)
        client = AsyncRedis(**redis_client_kwargs(ds, secrets.password, ca=secrets.ca))
        try:
            return await delete_redis(
                client,
                tenant=doc.tenant,
                instance_id=doc.id,
                user_sub=user_sub,
                between=checkpoint,
            )
        except AuthenticationError as e:
            raise self.error_cls(422, "auth_failed", f"{target}: auth_failed") from e
        except (RedisConnectionError, RedisTimeoutError) as e:
            raise self.error_cls(422, "db_unreachable", f"{target}: connect_failed") from e
        except (RedisError, OSError) as e:
            raise self.error_cls(422, "query_failed", f"{target}: {type(e).__name__}") from e
        finally:
            await aclose_quietly(client)

    async def forget(self, doc: Agent) -> None:
        """삭제의 마지막 단계다. 실패는 경고로 남기고 삭제는 이어간다(D34).

        기억은 사용자 개인의 것이라 문서보다 오래 남으면 안 되므로 체크포인터를 안 지우는 D33 과
        여기서 갈린다. **배포 해제는 이 단계를 타지 않는다.** 같은 id 로 다시 배포되기 때문이다.

        리비전마다 memory 노드가 다른 Database 를 가리킬 수 있다. 해제 뒤 노드를 고쳤거나 재배포로
        저장소를 옮긴 경우다. 삭제는 마지막 기회라 리비전 하나만 보면 남은 행을 가리키는 문서가
        세상에서 사라진다. 그래서 리비전 전부에서 저장소를 모아 중복 없이 돈다. 한 곳이 죽어도
        나머지는 지운다 (D34, D42).
        """
        stores: dict[str, Any] = {}
        for rev in doc.revisions:
            try:
                ds = await self.store_of(rev, doc.tenant)
            except (self.error_cls, ClusterUnreachable) as e:
                logger.warning("agent %s: long-term memory store not resolved: %s", doc.id, e)
                continue
            if ds is not None:
                stores.setdefault(ds.id, ds)
        for ds in stores.values():
            # 기억 저장소는 클러스터 요청이 아니라 훅이 보지 못하므로 저장소마다 클레임을 확인한다
            # (P66 2.4절)
            await checkpoint()
            try:
                secrets = await self._secrets(ds)
                if ds.kind == "redis":
                    deleted = await self._delete_redis(ds, secrets, doc, None)
                else:
                    deleted = await self._delete_postgres(ds, secrets.password, doc, None)
            except (self.error_cls, ClusterUnreachable, psycopg.Error, RedisError, OSError) as e:
                logger.warning(
                    "agent %s: long-term memory not deleted (data service %s): %s", doc.id, ds.id, e
                )
                continue
            if deleted:
                logger.info(
                    "agent %s: deleted %d long-term memory rows from %s", doc.id, deleted, ds.id
                )


__all__ = [
    "AgentMemories",
    "LONG_TERM_SCHEMA",
    "STORE_TABLE",
    "delete_postgres",
    "delete_redis",
    "ensure_schema",
    "namespace_prefix",
    "redis_store_prefix",
    "user_label",
]
