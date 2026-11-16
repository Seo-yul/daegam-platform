"""MCP Server 스토어 (D42) 만의 것. 세 인스턴스 스토어가 함께 지키는 계약(draft 빼기, 조건부 저장,
저장 단위)은 test_instance_stores.py 가 본다.

정의 쪽 질의(`bound_to_data_service`·`using_backend_api`)는 draft 도 본다.
"""

from __future__ import annotations

from typing import Any

from daegam.store.mcp_servers import MCPServer, MongoMCPServerStore
from tests.helpers.aio import run_store
from tests.helpers.docs import ids, mcp_server_doc

SPEC = {
    "backends": [{"backend_profile_id": "bp-1", "spec_version": 1, "tools": ["Add"]}],
    "bindings": [{"data_service_id": "ds-1", "env_prefix": "DATABASE"}],
}


def _server(sid: str, **kw: Any) -> MCPServer:
    """Backend API bp-1 을 쓰고 Data Service ds-1 에 바인딩하는 MCP Server."""
    return mcp_server_doc(sid, spec=SPEC, **kw)


def test_init_schema_keeps_the_old_backend_index_name(settings, mongo):
    """인덱스 키가 `backend_profile_id` 로 남았으므로 이름도 `ix_backend_profile` 그대로다. 이름만
    바꾸면 그 인덱스가 이미 있는 배포본에서 Mongo 가 IndexOptionsConflict(85)로 거절해 기동이
    막힌다. 테스트는 실행마다 새 DB 를 쓰므로 이 자리를 지나간다 (D42)."""
    mongo["mcp_servers"].create_index(
        [("spec.backends.backend_profile_id", 1)], name="ix_backend_profile"
    )

    async def go(store):
        return set(await store.coll.index_information())

    names = run_store(settings, MongoMCPServerStore, go)
    assert "ix_backend_profile" in names
    assert "ix_backend_api" not in names


def test_definition_queries_include_draft(settings):
    async def go(store):
        await store.create(_server("draft-1"))
        await store.create(_server("dep-1", state="deployed"))
        return (
            await store.bound_to_data_service("ds-1"),
            await store.using_backend_api("bp-1"),
        )

    bound, using = run_store(settings, MongoMCPServerStore, go)
    assert ids(bound) == ["dep-1", "draft-1"]
    assert ids(using) == ["dep-1", "draft-1"]
