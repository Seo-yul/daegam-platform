"""Redis 체크포인터의 델타 채널 이력 읽기다(P60, runtime/redis_saver.py).

기반 걷기(`BaseCheckpointSaver.aget_delta_channel_history`)와 같은 답을 같은 차례로 내는지 실제
Redis 8 의 네 모양 세션에서 본다. 30턴 세션(files 는 한 번도 쓰이지 않는다), 첫 턴에 파일을 쓰고
길어진 세션(files 의 창이 루트까지다), 다시 생성으로 갈라지고 파일을 쓴 세션, 서브에이전트가
`tools:` 네임스페이스에 남긴 턴이다. 쪽과 묶음이 작아도 같은지, 읽는 체크포인트가 없거나 사슬
가운데가 지워져도 같은지도 본다. `graph.aget_state` 한 번의 왕복이 10턴과 30턴에서 같은지 세어,
langgraph 가 이 메서드를 부르지 않게 바뀌면 실패하게 한다. 손으로 지은 사슬에서는 metadata 의
superstep 수가 맞을 때, 짧거나 길 때, 없을 때, step 이 건너뛸 때를 모두 본다. 상류가 이 메서드를
갖게 되거나 기반 메서드의 꼴이 바뀌면 실패하는 지킴 테스트는 Redis 없이 돈다. 실제 Redis 는
`DAEGAM_TEST_REDIS_URL` 이 없으면 건너뛴다.
"""

from __future__ import annotations

import inspect
from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager, contextmanager
from typing import Any, get_type_hints

import pytest
from langgraph.checkpoint.base import BaseCheckpointSaver, DeltaChannelHistory, empty_checkpoint
from langgraph.checkpoint.base.id import uuid6
from langgraph.checkpoint.redis.aio import AsyncRedisSaver
from langgraph.checkpoint.redis.util import from_storage_safe_str, to_storage_safe_id
from redisvl.query.filter import Tag

from runtime import redis_saver
from runtime.redis_saver import ChainRedisSaver, _ancestors, _depths, _object_keys
from runtime.sessions import SessionStore
from runtime.stream import thread_config
from tests.app_fixtures import regenerate, sid_of, tool_message, with_subagent, write_message
from tests.conftest import RunningApp, chat, needs_redis, run_app
from tests.store_fixtures import store_for

CHANNELS = ["messages", "files"]
"""deepagents 가 DeltaChannel 로 두는 채널이다. 상태 읽기가 이 둘을 함께 묻는다."""

# ---- 지킴 (Redis 없이) ------------------------------------------------------------------------


def test_the_upstream_saver_still_lacks_the_history_read() -> None:
    """상류 저장기가 이 메서드를 갖게 되면 실패한다. 그 판으로 잠금을 올리는 커밋이
    runtime/redis_saver.py 를 지우고 open_redis_store 를 AsyncRedisSaver 로 되돌린다."""
    upstream = AsyncRedisSaver.aget_delta_channel_history
    assert upstream is BaseCheckpointSaver.aget_delta_channel_history, (
        "상류 저장기가 aget_delta_channel_history 를 갖게 됐다. ChainRedisSaver 를 지운다"
    )


def test_the_override_keeps_the_base_signature_and_return_shape() -> None:
    """기반 메서드는 상류에서 Beta 라 꼴이 바뀔 수 있다. 인자와 돌려주는 칸이 바뀌면 실패한다."""
    ours = inspect.signature(ChainRedisSaver.aget_delta_channel_history)
    base = inspect.signature(BaseCheckpointSaver.aget_delta_channel_history)
    assert list(ours.parameters.items()) == list(base.parameters.items())
    assert set(get_type_hints(DeltaChannelHistory)) == {"writes", "seed"}


