"""동기 테스트가 Mongo 를 쓰는 비동기 코드를 한 번 돌린다.

비동기 Mongo 클라이언트는 그것을 쓰는 이벤트 루프 안에서 만들어야 하므로 돌릴 때마다 새로 열고
닫는다. 앱이 떠 있는 테스트에서 앱의 스토어를 쓸 때는 `client.portal.call` 로 앱의 루프에서 돈다.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Sequence
from typing import Any

from pymongo import AsyncMongoClient

from daegam.config import Settings


def run_db[R](settings: Settings, coro_fn: Callable[..., Awaitable[R]], *args: Any) -> R:
    """테스트 DB 로 `coro_fn(db, *args)` 를 한 번 돌린다."""

    async def go() -> R:
        client: AsyncMongoClient = AsyncMongoClient(settings.mongo_url)
        try:
            return await coro_fn(client[settings.mongo_db], *args)
        finally:
            await client.close()

    return asyncio.run(go())


def run_stores[R](
    settings: Settings,
    store_classes: Sequence[Callable[[Any], Any]],
    coro_fn: Callable[..., Awaitable[R]],
    *args: Any,
) -> R:
    """`store_classes` 를 저마다 `cls(db)` 로 만들어 스키마를 맞춘 뒤 `coro_fn(*stores, *args)` 를
    한 번 돌린다."""

    async def go(db) -> R:
        stores = [cls(db) for cls in store_classes]
        for store in stores:
            await store.init_schema()
        return await coro_fn(*stores, *args)

    return run_db(settings, go)


def run_store[R](
    settings: Settings,
    store_cls: Callable[[Any], Any],
    coro_fn: Callable[..., Awaitable[R]],
    *args: Any,
) -> R:
    """`store_cls(db)` 의 스키마를 맞춘 뒤 `coro_fn(store, *args)` 를 한 번 돌린다."""
    return run_stores(settings, (store_cls,), coro_fn, *args)
