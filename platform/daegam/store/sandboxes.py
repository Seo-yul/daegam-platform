"""코드 실행 샌드박스의 기록과 슬롯 수 (P40 5절·7절, D95).

- `sandboxes`: 샌드박스 하나가 문서 하나다. `_id` 가 샌드박스 id 이고 토큰은 그 id 에서 나온다
  (`auth/sandbox_key.py`). 세션의 샌드박스는 (Agent, 세션) 마다 열린 것이 하나뿐이다(유일 부분
  인덱스). 상태는 `open` → `closing` → `closed` 다. `closing` 은 닫는 쪽 하나가 파드를 지우고
  슬롯을 돌려주는 사이이고, 다시 세기는 이것도 센다. 닫힌 기록은 `CLOSED_TTL_DAYS` 동안 남아
  찾기가 "회수됨" 과 "잃음" 을 가르고 세대 번호가 이어진다.
- `sandbox_slots`: 문서 하나(`_id: slots`)에 사람·Agent·실행·테넌트·클러스터·노드마다의 열린 수를
  둔다. 열기는 이 문서 하나에 조건부 `$inc` 하나로 모든 상한과 노드 슬롯을 함께 잡는다(7절). 닫기가
  하나씩 빼고, 스윕이 열린 기록에서 다시 센다. 다시 세기는 그 사이 잡기나 돌려주기가 지나갔으면
  (`seq` 가 바뀌었으면) 쓰지 않는다. 열기는 기록을 먼저 넣고 슬롯을 잡고, 닫기는 슬롯을 돌려준 뒤
  `closed` 로 옮기므로 다시 세기는 넘치게 셀 수는 있어도 모자라게 세지 않는다.

- `sandbox_probes`: 88 로 끝난 샌드박스와 첫 점검이 87 로 끝난 여는 중의 샌드박스 뒤에 도는 사용자
  코드 없는 프로브(8절)다. (클러스터, 노드)마다 문서 하나이고, 한 노드에 한 번에 하나, 10분에
  하나다. `first_check` 가 있으면 그 프로브가 87 을 확인하는 중이다.

열쇠는 사람의 sub 처럼 점이나 `$` 가 들 수 있는 값이라 sha256 의 앞 32자로 바꿔 칸 이름으로 쓴다.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Literal

from pydantic import Field
from pymongo import ASCENDING, DESCENDING, ReturnDocument
from pymongo.asynchronous.database import AsyncDatabase
from pymongo.errors import DuplicateKeyError

from daegam.store.base import Document, UtcDatetime, ensure_ttl_index, new_id, utcnow

SANDBOXES = "sandboxes"
SANDBOX_SLOTS = "sandbox_slots"
SANDBOX_PROBES = "sandbox_probes"
SLOTS_ID = "slots"
# 닫힌 기록이 남는 날 수
CLOSED_TTL_DAYS = 7
KEY_LENGTH = 32
# 조건부 쓰기가 다른 쓰기와 겹쳤을 때 다시 해 보는 횟수
CAS_TRIES = 5

SandboxState = Literal["open", "closing", "closed"]
# session 은 세션의 샌드박스, check 는 클러스터 점검의 진짜 샌드박스(8절 2단계)다
SandboxKind = Literal["session", "check"]
LIVE_STATES: tuple[SandboxState, ...] = ("open", "closing")

# 상한의 이름. 거절은 이 이름을 싣는다(7절). 노드 슬롯이 없는 것도 `cluster` 다
CapName = Literal["person", "agent", "run", "tenant", "cluster"]
SLOT_FIELDS = ("persons", "agents", "runs", "tenants", "clusters", "nodes")


def slot_key(*parts: str) -> str:
    """칸 이름으로 쓸 열쇠."""
    raw = "\0".join(parts).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()[:KEY_LENGTH]


class SandboxRecord(Document):
    """샌드박스 하나. id 가 샌드박스 id 다. person 은 플랫폼의 세션 소유자 기록에서 왔고(없으면
    None), run_id 는 그 세션이 P41 실행의 것일 때다. 실행은 사람의 몫 대신 실행의 몫을 쓴다."""

    id: str = Field(default_factory=new_id)
    kind: SandboxKind = "session"
    agent_id: str
    tenant: str
    cluster_id: str
    namespace: str
    session_id: str
    person: str | None = None
    run_id: str | None = None
    node: str
    generation: int = 1
    pod: str
    state: SandboxState = "open"
    # 개발용 우회로 점검 없이 연 것이다 (8절)
    unchecked: bool = False
    opened_at: UtcDatetime = Field(default_factory=utcnow)
    last_used_at: UtcDatetime = Field(default_factory=utcnow)
    # 파드가 처음 준비된 때. 없으면 아직 여는 중이다
    ready_at: UtcDatetime | None = None
    # 파드가 알린 이미지 id. 판정의 image_id 와 맞댄다 (8절)
    image_id: str | None = None
    closed_at: UtcDatetime | None = None
    close_reason: str | None = None

    @property
    def ready(self) -> bool:
        return self.ready_at is not None

    def slot_paths(self) -> dict[str, str]:
        """상한의 이름(노드는 `node`) → 슬롯 문서의 점 경로. 이 기록이 잡는 칸이다. 점검의
        샌드박스는 클러스터와 노드의 칸만 잡는다."""
        out: dict[str, str] = {}
        if self.kind == "session":
            if self.run_id:
                out["run"] = f"runs.{slot_key(self.agent_id)}"
            elif self.person:
                out["person"] = f"persons.{slot_key(self.person)}"
            out["agent"] = f"agents.{slot_key(self.agent_id)}"
            out["tenant"] = f"tenants.{slot_key(self.tenant)}"
        out["cluster"] = f"clusters.{slot_key(self.cluster_id)}"
        out["node"] = f"nodes.{slot_key(self.cluster_id, self.node)}"
        return out


@dataclass(frozen=True)
class SlotCaps:
    """열기 하나에 드는 상한. node 는 그 노드의 `cap_i`, cluster 는 클러스터 용량이다."""

    person: int
    agent: int
    run: int
    tenant: int
    cluster: int
    node: int

    def of(self, name: str) -> int:
        return int(getattr(self, name))


# 거절의 이름을 정하는 차례. 노드 슬롯은 `cluster` 로 낸다
_REFUSAL_ORDER: tuple[tuple[str, CapName], ...] = (
    ("person", "person"),
    ("agent", "agent"),
    ("run", "run"),
    ("tenant", "tenant"),
    ("cluster", "cluster"),
    ("node", "cluster"),
)


def _value(doc: dict[str, Any], path: str) -> int:
    current: Any = doc
    for part in path.split("."):
        current = current.get(part) if isinstance(current, dict) else None
    return int(current or 0)


class MongoSandboxStore:
    def __init__(self, db: AsyncDatabase):
        self.coll = db[SANDBOXES]
        self.slots = db[SANDBOX_SLOTS]
        self.probes = db[SANDBOX_PROBES]

    async def init_schema(self) -> None:
        await self.coll.create_index(
            [("agent_id", ASCENDING), ("session_id", ASCENDING)],
            unique=True,
            name="uq_open_session",
            partialFilterExpression={"state": "open"},
        )
        await self.coll.create_index(
            [("agent_id", ASCENDING), ("session_id", ASCENDING), ("generation", DESCENDING)],
            name="ix_session_generation",
        )
        await self.coll.create_index(
            [("cluster_id", ASCENDING), ("state", ASCENDING)], name="ix_cl"
        )
        await self.coll.create_index(
            [("state", ASCENDING), ("last_used_at", ASCENDING)], name="ix_lu"
        )
        await ensure_ttl_index(
            self.coll,
            "closed_at",
            int(timedelta(days=CLOSED_TTL_DAYS).total_seconds()),
            "ttl_closed",
        )
        await self.ensure_slots()

    # -- 기록 -------------------------------------------------------------------------------

    async def get(self, sandbox_id: str) -> SandboxRecord | None:
        doc = await self.coll.find_one({"_id": sandbox_id})
        return SandboxRecord.from_doc(doc) if doc else None

    async def open_for(self, agent_id: str, session_id: str) -> SandboxRecord | None:
        doc = await self.coll.find_one(
            {"agent_id": agent_id, "session_id": session_id, "state": "open"}
        )
        return SandboxRecord.from_doc(doc) if doc else None

    async def last_for(self, agent_id: str, session_id: str) -> SandboxRecord | None:
        """그 세션의 가장 뒤 세대 기록. 닫힌 것이어도 된다."""
        doc = await self.coll.find_one(
            {"agent_id": agent_id, "session_id": session_id}, sort=[("generation", DESCENDING)]
        )
        return SandboxRecord.from_doc(doc) if doc else None

    async def insert(self, rec: SandboxRecord) -> bool:
        """넣었으면 참이다. 같은 세션의 열린 기록이 이미 있으면 거짓이다."""
        try:
            await self.coll.insert_one(rec.to_doc())
        except DuplicateKeyError:
            return False
        return True

    async def set_node(self, sandbox_id: str, node: str) -> None:
        await self.coll.update_one({"_id": sandbox_id, "state": "open"}, {"$set": {"node": node}})

    async def mark_ready(
        self, sandbox_id: str, image_id: str | None, *, unchecked: bool = False
    ) -> tuple[SandboxRecord | None, bool]:
        """처음 준비된 때와 파드의 이미지 id 를 적는다. (기록, 이 호출이 처음 적었는가). 열린 기록이
        아니면 기록이 None 이다. 같은 열기를 기다리던 여럿 가운데 하나만 처음이다. unchecked 면
        개발용 우회로 쓴 것으로 적는다."""
        now = utcnow()
        sets: dict[str, Any] = {"ready_at": now, "last_used_at": now, "image_id": image_id}
        if unchecked:
            sets["unchecked"] = True
        doc = await self.coll.find_one_and_update(
            {"_id": sandbox_id, "state": "open", "ready_at": None},
            {"$set": sets},
            return_document=ReturnDocument.AFTER,
        )
        if doc is not None:
            return SandboxRecord.from_doc(doc), True
        doc = await self.coll.find_one({"_id": sandbox_id, "state": "open"})
        return (SandboxRecord.from_doc(doc) if doc else None), False

    async def touch(self, agent_id: str, session_id: str) -> SandboxRecord | None:
        """열린 기록의 `last_used_at` 을 지금으로 민다. 열린 기록이 없으면 None 이다."""
        doc = await self.coll.find_one_and_update(
            {"agent_id": agent_id, "session_id": session_id, "state": "open"},
            {"$set": {"last_used_at": utcnow()}},
            return_document=ReturnDocument.AFTER,
        )
        return SandboxRecord.from_doc(doc) if doc else None

    async def begin_close(
        self, sandbox_id: str, reason: str, *, untouched_before: datetime | None = None
    ) -> SandboxRecord | None:
        """`open` 을 `closing` 으로 옮긴다. 옮긴 쪽 하나만 기록을 받고, 그쪽이 파드를 지우고 슬롯을
        돌려준 뒤 `finish_close` 한다. 이미 닫히는 중이거나 닫혔으면 None 이다. untouched_before 를
        주면(회수) 그때 뒤로 쓰이지 않았을 때만이다. 읽은 뒤에 온 쓰기 표시가 회수를 막는다."""
        where: dict[str, Any] = {"_id": sandbox_id, "state": "open"}
        if untouched_before is not None:
            where["last_used_at"] = {"$lt": untouched_before}
        doc = await self.coll.find_one_and_update(
            where,
            {"$set": {"state": "closing", "close_reason": reason, "closed_at": utcnow()}},
            return_document=ReturnDocument.AFTER,
        )
        return SandboxRecord.from_doc(doc) if doc else None

    async def finish_close(self, sandbox_id: str) -> None:
        await self.coll.update_one(
            {"_id": sandbox_id, "state": "closing"}, {"$set": {"state": "closed"}}
        )

    async def drop_unreserved(self, sandbox_id: str, reason: str) -> None:
        """슬롯을 잡지 못한 기록을 바로 닫는다. 돌려줄 슬롯이 없다."""
        await self.coll.update_one(
            {"_id": sandbox_id, "state": "open"},
            {"$set": {"state": "closed", "close_reason": reason, "closed_at": utcnow()}},
        )

    async def live(
        self,
        *,
        cluster_id: str | None = None,
        agent_id: str | None = None,
        states: tuple[SandboxState, ...] = ("open",),
    ) -> list[SandboxRecord]:
        q: dict[str, Any] = {"state": {"$in": list(states)}}
        if cluster_id is not None:
            q["cluster_id"] = cluster_id
        if agent_id is not None:
            q["agent_id"] = agent_id
        cursor = self.coll.find(q).sort([("opened_at", ASCENDING), ("_id", ASCENDING)])
        return [SandboxRecord.from_doc(d) for d in await cursor.to_list()]

    async def reclaimable(self, person: str, before: datetime) -> list[SandboxRecord]:
        """그 사람의 열린 세션 샌드박스 가운데 before 전부터 쓰이지 않은 것. 가장 오래 쓰이지 않은
        것이 앞이다. 실행의 것은 사람의 몫이 아니라 빠진다."""
        cursor = self.coll.find(
            {
                "state": "open",
                "kind": "session",
                "person": person,
                "run_id": None,
                "last_used_at": {"$lt": before},
            }
        ).sort([("last_used_at", ASCENDING), ("_id", ASCENDING)])
        return [SandboxRecord.from_doc(d) for d in await cursor.to_list()]

    async def stuck_closing(self, before: datetime) -> list[SandboxRecord]:
        cursor = self.coll.find({"state": "closing", "closed_at": {"$lt": before}})
        return [SandboxRecord.from_doc(d) for d in await cursor.to_list()]

    async def latest_for_pods(
        self, cluster_id: str, pods: list[tuple[str, str]]
    ) -> dict[tuple[str, str], SandboxRecord]:
        """주어진 (네임스페이스, 파드) 마다 가장 뒤 기록. 닫힌 기록도 든다. 스윕이 클러스터에서
        본 파드만 묻는다."""
        if not pods:
            return {}
        cursor = self.coll.find(
            {"cluster_id": cluster_id, "pod": {"$in": sorted({name for _ns, name in pods})}}
        ).sort([("opened_at", ASCENDING)])
        wanted = set(pods)
        out: dict[tuple[str, str], SandboxRecord] = {}
        for d in await cursor.to_list():
            rec = SandboxRecord.from_doc(d)
            if (rec.namespace, rec.pod) in wanted:
                out[(rec.namespace, rec.pod)] = rec
        return out

    async def agents_with_records(self, cluster_id: str) -> set[str]:
        """그 클러스터에 기록(닫힌 것도)이 남은 세션 샌드박스의 Agent. 이 설치의 Agent 다."""
        return set(
            await self.coll.distinct("agent_id", {"cluster_id": cluster_id, "kind": "session"})
        )

    # -- 슬롯 -------------------------------------------------------------------------------

    async def ensure_slots(self) -> None:
        try:
            await self.slots.update_one(
                {"_id": SLOTS_ID},
                {"$setOnInsert": {**{f: {} for f in SLOT_FIELDS}, "seq": 0}},
                upsert=True,
            )
        except DuplicateKeyError:
            # 두 레플리카가 함께 넣었다. 한쪽이 넣었으면 된다
            pass

    async def slot_counts(self) -> dict[str, Any]:
        return await self.slots.find_one({"_id": SLOTS_ID}) or {}

    async def node_counts(self, cluster_id: str, nodes: list[str]) -> dict[str, int]:
        """그 클러스터 노드마다 잡힌 슬롯 수."""
        doc = await self.slot_counts()
        held = doc.get("nodes") or {}
        return {n: int(held.get(slot_key(cluster_id, n)) or 0) for n in nodes}

    async def reserve(self, rec: SandboxRecord, caps: SlotCaps) -> CapName | None:
        """기록이 잡는 칸을 모두 하나씩 늘린다. 하나라도 상한에 닿았으면 아무것도 늘리지 않고 그
        상한의 이름이다. 잡았으면 None 이다."""
        paths = rec.slot_paths()
        for name, cap_name in _REFUSAL_ORDER:
            # 없는 칸은 `$not: {$gte}` 에 맞으므로 0 인 상한은 여기서 거절한다
            if name in paths and caps.of(name) <= 0:
                return cap_name
        await self.ensure_slots()
        where: dict[str, Any] = {"_id": SLOTS_ID}
        for name, path in paths.items():
            where[path] = {"$not": {"$gte": caps.of(name)}}
        res = await self.slots.update_one(
            where, {"$inc": {**dict.fromkeys(paths.values(), 1), "seq": 1}}
        )
        if res.modified_count == 1:
            return None
        refused = await self.refusing(rec, caps)
        # 읽는 사이 자리가 났으면 부르는 쪽이 다시 한다
        return refused[0] if refused else "cluster"

    async def refusing(self, rec: SandboxRecord, caps: SlotCaps) -> list[CapName]:
        """지금 그 기록을 거절할 상한 모두. 차례는 거절의 이름을 정하는 차례다. 사람의 몫만 막을
        때만 회수가 열기를 살릴 수 있다."""
        paths = rec.slot_paths()
        doc = await self.slot_counts()
        out: list[CapName] = []
        for name, cap_name in _REFUSAL_ORDER:
            path = paths.get(name)
            if path is None:
                continue
            if (caps.of(name) <= 0 or _value(doc, path) >= caps.of(name)) and cap_name not in out:
                out.append(cap_name)
        return out

    async def release(self, rec: SandboxRecord) -> None:
        """기록이 잡은 칸을 하나씩 돌려준다. `begin_close` 를 이긴 쪽만 부른다."""
        paths = rec.slot_paths()
        await self.slots.update_one(
            {"_id": SLOTS_ID}, {"$inc": {**dict.fromkeys(paths.values(), -1), "seq": 1}}
        )

    async def recount(self) -> bool:
        """열렸거나 닫히는 기록에서 다시 센다. 그 사이 잡기나 돌려주기가 지나갔으면 쓰지 않고
        거짓이다. 다음 스윕이 다시 센다."""
        await self.ensure_slots()
        seq = int((await self.slot_counts()).get("seq") or 0)
        counts: dict[str, dict[str, int]] = {f: {} for f in SLOT_FIELDS}
        async for d in self.coll.find({"state": {"$in": list(LIVE_STATES)}}):
            rec = SandboxRecord.from_doc(d)
            for path in rec.slot_paths().values():
                field, _, key = path.partition(".")
                counts[field][key] = counts[field].get(key, 0) + 1
        res = await self.slots.update_one(
            {"_id": SLOTS_ID, "seq": seq}, {"$set": {**counts, "seq": seq + 1}}
        )
        return res.modified_count == 1

    # -- 프로브 (8절) -------------------------------------------------------------------------

    async def want_probe(
        self, cluster_id: str, node: str, namespace: str, tenant: str, *, quiet_since: datetime
    ) -> bool:
        """그 노드의 프로브를 바란다. 도는 프로브가 있거나 quiet_since 뒤에 시작한 것이 있으면
        아무것도 하지 않고 거짓이다. `first_check` 는 건드리지 않는다. 읽고 쓰는 사이에
        `confirm_first_check` 가 넣은 표시를 지우지 않으려는 것이고, 끝난 프로브의 표시는 끝낼 때
        지워져 있다."""
        key = probe_id(cluster_id, node)
        doc = await self.probes.find_one({"_id": key})
        if doc is not None and (
            doc.get("state") in ("wanted", "running")
            or (doc.get("started_at") and doc["started_at"] >= quiet_since)
        ):
            return False
        await self.probes.update_one(
            {"_id": key},
            {
                "$set": {
                    "cluster_id": cluster_id,
                    "node": node,
                    "namespace": namespace,
                    "tenant": tenant,
                    "state": "wanted",
                    "wanted_at": utcnow(),
                }
            },
            upsert=True,
        )
        return True

    async def confirm_first_check(
        self,
        cluster_id: str,
        node: str,
        namespace: str,
        tenant: str,
        message: str | None,
        *,
        quiet_since: datetime,
    ) -> bool:
        """87 로 끝난 첫 점검을 그 노드의 프로브가 확인하게 한다. 바라거나 도는 프로브가 있으면
        그 문서에 표시하고(`at` 은 첫 87 의 때로 남아 시한을 재고, `last_at` 은 가장 늦은 87 의
        때다), 없으면 새로 바란다. `last_at` 보다 먼저 시작한 프로브는 그 87 을 답하지 못한다
        (`probe_end`). quiet_since 뒤에 시작해 이미 끝난 프로브가 있으면 10분의 창 때문에 띄울 수
        없어 거짓이다. 그때는 부른 쪽이 판정을 무효로 한다."""
        key = probe_id(cluster_id, node)
        now = utcnow()
        first = {"at": now, "last_at": now, "message": message}
        for _ in range(CAS_TRIES):
            doc = await self.probes.find_one({"_id": key})
            if doc is not None and doc.get("state") in ("wanted", "running"):
                had = doc.get("first_check")
                marked = {**had, "last_at": now, "message": message} if had else first
                res = await self.probes.update_one(
                    {"_id": key, "state": doc["state"], "first_check": had},
                    {"$set": {"first_check": marked}},
                )
                if res.matched_count == 1:
                    return True
                continue
            if doc is not None and doc.get("started_at") and doc["started_at"] >= quiet_since:
                return False
            try:
                res = await self.probes.update_one(
                    {"_id": key, "state": doc.get("state") if doc else {"$exists": False}},
                    {
                        "$set": {
                            "cluster_id": cluster_id,
                            "node": node,
                            "namespace": namespace,
                            "tenant": tenant,
                            "state": "wanted",
                            "wanted_at": now,
                            "first_check": first,
                        }
                    },
                    upsert=doc is None,
                )
            except DuplicateKeyError:
                continue
            if res.matched_count == 1 or res.upserted_id is not None:
                return True
        # 문서가 계속 바뀌어 표시하지 못했다. 확인을 맡기지 못했으므로 부른 쪽이 무효로 한다
        return False

    async def probes_of(self, cluster_id: str) -> list[dict[str, Any]]:
        cursor = self.probes.find(
            {"cluster_id": cluster_id, "state": {"$in": ["wanted", "running"]}}
        )
        return await cursor.to_list()

    async def probe_started(self, key: str, pod: str) -> None:
        await self.probes.update_one(
            {"_id": key, "state": "wanted"},
            {"$set": {"state": "running", "pod": pod, "started_at": utcnow()}},
        )

    async def probe_end(self, key: str, result: str) -> ProbeEnd | None:
        """끝난 프로브를 적고, 그때의 문서로 정한 결말을 돌려준다. 문서를 읽은 뒤에 붙은
        `first_check` 도 보도록 지금의 문서에 조건부로 쓴다. 표시가 있는데 프로브가 가장 늦은
        87 보다 먼저 시작했으면 그 87 을 답하지 못하므로, 87 로 끝난 것이 아니면 표시를 둔 채
        `wanted` 로 돌려 새 프로브가 확인하게 한다. 그 밖에는 `done` 이고 표시를 지운다. 문서가
        이미 끝났으면 None 이다."""
        for _ in range(CAS_TRIES):
            doc = await self.probes.find_one({"_id": key, "state": "running"})
            if doc is None:
                return None
            first = doc.get("first_check")
            started = doc.get("started_at")
            again = bool(
                first
                and result != "failed"
                and (started is None or started < first.get("last_at", first["at"]))
            )
            if again:
                change = {"state": "wanted", "pod": None, "started_at": None}
            else:
                change = {
                    "state": "done",
                    "result": result,
                    "done_at": utcnow(),
                    "first_check": None,
                }
            res = await self.probes.update_one(
                {"_id": key, "state": "running", "first_check": first}, {"$set": change}
            )
            if res.matched_count == 1:
                return ProbeEnd(first_check=first, again=again)
        return None

    async def probe_retry(self, key: str) -> None:
        """뜨지 못한 프로브를 다음 스윕이 다시 하게 한다. 10분의 창에 세지 않는다. `first_check`
        는 그대로 둔다."""
        await self.probes.update_one(
            {"_id": key, "state": {"$in": ["wanted", "running"]}},
            {"$set": {"state": "wanted", "pod": None, "started_at": None}},
        )

    async def end_late_first_checks(
        self, cluster_id: str, before: datetime
    ) -> list[dict[str, Any]]:
        """`first_check.at` 이 before 보다 이른, 아직 끝나지 않은 프로브를 `unconfirmed` 로 끝내고
        끝내기 전의 문서들을 돌려준다. Mongo 만 쓴다. 그 파드는 `orphan_pod` 에 남겨 프로브 단계가
        지운다."""
        ended: list[dict[str, Any]] = []
        while True:
            doc = await self.probes.find_one_and_update(
                {
                    "cluster_id": cluster_id,
                    "state": {"$in": ["wanted", "running"]},
                    "first_check.at": {"$lt": before},
                },
                [
                    {
                        "$set": {
                            "state": "done",
                            "result": "unconfirmed",
                            "done_at": utcnow(),
                            "first_check": None,
                            "orphan_pod": "$pod",
                        }
                    }
                ],
            )
            if doc is None:
                return ended
            ended.append(doc)

    async def orphan_probes(self, cluster_id: str) -> list[dict[str, Any]]:
        cursor = self.probes.find({"cluster_id": cluster_id, "orphan_pod": {"$type": "string"}})
        return await cursor.to_list()

    async def orphan_deleted(self, key: str, pod: str) -> None:
        await self.probes.update_one(
            {"_id": key, "orphan_pod": pod}, {"$set": {"orphan_pod": None}}
        )


@dataclass(frozen=True)
class ProbeEnd:
    """끝난 프로브의 결말(`probe_end`). first_check 는 끝낼 때 문서에 있던 표시이고, again 이면
    그 표시를 둔 채 다시 바라는 것으로 돌렸다."""

    first_check: dict[str, Any] | None
    again: bool


def probe_id(cluster_id: str, node: str) -> str:
    return f"{cluster_id}:{slot_key(node)}"
