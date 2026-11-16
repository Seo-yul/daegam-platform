"""사람이 쓴 Cedar 글을 문장마다 정책 원소 하나로 (T63).

ToolHive 는 정책 목록의 원소 하나를 문장 하나로 읽고 그 뒤는 오류 없이 버린다. 그래서 렌더는
authz_policy 를 늘 문장마다 나누고(`split_statements`) 파싱하지 않는다.
저장과 배포는 문장마다 파싱한다(`parse_statements`). 파싱은 검사(`statement_code`)를 지난 글만
하위 프로세스의 Cedar 엔진이 하고, 확실한 판정만 422 authz_invalid 이며 판정을 내지 못하면 503
authz_not_ready 다. 살아 있는 Gateway 는 렌더 번호로 다시 렌더되어 나뉜 정책을 받는다. 틀린 문장이
섞인 저장 문서는 오퍼레이터가 거절해 재적용이 막히고 vMCP 는 앞의 정책으로 돈다.
"""

from __future__ import annotations

import asyncio
import sys

import pytest

from daegam.auth.cedar import (
    GATEWAY_TARGET,
    MAX_BRACKET_DEPTH,
    MAX_POLICY_CHARS,
    CedarSyntaxError,
    delegation_forbid,
    delegation_permit,
    group_policy,
    split_statements,
)
from daegam.k8s.templates.vmcp import vmcp_policies
from daegam.services import cedar_check
from daegam.services.cedar_check import CedarCheckUnavailable, parse_statements
from daegam.services.data_plane_auth import KIND_GATEWAY, RENDER_EPOCHS
from tests.helpers.consts import DEFAULT_NS, GV, GW
from tests.helpers.gateways import SURFACE_OFF, cedar_policies, create_gateway, gateway_up, vmcp_of
from tests.helpers.http import detail_code
from tests.helpers.hub import hub_env
from tests.helpers.workers import run_data_plane_auth

PERMIT = 'permit(principal, action == Action::"call_tool", resource);'
FORBID = "forbid(principal, action, resource) unless { principal has claim_tenant };"
TWO = f"{PERMIT}\n{FORBID}"
# 둘째 문장이 파싱되지 않는 글
BROKEN_TAIL = f"{PERMIT}\nforbid(principal, action, resource"
BROKEN_SECOND = "forbid(principal, action, resource"
# Cedar 엔진이 프로세스째 죽을 수 있는 입력 (겹친 괄호 700). 죽는지는 환경에 따라 다르므로 테스트는
# 깊이 상한이 엔진 앞에서 막는 것만 본다
DEEP = "permit(principal, action, resource) when { " + "(" * 700 + "true" + ")" * 700 + " };"
# 엔진은 받지만 그 결과를 파이썬이 읽기에 너무 깊은 16KiB 아래의 사슬
CHAIN = "permit(principal, action, resource) when { " + "+".join(["1"] * 8100) + " == 1 };"


def _parse(text: str) -> None:
    asyncio.run(parse_statements(text))


# -- 나누기 ----------------------------------------------------------------------------------------


def test_the_second_statement_becomes_its_own_element():
    """두 문장을 적은 authz_policy 의 둘째 문장이 자기 원소로 렌더된다. 한 원소에 두 문장을
    두면 ToolHive 는 첫 문장만 읽는다."""
    policies = vmcp_policies(
        tenant_group="g", agent_client_ids=[], extra_policy=TWO, client_id_claim="cid"
    )
    assert policies == [group_policy("g"), PERMIT, FORBID]


def test_the_render_splits_a_broken_statement_too():
    """렌더는 파싱하지 않는다. 틀린 둘째 문장도 자기 원소가 되고 오퍼레이터가 판정한다."""
    policies = vmcp_policies(
        tenant_group="g", agent_client_ids=[], extra_policy=BROKEN_TAIL, client_id_claim="cid"
    )
    assert policies == [group_policy("g"), PERMIT, BROKEN_SECOND]


def test_semicolons_and_braces_in_strings_and_comments_do_not_split():
    tricky = (
        "// 머리 주석; } 은 문장이 아니다\n"
        'permit(principal, action, resource) when { principal.claim_sub == "a;b}\\";c" };'
        " // 같은 줄 주석; }\n"
        "/* 블록 주석; }\n 두 줄 */ forbid(principal, action, resource)\n"
        '  when { principal has claim_x && principal.claim_x like "x;*" };'
    )
    first, second = split_statements(tricky)
    assert first == (
        "// 머리 주석; } 은 문장이 아니다\n"
        'permit(principal, action, resource) when { principal.claim_sub == "a;b}\\";c" };'
        " // 같은 줄 주석; }"
    )
    assert second.startswith("/* 블록 주석") and second.endswith('like "x;*" };')
    _parse(tricky)


