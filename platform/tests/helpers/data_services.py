"""Database(Data Service)를 API 로 만드는 테스트가 함께 쓰는 도우미.

`post_database` 는 만든 응답을 그대로 주므로 등록 규칙을 보는 테스트가 쓰고, `created_database` 는
만든 문서만 필요한 준비 단계가 쓴다. 본문은 kind 마다 정한 접속 필드(`DATABASE_DEFAULTS`)에 테스트가
준 필드를 덮은 것이다. `FakeRedis` 는 Redis verify 가 붙는 클라이언트의 자리다.
"""

from __future__ import annotations

from typing import Any

import httpx
from fastapi.testclient import TestClient
from redis.exceptions import ResponseError

from tests.helpers.consts import DS
from tests.helpers.tls import ca_pem

# 테스트가 등록하는 Redis TLS 끝점의 포트 (P61)
TLS_PORT = 6380

# kind 마다 등록 본문의 기본 접속 필드
DATABASE_DEFAULTS: dict[str, dict[str, Any]] = {
    "postgres": {
        "host": "pg.svc",
        "port": 5432,
        "database": "app",
        "username": "u",
        "password": "p",
    },
    "redis": {"host": "redis.svc", "port": 6379, "db": 0, "password": "p"},
}


def post_database(
    client: TestClient,
    name: str = "pg",
    kind: str = "postgres",
    *,
    headers: dict[str, str] | None = None,
    **fields: Any,
) -> httpx.Response:
    """Database name 을 만든 응답이다. fields 는 kind 의 기본 접속 필드(host·port·database 또는
    db·username·password)와 cluster_id 등을 덮고, 값이 None 인 필드는 본문에서 뺀다."""
    body = {"name": name, "kind": kind, **DATABASE_DEFAULTS[kind], **fields}
    body = {k: v for k, v in body.items() if v is not None}
    return client.post(DS, json=body, headers=headers)


def created_database(client: TestClient, *args: Any, **kw: Any) -> dict[str, Any]:
    """`post_database` 로 새로 만든 Database 문서. 201 이 아니면 그 자리에서 실패한다."""
    r = post_database(client, *args, **kw)
    assert r.status_code == 201, r.text
    return r.json()


def tls_database(client: TestClient, name: str = "cache", **fields: Any) -> dict[str, Any]:
    """TLS 끝점(`TLS_PORT`, CA 는 `ca_pem`)을 가진 Redis Database 를 새로 만든 문서. fields 는
    `post_database` 의 것과 같다."""
    return created_database(client, name, "redis", tls={"port": TLS_PORT, "ca": ca_pem()}, **fields)


class FakeRedis:
    """redis.asyncio.Redis 의 자리. 명령마다 정해진 값이나 예외를 돌려주고 호출을 기록한다.
    `commands` 에 없는 명령은 Redis 7 처럼 unknown command 다."""

    def __init__(self, *, ping: object = True, info: object = None, commands: dict | None = None):
        self._ping = ping
        self._info = {"redis_version": "8.2.1"} if info is None else info
        self._commands = commands or {}
        self.kwargs: dict = {}
        self.calls: list[tuple] = []
        self.closed = False

    def factory(self, **kwargs):
        self.kwargs = kwargs
        return self

    @staticmethod
    def _give(value):
        if isinstance(value, BaseException):
            raise value
        return value

    async def ping(self):
        self.calls.append(("PING",))
        return self._give(self._ping)

    async def info(self, section=None):
        self.calls.append(("INFO", section))
        return self._give(self._info)

    async def execute_command(self, *args):
        self.calls.append(tuple(args))
        key = " ".join(args)
        if key not in self._commands:
            raise ResponseError(f"ERR unknown command '{args[0]}', with args beginning with: ")
        return self._give(self._commands[key])

    async def aclose(self):
        self.closed = True
