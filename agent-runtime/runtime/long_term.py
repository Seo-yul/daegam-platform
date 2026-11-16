"""장기 기억이다(D34). `/memories/` 아래 파일만 세션 밖 저장소에 산다.

`memory.long_term` 이 있으면 파일 백엔드(`CompositeBackend`, compile.py 의 `build_backend`)에
`"/memories/": StoreBackend(...)` 라우트가 더해진다. 그 경로 밑만 영구 저장소로 가고 나머지는
지금처럼 스레드 상태에서 산다. 키가 없으면 그 라우트가 없다.

저장소는 memory 노드가 고른 Database 다. 종류는 체크포인터와 같고 접속 env 도 같은
`memory.checkpointer.dsn_env` 다(D33 과 같은 이유로 한 곳에 모은다).

- Postgres 는 전용 스키마 `daegam_store` 에 둔다. langgraph 의 store 마이그레이션이
  `CREATE TABLE IF NOT EXISTS store` 다음에 `ALTER TABLE store ADD COLUMN` 을 하므로 테넌트가
  등록한 Database 에 같은 이름의 업무 테이블이 있으면 남의 테이블을 고친다. 스키마는 플랫폼이
  미리 만들고 런타임은 없으면 기동을 막는다. `IndexConfig` 는 주지 않으므로 `store_vectors` 는
  생기지 않는다(임베딩 검색은 Knowledge Base 가 한다. D32). 체크포인터·색인·카운터가 쓰는 풀과
  나눠 쓰지 않는다. 그 풀에 search_path 를 걸면 이미 쌓인 체크포인트 테이블의 자리가 바뀐다.
- Redis 는 `store_prefix` 를 인스턴스 접두어 아래에 준다. 안 주면 인덱스 이름이 `store` 하나라
  같은 Redis 를 나눠 쓰는 인스턴스끼리 섞인다.

네임스페이스는 `(tenant, instance_id, sha256(owner_sub))` 다. 주체는 세션 소유자
(`SessionRecord.user_sub`)이고 호출자가 아니다. 결정 재개 턴은 승인자가 열 수 있어서 호출자를
쓰면 승인자 기억에 쓴다. sub 를 그대로 못 쓰는 것은 langgraph 가 네임스페이스 라벨의 점을
금지하기 때문이다(이메일·`auth0|user.name` 꼴 sub 가 흔하다). 셋 중 하나라도 비면 기동을 막는다.

턴 중의 저장소 장애는 도구 오류로 남고 턴은 산다. 기동 시 `setup()` 실패는 기동 실패다. 기동을
마친 뒤 배경 작업이 만료된 기억을 한 번 지운다(`LongTermMemory.sweep_expired`, boot.py, P44 11절).
`create_deep_agent(memory=[...])` 는 쓰지 않는다. 그 경로는 저장소가 죽으면 턴 전체를 죽인다.
대신 `/memories/` 가 무엇인지 알리는 고정 문구를 시스템 프롬프트에 더한다(compile.py).
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
from collections.abc import Mapping
from contextlib import suppress
from dataclasses import dataclass, field
from typing import Any

from deepagents.backends import StoreBackend
from langgraph.store.base import BaseStore, TTLConfig
from psycopg.conninfo import make_conninfo

from runtime.approvals import PendingCalls
from runtime.citations import CitationLedger
from runtime.config import AgentConfig, ConfigError
from runtime.dsn import (
    LONG_TERM_CODES,
    check_postgres,
    check_redis,
    missing_dsn_error,
    redis_prefix,
    resolve_dsn,
    resolve_redis,
)
from runtime.fence import FencedStore
from runtime.judge import JudgeCalls
from runtime.outside import OutsideFlag
from runtime.turn_context import context_field
from runtime.uploads import TurnUploads
from runtime.web_guard import WebBudget

log = logging.getLogger("daegam.agent-runtime.long_term")

MEMORY_ROUTE = "/memories/"
"""영구 저장소로 가는 유일한 경로 접두어다. `root` 는 고정이고 설정으로 바꾸지 않는다(D34)."""

RESERVED_ROOT_LABEL = "langgraph"
"""langgraph 가 네임스페이스의 첫 라벨로 금지하는 이름이다(실측 InvalidNamespaceError)."""

POSTGRES_SCHEMA = "daegam_store"
"""Postgres store 전용 스키마다. 이름 관례는 daegam_sessions·daegam_kb_<id> 와 같다."""

MINUTES_PER_DAY = 1440
"""TTLConfig.default_ttl 의 단위가 분이라 보관 기간(일)에 이것을 곱한다(D34)."""

SWEEP_INTERVAL_MINUTES = 60
"""Postgres TTL sweep 주기다. 만료가 일 단위라 라이브러리 기본값 5분마다 훑을 이유가 없다."""

POOL_MIN_SIZE = 1
POOL_MAX_SIZE = 4


@dataclass(frozen=True)
class TurnContext:
    """턴마다 그래프에 넘기는 문맥이다(`astream(context=...)`).

    장기 기억의 주체, 이 턴의 인용 장부(citations.py), 이 턴이 보는 세션의 첨부(uploads.py), 웹
    가드의 몫(web_guard.py), 실행의 트레이스 id 가 여기 실린다. 장부와 첨부 때문에 장기 기억이
    꺼져도 늘 넘긴다. 서브에이전트도 같은 문맥을 받으므로 네임스페이스와 인용 번호, 웹 Tool 의 턴
    상한이 메인과 같다. 모델이 만든 값은 들어오지 않는다.

    holds_attachments 는 세션이 첨부를 한 번이라도 저장했거나 웹훅 실행의 세션인가다(색인의
    `upload_bytes > 0` 이나 `untrusted`, P41 9절. turn.py 가 턴을 열 때 채운다). 세션이 사는 동안
    내려가지 않고, 다시 생성이 갈래를 내는 그래프 상태 밖이라 갈래에도 그대로다. 기본값은 참이다.
    이 값을 모르는 자리는 첨부를 받은 대화로 본다(D64).

    기능 tool_safety(P43)의 셋도 실린다. outside 는 이 대화가 바깥 내용을 가졌는가와 그 표시를
    색인에 쓰는 수단이고 기본값은 표시된 쪽이다(outside.py). approval_choices 는 이 턴의 사람이
    기억한 선택, pending_calls 는 재개할 대기 요청과 그 메시지에서 답이 없는 호출 id 다
    (approvals.py). 셋 다 턴을 열 때 정하는 값이고 모델이 만든 값이 아니다. 기능 injection_check 의
    judge_calls 는 이 턴의 판정 호출 수다(judge.py).
    """

    owner_sub: str
    citations: CitationLedger = field(default_factory=CitationLedger, compare=False)
    uploads: TurnUploads = field(default_factory=TurnUploads, compare=False)
    trace_id: str | None = None
    """이 실행의 트레이스 id 다(P37). 검색 도구의 임베딩 요청이 이 id 로 상관 헤더를 싣는다."""
    holds_attachments: bool = True
    web: WebBudget = field(default_factory=WebBudget, compare=False)
    outside: OutsideFlag = field(default_factory=OutsideFlag, compare=False)
    approval_choices: Mapping[str, str] = field(default_factory=dict, compare=False)
    pending_calls: PendingCalls | None = field(default=None, compare=False)
    judge_calls: JudgeCalls = field(default_factory=JudgeCalls, compare=False)
    """이 턴의 판정 호출 수다(judge.py, P43 7절). 메인과 서브에이전트가 같은 상한을 센다."""
    deadline: float | None = None
    """턴 시한의 `time.monotonic()` 값이다. 샌드박스가 명령의 시한과 열기의 기다림을 턴의 남은
    시간으로 자른다(sandbox.py, P40 1절)."""


def user_label(owner_sub: str) -> str:
    """네임스페이스의 사용자 라벨이다. sha256 hex 다.

    langgraph 가 라벨의 점을 금지하므로 sub 를 그대로 못 쓴다. 사용자 식별자가 테넌트
    Database 의 라벨에 평문으로 남지 않는 것도 이롭다. 플랫폼은 sub 를 알므로 같은 라벨을
    다시 만들어 지울 수 있다.
    """
    return hashlib.sha256(owner_sub.encode("utf-8")).hexdigest()


def ttl_config(retention_days: int | None) -> TTLConfig | None:
    """보관 기간을 TTLConfig 로 옮긴다. null 이면 만료가 없어 TTL 자체를 두지 않는다.

    default_ttl 은 분이라 1440 을 곱한다. refresh_on_read 는 기본이 참이라 ls 한 번에 만료가
    되살아나므로 거짓으로 못 박는다. omit_expired 는 sweep 전이라도 만료된 파일을 안 보이게
    한다. Postgres 의 sweep 은 `DELETE FROM store WHERE expires_at < NOW()` 라 같은 Database 를
    나눠 쓰는 다른 인스턴스의 만료 행도 지운다(만료된 것만 지우므로 결과는 같다).
    """
    if retention_days is None:
        return None
    return TTLConfig(
        default_ttl=float(retention_days * MINUTES_PER_DAY),
        refresh_on_read=False,
        omit_expired=True,
        sweep_interval_minutes=SWEEP_INTERVAL_MINUTES,
    )


def store_dsn(dsn: str) -> str:
    """store 전용 접속 문자열이다. search_path 를 전용 스키마 하나로 고정한다(D34).

    라이브러리에 스키마 인자가 없어 연결 옵션으로만 정할 수 있다. DSN 에 options 가 이미 있으면
    이 값으로 덮는다.
    """
    return make_conninfo(dsn, options=f"-csearch_path={POSTGRES_SCHEMA}")


@dataclass
class LongTermMemory:
    """열린 장기 기억 저장소다. 기동 때 한 번 열고 종료 때 닫는다.

    재컴파일(도구 갱신)은 같은 것을 다시 넘긴다. 여기서 store 를 다시 열면 이벤트 루프에 붙은
    배치 루프와 TTL sweeper 가 샌다.
    """

    store: BaseStore
    tenant: str
    instance_id: str
    # psycopg_pool 의 풀과 redis.asyncio 의 클라이언트다. 연 쪽이 넣고 close 가 닫는다
    _pool: Any = None
    _client: Any = None

    def namespace(self, runtime: Any) -> tuple[str, ...]:
        """StoreBackend 가 턴마다 부르는 네임스페이스 팩토리다.

        주체는 `astream(context=TurnContext(owner_sub=...))` 로 실려 온 세션 소유자다.
        """
        owner_sub = context_field(runtime, "owner_sub")
        if not owner_sub:
            raise ValueError(
                "장기 기억 네임스페이스에 세션 소유자가 없다. astream(context=) 를 안 넘겼다"
            )
        return self.owner_namespace(owner_sub)

    def owner_namespace(self, owner_sub: str) -> tuple[str, ...]:
        """그 사람의 네임스페이스다. 턴의 팩토리(`namespace`)와 기억 보기 라우트(memories_api.py)가
        같은 값을 쓴다."""
        return (self.tenant, self.instance_id, user_label(owner_sub))

    def route(self) -> StoreBackend:
        """`/memories/` 라우트의 백엔드다. 이 경로 밑만 영구 저장소로 간다. 턴의 펜스는 백엔드가
        저장소를 부르는 자리에서 본다(`FencedStore`)."""
        return StoreBackend(namespace=self.namespace, store=FencedStore(self.store))

    async def sweep_expired(self) -> None:
        """만료된 기억을 한 번 지운다(P44 11절). 기동을 마친 뒤 배경 작업으로 돌아 준비 검사를
        기다리게 하지 않는다(boot.py). Postgres store 의 sweeper 는 첫 주기
        (`SWEEP_INTERVAL_MINUTES`)를 다 기다린 뒤에야 지우므로, 잠깐 깨었다 다시 잠드는 Agent 는
        그것으로 지우지 못한다. `sweep_ttl` 은 만료된 행을 지우는 DELETE 하나다(같은 Database 를
        나눠 쓰는 다른 인스턴스의 만료 행도 지운다, `ttl_config`). Redis 는 저장소가 스스로 지우므로
        아무것도 하지 않고 0 이다. `BaseStore` 에는 이 호출이 없어, 없는 저장소(테스트의
        InMemoryStore)는 건너뛴다. 지우지 못하면 로그만 남기고 다음 기동이나 sweeper 가 지운다."""
        sweep = getattr(self.store, "sweep_ttl", None)
        if sweep is None:
            return
        try:
            deleted = await sweep()
        except Exception as e:  # noqa: BLE001  정리 실패가 기동을 막지 않는다
            log.warning("만료된 장기 기억을 지우지 못했다: %s", e)
            return
        if deleted:
            log.info("만료된 장기 기억 %d 개를 지웠다", deleted)

    async def close(self) -> None:
        """TTL sweeper 와 배치 루프를 멈추고 자기가 연 풀·클라이언트를 닫는다."""
        with suppress(Exception):
            await self.store.stop_ttl_sweeper()
        await _cancel_batch_task(self.store)
        if self._pool is not None:
            await self._pool.close()
        if self._client is not None:
            await self._client.aclose()


async def open_long_term(config: AgentConfig, *, tenant: str | None) -> LongTermMemory | None:
    """`memory.long_term` 이 있으면 저장소를 열고 없으면 None 이다.

    셋 중 하나라도 못 갖추면 기동 실패다. 저장소가 없거나(memory 노드 없음), 읽고 쓸 수단이
    없거나(파일 도구 꺼짐), 네임스페이스 라벨이 비면 켜 봐야 첫 도구 호출에서 죽는다.
    """
    long_term = config.long_term
    if long_term is None:
        return None
    memory = config.memory
    if memory is None:
        raise ConfigError(
            "long_term_memory_missing", "장기 기억이 켜져 있는데 memory 노드가 없어 저장소가 없다"
        )
    if not config.filesystem:
        raise ConfigError(
            "long_term_filesystem_disabled",
            "장기 기억이 켜져 있는데 builtin_tools.filesystem 이 거짓이라 읽고 쓸 Tool 이 없다",
        )
    if not tenant:
        raise ConfigError(
            "long_term_tenant_missing",
            "장기 기억의 네임스페이스에 쓸 DAEGAM_TENANT 가 비어 있다",
        )
    if tenant == RESERVED_ROOT_LABEL:
        # langgraph 가 첫 라벨로 이것을 금지한다. 첫 도구 호출에서 죽느니 여기서 막는다
        raise ConfigError(
            "long_term_tenant_reserved",
            f"테넌트 이름 {RESERVED_ROOT_LABEL!r} 는 네임스페이스의 첫 라벨로 쓸 수 없다",
        )
    instance_id = str(config.instance["id"])
    if not instance_id:
        raise ConfigError(
            "long_term_instance_missing", "장기 기억의 네임스페이스에 쓸 instance.id 가 비어 있다"
        )
    checkpointer = memory["checkpointer"]
    dsn_env = checkpointer["dsn_env"]
    ttl = ttl_config(long_term["retention_days"])
    if checkpointer["kind"] == "redis":
        return await _open_redis(dsn_env, tenant=tenant, instance_id=instance_id, ttl=ttl)
    return await _open_postgres(dsn_env, tenant=tenant, instance_id=instance_id, ttl=ttl)


async def _open_postgres(
    dsn_env: str, *, tenant: str, instance_id: str, ttl: TTLConfig | None
) -> LongTermMemory:
    from langgraph.store.postgres.aio import AsyncPostgresStore
    from psycopg_pool import AsyncConnectionPool

    from runtime.sessions import postgres_pool_kwargs, setup_postgres

    dsn = resolve_dsn(dsn_env)
    if not dsn:
        raise _dsn_missing(dsn_env)
    # 마이그레이션 2 가 CREATE INDEX CONCURRENTLY 라 트랜잭션 안에서 못 돈다. autocommit 이어야
    # 하고, 이 풀은 store 만 쓴다(search_path 가 걸려 있다). 턴의 펜스는 커서가 아니라 저장소를
    # 부르는 자리(`route`)에서 본다
    pool = AsyncConnectionPool(
        store_dsn(dsn),
        open=False,
        min_size=POOL_MIN_SIZE,
        max_size=POOL_MAX_SIZE,
        kwargs=postgres_pool_kwargs(fenced=False),
    )
    await pool.open()
    store = None
    try:
        await _check_schema(pool, dsn_env)
        # IndexConfig 를 주지 않으므로 store 와 store_migrations 만 생긴다(store_vectors 없음)
        store = AsyncPostgresStore(pool, ttl=ttl)
        await setup_postgres(store.setup, what="장기 기억 store")
        if ttl is not None:
            # 부르지 않으면 Postgres 는 아무것도 안 지운다. Redis 는 네이티브 TTL 이라 없다
            await store.start_ttl_sweeper()
    except BaseException:
        if store is not None:
            await _cancel_batch_task(store)
        await pool.close()
        raise
    return LongTermMemory(store=store, tenant=tenant, instance_id=instance_id, _pool=pool)


async def _open_redis(
    dsn_env: str, *, tenant: str, instance_id: str, ttl: TTLConfig | None
) -> LongTermMemory:
    from langgraph.store.redis.aio import AsyncRedisStore

    from runtime.session_index_redis import make_redis_client, setup_redis

    conn = resolve_redis(dsn_env)
    if conn is None:
        raise _dsn_missing(dsn_env)
    client = make_redis_client(conn)
    store = None
    try:
        await check_redis(client, "장기 기억 저장소(Redis)", LONG_TERM_CODES)
        store = AsyncRedisStore(
            redis_client=client,
            ttl=ttl,
            store_prefix=f"{redis_prefix(instance_id)}store",
        )
        await setup_redis(store.setup, what="장기 기억 store")
    except BaseException:
        if store is not None:
            await _cancel_batch_task(store)
        await client.aclose()
        raise
    return LongTermMemory(store=store, tenant=tenant, instance_id=instance_id, _client=client)


async def _check_schema(pool: Any, dsn_env: str) -> None:
    """전용 스키마가 있는지 본다. 없으면 기동 실패다. 런타임은 스키마를 만들지 않는다(D34).

    만드는 것은 플랫폼 몫이다. 런타임이 만들면 테넌트 Database 에 스키마를 만들 권한을 파드에
    요구하게 되고, 이름을 잘못 준 인스턴스가 남의 DB 에 스키마를 하나 더 만든다. 스키마가 없으면
    마이그레이션도 어차피 실패하지만 그 오류는 이유를 말해 주지 않아 여기서 먼저 가른다.
    """
    row = await check_postgres(
        pool,
        f"장기 기억 Database({dsn_env})",
        LONG_TERM_CODES,
        query="SELECT to_regnamespace(%s) IS NOT NULL AS ok",
        params=(POSTGRES_SCHEMA,),
    )
    if not (row and row["ok"]):
        raise ConfigError(
            "long_term_schema_missing",
            f"{dsn_env} 가 가리키는 DB 에 스키마 {POSTGRES_SCHEMA!r} 가 없다. "
            "플랫폼이 미리 만들어야 하고 런타임은 만들지 않는다",
        )


async def _cancel_batch_task(store: Any) -> None:
    """AsyncBatchedBaseStore 가 __init__ 에서 띄우는 배치 루프를 끝낸다.

    라이브러리는 __del__ 에서 취소하는데 GC 시점에 기대면 종료 뒤에도 루프에 태스크가 남는다.
    """
    task = getattr(store, "_task", None)
    if task is None or task.done():
        return
    task.cancel()
    with suppress(asyncio.CancelledError, Exception):
        await task


def _dsn_missing(dsn_env: str) -> ConfigError:
    return missing_dsn_error("long_term_dsn_missing", "장기 기억이 쓸", dsn_env)
