"""Database·Redis 바인딩 env 를 접속 정보로 풀고, 기동 때 그 저장소에 연결되는지 본다.

env 규칙은 docs/guide/agents.md 5절에 있다. 메모리가 한 저장소를 함께 쓰는 것은 D33 이, 연결
확인은 T22·T23 이 정했다.

`memory.checkpointer.dsn_env` 와 `knowledge[].store.dsn_env` 가 가리키는 이름이 입구다. 같은
접두어의 개별 env(`<PREFIX>_HOST/PORT/...`)가 있으면 그것을 우선하고 없을 때만 `<PREFIX>_URL` 을
쓴다. `_URL` 은 플랫폼이 K8s env 확장으로 비밀번호를 URL 에 끼워 넣은 것이라 비밀번호에 URL 예약
문자가 있으면 깨지기 때문이다.

연결 확인은 세션 저장소·Knowledge Base·장기 기억이 같은 꼴로 한다(`check_postgres`·
`check_redis`). 실패는 둘로 가른다. 아직 연결할 수 없는 것(`*_unreachable`, T22)과 서버가 인증을
마친 뒤 접속을 거부한 것(`*_rejected`, T23)이다. 기동은 둘 다 기다리며 다시 시도한다(`is_waitable`).
거부에는 연결 수 한도처럼 기다리면 풀리는 것이 섞여 있고, 비밀번호도 서버 쪽에서 고치면 풀리기
때문이다. 둘은 화면에 보이는 이유(healthz 의 unready, T21)로 갈린다.
"""

from __future__ import annotations

import asyncio
import os
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

import psycopg
from psycopg.conninfo import make_conninfo
from redis.exceptions import AuthenticationError, NoPermissionError

from runtime.config import ConfigError

DSN_ENV_SUFFIX = "_URL"
# 바인딩 개별 env 접미어 → psycopg conninfo 키. 값이 비어 있는 항목은 넣지 않는다
BINDING_CONNINFO_KEYS = (
    ("HOST", "host"),
    ("PORT", "port"),
    ("DATABASE", "dbname"),
    ("USER", "user"),
    ("PASSWORD", "password"),
)
# redis 바인딩 개별 env 접미어 → redis.asyncio.Redis 인자. 값이 비어 있는 항목은 넣지 않는다
REDIS_BINDING_KEYS = (
    ("HOST", "host"),
    ("PORT", "port"),
    ("DB", "db"),
    ("USER", "username"),
    ("PASSWORD", "password"),
)
# Redis 의 TLS 끝점을 쓰는 바인딩만 갖는 env 접미어다(P61). TLS 가 "1" 이면 TLS 로만 붙고 평문으로
# 물러서지 않는다. CA_FILE 은 플랫폼이 붙인 사설 CA 의 경로이고 서버 인증서와 호스트 이름을 그것으로
# 확인한다
REDIS_TLS_SUFFIX = "TLS"
REDIS_CA_FILE_SUFFIX = "CA_FILE"
REDIS_DEFAULT_PORT = 6379
REDIS_DEFAULT_DB = 0
# pg_hba 가 접속을 막을 때 서버가 보내는 문장의 한 조각이다(영어 locale)
PG_HBA_REJECT_TEXT = "no pg_hba.conf entry"

