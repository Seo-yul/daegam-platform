"""요청 검증의 422 는 어느 라우트에서도 오류의 `input` 을 싣지 않는다.

FastAPI 는 오류마다 틀린 필드의 값, 필수 필드가 빠지면 본문 전체를 `input` 으로 싣는다. 비밀번호,
provider 키, 토큰을 받는 라우트가 그 값을 응답으로 돌려주지 않도록 처리기가 모든 라우트에서 뺀다.
"""

from __future__ import annotations

SECRET = "sk-must-not-come-back-0123456789"


def _assert_no_echo(r) -> list[dict]:
    assert r.status_code == 422, r.text
    assert SECRET not in r.text, r.text
    errors = r.json()["detail"]
    assert errors and all("input" not in e for e in errors), errors
    # 화면이 읽는 필드는 그대로다
    assert all({"loc", "msg", "type"} <= e.keys() for e in errors), errors
    return errors


def test_provider_key_route_drops_the_refused_body(client):
    """필수 필드가 빠진 본문(오류의 `input` 이 본문 전체), 너무 긴 키, 남는 필드 어디에도 키가
    없다."""
    url = "/settings/llm-providers/openai/api-key"
    errors = _assert_no_echo(client.put(url, json={"apikey": SECRET}))
    assert {e["type"] for e in errors} == {"missing", "extra_forbidden"}
    _assert_no_echo(client.put(url, json={"api_key": SECRET * 200}))
    _assert_no_echo(client.put(url, json={"api_key": "sk-ok", "note": SECRET}))


def test_local_login_drops_the_refused_body(local_client):
    _assert_no_echo(local_client.post("/auth/login", json={"password": SECRET}))
    _assert_no_echo(local_client.post("/auth/login", json={"username": "a", "password": [SECRET]}))
