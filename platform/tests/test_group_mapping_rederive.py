"""Group Mapping 을 바꾸면 사람들의 소속 줄을 그 자리에서 다시 푼다 (P53b 6.3절).

사람 기록이 있는 사람은 기록의 그룹을 새 표로 다시 풀고, 빠진 테넌트는 `membership`, 내려간 역할은
`role_changed` 훅을 부른다. 기록이 없는 사람은 매핑이 빠졌거나 역할이 내려간 테넌트의 okta_group
줄만 잃고, 매핑을 더하거나 역할을 올리면 아무것도 잃지 않는다. 요청이 끝내지 못한 것은 리컨실
단계가 리비전으로 마저 한다. 매핑과 소속 줄의 문서는 2.0.0-rc1 의 모델(extra=forbid)이 읽는
모양 그대로다.
"""

from __future__ import annotations

from typing import Any

import pytest
from fastapi.testclient import TestClient
from pydantic import BaseModel, ConfigDict

import daegam.services.memberships as membership_service
from daegam.store.base import utcnow
from tests.helpers.access import plant, revoked, run_step, sign_in
from tests.helpers.auth import bearer
from tests.helpers.consts import GROUP_MAPPINGS

ADMIN_MAPPING = {"group": "daegam-admins", "tenant": "*", "role": "platform-admin"}


class Rc1Membership(BaseModel):
    """2.0.0-rc1 의 소속 줄 모델. 남는 필드를 받지 않는다."""

    model_config = ConfigDict(extra="forbid")

    sub: str
    tenant: str
    role: str
    source: str
    updated_at: Any


class Rc1Mapping(BaseModel):
    model_config = ConfigDict(extra="forbid")

    group: str
    tenant: str
    role: str


@pytest.fixture
def admin(oidc, token) -> dict[str, str]:
    return bearer(token("root", "daegam-admins"))


@pytest.fixture
def lost(oidc) -> list[tuple[str, str, str | None]]:
    """회수 훅이 실행 소스에 알린 (sub, 까닭, 테넌트)."""
    calls: list[tuple[str, str, str | None]] = []

    async def on_lost(sub: str, reason: str, tenant: str | None) -> None:
        calls.append((sub, reason, tenant))

    oidc.app.state.membership_service.revoker.on_lost = on_lost
    return calls


def rows(mongo, sub: str) -> dict[str, str]:
    docs = list(mongo["memberships"].find({"sub": sub}, {"_id": 0}))
    for d in docs:
        Rc1Membership.model_validate(d)
    return {d["tenant"]: d["role"] for d in docs}


def put(c: TestClient, admin: dict[str, str], mappings: list[dict[str, str]]) -> None:
    r = c.put("/group-mappings", json=[ADMIN_MAPPING, *mappings], headers=admin)
    assert r.status_code == 200, r.text


def mappings_but(*groups: str, **changed: str) -> list[dict[str, str]]:
    """GROUP_MAPPINGS 에서 groups 를 빼고 changed(그룹 → 역할)의 역할을 바꾼 표. 관리자 매핑은
    put 이 더한다."""
    out = []
    for m in GROUP_MAPPINGS:
        if m["group"] in groups or m == ADMIN_MAPPING:
            continue
        out.append({**m, "role": changed.get(m["group"], m["role"])})
    return out


def old_timer(mongo, sub: str, **tenants: str) -> None:
    """업그레이드 전에 로그인해 기록이 없는 사람의 okta_group 줄."""
    for tenant, role in tenants.items():
        mongo["memberships"].insert_one(
            {
                "sub": sub,
                "tenant": tenant,
                "role": role,
                "source": "okta_group",
                "updated_at": utcnow(),
            }
        )