def test_comments_and_empty_statements_are_not_elements_and_a_tail_is_kept():
    assert split_statements(None) == split_statements("  \n") == []
    assert split_statements("// 주석뿐\n/* 그리고 */") == []
    assert split_statements(f"{PERMIT};\n;\n// 끝") == [PERMIT]
    assert split_statements(BROKEN_TAIL) == [PERMIT, BROKEN_SECOND]


# -- 파싱 ------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "message"),
    [
        (BROKEN_TAIL, "statement 2"),
        (f"{PERMIT} forbid(principal, actoin, resource);", "statement 2"),
        ("pernit(principal, action, resource);", "statement 1"),
        ('permit(principal, action, resource) when { principal.x == "a };', "not closed"),
        ("permit(principal, action, resource); /* 닫지 않은 주석", "not closed"),
        ("permit(principal == ?principal, action, resource);", "template"),
        (DEEP, f"deeper than {MAX_BRACKET_DEPTH}"),
        (CHAIN, "nests too deeply"),
        (PERMIT + "\n" + "// x" * MAX_POLICY_CHARS, "longer than"),
        ('permit(principal, action, resource) when { "a\0" == "b" };', "NUL"),
        ('permit(principal, action, resource) when { "\ud800" == "b" };', "surrogate"),
    ],
)
def test_text_that_does_not_parse_is_refused(text, message):
    with pytest.raises(CedarSyntaxError, match=message):
        _parse(text)


@pytest.mark.parametrize("space", ["\u00a0", "\u3000", "\u2028", "\x0b", "\x0c"])
def test_whitespace_cedar_go_refuses_is_refused_outside_strings(space):
    """Cedar 엔진은 받고 cedar-go 는 거절하는 공백이다. 저장이 받으면 배포가 거절한다."""
    with pytest.raises(CedarSyntaxError, match=f"U\\+{ord(space):04X}"):
        _parse(f"permit(principal,{space}action, resource);")
    # 문자열과 주석 안에서는 글자다
    _parse(f'permit(principal, action, resource) when {{ "a{space}b" == "c" }}; // {space}')


def test_a_text_outside_the_bmp_fits_the_worker_input():
    _parse('permit(principal, action, resource) when { "' + "😀" * 8000 + '" == "a" };')


def test_a_parser_that_cannot_run_never_passes_a_save(client, fake_clusters, monkeypatch):
    """하위 프로세스가 준비 전에 끝나면(없는 모듈) 판정이 없다. 맞다고도 틀렸다고도 보지 않고 503
    이며 아무것도 저장하지 않는다."""
    _cid, items, *_rest = hub_env(client, fake_clusters)
    missing = (sys.executable, "-I", "-m", "daegam.auth.no_such_worker")
    monkeypatch.setattr(cedar_check, "WORKER_ARGV", missing)
    with pytest.raises(CedarCheckUnavailable):
        _parse(TWO)
    r = create_gateway(client, items, authz_policy=TWO)
    assert detail_code(r) == (503, "authz_not_ready"), r.text
    assert client.get(GW).json() == []
    monkeypatch.setattr(cedar_check, "WORKER_ARGV", ("/nonexistent/python",))
    assert detail_code(create_gateway(client, items, authz_policy=TWO)) == (503, "authz_not_ready")


def test_at_most_two_workers_run_at_once(monkeypatch):
    running, peak = 0, 0

    async def worker(*_args, **_kw):
        nonlocal running, peak
        running += 1
        peak = max(peak, running)
        await asyncio.sleep(0.05)
        running -= 1
        return {}

    async def five():
        monkeypatch.setattr(cedar_check, "_workers", asyncio.Semaphore(cedar_check.MAX_WORKERS))
        await asyncio.gather(*(parse_statements(PERMIT) for _ in range(5)))

    monkeypatch.setattr(cedar_check, "run_worker", worker)
    asyncio.run(five())
    assert peak == cedar_check.MAX_WORKERS == 2


