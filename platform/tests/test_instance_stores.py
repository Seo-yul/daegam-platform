"""인스턴스 스토어 셋(MCP Server·Gateway·Agent, D42)이 함께 지키는 계약.

셋은 옛 인스턴스 스토어의 프로토콜을 지키며 draft 를 뺀다. `list`·`live_*`·`stale` 은 옛 인스턴스
스토어 자리에 그대로 들어가므로 draft 를 보이지 않고 `include_draft=True` 목록만 draft 도 본다.
저장은 정의 쪽과 배포 쪽이 자기 필드만 조건부로 쓴다.

테스트마다 세 스토어로 한 번씩 돈다. 스토어마다 다른 값과 그 스토어만의 단정은 표의 행
(`StoreCase`)에 있다. 한 스토어에만 있는 질의는 test_<리소스>_store.py 가 본다.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import dataclass, field
from functools import partial
from operator import attrgetter
from types import ModuleType
from typing import Any

import pytest
from pymongo.errors import DuplicateKeyError

from daegam.store import agents, gateways, mcp_servers
from daegam.store.base import fields_of
from daegam.store.tenants import Binding
from tests.helpers.aio import run_store
from tests.helpers.docs import agent_doc, gateway_doc, mcp_server_doc

# 배포 쪽 바인딩이 가리키게 하는 Data Service
BOUND_DS = "ds-1"


@dataclass(frozen=True)
class StoreCase:
    """표의 한 행. 스토어 하나에 계약을 돌리는 데 필요한 것과 그 스토어만의 단정이다."""

    name: str
    # 저장 단위(SHARED·DEFINITION·DEPLOYMENT_FIELDS)를 가진 스토어 모듈
    module: ModuleType
    store: Callable[[Any], Any]
    # tests/helpers/docs.py 의 문서 빌더. draft 가 아니면 배포 쪽 칸이 찬다
    build: Callable[..., Any]
    # 새 draft 가 셋의 공통 기본값 밖에 더 가진 값 (속성 경로 → 값)
    draft: dict[str, Any] = field(default_factory=dict)
    # 그 리소스만 정의 쪽에 두는 필드와 정의 저장이 쓸 값
    own_definition: dict[str, Any] = field(default_factory=dict)
    # 배포 쪽 바인딩이 BOUND_DS 를 가리키게 하는 필드. 바인딩이 없는 스토어는 None
    bound: dict[str, Any] | None = None


AGENT = StoreCase(
    "agent",
    agents,
    agents.MongoAgentStore,
    partial(agent_doc, layout={"main": {"x": 1, "y": 2}}),
    draft={
        "bindings": [],
        "llm_secrets": [],
        "token_id": None,
        "replicas": 1,  # draft 에서도 배포 설정은 있다
        "layout": {"main": agents.Point(x=1, y=2)},
    },
    own_definition={"layout": {"main": agents.Point(x=9, y=9)}},  # 캔버스 배치는 정의 쪽이다
    bound={"bindings": [Binding(data_service_id=BOUND_DS, env_prefix="DAEGAM_DB_0")]},
)
GATEWAY = StoreCase(
    "gateway",
    gateways,
    gateways.MongoGatewayStore,
    gateway_doc,
    draft={
        "deployed_backends": [],
        "deployed_conflict_resolution": "priority",
        "spec.conflict_resolution": "auto",  # 정의 쪽은 auto 일 수 있다
    },
)
MCP_SERVER = StoreCase(
    "mcp_server",
    mcp_servers,
    mcp_servers.MongoMCPServerStore,
    mcp_server_doc,
    bound={"effective_config": {"bindings": [{"data_service_id": BOUND_DS}]}},
)


def _cases(*cases: StoreCase) -> list[Any]:
    """parametrize 값. 테스트 id 는 스토어 이름이다."""
    return [pytest.param(case, id=case.name) for case in cases]


STORES = _cases(AGENT, GATEWAY, MCP_SERVER)
# 조건부 저장은 스토어마다 같은 도우미를 거치므로 한 스토어로 본다
CONDITIONAL_STORES = _cases(MCP_SERVER)


@pytest.mark.parametrize("case", STORES)
def test_model_defaults_and_document_roundtrip(case):
    d = case.build("d1")
    assert d.state == "draft" and d.is_deployed is False and d.deployed is None
    assert d.cluster_id is None and d.k8s is None and d.effective_config is None
    assert d.exposure.enabled is False  # draft 에서도 노출 설정은 늘 있다
    assert d.current.revision == 1 and d.get_revision(2) is None
    assert {path: attrgetter(path)(d) for path in case.draft} == case.draft
    live = case.build("d2", state="deployed")
    assert live.is_deployed and live.deployed is not None and live.deployed.revision == 1
    # 배포한 문서도 저장 모양으로 썼다 읽으면 그대로다. 지우지 않은 문서에는 deleted_at 이 없다
    doc = live.to_doc()
    assert doc["state"] == "deployed" and doc["exposure"]["enabled"] is False
    assert "deleted_at" not in doc
    assert type(live).from_doc(doc) == live


@pytest.mark.parametrize("case", STORES)
def test_field_sets_split_the_document_into_definition_and_deployment(case):
    """두 저장 단위의 합이 문서 전체다. 필드를 더하면 어느 쪽인지 정해야 한다. 겹치는 것은
    `updated_at` 뿐이다 (어느 쪽 저장이든 찍는다)."""
    m = case.module
    doc = case.build("a")
    # 저장 이름 기준이다. 문서의 `id` 는 Mongo 에 `_id` 로 저장되므로 합에도 `_id` 로 든다
    every = {"_id"} | (set(type(doc).model_fields) - {"id"})
    # 저장 단위 밖에서 자기 조건부 갱신만 쓰는 칸이다 (Agent 의 쉬는 상태, P44 7절)
    own = getattr(m, "OWN_UPDATE_FIELDS", frozenset())
    assert m.SHARED_FIELDS | m.DEFINITION_FIELDS | m.DEPLOYMENT_FIELDS | own == every
    assert m.DEFINITION_FIELDS & m.DEPLOYMENT_FIELDS == {"updated_at"}
    assert not m.SHARED_FIELDS & (m.DEFINITION_FIELDS | m.DEPLOYMENT_FIELDS)
    assert not own & (m.SHARED_FIELDS | m.DEFINITION_FIELDS | m.DEPLOYMENT_FIELDS)
    assert set(fields_of(doc, m.DEPLOYMENT_FIELDS)) == m.DEPLOYMENT_FIELDS
    assert "_id" not in fields_of(doc, m.DEFINITION_FIELDS)
    assert set(case.own_definition) <= m.DEFINITION_FIELDS


@pytest.mark.parametrize("case", CONDITIONAL_STORES)
def test_transition_and_delete_draft_are_conditional_on_state(case, settings):
    """상태를 바꾸는 저장은 DB 의 상태가 기대와 같을 때만 쓴다. 검사와 쓰기 사이에 다른 요청이
    먼저 바꿨으면 0건이다 (같은 draft 에 배포 둘, 삭제와 배포, 해제와 재배포)."""

    async def go(store):
        draft = case.build("a")
        await store.create(draft)
        first = draft.model_copy(update={"state": "deploying", "cluster_id": "c1"})
        second = draft.model_copy(update={"state": "deploying"})  # 같은 draft 를 본 둘째 요청
        assert await store.transition(first, expect={"draft"}) is True
        assert await store.transition(second, expect={"draft"}) is False
        after = await store.get("a")
        assert after.state == "deploying" and after.cluster_id == "c1"
        # 진행 중 문서에는 draft 삭제가 걸리지 않는다
        assert await store.delete_draft("a") is False
        deployed = first.model_copy(update={"state": "deployed"})
        assert await store.transition(deployed, expect={"deploying"}) is True
        # 기대 집합이 여럿이면 그중 하나면 된다. draft 로 돌아오면 지울 수 있다
        assert await store.transition(draft, expect={"deployed", "failed"}) is True
        assert await store.delete_draft("a") is True
        assert await store.delete_draft("a") is False
        assert await store.get("a") is None

    run_store(settings, case.store, go)


@pytest.mark.parametrize("case", CONDITIONAL_STORES)
def test_set_fields_is_conditional_on_state_and_revision(case, settings):
    """`expect_state` 는 저장된 state 가 그 안에 있을 때만, `expect_revision` 은 저장된 revision 이
    그 값일 때만 쓴다. 조건에 어긋나면 0건이고 문서는 그대로다."""

    async def go(store):
        await store.create(case.build("a", state="deployed"))
        assert not await store.set_fields(
            "a", {"svc_token_rev": "x1"}, expect_state={"draft", "failed"}
        )
        assert await store.set_fields("a", {"svc_token_rev": "x2"}, expect_state={"deployed"})
        assert not await store.set_fields("a", {"updated_by": "u-x"}, expect_revision=2)
        assert await store.set_fields("a", {"updated_by": "u-y"}, expect_revision=1)
        assert await store.set_fields(
            "a", {"last_error": "e"}, expect_state={"deployed"}, expect_revision=1
        )
        assert not await store.set_fields("nope", {"last_error": None}, expect_state={"deployed"})
        assert await store.set_fields("a", {"cluster_id": "c9"})  # 조건이 없으면 그냥 쓴다
        return await store.get("a")

    after = run_store(settings, case.store, go)
    assert after.svc_token_rev == "x2" and after.updated_by == "u-y"
    assert after.last_error == "e" and after.cluster_id == "c9"


@pytest.mark.parametrize("fn_name", ["set_fields_where", "set_fields_guarded"])
def test_set_fields_where_refuses_reserved_conditions(fn_name) -> None:
    """`also` 가 `_id`·`state`·`$or`·`seq` 를 담으면 조용히 덮어쓰지 않고 ValueError 다. 질의는
    `set_fields_guarded` 한 곳에서 만들므로 두 함수가 같다 (P68)."""
    from daegam.store import base
    from daegam.store.base import RESERVED_CONDITIONS

    fn = getattr(base, fn_name)
    for key in sorted(RESERVED_CONDITIONS):
        with pytest.raises(ValueError, match=key.replace("$", r"\$")):
            asyncio.run(fn(None, "a", {"x": 1}, also={key: 1}))


@pytest.mark.parametrize("case", CONDITIONAL_STORES)
def test_delete_and_unique_name(case, settings):
    """이름은 테넌트 안에서 하나다. `delete` 는 상태와 상관없이 지운다."""

    async def go(store):
        await store.create(case.build("a", name="bot", state="deployed"))
        with pytest.raises(DuplicateKeyError):
            await store.create(case.build("b", name="bot"))
        await store.create(case.build("c", name="bot", tenant="beta"))
        return await store.delete("a"), await store.delete("a"), await store.get("a")

    first, second, gone = run_store(settings, case.store, go)
    assert first is True and second is False and gone is None
