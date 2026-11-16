"""Redis 세션 저장소 테스트(test_redis_store.py)의 가짜다(D33 F2).

실제 서버로 만들기 어려운 자리만 가짜로 둔다. setup 에 실패하는 체크포인터, 한 번에 다 지우지 못하는
체크포인터, open_redis_store 가 만드는 클라이언트와 saver 다. `fake_redis_client` 는 conftest 가
pytest_plugins 로 싣는 픽스처다.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from types import SimpleNamespace
from typing import Any

import pytest
from redis.asyncio.connection import Connection
from redis.exceptions import ResponseError

# ---- 체크포인터 setup 과 스레드 지우기 -----------------------------------------------------------


class CountingIndex:
    """CountQuery 의 개수만 답하는 redisvl 인덱스다. 받은 질의를 남긴다."""

    def __init__(self, saver: ChunkedSaver, name: str) -> None:
        self.saver = saver
        self.name = name
        self.queries: list[str] = []

    async def query(self, query: Any) -> int:
        self.queries.append(query.query_string())
        return self.saver.left if self.name == "cp" else 0


class ChunkedSaver:
    """한 번에 per_call 개까지만 지우는 체크포인터다. 남은 수는 체크포인트 인덱스가 센다."""

    def __init__(self, documents: int, per_call: int) -> None:
        self.left = documents
        self.per_call = per_call
        self.calls = 0
        self.checkpoints_index = CountingIndex(self, "cp")
        self.checkpoint_writes_index = CountingIndex(self, "wr")

    async def adelete_thread(self, thread_id: str) -> None:
        self.calls += 1
        self.left = max(0, self.left - self.per_call)


class FakeSaver:
    """asetup 이 미리 정한 예외를 차례로 내고 다 떨어지면 성공한다. 부른 수를 calls 에 센다."""

    def __init__(self, *errors: BaseException) -> None:
        self.errors = list(errors)
        self.calls = 0

    async def asetup(self) -> None:
        self.calls += 1
        if self.errors:
            raise self.errors.pop(0)


class WrappedError(Exception):
    """redisvl 이 RedisError 를 감싸는 모양이다."""


def wrapped(inner: BaseException) -> WrappedError:
    try:
        raise inner
    except BaseException as e:
        outer = WrappedError(f"wrapped: {e}")
        outer.__cause__ = e
        return outer


def index_exists() -> WrappedError:
    return wrapped(ResponseError("Index already exists"))


# ---- open_redis_store 의 배선 (가짜 클라이언트) --------------------------------------------------

Call = tuple[str, tuple[Any, ...], dict[str, Any]]


def cmd(name: str, *args: Any, **kwargs: Any) -> Call:
    return (name, args, kwargs)


class FakeRedis:
    """open_redis_store 가 만드는 클라이언트 자리에 두는 가짜다. 만든 인자를 made 에, 부른 것을
    (이름, 인자, 키워드)로 calls 에 남기고 responses 의 값으로 답한다. 값이 callable 이면 인자로
    불러 답하므로 예외를 내게 할 수도 있다."""

    def __init__(self) -> None:
        self.made: list[tuple[str, Any]] = []
        self.calls: list[Call] = []
        self.responses: dict[str, Any] = {"ping": True}
        # 연결 클래스를 펜스 하위 클래스로 바꾸는 자리(fence.py)다
        self.connection_pool = SimpleNamespace(connection_class=Connection)

    def register_script(self, script: str) -> None:
        self.calls.append(cmd("register_script", script))

    def __getattr__(self, name: str) -> Callable[..., Awaitable[Any]]:
        if name.startswith("_"):
            raise AttributeError(name)

        async def call(*args: Any, **kwargs: Any) -> Any:
            self.calls.append(cmd(name, *args, **kwargs))
            value = self.responses.get(name)
            return value(*args, **kwargs) if callable(value) else value

        return call


class FakeSaverClass(FakeSaver):
    """monkeypatch 로 ChainRedisSaver(runtime/redis_saver.py) 자리에 두는 FakeSaver 다. 만든
    인자를 kwargs 에 남긴다.
    open_redis_store 가 만들므로 예외는 만들기 전에 클래스의 errors 에 넣고, 인스턴스는 그 목록을
    그대로 꺼내 쓴다."""

    instances: list[FakeSaverClass] = []
    errors: list[BaseException] = []

    def __init__(self, **kwargs: Any) -> None:
        super().__init__()
        self.errors = FakeSaverClass.errors
        self.kwargs = kwargs
        FakeSaverClass.instances.append(self)


@pytest.fixture
def fake_redis_client(monkeypatch: pytest.MonkeyPatch) -> FakeRedis:
    """open_redis_store 가 만드는 클라이언트 둘과 saver 를 가짜로 바꾼다. 두 클라이언트는 같은
    가짜다."""
    fake = FakeRedis()

    class FakeRedisClass:
        def __new__(cls, **kwargs: Any) -> FakeRedis:
            fake.made.append(("args", kwargs))
            return fake

        @classmethod
        def from_url(cls, url: str, **kwargs: Any) -> FakeRedis:
            fake.made.append(("url", (url, kwargs)))
            return fake

    monkeypatch.setattr("redis.asyncio.Redis", FakeRedisClass)
    monkeypatch.setattr("runtime.redis_saver.ChainRedisSaver", FakeSaverClass)
    FakeSaverClass.instances.clear()
    FakeSaverClass.errors.clear()
    return fake
