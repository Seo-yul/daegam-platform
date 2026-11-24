"""세션 상태(체크포인터)와 세션 색인이다(D26, D33).

memory 가 있으면 `memory.checkpointer.kind` 로 가른다. postgres 는 AsyncPostgresSaver(기동 시 setup
한 번)와 색인 테이블 `daegam_sessions` 를 같은 DB 에 둔다. 공유 카운터 표 `daegam_counters` 는
counters.py 가, 화면용 전사 표 `daegam_transcripts` 는 transcript.py 가 정의하고 색인의 setup 이
같이 만든다. redis 는 정렬 집합·해시 색인과 체크포인터를 인스턴스 접두어 아래에 두는데 그 구현은
session_index_redis.py 에 있다. memory 가 없으면 InMemorySaver 와 인메모리 색인·카운터·전사이고
no_shared_state 경고가 붙는다. 세션 TTL 은 색인의 last_turn_at 을 기준으로 런타임이
sweep 한다(체크포인터에 TTL 이 없다). sweep 이 보는 시간은 둘이다(D46). 세션 TTL 이 지나면 세션을
닫고 체크포인트를 지우되 전사와 색인 행은 남기고, 기록 보존 기간(DAEGAM_TRANSCRIPT_RETENTION_DAYS,
기본 30일)이 지나야 전사와 색인 행을 함께 지운다. 만료된 카운터 행은 매번 지운다. 첨부
저장소(uploads.py, 표 `daegam_uploads`)는 체크포인트와 함께 세션을 닫을 때 지우고, 그때 지우지
못한 것은 기록을 지울 때 마저 지운다. 소유자가 세션을 지우면(sessions_api.py) 같은 차례로
체크포인트, 첨부, 전사, 색인 행을 한 번에 지운다. 기동 setup 은 파드끼리 겹칠 수 있어 재시도를
태운다(setup_postgres, P17 F5).

색인 행의 턴 기록 칸(제목, turn_id, last_turn, upload_bytes, cite_seq)을 채우는 규칙은
turn_record.py 에 있다(P36 3절). `outside_at` 은 세션이 바깥 내용을 처음 받은 때이고 바깥 표시
층이 한 번 쓴다(P43 3절, tool_safety.py).

백그라운드 실행의 세션(P41 9절)은 만들 때 `run_id` 와 `untrusted` 를 받고 바꾸지 않는다. 실행 id
하나에는 세션 하나뿐이라 같은 id 로 두 번째 세션을 만들면 아무것도 쓰지 않고 `RunTaken` 이다.
Postgres 는 부분 유일 인덱스, Redis 는 실행 키의 SET NX, 메모리 색인은 같은 이벤트 루프 안의
확인이 그것을 지킨다. 실행 id 는 닫힌 세션에도 남고 색인 행을 지울 때 함께 풀린다. 목록은 실행
세션을 기본으로 빼고(`runs`) TTL sweep 은 리스가 살아 있는 세션을 닫지 않는다. 마감 가까이 재개된
턴이 체크포인트를 잃지 않게 하려는 것이고 채팅 세션에도 같다.

작업 세션(`task`, runs.py)은 실행 세션 가운데 실행 id 를 여럿 쥐는 것이다. 만들 때 `task` 를 받고,
다음 실행은 턴의 리스를 쥔 쓰기 하나(`take_run`)가 `run_id` 를 그 실행의 id 로 바꾸며 앞 실행의
id 도 계속 쥔다. 그래서 `find_run` 은 앞 실행의 id 로도 그 세션을 찾고, 그 id 를 이미 어느 세션이
쥐었으면 아무것도 쓰지 않고 `RunTaken` 이다. Postgres 는 표 `daegam_task_runs` 에 세션이 쥔 실행
id 를 모두 두고(그 행이 있는 세션이 작업 세션이다), Redis 는 해시 칸 `task` 와 앞 실행 id 의 목록
`past_runs`, 실행 id 마다의 실행 키에 둔다.

턴이 리스를 풀지 못하고 끝난 세션(P79 3절)은 열린 표시가 남는다. Postgres 는 `turn_token` 이 그
표시이고(풀기가 지운다), Redis 는 해시 칸 `turn_open` 과 집합 `turns:open` 이다. 리스가 살아 있지
않은데 표시가 남은 세션 한 줄은 그 토큰을 `orphan` 으로 들고, 잡기는 표시가 없을 때만 된다. 시도
기록이 적고 마감이 지우는 실행 표식은 Postgres 의 표 `daegam_turn_runs`(세션마다 한 행)와 Redis 의
해시 칸 `turn_run` 이다. `daegam_sessions` 는 열을 늘리지 않는다. 옛 파드와 같은 Database 를 쓰는
다른 Agent 가 준비해 둔 문장의 결과 형이 바뀌지 않게 하려는 것이고, 같은 까닭으로 새 코드는 색인을
열 이름으로 읽는다(`SELECT_ROW`). 되살리기는 turn_repair.py 가 차례를 정하고 색인이 한 트랜잭션이나
스크립트 하나로 쓴다(`repair_turn`).

drain 표시(P44 7절)는 인스턴스마다 하나이고 플랫폼이 Agent 를 재우기 직전에 `POST /drain`
으로 세운다. 세우기는 살아 있는 리스의 수를 함께 세고, 표시가 선 동안 모든 잡기가 `DrainMarked`
로 물러선다. 둘은 모든 잡기와 원자적이다. Postgres 는 표 `daegam_drains` 에 두고 인스턴스 id
에서 정한 advisory 잠금으로 가른다. 잡기의 트랜잭션은 그 잠금을 공유로, 세우기는 배타로 잡는다.
Redis 는 키 `drain` 이고 잡기 스크립트와 세우기 스크립트가 각자 한 번에 본다. 메모리 색인은
이벤트 루프의 한 걸음이다. 표시는 플랫폼이 재우기마다 정한 id 를 들고, 지우기는 id 가 같은
표시만 지운다. 늦게 온 지우기가 다음 재우기의 표시를 지우지 않게 한다. 표시는
`DRAIN_MARK_SECONDS` 뒤에 저절로 풀리고, 런타임은 기동할 때 지우지 않는다.

접속 정보는 memory.checkpointer.dsn_env 가 가리키는 env 에서 온다. 푸는 규칙은 dsn.py 에 있다.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import secrets
import time
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping
from contextlib import asynccontextmanager
from dataclasses import asdict, dataclass, field, replace
from datetime import UTC, datetime, timedelta
from functools import partial
from typing import Any, Literal, Protocol, get_args

from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.checkpoint.memory import InMemorySaver
from psycopg import errors as pg_errors
from psycopg.rows import dict_row

from runtime.config import AgentConfig, ConfigError
from runtime.counters import (
    COUNTERS_DDL,
    Counters,
    MemoryCounters,
    PostgresCounters,
)
from runtime.dsn import (
    MEMORY_CODES,
    check_postgres,
    missing_dsn_error,
    resolve_dsn,
    resolve_redis,
)
from runtime.fence import HELD_LEASE_SQL, FencedCursor, FencedPostgresSaver, refused
from runtime.transcript import (
    APPEND_SQL,
    READ_SQL,
    TRANSCRIPT_LIMIT,
    TRANSCRIPTS_DDL,
    TRIM_SQL,
    MemoryTranscripts,
    PostgresTranscripts,
    TranscriptStore,
    dumps,
)
from runtime.turn_record import RunMarker, like_pattern, title_matches
from runtime.uploads import UPLOADS_DDL, MemoryUploads, PostgresUploads, UploadStore

log = logging.getLogger("daegam.agent-runtime.sessions")

NO_SHARED_STATE = "no_shared_state"
STATUS_ACTIVE = "active"
STATUS_PENDING = "pending"
STATUS_CLOSED = "closed"
STATUSES = (STATUS_ACTIVE, STATUS_PENDING, STATUS_CLOSED)
# update 가 받는 필드. 나머지(session_id·user_sub·created_at·tenant·instance_id)는 만들 때 정해진다
UPDATABLE_FIELDS = frozenset(
    {
        "last_turn_at",
        "turns",
        "usage_input",
        "usage_output",
        "status",
        "pending_since",
        "title",
        "turn_id",
        "last_turn",
        "upload_bytes",
        "cite_seq",
        "outside_at",
    }
)
RECORD_INT_FIELDS = ("turns", "usage_input", "usage_output")
# 턴 기록(turn_record.py)과 첨부·인용·바깥 표시(P43)·웹훅 실행 표시(`untrusted`, P41)·끊긴 열린
# 표시(`orphan`, P79)가 쓰는 내부값이다.
# 목록과 상세의 세션 한 줄에는 싣지 않는다. turn_id 만은 턴이 진행 중일 때 싣는다. 화면의 중단이
# 그 시도를 이름으로 멈춘다(P78 2절)
INTERNAL_FIELDS = (
    "turn_id",
    "last_turn",
    "upload_bytes",
    "cite_seq",
    "outside_at",
    "untrusted",
    "task",
    "orphan",
)
# 읽을 때 정하는 칸이다. 어느 저장소에도 쓰지 않는다
COMPUTED_FIELDS = ("running", "orphan")

# 목록이 실행 세션(P41 9절)을 어떻게 다루는가다. exclude 가 기본이라 채팅 목록에 실행 세션이 섞이지
# 않고, 플랫폼이 물을 때만 include(함께)나 only(실행 세션만)로 준다
RunsFilter = Literal["exclude", "include", "only"]
RUNS_FILTERS: tuple[str, ...] = get_args(RunsFilter)
RUNS_EXCLUDE, RUNS_INCLUDE, RUNS_ONLY = RUNS_FILTERS
# 실행 id 의 꼴이다. `/chat` 과 결정의 요청 모델이 같이 쓴다. 응답 머리(`X-Daegam-Run-Id`)와 Redis
# 키 이름에 들어가므로 글자를 좁힌다. 플랫폼의 실행 id 는 UUID 다
RUN_ID_PATTERN = r"^[A-Za-z0-9_-]{1,64}$"
# 실행 id 를 지키는 Postgres 부분 유일 인덱스와 작업 세션이 쥔 실행 id 의 기본 키다. create 와
# take_run 은 이 둘의 위반만 RunTaken 으로 읽는다
RUN_INDEX = "daegam_sessions_run"
TASK_RUN_KEY = "daegam_task_runs_run"
RUN_CONSTRAINTS = (RUN_INDEX, TASK_RUN_KEY)

# 닫힌 실행 세션의 기록이 보존 기간과 상관없이 남는 최소 시간이다(P41 9절). 기록을 지우면 run_id 가
# 풀려, 플랫폼의 시작 워커가 다시 한 시작(10분 안)이 둘째 턴을 열 수 있기 때문이다
RUN_RECORD_HOLD = timedelta(hours=1)

# drain 표시가 서 있는 초다(P44 7절). 플랫폼이 재우기를 그만둔 뒤 표시를 지우지 못해도 이만큼 지나면
# 잡기가 다시 된다
DRAIN_MARK_SECONDS = 120.0

TRANSCRIPT_RETENTION_ENV = "DAEGAM_TRANSCRIPT_RETENTION_DAYS"
DEFAULT_TRANSCRIPT_RETENTION_DAYS = 30
"""닫힌 대화의 기록이 남는 날 수다(D46). 0 이면 세션을 닫는 그 sweep 이 기록까지 지운다."""


def transcript_retention_days(raw: str | None) -> int:
    """env 값을 보존 일수로 읽는다. 비었거나 숫자가 아니거나 음수면 기본값이다."""
    if raw:
        try:
            days = int(raw)
        except ValueError:
            log.warning("%s=%r 가 정수가 아니라 기본값을 쓴다", TRANSCRIPT_RETENTION_ENV, raw)
            return DEFAULT_TRANSCRIPT_RETENTION_DAYS
        if days >= 0:
            return days
        log.warning("%s=%r 가 음수라 기본값을 쓴다", TRANSCRIPT_RETENTION_ENV, raw)
    return DEFAULT_TRANSCRIPT_RETENTION_DAYS


# 파드 여럿이 같은 DDL 을 동시에 돌릴 때 지는 예외다. 다시 부르면 통과한다(P17 F5).
# DeadlockDetected 는 파드 셋이 store 마이그레이션을 동시에 돌린 실측에서 나왔다. 진 쪽은
# 트랜잭션이 되감겼고 DDL 이 전부 IF NOT EXISTS 라 다시 부르면 그대로 통과한다
PG_SETUP_CONFLICTS = (
    pg_errors.UniqueViolation,
    pg_errors.DuplicateTable,
    pg_errors.DuplicateObject,
    pg_errors.DuplicateColumn,
    pg_errors.DeadlockDetected,
)
PG_SETUP_RETRIES = 20
"""겹침을 다시 부르는 상한이다. 진 파드는 이긴 파드를 따라가며 문장마다 다시 질 수 있어
Redis 쪽(인덱스 둘, 5회)보다 넉넉해야 한다. 실측은 파드 3·5·8 에 겹침 2·4·7 회다."""
PG_SETUP_RETRY_DELAY_SECONDS = 0.1
"""겹쳤을 때 쉬는 시간이다. 곱해 늘려 파드들이 같은 박자로 다시 부딪히지 않게 한다.
안 쉬면 같은 이벤트 루프의 파드 셋이 박자를 맞춰 겹침이 6 회까지 올라갔다(실측)."""

# 세션 풀과 장기 기억 풀의 libpq 전송 시간 제한이다(P79 2절). 보낸 데이터의 확인이 10초(ms
# 단위) 안에 오지 않거나, 말 없는 상대가 keepalive 에 10 + 5 x 2 초 동안 답하지 않으면 연결이
# 오류로 끝나고 취소한 문장의 답을 기다리던 psycopg 도 풀린다. 살아 있되 느린 서버에는 걸리지
# 않는다
PG_TRANSPORT_KWARGS: dict[str, int] = {
    "tcp_user_timeout": 10_000,
    "keepalives": 1,
    "keepalives_idle": 10,
    "keepalives_interval": 5,
    "keepalives_count": 2,
}


def postgres_pool_kwargs(*, fenced: bool) -> dict[str, Any]:
    """풀의 연결 인자다. AsyncPostgresSaver 가 요구하는 것(autocommit, prepare_threshold 0,
    dict_row)에 전송 시간 제한을 더한다. fenced 면 커서가 턴의 펜스를 본다. 세션 풀만 그렇고 장기
    기억 풀이 아닌 까닭은 fence.py 에 있다."""
    kwargs: dict[str, Any] = {
        "autocommit": True,
        "prepare_threshold": 0,
        "row_factory": dict_row,
        **PG_TRANSPORT_KWARGS,
    }
    if fenced:
        kwargs["cursor_factory"] = FencedCursor
    return kwargs


# 같은 Database 를 여러 Agent 가 바인딩할 수 있으므로 색인 행은 instance_id 로 나뉜다.
# 목록·수·합계·sweep 은 자기 인스턴스 행만 본다. 열이 없던 배포본에는 ALTER 로 더한다
SESSIONS_DDL = """
CREATE TABLE IF NOT EXISTS daegam_sessions (
    session_id    text PRIMARY KEY,
    instance_id   text,
    tenant        text,
    user_sub      text NOT NULL,
    created_at    timestamptz NOT NULL,
    last_turn_at  timestamptz NOT NULL,
    turns         integer NOT NULL DEFAULT 0,
    usage_input   bigint NOT NULL DEFAULT 0,
    usage_output  bigint NOT NULL DEFAULT 0,
    status        text NOT NULL,
    pending_since timestamptz,
    -- 진행 중 턴의 임차 만료 시각. 파드가 죽어도 만료되면 다음 턴이 가져간다
    turn_lease    timestamptz,
    -- 임차를 잡은 턴의 토큰과 중지 요청이 옮겨 적은 토큰(D65). 턴은 제 토큰과 같은 표시만 따른다
    turn_token    text,
    cancel_token  text,
    -- 턴 기록이다(P36 3절). 정수 열에 DEFAULT 를 두는 것은 같은 Database 를 쓰는 다른 Agent 의 옛
    -- 런타임이 이 열 없이 INSERT 하기 때문이다
    title         text,
    turn_id       text,
    last_turn     integer NOT NULL DEFAULT 0,
    upload_bytes  bigint NOT NULL DEFAULT 0,
    cite_seq      integer NOT NULL DEFAULT 0,
    -- 세션이 바깥 내용을 처음 받은 때다(P43 3절). 한 번 쓰고 지우지 않는다
    outside_at    timestamptz,
    -- 백그라운드 실행의 id 와 웹훅 실행 표시다(P41 9절). 만들 때 정하고 바꾸지 않는다. 실행 id
    -- 하나에 세션 하나뿐이다(daegam_sessions_run)
    run_id        text,
    untrusted     boolean NOT NULL DEFAULT false
);
CREATE INDEX IF NOT EXISTS daegam_sessions_user ON daegam_sessions (user_sub, last_turn_at DESC);
CREATE INDEX IF NOT EXISTS daegam_sessions_status ON daegam_sessions (status, last_turn_at);
CREATE INDEX IF NOT EXISTS daegam_sessions_instance
    ON daegam_sessions (instance_id, last_turn_at DESC);
