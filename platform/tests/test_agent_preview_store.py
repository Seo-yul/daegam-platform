"""미리 보기 문서와 Agent 스토어의 질의마다의 선택 (P44b 5절·10절, F0.1, D107).

미리 보기는 `agents` 컬렉션의 문서라 스토어의 모든 읽기가 그것을 넣을지 정해야 한다. 이 표가 그
선택이다. 기본 보기는 미리 보기를 빼고, 배포 일을 하는 쪽은 `with_previews()` 의 보기로 넣는다
(VIEW). 리컨실·이름 검사·LLM 정리는 늘 넣고(ALWAYS), 참조 세기와 쉬기 워커는 늘 뺀다(NEVER).
미리 보기를 찾는 질의는 미리 보기만 읽는다(PREVIEWS). 쓰기는 부르는 쪽이 쥔 문서의 id 로 하므로
고르지 않는다(WRITES). 스토어에 메서드를 더하면 이 표에 넣어야 첫 시험이 지난다.
"""

from __future__ import annotations

import asyncio
import inspect
from collections.abc import Awaitable, Callable
from datetime import timedelta
from typing import Any

import pytest
from pymongo.errors import DuplicateKeyError

from daegam.services.agent_features import SCALE_TO_ZERO
from daegam.store.agent_previews import MongoAgentPreviewStore
from daegam.store.agents import (
    HOUSEKEEPING,
    Agent,
    AgentPreview,
    AgentRuntime,
    AgentSleep,
    LlmCleanup,
    MongoAgentStore,
)
from daegam.store.base import to_millis, utcnow
from daegam.store.exposure import AgentExposure
from daegam.store.tenants import Binding
from tests.helpers.aio import run_store
from tests.helpers.docs import TENANT, agent_doc, agent_refs_spec

pytestmark = pytest.mark.usefixtures("settings")

SOURCE_ID = "ag-source"
PREVIEW_ID = "0d6f1a3e-2c7b-4a59-9e0f-6f5b1c2d3e4f"
OWNER = "dev-1"
RUN_ID = "run-1"
HOST = "support.example.com"

VIEW, ALWAYS, NEVER, PREVIEWS = "view", "always", "never", "previews"
# 질의 → (선택, 그 질의를 부르고 받은 문서 id 의 집합을 내는 함수)
Query = Callable[[MongoAgentStore], Awaitable[set[str]]]


def _ids(docs: Any) -> set[str]:
    if docs is None:
        return set()
    if isinstance(docs, Agent):
        return {docs.id}
    if isinstance(docs, dict | set):
        return set(docs)
    return {d.id for d in docs}


async def _preview_of_run(store: MongoAgentStore) -> set[str]:
    """미리 보기를 평가 실행의 것으로 표시하고 그 실행으로 찾는다 (P45 9절)."""
    await store.coll.update_one({"_id": PREVIEW_ID}, {"$set": {"preview.run": RUN_ID}})
    return _ids(await store.preview_of_run(TENANT, SOURCE_ID, RUN_ID))


async def _sleep_of(store: MongoAgentStore) -> set[str]:
    return {PREVIEW_ID} if await store.sleep_of(PREVIEW_ID) is not None else set()


async def _housekeeping(store: MongoAgentStore) -> set[str]:
    """세는 질의라 수를 id 로 바꾼다. 원본 하나면 원본, 둘이면 미리 보기도 든 것이다."""
    held = await store.housekeeping_wakes("c1", since_after=utcnow() - timedelta(hours=1))
    return [set(), {SOURCE_ID}, {SOURCE_ID, PREVIEW_ID}][held]


