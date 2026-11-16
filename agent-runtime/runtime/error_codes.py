"""런타임 오류 코드표 (P74 2.2절, D86). 코드 하나는 조건 하나이고 상태 하나다.

`STATUS` 가 런타임이 내는 모든 코드의 상태를 한 곳에 둔다. 상태가 둘 이상인 코드는 `RECORDED` 에만
있고, 상태 하나로는 깨지는 동작을 이유로 적는다. 플랫폼은 이 파일을 import 하지 못하므로 플랫폼
테스트(test_error_code_parity.py)가 글로 읽어 같은 이름의 코드끼리 상태를 맞춰 본다.

`STRICT` 는 테스트만 켠다(tests/conftest.py). 켜지면 `_http_error` 가 모든 응답을 표와 맞춰
보고, `status_of` 가 표에 없는 코드나 상태가 여럿인 코드를 찾을 때 멈춘다. 운영에서는 아무것도
보지 않는다.
"""

from __future__ import annotations

from dataclasses import dataclass

# 테스트만 켠다. 켜지면 응답과 조회를 표와 맞춰 본다
STRICT = False

STATUS: dict[str, int] = {
    # 인스턴스의 drain 표시가 리스 잡기를 물렸다. 플랫폼은 agent_waking 으로 바꿔 답한다 (P44 7절)
    "agent_draining": 503,
    "attachments_need_filesystem": 422,
    "attachments_too_large": 413,
    "daily_budget_exceeded": 429,
    "decision_count_mismatch": 422,
    "decision_invalid": 422,
    "decision_not_allowed": 422,
    "dependencies_unavailable": 503,
    "edit_name_mismatch": 422,
    "file_not_found": 404,
    # 열면 코드가 돌 수 있는 작업 공간 파일의 내려받기다 (P40 6절)
    "file_refused_executable": 403,
    # 다운로드할 답이 큰 것이라 2.1절의 413 뜻과 어긋나지만 상태를 그대로 둔다 (P74 2.4절)
    "file_too_large": 413,
    "forbidden": 403,
    "guardrail_blocked": 422,
    "idp_unavailable": 503,
    "images_unsupported": 422,
    "invalid_attachment": 422,
    "invalid_path": 422,
    "invalid_status": 422,
    "invalid_title": 422,
    "invalid_token": 401,
    "long_term_off": 404,
    "max_concurrent_sessions": 429,
    "max_input_chars": 413,
    "max_regenerations": 429,
    # Retry-After 가 없는 429 지만 상태를 그대로 둔다 (P74 2.4절)
    "max_turns": 429,
    "memory_not_found": 404,
    "no_turn_in_progress": 409,
    "not_session_owner": 403,
    "nothing_to_regenerate": 409,
    "payload_too_large": 413,
    "rate_limited": 429,
    # 실행 세션의 결정이 그 세션의 run_id 를 싣지 않았다. 플랫폼이 실행을 닫았다고 알리는 코드와
    # 같은 이름이다 (P41 9절)
    "run_closed": 409,
    # 같은 run_id 의 세션이 이미 있다. 본문이 그 세션 id 와 턴 id 를 싣는다 (P41 9절)
    "run_exists": 409,
    # 실행 세션에는 새 말도 다시 생성도 보내지 않는다 (P41 9절)
    "run_session": 409,
    # 문서의 기능 이름을 모르는 이미지가 내는 거절. 이 코드는 문서보다 오래된 이미지만 내므로
    # 새 이미지가 바꿀 수 없다. 플랫폼 표는 같은 코드에 409 를 함께 적는다 (P74 2.2절)
    "runtime_image_too_old": 503,
    "session_budget_exceeded": 429,
    "session_closed": 410,
    "session_delete_incomplete": 503,
    "session_not_found": 404,
    "session_pending": 409,
    "token_expired": 401,
    "too_many_attachments": 422,
    "tools_unavailable": 503,
    # 턴 태스크를 띄우기 전에 리스를 잃었다 (P79 2절)
    "turn_aborted": 503,
    "turn_in_progress": 409,
    # 위임의 사람이 이 턴의 사람이 아니다 (P53 6절)
    "turn_grant_mismatch": 409,
    "unauthorized": 401,
    "user_sub_required": 422,
    # 본문·쿼리가 요청 모델을 지나지 못했다. 플랫폼과 같은 코드다 (P76 F1)
    "validation_error": 422,
}


@dataclass(frozen=True)
class Recorded:
    """상태가 둘 이상인 코드. `statuses` 가 None 이면 어떤 상태든 된다."""

    statuses: frozenset[int] | None
    reason: str


RECORDED: dict[str, Recorded] = {
    "error": Recorded(
        None,
        "코드 없이 글만 실은 HTTPException 을 감싼 것이라 상태만이 남은 정보다",
    ),
    "not_pending": Recorded(
        frozenset({404, 409}),
        "GET 은 경로가 가리키는 보류가 없는 것이고(404), 결정은 세션 상태와 부딪힌 것이다(409). "
        "노출된 에이전트를 부르는 쪽과의 계약이다",
    ),
}


def allowed(status: int, code: str) -> bool:
    """이 상태와 코드의 짝이 표에 맞는가. 표에 없는 코드는 맞지 않는다."""
    if code in RECORDED:
        statuses = RECORDED[code].statuses
        return statuses is None or status in statuses
    return STATUS.get(code) == status


def status_of(code: str, default: int) -> int:
    """다른 예외의 코드를 다시 쌀 때의 상태. 상태가 하나인 코드만 찾는다.

    표에 없으면 `default`(지금까지 쓰던 상태)다. STRICT 에서는 표에 없는 코드와 상태가 여럿인
    코드가 멈춘다. 다시 싸는 쪽이 여럿 중 하나를 우연히 고르지 않게 한다."""
    if code in RECORDED:
        if STRICT:
            raise AssertionError(f"{code} 는 상태가 여럿이라 다시 싸는 쪽이 찾을 수 없다")
        return default
    status = STATUS.get(code)
    if status is None:
        if STRICT:
            raise AssertionError(f"{code} 가 오류 코드표(runtime/error_codes.py)에 없다")
        return default
    return status