CREATE UNIQUE INDEX IF NOT EXISTS daegam_sessions_run
    ON daegam_sessions (run_id) WHERE run_id IS NOT NULL;
CREATE TABLE IF NOT EXISTS daegam_turn_runs (
    session_id text PRIMARY KEY,
    token      text NOT NULL,
    turn_id    text NOT NULL,
    turn       integer NOT NULL,
    kind       text NOT NULL,
    trace_id   text NOT NULL,
    span_id    text NOT NULL,
    started_at timestamptz NOT NULL
);
CREATE INDEX IF NOT EXISTS daegam_sessions_open
    ON daegam_sessions (instance_id) WHERE turn_token IS NOT NULL;
CREATE TABLE IF NOT EXISTS daegam_drains (
    instance_id text PRIMARY KEY,
    mark        text NOT NULL,
    expires_at  timestamptz NOT NULL
);
"""
"""실행 표식의 표(P79 3절), sweep 이 끊긴 열린 표시를 찾는 부분 인덱스, 인스턴스의 drain 표시
표(P44 7절)다. 인덱스는 행 모양을 바꾸지 않는다. 만드는 동안 표에 쓰기를 막지만 지금 크기에서는
짧고, CONCURRENTLY 는 문장마다 다시 부르는 setup 에 맞지 않는다."""

TASK_RUNS_DDL = f"""
CREATE TABLE IF NOT EXISTS daegam_task_runs (
    run_id     text CONSTRAINT {TASK_RUN_KEY} PRIMARY KEY,
    session_id text NOT NULL
);
CREATE INDEX IF NOT EXISTS daegam_task_runs_session ON daegam_task_runs (session_id);
"""
"""작업 세션이 쥔 실행 id 의 표다. 첫 실행의 id 는 세션을 만드는 문장이, 다음 실행의 id 는
`take_run` 이 적고 색인 행을 지우는 문장이 함께 지운다. 색인 표의 열을 늘리지 않으려고 제 표에
둔다."""


# 샌드박스를 닫는 까닭이다(플랫폼 `services/sandboxes.py` RuntimeCloseReason)
CLOSE_SESSION = "session_closed"
CLOSE_DELETED = "session_deleted"
CLOSE_RUN = "run_ended"


def utcnow() -> datetime:
    return datetime.now(UTC)


def new_turn_token() -> str:
    """리스를 잡는 턴의 무작위 토큰이다(D65). 중지 표시가 이 값에 묶여 다음 턴을 멈추지 않는다."""
    return secrets.token_hex(16)


def drain_lock_key(instance_id: str) -> int:
    """인스턴스의 drain advisory 잠금 키다(P44 7절). 잠금 키는 bigint 하나라 인스턴스 id 의
    sha256 앞 8바이트를 부호 있는 정수로 읽는다. 같은 Database 를 나눠 쓰는 인스턴스끼리 잠금이
    갈린다."""
    digest = hashlib.sha256(f"daegam-drain:{instance_id}".encode()).digest()
    return int.from_bytes(digest[:8], "big", signed=True)


def ddl_statements(sql: str) -> list[str]:
    """세미콜론으로 나뉜 DDL 을 문장 하나씩의 목록으로 나눈다. 빈 조각은 버린다."""
    return [part.strip() for part in sql.split(";") if part.strip()]


def check_updatable(fields: Mapping[str, Any]) -> None:
    """update 가 받는 필드인지 본다. 세 색인 구현이 이 규칙 하나를 같이 쓴다."""
    unknown = set(fields) - UPDATABLE_FIELDS
    if unknown:
        raise ValueError(f"색인에 없는 필드다: {sorted(unknown)}")


def turn_record_fields(row: Mapping[str, Any]) -> dict[str, Any]:
    """색인 행에서 읽은 턴 기록 칸 다섯이다(turn_record.py). 옛 런타임이 만든 행처럼 정수 칸이
    없거나 비었으면 0 이다. Postgres 행과 Redis 해시가 같이 쓴다."""
    return {
        "title": row.get("title"),
        "turn_id": row.get("turn_id"),
        "last_turn": int(row.get("last_turn") or 0),
        "upload_bytes": int(row.get("upload_bytes") or 0),
        "cite_seq": int(row.get("cite_seq") or 0),
    }


def check_runs(runs: str) -> None:
    """목록의 runs 값인지 본다. 모르는 값은 라우트가 이미 막았어야 하므로 ValueError 다. 세 색인의
    `list` 가 처음에 한 번 부른다."""
    if runs not in RUNS_FILTERS:
        raise ValueError(f"모르는 실행 거르기다: {runs!r}")


def runs_match(run_id: str | None, runs: str) -> bool:
    """세션 한 줄이 목록의 runs 를 지나는가(P41 9절). 메모리와 Redis 색인이 같이 쓴다. runs 는
    `list` 가 이미 `check_runs` 로 보았다."""
    return runs == RUNS_INCLUDE or (run_id is not None) == (runs == RUNS_ONLY)


class RunTaken(Exception):
    """같은 실행 id 를 이미 다른 세션이 쥐고 있다(P41 9절). 만들기는 아무것도 쓰지 않았다.
    session_id 는 그 실행 id 를 쥔 세션이다."""

    def __init__(self, session_id: str) -> None:
        super().__init__(f"run already held by session {session_id}")
        self.session_id = session_id


class DrainMarked(Exception):
    """이 인스턴스의 drain 표시가 서 있어 리스를 잡지 않았다(P44 7절). 아무것도 쓰지 않았다."""

    def __init__(self) -> None:
        super().__init__("the instance's drain mark refused the lease claim")


class CheckpointsRemain(Exception):
    """스레드의 체크포인트를 정한 횟수만큼 지워도 남았다. 세션 지우기는 503 으로 거절하고 색인 행을
    남겨 다음 지우기가 이어서 지우게 한다."""


@dataclass(frozen=True)
class SessionRecord:
    session_id: str
    tenant: str | None
    user_sub: str
    created_at: datetime
    last_turn_at: datetime
    turns: int = 0
    usage_input: int = 0
    usage_output: int = 0
    status: str = STATUS_ACTIVE
    pending_since: datetime | None = None
    instance_id: str | None = None
    title: str | None = None
    turn_id: str | None = None
    last_turn: int = 0
    upload_bytes: int = 0
    cite_seq: int = 0
    outside_at: datetime | None = None
    # 백그라운드 실행의 id 와 웹훅 실행 표시다(P41 9절). 만들 때만 정한다. run_id 는 세션 한
    # 줄에 실리고 untrusted 는 내부값이다
    run_id: str | None = None
    untrusted: bool = False
    # 실행 id 를 여럿 쥐는 작업 세션인가(runs.py). 만들 때만 정하고 내부값이다. 작업 세션의 run_id
    # 는 가장 최근 실행의 것이고 `take_run` 만 바꾼다
    task: bool = False
    # 살아 있는 리스가 세션을 잡고 있는가(P78 2절). 색인이 읽을 때 정하고 저장하지 않는다. 리스는
    # 마지막 전사를 저장한 뒤에 풀리므로 이것이 거짓이 된 뒤에 읽은 전사에는 마지막 항목이 있다
    running: bool = False
    # 리스가 살아 있지 않은데 남은 열린 표시의 토큰이다(P79 3절). 그 턴은 풀지 못하고 끝났다.
    # running 처럼 읽을 때 정하고 저장하지도 내보내지도 않는다. 되살리기(turn_repair.py)가 이것을
    # 보고 고친다
    orphan: str | None = None

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        for key in INTERNAL_FIELDS:
            del data[key]
        if self.running:
            data["turn_id"] = self.turn_id
        for key in ("created_at", "last_turn_at", "pending_since"):
            value = data[key]
            data[key] = value.isoformat() if isinstance(value, datetime) else None
        data["usage"] = {"input_tokens": self.usage_input, "output_tokens": self.usage_output}
        return data


class SessionIndex(Protocol):
    # record.run_id 가 있고 다른 세션이 그 실행 id 를 쥐고 있으면 아무것도 쓰지 않고 RunTaken 이다.
    # 같은 실행 id 로 동시에 만들어도 하나만 이긴다(P41 9절)
    async def create(self, record: SessionRecord) -> None: ...

    async def get(self, session_id: str) -> SessionRecord | None: ...

    # 그 실행 id 를 쥔 세션이다. 닫힌 세션도 준다(P41 9절). 작업 세션은 앞 실행의 id 로도 찾는다
    async def find_run(self, run_id: str) -> SessionRecord | None: ...

    # 작업 세션의 다음 실행이다(runs.py). 턴 자신의 쓰기라 그 토큰이 살아 있는 리스를 쥐고 있을 때만
    # 쓰고 아니면 `fence.refused()` 다. 세션의 run_id 를 그 id 로 바꾸고 앞 실행의 id 도 계속 쥔다.
    # 그 id 를 이미 어느 세션이 쥐었으면(이 세션이어도) 아무것도 쓰지 않고 RunTaken 이다
    async def take_run(self, session_id: str, token: str, run_id: str) -> SessionRecord: ...

    async def list(
        self,
        *,
        user_sub: str | None = None,
        status: str | None = None,
        limit: int = 100,
        q: str | None = None,
        runs: RunsFilter = RUNS_EXCLUDE,
    ) -> list[SessionRecord]:
        """최근 턴 순 목록이다. q 를 주면 제목에 q 가 든 것만이다(대소문자를 가리지 않는다). runs
        는 실행 세션을 빼거나(기본) 함께 주거나 그것만 준다(P41 9절). limit 는 거른 뒤의 수다."""
        ...

    async def update(self, session_id: str, **fields: Any) -> SessionRecord | None: ...

    # 턴 자신의 쓰기다(P79 2절). 그 토큰이 살아 있는 리스를 쥐고 있을 때만 쓰고, 아니면 리스를 잃은
    # 것이라 `fence.refused()` 를 던진다(지금 문맥의 펜스가 서고 턴이 멈춘다). 이름 바꾸기, 만료,
    # 중지처럼 턴의 것이 아닌 쓰기는 `update` 다
    # run 을 주면 같은 문장에서 그 실행 표식을 적고(시도 기록), settle 이면 같은 문장에서 이 토큰의
    # 표식을 지운다(마감, P79 3절). 쓰기가 거절되면 표식도 그대로다
    async def update_held(
        self,
        session_id: str,
        token: str,
        *,
        run: RunMarker | None = None,
        settle: bool = False,
        **fields: Any,
    ) -> SessionRecord | None: ...

    # 진행 중 턴의 리스와 중지 표시다(D65). 리스는 세션마다 하나이고 잡은 턴의 토큰을 적는다. 잡기는
    # 같은 문장 안에서 색인의 turn_id 를 지운다. 새 시도가 제 id 를 적기(`note_attempt`) 전에 앞
    # 시도의 이름으로 온 중지가 새 턴을 멈추지 않게 하려는 것이다(P78 2절). 중지는 살아 있는 리스의
    # 토큰을 표시에 옮겨 적고 그 토큰을 돌려주며, 턴은 제 토큰과 같은 표시만 따른다. 풀기는 제
    # 토큰의 리스와 표시만 지운다. turn_id 를 주면 색인의 turn_id 가 그것일 때만 같은 문장 안에서
    # 적는다. cancel_all 은 이 인스턴스의 살아 있는 리스마다 표시를 적고 그 수를 돌려준다
    #
    # 잡기는 열린 표시가 없을 때만 된다(P79 1절). 끊긴 표시는 되살리기만 지운다. 지우기의
    # 잡기(`claim_delete`)만은 리스가 살아 있지 않으면 끊긴 표시 위에서도 잡고, 같은 문장에서 그
    # 표시의 토큰을 읽어 그 토큰의 실행 표식을 지우고 돌려준다. 그 표식이 있으면 지우기가 되살린
    # turn_end 를 낸다. 되살리기와 겨뤄도 둘 가운데 하나만 표식을 얻는다
    #
    # 두 잡기 모두 인스턴스의 drain 표시가 서 있으면 아무것도 쓰지 않고 DrainMarked 다(P44 7절)
    async def claim_turn(self, session_id: str, *, lease_seconds: float, token: str) -> bool: ...
    async def claim_delete(
        self, session_id: str, *, lease_seconds: float, token: str
    ) -> tuple[bool, RunMarker | None]: ...
    async def release_turn(self, session_id: str, token: str) -> None: ...
    async def request_cancel(self, session_id: str, turn_id: str | None = None) -> str | None: ...
    async def cancel_requested(self, session_id: str, token: str) -> bool: ...
    async def cancel_all(self) -> int: ...

    # 인스턴스의 drain 표시다(P44 7절). 세우기는 seconds 뒤에 저절로 풀리는 표시를 id(mark)와 함께
    # 세우고(이미 서 있으면 그 id 와 시간을 바꾼다) 같은 걸음에서 이 인스턴스의 살아 있는 리스 수를
    # 센다. 어느 잡기든 세기 앞에 끝나 그 수에 들거나 표시를 보고 물러선다. 지우기는 id 가 같은
    # 표시만 지우고 다르거나 없으면 아무것도 하지 않는다. 앞 재우기의 늦은 지우기가 다음 재우기의
    # 표시를 지우지 못한다
    async def mark_drain(self, *, mark: str, seconds: float) -> int: ...
    async def clear_drain_mark(self, mark: str) -> None: ...
    async def drain_marked(self) -> bool: ...

    # 리스 갱신이다(P79 1절). leases 는 세션 id 와 그 리스를 쥔 턴의 토큰이다. 살아 있고 토큰이 같은
    # 리스만 lease_seconds 로 늘리고 그 세션 id 를 돌려준다. 만료된 리스도, 풀린 뒤 다른 턴이 잡은
    # 리스도 되살리지 않으므로 돌려준 것에서 빠진 리스는 잃은 것이다. 중지 표시도 리스와 같이 늘린다
    async def renew_turns(self, leases: Mapping[str, str], *, lease_seconds: float) -> set[str]: ...

    # 끊긴 열린 표시가 남은 이 인스턴스의 세션이다(P79 3절). sweep 이 되살릴 후보다. Redis 는 해시가
    # 사라진 `turns:open` 의 줄을 여기서 함께 지운다
    async def orphans(self) -> list[str]: ...

    # 끊긴 열린 표시(orphan 토큰)를 되살린다(P79 3절 2~4단계). 그 토큰이 아직 표시이고 리스가 살아
    # 있지 않을 때만 쓴다. status 는 체크포인트에서 정한 상태이고 None 이면 그대로 둔다. 닫힌 세션은
    # 어느 쪽이든 그대로다. pending 이면 처음 시각을 두고 없으면 pending_since 다. plan 은 잠근
    # 한 줄, 그 토큰의 실행 표식(없으면 None), 저장된 전사를 받아 붙일 항목을 돌려준다. plan 이
    # None 이면 최소 되살리기라 전사를 읽지도 붙이지도 않는다. 쓰면 고친 한 줄과 그 표식을, 다른
    # 쪽이 먼저 고쳤거나 잡았으면 None 을 돌려준다
    async def repair_turn(
        self,
        session_id: str,
        orphan: str,
        *,
        status: str | None,
        pending_since: datetime,
        plan: RepairPlan | None,
    ) -> tuple[SessionRecord, RunMarker | None] | None: ...

    # P79 앞에 막힌 세션의 상태를 체크포인트에 맞춘다(P79 3절). 리스도 열린 표시도 없고 last_turn_at
    # 이 읽은 때(seen)와 같고 닫히지 않았을 때만 쓴다. 쓰면 그 한 줄이고 아니면 None 이다
    async def align_status(
        self, session_id: str, *, status: str, pending_since: datetime | None, seen: datetime
    ) -> SessionRecord | None: ...

    # last_turn_at 이 before 앞인 열린 세션을 닫는다. 리스가 살아 있는 세션은 건너뛴다. 마감 가까이
    # 재개된 턴은 끝난 뒤 last_turn_at 이 새로워져 한 TTL 뒤에 닫힌다(P41 9절)
    async def expire(self, before: datetime) -> list[str]: ...

    async def closed_before(self, before: datetime) -> list[str]: ...

    # 색인 행을 지운다. 실행 세션이면 그 실행 id 도 풀린다(P41 9절)
    async def delete(self, session_id: str) -> None: ...

    async def counts(self) -> dict[str, int]: ...

    async def totals(self) -> dict[str, int]: ...


class MemorySessionIndex:
    """파드 하나 안에서만 보이는 색인이다. memory 노드가 없을 때 쓴다."""

    def __init__(self) -> None:
        self._rows: dict[str, SessionRecord] = {}
        # 진행 중 턴을 가진 세션 id 와 그 턴의 토큰이다. SessionRecord.turns(턴 수) 와 다른 것이다
        self._leases: dict[str, str] = {}
        # 중지 표시다. 세션 id 와 표시가 가리키는 턴 토큰이다
        self._cancels: dict[str, str] = {}
        # 실행 id 와 그것을 쥔 세션 id 다(P41 9절). 확인과 적기 사이에 await 가 없어 같은 이벤트
        # 루프의 두 만들기 가운데 하나만 이긴다. 작업 세션은 여러 id 가 같은 세션을 가리킨다
        self._runs: dict[str, str] = {}
        # drain 표시의 id 와 그것이 풀리는 `time.monotonic()` 값이다(P44 7절). 표시와 잡기 사이에
        # await 가 없어 잡기는 세기 앞이거나 표시 뒤다
        self._drain: tuple[str, float] | None = None

    async def create(self, record: SessionRecord) -> None:
        run_id = record.run_id
        if run_id is not None:
            holder = self._runs.get(run_id)
            if holder is not None:
                raise RunTaken(holder)
            self._runs[run_id] = record.session_id
        self._rows[record.session_id] = record

    async def get(self, session_id: str) -> SessionRecord | None:
        row = self._rows.get(session_id)
        return self._with_running(row) if row is not None else None

    async def find_run(self, run_id: str) -> SessionRecord | None:
        holder = self._runs.get(run_id)
        return await self.get(holder) if holder is not None else None

    async def take_run(self, session_id: str, token: str, run_id: str) -> SessionRecord:
        if self._leases.get(session_id) != token:
            raise refused()
        holder = self._runs.get(run_id)
        if holder is not None:
            raise RunTaken(holder)
        self._runs[run_id] = session_id
        row = replace(self._rows[session_id], run_id=run_id)
        self._rows[session_id] = row
        return self._with_running(row)

    def _with_running(self, row: SessionRecord) -> SessionRecord:
        return replace(row, running=True) if row.session_id in self._leases else row

    async def list(
        self,
        *,
        user_sub: str | None = None,
        status: str | None = None,
        limit: int = 100,
        q: str | None = None,
        runs: RunsFilter = RUNS_EXCLUDE,
    ) -> list[SessionRecord]:
        check_runs(runs)
        rows = [
            r
            for r in self._rows.values()
            if (user_sub is None or r.user_sub == user_sub)
            and (status is None or r.status == status)
            and title_matches(r.title, q)
            and runs_match(r.run_id, runs)
        ]
        rows.sort(key=lambda r: r.last_turn_at, reverse=True)
        return [self._with_running(r) for r in rows[:limit]]

    async def update(self, session_id: str, **fields: Any) -> SessionRecord | None:
        check_updatable(fields)
        row = self._rows.get(session_id)
        if row is None:
            return None
        row = replace(row, **fields)
        self._rows[session_id] = row
        return self._with_running(row)

    async def update_held(
        self,
        session_id: str,
        token: str,
        *,
        run: RunMarker | None = None,
        settle: bool = False,
        **fields: Any,
    ) -> SessionRecord | None:
        """파드 안 색인은 끊긴 표시가 생기지 않아(같은 세션을 쓸 다른 파드가 없다) 실행 표식을 두지
        않는다."""
        check_updatable(fields)
        if self._leases.get(session_id) != token:
            raise refused()
        return await self.update(session_id, **fields)

    async def claim_turn(self, session_id: str, *, lease_seconds: float, token: str) -> bool:
        """진행 중 턴을 하나로 막는다. 파드가 하나뿐인 경로라 만료 시각은 보지 않는다."""
        if self._drained():
            raise DrainMarked()
        if session_id in self._leases:
            return False
        self._leases[session_id] = token
        self._cancels.pop(session_id, None)
        row = self._rows.get(session_id)
        if row is not None:
            self._rows[session_id] = replace(row, turn_id=None)
        return True

    async def claim_delete(
        self, session_id: str, *, lease_seconds: float, token: str
    ) -> tuple[bool, RunMarker | None]:
        return await self.claim_turn(session_id, lease_seconds=lease_seconds, token=token), None

    async def release_turn(self, session_id: str, token: str) -> None:
        if self._leases.get(session_id) != token:
            return
        del self._leases[session_id]
        self._cancels.pop(session_id, None)

    async def orphans(self) -> list[str]:
        return []

    async def repair_turn(
        self,
        session_id: str,
        orphan: str,
        *,
        status: str | None,
        pending_since: datetime,
        plan: RepairPlan | None,
    ) -> tuple[SessionRecord, RunMarker | None] | None:
        return None

    async def align_status(
        self, session_id: str, *, status: str, pending_since: datetime | None, seen: datetime
    ) -> SessionRecord | None:
        row = self._rows.get(session_id)
        if (
            row is None
            or session_id in self._leases
            or row.status == STATUS_CLOSED
            or row.last_turn_at != seen
        ):
            return None
        self._rows[session_id] = replace(row, status=status, pending_since=pending_since)
        return self._rows[session_id]

    async def request_cancel(self, session_id: str, turn_id: str | None = None) -> str | None:
        token = self._leases.get(session_id)
        if token is None:
            return None
        row = self._rows.get(session_id)
        if turn_id is not None and (row is None or row.turn_id != turn_id):
            return None
        self._cancels[session_id] = token
        return token

    async def cancel_requested(self, session_id: str, token: str) -> bool:
        return self._cancels.get(session_id) == token

    async def cancel_all(self) -> int:
        self._cancels.update(self._leases)
        return len(self._leases)

    def _drained(self) -> bool:
        return self._drain is not None and time.monotonic() < self._drain[1]

    async def mark_drain(self, *, mark: str, seconds: float) -> int:
        self._drain = (mark, time.monotonic() + seconds)
        return len(self._leases)

    async def clear_drain_mark(self, mark: str) -> None:
        if self._drain is not None and self._drain[0] == mark:
            self._drain = None

    async def drain_marked(self) -> bool:
        return self._drained()

    async def renew_turns(self, leases: Mapping[str, str], *, lease_seconds: float) -> set[str]:
        """파드가 하나뿐인 경로라 만료가 없다. 제 토큰이 아직 리스를 쥐고 있으면 갱신된 것이다."""
        return {sid for sid, token in leases.items() if self._leases.get(sid) == token}

    async def expire(self, before: datetime) -> list[str]:
        """리스를 쥔 세션은 리스 지도로 건너뛴다(P41 9절)."""
        expired = [
            sid
            for sid, r in self._rows.items()
            if r.status != STATUS_CLOSED and r.last_turn_at < before and sid not in self._leases
        ]
        for sid in expired:
            self._rows[sid] = replace(self._rows[sid], status=STATUS_CLOSED)
        return expired

    async def closed_before(self, before: datetime) -> list[str]:
        return [
            sid
            for sid, r in self._rows.items()
            if r.status == STATUS_CLOSED and r.last_turn_at < before
        ]

    async def delete(self, session_id: str) -> None:
        self._rows.pop(session_id, None)
        self._leases.pop(session_id, None)
        self._cancels.pop(session_id, None)
        self._runs = {run_id: sid for run_id, sid in self._runs.items() if sid != session_id}

    async def counts(self) -> dict[str, int]:
        out = dict.fromkeys(STATUSES, 0)
        for r in self._rows.values():
            out[r.status] = out.get(r.status, 0) + 1
        return out

    async def totals(self) -> dict[str, int]:
        return {
            "sessions": len(self._rows),
            "turns": sum(r.turns for r in self._rows.values()),
            "input_tokens": sum(r.usage_input for r in self._rows.values()),
            "output_tokens": sum(r.usage_output for r in self._rows.values()),
        }


class PostgresSessionIndex:
    """memory 의 Postgres 에 두는 색인이다. 파드가 여럿이어도 같은 것을 본다. instance_id 를 주면
    그 인스턴스의 행만 보고 만든다(같은 Database 를 나눠 쓰는 다른 인스턴스와 섞이지 않는다)."""

    def __init__(self, pool: Any, instance_id: str | None = None) -> None:
        # psycopg_pool.AsyncConnectionPool 이다. 제네릭 인자가 연결 형에 얽혀 Any 로 둔다
        self._pool = pool
        self._instance_id = instance_id
        # drain 표시의 행 이름과 잠금 키다(P44 7절). 인스턴스를 주지 않은 색인은 빈 이름 하나를 쓴다
        self._drain_id = instance_id or ""
        self._drain_lock = drain_lock_key(self._drain_id)

    @property
    def conninfo(self) -> str:
        """풀의 접속 정보다. 리스 관리자가 풀 밖에 제 연결을 열 때 쓴다(lease_keeper.py)."""
        return str(self._pool.conninfo)

    def _where(self, *conds: tuple[str, Any]) -> tuple[str, list[Any]]:
        """인스턴스 범위에 (조각, 파라미터) 조건을 더한 WHERE 절과 파라미터다.

        instance_id 가 없으면 전체를 보고, 그때 조건도 없으면 빈 절이다.
        """
        if self._instance_id is not None:
            conds = (("instance_id = %s", self._instance_id), *conds)
        if not conds:
            return "", []
        return "WHERE " + " AND ".join(clause for clause, _ in conds), [p for _, p in conds]

    def _scoped(self, condition: str) -> tuple[str, list[Any]]:
        """인스턴스 범위에 자리표 없는 조건 하나(살아 있는 리스 같은)를 더한 WHERE 절과
        파라미터다."""
        where, params = self._where()
        return (f"{where} AND {condition}" if where else f"WHERE {condition}"), params

    async def setup(self) -> None:
        # 풀이 prepare_threshold 0 이라 모든 문장이 준비문(prepared statement)으로 가는데 준비문은
        # 문장 하나만 받는다(psycopg "cannot insert multiple commands into a prepared statement",
        # 실측). DDL 을 문장 단위로 나눠 준비 없이 실행한다
        statements = (
            ddl_statements(SESSIONS_DDL)
            + ddl_statements(COUNTERS_DDL)
            + ddl_statements(TRANSCRIPTS_DDL)
            + ddl_statements(UPLOADS_DDL)
            + ddl_statements(TASK_RUNS_DDL)
        )
        async with self._pool.connection() as conn:
            for statement in statements:
                await conn.execute(statement, prepare=False)

    async def create(self, record: SessionRecord) -> None:
        """INSERT 한 문장이다. 실행 id 를 이미 다른 세션이 쥐었으면 유일 제약이 막고, 그 세션을
        읽어 RunTaken 을 던진다(P41 9절). 쥔 세션이 이 인스턴스의 것이 아니면(같은 Database 를 나눠
        쓰는 다른 인스턴스) 위반을 그대로 올린다. 실행 id 는 플랫폼의 UUID 라 겹칠 일이 없다. 작업
        세션이면 같은 문장이 그 실행 id 를 `daegam_task_runs` 에도 적는다."""
        sql = """
            INSERT INTO daegam_sessions
              (session_id, instance_id, tenant, user_sub, created_at, last_turn_at, turns,
               usage_input, usage_output, status, pending_since, title, turn_id, last_turn,
               upload_bytes, cite_seq, outside_at, run_id, untrusted)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            """
        params: list[Any] = [
            record.session_id,
            record.instance_id if record.instance_id is not None else self._instance_id,
            record.tenant,
            record.user_sub,
            record.created_at,
            record.last_turn_at,
            record.turns,
            record.usage_input,
            record.usage_output,
            record.status,
            record.pending_since,
            record.title,
            record.turn_id,
            record.last_turn,
            record.upload_bytes,
            record.cite_seq,
            record.outside_at,
            record.run_id,
            record.untrusted,
        ]
        if record.task:
            sql = f"WITH u AS ({sql} RETURNING session_id) {TASK_RUN_SQL}"
            params.append(record.run_id)
        try:
            async with self._pool.connection() as conn:
                await conn.execute(sql, params)
        except pg_errors.UniqueViolation as e:
            raise await self._run_taken(record.run_id, e) from e

    async def _run_taken(self, run_id: str | None, e: pg_errors.UniqueViolation) -> RunTaken:
        """실행 id 의 유일 위반을 그 id 를 쥔 세션의 RunTaken 으로 옮긴다. 다른 제약의 위반이거나
        쥔 세션을 이 인스턴스에서 찾지 못하면 위반을 그대로 올린다."""
        if run_id is None or e.diag.constraint_name not in RUN_CONSTRAINTS:
            raise e
        holder = await self.find_run(run_id)
        if holder is None:
            raise e
        return RunTaken(holder.session_id)

    async def get(self, session_id: str) -> SessionRecord | None:
        return await self._get_one(("session_id = %s", session_id))

    async def find_run(self, run_id: str) -> SessionRecord | None:
        """세션의 run_id 로 먼저 찾고, 없으면 작업 세션이 쥔 앞 실행의 id 에서 찾는다."""
        held = await self._get_one(("run_id = %s", run_id))
        return held or await self._get_one((TASK_RUN_HOLDER, run_id))

    async def take_run(self, session_id: str, token: str, run_id: str) -> SessionRecord:
        """리스의 토큰을 건 UPDATE 하나가 run_id 를 바꾸고, 그 UPDATE 를 CTE 로 둔 같은 문장이 그
        id 를 `daegam_task_runs` 에 적는다. 어느 쪽의 유일 위반이든 문장 전체가 물러난다."""
        try:
            row = await self._update(
                session_id,
                {"run_id": run_id},
                held=(HELD_LEASE_SQL, token),
                tail=(TASK_RUN_SQL, [run_id]),
            )
        except pg_errors.UniqueViolation as e:
            raise await self._run_taken(run_id, e) from e
        if row is None:
            raise refused()
        return row

    async def _get_one(self, cond: tuple[str, Any]) -> SessionRecord | None:
        where, params = self._where(cond)
        async with self._pool.connection() as conn:
            cur = await conn.execute(f"SELECT {SELECT_ROW} FROM daegam_sessions {where}", params)
            row = await cur.fetchone()
        return _row_to_record(row) if row else None

    async def list(
        self,
        *,
        user_sub: str | None = None,
        status: str | None = None,
        limit: int = 100,
        q: str | None = None,
        runs: RunsFilter = RUNS_EXCLUDE,
    ) -> list[SessionRecord]:
        conds: list[tuple[str, Any]] = []
        if user_sub is not None:
            conds.append(("user_sub = %s", user_sub))
        if status is not None:
            conds.append(("status = %s", status))
        if q is not None:
            conds.append(("title ILIKE %s ESCAPE '\\'", like_pattern(q)))
        check_runs(runs)
        if runs != RUNS_INCLUDE:
            # 실행 세션인가를 참거짓 하나로 견준다. exclude 는 거짓, only 는 참이다
            conds.append(("(run_id IS NOT NULL) = %s", runs == RUNS_ONLY))
        where, params = self._where(*conds)
        async with self._pool.connection() as conn:
            cur = await conn.execute(
                f"SELECT {SELECT_ROW} FROM daegam_sessions {where} "
                "ORDER BY last_turn_at DESC LIMIT %s",
                [*params, limit],
            )
            rows = await cur.fetchall()
        return [_row_to_record(row) for row in rows]

    async def update(self, session_id: str, **fields: Any) -> SessionRecord | None:
        check_updatable(fields)
        if not fields:
            return await self.get(session_id)
        return await self._update(session_id, fields)

    async def update_held(
        self,
        session_id: str,
        token: str,
        *,
        run: RunMarker | None = None,
        settle: bool = False,
        **fields: Any,
    ) -> SessionRecord | None:
        """같은 UPDATE 가 그 토큰과 살아 있는 리스를 조건으로 건다(HELD_LEASE_SQL). 행이 없으면
        리스를 잃은 것이다(`fence.refused`). 실행 표식은 그 UPDATE 를 CTE 로 둔 같은 문장에서
        적거나 지운다. 표식 쪽이 UPDATE 의 결과를 읽으므로 거절된 쓰기는 표식을 건드리지 않는다
        (거꾸로 두면 WITH 안의 쓰기는 언제나 돌아 거절된 마감도 표식을 지운다)."""
        check_updatable(fields)
        tail: tuple[str, list[Any]] | None = None
        if run is not None:
            tail = (RUN_UPSERT_SQL, _run_params(run))
        elif settle:
            tail = (RUN_SETTLE_SQL, [token])
        row = await self._update(session_id, fields, held=(HELD_LEASE_SQL, token), tail=tail)
        if row is None:
            raise refused()
        return row

    async def _update(
        self,
        session_id: str,
        fields: Mapping[str, Any],
        *,
        held: tuple[str, Any] | None = None,
        tail: tuple[str, list[Any]] | None = None,
    ) -> SessionRecord | None:
        """색인 한 줄의 UPDATE 다. tail 은 그 UPDATE 를 CTE `u` 로 읽는 쓰기(실행 표식, 작업 세션의
        실행 id)와 그 자리표 값이다."""
        assignments = ", ".join(f"{key} = %s" for key in fields) or NO_CHANGE
        conds: list[tuple[str, Any]] = [("session_id = %s", session_id)]
        if held is not None:
            conds.append(held)
        where, params = self._where(*conds)
        sql = f"UPDATE daegam_sessions SET {assignments} {where} RETURNING {SELECT_ROW}"
        args = [*fields.values(), *params]
        if tail is not None:
            sql = f"WITH u AS ({sql}), marker AS ({tail[0]}) SELECT * FROM u"
            args += tail[1]
        async with self._pool.connection() as conn:
            cur = await conn.execute(sql, args)
            row = await cur.fetchone()
        return _row_to_record(row) if row else None

    async def claim_turn(self, session_id: str, *, lease_seconds: float, token: str) -> bool:
        """진행 중 턴을 파드 사이에서 하나로 막는다.

        파드 로컬 집합으로 막으면 replicas 가 둘 이상일 때 같은 세션의 두 요청이 다른 파드에
        걸려 같은 thread 에 동시에 쓴다. 조건부 UPDATE 한 문장이 임차를 가져간 쪽만 1행을
        돌려준다. 임차는 만료가 있어서 파드가 죽어도 세션이 영영 막히지 않는다. 잡은 턴의 토큰을
        함께 적고 앞 턴의 중지 표시와 시도의 id 는 지운다. `turn_token` 이 열린 표시라 풀지 못하고
        끝난 턴의 토큰이 남은 행은 잡지 않는다(P79 1절). drain 표시를 보는 트랜잭션 안에서
        돈다(`_claiming`)."""
        where, params = self._where(("session_id = %s", session_id))
        async with self._claiming() as conn:
            cur = await conn.execute(
                f"UPDATE daegam_sessions SET {CLAIM_SET} {where} "
                f"AND {LAPSED_LEASE} AND turn_token IS NULL RETURNING session_id",
                [lease_seconds, token, *params],
            )
            return await cur.fetchone() is not None

    async def claim_delete(
        self, session_id: str, *, lease_seconds: float, token: str
    ) -> tuple[bool, RunMarker | None]:
        """잠그는 하위 SELECT 가 리스가 살아 있지 않은 행과 그 앞 토큰을 읽고, 같은 UPDATE 가 잡으며
        앞 토큰을 돌려준다. 되살리기의 FOR UPDATE 와 부딪혀 한쪽만 그 토큰을 얻는다. 앞 토큰이
        있으면 그 토큰의 실행 표식을 지우며 읽는다. 둘 다 drain 표시를 보는 트랜잭션 안이다."""
        where, params = self._where(("session_id = %s", session_id))
        async with self._claiming() as conn:
            cur = await conn.execute(
                f"UPDATE daegam_sessions AS s SET {CLAIM_SET} "
                f"FROM (SELECT session_id, turn_token FROM daegam_sessions {where} "
                f"AND {LAPSED_LEASE} FOR UPDATE) AS o "
                "WHERE s.session_id = o.session_id RETURNING o.turn_token AS orphan",
                [lease_seconds, token, *params],
            )
            row = await cur.fetchone()
            if row is None:
                return False, None
            if row["orphan"] is None:
                return True, None
            cur = await conn.execute(
                f"{RUN_TAKE_SQL} AND token = %s {RUN_RETURNING}", [session_id, row["orphan"]]
            )
            taken = await cur.fetchone()
        return True, _row_to_marker(taken) if taken else None

    @asynccontextmanager
    async def _claiming(self) -> AsyncIterator[Any]:
        """잡기의 트랜잭션이다(P44 7절). 인스턴스의 drain 잠금을 공유로 잡은 뒤 표시를 보고, 서
        있으면 DrainMarked 로 되감는다. 표시는 잠금 다음 문장에서 본다. READ COMMITTED 는 문장마다
        새로 보므로 잠금을 기다리는 동안 선 표시도 보인다. `mark_drain` 은 같은 키를 배타로 잡고
        표시를 쓴 뒤 세므로, 잡기는 세기 앞에 커밋되어 그 수에 들거나 표시를 보고 물러선다."""
        async with self._pool.connection() as conn, conn.transaction():
            await conn.execute("SELECT pg_advisory_xact_lock_shared(%s)", [self._drain_lock])
            if await self._drain_set(conn):
                raise DrainMarked()
            yield conn

    async def _drain_set(self, conn: Any) -> bool:
        cur = await conn.execute(
            "SELECT 1 FROM daegam_drains WHERE instance_id = %s AND expires_at > now()",
            [self._drain_id],
        )
        return await cur.fetchone() is not None

    async def mark_drain(self, *, mark: str, seconds: float) -> int:
        """한 트랜잭션이다. drain 잠금을 배타로 잡고 표시를 쓴 뒤 살아 있는 리스를 센다. 잠금을 쥔
        잡기가 끝나기를 기다리므로 그 잡기는 셈에 든다."""
        live, params = self._scoped(LIVE_LEASE)
        async with self._pool.connection() as conn, conn.transaction():
            await conn.execute("SELECT pg_advisory_xact_lock(%s)", [self._drain_lock])
            await conn.execute(
                "INSERT INTO daegam_drains (instance_id, mark, expires_at) "
                "VALUES (%s, %s, now() + %s * interval '1 second') "
                "ON CONFLICT (instance_id) DO UPDATE "
                "SET mark = EXCLUDED.mark, expires_at = EXCLUDED.expires_at",
                [self._drain_id, mark, seconds],
            )
            cur = await conn.execute(f"SELECT count(*) AS n FROM daegam_sessions {live}", params)
            row = await cur.fetchone()
        return int(row["n"])

    async def clear_drain_mark(self, mark: str) -> None:
        """id 가 같은 표시만 지우는 DELETE 한 문장이다."""
        async with self._pool.connection() as conn:
            await conn.execute(
                "DELETE FROM daegam_drains WHERE instance_id = %s AND mark = %s",
                [self._drain_id, mark],
            )

    async def drain_marked(self) -> bool:
        async with self._pool.connection() as conn:
            return await self._drain_set(conn)

    async def release_turn(self, session_id: str, token: str) -> None:
        """제 토큰의 리스와 표시만 지운다. 만료 뒤 다른 턴이 잡은 리스는 건드리지 않는다."""
        where, params = self._where(("session_id = %s", session_id), ("turn_token = %s", token))
        async with self._pool.connection() as conn:
            await conn.execute(
                "UPDATE daegam_sessions "
                f"SET turn_lease = NULL, turn_token = NULL, cancel_token = NULL {where}",
                params,
            )

    async def request_cancel(self, session_id: str, turn_id: str | None = None) -> str | None:
        """살아 있는 리스의 토큰을 표시에 옮겨 적고 그 토큰을 돌려준다(D65). 한 UPDATE 라 잡기와
        지우기 사이의 경주가 없다. turn_id 를 주면 그 시도일 때만 적는다(P78 2절). 진행 중 턴이
        없으면 None 이다."""
        conds: list[tuple[str, Any]] = [("session_id = %s", session_id)]
        if turn_id is not None:
            conds.append(("turn_id = %s", turn_id))
        where, params = self._where(*conds)
        async with self._pool.connection() as conn:
            cur = await conn.execute(
                f"UPDATE daegam_sessions SET cancel_token = turn_token {where} AND {LIVE_LEASE} "
                "RETURNING cancel_token",
                params,
            )
            row = await cur.fetchone()
        return row["cancel_token"] if row else None

    async def cancel_all(self) -> int:
        """이 인스턴스의 살아 있는 리스마다 표시를 적는다(P78 2절). 한 UPDATE 이고 적은 수다."""
        live, params = self._scoped(LIVE_LEASE)
        async with self._pool.connection() as conn:
            cur = await conn.execute(
                f"UPDATE daegam_sessions SET cancel_token = turn_token {live} RETURNING session_id",
                params,
            )
            return len(await cur.fetchall())

    async def cancel_requested(self, session_id: str, token: str) -> bool:
        where, params = self._where(("session_id = %s", session_id), ("cancel_token = %s", token))
        async with self._pool.connection() as conn:
            cur = await conn.execute(f"SELECT session_id FROM daegam_sessions {where}", params)
            return await cur.fetchone() is not None

    async def renew_turns(
        self, leases: Mapping[str, str], *, lease_seconds: float, conn: Any = None
    ) -> set[str]:
        """한 문장으로 모든 리스를 늘린다. 리스가 몇 개든 문장은 하나라 준비문도 하나다. 토큰이
        같고 아직 살아 있는 행만 세션 id 순으로 잠근 뒤 늘리므로, 두 파드의 갱신이 같은 행들을 다른
        차례로 잠가 교착하지 않는다. 잠금은 FOR NO KEY UPDATE 라 전사 붙이기의 FOR KEY SHARE 와
        부딪히지 않고(transcript.py `HELD_APPEND_SQL`) 되살리기의 FOR UPDATE 와는 부딪힌다. 만료된
        리스는 되살리지 않는다. 중지 표시는 리스와 같은 행의 열이라 따로 늘릴 것이 없다. conn 을
        주면 풀 대신 그 연결로 보낸다(리스 관리자의 연결, lease_keeper.py)."""
        if not leases:
            return set()
        ids = sorted(leases)
        held, scope = self._scoped("turn_token = v.token AND turn_lease >= now()")
        sql = (
            "WITH v AS (SELECT * FROM unnest(%s::text[], %s::text[]) AS u(session_id, token)), "
            "held AS (SELECT session_id FROM daegam_sessions JOIN v USING (session_id) "
            f"{held} ORDER BY session_id FOR NO KEY UPDATE OF daegam_sessions) "
            "UPDATE daegam_sessions AS s SET turn_lease = now() + %s * interval '1 second' "
            "FROM held WHERE s.session_id = held.session_id RETURNING s.session_id"
        )
        params = [ids, [leases[sid] for sid in ids], *scope, lease_seconds]
        if conn is not None:
            cur = await conn.execute(sql, params)
            return {row["session_id"] for row in await cur.fetchall()}
        async with self._pool.connection() as pooled:
            cur = await pooled.execute(sql, params)
            return {row["session_id"] for row in await cur.fetchall()}

    async def orphans(self) -> list[str]:
        """부분 인덱스(daegam_sessions_open)로 찾는 한 SELECT 다."""
        orphaned, params = self._scoped(f"turn_token IS NOT NULL AND {LAPSED_LEASE}")
        async with self._pool.connection() as conn:
            cur = await conn.execute(f"SELECT session_id FROM daegam_sessions {orphaned}", params)
            return [row["session_id"] for row in await cur.fetchall()]

    async def repair_turn(
        self,
        session_id: str,
        orphan: str,
        *,
        status: str | None,
        pending_since: datetime,
        plan: RepairPlan | None,
    ) -> tuple[SessionRecord, RunMarker | None] | None:
        """연결 하나의 트랜잭션 하나다. lock_timeout 아래에서 그 토큰과 끝난 리스를 조건으로 행을
        FOR UPDATE 로 잡는다. 잠금을 기다리다 넘기면 오류로 실패하고 다음 읽기가 다시 한다. 행이
        없으면 다른 파드가 먼저 고쳤거나 잡은 것이다. 잡았으면 실행 표식을 지우며 읽고, 전사를
        읽어 plan 이 붙일 항목을 정하고, 색인 행을 고친 뒤 항목을 붙인다. 도는 턴의 전사 붙이기는
        같은 행의 FOR KEY SHARE 로 이 잠금을 기다렸다가 조건을 다시 보고 아무것도 넣지 않는다."""
        where, params = self._where(("session_id = %s", session_id), ("turn_token = %s", orphan))
        async with self._pool.connection() as conn, conn.transaction():
            await conn.execute("SELECT set_config('lock_timeout', %s, true)", [REPAIR_LOCK_TIMEOUT])
            cur = await conn.execute(
                f"SELECT {SELECT_ROW} FROM daegam_sessions {where} AND {LAPSED_LEASE} FOR UPDATE",
                params,
            )
            row = await cur.fetchone()
            if row is None:
                return None
            locked = _row_to_record(row)
            cur = await conn.execute(f"{RUN_TAKE_SQL} {RUN_RETURNING}", [session_id])
            markers = [_row_to_marker(taken) for taken in await cur.fetchall()]
            marker = next((m for m in markers if m.token == orphan), None)
            added: list[dict[str, Any]] = []
            if plan is not None:
                cur = await conn.execute(READ_SQL, (session_id, TRANSCRIPT_LIMIT))
                items = [stored["item"] for stored in reversed(await cur.fetchall())]
                added = plan(locked, marker, items)
            fields: dict[str, Any] = {}
            if status is not None and locked.status != STATUS_CLOSED:
                fields["status"] = status
                fields["pending_since"] = (
                    locked.pending_since or pending_since if status == STATUS_PENDING else None
                )
            assignments = ", ".join([*(f"{key} = %s" for key in fields), CLEAR_MARKS])
            cur = await conn.execute(
                f"UPDATE daegam_sessions SET {assignments} WHERE session_id = %s "
                f"RETURNING {SELECT_ROW}",
                [*fields.values(), session_id],
            )
            repaired = _row_to_record(await cur.fetchone())
            if added:
                await conn.execute(APPEND_SQL, (session_id, [dumps(item) for item in added]))
                await conn.execute(TRIM_SQL, (session_id, session_id, TRANSCRIPT_LIMIT))
        return repaired, marker

    async def align_status(
        self, session_id: str, *, status: str, pending_since: datetime | None, seen: datetime
    ) -> SessionRecord | None:
        """한 UPDATE 다. 열린 표시(`turn_token`)가 없으면 살아 있는 리스도 없다."""
        where, params = self._where(
            ("session_id = %s", session_id),
            ("last_turn_at = %s", seen),
            ("status <> %s", STATUS_CLOSED),
        )
        async with self._pool.connection() as conn:
            cur = await conn.execute(
                f"UPDATE daegam_sessions SET status = %s, pending_since = %s {where} "
                f"AND turn_token IS NULL RETURNING {SELECT_ROW}",
                [status, pending_since, *params],
            )
            row = await cur.fetchone()
        return _row_to_record(row) if row else None

    async def expire(self, before: datetime) -> list[str]:
        """한 UPDATE 다. 리스가 살아 있는 행은 같은 문장의 리스 조건이 건너뛴다(P41 9절)."""
        where, params = self._where(("status <> %s", STATUS_CLOSED), ("last_turn_at < %s", before))
        async with self._pool.connection() as conn:
            cur = await conn.execute(
                f"UPDATE daegam_sessions SET status = %s {where} AND NOT ({LIVE_LEASE}) "
                "RETURNING session_id",
                [STATUS_CLOSED, *params],
            )
            rows = await cur.fetchall()
        return [row["session_id"] for row in rows]

    async def closed_before(self, before: datetime) -> list[str]:
        """보존 기간이 지난 닫힌 세션이다. 읽기만 하므로 두 파드가 같은 sid 를 볼 수 있는데
        뒤따르는 전사·행 삭제가 둘 다 멱등이라 탈이 없다."""
        where, params = self._where(("status = %s", STATUS_CLOSED), ("last_turn_at < %s", before))
        async with self._pool.connection() as conn:
            cur = await conn.execute(
                f"SELECT session_id FROM daegam_sessions {where} ORDER BY last_turn_at",
                params,
            )
            rows = await cur.fetchall()
        return [row["session_id"] for row in rows]

    async def delete(self, session_id: str) -> None:
        """색인 행과 그 실행 표식, 작업 세션이 쥔 실행 id 를 한 문장으로 지운다. 둘 다 지운 행을
        읽으므로 이 인스턴스의 행만 지운다."""
        where, params = self._where(("session_id = %s", session_id))
        async with self._pool.connection() as conn:
            await conn.execute(
                f"WITH s AS (DELETE FROM daegam_sessions {where} RETURNING session_id), "
                "t AS (DELETE FROM daegam_task_runs t USING s WHERE t.session_id = s.session_id) "
                "DELETE FROM daegam_turn_runs r USING s WHERE r.session_id = s.session_id",
                params,
            )

    async def counts(self) -> dict[str, int]:
        out = dict.fromkeys(STATUSES, 0)
        where, params = self._where()
        async with self._pool.connection() as conn:
            cur = await conn.execute(
                f"SELECT status, count(*) AS n FROM daegam_sessions {where} GROUP BY status",
                params,
            )
            for row in await cur.fetchall():
                out[row["status"]] = int(row["n"])
        return out

    async def totals(self) -> dict[str, int]:
        where, params = self._where()
        async with self._pool.connection() as conn:
            cur = await conn.execute(
                f"""
                SELECT count(*) AS sessions, coalesce(sum(turns), 0) AS turns,
                       coalesce(sum(usage_input), 0) AS input_tokens,
                       coalesce(sum(usage_output), 0) AS output_tokens
                FROM daegam_sessions {where}
                """,
                params,
            )
            row = await cur.fetchone()
        return {
            key: int(row[key]) for key in ("sessions", "turns", "input_tokens", "output_tokens")
        }


# 칸 없이 리스 토큰만 보는 턴의 쓰기(`update_held`)의 SET 이다. 행을 바꾸지 않는다
NO_CHANGE = "session_id = session_id"
# 살아 있는 리스의 조건이다. 진행 중 표시(running)와 중지가 같은 조건을 본다
LIVE_LEASE = "turn_lease IS NOT NULL AND turn_lease >= now() AND turn_token IS NOT NULL"
# 리스가 살아 있지 않은 행이다. 잡기, 되살리기, sweep 의 후보 찾기가 본다
LAPSED_LEASE = "(turn_lease IS NULL OR turn_lease < now())"
# 잡기의 SET 이다. 자리표는 리스 초와 토큰이다. 앞 턴의 중지 표시와 시도의 id 를 지운다
CLAIM_SET = (
    "turn_lease = now() + %s * interval '1 second', turn_token = %s, cancel_token = NULL, "
    "turn_id = NULL"
)
# 되살리기가 지우는 열린 표시와 리스, 중지 표시다
CLEAR_MARKS = "turn_token = NULL, turn_lease = NULL, cancel_token = NULL"
# 되살리기 트랜잭션의 잠금 기다림 상한이다(P79 3절). 넘기면 오류로 실패하고 다음 읽기가 다시 한다
REPAIR_LOCK_TIMEOUT = "3s"
# 작업 세션인가다. 그 세션이 쥔 실행 id 의 행이 있으면 참이다
TASK_SESSION = (
    "EXISTS (SELECT 1 FROM daegam_task_runs t WHERE t.session_id = daegam_sessions.session_id)"
)
# 작업 세션이 쥔 실행 id 로 그 세션을 찾는 조건이다. 자리표는 실행 id 다
TASK_RUN_HOLDER = "session_id = (SELECT session_id FROM daegam_task_runs WHERE run_id = %s)"
# 세션을 만들거나 run_id 를 바꾼 문장(CTE `u`)의 행에 그 실행 id 를 적는다. 자리표는 실행 id 다
TASK_RUN_SQL = (
    "INSERT INTO daegam_task_runs (run_id, session_id) SELECT %s::text, u.session_id FROM u"
)
# 색인 한 줄로 읽는 열이다. 열 이름으로 적어 뒤에 열이 늘어도 준비해 둔 문장의 결과 형이 그대로다
# (P79 3절). running 과 orphan 은 저장하지 않고 리스와 열린 표시에서 정한다
SELECT_ROW = (
    "session_id, instance_id, tenant, user_sub, created_at, last_turn_at, turns, usage_input, "
    "usage_output, status, pending_since, title, turn_id, last_turn, upload_bytes, cite_seq, "
    f"outside_at, run_id, untrusted, {TASK_SESSION} AS task, ({LIVE_LEASE}) AS running, "
    f"CASE WHEN turn_token IS NOT NULL AND {LAPSED_LEASE} THEN turn_token END AS orphan"
)

# 실행 표식의 열이다(daegam_turn_runs)
RUN_COLUMNS = "token, turn_id, turn, kind, trace_id, span_id, started_at"
# 시도 기록의 UPDATE(CTE `u`)가 고친 행에 실행 표식을 적는다. 세션마다 한 행이라 덮어쓴다
RUN_UPSERT_SQL = (
    f"INSERT INTO daegam_turn_runs (session_id, {RUN_COLUMNS}) "
    "SELECT u.session_id, %s::text, %s::text, %s::integer, %s::text, %s::text, %s::text, "
    "%s::timestamptz FROM u "
    "ON CONFLICT (session_id) DO UPDATE SET token = EXCLUDED.token, turn_id = EXCLUDED.turn_id, "
    "turn = EXCLUDED.turn, kind = EXCLUDED.kind, trace_id = EXCLUDED.trace_id, "
    "span_id = EXCLUDED.span_id, started_at = EXCLUDED.started_at"
)
# 마감의 UPDATE(CTE `u`)가 고친 행에서만 이 토큰의 표식을 지운다. 자리표는 토큰이다
RUN_SETTLE_SQL = (
    "DELETE FROM daegam_turn_runs r USING u WHERE r.session_id = u.session_id AND r.token = %s"
)
# 실행 표식을 지우며 읽는다. 자리표는 세션 id 다
RUN_TAKE_SQL = "DELETE FROM daegam_turn_runs WHERE session_id = %s"
RUN_RETURNING = f"RETURNING {RUN_COLUMNS}"

# 되살리기의 plan 이다(`SessionIndex.repair_turn`). 잠근 한 줄, 그 토큰의 실행 표식, 저장된 전사를
# 받아 붙일 항목을 돌려준다
RepairPlan = Callable[
    ["SessionRecord", RunMarker | None, list[dict[str, Any]]], list[dict[str, Any]]
]


def _run_params(run: RunMarker) -> list[Any]:
    return [run.token, run.turn_id, run.turn, run.kind, run.trace_id, run.span_id, run.start]


def _row_to_marker(row: Mapping[str, Any]) -> RunMarker:
    return RunMarker(
        token=row["token"],
        turn_id=row["turn_id"],
        turn=int(row["turn"]),
        kind=row["kind"],
        trace_id=row["trace_id"],
        span_id=row["span_id"],
        start=row["started_at"],
    )


def _row_to_record(row: dict[str, Any]) -> SessionRecord:
    return SessionRecord(
        session_id=row["session_id"],
        tenant=row["tenant"],
        user_sub=row["user_sub"],
        created_at=row["created_at"],
        last_turn_at=row["last_turn_at"],
        turns=int(row["turns"]),
        usage_input=int(row["usage_input"]),
        usage_output=int(row["usage_output"]),
        status=row["status"],
        pending_since=row["pending_since"],
        instance_id=row.get("instance_id"),
        running=bool(row.get("running")),
        orphan=row.get("orphan"),
        outside_at=row.get("outside_at"),
        run_id=row.get("run_id"),
        untrusted=bool(row.get("untrusted")),
        task=bool(row.get("task")),
        **turn_record_fields(row),
    )


@dataclass
class SessionStore:
    checkpointer: BaseCheckpointSaver
    index: SessionIndex
    counters: Counters
    shared: bool
    warnings: list[str]
    # 화면용 전사다(P18 F1). 색인과 같은 저장소에 산다
    transcripts: TranscriptStore = field(default_factory=MemoryTranscripts)
    # 첨부 저장소다(P36 9절). 색인과 같은 저장소에 살고 세션이 닫히면 체크포인트와 함께 지운다
    uploads: UploadStore = field(default_factory=MemoryUploads)
    # 스레드의 체크포인트를 남김없이 지우는 도우미다. 한 번에 다 지우지 못하는 저장소(Redis)가 넣고
    # 없으면 체크포인터의 adelete_thread 한 번이다
    delete_thread: Callable[[str], Awaitable[None]] | None = None
    # 세션의 상태를 지울 때 함께 닫을 것이다(P40 5절). 코드 실행 문서면 기동이 샌드박스의 닫기를
    # 넣는다(boot.py). (세션 id, 까닭) 이고 까닭은 `CLOSE_*` 다
    on_close: Callable[[str, str], Awaitable[None]] | None = None
    # psycopg_pool 의 풀과 redis.asyncio 의 클라이언트다. 연 쪽이 넣고 close 가 닫는다. Redis 는
    # 첨부 본문을 바이트 그대로 담는 디코드하지 않는 클라이언트가 하나 더 있다
    _pool: Any = None
    _client: Any = None
    _binary_client: Any = None

    async def close(self) -> None:
        if self._pool is not None:
            await self._pool.close()
        for client in (self._client, self._binary_client):
            if client is not None:
                await client.aclose()

    async def sweep(
        self, ttl_seconds: int, *, retention_days: int = DEFAULT_TRANSCRIPT_RETENTION_DAYS
    ) -> list[str]:
        """세션 TTL 이 지난 것을 닫고, 보존 기간이 지난 기록을 지운다(D46). 만료 카운터도 지운다.

        두 시간은 다른 것이다. 세션 TTL 은 이어 쓸 수 있는 기간이라 지나면 상태가 closed 가 되고
        체크포인트가 사라지지만 전사와 색인 행은 남아 목록에서 열 수 있다. 보존 기간은 기록이
        남는 기간이라 그것까지 지나야 전사와 색인 행이 함께 사라진다. retention_days 가 0 이면
        두 시각이 같아 닫는 그 sweep 이 기록까지 지운다(D46 앞의 동작이다).

        돌려주는 것은 이번에 닫힌 세션이다. 기록을 지운 세션은 이미 닫힌 것이라 다시 알리지 않는다.
        """
        now = utcnow()
        expired = await self.close_expired(now - timedelta(seconds=ttl_seconds))
        await self.purge_records(now - timedelta(days=retention_days))
        try:
            await self.counters.purge(now)
        except Exception as e:  # noqa: BLE001  카운터 정리 실패는 다음 sweep 에 다시 한다
            log.warning("카운터 정리 실패: %s", e)
        return expired

    async def close_expired(self, before: datetime) -> list[str]:
        """last_turn_at 이 before 앞인 세션을 닫고 체크포인트와 첨부를 지운다.

        지우는 것은 그래프 상태와 첨부뿐이다. 닫힌 대화도 전사는 그대로라 `GET /sessions/{sid}` 가
        읽히고, 새 턴만 거절된다(turn_gate.py 의 `check_open_session` 이 내는 410
        `session_closed`). 전사의 사용자 항목에는 첨부의 이름·크기·쪽 수만 남는다. 지우지 못한
        것은 `drop_state` 가 적은 대로다.
        """
        expired = await self.index.expire(before)
        for session_id in expired:
            await self.drop_state(session_id, CLOSE_SESSION)
        return expired

    async def drop_state(self, session_id: str, reason: str = CLOSE_RUN) -> None:
        """닫힌 세션의 체크포인트와 첨부를 지우고 그 샌드박스를 닫는다(`on_close`). 전사와 색인 행은
        남는다. TTL 로 닫힌 세션과 턴이 대기 없이 끝나 닫힌 실행 세션(P41 9절, 기본 까닭)이 같은
        차례를 밟는다. 하나를 지우지 못해도 로그만 남기고 다음 것을 지운다. 지우지 못한 것은 보존
        기간이 지나 기록을 지울 때(`purge_records`) 마저 지우고, 샌드박스는 플랫폼의 회수가
        닫는다."""
        for what, delete in (
            ("체크포인트", self.delete_checkpoints),
            ("첨부", self.uploads.delete),
            ("샌드박스", partial(self._close_sandbox, reason=reason)),
        ):
            try:
                await delete(session_id)
            except Exception as e:  # noqa: BLE001  삭제 실패가 닫기를 되돌리지 않는다
                log.warning("세션 %s %s 삭제 실패: %s", session_id, what, e)

    async def _close_sandbox(self, session_id: str, *, reason: str) -> None:
        if self.on_close is not None:
            await self.on_close(session_id, reason)

    async def delete_checkpoints(self, session_id: str) -> None:
        """세션의 체크포인트를 남김없이 지운다. sweep 과 세션 지우기가 같이 쓴다."""
        if self.delete_thread is not None:
            await self.delete_thread(session_id)
            return
        await self.checkpointer.adelete_thread(session_id)

    async def delete_session(self, session_id: str, *, close_sandbox: bool = True) -> None:
        """세션 하나를 통째로 지운다(P36 4절). 체크포인트, 첨부, 전사, 색인 행 차례다.

        색인 행을 마지막에 지워야 가운데서 멈춰도 남은 것을 색인으로 다시 찾는다. 장기 기억은 사람의
        것이라 지우지 않는다. 샌드박스는 먼저 닫는다. 닫지 못해도 지우기는 이어 가고 플랫폼의 회수가
        닫는다.
        """
        if close_sandbox:
            try:
                await self._close_sandbox(session_id, reason=CLOSE_DELETED)
            except Exception as e:  # noqa: BLE001  샌드박스 닫기 실패가 지우기를 막지 않는다
                log.warning("세션 %s 샌드박스 닫기 실패: %s", session_id, e)
        await self.delete_checkpoints(session_id)
        await self._delete_records(session_id)

    async def purge_records(self, before: datetime) -> list[str]:
        """닫힌 지 before 보다 오래된 세션의 전사와 색인 행을 함께 지운다. 세션 지우기와 같은 차례라
        체크포인트부터 다시 지운다. 닫을 때(`drop_state`) 지우지 못한 체크포인트는 색인 행이 사라진
        뒤에는 아무도 찾지 못하기 때문이다. 남은 행은 다음 sweep 이 다시 집어 마저 지우고, 두 파드가
        같은 세션을 집어도 모두 멱등이라 탈이 없다."""
        purged: list[str] = []
        hold = utcnow() - RUN_RECORD_HOLD
        for session_id in await self.index.closed_before(before):
            if before > hold and await self._run_held(session_id, hold):
                continue
            try:
                # 닫힌 세션이라 샌드박스는 닫을 때 이미 닫았다
                await self.delete_session(session_id, close_sandbox=False)
            except Exception as e:  # noqa: BLE001  삭제 실패는 다음 sweep 에 다시 한다
                log.warning("세션 %s 기록 삭제 실패: %s", session_id, e)
                continue
            purged.append(session_id)
        return purged

    async def _run_held(self, session_id: str, hold: datetime) -> bool:
        """hold 뒤에 닫힌 실행 세션인가. 보존 기간이 RUN_RECORD_HOLD 보다 짧을 때(0일)만 묻는다."""
        record = await self.index.get(session_id)
        return record is not None and record.run_id is not None and record.last_turn_at >= hold

    async def _delete_records(self, session_id: str) -> None:
        """세션의 첨부, 전사, 색인 행을 지운다. 색인 행을 마지막에 지운다. 먼저 지우면 그 사이에
        파드가 죽었을 때 첨부나 전사만 남아 아무도 찾지 못한다. 첨부는 닫을 때 이미 지웠어도 다시
        지운다. 삭제가 멱등이라 닫을 때 실패한 삭제를 여기서 마저 한다."""
        await self.uploads.delete(session_id)
        await self.transcripts.delete(session_id)
        await self.index.delete(session_id)


async def open_session_store(config: AgentConfig) -> SessionStore:
    memory = config.memory
    if memory is None:
        log.warning(
            "%s: memory 가 없어 InMemorySaver 와 파드별 색인·카운터로 돈다", NO_SHARED_STATE
        )
        return SessionStore(
            checkpointer=InMemorySaver(),
            index=MemorySessionIndex(),
            counters=MemoryCounters(),
            shared=False,
            warnings=[NO_SHARED_STATE],
            transcripts=MemoryTranscripts(),
            uploads=MemoryUploads(),
        )
    checkpointer = memory["checkpointer"]
    dsn_env = checkpointer["dsn_env"]
    instance_id = config.instance["id"]
    if checkpointer["kind"] == "redis":
        # Redis 쪽은 여기서 가져온다. 그 모듈이 SessionRecord·SessionStore 를 쓰므로 위에서
        # 가져오면 순환 import 다
        from runtime.session_index_redis import open_redis_store

        conn = resolve_redis(dsn_env)
        if conn is None:
            raise _dsn_missing(dsn_env)
        return await open_redis_store(conn, instance_id=instance_id)
    dsn = resolve_dsn(dsn_env)
    if not dsn:
        raise _dsn_missing(dsn_env)
    return await open_postgres_store(dsn, instance_id=instance_id)


def memory_kind(config: AgentConfig) -> str:
    """기동 이벤트에 싣는 memory 종류다. postgres·redis, 없으면 none 이다."""
    if config.memory is None:
        return "none"
    return str(config.memory["checkpointer"]["kind"])


def _dsn_missing(dsn_env: str) -> ConfigError:
    return missing_dsn_error("memory_dsn_missing", "memory.checkpointer.dsn_env", dsn_env)


async def setup_postgres(
    setup: Callable[[], Awaitable[Any]], *, what: str, retries: int = PG_SETUP_RETRIES
) -> None:
    """setup 을 부르고 파드끼리 겹친 것이면 다시 부른다(P17 F5). Redis 쪽과 같은 모양이다.

    DDL 이 전부 IF NOT EXISTS 여도 파드가 여럿이면 카탈로그 유일 인덱스에서 진다(같은 이름의
    테이블·인덱스를 동시에 만들면 UniqueViolation·DuplicateTable 이고, 마이그레이션 기록 표에
    같은 번호를 넣으면 UniqueViolation 이다). 다른 파드가 이미 만든 것이라 다시 부르면 통과하므로
    상한까지 다시 부르되 조금씩 쉬어 같은 박자로 부딪히지 않게 한다. 그 밖의 예외는
    memory_setup_failed 다.
    """
    collisions = 0
    while True:
        try:
            await setup()
            return
        except PG_SETUP_CONFLICTS as e:
            collisions += 1
            if collisions <= retries:
                await asyncio.sleep(PG_SETUP_RETRY_DELAY_SECONDS * collisions)
                continue
            raise ConfigError(
                "memory_setup_failed",
                f"{what} 생성이 {collisions} 번 겹쳤다: {type(e).__name__}",
            ) from e
        except Exception as e:
            raise ConfigError(
                "memory_setup_failed", f"{what} setup 실패: {type(e).__name__}"
            ) from e


async def open_postgres_store(
    dsn: str, *, instance_id: str | None = None, max_size: int = 8
) -> SessionStore:
    from psycopg_pool import AsyncConnectionPool

    # 체크포인터와 색인이 풀 하나를 나눠 쓰므로 동시 턴이 한 연결에 몰리지 않는다. 색인은
    # instance_id 로 나뉜다. 같은 Database 를 바인딩한 다른 인스턴스의 세션은 보이지 않는다.
    pool = AsyncConnectionPool(
        dsn,
        open=False,
        min_size=1,
        max_size=max_size,
        kwargs=postgres_pool_kwargs(fenced=True),
    )
    await pool.open()
    try:
        await check_postgres(pool, "세션 저장소(Postgres)", MEMORY_CODES)
        saver = FencedPostgresSaver(pool)
        await setup_postgres(saver.setup, what="체크포인터 테이블")
        index = PostgresSessionIndex(pool, instance_id)
        await setup_postgres(index.setup, what="세션 색인 테이블")
    except BaseException:
        # 다시 열기 도중 앱이 닫혀 취소될 때도 닫는다(T22). 아직 스택에 오르기 전이다
        await pool.close()
        raise
    return SessionStore(
        checkpointer=saver,
        index=index,
        counters=PostgresCounters(pool),
        shared=True,
        warnings=[],
        transcripts=PostgresTranscripts(pool),
        uploads=PostgresUploads(pool),
        _pool=pool,
    )


async def sweep_loop(
    store: SessionStore,
    ttl_seconds: int,
    interval: float,
    on_expired: Callable[[str], None],
    *,
    retention_days: int = DEFAULT_TRANSCRIPT_RETENTION_DAYS,
    repair: Callable[[], Awaitable[Any]] | None = None,
) -> None:
    """기동하자마자 한 번, 그 뒤 interval 마다 sweep 한다(P44 11절). 잠들었다 깬 파드는 첫 주기를
    기다리지 않고 잠든 동안 TTL 이 지난 세션을 닫고 보존 기간이 지난 기록을 지운다. repair 를 주면
    TTL 로 닫기 전에 끊긴 열린 표시를 되살린다(P79 3절, turn_repair.py). 아무도 다시 열지 않는
    세션의 끊긴 턴도 그 줄과 turn_end 를 얻는다. 한 주기가 실패해도 다음 주기에 다시 한다."""
    while True:
        if repair is not None:
            try:
                await repair()
            except Exception as e:  # noqa: BLE001  되살리기 실패는 다음 주기에 다시 한다
                log.warning("끊긴 턴 되살리기 실패: %s", e)
        try:
            expired = await store.sweep(ttl_seconds, retention_days=retention_days)
        except Exception as e:  # noqa: BLE001  sweep 실패는 다음 주기에 다시 시도한다
            log.warning("세션 sweep 실패: %s", e)
            expired = []
        for session_id in expired:
            on_expired(session_id)
        await asyncio.sleep(interval)