def test_the_chain_follows_parents_and_refuses_broken_ones() -> None:
    """사슬은 부모를 따라 루트까지거나 limit 개까지다. 갈라진 갈래의 형제는 들지 않는다. 부모가
    목록 밖이거나 되돌아오면 None 이라 다른 길로 돌아간다."""
    parents = {"r": None, "a": "r", "b": "a", "b2": "a", "c": "b"}
    assert _ancestors("c", "b", parents) == (["b", "a", "r"], True)
    assert _ancestors("b2", "a", parents) == (["a", "r"], True)
    assert _ancestors("r", None, parents) == ([], True)
    assert _ancestors("c", "b", parents, 2) == (["b", "a"], False)
    assert _ancestors("c", "b", parents, 3) == (["b", "a", "r"], True)
    assert _ancestors("c", "gone", parents) is None
    assert _ancestors("c", "b", {"b": "c"}) is None
    assert _ancestors("c", "b", {"b": "a", "a": "b"}) is None


def test_depths_read_the_superstep_counts_from_the_stored_metadata() -> None:
    """저장기는 metadata 를 JSON 문자열로 둔다. 모양이 다르거나 0인 수는 버린다."""
    stored = '{"step": 9, "counters_since_delta_snapshot": {"messages": [3, 7], "files": [0, 10]}}'
    assert _depths(stored) == {"messages": 7, "files": 10}
    assert _depths({"counters_since_delta_snapshot": {"messages": [1, 2]}}) == {"messages": 2}
    assert _depths({"counters_since_delta_snapshot": {"a": [1, 0], "b": [1], "c": "x"}}) == {}
    assert _depths('{"step": 1}') == {}
    assert _depths("not json") == {}
    assert _depths(None) == {}


def test_object_keys_reads_the_json_path_reply() -> None:
    """channel_values 가 객체가 아니면 이름이 없다. 키가 없거나 읽기가 실패하면 사슬이 끊겼다."""
    assert _object_keys([["messages", "files"]]) == {"messages", "files"}
    assert _object_keys([None]) == set()
    assert _object_keys([]) == set()
    assert _object_keys(None) is None
    assert _object_keys(ValueError("no such key")) is None


# ---- 실제 Redis --------------------------------------------------------------------------------


@contextmanager
def round_trips(client: Any) -> Iterator[list[int]]:
    """client 로 서버에 다녀온 횟수다. 명령 하나와 파이프라인 실행 하나가 각각 한 번이다."""
    count = [0]
    send, pipeline = client.execute_command, client.pipeline

    async def counted_send(*args: Any, **kwargs: Any) -> Any:
        count[0] += 1
        return await send(*args, **kwargs)

    def counted_pipeline(*args: Any, **kwargs: Any) -> Any:
        pipe = pipeline(*args, **kwargs)
        run = pipe.execute

        async def execute(*a: Any, **k: Any) -> Any:
            count[0] += 1
            return await run(*a, **k)

        pipe.execute = execute
        return pipe

    client.execute_command, client.pipeline = counted_send, counted_pipeline
    try:
        yield count
    finally:
        del client.execute_command
        del client.pipeline


def config_of(thread_id: str, ns: str, checkpoint_id: str) -> dict[str, Any]:
    return {
        "configurable": {
            "thread_id": thread_id,
            "checkpoint_ns": ns,
            "checkpoint_id": checkpoint_id,
        }
    }


async def checkpoints(saver: ChainRedisSaver, thread_id: str) -> list[tuple[str, str]]:
    """스레드의 모든 네임스페이스에 있는 (네임스페이스, 체크포인트 id) 다. 시각 순이다."""
    docs = await saver._search_all(
        saver.checkpoints_index,
        Tag("thread_id") == to_storage_safe_id(thread_id),
        ["checkpoint_ns", "checkpoint_id"],
        sort="checkpoint_ts",
    )
    return [(from_storage_safe_str(doc.checkpoint_ns), doc.checkpoint_id) for doc in docs]


async def assert_same_as_base(saver: ChainRedisSaver, thread_id: str, ns: str, cid: str) -> None:
    config = config_of(thread_id, ns, cid)
    ours = await saver.aget_delta_channel_history(config=config, channels=CHANNELS)
    base = await BaseCheckpointSaver.aget_delta_channel_history(
        saver, config=config, channels=CHANNELS
    )
    assert ours == base, (thread_id, ns, cid)


