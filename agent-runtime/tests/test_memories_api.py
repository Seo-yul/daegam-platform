"""장기 기억 보기와 하나 지우기(P36 8절, runtime/memories_api.py)다.

에이전트가 파일 도구로 남긴 기억이 목록과 읽기에 그대로 나오는 것, 호출자 네임스페이스만 보는
것(승인자 헤더도 남의 기억을 열지 못한다), 하나 지우기가 같은 접두어의 다른 키를 지우지 않는 것,
경로 없는 `memory_deleted`, 장기 기억이 꺼진 에이전트의 404, 경로 검증, 1MiB 상한을 든다. 저장소는
InMemoryStore 이고, 만료된 파일이 빠지는 것은 TTL 이 있는 실제 Postgres·Redis 에서만 돈다.
"""

from __future__ import annotations

from typing import Any

import pytest

from runtime.hitl import APPROVER_HEADER
from runtime.long_term import LongTermMemory
from runtime.memories_api import MEMORY_FILE_MAX_BYTES
from tests.app_fixtures import BAD_KEYS, memory_app, memory_doc, write_message
from tests.conftest import REDIS_URL, RunningApp, chat, run_app, wait_for
from tests.store_fixtures import memory_backend, real_memory

ALICE = {"user_sub": "alice"}
BOB = {"user_sub": "bob"}


async def put(memory: LongTermMemory, owner: str, key: str, content: Any, **kwargs: Any) -> None:
    """에이전트의 StoreBackend 가 쓰는 모양(`FileData`)으로 저장소에 바로 둔다."""
    value = {"content": content, "encoding": "utf-8"}
    await memory.store.aput(memory.owner_namespace(owner), key, value, **kwargs)


async def listed(running: RunningApp, **params: Any) -> list[str]:
    response = await running.client.get("/memories", params=params or ALICE)
    assert response.status_code == 200, response.text
    return [row["path"] for row in response.json()["files"]]


async def test_files_the_agent_wrote_are_listed_and_read() -> None:
    """에이전트가 `/memories/` 에 쓴 파일이 그 경로로 목록에 오고 내용을 읽는다. 저장소 키와
    경로의 변환이 StoreBackend 와 맞는지 실제 도구 호출로 본다."""
    memory = memory_backend()
    async with memory_app(memory) as running:
        response, _ = await chat(running, write_message("/memories/prefs.md", "# 취향\n커피"))
        assert response.status_code == 200

        body = (await running.client.get("/memories", params=ALICE)).json()
        assert body["enabled"] is True and body["retention_days"] == 30
        [row] = body["files"]
        assert row["path"] == "/memories/prefs.md"
        assert row["size"] == len("# 취향\n커피".encode())
        assert row["created_at"] and row["modified_at"]

        response = await running.client.get("/memories/files/prefs.md", params=ALICE)
        assert response.status_code == 200, response.text
        assert response.json() == {
            "path": "/memories/prefs.md",
            "encoding": "utf-8",
            "content": "# 취향\n커피",
        }


async def test_callers_see_only_their_own_memories() -> None:
    """남의 파일은 없는 파일과 같다. 승인자 헤더도 남의 네임스페이스를 열지 못한다. 프록시 경로에
    user_sub 가 없으면 422 다."""
    memory = memory_backend()
    await put(memory, "alice", "/prefs.md", "앨리스의 기억")
    async with memory_app(memory) as running:
        approver = {APPROVER_HEADER: "1"}
        assert await listed(running, **BOB) == []
        response = await running.client.get(
            "/memories/files/prefs.md", params=BOB, headers=approver
        )
        assert response.status_code == 404 and response.json()["code"] == "memory_not_found"
        response = await running.client.delete(
            "/memories/files/prefs.md", params=BOB, headers=approver
        )
        assert response.status_code == 404
        assert await listed(running) == ["/memories/prefs.md"]

        for response in (
            await running.client.get("/memories"),
            await running.client.get("/memories/files/prefs.md"),
            await running.client.delete("/memories/files/prefs.md"),
        ):
            assert response.status_code == 422 and response.json()["code"] == "user_sub_required"
    assert await listed_keys(memory, "alice") == {"/prefs.md"}


