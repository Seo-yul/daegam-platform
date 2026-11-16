"""기동: 저장소가 아직 뜨지 않았을 때 HTTP 를 먼저 열고 배경에서 다시 연다(T22). 저장소가 접속을
거부해도 같이 기다리며 그 이유를 보인다(T23). 설정 오류는 이유를 termination log 에 남기고 실패한다.

다시 시도의 결과는 테스트가 바꾸는 값(`gate`, `stage`)이 정한다. 다시 시도의 횟수나 간격에 기대지
않는다. 실제 Postgres 로 거부를 보는 테스트는 `DAEGAM_TEST_PG_DSN` 이 있을 때만 돈다.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import httpx
import psycopg
import pytest
from psycopg.conninfo import conninfo_to_dict, make_conninfo
from psycopg_pool import AsyncConnectionPool
from redis.exceptions import AuthenticationError, NoPermissionError

from runtime import boot, dsn, sessions
from runtime.config import ConfigError
from runtime.dsn import (
    MEMORY_CODES,
    MEMORY_REJECTED,
    MEMORY_UNREACHABLE,
    StoreCodes,
    check_postgres,
    check_redis,
    is_unreachable,
    is_waitable,
)
from runtime.knowledge import KnowledgeTools
from runtime.sessions import open_session_store
from runtime.state import BACKGROUND_RETRY_MAX_SECONDS, Settings
from tests.app_fixtures import INSTANCE_TOKEN, bearer
from tests.conftest import PG_DSN, chat, events_named, needs_pg, run_app, wait_for
from tests.pg_fakes import FakePool

FAST = Settings(retry_initial=0.05)


def _unreachable() -> ConfigError:
    return ConfigError(MEMORY_UNREACHABLE, "세션 저장소(Redis) 에 연결할 수 없다: ConnectionError")


def _rejected() -> ConfigError:
    return ConfigError(
        MEMORY_REJECTED, "세션 저장소(Redis) 가 접속을 거부했다. 권한을 확인해야 한다"
    )


def _store_gate(monkeypatch: pytest.MonkeyPatch, failure: Any = _unreachable) -> dict[str, Any]:
    """`gate["open"]` 이 거짓인 동안 세션 저장소 열기가 failure 로 실패한다. 부른 횟수는
    `gate["calls"]` 다."""
    gate: dict[str, Any] = {"open": False, "calls": 0}

    async def open_store(cfg: Any) -> Any:
        gate["calls"] += 1
        if not gate["open"]:
            raise failure()
        return await open_session_store(cfg)

    monkeypatch.setattr(boot, "open_session_store", open_store)
    return gate


async def _is_ready(client: httpx.AsyncClient) -> bool:
    return (await client.get("/healthz", params={"probe": "ready"})).status_code == 200


async def test_runtime_serves_health_before_its_store_and_opens_it_later(
    support_doc, monkeypatch, termination_log: Path
) -> None:
    """저장소에 연결할 수 없어도 HTTP 는 뜬다. 살아 있음 검사는 200 이고 상세는 이유를 싣는다. 준비
    검사와 다른 요청은 503 dependencies_unavailable 이다. 배경에서 다시 열면 기동 이벤트를 한 번
    내고 턴을 받는다."""
    gate = _store_gate(monkeypatch)
    async with run_app(support_doc, settings=FAST) as running:
        client = running.client
        health = await client.get("/healthz")
        assert health.status_code == 200
        assert health.json() == {
            "status": "starting",
            "unready": {"code": "dependencies_unavailable", "message": str(_unreachable())},
        }
        ready = await client.get("/healthz", params={"probe": "ready"})
        assert ready.status_code == 503 and ready.json()["code"] == "dependencies_unavailable"
        assert "Redis" not in ready.text  # kubelet 이 보는 본문에 원문을 싣지 않는다
        for refused in (
            (await chat(running, "안녕"))[0],
            await client.get("/sessions", params={"user_sub": "alice"}),
            await client.get("/metrics"),
        ):
            assert refused.status_code == 503
            assert refused.json()["code"] == "dependencies_unavailable"
            assert refused.headers["Retry-After"] == "5"
        assert not running.observed("runtime_start")
        assert not termination_log.exists()  # 기다리는 것은 기동 실패가 아니다

        gate["open"] = True
        await wait_for(lambda: _is_ready(client), what="준비 검사 200")
        assert gate["calls"] >= 2
        response, sse = await chat(running, '더해 줘 {"tool": "Add", "args": {"a": 3, "b": 4}}')
        assert response.status_code == 200
        assert any(d["content"] == "7" for d in events_named(sse, "tool_result"))
        starts = running.observed("runtime_start")
        assert len(starts) == 1 and starts[0]["tools"] == ["Add"]
        assert (await client.get("/healthz")).json()["unready"] is None


async def test_starting_health_hides_the_reason_without_the_instance_token(
    support_doc, monkeypatch
) -> None:
    """상세(이유)는 인스턴스 토큰을 낸 요청에만 낸다. 플랫폼이 그것을 화면의 이유로 싣는다(T21)."""
    _store_gate(monkeypatch)
    settings = Settings(instance_token=INSTANCE_TOKEN, retry_initial=0.05)
    async with run_app(support_doc, settings=settings) as running:
        client = running.client
        assert (await client.get("/healthz")).json() == {"status": "starting"}
        detail = await client.get("/healthz", headers=bearer(INSTANCE_TOKEN))
        assert detail.json()["unready"]["code"] == "dependencies_unavailable"


async def test_a_wrong_setting_still_fails_the_start(
    support_doc, monkeypatch, termination_log: Path
) -> None:
    """연결할 수 없는 것이 아니라 설정이 틀린 것(env 가 비었다)은 기다리지 않고 기동이 실패한다.
    이유는 termination log 에 남아 플랫폼이 CrashLoopBackOff 의 이유로 보인다(T23)."""
    missing = ConfigError("memory_dsn_missing", "memory.checkpointer.dsn_env 가 비어 있다")

    async def open_store(cfg: Any) -> Any:
        raise missing

    monkeypatch.setattr(boot, "open_session_store", open_store)
    with pytest.raises(ConfigError) as info:
        async with run_app(support_doc):
            pass
    assert info.value is missing
    assert termination_log.read_text(encoding="utf-8") == str(missing)


async def test_closing_while_still_starting_stops_the_retries(support_doc, monkeypatch) -> None:
    """저장소가 끝내 뜨지 않은 채 앱이 닫혀도 다시 시도가 멈추고 닫기가 걸리지 않는다."""
    gate = _store_gate(monkeypatch)

    async def retried() -> bool:
        return gate["calls"] >= 2

    async with asyncio.timeout(5):
        async with run_app(support_doc, settings=FAST):
            await wait_for(retried, what="배경 다시 시도")
    seen = gate["calls"]
    await asyncio.sleep(0.2)
    assert gate["calls"] == seen


async def test_an_unreachable_postgres_is_memory_unreachable(monkeypatch) -> None:
    """Postgres 에 연결할 수 없는 것은 테이블 setup 실패와 갈라 memory_unreachable 로 낸다. 그래야
    기동이 기다릴 오류로 알아본다."""
    monkeypatch.setattr(dsn, "OPEN_CHECK_TIMEOUT_SECONDS", 0.5)
    with pytest.raises(ConfigError) as info:
        await sessions.open_postgres_store("postgresql://u:p@127.0.0.1:9/agent")
    assert info.value.code == "memory_unreachable"
    assert is_unreachable(info.value)


def test_only_connection_failures_are_waited_for() -> None:
    """기다리는 것은 저장소에 아직 연결할 수 없는 것과 접속 거부다. 화면에 dependencies_unavailable
    로 보이는 것은 앞의 것뿐이다. 설정 오류는 기다리지 않는다."""
    for code in ("memory_unreachable", "knowledge_db_unreachable", "long_term_unreachable"):
        assert is_unreachable(ConfigError(code, "x")) and is_waitable(ConfigError(code, "x"))
    for code in ("memory_rejected", "knowledge_db_rejected", "long_term_rejected"):
        assert is_waitable(ConfigError(code, "x")) and not is_unreachable(ConfigError(code, "x"))
    for code in ("memory_dsn_missing", "long_term_schema_missing"):
        assert not is_waitable(ConfigError(code, "x"))


def test_next_retry_spreads_the_pause_and_doubles_up_to_the_cap() -> None:
    """쉴 시간은 [간격/2, 간격] 이고 다음 간격은 두 배이되 상한에서 멈춘다."""
    for retry in (0.05, 2.0, 20.0, BACKGROUND_RETRY_MAX_SECONDS):
        pause, following = boot.next_retry(retry)
        assert retry / 2 <= pause <= retry
        assert following == min(retry * 2, BACKGROUND_RETRY_MAX_SECONDS)


async def test_each_failed_retry_closes_what_it_opened_and_shows_why(
    support_doc, monkeypatch
) -> None:
    """다시 열기는 세션 저장소를 열고 knowledge 에서 실패하면 그 저장소를 닫는다. 끝내 연 시도의
    자원은 앱이 닫힐 때 닫는다. healthz 의 이유는 연결할 수 없는 것이면
    dependencies_unavailable 아래 안쪽 오류이고, 다시 열다 드러난 설정 오류와 코드
    결함(startup_failed)은 그 코드와 문구다. 플랫폼이 `code: message` 로 싣으므로 같은 코드를
    message 에 겹쳐 싣지 않는다."""
    opened: list[str] = []
    closed: list[str] = []

    async def open_store(cfg: Any) -> Any:
        store = await open_session_store(cfg)
        name = f"store-{len(opened)}"
        opened.append(name)
        real_close = store.close

        async def close() -> None:
            closed.append(name)
            await real_close()

        store.close = close  # type: ignore[method-assign]
        return store

    failures = {
        "unreachable": lambda: ConfigError(
            "knowledge_db_unreachable",
            "knowledge[faq] 의 Database(DAEGAM_DB_0_URL) 에 연결할 수 없다: PoolTimeout",
        ),
        "schema": lambda: ConfigError("long_term_schema_missing", "daegam_store 스키마가 없다"),
        "bug": lambda: ValueError("코드 결함"),
    }
    reasons = {
        "unreachable": {
            "code": "dependencies_unavailable",
            "message": (
                "knowledge_db_unreachable: "
                "knowledge[faq] 의 Database(DAEGAM_DB_0_URL) 에 연결할 수 없다: PoolTimeout"
            ),
        },
        "schema": {"code": "long_term_schema_missing", "message": "daegam_store 스키마가 없다"},
        "bug": {"code": "startup_failed", "message": "ValueError"},
    }
    stage = {"now": "unreachable"}
    real_open = KnowledgeTools.open

    async def open_knowledge(cfg: Any) -> Any:
        failure = failures.get(stage["now"])
        if failure is not None:
            raise failure()
        return await real_open(cfg)

    monkeypatch.setattr(boot, "open_session_store", open_store)
    monkeypatch.setattr(boot.KnowledgeTools, "open", open_knowledge)
    async with run_app(support_doc, settings=FAST) as running:
        client = running.client
        for now, reason in reasons.items():
            stage["now"] = now

            async def shows(want: dict[str, str] = reason) -> bool:
                return (await client.get("/healthz")).json().get("unready") == want

            await wait_for(shows, what=f"{now} 의 이유")
        stage["now"] = "ok"
        await wait_for(lambda: _is_ready(client), what="준비 검사 200")
        assert closed == opened[:-1]
    assert closed == opened


# -- 접속 거부 (T23) -----------------------------------------------------------------------------


class _Pgconn:
    def __init__(self, used_password: bool) -> None:
        self.used_password = used_password


class _Connected:
    """직접 접속이 된 연결이다. 풀이 실패한 사이 서버가 떴을 때다."""

    async def __aenter__(self) -> _Connected:
        return self

    async def __aexit__(self, *exc: Any) -> None:
        return None


REJECTED_MESSAGE = (
    "세션 저장소(Postgres) 가 접속을 거부했다. 사용자 이름과 비밀번호, Database 이름, 이 계정의 "
    "접속 권한을 확인해야 한다. Database 연결 수가 한도에 이르렀을 때도 이렇게 거부된다"
)
UNREACHABLE_MESSAGE = "세션 저장소(Postgres) 에 연결할 수 없다: TimeoutError"


@pytest.mark.parametrize(
    ("direct", "code", "message"),
    [
        # 서버가 비밀번호를 받은 뒤 거부했다(틀린 비밀번호, 없는 Database, 권한 없음, 연결 수
        # 한도)
        (
            psycopg.OperationalError("FATAL", pgconn=_Pgconn(True)),
            MEMORY_REJECTED,
            REJECTED_MESSAGE,
        ),
        # 비밀번호를 묻기 전에 끊겼다(기동 중, 포트 닫힘)
        (
            psycopg.OperationalError("refused", pgconn=_Pgconn(False)),
            MEMORY_UNREACHABLE,
            UNREACHABLE_MESSAGE,
        ),
        # pg_hba 는 비밀번호를 묻기 전에 막으므로 서버 문장으로 알아본다
        (
            psycopg.OperationalError('FATAL:  no pg_hba.conf entry for host "10.0.0.1"'),
            MEMORY_REJECTED,
            REJECTED_MESSAGE,
        ),
        # 까닭을 모르는 실패와 그사이 서버가 뜬 경우는 연결할 수 없는 쪽이다
        (ValueError("x"), MEMORY_UNREACHABLE, UNREACHABLE_MESSAGE),
        (None, MEMORY_UNREACHABLE, UNREACHABLE_MESSAGE),
    ],
)
async def test_a_postgres_failure_is_told_apart_by_a_direct_connection(
    monkeypatch, direct: Exception | None, code: str, message: str
) -> None:
    """풀이 감춘 실패의 까닭은 직접 한 번 접속해 본다. 서버가 인증을 마친 뒤 거부했으면(libpq
    used_password) 접속 거부이고, 비밀번호를 묻기 전에 끊겼으면 연결할 수 없는 것이다. 원문(DSN)은
    싣지 않는다."""

    async def connect(conninfo: str, **kwargs: Any) -> Any:
        if direct is None:
            return _Connected()
        raise direct

    monkeypatch.setattr(dsn.psycopg.AsyncConnection, "connect", connect)
    with pytest.raises(ConfigError) as info:
        await check_postgres(FakePool(failing=True), "세션 저장소(Postgres)", MEMORY_CODES)
    assert (info.value.code, info.value.message) == (code, message)
    assert "secret" not in str(info.value)


@pytest.mark.parametrize(
    ("error", "code"),
    [
        (AuthenticationError("invalid username-password pair"), MEMORY_REJECTED),
        (NoPermissionError("NOPERM this user has no permissions to run 'ping'"), MEMORY_REJECTED),
        (ConnectionError("refused"), MEMORY_UNREACHABLE),
    ],
)
async def test_a_redis_failure_is_told_apart_by_its_error(error: Exception, code: str) -> None:
    """Redis 는 비밀번호 거부(AuthenticationError)와 권한 없음(NoPermissionError)을 따로 준다."""

    class Client:
        async def ping(self) -> None:
            raise error

    with pytest.raises(ConfigError) as info:
        await check_redis(Client(), "세션 저장소(Redis)", MEMORY_CODES)
    assert info.value.code == code
    assert "invalid username" not in info.value.message and "NOPERM" not in info.value.message


async def test_a_rejected_store_is_waited_for_and_its_reason_is_shown(
    support_doc, monkeypatch, termination_log: Path
) -> None:
    """첫 시도에서 접속이 거부돼도 파드를 죽이지 않고 기다린다. 연결 수 한도처럼 기다리면 풀리는
    거부가 있고 비밀번호도 서버 쪽에서 고치면 풀린다. healthz 의 이유는 그 코드와 문구라 화면에서
    연결할 수 없는 것과 갈린다(T21)."""
    gate = _store_gate(monkeypatch, failure=_rejected)
    async with run_app(support_doc, settings=FAST) as running:
        client = running.client
        health = (await client.get("/healthz")).json()
        assert health == {
            "status": "starting",
            "unready": {"code": MEMORY_REJECTED, "message": _rejected().message},
        }
        ready = await client.get("/healthz", params={"probe": "ready"})
        assert ready.status_code == 503 and ready.json()["code"] == "dependencies_unavailable"
        assert not termination_log.exists()
        gate["open"] = True
        await wait_for(lambda: _is_ready(client), what="준비 검사 200")


@needs_pg
async def test_a_real_postgres_tells_a_refusal_from_a_closed_port(monkeypatch) -> None:
    """실제 서버에서도 틀린 비밀번호와 없는 Database 는 접속 거부이고 닫힌 포트는 연결할 수 없는
    것이다."""
    assert PG_DSN is not None
    monkeypatch.setattr(dsn, "OPEN_CHECK_TIMEOUT_SECONDS", 1.0)
    parts = conninfo_to_dict(PG_DSN)
    codes = StoreCodes(unreachable="unreachable", rejected="rejected")
    cases = [
        ("rejected", make_conninfo(**{**parts, "password": "wrong-password"})),
        ("rejected", make_conninfo(**{**parts, "dbname": "no_such_database"})),
        ("unreachable", make_conninfo(**{**parts, "port": "9"})),
    ]
    for want, conninfo in cases:
        pool = AsyncConnectionPool(conninfo, open=False, min_size=1, max_size=1)
        await pool.open()
        try:
            with pytest.raises(ConfigError) as info:
                await check_postgres(pool, "Database", codes)
        finally:
            await pool.close()
        assert info.value.code == want
