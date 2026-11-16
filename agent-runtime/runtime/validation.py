"""요청 검증 실패의 응답이다(P76 F1, D88). 본문이나 쿼리가 요청 모델을 지나지 못하면 FastAPI 는
`{"detail": [...]}` 를 내고 항목마다 받은 값(`input`)을 되돌려 준다. 런타임의 오류 본문은
`{code, message}` 한 모양이고(README "API"), 받은 값은 붙여 넣은 비밀일 수 있어 되돌려 주지 않는다.

코드는 플랫폼이 같은 상황에 쓰는 `validation_error` 다. `message` 는 항목을 `loc: msg` 로 `; ` 를
사이에 두고 이은 것이고 `loc` 은 `.` 으로 잇는다. 플랫폼 화면(`ui/src/api/client.ts`
`parseErrorBody`)이 플랫폼 자신의 검증 실패에서 만드는 글과 같은 모양이라 프록시를 거친 뒤에도 같은
코드와 글로 읽힌다.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from fastapi import HTTPException

from runtime.turn_gate import error

VALIDATION_ERROR = "validation_error"
# 응답에 싣는 검증 항목의 상한이다. 넘는 것은 개수(`more_errors`)만 싣는다
MAX_ENTRIES = 50

# pydantic 이 받은 값을 문장에 싣는 오류 종류다. 그 값 대신 고정 문장을 쓴다. union_tag_invalid 는
# 받은 첨부 종류를, value_error 는 검증기가 만든 글(받은 값을 넣을 수 있다)을 싣는다. 나머지 종류의
# 문장은 기대한 값이나 상한만 말한다. 받은 값이 나오지 않는지는 tests/test_validation_errors.py 가
# 본다
FIXED_MESSAGES: dict[str, str] = {
    "union_tag_invalid": "Input tag does not match any of the expected tags",
    "value_error": "Value error",
}


def body_field_error(name: str, msg: str, kind: str) -> dict[str, Any]:
    """본문 칸 하나의 검증 항목이다. 요청 모델이 아니라 라우트가 칸을 맞춰 볼 때
    `validation_error` 에 넘긴다. kind 가 FIXED_MESSAGES 에 있으면 msg 는 고정 문장으로 바뀌므로 그
    밖의 종류를 쓴다."""
    return {"loc": ("body", name), "msg": msg, "type": kind}


def _entry(error: dict[str, Any]) -> dict[str, Any]:
    kind = str(error.get("type", ""))
    return {
        "loc": list(error.get("loc", ())),
        "msg": FIXED_MESSAGES.get(kind, str(error.get("msg", ""))),
        "type": kind,
    }


def validation_error(errors: Sequence[dict[str, Any]]) -> HTTPException:
    """검증 실패 항목에서 422 를 만든다. `input`·`ctx` 는 싣지 않는다. 항목은 앞의
    `MAX_ENTRIES` 개만 싣고 나머지는 `more_errors` 로 개수만 알린다(목록이 큰 본문이 응답을 키우지
    않게). 다른 거절과 같은 `turn_gate.error` 라 앱의 핸들러가 같은 모양으로 내고 오류 코드표와도
    맞춰 본다."""
    entries = [_entry(e) for e in errors[:MAX_ENTRIES]]
    message = "; ".join(
        f"{'.'.join(str(p) for p in e['loc'])}: {e['msg']}" if e["loc"] else e["msg"]
        for e in entries
    )
    extra: dict[str, Any] = {"errors": entries}
    if len(errors) > MAX_ENTRIES:
        extra["more_errors"] = len(errors) - MAX_ENTRIES
    return error(422, VALIDATION_ERROR, message, **extra)
