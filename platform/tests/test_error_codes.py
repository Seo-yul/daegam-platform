"""플랫폼 오류 코드표(daegam/error_codes.py)와 실제로 내는 곳이 맞는가 (P74 2.2절, D86).

읽는 꼴은 tests/helpers/error_scan.py 에 있다. 표와 어긋난 짝, 표에 없는 코드, 아무도 내지 않는 표의
코드를 찾는다. 상태가 변수인 곳은 conftest 가 켜는 처리기 검사가 본다.
"""

from __future__ import annotations

import pytest

from daegam import error_codes
from daegam.error_codes import RECORDED, STATUS, allowed, holds_reapply, status_of
from tests.helpers.error_scan import scan

# 글로 적힌 짝 없이 변수나 dict 로만 나가는 코드다. 각각 어디서 나가는지 적는다
REACHED_BY_VARIABLE = {
    "agent_run_limit": "services/runs/service.py RunCaps.error(code=RUN_CAPS.code)",
    # docparse 서비스의 답을 그 상태 그대로 넘긴다(services/docparse.py), zip 검사의 코드
    "archive_too_large": "documents/ooxml.py ARCHIVE_CODES",
    "cluster_rights_missing": "k8s/gates.py tenant_secret_refusal",
    "domain_is_public_suffix": "services/domains.py DomainError 를 web_settings 가 그 코드로 낸다",
    "toolhive_too_old": "k8s/gates.py Gate(code=) 를 gate_error 가 422 로 낸다",
    "unknown_database": "services/knowledge/pg.py classify_connect_error",
    "cross_tenant_consume_unsupported": "auth/hub_access.py CROSS_TENANT_ACTION_CODES",
    "cross_tenant_manage_unsupported": "auth/hub_access.py CROSS_TENANT_ACTION_CODES",
    "cross_tenant_view_unsupported": "auth/hub_access.py CROSS_TENANT_ACTION_CODES",
    "invalid_credentials": "services/local_auth.py confirm_password(status=)",
    "runtime_error": "api/_agent_proxy.py runtime_error",
    "runtime_image_too_old": "services/agent_features.py RUNTIME_IMAGE_TOO_OLD",
    "validation_error": "FastAPI 의 검증 응답을 부르는 이름",
    "rule_names_unknown_tool": "services/tool_rules.py check_rule_keys(code=)",
    "rule_names_unknown_gateway_tool": "services/tool_rules.py check_rule_keys(code=)",
    "code_mode_exposed": "services/gateway_code_mode.py CodeModeVerdict.error",
    "code_mode_not_read_only": "services/gateway_code_mode.py CodeModeVerdict.error",
    "code_mode_owner_cedar": "services/gateway_code_mode.py CodeModeVerdict.error",
    "code_mode_reserved_name": "services/gateway_code_mode.py CodeModeVerdict.error",
    "code_mode_unknown_names": "services/gateway_code_mode.py CodeModeVerdict.error",
    "code_mode_toolhive_old": "services/gateway_code_mode.py toolhive_refusal",
}


def test_every_raised_code_matches_the_registry() -> None:
    found = scan()
    assert len(found) > 300, f"{len(found)} 곳밖에 못 읽었다. 읽는 꼴이 코드와 어긋났는지 본다"
    problems = []
    for r in found:
        if r.code not in STATUS and r.code not in RECORDED:
            problems.append(f"{r.where} {r.code}: 표에 없다")
        elif r.status is not None and not allowed(r.status, r.code):
            problems.append(f"{r.where} {r.status} {r.code}: 표와 다르다")
    assert not problems, "오류 코드표와 어긋난 곳:\n  " + "\n  ".join(problems)


def test_registry_has_no_unused_code() -> None:
    raised = {r.code for r in scan()}
    unused = sorted((set(STATUS) | set(RECORDED)) - raised - set(REACHED_BY_VARIABLE))
    assert not unused, f"내는 곳이 없는 코드: {unused}"
    assert not set(REACHED_BY_VARIABLE) - (set(STATUS) | set(RECORDED))


def test_recorded_codes_have_one_home_and_a_reason() -> None:
    assert not set(STATUS) & set(RECORDED)
    assert all(r.reason for r in RECORDED.values())


def test_status_of_takes_only_single_status_codes() -> None:
    assert status_of("not_deployed", 422) == 409
    assert status_of("integrity_error", 422) == 500
    with pytest.raises(AssertionError, match="없다"):
        status_of("no_such_code", 422)
    with pytest.raises(AssertionError, match="여럿"):
        status_of("invalid_credentials", 401)


def test_status_of_falls_back_outside_tests(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(error_codes, "STRICT", False)
    assert status_of("no_such_code", 422) == 422
    assert status_of("invalid_credentials", 401) == 401


def test_reapply_mark_follows_today_422_codes() -> None:
    """재적용이 막는 코드는 오늘 422 인 코드와 2.3절이 422 에서 옮긴 코드다 (P74 2.5절)."""
    assert holds_reapply("unauthenticated_exposure") and holds_reapply("locked_field")
    for moved in ("runtime_image_too_old", "unknown_revision", "not_deployed", "integrity_error"):
        assert holds_reapply(moved), moved
    assert holds_reapply("host_conflict") and holds_reapply("host_reserved")
    assert not holds_reapply("cluster_rights_missing")
    assert not holds_reapply("state_conflict") and not holds_reapply("namespace_no_longer_allowed")
