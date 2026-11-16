"""요청 검증 실패의 응답이다 (P76 F1, D88). 본문이나 쿼리가 요청 모델을 지나지 못하면 422
`validation_error` 이고 받은 값은 어디에도 되돌아오지 않는다."""

from __future__ import annotations

from typing import Any

from runtime.validation import MAX_ENTRIES, validation_error
from tests.conftest import run_app

# 요청에 심는 표지. 응답 어디에서도 다시 나오면 안 된다
MARK = "zz-secret-marker-7731"


def test_body_shape_drops_input_and_ctx_and_joins_loc_like_the_platform() -> None:
    errors = [
        {"type": "missing", "loc": ("body", "message"), "msg": "Field required", "input": {}},
        {
            "type": "union_tag_invalid",
            "loc": ("body", "attachments", 0),
            "msg": f"Input tag '{MARK}' found using 'kind' does not match any of the expected tags",
            "input": {"kind": MARK},
            "ctx": {"tag": MARK},
        },
    ]
    error = validation_error(errors)
    assert error.status_code == 422
    body = error.detail
    assert body["code"] == "validation_error"
    assert body["message"] == (
        "body.message: Field required; "
        "body.attachments.0: Input tag does not match any of the expected tags"
    )
    assert body["errors"][0] == {
        "loc": ["body", "message"],
        "msg": "Field required",
        "type": "missing",
    }
    assert MARK not in str(body)
    assert "more_errors" not in body


def test_entries_are_capped_and_the_rest_counted() -> None:
    errors = [
        {"type": "missing", "loc": ("body", "f", i), "msg": "Field required"}
        for i in range(MAX_ENTRIES + 7)
    ]
    body = validation_error(errors).detail
    assert len(body["errors"]) == MAX_ENTRIES and body["more_errors"] == 7
    assert body["message"].count("Field required") == MAX_ENTRIES


# (메서드, 경로, 본문 또는 None, 쿼리) 마다 요청 모델 칸 하나에 표지를 틀린 꼴로 넣는다. 프록시
# 경로의 주체(user_sub)는 인증이 모델보다 먼저 읽어 user_sub_required 로 막으므로 여기 없다
CASES: list[tuple[str, str, Any, dict[str, Any] | None]] = [
    ("POST", "/chat", {"message": {"m": MARK}}, None),
    ("POST", "/chat", {"message": "hi", "session_id": [MARK]}, None),
    ("POST", "/chat", {"message": "hi", "ephemeral": MARK}, None),
    ("POST", "/chat", {"message": "hi", "attachments": [{"kind": MARK}]}, None),
    ("POST", "/chat", {"message": "hi", "attachments": MARK}, None),
    (
        "POST",
        "/chat",
        {"message": "hi", "attachments": [{"kind": "text", "name": [MARK], "format": MARK}]},
        None,
    ),
    (
        "POST",
        "/chat",
        {"message": "hi", "attachments": [{"kind": "image", "name": "a", "data": [MARK]}]},
        None,
    ),
    ("POST", "/sessions/s1/decisions", {"decisions": [{"type": MARK}]}, None),
    (
        "POST",
        "/sessions/s1/decisions",
        {"decisions": [{"type": "edit", "edited_action": MARK, "message": [MARK]}]},
        None,
    ),
    ("POST", "/sessions/s1/decisions", {"decisions": MARK, "ephemeral": MARK}, None),
    ("POST", "/sessions/s1/regenerate", {"message": [MARK], "ephemeral": MARK}, None),
    ("PATCH", "/sessions/s1", {"title": [MARK]}, None),
    ("GET", "/sessions", None, {"limit": MARK}),
    ("GET", "/sessions", None, {"q": MARK * 20}),
]


def _with_caller(body: Any, params: dict[str, Any] | None) -> tuple[Any, dict[str, Any]]:
    """프록시 경로는 주체(user_sub)를 먼저 본다. 표지를 심지 않은 자리에는 주체를 채운다."""
    query = {"user_sub": "u1", **(params or {})}
    if isinstance(body, dict) and "user_sub" not in body:
        body = {"user_sub": "u1", **body}
    return body, query


async def test_no_answer_echoes_a_rejected_value(plain_doc) -> None:
    async with run_app(plain_doc) as running:
        for method, path, raw, raw_params in CASES:
            body, params = _with_caller(raw, raw_params)
            response = await running.client.request(method, path, json=body, params=params)
            assert response.status_code == 422, (path, body, params, response.text)
            got = response.json()
            assert got["code"] == "validation_error", got
            assert got["errors"] and all(set(e) == {"loc", "msg", "type"} for e in got["errors"])
            assert MARK not in response.text, (path, body, params, response.text)
        # 본문이 JSON 이 아니어도 같은 모양이다
        response = await running.client.post(
            "/chat",
            content=f"{MARK} not json".encode(),
            headers={"content-type": "application/json"},
        )
        assert response.status_code == 422 and response.json()["code"] == "validation_error"
        assert MARK not in response.text