QUERIES: dict[str, tuple[str, Query]] = {
    "get": (VIEW, lambda s: _wrap(s.get(PREVIEW_ID))),
    "get_by_name": (VIEW, lambda s: _wrap(s.get_by_name(TENANT, PREVIEW_ID))),
    "list": (VIEW, lambda s: _wrap(s.list(TENANT, include_draft=True))),
    "get_many": (VIEW, lambda s: _wrap(s.get_many([SOURCE_ID, PREVIEW_ID]))),
    "existing_ids": (VIEW, lambda s: _wrap(s.existing_ids([SOURCE_ID, PREVIEW_ID]))),
    "live": (VIEW, lambda s: _wrap(s.live())),
    "live_on_host": (VIEW, lambda s: _wrap(s.live_on_host(HOST))),
    "live_on_cluster": (VIEW, lambda s: _wrap(s.live_on_cluster("c1"))),
    "live_binding_data_service": (VIEW, lambda s: _wrap(s.live_binding_data_service("ds-1"))),
    "sleep_of": (VIEW, _sleep_of),
    "stale": (ALWAYS, lambda s: _wrap(s.stale(["deployed"], utcnow()))),
    "live_by_k8s_name": (
        ALWAYS,
        lambda s: _wrap(s.live_by_k8s_name("c1", "daegam-acme", f"agent-{PREVIEW_ID}")),
    ),
    "with_llm_cleanup": (ALWAYS, lambda s: _wrap(s.with_llm_cleanup())),
    "referencing_data_service": (NEVER, lambda s: _wrap(s.referencing_data_service("ds-1"))),
    "referencing_tool_source": (
        NEVER,
        lambda s: _wrap(s.referencing_tool_source("mcp_instance", "mcp-1")),
    ),
    "revisions_referencing_tool_source": (
        NEVER,
        lambda s: _wrap(s.revisions_referencing_tool_source("mcp_instance", "mcp-1")),
    ),
    "referencing_knowledge_base": (NEVER, lambda s: _wrap(s.referencing_knowledge_base("kb-1"))),
    "revisions_referencing_knowledge_base": (
        NEVER,
        lambda s: _wrap(s.revisions_referencing_knowledge_base("kb-1")),
    ),
    "referencing_skill": (NEVER, lambda s: _wrap(s.referencing_skill("sk-1"))),
    "sleep_candidates": (
        NEVER,
        lambda s: _wrap(
            s.sleep_candidates(idle_before=None, stale_before=utcnow(), capability=SCALE_TO_ZERO)
        ),
    ),
    "housekeeping_wakes": (NEVER, _housekeeping),
    "preview_of": (PREVIEWS, lambda s: _wrap(s.preview_of(TENANT, SOURCE_ID, OWNER))),
    "preview_of_run": (PREVIEWS, _preview_of_run),
    "previews_of": (PREVIEWS, lambda s: _wrap(s.previews_of(TENANT, SOURCE_ID))),
    "previews_of_owner": (PREVIEWS, lambda s: _wrap(s.previews_of_owner(OWNER))),
    "all_previews": (PREVIEWS, lambda s: _wrap(s.all_previews())),
}
# 부르는 쪽이 쥔 문서의 id 로 쓰는 것과 스키마·보기
WRITES = frozenset(
    {
        "create",
        "set_fields",
        "mark_stale",
        "transition",
        "delete",
        "delete_draft",
        "add_llm_token",
        "drop_llm_tokens",
        "add_llm_cleanup",
        "drop_llm_cleanup",
        "drop_llm_secrets",
        "record_runtime",
        "end_deploy_record",
        "set_evaluation_gate",
        "move_sleep",
        "mark_activity",
        "start_window",
        "clear_sleep",
        "set_phase",
    }
)
OTHER = frozenset({"init_schema", "with_previews"})


async def _wrap(result: Awaitable[Any]) -> set[str]:
    return _ids(await result)


def _pair() -> tuple[Agent, Agent]:
    """같은 모양의 원본과 그 미리 보기. 모든 질의에 맞는다."""
    now = to_millis(utcnow())
    old = now - timedelta(hours=1)
    common: dict[str, Any] = {
        "state": "deployed",
        "spec": agent_refs_spec(
            source=("mcp_instance", "mcp-1"), kb_id="kb-1", ds_id="ds-1", skill=("sk-1", 1)
        ),
        "bindings": [Binding(data_service_id="ds-1", env_prefix="DAEGAM_DB_0")],
        "llm_cleanup": [LlmCleanup(revoke=["j-1"])],
        "exposure": AgentExposure(host=HOST),
        "runtime": AgentRuntime(image="img", features=[SCALE_TO_ZERO]),
        "sleep": AgentSleep(state="waking", reason=HOUSEKEEPING, since=now),
        "updated_at": old,
    }
    source = agent_doc(SOURCE_ID, name="support", **common)
    preview = agent_doc(
        PREVIEW_ID,
        **common,
        preview=AgentPreview(
            source=SOURCE_ID,
            source_name="support",
            owner=OWNER,
            started_at=now,
            ends_at=now + timedelta(hours=2),
        ),
    )
    return source, preview


def test_every_store_method_has_a_choice():
    """새 질의가 미리 보기를 넣을지 고르지 않고 들어오지 못한다."""
    methods = {
        name
        for name, member in inspect.getmembers(MongoAgentStore, inspect.isfunction)
        if not name.startswith("_")
    }
    assert methods == set(QUERIES) | WRITES | OTHER


@pytest.mark.parametrize("name", sorted(QUERIES))
def test_each_query_follows_its_choice(settings, name):
    """기본 보기와 배포 일의 보기에서 미리 보기가 드는지가 표의 선택과 같다. 원본은 PREVIEWS 말고
    어디서나 든다."""
    choice, query = QUERIES[name]

    async def go(store: MongoAgentStore) -> tuple[set[str], set[str]]:
        for doc in _pair():
            await store.create(doc)
        return await query(store), await query(store.with_previews())

    hidden, shown = run_store(settings, MongoAgentStore, go)
    expected = {
        VIEW: (False, True),
        ALWAYS: (True, True),
        NEVER: (False, False),
        PREVIEWS: (True, True),
    }[choice]
    assert (PREVIEW_ID in hidden, PREVIEW_ID in shown) == expected
    if choice == PREVIEWS:
        assert SOURCE_ID not in hidden | shown
    elif name not in ("get", "get_by_name", "sleep_of", "live_by_k8s_name"):
        # 이 넷은 미리 보기의 id·이름으로 묻는다
        assert SOURCE_ID in hidden and SOURCE_ID in shown


