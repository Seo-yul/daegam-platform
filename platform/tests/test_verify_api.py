"""P2 F9: POST /backend-apis/verify · /convert 와 업로드 수락 규칙의 일치."""

from __future__ import annotations

import yaml

from daegam.pipeline.verdict import compute_verdict
from tests.helpers.backend_apis import fixture_bytes, upload_backend
from tests.helpers.consts import BP
from tests.helpers.http import detail_code

BROKEN_JSON = b'{"openapi": "3.0.3", "info": {"title": "broken"'

# 픽스처 5종의 verdict 스냅샷: (level, code 집합)
SNAPSHOTS = {
    "calculator.wsdl": ("ok", {"converted-from-wsdl"}),
    "legacy-swagger.json": ("warn", {"converted-from-swagger2", "tool-description.present"}),
    "orders.openapi.yaml": ("ok", set()),
    "mixed.openapi.yaml": ("error", {"x-soap.mixed-rest"}),
    "broken.json": ("error", {"unsupported-format"}),
}
# 판정이 error 인 픽스처를 업로드하면 받는 기존 오류 코드
UPLOAD_ERROR_CODES = {"mixed.openapi.yaml": "mixed", "broken.json": "unsupported"}


def _body(name: str) -> bytes:
    return BROKEN_JSON if name == "broken.json" else fixture_bytes(name)


def verify_multipart(client, name: str, headers=None):
    return client.post(
        f"{BP}/verify",
        files={"file": (name, _body(name), "application/octet-stream")},
        headers=headers or {},
    )


def verify_json(client, name: str):
    return client.post(f"{BP}/verify", json={"text": _body(name).decode("utf-8"), "filename": name})


def convert(client, name: str, fmt: str | None = None):
    url = f"{BP}/convert" + (f"?format={fmt}" if fmt else "")
    return client.post(url, files={"file": (name, _body(name), "application/octet-stream")})


def codes(verdict: dict) -> set[str]:
    return {i["code"] for i in verdict["items"]}


# -- verify -------------------------------------------------------------------


def test_verify_snapshot(client):
    name = "mixed.openapi.yaml"
    level, expected_codes = SNAPSHOTS[name]
    r = verify_multipart(client, name)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["verdict"]["level"] == level
    assert codes(body["verdict"]) == expected_codes
    for item in body["verdict"]["items"]:
        assert set(item) == {"level", "code", "message", "count", "args"}
        assert item["level"] in ("error", "warn", "info")
        assert item["count"] >= 1
    # json 입력도 같은 판정
    rj = verify_json(client, name)
    assert rj.status_code == 200, rj.text
    assert rj.json()["verdict"] == body["verdict"]
    assert rj.json()["source_format"] == body["source_format"]


def test_verify_input_validation(client):
    r = client.post(f"{BP}/verify", json={"nope": 1})
    assert detail_code(r) == (422, "invalid_input")
    r = client.post(f"{BP}/verify", content=b"hello", headers={"Content-Type": "text/plain"})
    assert detail_code(r) == (422, "invalid_input")
    r = client.post(f"{BP}/verify", files={"other": ("a.wsdl", fixture_bytes("calculator.wsdl"))})
    assert detail_code(r) == (422, "invalid_input")


# -- convert --------------------------------------------------------------------


def test_convert_json(client):
    r = convert(client, "legacy-swagger.json", fmt="json")
    assert r.status_code == 200, r.text
    assert r.headers["x-daegam-verdict"] == "warn"
    body = r.json()
    assert set(body) == {"yaml", "report", "verdict"}
    assert yaml.safe_load(body["yaml"])["openapi"].startswith("3.")
    assert body["report"]["ok"] is True
    assert body["verdict"]["level"] == "warn"
    # yaml 형식과 같은 본문이고 판정 헤더도 같다
    as_yaml = convert(client, "legacy-swagger.json")
    assert as_yaml.text == body["yaml"] and as_yaml.headers["x-daegam-verdict"] == "warn"


def test_convert_bad_format(client):
    r = convert(client, "calculator.wsdl", fmt="xml")
    assert detail_code(r) == (422, "invalid_format")


# -- 업로드 수락과 verify 판정의 일치 ---------------------------------------------


def test_upload_matches_verify(client):
    """verify 가 error 로 판정한 문서는 업로드도 같은 판정의 422 다. error 가 아닌 문서의 업로드는
    test_api_backend_apis.py 가 본다."""
    name = "broken.json"
    verdict = verify_multipart(client, name).json()["verdict"]
    assert verdict["level"] == "error"
    r = upload_backend(client, "p-" + name.split(".")[0], name, body=_body(name))
    assert r.status_code == 422, r.text
    detail = r.json()["detail"]
    assert detail["verdict"] == verdict
    # 기존 코드 체계는 유지된다
    assert detail["code"] == UPLOAD_ERROR_CODES[name]
    assert client.get(BP).json() == []


def test_upload_verify_failure_carries_verdict(client):
    empty = b'{"openapi":"3.0.3","info":{"title":"t","version":"1"},"paths":{}}'
    r = upload_backend(client, "empty", "empty.json", body=empty)
    assert r.status_code == 422
    detail = r.json()["detail"]
    assert detail["code"] == "verify"
    assert detail["report"]["ok"] is False
    assert detail["verdict"]["level"] == "error"
    assert "document.has-paths" in codes(detail["verdict"])


# -- compute_verdict 단위 ----------------------------------------------------------


def _report(*results):
    return {
        "ok": all(r[1] != "fail" for r in results),
        "complete": True,
        "results": [{"id": i, "status": s, "message": m} for i, s, m in results],
    }