@asynccontextmanager
async def redis_app(doc: dict[str, Any]) -> AsyncIterator[tuple[RunningApp, SessionStore]]:
    """Redis 저장소로 뜬 앱이다. 긴 세션을 한 사용자가 몰아 보내므로 분당 한도를 넉넉히 둔다."""
    doc["entry"]["rate_limit"] = {"per_user_per_minute": 1000}
    async with store_for("redis", doc) as store, run_app(doc, store=store) as running:
        assert isinstance(store.checkpointer, ChainRedisSaver)
        yield running, store


async def assert_wrote_notes(running: RunningApp, *sids: str) -> None:
    """세션들의 상태에 /notes.md 가 있다. 파일 도구가 숨겨져 쓰기가 실패해도 기반 걷기와의 비교는
    같게 나오므로 따로 본다. 파일 도구는 files_doc 의 모델 이름으로 켠다(conftest 의 files_doc)."""
    for sid in sids:
        snapshot = await running.runtime.agent.graph.aget_state(thread_config(sid))
        assert "/notes.md" in (snapshot.values.get("files") or {}), sid


async def turns(running: RunningApp, messages: list[str], sid: str | None = None) -> str:
    for message in messages:
        response, sse = await chat(running, message, session_id=sid)
        assert response.status_code == 200, response.text
        sid = sid_of(sse, "done")
    assert sid is not None
    return sid


@needs_redis
async def test_the_chain_read_matches_the_base_walk_on_four_session_shapes(
    files_doc, monkeypatch
) -> None:
    """30턴 세션, 첫 턴에 파일을 쓴 16턴 세션, 다시 생성으로 갈라지고 파일을 쓴 세션, 서브에이전트
    턴에서 체크포인트마다 기반 걷기와 같은 답이다. 작은 쪽과 묶음은 검색의 쪽 넘기기와 파이프라인
    나누기를 지나게 한다. PAGE 와 CHUNK 는 이 저장소의 읽기만 보고 기반 걷기는 보지 않으므로, 세션과
    기반 걷기의 답은 한 번 만들고 두 설정의 읽기를 그 답과 견준다."""
    with_subagent(files_doc)
    add = tool_message("Add", a=1, b=2)
    async with redis_app(files_doc) as (running, store):
        saver = store.checkpointer
        long_sid = await turns(running, [f"{n}번째 질문" for n in range(30)])
        filed = await turns(
            running, [write_message("/notes.md", "메모")] + [f"{n}번째 질문" for n in range(15)]
        )
        branched = await turns(running, ["첫 질문", write_message("/notes.md", "메모"), add])
        for _ in range(2):
            response, _sse = await regenerate(running, branched)
            assert response.status_code == 200, response.text
        task = tool_message("task", description=add, subagent_type="calc")
        delegated = await turns(running, ["앞 질문", task])
        try:
            await assert_wrote_notes(running, filed, branched)
            configs: list[dict[str, Any]] = []
            for sid in (branched, delegated):
                found = await checkpoints(saver, sid)
                assert any(ns.startswith("tools:") for ns, _ in found) == (sid == delegated)
                configs += [config_of(sid, ns, cid) for ns, cid in found]
            for sid, every in ((long_sid, 10), (filed, 5)):
                found = await checkpoints(saver, sid)
                configs += [config_of(sid, ns, cid) for ns, cid in found[::every] + found[-3:]]
            base = [
                await BaseCheckpointSaver.aget_delta_channel_history(
                    saver, config=config, channels=CHANNELS
                )
                for config in configs
            ]
            for page, chunk in ((1000, 500), (4, 3)):
                monkeypatch.setattr(redis_saver, "PAGE", page)
                monkeypatch.setattr(redis_saver, "CHUNK", chunk)
                for config, expected in zip(configs, base, strict=True):
                    ours = await saver.aget_delta_channel_history(config=config, channels=CHANNELS)
                    assert ours == expected, (page, config["configurable"])
        finally:
            for sid in (long_sid, filed, branched, delegated):
                await store.delete_session(sid)


