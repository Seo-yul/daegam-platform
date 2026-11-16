"""턴 리스를 잃은 턴의 쓰기를 막는 펜스다(P79 2절, D98).

리스를 잡은 턴은 제 펜스를 문맥 변수에 두고(`fenced`), 그 뒤의 일(시도 기록, 그래프의 태스크와
LangGraph 가 옮겨 받은 문맥, 마감)이 그것을 물려받는다. 펜스는 마감을 이벤트 루프 시계로 들고, 그
마감이 지나거나 리스를 잃었다고 적히면(`Fence.lose`) 선다. 마감을 미는 것(`Fence.move`)은 파드의
리스 관리자다.

선 펜스는 바이트가 프로세스를 떠나는 곳에서 본다.

- Redis: 클라이언트를 만든 뒤 풀이 고른 연결 클래스를 그 펜스 하위 클래스로 바꾼다
  (`fence_redis_client`). 연결을 열기 전과 `send_packed_command` 맨 앞에서 보므로 명령, 파이프라인,
  스크립트의 모든 보내기와 redis-py 의 다시 보내기가 거친다. 연결의 핸드셰이크(HELLO·AUTH·SELECT)는
  턴의 쓰기가 아니라 펜스 밖에서 끝까지 돈다. 중간에 끊기면 덜 맺은 연결이 풀로 돌아가 다음 호출이
  틀린 DB 나 인증 없이 돈다. 생성자에 연결 클래스를 넘기면 `rediss://`·`unix://` 주소와
  `ssl=True` 가 그 값을 쓰지 않아 바꾸기로 한다.
- Postgres: 세션 풀의 커서 클래스(`FencedCursor`)가 `execute`·`executemany`·`stream`·`copy` 를
  보내기 전에 본다. 체크포인터, 색인, 전사, 카운터, 첨부가 모두 이 커서를 만든다. 펜스 문맥의
  문장은 펜스 마감까지만 돈다(`bounded`). 마감에 걸린 문장은 psycopg 가 서버에 취소를 보낸다.
  파이프라인은 문장을 큐에만 넣고 닫을 때 보내므로 체크포인터(`FencedPostgresSaver`)는 커서 블록
  전체를, 첨부의 트랜잭션은 COMMIT 까지를 마감 안에 둔다.
- 장기 기억: 저장소의 배치 태스크는 턴 밖에서 여러 턴의 일을 묶어 보내므로, 턴이 저장소를 부르는
  자리에서 본다(`FencedStore`). 저장소의 풀과 클라이언트에 걸면 배치 태스크가 한 턴의 펜스를 물고
  다른 턴의 일까지 거절하므로 걸지 않는다.

거절은 `TurnFenced` 다. redis 의 ConnectionError·TimeoutError 도 OSError 도 아니라 다시 보내기
루프가 곧바로 멈춘다. 펜스 문맥이 없는 일(파드의 태스크, 턴 밖의 라우트)은 막지 않는다.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Callable, Iterable, Iterator
from contextlib import asynccontextmanager, contextmanager
from contextvars import ContextVar
from functools import cache
from typing import Any

from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
from langgraph.store.base import BaseStore, Op, Result
from psycopg import AsyncCursor
from psycopg.rows import Row

FENCED_MESSAGE = "턴 리스를 잃어 저장소에 쓰지 않았다"
CUT_MESSAGE = "턴 리스를 잃어 보내던 문장을 멈췄다"
REFUSED_MESSAGE = "저장소가 이 턴의 리스 토큰을 거절해 쓰지 않았다"

# 서버 쪽 토큰 검사다(P79 2절). 턴 자신의 색인 쓰기와 전사 붙이기는 리스 토큰을 실어, 그 토큰이
# 살아 있는 리스를 쥐고 있을 때만 쓰고 아니면 0 행이나 0 으로 거절된다(`refused`). Postgres 는
# daegam_sessions 의 조건 조각이고 자리표는 토큰 하나다
HELD_LEASE_SQL = "turn_token = %s AND turn_lease >= now()"
# Redis 스크립트의 앞머리다. KEYS[2] 가 그 세션의 리스 키, ARGV[1] 이 토큰이다. 토큰이 비면 보지
# 않는다
HELD_LEASE_LUA = """if ARGV[1] ~= '' and redis.call('GET', KEYS[2]) ~= ARGV[1] then
  return 0