def test_a_person_holds_one_preview_per_agent_and_other_documents_carry_no_preview(settings):
    """부분 고유 인덱스가 (원본, 사람)마다 미리 보기 하나만 둔다. 미리 보기가 아닌 문서는 `preview`
    칸이 없어 인덱스에 들지 않는다."""

    async def go(store: MongoAgentStore) -> dict[str, Any]:
        source, preview = _pair()
        await store.create(source)
        await store.create(preview)
        twin = preview.model_copy(update={"id": "1d6f1a3e-2c7b-4a59-9e0f-6f5b1c2d3e4f"})
        twin.name = twin.id
        with pytest.raises(DuplicateKeyError):
            await store.create(twin)
        other = twin.model_copy(update={"preview": twin.preview.model_copy(update={"owner": "b"})})
        await store.create(other)
        await store.create(agent_doc("ag-other", name="other"))
        raw = await store.coll.find_one({"_id": "ag-other"})
        return {"raw": raw, "count": await store.coll.count_documents({})}

    out = run_store(settings, MongoAgentStore, go)
    assert "preview" not in out["raw"] and out["count"] == 4


def test_previews_carry_the_source_id_and_subject():
    """규칙과 토큰이 쓰는 id 와 서비스 주체의 이름은 미리 보기면 원본의 것이다 (5절, 9절)."""
    source, preview = _pair()
    assert (source.acting_id, source.acting_k8s_name) == (SOURCE_ID, "agent-support")
    assert (preview.acting_id, preview.acting_k8s_name) == (SOURCE_ID, "agent-support")
    assert preview.k8s is not None and preview.k8s.name == f"agent-{PREVIEW_ID}"


def test_two_starts_for_the_last_slot_take_it_once(settings):
    """테넌트의 마지막 자리를 둘이 함께 잡으면 하나만 잡는다. 놓으면 다시 잡힌다 (10절)."""

    async def go(store: MongoAgentPreviewStore) -> dict[str, Any]:
        now = utcnow()
        first = await store.take_slot(TENANT, "p-0", 2, now)
        both = await asyncio.gather(
            store.take_slot(TENANT, "p-1", 2, now), store.take_slot(TENANT, "p-2", 2, now)
        )
        released = await store.release_slot("p-1")
        again = await store.take_slot(TENANT, "p-3", 2, now)
        other_tenant = await store.take_slot("other", "p-4", 2, now)
        return {
            "first": first,
            "both": sorted(both, key=lambda n: (n is None, n)),
            "released": released,
            "again": again,
            "other": other_tenant,
            "slots": [(s.id, s.preview_id) for s in await store.all_slots()],
        }

    out = run_store(settings, MongoAgentPreviewStore, go)
    assert out["first"] == 0 and out["both"] == [1, None]
    assert out["released"] == 1 and out["again"] == 1 and out["other"] == 0
    assert out["slots"] == [("acme:0", "p-0"), ("acme:1", "p-3"), ("other:0", "p-4")]


def test_starts_count_per_person_and_tenant_by_utc_day(settings):
    async def go(store: MongoAgentPreviewStore) -> list[int]:
        now = utcnow()
        counts = [(await store.count_start(TENANT, OWNER, now))[0] for _ in range(3)]
        counts.append((await store.count_start("other", OWNER, now))[0])
        counts.append((await store.count_start(TENANT, "dev-2", now))[0])
        return counts

    assert run_store(settings, MongoAgentPreviewStore, go) == [1, 2, 3, 1, 1]


def test_the_last_end_of_a_preview_is_kept_until_the_next_start(settings):
    async def go(store: MongoAgentPreviewStore) -> list[Any]:
        now = utcnow()
        seen: list[Any] = [await store.end_of(TENANT, SOURCE_ID, OWNER)]
        for reason in ("idle", "max_age"):
            await store.record_end(
                tenant=TENANT,
                source=SOURCE_ID,
                owner=OWNER,
                preview_id=PREVIEW_ID,
                reason=reason,  # type: ignore[arg-type]
                by=None,
                now=now,
            )
        end = await store.end_of(TENANT, SOURCE_ID, OWNER)
        seen.append(end.reason if end else None)
        await store.forget_end(TENANT, SOURCE_ID, OWNER)
        seen.append(await store.end_of(TENANT, SOURCE_ID, OWNER))
        return seen

    assert run_store(settings, MongoAgentPreviewStore, go) == [None, "max_age", None]