# 기동 때 Database·Redis 에 연결되는지 확인하며 기다리는 시간. 풀은 접속 실패를 조용히 재시도하므로
# 상한이 있어야 한다. 세션 저장소·Knowledge Base·장기 기억이 Postgres 확인과 Redis PING 에 같은 값을
# 쓴다
OPEN_CHECK_TIMEOUT_SECONDS = 15.0
# 풀이 감춘 Postgres 접속 실패의 까닭을 보려고 직접 한 번 접속해 볼 때 기다리는 시간(초). libpq 의
# connect_timeout 이라 정수다
REJECTION_PROBE_TIMEOUT_SECONDS = 5
# 기동 때 아직 연결할 수 없는 저장소의 오류 코드다(T22). 설정이 틀린 것이 아니라 아직 뜨지 않은 것일
# 수 있다(클러스터가 통째로 다시 뜰 때)
MEMORY_UNREACHABLE = "memory_unreachable"
KNOWLEDGE_DB_UNREACHABLE = "knowledge_db_unreachable"
LONG_TERM_UNREACHABLE = "long_term_unreachable"
UNREACHABLE_CODES = frozenset({MEMORY_UNREACHABLE, KNOWLEDGE_DB_UNREACHABLE, LONG_TERM_UNREACHABLE})
# 서버가 인증을 마친 뒤 접속을 거부한 오류 코드다(T23). 틀린 비밀번호, 없는 Database, 접속 권한
# 없음, 연결 수 한도가 여기에 든다
MEMORY_REJECTED = "memory_rejected"
KNOWLEDGE_DB_REJECTED = "knowledge_db_rejected"
LONG_TERM_REJECTED = "long_term_rejected"
REJECTED_CODES = frozenset({MEMORY_REJECTED, KNOWLEDGE_DB_REJECTED, LONG_TERM_REJECTED})
# 기동이 기다리며 다시 시도하는 오류다. 설정 오류(env 가 비었다, 스키마가 없다)는 들지 않는다
WAITABLE_CODES = UNREACHABLE_CODES | REJECTED_CODES
# 접속 거부의 안내다. 까닭을 하나로 단정하지 않는다(연결 수 한도도 같은 거부로 온다, 실측)
REJECTED_HINT = (
    "사용자 이름과 비밀번호, Database 이름, 이 계정의 접속 권한을 확인해야 한다. "
    "Database 연결 수가 한도에 이르렀을 때도 이렇게 거부된다"
)
REDIS_REJECTED_HINT = "사용자 이름과 비밀번호, 이 계정의 권한을 확인해야 한다"


@dataclass(frozen=True)
class StoreCodes:
    """저장소 한 자리의 연결 실패 코드 한 쌍이다."""

    unreachable: str
    rejected: str


MEMORY_CODES = StoreCodes(MEMORY_UNREACHABLE, MEMORY_REJECTED)
KNOWLEDGE_DB_CODES = StoreCodes(KNOWLEDGE_DB_UNREACHABLE, KNOWLEDGE_DB_REJECTED)
LONG_TERM_CODES = StoreCodes(LONG_TERM_UNREACHABLE, LONG_TERM_REJECTED)


def is_unreachable(e: ConfigError) -> bool:
    """저장소에 아직 연결할 수 없는 오류인가. 화면에는 dependencies_unavailable 로 보인다(T22)."""
    return e.code in UNREACHABLE_CODES


def is_waitable(e: ConfigError) -> bool:
    """기동이 기다리며 다시 시도할 오류인가. 아직 연결할 수 없는 것과 접속 거부다."""
    return e.code in WAITABLE_CODES


async def check_postgres(
    pool: Any,
    subject: str,
    codes: StoreCodes,
    *,
    query: str = "SELECT 1",
    params: tuple[Any, ...] | None = None,
) -> Any:
    """풀에서 연결을 하나 받아 query 를 돌리고 첫 행을 돌려준다. 기동 때 저장소에 연결되는지 보는
    자리이고, 테이블 setup 실패와 갈라 내야 기동이 기다릴 오류인지 알아본다. 실패하면 서버가 접속을
    거부했는지 보고 codes 의 둘 가운데 하나로 낸다. 서버 원문은 싣지 않고 예외 클래스 이름만
    싣는다. 문구가 "가" 를 붙이므로 subject 는 받침 없는 말로 끝난다."""
    try:
        async with pool.connection(timeout=OPEN_CHECK_TIMEOUT_SECONDS) as conn:
            cur = await conn.execute(query, params)
            return await cur.fetchone()
    except Exception as e:
        raise await _postgres_error(e, pool.conninfo, subject, codes) from e


async def _postgres_error(
    e: Exception, conninfo: str, subject: str, codes: StoreCodes
) -> ConfigError:
    """풀이 낸 실패 e 를 접속 거부와 연결할 수 없음으로 가른다."""
    if await _postgres_rejects(conninfo):
        return ConfigError(codes.rejected, f"{subject} 가 접속을 거부했다. {REJECTED_HINT}")
    return ConfigError(codes.unreachable, f"{subject} 에 연결할 수 없다: {type(e).__name__}")