def test_compute_verdict_rules():
    v = compute_verdict("openapi3", _report(("tool-name.safe", "pass", "")), False)
    assert v.level == "ok" and v.items == ()

    v = compute_verdict(
        "openapi3",
        _report(
            ("tool-name.safe", "fail", "a: bad"),
            ("tool-name.safe", "fail", "b: bad"),
            ("x-soap.endpoint", "warn", "no endpoint"),
            ("openapi.schema-valid", "skip", "no validator"),
            ("some.unknown-check", "warn", "hm"),
            ("some.other-check", "fail", "hm"),
        ),
        False,
    )
    assert v.level == "error"
    by = {i.code: i for i in v.items}
    assert by["tool-name.safe"].level == "error" and by["tool-name.safe"].count == 2
    assert by["tool-name.safe"].message == "a: bad"
    # 라이브러리가 쓴 항목은 인자가 없다. 화면은 message 를 그대로 보인다
    assert by["tool-name.safe"].to_dict()["args"] == {}
    assert by["x-soap.endpoint"].level == "warn"
    assert by["openapi.schema-valid"].level == "info"
    assert by["some.unknown-check"].level == "warn"
    assert by["some.other-check"].level == "error"
    assert [i.level for i in v.items] == ["error", "error", "warn", "warn", "info"]

    # x-soap.version: soapVersion 없음(warn) 은 warn, 값이 틀림(fail) 은 error
    v = compute_verdict("openapi3", _report(("x-soap.version", "warn", "missing")), False)
    assert v.level == "warn"
    v = compute_verdict("openapi3", _report(("x-soap.version", "fail", "bad")), False)
    assert v.level == "error"

    # 형식 감지
    assert compute_verdict("swagger2", _report(), False).items[0].code == "converted-from-swagger2"
    assert compute_verdict("unsupported", None, False).level == "error"
    # 혼합: 리포트에 없어도 error 로 보탠다. 있으면 warn 을 error 로 올린다
    assert compute_verdict("openapi3", _report(), True).codes == {"x-soap.mixed-rest"}
    v = compute_verdict("openapi3", _report(("x-soap.mixed-rest", "warn", "mixed")), True)
    assert [i.code for i in v.items] == ["x-soap.mixed-rest"] and v.level == "error"
    # 리포트 전 실패
    v = compute_verdict("wsdl", None, False, error=("conversion", "boom"))
    assert v.codes == {"conversion-failed"} and v.level == "error"
    v = compute_verdict("unsupported", None, False, error=("too_large", "big"))
    assert v.codes == {"too-large"}


# -- 도구 인자 대소문자 충돌 (ToolHive 파서가 400 으로 막는다) --------------------------------

_CASE_SPEC = {
    "openapi": "3.0.3",
    "info": {"title": "users", "version": "1"},
    "paths": {
        "/users/{id}": {
            "parameters": [{"$ref": "#/components/parameters/UserId"}],
            "post": {
                "operationId": "updateUser",
                "summary": "update",
                "requestBody": {
                    "content": {
                        "application/json": {"schema": {"$ref": "#/components/schemas/User"}}
                    }
                },
                "responses": {"200": {"description": "ok"}},
            },
        },
        "/ping": {
            "get": {
                "operationId": "ping",
                "summary": "ping",
                "parameters": [{"name": "q", "in": "query", "schema": {"type": "string"}}],
                "responses": {"200": {"description": "ok"}},
            }
        },
    },
    "components": {
        "parameters": {
            "UserId": {"name": "id", "in": "path", "required": True, "schema": {"type": "string"}}
        },
        "schemas": {
            "User": {
                "type": "object",
                "properties": {
                    "Id": {"type": "string"},
                    "name": {"type": "string"},
                    "tags": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {"key": {"type": "string"}, "KEY": {"type": "string"}},
                        },
                    },
                    "self": {"$ref": "#/components/schemas/User"},
                },
            }
        },
    },
}


def test_verify_warns_on_case_collision(client):
    r = client.post(
        f"{BP}/verify",
        files={
            "file": (
                "users.json",
                __import__("json").dumps(_CASE_SPEC).encode(),
                "application/json",
            )
        },
    )
    assert r.status_code == 200
    # 화면이 읽는 칸이다. verify 는 아무것도 저장하지 않는다
    shape = {"source_format", "kind", "mixed", "tool_names", "tool_details", "report", "verdict"}
    assert shape <= r.json().keys()
    assert client.get(BP).json() == []
    items = {i["code"]: i for i in r.json()["verdict"]["items"]}
    item = items["tool-args.case-collision"]
    # 두 쌍이다. 경로 파라미터 id 와 본문 속성 Id 는 한 arguments 객체에 같이 놓이고, 배열 안 객체의
    # key·KEY 도 본다. 자기 참조(self)는 한 번만 따라간다
    assert item["level"] == "warn" and item["count"] == 2
    assert item["message"].startswith("updateUser: 인자 Id · id 는 대소문자만 다릅니다")
    # 화면은 code 와 args 로 화면 언어의 문장을 만든다. 접힌 항목은 처음 본 쌍의 값이다 (P49 6절)
    assert item["args"] == {"tool": "updateUser", "names": ["Id", "id"]}
    assert r.json()["verdict"]["level"] in ("warn", "error")


def test_convert_error_422(client):
    name = "broken.json"
    r = convert(client, name)
    assert r.status_code == 422
    detail = r.json()["detail"]
    assert detail["code"] == "verdict_error"
    assert detail["verdict"]["level"] == "error"
    assert codes(detail["verdict"]) == SNAPSHOTS[name][1]
    assert convert(client, name, fmt="json").status_code == 422
