"""테스트 대역이 이벤트 루프 위의 블로킹 호출을 거부하게 하는 검사 (P69, D81).

진짜 K8s 클라이언트와 Secret 저장소는 동기 I/O 라 async 코드는 `run_in_threadpool` 로 부른다.
대역은 바로 답하므로 루프 위에서 불러도 테스트가 지난다. 그래서 대역의 I/O 메서드가 이벤트 루프를
돌리는 스레드에서 불리면 거부한다. `run_in_threadpool` 의 일꾼 스레드에는 돌아가는 루프가 없어
지난다. async 테스트가 대역을 미리 채우는 자리는 `allow_on_loop()` 안에서 부른다.
"""

from __future__ import annotations

import asyncio
import contextlib
import functools
from collections.abc import Callable, Iterable, Iterator
from contextvars import ContextVar
from typing import Any

_allowed: ContextVar[bool] = ContextVar("daegam_loop_guard_allowed", default=False)


class BlockingCallOnLoop(AssertionError):
    """테스트 대역의 블로킹 메서드를 이벤트 루프 스레드에서 불렀다."""


def on_event_loop() -> bool:
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return False
    return True


def violation(name: str) -> None:
    """루프 위의 호출 하나. 거부한다. F0 측정은 이 함수를 바꿔 끼워 세기만 한다."""
    raise BlockingCallOnLoop(
        f"{name} was called on the event loop thread; call it through run_in_threadpool (P69, D81)"
    )


def check(name: str) -> None:
    if not _allowed.get() and on_event_loop():
        violation(name)


@contextlib.contextmanager
def allow_on_loop() -> Iterator[None]:
    """async 테스트가 대역을 직접 채우거나 들여다보는 동안만 검사를 끈다."""
    token = _allowed.set(True)
    try:
        yield
    finally:
        _allowed.reset(token)


def _guarded(method: Callable[..., Any], label: str) -> Callable[..., Any]:
    @functools.wraps(method)
    def guarded(self: Any, *args: Any, **kwargs: Any) -> Any:
        check(label)
        return method(self, *args, **kwargs)

    guarded._loop_guarded = True  # type: ignore[attr-defined]
    return guarded


def _wrap(cls: type, names: tuple[str, ...]) -> None:
    """cls 가 직접 정의한 메서드 가운데 names 에 든 것을 감싼다. 감싼 것은 다시 감싸지 않는다."""
    for name in names:
        method = cls.__dict__.get(name)
        if callable(method) and not getattr(method, "_loop_guarded", False):
            setattr(cls, name, _guarded(method, f"{cls.__name__}.{name}"))


def guard_methods(names: Iterable[str]) -> Callable[[type], type]:
    """클래스 데코레이터. 주어진 동기 메서드가 루프 스레드에서 불리면 거부한다. 하위 클래스가 그
    메서드를 다시 정의해도(테스트의 흉내 클라이언트) 만들 때 다시 감싼다."""
    guarded = tuple(names)

    def decorate(cls: type) -> type:
        _wrap(cls, guarded)
        parent_hook = cls.__init_subclass__

        def __init_subclass__(sub: type, **kwargs: Any) -> None:
            parent_hook(**kwargs)
            _wrap(sub, guarded)

        cls.__init_subclass__ = classmethod(__init_subclass__)  # type: ignore[assignment]
        return cls

    return decorate