async def _postgres_rejects(conninfo: str) -> bool:
    """서버가 인증을 마친 뒤 접속을 거부했는가(T23). 풀은 접속 실패를 PoolTimeout 하나로 감추므로
    직접 한 번 접속해 본다. psycopg 는 접속 실패에 SQLSTATE 를 주지 않는다. libpq 의 used_password
    는 서버가 비밀번호를 받은 뒤의 실패(틀린 비밀번호, 없는 Database, 접속 권한 없음, 연결 수
    한도)에서만 참이다. 기동 중이거나 연결을 받지 않는 서버는 비밀번호를 묻기 전에 끊으므로
    거짓이다(실측). 비밀번호를 묻기 전에 막는 pg_hba 의 거부는 서버 문장(영어 locale)으로만
    알아본다. 비밀번호를 묻지 않는 인증(trust)의 거부는 가르지 못한다."""
    try:
        async with await psycopg.AsyncConnection.connect(
            conninfo, connect_timeout=REJECTION_PROBE_TIMEOUT_SECONDS
        ):
            return False
    except psycopg.OperationalError as e:
        if e.pgconn is not None and e.pgconn.used_password:
            return True
        return PG_HBA_REJECT_TEXT in str(e)
    except Exception:  # noqa: BLE001 - 까닭을 모르면 연결할 수 없는 쪽으로 본다
        return False


async def check_redis(client: Any, subject: str, codes: StoreCodes) -> None:
    """클라이언트가 Redis 에 연결되는지 PING 으로 본다. 클라이언트는 접속 실패를 스스로 다시
    시도하므로 시간 상한을 둔다. 비밀번호가 틀렸거나(AuthenticationError) 계정에 PING 권한이 없으면
    (NoPermissionError) 접속 거부(T23)이고, 그 밖의 실패는 연결할 수 없는 것이다. 원문은 싣지
    않는다."""
    try:
        async with asyncio.timeout(OPEN_CHECK_TIMEOUT_SECONDS):
            await client.ping()
    except (AuthenticationError, NoPermissionError) as e:
        message = f"{subject} 가 접속을 거부했다. {REDIS_REJECTED_HINT}"
        raise ConfigError(codes.rejected, message) from e
    except Exception as e:
        message = f"{subject} 에 연결할 수 없다: {type(e).__name__}"
        raise ConfigError(codes.unreachable, message) from e


def binding_prefix(dsn_env: str) -> str:
    """`DAEGAM_DB_0_URL` → `DAEGAM_DB_0`. 접미어가 `_URL` 이 아니면 이름 그대로다."""
    if dsn_env.endswith(DSN_ENV_SUFFIX):
        return dsn_env[: -len(DSN_ENV_SUFFIX)]
    return dsn_env


def missing_dsn_error(code: str, subject: str, dsn_env: str) -> ConfigError:
    """dsn_env 도 같은 접두어의 개별 env 도 비어 있을 때의 기동 오류다.

    subject 는 그 dsn_env 를 가리킨 자리이고 문구 앞머리가 된다. 코드는 부르는 쪽마다 다르다.
    """
    return ConfigError(
        code, f"{subject} {dsn_env!r} 도 개별 env {binding_prefix(dsn_env)}_HOST 도 비어 있다"
    )


def resolve_dsn(dsn_env: str, env: Mapping[str, str] | None = None) -> str | None:
    """dsn_env 가 가리키는 Postgres 접속 문자열이다.

    같은 접두어의 `<PREFIX>_HOST` 가 있으면 개별 env(`HOST/PORT/DATABASE/USER/PASSWORD`)로
    psycopg conninfo(`host=... password='...'`)를 조립한다. make_conninfo 가 공백·따옴표를
    인용하고 그 밖의 문자는 키워드 형식이라 URL 예약 문자가 그대로 값이 된다. 개별 env 가 없으면
    `<PREFIX>_URL` 값을 그대로 쓰고, 둘 다 없으면 None 이다.
    """
    source = os.environ if env is None else env
    prefix = binding_prefix(dsn_env)
    if source.get(f"{prefix}_HOST"):
        parts = {
            key: source[f"{prefix}_{suffix}"]
            for suffix, key in BINDING_CONNINFO_KEYS
            if source.get(f"{prefix}_{suffix}")
        }
        return make_conninfo(**parts)
    return source.get(dsn_env) or None