def test_people_with_a_record_are_rederived_and_hooks_run(oidc, token, admin, lost, mongo):
    """p1 은 a 의 developer 와 b 의 developer 다. b 의 매핑을 빼고 a 의 역할을 내리면 b 의 줄이
    빠지고 a 는 viewer 가 되며, b 의 토큰과 연결은 `membership`, a 의 토큰은 `role_changed` 로
    회수된다."""
    sign_in(oidc, token, "p1", "daegam-a-dev", "daegam-b-dev")
    in_a, in_b = plant(oidc, mongo, "p1", "a"), plant(oidc, mongo, "p1", "b")
    put(oidc, admin, mappings_but("daegam-b-dev", **{"daegam-a-dev": "viewer"}))
    assert rows(mongo, "p1") == {"a": "viewer"}
    assert revoked(mongo, in_b) == ("membership", None)
    assert revoked(mongo, in_a) == ("membership", "role_changed")
    assert ("p1", "membership", "b") in lost and all(t != "a" for _s, _r, t in lost)
    state = mongo["group_mapping_state"].find_one({})
    assert mongo["idp_people"].find_one({"_id": "p1"})["mapping_rev"] == state["revision"] > 0
    for m in mongo["group_mappings"].find({}, {"_id": 0}):
        Rc1Mapping.model_validate(m)


def test_people_without_a_record_lose_only_removed_or_lowered_tenants(oidc, admin, lost, mongo):
    """기록 없는 사람은 그룹을 모르므로 매핑이 빠졌거나 역할이 내려간 테넌트의 줄만 잃는다. 더하거나
    올린 매핑은 아무것도 지우지 않고 실행도 멈추지 않는다. manual 줄은 남는다."""
    old_timer(mongo, "old", a="developer", b="developer")
    mongo["memberships"].insert_one(
        {"sub": "kept", "tenant": "b", "role": "viewer", "source": "manual", "updated_at": utcnow()}
    )
    put(
        oidc,
        admin,
        [*mappings_but(), {"group": "daegam-c-dev", "tenant": "c", "role": "developer"}],
    )
    put(oidc, admin, mappings_but(**{"daegam-a-view": "tenant-admin"}))
    assert rows(mongo, "old") == {"a": "developer", "b": "developer"} and lost == []
    put(oidc, admin, mappings_but("daegam-b-dev", **{"daegam-a-view": "tenant-admin"}))
    assert rows(mongo, "old") == {"a": "developer"} and rows(mongo, "kept") == {"b": "viewer"}
    assert lost == [("old", "membership", "b")]
    assert mongo["group_mapping_state"].find_one({})["unrecorded"] == []


def test_the_first_state_is_todays_table_and_pauses_nothing(oidc, mongo):
    """업그레이드 뒤 첫 리컨실은 지금 표를 리비전 0 으로 적고 아무도 다시 풀지 않는다."""
    old_timer(mongo, "old", a="developer")
    assert run_step(oidc, "group_mappings") == {}
    state = mongo["group_mapping_state"].find_one({})
    assert state["revision"] == 0 and state["unrecorded"] == []
    assert rows(mongo, "old") == {"a": "developer"}


def test_a_rederivation_cut_short_is_finished_by_the_reconcile_step(
    oidc, token, admin, lost, mongo, monkeypatch
):
    """요청이 시간 안에 아무도 다시 풀지 못해도 리비전은 올랐다. 리컨실 단계가 리비전이 낮은 사람과
    남은 테넌트를 마저 한다. 표를 바꾼 뒤 리비전을 올리기 전에 멈춘 요청과, 2.0.0-rc1 레플리카가
    바꾼 표도 다음 단계가 알아본다."""
    sign_in(oidc, token, "p1", "daegam-a-dev", "daegam-b-dev")
    old_timer(mongo, "old", b="developer")
    monkeypatch.setattr(membership_service, "REQUEST_BUDGET", 0.0)
    put(oidc, admin, mappings_but("daegam-b-dev"))
    assert rows(mongo, "p1") == {"a": "developer", "b": "developer"}
    assert rows(mongo, "old") == {"b": "developer"}
    result = run_step(oidc, "group_mappings")
    assert result == {"rederived": ["p1"], "dropped": ["old/b"]}
    assert rows(mongo, "p1") == {"a": "developer"} and rows(mongo, "old") == {}
    assert sorted(lost) == [("old", "membership", "b"), ("p1", "membership", "b")]
    # 리비전을 올리지 않고 표만 바뀌었다
    mongo["group_mappings"].delete_many({"group": "daegam-a-dev"})
    assert run_step(oidc, "group_mappings") == {"rederived": ["p1"]}
    assert rows(mongo, "p1") == {}