@needs_redis
async def test_a_state_read_makes_the_same_round_trips_at_10_and_30_turns(
    files_doc, monkeypatch
) -> None:
    """`graph.aget_state` 한 번의 왕복을 10턴과 30턴에서 센다. 파일을 쓰지 않은 세션과 첫 턴에
    파일을 쓴 세션이다. langgraph 가 이 메서드를 부르지 않게 바뀌면 부른 수가 0이 되거나 기반
    걷기처럼 왕복이 사슬만큼 늘어 실패한다."""
    async with redis_app(files_doc) as (running, store):
        saver = store.checkpointer
        graph = running.runtime.agent.graph
        history = saver.aget_delta_channel_history
        calls: list[int] = []

        async def counted(**kwargs: Any) -> Any:
            calls.append(1)
            return await history(**kwargs)

        monkeypatch.setattr(saver, "aget_delta_channel_history", counted)
        plain = await turns(running, [f"{n}번째" for n in range(10)])
        filed = await turns(
            running, [write_message("/notes.md", "메모")] + [f"{n}번째" for n in range(9)]
        )
        try:
            await assert_wrote_notes(running, filed)
            counts: dict[str, list[tuple[int, int, int]]] = {plain: [], filed: []}
            for more in (0, 20):
                for sid in (plain, filed):
                    await turns(running, [f"더 {n}" for n in range(more)], sid)
                    calls.clear()
                    with round_trips(saver._redis) as state:
                        await graph.aget_state(thread_config(sid))
                    assert calls == [1], sid
                    ns, cid = (await checkpoints(saver, sid))[-1]
                    config = config_of(sid, ns, cid)
                    with round_trips(saver._redis) as ours:
                        await history(config=config, channels=CHANNELS)
                    with round_trips(saver._redis) as base:
                        await BaseCheckpointSaver.aget_delta_channel_history(
                            saver, config=config, channels=CHANNELS
                        )
                    counts[sid].append((state[0], ours[0], base[0]))
            # 파일을 쓰지 않은 세션은 읽는 체크포인트, files 의 쓰기 수, 창, 씨앗과 쓰기 등록, 쓰기
            # 채널, 쓰기 문서다. 파일을 쓴 세션은 files 에 버전이 있어 쓰기 수를 세지 않는다
            for sid, bound in ((plain, 6), (filed, 5)):
                (state_10, ours_10, base_10), (state_30, ours_30, base_30) = counts[sid]
                assert state_30 == state_10 and ours_30 == ours_10 <= bound, counts
                assert base_30 > base_10 > ours_10, counts
        finally:
            for sid in (plain, filed):
                await store.delete_session(sid)