def resolve_redis(dsn_env: str, env: Mapping[str, str] | None = None) -> dict[str, Any] | None:
    """dsn_env 가 가리키는 Redis 클라이언트 인자다.

    같은 접두어의 `<PREFIX>_HOST` 가 있으면 개별 env(`HOST/PORT/DB/USER/PASSWORD`)로
    `{"host", "port", "db", "username"?, "password"?}` 를 만든다(PORT·DB 가 비어 있으면 6379·0).
    개별 env 가 없으면 `{"url": <PREFIX>_URL}` 이고, 둘 다 없으면 None 이다. 비밀번호를 URL 에
    끼우지 않고 인자로 넘기므로 예약 문자가 그대로 값이 된다.

    `<PREFIX>_TLS` 가 "1" 이면(P61) 두 쪽 다 인증서 확인 인자(`redis_tls_args`)를 더하고, 개별 env
    쪽은 `ssl` 도 켠다. URL 쪽은 `rediss://` 가 TLS 를 켠다. 어느 쪽도 평문으로 물러서지 않는다.
    """
    source = os.environ if env is None else env
    prefix = binding_prefix(dsn_env)
    tls = source.get(f"{prefix}_{REDIS_TLS_SUFFIX}") == "1"
    ca_file = source.get(f"{prefix}_{REDIS_CA_FILE_SUFFIX}") or None
    if source.get(f"{prefix}_HOST"):
        conn: dict[str, Any] = {
            key: source[f"{prefix}_{suffix}"]
            for suffix, key in REDIS_BINDING_KEYS
            if source.get(f"{prefix}_{suffix}")
        }
        for key, default in (("port", REDIS_DEFAULT_PORT), ("db", REDIS_DEFAULT_DB)):
            raw = conn.get(key)
            if raw is None:
                conn[key] = default
                continue
            try:
                conn[key] = int(raw)
            except ValueError:
                # 값은 싣지 않는다. 플랫폼은 정수로 렌더하므로 사람이 손댄 env 다
                raise ConfigError(
                    "memory_dsn_invalid", f"{prefix}_{key.upper()} 가 정수가 아니다"
                ) from None
        if tls:
            conn.update(ssl=True, **redis_tls_args(ca_file))
        return conn
    url = source.get(dsn_env)
    if not url:
        return None
    if tls and not url.startswith("rediss://"):
        # 플랫폼은 TLS 바인딩에 rediss:// 만 렌더한다. 사람이 손댄 env 를 평문으로 따르지 않는다
        raise ConfigError(
            "memory_dsn_invalid", f"{prefix}_TLS 가 1 인데 {dsn_env} 가 rediss:// 가 아니다"
        )
    conn: dict[str, Any] = {"url": url}
    if tls:
        conn.update(redis_tls_args(ca_file))
    return conn


def redis_tls_args(ca_file: str | None) -> dict[str, Any]:
    """TLS 로 붙는 redis.asyncio 클라이언트의 인증서 확인 인자다. 서버 인증서를 CA 로, 이름을
    호스트로 확인한다. CA 경로가 없으면 시스템 신뢰 저장소로 확인한다. TLS 를 켜는 것(`ssl` 이나
    `rediss://`)은 부르는 쪽이다. 플랫폼의 `redis_client_kwargs` 와 같은 값이고
    platform/tests/test_redis_tls_copy.py 가 둘을 맞댄다."""
    args: dict[str, Any] = {"ssl_cert_reqs": "required", "ssl_check_hostname": True}
    if ca_file:
        args["ssl_ca_certs"] = ca_file
    return args


def redis_prefix(instance_id: str) -> str:
    """Redis 키·인덱스 접두어다. 같은 Redis 를 나눠 쓰는 인스턴스는 이 접두어로 갈린다."""
    return f"daegam-agent:{instance_id}:"


def turn_lease_key(prefix: str, session_id: str) -> str:
    """세션의 턴 리스 키다(D65). 색인이 잡고 풀며, 턴의 색인 쓰기와 전사 붙이기가 그 토큰을 본다."""
    return f"{prefix}turn_lease:{session_id}"