end
"""


class TurnFenced(Exception):
    """펜스가 선 턴의 쓰기다. 이 예외 자체는 보내기 전의 거절이라 저장소에 아무것도 가지 않았다.
    보낸 뒤 마감에 걸려 멈춘 것은 하위 클래스 `StatementCut` 이다."""


class StatementCut(TurnFenced):
    """펜스 마감에 걸려 멈춘 문장이나 블록이다. 이미 보냈으므로 서버가 적용했을 수 있다."""


class Fence:
    """리스 하나의 펜스다. 마감은 만든 이벤트 루프의 시계(`loop.time()`)로 잰 초다. 루프를 들고
    있어 루프 밖 스레드의 동기 저장소 호출도 같은 시계로 본다. 마감이 움직이면 그 마감으로 돌고
    있는 문장의 제한 시간도 같이 옮긴다."""

    def __init__(self, deadline: float, *, on_lose: Callable[[], None] | None = None) -> None:
        self._loop = asyncio.get_running_loop()
        self._deadline = deadline
        self._lost = False
        # 이 펜스의 마감으로 돌고 있는 제한 시간이다(`bound`)
        self._bounds: set[asyncio.Timeout] = set()
        # 처음 잃었을 때 한 번 부른다. 리스 관리자가 리스를 놓고 턴을 멈추게 한다(lease_keeper.py)
        self._on_lose = on_lose

    @property
    def deadline(self) -> float:
        return self._deadline

    @property
    def standing(self) -> bool:
        """펜스가 섰는가. 리스를 잃었다고 적혔거나 마감이 지났다."""
        return self._lost or self._loop.time() >= self._deadline

    @property
    def lost(self) -> bool:
        """리스를 잃었다고 적혔는가(`lose`). 마감이 지난 것만으로는 거짓이다."""
        return self._lost

    # move 와 lose 는 이벤트 루프 스레드에서만 부른다. 돌고 있는 제한 시간을 옮기는 reschedule 이
    # 루프의 call_at 이기 때문이다. 리스 관리자와 거절된 쓰기가 루프 위에서 부른다

    def move(self, deadline: float) -> None:
        """마감을 옮긴다. 선 펜스는 다시 내리지 않는다."""
        if self.standing:
            return
        self._deadline = deadline
        self._reschedule(deadline)

    def lose(self) -> None:
        """리스를 잃었다. 마감과 상관없이 서고, 돌고 있는 문장을 지금 멈추며 on_lose 를 부른다. 두
        번째부터는 아무것도 하지 않는다."""
        if self._lost:
            return
        self._lost = True
        self._reschedule(self._loop.time())
        if self._on_lose is not None:
            self._on_lose()

    def _reschedule(self, when: float) -> None:
        for timeout in list(self._bounds):
            if not timeout.expired():
                timeout.reschedule(when)

    def check(self) -> None:
        if self.standing:
            raise TurnFenced(FENCED_MESSAGE)

    @asynccontextmanager
    async def bound(self) -> AsyncIterator[None]:
        """펜스를 본 뒤 안의 일을 펜스 마감까지만 돌린다. 펜스가 선 뒤에 실패한 일은 마감의 제한
        시간이든, 취소된 문장의 오류(psycopg 의 파이프라인은 QueryCanceled 로 끝날 수 있다)든
        `StatementCut` 이다."""
        self.check()
        try:
            async with asyncio.timeout_at(self._deadline) as timeout:
                self._bounds.add(timeout)
                try:
                    yield
                finally:
                    self._bounds.discard(timeout)
        except TurnFenced:
            raise
        except Exception as e:
            if self.standing:
                raise StatementCut(CUT_MESSAGE) from e
            raise


# 지금 도는 턴의 펜스다. 펜스 문맥 밖이면 None 이다
_CURRENT: ContextVar[Fence | None] = ContextVar("daegam_turn_fence", default=None)


def current() -> Fence | None:
    return _CURRENT.get()


def check() -> None:
    """지금 문맥의 펜스가 섰으면 `TurnFenced` 다. 펜스 문맥 밖이면 아무것도 하지 않는다."""
    fence = _CURRENT.get()
    if fence is not None:
        fence.check()


def refused() -> TurnFenced:
    """서버가 턴의 토큰 검사 쓰기를 거절했다(0 행, 스크립트의 0). 리스를 잃은 것이라 지금 문맥의
    펜스를 세우고(관리자가 턴을 멈춘다) 던질 예외를 돌려준다. 저장소가 `raise refused()` 로 쓴다."""
    fence = _CURRENT.get()
    if fence is not None:
        fence.lose()
    return TurnFenced(REFUSED_MESSAGE)


@contextmanager
def fenced(fence: Fence | None) -> Iterator[None]:
    """안의 일을 그 펜스의 문맥에서 돌린다. None 은 펜스 문맥 밖이다."""
    token = _CURRENT.set(fence)
    try:
        yield
    finally:
        _CURRENT.reset(token)


@asynccontextmanager
async def bounded() -> AsyncIterator[None]:
    """지금 문맥의 펜스로 안의 일을 묶는다(`Fence.bound`). 펜스 문맥 밖이면 그대로 돈다."""
    fence = _CURRENT.get()
    if fence is None:
        yield
        return
    async with fence.bound():
        yield


# --- Redis ---------------------------------------------------------------------------------


class _FencedSend:
    """redis.asyncio 연결 클래스에 섞는 펜스다. 연결을 열기 전과 명령을 쓰기 직전에 본다. 연결의
    핸드셰이크와 건강 확인(PING)은 턴의 쓰기가 아니라 펜스 밖에서 끝까지 돌고, 그 뒤에 펜스를 다시
    본다. 다시 연결하는 동안(장애 조치 때 수십 초) 펜스가 섰으면 명령은 쓰지 않고, 온전히 맺은
    연결은 풀에 남는다."""

    async def connect_check_health(
        self, check_health: bool = True, retry_socket_connect: bool = True
    ) -> None:
        check()
        with fenced(None):
            await super().connect_check_health(check_health, retry_socket_connect)  # type: ignore[misc]
        check()

    async def send_packed_command(self, command: Any, check_health: bool = True) -> None:
        check()
        if not self.is_connected:  # type: ignore[attr-defined]
            await self.connect_check_health(check_health=False)
        if check_health:
            with fenced(None):
                await self.check_health()  # type: ignore[attr-defined]
            check()
        await super().send_packed_command(command, check_health=False)  # type: ignore[misc]


@cache
def _fenced_class(base: type) -> type:
    return type(f"Fenced{base.__name__}", (_FencedSend, base), {})


def fenced_connection_class(base: type) -> type:
    """바탕 연결 클래스(`Connection`·`SSLConnection`·`UnixDomainSocketConnection`)의 펜스 하위
    클래스다. 바탕마다 하나이고, 이미 펜스가 있으면 그대로다."""
    return base if issubclass(base, _FencedSend) else _fenced_class(base)


def fence_redis_client(client: Any) -> Any:
    """첫 명령 전에 풀이 고른 연결 클래스를 그 펜스 하위 클래스로 바꾸고 클라이언트를 돌려준다."""
    pool = client.connection_pool
    pool.connection_class = fenced_connection_class(pool.connection_class)
    return client


# --- Postgres ------------------------------------------------------------------------------


class FencedCursor(AsyncCursor[Row]):
    """세션 풀의 커서다(`cursor_factory`). 보내기 전에 펜스를 보고, 펜스 문맥의 문장은 펜스
    마감까지만 돈다(`bounded`)."""

    async def execute(
        self,
        query: Any,
        params: Any = None,
        *,
        prepare: bool | None = None,
        binary: bool | None = None,
    ) -> FencedCursor[Row]:
        async with bounded():
            await super().execute(query, params, prepare=prepare, binary=binary)
        return self

    async def executemany(
        self, query: Any, params_seq: Iterable[Any], *, returning: bool = False
    ) -> None:
        async with bounded():
            await super().executemany(query, params_seq, returning=returning)

    async def stream(self, *args: Any, **kwargs: Any) -> AsyncIterator[Row]:
        check()
        rows = super().stream(*args, **kwargs)
        try:
            async for row in rows:
                yield row
        finally:
            await rows.aclose()

    @asynccontextmanager
    async def copy(self, *args: Any, **kwargs: Any) -> AsyncIterator[Any]:
        check()
        async with super().copy(*args, **kwargs) as copy:
            yield copy


class FencedPostgresSaver(AsyncPostgresSaver):
    """세션 풀의 체크포인터다. 파이프라인으로 쓰는 `aput`·`aput_writes`·`adelete_thread` 는 문장을
    큐에만 넣고 파이프라인을 닫을 때 보내므로, 그 커서 블록 전체를 펜스 마감 안에 둔다. 파이프라인이
    아닌 블록은 커서가 문장마다 묶는다. 블록째 묶으면 `alist` 처럼 블록 안에서 내주고 멈춘 생성기가
    제한 시간을 남겨, 리스를 잃을 때 상관없는 일이 취소된다."""

    @asynccontextmanager
    async def _cursor(self, *, pipeline: bool = False) -> AsyncIterator[Any]:
        if not pipeline:
            async with super()._cursor() as cur:
                yield cur
            return
        async with bounded(), super()._cursor(pipeline=True) as cur:
            yield cur


# --- 장기 기억 --------------------------------------------------------------------------------


class FencedStore(BaseStore):
    """턴의 `/memories` 백엔드에 넘기는 저장소다. 쓰기와 묶음(`put`·`aput`·`batch`·`abatch`,
    지우기 포함)은 큐에 넣기 전에 부른 쪽 문맥의 펜스를 본다. deepagents 의 백엔드는 지우기도 값
    없는 put 으로 묶음에 싣는다. 읽기는 감싼 저장소 그대로다."""

    def __init__(self, store: BaseStore) -> None:
        self._store = store
        self.supports_ttl = store.supports_ttl
        self.ttl_config = store.ttl_config

    def batch(self, ops: Iterable[Op]) -> list[Result]:
        check()
        return self._store.batch(ops)

    async def abatch(self, ops: Iterable[Op]) -> list[Result]:
        check()
        return await self._store.abatch(ops)

    def put(self, *args: Any, **kwargs: Any) -> None:
        check()
        self._store.put(*args, **kwargs)

    async def aput(self, *args: Any, **kwargs: Any) -> None:
        check()
        await self._store.aput(*args, **kwargs)

    def delete(self, *args: Any, **kwargs: Any) -> None:
        check()
        self._store.delete(*args, **kwargs)

    async def adelete(self, *args: Any, **kwargs: Any) -> None:
        check()
        await self._store.adelete(*args, **kwargs)

    def get(self, *args: Any, **kwargs: Any) -> Any:
        return self._store.get(*args, **kwargs)

    async def aget(self, *args: Any, **kwargs: Any) -> Any:
        return await self._store.aget(*args, **kwargs)

    def search(self, *args: Any, **kwargs: Any) -> Any:
        return self._store.search(*args, **kwargs)

    async def asearch(self, *args: Any, **kwargs: Any) -> Any:
        return await self._store.asearch(*args, **kwargs)

    def list_namespaces(self, *args: Any, **kwargs: Any) -> Any:
        return self._store.list_namespaces(*args, **kwargs)

    async def alist_namespaces(self, *args: Any, **kwargs: Any) -> Any:
        return await self._store.alist_namespaces(*args, **kwargs)
