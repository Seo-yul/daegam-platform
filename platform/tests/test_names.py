"""P10 F1: 표시 이름과 K8s 이름 분리 (D24).

이름 규칙의 경계, display_name 기본값·trim·상한, name 불변(같은 이름 통과, 옛 형식 이름 문서도),
모델을 만들 때의 display_name 기본값, 게시 기본 이름.
"""

from __future__ import annotations

import pytest
from fastapi import HTTPException
from pydantic import ValidationError

from daegam.api._common import check_tenant
from daegam.names import NAME_MAX, is_valid_name
from daegam.store.hub import HubItem, HubK8s
from tests.helpers.backend_apis import uploaded_backend
from tests.helpers.clusters import default_cluster
from tests.helpers.consts import HUB, MS
from tests.helpers.http import detail_code
from tests.helpers.mcp_servers import clone_and_deploy, mcp_server_env

REMOTE_URL = "http://spike.svc:8080/mcp"
# 규칙 밖의 이름: 빈 값, 공백뿐, 상한보다 한 자 긴 이름, 양끝·연속 하이픈, 대문자,
# 밑줄·점, 한글, 가운데 공백
BAD_NAMES = (
    "",
    "   ",
    "a" * (NAME_MAX + 1),
    "-calc",
    "calc-",
    "a--b",
    "Calc",
    "My_Calc.v2",
    "계산기",
    "a b",
)
# 규칙 안의 경계: 한 글자, 숫자뿐, 하이픈 여럿, 끝 가까운 하이픈, 상한 길이
GOOD_NAMES = ("a", "0", "a-b-c", "a" * (NAME_MAX - 2) + "-b", "a" * NAME_MAX)


def _code(exc: pytest.ExceptionInfo[HTTPException]) -> str:
    return exc.value.detail["code"]  # type: ignore[index]


# -- 규칙 ---------------------------------------------------------------------------------


@pytest.mark.parametrize("name", ["demo\n", "demo\n\n", "\ndemo"])
def test_is_valid_name_rejects_line_breaks(name):
    """`$` 는 끝 줄바꿈 하나 앞에서도 맞으므로 규칙 검사는 fullmatch 다. 앞뒤 공백을 지우지 않는
    자리(Skill 머리말의 name, 경로의 테넌트)가 "demo\\n" 을 받으면 그 이름의 리소스가 생긴다."""
    assert not is_valid_name(name)
    with pytest.raises(HTTPException) as exc:
        check_tenant(name)
    assert exc.value.status_code == 400 and _code(exc) == "invalid_tenant"


# -- API: 생성·갱신·폴백 ------------------------------------------------------------------


def _hub(client, name, **extra):
    return client.post(HUB, json={"name": name, "kind": "remote", "url": REMOTE_URL, **extra})


def test_create_applies_rule_and_display_name_default(client, fake_clusters):
    default_cluster(client)
    r = _hub(client, "  calc  ")
    assert r.status_code == 201, r.text
    assert (r.json()["name"], r.json()["display_name"]) == ("calc", "calc")
    r = _hub(client, "calc-2", display_name="  계산기 SOAP  ")
    assert r.status_code == 201, r.text
    assert (r.json()["name"], r.json()["display_name"]) == ("calc-2", "계산기 SOAP")
    for bad in BAD_NAMES:
        r = _hub(client, bad)
        assert detail_code(r) == (422, "invalid_name"), bad
    for good in GOOD_NAMES:
        assert _hub(client, good).status_code == 201, good
    r = _hub(client, "calc-3", display_name="   ")
    assert detail_code(r) == (422, "invalid_display_name")
    r = _hub(client, "calc-3", display_name="x" * 81)
    assert detail_code(r) == (422, "invalid_display_name")
    assert _hub(client, "calc-3", display_name="x" * 80).status_code == 201


def test_put_same_name_passes_even_for_legacy_name(client, mongo):
    """옛 형식 이름으로 저장된 문서도 같은 이름을 보내면 200 이다."""
    bp = uploaded_backend(client)
    p = client.post(
        MS, json={"name": "calc-mcp", "backend_profile_id": bp["id"], "spec_version": 1}
    ).json()
    assert p["display_name"] == "calc-mcp"
    mongo["mcp_servers"].update_one({"_id": p["id"]}, {"$set": {"name": "My_Calc.v2"}})
    body = {"name": "My_Calc.v2", "backend_profile_id": bp["id"], "spec_version": 1}
    r = client.put(f"{MS}/{p['id']}", json=body)
    assert r.status_code == 200, r.text
    assert r.json()["revision"] == 2 and r.json()["name"] == "My_Calc.v2"
    r = client.put(f"{MS}/{p['id']}", json={**body, "name": "my-calc-v2"})
    assert detail_code(r) == (422, "name_immutable")
    # display_name 은 갱신되고 검증된다. name 은 앞뒤 공백을 지우고 비교한다
    padded = {**body, "name": " My_Calc.v2 ", "display_name": " 계산기 "}
    r = client.put(f"{MS}/{p['id']}", json=padded)
    assert r.status_code == 200, r.text
    assert (r.json()["name"], r.json()["display_name"]) == ("My_Calc.v2", "계산기")
    r = client.put(f"{MS}/{p['id']}", json={**body, "display_name": "x" * 81})
    assert detail_code(r) == (422, "invalid_display_name")


def test_models_fill_display_name_from_name_only_when_built():
    """이름 하나로 만든 모델은 display_name 이 name 이다. 저장된 문서는 display_name 을 가져야 하고
    없으면 읽지 않는다."""
    k8s = HubK8s(kind="MCPRemoteProxy", name="hub-spike", namespace="ns")
    item = HubItem(tenant="t", name="spike", kind="remote", cluster_id="c1", k8s=k8s)
    assert item.display_name == "spike" and item.to_doc()["display_name"] == "spike"
    given = HubItem(
        tenant="t", name="spike", display_name="스파이크", kind="remote", cluster_id="c1", k8s=k8s
    )
    assert HubItem.from_doc(given.to_doc()).display_name == "스파이크"
    stored = item.to_doc()
    del stored["display_name"]
    with pytest.raises(ValidationError, match="display_name"):
        HubItem.from_doc(stored)


def test_instance_create_and_publish_carry_display_name(client, fake_clusters):
    cid, bp, ds, mp = mcp_server_env(client)
    r = clone_and_deploy(client, mp, display_name="계산기 인스턴스")
    assert r.status_code == 201, r.text
    inst = r.json()
    assert (inst["name"], inst["display_name"]) == ("calc-1", "계산기 인스턴스")
    assert inst["k8s"]["name"] == "mcp-calc-1"
    # 게시 기본값: name 은 인스턴스 name 그대로, display_name 은 인스턴스 display_name
    r = client.post(f"{MS}/{inst['id']}/publish", json={})
    assert r.status_code == 201, r.text
    assert (r.json()["name"], r.json()["display_name"]) == ("calc-1", "계산기 인스턴스")
    ref = client.get(f"{MS}/{inst['id']}").json()["hub_item"]
    assert (ref["name"], ref["display_name"]) == ("calc-1", "계산기 인스턴스")
    r = clone_and_deploy(client, mp, name="calc-2", display_name="   ")
    assert detail_code(r) == (422, "invalid_display_name")