def test_a_save_with_invalid_cedar_is_refused(client, fake_clusters):
    """만들기와 수정이 authz_policy 를 문장마다 파싱한다. 틀리면 422 authz_invalid 다. 길이 상한은
    본문 검증이 먼저 막는다."""
    _cid, items, *_rest = hub_env(client, fake_clusters)
    r = create_gateway(client, items, authz_policy=BROKEN_TAIL)
    assert detail_code(r) == (422, "authz_invalid"), r.text
    assert r.json()["detail"]["path"] == "authz_policy"
    assert "statement 2" in r.json()["detail"]["detail"]
    r = create_gateway(client, items, authz_policy="x" * (MAX_POLICY_CHARS + 1))
    assert r.status_code == 422 and "authz_policy" in r.text
    r = create_gateway(client, items, authz_policy=TWO)
    assert r.status_code == 201, r.text
    body = {"name": "a", "backends": r.json()["backends"], "authz_policy": BROKEN_TAIL}
    put = client.put(f"{GW}/{r.json()['id']}", json=body)
    assert detail_code(put) == (422, "authz_invalid"), put.text


# -- 살아 있는 Gateway -----------------------------------------------------------------------------


def _bound_forbid(gateway_id: str) -> str:
    """local 모드 vMCP 의 맨 끝 정책. 이 Gateway 에 묶이지 않은 위임 토큰의 forbid 다
    (P53 3.1절)."""
    return delegation_forbid(GATEWAY_TARGET.format(gateway_id), "default")


def _old_render(fake, mongo, gateway_id: str, policies: list[str], text: str) -> None:
    """고치기 전의 렌더로 되돌린다. authz_policy 전체가 원소 하나이고 앞 판의 렌더 번호다."""
    config = fake.objects[(GV, "MCPAuthzConfig", DEFAULT_NS, "gw-a-authz")]
    config["spec"]["config"]["policies"] = [
        *policies[:-5],
        text,
        *SURFACE_OFF,
        _bound_forbid(gateway_id),
    ]
    mongo["gateways"].update_one(
        {"_id": gateway_id}, {"$set": {"render_epoch": RENDER_EPOCHS[KIND_GATEWAY] - 1}}
    )


def test_a_stored_broken_statement_stops_the_reapply_not_the_policy(
    local_admin, fake_clusters, mongo
):
    """저장 검사가 생기기 전의 글에 틀린 둘째 문장이 있으면 렌더가 그것을 자기 원소로 두고
    오퍼레이터가 설정을 거절한다. 재적용은 authz_invalid 로 막히고 vMCP 는 다시 적용되지 않는다.
    앞의 정책으로 도는 fail-stale 이고, 첫 문장만 적용하는 fail-open 이 아니다."""
    _cid, items, *_rest, fake = hub_env(local_admin, fake_clusters)
    gateway = gateway_up(local_admin, items, authz_policy=TWO)
    assert run_data_plane_auth(local_admin)["reapplied"] == []
    policies = cedar_policies(vmcp_of(fake, "gw-a"), fake=fake)
    # local 모드 Gateway 는 자기에게 묶인 위임 토큰을 받고 맨 끝에서 나머지 위임 토큰을 거절한다
    target = GATEWAY_TARGET.format(gateway["id"])
    assert policies == [
        group_policy("daegam-default"),
        delegation_permit(target, "default"),
        PERMIT,
        FORBID,
        *SURFACE_OFF,
        _bound_forbid(gateway["id"]),
    ]
    mongo["gateways"].update_one(
        {"_id": gateway["id"]},
        {"$set": {"spec.authz_policy": BROKEN_TAIL, "revisions.0.authz_policy": BROKEN_TAIL}},
    )
    _old_render(fake, mongo, gateway["id"], policies, BROKEN_TAIL)
    vmcp_before = vmcp_of(fake, "gw-a")
    fake.authz_invalid = "failed to parse policy 4: unexpected EOF"
    assert run_data_plane_auth(local_admin)["blocked"] == [gateway["id"]]
    config = fake.get(GV, "MCPAuthzConfig", "gw-a-authz", DEFAULT_NS)
    assert config["spec"]["config"]["policies"][-5:] == [
        PERMIT,
        BROKEN_SECOND,
        *SURFACE_OFF,
        _bound_forbid(gateway["id"]),
    ]
    assert vmcp_of(fake, "gw-a") == vmcp_before
    [row] = local_admin.get("/settings/data-plane-auth").json()["blocked"]
    assert row["id"] == gateway["id"] and row["code"] == "authz_invalid"
    assert "unexpected EOF" in row["detail"]
