"""Redis 체크포인터가 델타 채널 이력을 정해진 수의 왕복으로 읽게 한다(P60, D72).

deepagents 는 messages·files 를 DeltaChannel 로 둔다. 상태를 읽으면 그 채널을 가장 가까운 스냅샷에서
다시 짓는데, 기반 클래스의 `aget_delta_channel_history` 는 루트 사슬의 조상마다 `aget_tuple` 을 한
번씩 부른다. InMemory·Postgres 저장기는 이것을 덮어쓰고 잠근 Redis 저장기(langgraph-checkpoint-redis
0.5.2)는 덮어쓰지 않는다. 그래서 Redis 세션은 길어질수록 상태 한 번 읽기가 그만큼 더 왕복한다. 한
번도 쓰이지 않은 채널(파일을 쓰지 않은 세션의 files)은 스냅샷이 없어 루트까지 걷는다.

`ChainRedisSaver` 는 같은 답을 사슬 길이와 상관없는 수의 왕복으로 낸다.

1. 읽는 체크포인트의 부모, step, metadata, channel_versions 의 이름을 한 파이프라인으로 읽는다.
2. 버전이 없는 채널은 사슬 어디에도 씨앗이 없다. channel_values 에는 버전이 있는 채널만 실리고,
   버전은 부모에서 자식으로 옮겨질 뿐 지워지지 않기 때문이다. 이런 채널은 스레드의 쓰기 수를 세어
   0이면 걷지 않는다.
3. metadata 의 `counters_since_delta_snapshot` 은 채널마다 마지막 스냅샷 뒤의 superstep 수다. 그
   깊이의 step 범위에서 체크포인트 id 와 부모 id 를 검색으로 읽어 조상 사슬을 메모리에서 풀고,
   채널마다 그 창 안의 channel_values 값을 JSON.MGET 으로 읽어 가장 가까운 씨앗과 그 값을 함께
   얻는다. 수는 어림일 뿐이라 창 안의 체크포인트를 모두 본다. 다시 생성으로 갈라진 다른 갈래는
   사슬에 들지 않는다. 파일을 50번 넘게 쓰지 않은 세션의 files 는 스냅샷이 없어 창이 루트까지다.
4. 수가 없거나 창이 씨앗과 루트에 닿지 못하면 스레드의 체크포인트를 모두 읽어 사슬을 루트까지 풀고,
   사슬 위 channel_values 의 이름을 파이프라인으로 읽어 씨앗을 찾는다.
5. 씨앗까지의 사슬에서 체크포인트마다 쓰기 등록(sorted set)의 키를 ZRANGE 로 읽는다. 기반 걷기가
   `aget_tuple` 로 읽는 것과 같은 키를 같은 차례로 읽는 것이다. 3에서는 씨앗 값과 같은 파이프라인에
   싣는다. 키마다 채널을 JSON.MGET 으로 먼저 읽고, 그 자리에서 걷는 채널과 정확히 같은 쓰기만 문서를
   통째로 읽는다. 세션의 모든 쓰기 본문을 읽지 않기 위해서다.

한 번 읽기의 왕복은 다음을 더한 수를 넘지 않는다. 파일을 쓰지 않은 30턴 세션은 6번, 첫 턴에 파일을
쓴 30턴 세션은 5번이다.

- 읽는 체크포인트 1(config 에 체크포인트 id 가 없으면 `aget_tuple` 의 왕복이 더해진다), 버전이 없는
  채널마다 쓰기 수 1
- 창의 체크포인트 1000개(`PAGE`)마다 검색 1. 1000개를 넘으면 1 더
- 창의 사슬 id 500개(`CHUNK`)마다 씨앗 값과 쓰기 등록 1
- 씨앗까지의 사슬 id 500개마다 쓰기 채널 1, 쓰기 문서 1
- 4로 가면 스레드의 체크포인트 1000개마다 검색 1(넘으면 1 더), 사슬 id 500개마다 channel_values
  이름 1과 쓰기 등록 1, 씨앗 값 1

기반 걷기와 같은 답을 같은 차례로 내는지는 tests/test_redis_saver.py 가 네 모양의 세션과 수가 맞지
않는 사슬에서 본다. 클러스터 모드, 쓰기 등록이 없는 저장기, TTL 을 쓰는 저장기(읽을 때 TTL 을 늘리는
일은 `aget_tuple` 이 한다), 읽는 체크포인트가 없거나 읽는 사이 사슬이 끊긴 경우에는 기반 걷기로
돌아간다. 상류 저장기가 이 메서드를 갖게 되면 테스트가 실패하고, 그때 이 하위 클래스를 지운다.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from langchain_core.runnables import RunnableConfig
from langgraph.checkpoint.base import DeltaChannelHistory, PendingWrite, get_checkpoint_id
from langgraph.checkpoint.redis.aio import AsyncRedisSaver
from langgraph.checkpoint.redis.base import REDIS_KEY_SEPARATOR, BaseRedisSaver
from langgraph.checkpoint.redis.util import (
    from_storage_safe_id,
    to_storage_safe_id,
    to_storage_safe_str,
)
from redisvl.query import CountQuery, FilterQuery
from redisvl.query.filter import FilterExpression, Num, Tag

PAGE = 1000
"""검색 한 번이 돌려주는 문서 수다. 넘으면 다음 쪽을 읽는다."""
CHUNK = 500
"""파이프라인 한 번에 싣는 사슬의 체크포인트 수다."""

Seeds = dict[str, tuple[int, Any]]
"""채널 → (씨앗의 사슬 위치, 씨앗 값)이다. 사슬에 씨앗이 없는 채널은 빠진다."""
Found = tuple[list[str], Seeds, dict[str, list[str]]]
"""사슬, 씨앗, 이미 읽은 체크포인트마다의 쓰기 등록 키다."""
Registered = dict[tuple[str, str], dict[str, Any]]
"""한 체크포인트의 쓰기다. 기반 걷기(`_aload_pending_writes`)처럼 (task_id, idx) 마다 하나다."""


@dataclass(frozen=True)
class Head:
    """사슬을 풀기 전에 읽는 체크포인트에서 아는 것이다."""

    parent: str | None
    step: int | None
    versions: frozenset[str]
    """버전이 있는 채널이다."""
    depths: Mapping[str, int]
    """채널마다 마지막 스냅샷 뒤의 superstep 수다. 씨앗은 부모부터 세어 이만큼째에 있으리라 본다."""


class ChainRedisSaver(AsyncRedisSaver):
    """`aget_delta_channel_history` 만 덮어쓴 Redis 체크포인터다. 나머지는 상류 그대로다."""

    async def aget_delta_channel_history(
        self, *, config: RunnableConfig, channels: Sequence[str]
    ) -> Mapping[str, DeltaChannelHistory]:
        if not channels:
            return {}
        if self.cluster_mode or self._key_registry is None or self.ttl_config:
            return await super().aget_delta_channel_history(config=config, channels=channels)
        thread_id = config["configurable"]["thread_id"]
        ns = config["configurable"].get("checkpoint_ns", "")
        target = get_checkpoint_id(config)
        if not target:
            latest = await self.aget_tuple(config)
            if latest is None:
                return {ch: {"writes": []} for ch in channels}
            target = latest.config["configurable"]["checkpoint_id"]
        head = await self._head(thread_id, ns, target)
        if head is None:
            return await super().aget_delta_channel_history(config=config, channels=channels)
        if head.parent is None:
            return {ch: {"writes": []} for ch in channels}
        walk = [
            ch for ch in channels if ch in head.versions or await self._written(thread_id, ns, ch)
        ]
        found: Found | None = ([], {}, {})
        if walk:
            found = await self._near(thread_id, ns, target, head, walk)
            if found is None:
                found = await self._far(thread_id, ns, target, head, walk)
        if found is None:
            return await super().aget_delta_channel_history(config=config, channels=channels)
        chain, seeds, zsets = found
        spans = {ch: chain[: seeds[ch][0] + 1] if ch in seeds else chain for ch in walk}
        writes = await self._writes(thread_id, ns, spans, zsets)
        result: dict[str, DeltaChannelHistory] = {}
        for ch in channels:
            entry: DeltaChannelHistory = {"writes": writes.get(ch, [])}
            if ch in seeds:
                entry["seed"] = seeds[ch][1]
            result[ch] = entry
        return result

    def _scope(self, thread_id: str, ns: str) -> FilterExpression:
        return (Tag("thread_id") == to_storage_safe_id(thread_id)) & (
            Tag("checkpoint_ns") == to_storage_safe_str(ns)
        )

    def _key(self, thread_id: str, ns: str, checkpoint_id: str) -> str:
        return self._make_redis_checkpoint_key(thread_id, ns, checkpoint_id)

    @staticmethod
    async def _search_all(
        index: Any, flt: FilterExpression, fields: list[str], sort: str | None = None
    ) -> list[Any]:
        """조건에 맞는 문서를 쪽마다 끝까지 읽는다. sort 가 있으면 그 칸의 차례로 읽어 쪽 사이에
        새 문서가 끼어도 앞 쪽의 문서를 다시 건너뛰지 않는다."""
        docs: list[Any] = []
        while True:
            query = FilterQuery(
                filter_expression=flt, return_fields=fields, num_results=PAGE, sort_by=sort
            )
            query.paging(len(docs), PAGE)
            res = await index.search(query)
            docs.extend(res.docs)
            if not res.docs or len(docs) >= res.total:
                return docs

    async def _head(self, thread_id: str, ns: str, target: str) -> Head | None:
        """읽는 체크포인트의 부모, step, 버전이 있는 채널, 채널마다 마지막 스냅샷 뒤의 superstep
        수다. 체크포인트가 없거나 모양이 다르면 None 이다."""
        key = self._key(thread_id, ns, target)
        pipe = self._redis.pipeline(transaction=False)
        pipe.json().get(key, "$.parent_checkpoint_id", "$.step", "$.metadata")
        pipe.json().objkeys(key, "$.checkpoint.channel_versions")
        fields, versions = await pipe.execute(raise_on_error=False)
        if not isinstance(fields, dict) or not fields.get("$.parent_checkpoint_id"):
            return None
        if not (isinstance(versions, list) and versions and isinstance(versions[0], list)):
            return None
        step = (fields.get("$.step") or [None])[0]
        return Head(
            parent=from_storage_safe_id(fields["$.parent_checkpoint_id"][0] or "") or None,
            step=step if isinstance(step, int) else None,
            versions=frozenset(versions[0]),
            depths=_depths((fields.get("$.metadata") or [None])[0]),
        )

    async def _written(self, thread_id: str, ns: str, channel: str) -> bool:
        """스레드·네임스페이스에 그 채널의 쓰기가 하나라도 있는가다."""
        scope = self._scope(thread_id, ns) & (Tag("channel") == channel)
        return bool(await self.checkpoint_writes_index.query(CountQuery(scope)))

    async def _parents(self, flt: FilterExpression) -> dict[str, str | None]:
        """조건에 맞는 체크포인트의 id → 부모 id(루트는 None)다. id 는 문서 키의 마지막 칸에서
        읽는다. 한 쪽에 다 들면 정렬하지 않고, 넘치면 처음부터 정렬해 쪽마다 읽는다. 돌려주는 칸이
        하나 줄고 정렬이 빠지면 300개를 읽을 때 2ms 남짓 빠르다."""
        fields = ["parent_checkpoint_id"]
        query = FilterQuery(filter_expression=flt, return_fields=fields, num_results=PAGE)
        res = await self.checkpoints_index.search(query)
        docs = res.docs
        if len(docs) < res.total:
            docs = await self._search_all(self.checkpoints_index, flt, fields, sort="checkpoint_ts")
        parents: dict[str, str | None] = {}
        for doc in docs:
            parent = from_storage_safe_id(getattr(doc, "parent_checkpoint_id", "") or "")
            cid = doc.id.rsplit(REDIS_KEY_SEPARATOR, 1)[-1]
            parents[from_storage_safe_id(cid)] = parent or None
        return parents

    async def _near(
        self, thread_id: str, ns: str, target: str, head: Head, walk: list[str]
    ) -> Found | None:
        """metadata 의 superstep 수만큼만 사슬을 읽고 채널마다 창 안의 channel_values 값을 JSON.MGET
        으로 읽는다. 창 안의 가장 가까운 값이 씨앗이고, 같은 파이프라인에서 창의 쓰기 등록도 읽는다.
        수가 없거나 사슬이 창 안에서 끊기거나 창 안에 씨앗도 루트도 없으면 None 이다."""
        if head.step is None or any(ch not in head.depths for ch in walk):
            return None
        depth = max(head.depths[ch] for ch in walk)
        flt = self._scope(thread_id, ns) & Num("step").between(head.step - depth, head.step - 1)
        found = _ancestors(target, head.parent, await self._parents(flt), depth)
        if found is None:
            return None
        chain, complete = found
        seeds: Seeds = {}
        zsets: dict[str, list[str]] = {}
        for start in range(0, len(chain), CHUNK):
            looking = [ch for ch in walk if ch not in seeds and start < head.depths[ch]]
            if not looking:
                break
            cids = chain[start : start + CHUNK]
            pipe = self._redis.pipeline(transaction=False)
            self._queue_zsets(pipe, thread_id, ns, cids)
            for ch in looking:
                window = chain[start : min(start + CHUNK, head.depths[ch])]
                pipe.json().mget([self._key(thread_id, ns, cid) for cid in window], _value_path(ch))
            replies = await pipe.execute()
            zsets.update(zip(cids, replies[: len(cids)], strict=True))
            for ch, values in zip(looking, replies[len(cids) :], strict=True):
                if not isinstance(values, list) or None in values:
                    return None
                pos = next((i for i, value in enumerate(values) if value), None)
                if pos is not None:
                    seeds[ch] = (start + pos, self._recursive_deserialize(values[pos][0]))
        for ch in walk:
            if ch not in seeds and not (complete and head.depths[ch] >= len(chain)):
                return None
        return chain, seeds, zsets

    async def _far(
        self, thread_id: str, ns: str, target: str, head: Head, walk: list[str]
    ) -> Found | None:
        """스레드·네임스페이스의 체크포인트를 모두 읽어 사슬을 루트까지 풀고 씨앗을 찾는다. 사슬이
        끊겼거나 그 사이 체크포인트나 씨앗 값이 사라졌으면 None 이다."""
        found = _ancestors(target, head.parent, await self._parents(self._scope(thread_id, ns)))
        if found is None:
            return None
        chain, _complete = found
        positions = await self._seed_positions(thread_id, ns, chain, walk)
        if positions is None:
            return None
        pipe = self._redis.pipeline(transaction=False)
        for ch, pos in positions.items():
            pipe.json().get(self._key(thread_id, ns, chain[pos]), _value_path(ch))
        seeds: Seeds = {}
        for (ch, pos), value in zip(positions.items(), await pipe.execute(), strict=True):
            if not value:
                return None
            seeds[ch] = (pos, self._recursive_deserialize(value[0]))
        return chain, seeds, {}

    async def _seed_positions(
        self, thread_id: str, ns: str, chain: list[str], channels: Sequence[str]
    ) -> dict[str, int] | None:
        """채널마다 channel_values 에 값이 있는 가장 가까운 조상의 사슬 위치다. 루트까지 없으면 그
        채널은 빠진다. 기반 걷기가 `tup.checkpoint["channel_values"]` 에서 보는 것과 같다. 사슬의
        체크포인트가 없거나 읽기가 실패하면 사슬이 끊긴 것이라 None 이다."""
        found: dict[str, int] = {}
        for start in range(0, len(chain), CHUNK):
            pipe = self._redis.pipeline(transaction=False)
            for cid in chain[start : start + CHUNK]:
                pipe.json().objkeys(self._key(thread_id, ns, cid), "$.checkpoint.channel_values")
            for offset, reply in enumerate(await pipe.execute(raise_on_error=False)):
                names = _object_keys(reply)
                if names is None:
                    return None
                for ch in channels:
                    if ch not in found and ch in names:
                        found[ch] = start + offset
            if len(found) == len(channels):
                break
        return found

    def _queue_zsets(self, pipe: Any, thread_id: str, ns: str, cids: list[str]) -> None:
        """체크포인트마다 쓰기 등록의 키를 등록 차례로 읽는 ZRANGE 를 파이프라인에 싣는다."""
        for cid in cids:
            zset = self._key_registry.make_write_keys_zset_key(thread_id, ns, cid)
            pipe.execute_command("ZRANGE", zset, 0, -1)

    async def _writes(
        self,
        thread_id: str,
        ns: str,
        spans: Mapping[str, list[str]],
        zsets: Mapping[str, list[str]],
    ) -> dict[str, list[PendingWrite]]:
        """채널마다 씨앗까지의 사슬 위 쓰기를 오래된 것부터 늘어놓는다. 기반 걷기가 `aget_tuple` 로
        읽는 것처럼 체크포인트마다 쓰기 등록의 키를 ZRANGE 차례로 읽는다(zsets 에 없는 것만 읽는다).
        키마다 채널을 먼저 읽고, 그 자리에서 걷는 채널과 정확히 같은 키만 문서를 통째로 읽는다.
        문서가 없는 키는 건너뛰고, (task_id, idx) 가 같은 쓰기는 하나로 친다."""
        longest = max(spans.values(), key=len, default=[])
        registered: dict[str, Registered] = {}
        for start in range(0, len(longest), CHUNK):
            cids = longest[start : start + CHUNK]
            missing = [cid for cid in cids if cid not in zsets]
            read: dict[str, list[str]] = {}
            if missing:
                pipe = self._redis.pipeline(transaction=False)
                self._queue_zsets(pipe, thread_id, ns, missing)
                read = dict(zip(missing, await pipe.execute(), strict=True))
            keys_by_cid = [zsets[cid] if cid in zsets else read[cid] for cid in cids]
            keys = [key for cid_keys in keys_by_cid for key in cid_keys]
            channels = iter(await self._redis.json().mget(keys, "$.channel") if keys else [])
            wanted: list[str] = []
            for offset, cid_keys in enumerate(keys_by_cid):
                walking = {ch for ch, span in spans.items() if start + offset < len(span)}
                for key in cid_keys:
                    channel = next(channels)
                    if channel and channel[0] in walking:
                        wanted.append(key)
            found = await self._redis.json().mget(wanted, "$") if wanted else []
            docs = dict(zip(wanted, found, strict=True))
            for cid, cid_keys in zip(cids, keys_by_cid, strict=True):
                registered[cid] = _registered(docs[key] for key in cid_keys if key in docs)
        out: dict[str, list[PendingWrite]] = {}
        for ch, span in spans.items():
            out[ch] = [
                write
                for cid in reversed(span)
                for write in BaseRedisSaver._load_writes(
                    self.serde,
                    {k: data for k, data in registered[cid].items() if data["channel"] == ch},
                )
            ]
        return out


def _registered(docs: Any) -> Registered:
    """한 체크포인트의 쓰기 문서(JSON.MGET `$` 의 답, 등록 차례)를 기반 걷기의
    `_aload_pending_writes` 와 같은 모양으로 모은다."""
    writes: Registered = {}
    for doc in docs:
        data = doc[0] if doc else None
        if not data:
            continue
        task_id = data.get("task_id", "")
        idx = str(data.get("idx", 0))
        writes[(task_id, idx)] = {
            "task_id": task_id,
            "idx": idx,
            "channel": data.get("channel", ""),
            "type": data.get("type", ""),
            "blob": data.get("blob", b""),
        }
    return writes


def _ancestors(
    target: str, first: str | None, parents: Mapping[str, str | None], limit: int | None = None
) -> tuple[list[str], bool] | None:
    """읽는 체크포인트의 부모(first)부터 루트 쪽으로 limit 개까지의 id 와, 사슬이 루트에서
    끝났는지다. 사슬이 목록 밖으로 나가거나 되돌아오면 None 이다(읽는 사이 지워졌거나 망가졌다)."""
    chain: list[str] = []
    seen = {target}
    cid = first
    while cid is not None and (limit is None or len(chain) < limit):
        if cid not in parents or cid in seen:
            return None
        chain.append(cid)
        seen.add(cid)
        cid = parents[cid]
    return chain, cid is None


def _depths(metadata: Any) -> dict[str, int]:
    """metadata 의 `counters_since_delta_snapshot`({채널: [업데이트 수, superstep 수]})에서 채널마다
    superstep 수다. 저장기는 metadata 를 JSON 문자열로 둔다. 읽을 수 없으면 빈 dict 다."""
    if isinstance(metadata, str):
        try:
            metadata = json.loads(metadata)
        except ValueError:
            return {}
    counters = metadata.get("counters_since_delta_snapshot") if isinstance(metadata, dict) else None
    if not isinstance(counters, dict):
        return {}
    return {
        ch: pair[1]
        for ch, pair in counters.items()
        if isinstance(pair, list) and len(pair) == 2 and isinstance(pair[1], int) and pair[1] > 0
    }


def _value_path(channel: str) -> str:
    return f"$.checkpoint.channel_values[{json.dumps(channel)}]"


def _object_keys(reply: Any) -> set[str] | None:
    """`JSON.OBJKEYS key $.checkpoint.channel_values` 의 답에서 이름 집합이다. channel_values 가
    객체가 아니면 빈 집합이다. 키가 없거나(None) 읽기가 실패했으면 None 이다."""
    if reply is None or isinstance(reply, Exception):
        return None
    if isinstance(reply, list) and reply and isinstance(reply[0], list):
        return set(reply[0])
    return set()