@needs_redis
async def test_a_missing_target_or_a_deleted_ancestor_answers_like_the_base_walk(
    support_doc,
) -> None:
    """없는 체크포인트를 읽으면 채널마다 쓰기가 빈 답이다. 사슬 가운데 체크포인트가 지워졌으면
    사슬이 끊겨 기반 걷기로 돌아가므로, 기반 걷기가 거기서 멈춘 답과 같다."""
    async with redis_app(support_doc) as (running, store):
        saver = store.checkpointer
        sid = await turns(running, [f"{n}번째" for n in range(4)])
        try:
            found = await checkpoints(saver, sid)
            missing = config_of(sid, "", str(uuid6()))
            ours = await saver.aget_delta_channel_history(config=missing, channels=CHANNELS)
            assert ours == {ch: {"writes": []} for ch in CHANNELS}
            await assert_same_as_base(saver, sid, "", str(uuid6()))
            ns, last = found[-1]
            middle = found[len(found) // 2][1]
            await saver._redis.delete(saver._make_redis_checkpoint_key(sid, ns, middle))
            config = config_of(sid, ns, last)
            ours = await saver.aget_delta_channel_history(config=config, channels=CHANNELS)
            base = await BaseCheckpointSaver.aget_delta_channel_history(
                saver, config=config, channels=CHANNELS
            )
            assert ours == base and ours["messages"]["writes"], ours
        finally:
            await store.delete_session(sid)


SYNTHETIC = ["messages", "files", "notes", "todos"]
"""손으로 지은 사슬의 채널이다. messages 는 체크포인트마다 쓰이고 150에 씨앗이 있다. files 는 5에
한 번 쓰이고 씨앗이 없다. notes 는 버전이 없는데 100에 쓰기가 남았다(갈래가 같은 조상에서 다시 돈
흔적). todos 는 버전도 쓰기도 없다."""


def synthetic_counters(step: int, mode: str) -> dict[str, list[int]] | None:
    """step 의 체크포인트 metadata 에 둘 superstep 수다. exact 는 langgraph 가 적는 값이다."""
    if mode == "none":
        return None
    since_start = step + 1
    counters = {ch: [0, since_start] for ch in ("files", "notes", "todos")}
    if step != 150:
        exact = step - 150 if step > 150 else since_start
        messages = {"exact": exact, "gap": exact, "short": exact // 2, "long": exact + 30}[mode]
        counters["messages"] = [1, messages]
    return counters


@needs_redis
@pytest.mark.parametrize("mode", ["exact", "long", "short", "none", "gap"])
async def test_a_hand_built_chain_matches_the_base_walk_whatever_the_counters_say(
    support_doc, mode
) -> None:
    """200개 체크포인트 사슬에서 metadata 의 superstep 수가 맞으면 그 창만 읽고, 길면 창 안의 가까운
    씨앗을 찾고, 짧거나 없거나 step 이 건너뛰면 사슬을 모두 읽는다. 어느 쪽이든 기반 걷기와 같다."""
    async with redis_app(support_doc) as (_running, store):
        saver = store.checkpointer
        thread_id = f"chain-{mode}-{uuid6()}"
        config: dict[str, Any] = {"configurable": {"thread_id": thread_id, "checkpoint_ns": ""}}
        ids: list[str] = []
        for step in range(200):
            checkpoint = empty_checkpoint()
            checkpoint["id"] = str(uuid6())
            checkpoint["channel_versions"] = {
                ch: str(step) for ch, since in (("messages", 1), ("files", 6)) if step >= since
            }
            if step == 150:
                checkpoint["channel_values"] = {"messages": ["씨앗"]}
            metadata: dict[str, Any] = {
                "source": "loop",
                "step": step * 2 if mode == "gap" else step,
                "parents": {},
            }
            if counters := synthetic_counters(step, mode):
                metadata["counters_since_delta_snapshot"] = counters
            config = await saver.aput(config, checkpoint, metadata, {})
            ids.append(checkpoint["id"])
            writes: list[tuple[str, Any]] = [("messages", f"m{step}")]
            writes += [("files", {"a.md": step})] if step == 5 else []
            await saver.aput_writes(config, writes, task_id=f"task-{step % 3}")
            if step == 100:
                await saver.aput_writes(config, [("notes", "다시")], task_id="rerun")
        try:
            for target in (ids[199], ids[151], ids[120]):
                with round_trips(saver._redis) as count:
                    ours = await saver.aget_delta_channel_history(
                        config=config_of(thread_id, "", target), channels=SYNTHETIC
                    )
                base = await BaseCheckpointSaver.aget_delta_channel_history(
                    saver, config=config_of(thread_id, "", target), channels=SYNTHETIC
                )
                assert ours == base, target
                if target == ids[199]:
                    assert ours["messages"]["seed"] == ["씨앗"]
                    assert [w[2] for w in ours["messages"]["writes"]] == [
                        f"m{step}" for step in range(150, 199)
                    ]
                    assert ours["files"] == {"writes": [("task-2", "files", {"a.md": 5})]}
                    assert ours["notes"] == {"writes": [("rerun", "notes", "다시")]}
                    assert ours["todos"] == {"writes": []}
                    if mode == "exact":
                        # 읽는 체크포인트, notes·todos 의 쓰기 수, 창, 씨앗과 쓰기 등록,
                        # 쓰기 채널, 쓰기 문서다
                        assert count[0] == 7, count
        finally:
            await store.delete_checkpoints(thread_id)