async def test_deleting_one_key_keeps_the_keys_under_it() -> None:
    """StoreBackend 의 지우기는 그 키를 접두어로 삼는 파일까지 지운다. 하나 지우기는 키 하나만
    지운다. 관측 이벤트에는 사람만 있고 경로는 없다."""
    memory = memory_backend()
    for key in ("/notes", "/notes/today.md", "/notes.md"):
        await put(memory, "alice", key, key)
    async with memory_app(memory) as running:
        response = await running.client.delete("/memories/files/notes", params=ALICE)
        assert response.status_code == 200, response.text
        assert response.json() == {"path": "/memories/notes", "deleted": True}
        assert await listed(running) == ["/memories/notes.md", "/memories/notes/today.md"]

        response = await running.client.delete("/memories/files/notes", params=ALICE)
        assert response.status_code == 404 and response.json()["code"] == "memory_not_found"

        [event] = running.observed("memory_deleted")
        assert event["user_sub"] == "alice"
        assert "notes" not in str(event)


async def test_long_term_off_is_404(support_doc) -> None:
    """장기 기억이 꺼진 에이전트는 세 라우트가 모두 404 long_term_off 다."""
    async with run_app(support_doc) as running:
        for response in (
            await running.client.get("/memories", params=ALICE),
            await running.client.get("/memories/files/prefs.md", params=ALICE),
            await running.client.delete("/memories/files/prefs.md", params=ALICE),
        ):
            assert response.status_code == 404 and response.json()["code"] == "long_term_off"


@pytest.mark.parametrize("key", BAD_KEYS)
async def test_dot_segments_nul_and_long_keys_are_refused(key: str) -> None:
    memory = memory_backend()
    async with memory_app(memory) as running:
        for method in ("GET", "DELETE"):
            response = await running.client.request(method, f"/memories/files/{key}", params=ALICE)
            assert response.status_code == 422, response.text
            assert response.json()["code"] == "invalid_path"


async def test_a_file_over_1mib_is_listed_but_not_read() -> None:
    """1MiB 를 넘는 파일은 목록에 크기와 함께 오고 내용 읽기는 413 이다."""
    memory = memory_backend()
    await put(memory, "alice", "/big.md", "가" * (MEMORY_FILE_MAX_BYTES // 3 + 1))
    async with memory_app(memory) as running:
        body = (await running.client.get("/memories", params=ALICE)).json()
        sizes = {row["path"]: row["size"] for row in body["files"]}
        assert sizes["/memories/big.md"] > MEMORY_FILE_MAX_BYTES
        response = await running.client.get("/memories/files/big.md", params=ALICE)
        assert response.status_code == 413 and response.json()["code"] == "file_too_large"


# --- 실제 저장소 -----------------------------------------------------------------------------


def _redis_major() -> int:
    import redis

    return int(redis.Redis.from_url(REDIS_URL).info("server")["redis_version"].split(".", 1)[0])


@pytest.mark.parametrize("kind", ["postgres", "redis"])
async def test_real_store_lists_reads_deletes_and_omits_expired_files(kind: str) -> None:
    """실제 저장소에서 목록·읽기·하나 지우기가 같고, 만료된 파일은 sweep 전이라도 목록과 읽기에
    없다(`omit_expired`, Redis 는 키 만료)."""
    if kind == "redis" and REDIS_URL and _redis_major() < 8:
        # langgraph 의 Redis 저장소 검색은 Redis 8 의 검색 답(JSON 문서의 json 칸)을 읽는다. CI 와
        # 배포는 Redis 8 이고, 로컬의 Redis Stack 7.4 는 그 칸 없이 답한다
        pytest.skip("Redis 8 이 필요하다: 이 Redis 의 검색은 JSON 문서를 json 칸 없이 답한다")
    doc = memory_doc()
    async with real_memory(kind, doc) as memory:
        await put(memory, "alice", "/notes", "상위")
        await put(memory, "alice", "/notes/today.md", "오늘")
        await put(memory, "bob", "/notes", "밥의 것")
        await put(memory, "alice", "/stale.md", "곧 만료", ttl=1.5 / 60)
        async with memory_app(memory, doc) as running:

            async def expired() -> bool:
                return "/memories/stale.md" not in await listed(running)

            await wait_for(expired, what="만료된 기억 파일이 목록에서 빠진 것")
            assert await listed(running) == ["/memories/notes", "/memories/notes/today.md"]
            response = await running.client.get("/memories/files/stale.md", params=ALICE)
            assert response.status_code == 404
            response = await running.client.get("/memories/files/notes/today.md", params=ALICE)
            assert response.json()["content"] == "오늘"

            response = await running.client.delete("/memories/files/notes", params=ALICE)
            assert response.status_code == 200, response.text
            assert await listed(running) == ["/memories/notes/today.md"]
            assert await listed(running, **BOB) == ["/memories/notes"]


async def listed_keys(memory: LongTermMemory, owner: str) -> set[str]:
    items = await memory.store.asearch(memory.owner_namespace(owner), limit=100)
    return {item.key for item in items}