@pytest.mark.parametrize("cleared", [False, True])
def test_a_record_without_groups_is_treated_like_no_record(oidc, token, admin, mongo, cleared):
    """groups 클레임 없는 토큰만 본 사람의 기록과 비운 기록은 그룹을 모른다. 그 사람의 줄은 다시
    풀지 않고 빠진 테넌트의 것만 잃는다. 매핑을 더해도 아무것도 잃지 않는다."""
    sign_in(oidc, token, "p1", "daegam-a-dev")
    fields: dict[str, Any] = {"groups": None, **({"cleared_at": utcnow()} if cleared else {})}
    mongo["idp_people"].update_one({"_id": "p1"}, {"$set": fields})
    old_timer(mongo, "p1", b="developer")
    put(oidc, admin, [*mappings_but(), {"group": "new", "tenant": "c", "role": "viewer"}])
    assert rows(mongo, "p1") == {"a": "developer", "b": "developer"}
    put(oidc, admin, mappings_but("daegam-b-dev"))
    assert rows(mongo, "p1") == {"a": "developer"}


def test_a_pass_skips_people_already_stamped_and_stops_when_the_revision_moves(
    oidc, token, admin, lost, mongo, monkeypatch
):
    """다른 차례가 이 리비전 이상으로 찍은 사람은 건너뛴다. 차례 중에 리비전이 오르면 멈춘다. 늦은
    차례가 앞 리비전의 줄을 새 리비전으로 찍힌 사람에게 덮어쓰지 않게 하려는 것이다."""
    sign_in(oidc, token, "p1", "daegam-a-dev", "daegam-b-dev")
    sign_in(oidc, token, "p2", "daegam-b-dev")
    monkeypatch.setattr(membership_service, "REQUEST_BUDGET", 0.0)
    put(oidc, admin, mappings_but("daegam-b-dev"))
    revision = mongo["group_mapping_state"].find_one({})["revision"]
    mongo["idp_people"].update_one({"_id": "p1"}, {"$set": {"mapping_rev": revision}})
    store = oidc.app.state.membership_service.store
    real = store.mapping_revision

    async def moved() -> int:
        return await real() + 1

    monkeypatch.setattr(store, "mapping_revision", moved)
    assert run_step(oidc, "group_mappings") == {}
    assert rows(mongo, "p2") == {"b": "developer"}
    monkeypatch.setattr(store, "mapping_revision", real)
    assert run_step(oidc, "group_mappings") == {"rederived": ["p2"]}
    assert rows(mongo, "p1") == {"a": "developer", "b": "developer"}
    assert rows(mongo, "p2") == {} and lost == [("p2", "membership", "b")]


def test_only_an_admins_put_may_empty_the_table(oidc, token, admin, lost, mongo):
    """2.0.0-rc1 레플리카의 교체는 지우고 넣는다. 그 사이에 리컨실이 읽은 빈 표는 리비전이 되지
    않는다. 그것을 리비전으로 삼으면 모두의 줄을 지운다. 관리자가 표를 비운 요청은 리비전이다."""
    sign_in(oidc, token, "p1", "daegam-a-dev")
    # 표가 비어도 플랫폼 관리자로 남도록 root 에게 manual `*` 줄을 준다
    mongo["memberships"].insert_one(
        {
            "sub": "root",
            "tenant": "*",
            "role": "platform-admin",
            "source": "manual",
            "updated_at": utcnow(),
        }
    )
    run_step(oidc, "group_mappings")
    mongo["group_mappings"].delete_many({})
    assert run_step(oidc, "group_mappings") == {}
    assert mongo["group_mapping_state"].find_one({})["revision"] == 0
    assert rows(mongo, "p1") == {"a": "developer"} and lost == []
    r = oidc.put("/group-mappings", json=[], headers=admin)
    assert r.status_code == 200, r.text
    assert mongo["group_mapping_state"].find_one({})["revision"] == 1
    assert rows(mongo, "p1") == {} and lost == [("p1", "membership", "a")]
