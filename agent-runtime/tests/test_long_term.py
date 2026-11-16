"""장기 기억(D34, P17 F2·F5).

가장 중요한 둘을 든다. (1) `memory.long_term` 이 없으면 파일 백엔드가 예전과 글자 그대로 같다.
(2) 같은 사용자가 새 세션에서 앞 세션이 남긴 파일을 읽고 다른 사용자는 못 읽는다. 저장소는
InMemoryStore 이고 실제 컨테이너는 `DAEGAM_TEST_REDIS_URL`·`DAEGAM_TEST_PG_DSN` 이 있을 때만 돈다.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any

import psycopg
import pytest
from deepagents.backends import CompositeBackend, StateBackend, StoreBackend
from langgraph.store.memory import InMemoryStore

import runtime.dsn as dsn_module
import runtime.long_term as long_term_module
from runtime.compile import (
    MEMORY_INSTRUCTION,
    _subagent,
    _system_prompt,
    build_backend,
    compile_agent,
)
from runtime.config import ConfigError, parse_config
from runtime.long_term import (
    MEMORY_ROUTE,
    LongTermMemory,
    TurnContext,
    open_long_term,
    ttl_config,
    user_label,
)
from runtime.sessions import open_postgres_store
from runtime.stub import StubChatModel
from runtime.uploads import UPLOADS_ROUTE, UploadsBackend
from tests.app_fixtures import memory_app, memory_doc, read_message, sid_of, write_message
from tests.conftest import chat, events_named, load_fixture, needs_pg, needs_redis, wait_for
from tests.pg_fakes import FakePool
from tests.store_fixtures import memory_backend, open_pg_memory, pg_schema, real_memory

# --- 회귀: long_term 이 없으면 /memories/ 라우트가 없다 ------------------------------------


def test_backend_without_long_term_routes_only_uploads() -> None:
    """장기 기억이 꺼져 있으면 첨부(`/uploads/`, P36 9절) 말고는 세션 안 state 다."""
    backend = build_backend(None)
    assert isinstance(backend, CompositeBackend)
    assert type(backend.default) is StateBackend
    assert list(backend.routes) == [UPLOADS_ROUTE]
    assert isinstance(backend.routes[UPLOADS_ROUTE], UploadsBackend)


def test_backend_with_long_term_adds_the_memories_route() -> None:
    backend = build_backend(memory_backend())
    assert isinstance(backend, CompositeBackend)
    assert type(backend.default) is StateBackend
    assert sorted(backend.routes) == sorted([MEMORY_ROUTE, UPLOADS_ROUTE])
    assert isinstance(backend.routes[MEMORY_ROUTE], StoreBackend)
    # 압축이 흘려보내는 파일은 기본 백엔드로 간다
    assert backend.artifacts_root == "/"


def test_compile_without_long_term_keeps_prompt_and_single_graph() -> None:
    config = parse_config(load_fixture("support-agent.json"))
    agent = compile_agent(config, tools_by_ref={}, checkpointer=None)
    assert agent.ephemeral_graph is None
    assert agent.graph_for(ephemeral=True) is agent.graph
    # 문맥은 인용 장부 때문에 장기 기억과 상관없이 늘 넘긴다(P36 7절)
    assert agent.graph.context_schema is TurnContext


def test_compile_with_long_term_adds_prompt_and_ephemeral_graph() -> None:
    config = parse_config(memory_doc())
    agent = compile_agent(config, tools_by_ref={}, checkpointer=None, long_term=memory_backend())
    assert agent.ephemeral_graph is not None
    assert agent.graph_for(ephemeral=True) is agent.ephemeral_graph
    assert agent.graph_for(ephemeral=False) is agent.graph
    assert agent.graph.context_schema is agent.ephemeral_graph.context_schema is TurnContext


def test_memory_instruction_comes_before_the_language_instruction() -> None:
    main = parse_config(memory_doc()).main
    prompt = _system_prompt(main, long_term=True)
    assert MEMORY_INSTRUCTION in prompt
    assert prompt.index(MEMORY_INSTRUCTION) < prompt.index("Always respond in the language")
    # 꺼져 있으면 한 글자도 안 붙는다
    assert MEMORY_INSTRUCTION not in _system_prompt(main, long_term=False)


def test_subagent_gets_the_same_instruction() -> None:
    spec = parse_config(memory_doc()).subagents[0]
    sub = _subagent(
        spec,
        model=StubChatModel(),
        tools=[],
        backend=StateBackend(),
        compaction={
            "trigger": {"kind": "tokens", "value": 100},
            "keep": {"kind": "messages", "value": 2},
        },
        long_term=True,
    )
    assert sub["system_prompt"].endswith(MEMORY_INSTRUCTION)


# --- 네임스페이스 ---------------------------------------------------------------------------


def test_namespace_is_tenant_instance_and_hashed_owner() -> None:
    memory = memory_backend()
    namespace = memory.namespace(_runtime(TurnContext(owner_sub="auth0|user.name")))
    assert namespace == ("tenant-a", "inst-1", user_label("auth0|user.name"))
    # 점이 든 sub 를 그대로 쓰면 langgraph 가 거부한다. 그래서 sha256 hex 다
    assert "." not in namespace[2] and len(namespace[2]) == 64


def test_namespace_without_owner_is_refused() -> None:
    memory = memory_backend()
    for runtime in (_runtime(None), _runtime(TurnContext(owner_sub=""))):
        with pytest.raises(ValueError, match="세션 소유자"):
            memory.namespace(runtime)


# --- TTL 과 전용 스키마 ---------------------------------------------------------------------


def test_retention_days_become_minutes() -> None:
    ttl = ttl_config(30)
    assert ttl["default_ttl"] == 30 * 1440
    # 읽었다고 만료가 되살아나면 안 된다
    assert ttl["refresh_on_read"] is False
    assert ttl["omit_expired"] is True


def test_no_retention_means_no_ttl() -> None:
    assert ttl_config(None) is None


# --- 여는 자리의 거절 -------------------------------------------------------------------------


async def test_no_long_term_key_opens_nothing() -> None:
    """memory.long_term 이 없으면 저장소를 열지 않는다. memory 노드가 없는 문서(minimal)도 같다."""
    for name in ("support-agent.json", "minimal.json"):
        config = parse_config(load_fixture(name))
        assert await open_long_term(config, tenant="tenant-a") is None


@pytest.mark.parametrize(
    ("filesystem", "tenant", "code"),
    [
        (False, "tenant-a", "long_term_filesystem_disabled"),
        (True, None, "long_term_tenant_missing"),
        (True, "", "long_term_tenant_missing"),
        # langgraph 가 첫 라벨로 금지하는 이름이다. 첫 도구 호출에서 죽느니 기동에서 막는다
        (True, "langgraph", "long_term_tenant_reserved"),
    ],
)
async def test_open_long_term_refuses_at_startup(
    filesystem: bool, tenant: str | None, code: str
) -> None:
    doc = memory_doc()
    doc["main"]["builtin_tools"]["filesystem"] = filesystem
    with pytest.raises(ConfigError) as info:
        await open_long_term(parse_config(doc), tenant=tenant)
    assert info.value.code == code


async def test_missing_dsn_is_a_startup_error(monkeypatch) -> None:
    monkeypatch.delenv("DAEGAM_DB_0_URL", raising=False)
    monkeypatch.delenv("DAEGAM_DB_0_HOST", raising=False)
    with pytest.raises(ConfigError) as info:
        await open_long_term(parse_config(memory_doc()), tenant="tenant-a")
    assert info.value.code == "long_term_dsn_missing"


@pytest.mark.parametrize(
    ("rejects", "code"), [(False, "long_term_unreachable"), (True, "long_term_rejected")]
)
async def test_a_failed_schema_check_is_unreachable_or_rejected(
    monkeypatch, rejects: bool, code: str
) -> None:
    """스키마 확인이 연결 확인을 겸하므로 실패는 세션 저장소와 같은 둘로 갈린다(T22, T23)."""

    async def postgres_rejects(conninfo: str) -> bool:
        return rejects

    monkeypatch.setattr(dsn_module, "_postgres_rejects", postgres_rejects)
    with pytest.raises(ConfigError) as info:
        await long_term_module._check_schema(FakePool(failing=True), "DAEGAM_DB_0_URL")
    assert info.value.code == code
    assert "DAEGAM_DB_0_URL" in info.value.message and "secret" not in info.value.message


# --- 핵심: 세션을 건너 남고 사용자를 건너 안 보인다 -------------------------------------------


async def test_memory_survives_a_new_session_and_stays_with_its_owner() -> None:
    memory = memory_backend()
    async with memory_app(memory) as pod:
        # 임시 표시를 안 보낸 턴이다. 기본값이 '남긴다' 여야 한다
        response, events = await chat(
            pod, write_message("/memories/prefs.md", "커피는 아메리카노"), user_sub="alice"
        )
        assert response.status_code == 200
        first_session = sid_of(events, "done")

        # 새 세션이다. 체크포인트는 비어 있지만 /memories/ 는 남아 있어야 한다
        response, events = await chat(pod, read_message("/memories/prefs.md"), user_sub="alice")
        assert sid_of(events, "done") != first_session
        assert "커피는 아메리카노" in _tool_result(events)

        # /memories/ 밖은 지금처럼 스레드 상태에 산다. 세션이 바뀌면 사라진다
        await chat(pod, write_message("/scratch.md", "임시"), user_sub="alice")
        _, events = await chat(pod, read_message("/scratch.md"), user_sub="alice")
        assert "임시" not in _tool_result(events)

        # 다른 사용자는 같은 경로를 못 읽는다
        _, events = await chat(pod, read_message("/memories/prefs.md"), user_sub="bob")
        assert "커피는 아메리카노" not in _tool_result(events)

    # 저장소에는 소유자의 /memories/ 파일만 있다
    assert _files(memory, "alice") == {"/prefs.md"}
    assert _files(memory, "bob") == set()


async def test_memory_is_scoped_by_instance() -> None:
    """같은 Database 를 바인딩한 두 인스턴스가 서로를 못 본다(네임스페이스의 instance_id 다)."""
    store = InMemoryStore()
    first = LongTermMemory(store=store, tenant="tenant-a", instance_id="inst-1")
    second = LongTermMemory(store=store, tenant="tenant-a", instance_id="inst-2")
    async with memory_app(first) as pod:
        await chat(pod, write_message("/memories/note.md", "첫째 인스턴스"), user_sub="alice")
    async with memory_app(second) as pod:
        _, events = await chat(pod, read_message("/memories/note.md"), user_sub="alice")
        assert "첫째 인스턴스" not in _tool_result(events)


async def test_ephemeral_turn_leaves_nothing_in_the_store() -> None:
    """임시 표시가 있으면 그 턴은 저장소를 안 붙인다. 파일은 세션 안에서만 산다."""
    memory = memory_backend()
    async with memory_app(memory) as pod:
        response, events = await chat(
            pod,
            write_message("/memories/prefs.md", "임시로 쓴 것"),
            user_sub="alice",
            ephemeral=True,
        )
        assert response.status_code == 200
        session_id = sid_of(events, "done")
        # 같은 세션 안에서는 읽힌다(세션 상태에 있다)
        _, events = await chat(
            pod,
            read_message("/memories/prefs.md"),
            session_id=session_id,
            user_sub="alice",
            ephemeral=True,
        )
        assert "임시로 쓴 것" in _tool_result(events)
        # 임시가 아닌 새 세션에는 없다
        _, events = await chat(pod, read_message("/memories/prefs.md"), user_sub="alice")
        assert "임시로 쓴 것" not in _tool_result(events)
        # 감사에 그 턴이 임시였다는 것이 남는다
        starts = pod.observed("turn_start")
        assert [e.get("ephemeral") for e in starts] == [True, True, None]
    assert _files(memory, "alice") == set()


# --- 실제 저장소 -----------------------------------------------------------------------------
# 컨테이너를 다른 실행과 나눠 써도 되게 자기가 만든 것만 지운다. Postgres 는 이 테스트만의
# 스키마(pg_schema)에서, Redis 는 이 테스트만의 인스턴스 접두어에서 돈다.


@needs_pg
async def test_postgres_store_lives_in_its_own_schema(monkeypatch) -> None:
    """전용 스키마 안에 store 가 생기고 같은 이름의 남의 테이블은 안 건드린다(D34).

    고객 Database 의 기본 스키마(DSN 의 search_path)와 전용 스키마 둘 다 이 테스트의 것이다.
    """
    async with (
        pg_schema() as customer,
        pg_schema(create=False) as dedicated,
        await psycopg.AsyncConnection.connect(customer.dsn, autocommit=True) as conn,
    ):
        monkeypatch.setattr(long_term_module, "POSTGRES_SCHEMA", dedicated.name)
        # 고객의 업무 테이블을 흉내 낸다. 이름이 겹쳐도 손대면 안 된다
        await conn.execute("CREATE TABLE store (id int primary key, name text)")

        # 스키마가 없으면 기동 실패다. 런타임은 만들지 않는다
        doc = memory_doc()
        with pytest.raises(ConfigError) as info:
            await open_pg_memory(doc, customer.dsn)
        assert info.value.code == "long_term_schema_missing"

        await conn.execute(f"CREATE SCHEMA {dedicated.name}")
        memory = await open_pg_memory(doc, customer.dsn)
        try:
            await memory.store.aput(
                ("tenant-a", "inst-1", user_label("alice")), "prefs.md", {"content": "커피"}
            )
            item = await memory.store.aget(("tenant-a", "inst-1", user_label("alice")), "prefs.md")
            assert item is not None and item.value["content"] == "커피"
        finally:
            await memory.close()

        # IndexConfig 를 안 줬으므로 store_vectors 는 없다
        assert await dedicated.tables() == ["store", "store_migrations"]
        cur = await conn.execute(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_schema = %s AND table_name = 'store'",
            (customer.name,),
        )
        assert sorted(row[0] for row in await cur.fetchall()) == ["id", "name"]


@needs_pg
async def test_postgres_store_setup_survives_concurrent_pods(monkeypatch) -> None:
    """파드 셋이 동시에 기동해도 UniqueViolation·DeadlockDetected 로 죽지 않는다(P17 F5)."""
    async with pg_schema() as dedicated:
        monkeypatch.setattr(long_term_module, "POSTGRES_SCHEMA", dedicated.name)
        doc = memory_doc()
        opened = await asyncio.gather(*[open_pg_memory(doc) for _ in range(3)])
        for memory in opened:
            await memory.close()


@needs_pg
async def test_postgres_checkpointer_setup_survives_concurrent_pods() -> None:
    """체크포인터·세션 색인도 같은 재시도를 탄다. 물려받은 결함을 여기서 막는다(P17 F5)."""
    async with pg_schema() as empty:
        opened = await asyncio.gather(
            *[open_postgres_store(empty.dsn, instance_id="inst-1") for _ in range(3)]
        )
        for store in opened:
            await store.close()


@needs_redis
async def test_redis_store_keeps_its_prefix_under_the_instance() -> None:
    """인덱스와 키가 인스턴스 접두어 아래다. 안 주면 인스턴스끼리 섞인다(D34)."""
    async with real_memory("redis", memory_doc()) as memory:
        client = memory._client
        prefix = f"daegam-agent:{memory.instance_id}:store"
        namespace = (memory.tenant, memory.instance_id, user_label("alice"))
        await memory.store.aput(namespace, "prefs.md", {"content": "커피"})
        item = await memory.store.aget(namespace, "prefs.md")
        assert item is not None and item.value["content"] == "커피"
        assert await client.keys(f"{prefix}*")
        # 인덱스 이름이 접두어와 같다. store 하나면 인스턴스끼리 섞인다
        assert prefix in await client.execute_command("FT._LIST")


@needs_pg
async def test_a_started_runtime_deletes_expired_postgres_memories_in_the_background() -> None:
    """기동을 마친 배경 작업이 store 의 sweeper 가 첫 주기를 기다리기 전에 만료된 기억을 한 번
    지운다(P44 11절). 잠깐 깨었다 다시 잠드는 Agent 의 만료 기억도 지워진다. 만료되지 않은 행은
    남는다."""
    doc = memory_doc()
    async with real_memory("postgres", doc) as memory:
        namespace = memory.owner_namespace("alice")
        await memory.store.aput(namespace, "/old.md", {"content": "지난 일"})
        await memory.store.aput(namespace, "/new.md", {"content": "오늘 일"})
        prefix = ".".join(namespace)
        async with memory._pool.connection() as conn:
            await conn.execute(
                "UPDATE store SET expires_at = now() - interval '1 day'"
                " WHERE prefix = %s AND key = %s",
                (prefix, "/old.md"),
            )

        async def keys() -> list[str]:
            async with memory._pool.connection() as conn:
                cur = await conn.execute("SELECT key FROM store WHERE prefix = %s", (prefix,))
                return sorted(row["key"] for row in await cur.fetchall())

        assert await keys() == ["/new.md", "/old.md"]
        async with memory_app(memory, doc):

            async def swept() -> bool:
                return await keys() == ["/new.md"]

            await wait_for(swept, what="기동 뒤의 만료 정리")


@needs_redis
async def test_opening_redis_memory_sweeps_without_a_branch() -> None:
    """Redis 는 저장소가 스스로 만료를 지우므로 같은 정리가 아무것도 하지 않고 기동이 그대로다."""
    async with real_memory("redis", memory_doc()) as memory:
        assert await memory.store.sweep_ttl() == 0


async def test_a_failed_sweep_at_start_leaves_the_start_alone(caplog) -> None:
    """만료 정리가 실패해도 기동은 이어 간다. 다음 기동이나 store 의 sweeper 가 지운다."""

    async def failing() -> int:
        raise OSError("store blip")

    memory = LongTermMemory(
        store=SimpleNamespace(sweep_ttl=failing),  # type: ignore[arg-type]
        tenant="tenant-a",
        instance_id="inst-1",
    )
    await memory.sweep_expired()
    assert "store blip" in caplog.text
    # 정리가 없는 저장소(BaseStore)는 건너뛴다
    caplog.clear()
    await memory_backend().sweep_expired()
    assert caplog.text == ""


def _files(memory: LongTermMemory, owner_sub: str) -> set[str]:
    """그 사용자의 네임스페이스에 실제로 남은 파일 이름이다.

    list_namespaces 는 읽기만 해도 빈 네임스페이스가 생겨(InMemoryStore 가 defaultdict 다)
    "안 남았다" 를 못 든다.
    """
    namespace = memory.namespace(_runtime(TurnContext(owner_sub=owner_sub)))
    return {item.key for item in memory.store.search(namespace)}


def _runtime(context: TurnContext | None) -> Any:
    """ToolNode 가 넣어 주는 ToolRuntime 대신이다. 네임스페이스는 문맥의 소유자만 읽는다."""
    return SimpleNamespace(context=context)


def _tool_result(events: list[tuple[str, dict[str, Any]]]) -> str:
    return "".join(data.get("content") or "" for data in events_named(events, "tool_result"))
