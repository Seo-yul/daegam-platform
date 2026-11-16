"""P11 F3: minify 와 초안 적용(apply-draft).

fixtures × minify 옵션 3조합이 표면 보호(ALLOW_MINIFY)·verify 를 통과하고 tool_names 가 그대로인
것을 고정한다. API 는 minify 즉시 응답(저장 없음), apply-draft 의 일부·전부 채택, origin 기록,
무변경 200, 모르는 id·변경 없음·본문 오류의 422, refine job 결과 적용, 채택 결과의 표면 보호
재검사, 감사 action 을 검증한다.
"""

from __future__ import annotations

import json

import gridfs
import pytest
from spec2openapi import dump_spec

from daegam.pipeline import convert as pipeline
from daegam.services.spec_generator import minify as minify_svc
from daegam.services.spec_generator import projection, refine, surface
from daegam.services.spec_generator.minify import MinifyOptions, run_minify
from daegam.store.spec_jobs import draft_name
from tests.helpers.backend_apis import fixture_bytes, upload_backend
from tests.helpers.consts import BP, T2
from tests.helpers.fake_llm import FakeProvider
from tests.helpers.http import detail_code
from tests.helpers.spec_jobs import (
    GET_PET,
    PROVIDERS,
    j,
    post_job,
    refine_body,
    wait_done,
    worker_client,
)

OPTIONS = [
    MinifyOptions(),
    MinifyOptions(enrich=("errors", "examples")),
    MinifyOptions(enrich=("errors", "examples"), max_description=80, drop_value_examples=True),
]
FIXTURES = [
    "calculator.wsdl",
    "orders.wsdl",
    "petstore-swagger2.json",
    "orders.openapi.yaml",
    "legacy-swagger.json",
]


def spec_and_result(fixture: str):
    res = pipeline.run(fixture_bytes(fixture), filename=fixture)
    return refine.spec_from_yaml(res.yaml.encode("utf-8")), res


# -- minify 고정 -------------------------------------------------------------------------


@pytest.mark.parametrize("fixture", FIXTURES)
def test_minify_fixtures_pass_surface_and_verify(fixture):
    original, res = spec_and_result(fixture)
    for options in OPTIONS:
        draft, changes = run_minify(original, options)
        assert surface.guard(original, draft, rules=surface.ALLOW_MINIFY) == [], options
        insp = pipeline.inspect(dump_spec(draft, fmt="yaml").encode("utf-8"), filename="draft.yaml")
        failed = [r["id"] for r in insp.report["results"] if r["status"] == "fail"]
        assert failed == [] and insp.report["complete"] is True, options
        assert insp.tool_names == res.tool_names and insp.kind == res.kind
        # 채택 전부를 다시 심으면 초안과 같고, 같은 옵션이면 id 도 같다
        assert surface.diff(projection.apply_changes(original, changes), draft) == []
        assert [c.id for c in run_minify(original, options)[1]] == [c.id for c in changes]
        assert all(c.kind in ("changed", "added", "removed") for c in changes)
    # 원본은 건드리지 않는다
    assert original == spec_and_result(fixture)[0]


def test_minify_changes_content():
    original, _ = spec_and_result("petstore-swagger2.json")
    assert run_minify(original, MinifyOptions())[1] == []
    _draft, changes = run_minify(original, OPTIONS[1])
    pointers = [c.pointer for c in changes]
    assert f"{GET_PET}/description" in pointers  # 오류 응답을 설명에 접었다
    assert any(p.startswith("/x-s2o") for p in pointers)  # 기록
    desc = next(c for c in changes if c.pointer == f"{GET_PET}/description")
    assert desc.kind == "changed" and desc.before == "Returns a single pet"
    assert desc.after.startswith("Returns a single pet\nErrors: ")
    with pytest.raises(ValueError):
        run_minify({"swagger": "2.0"}, MinifyOptions())


# -- API ---------------------------------------------------------------------------------


def minify_url(pid: str, version: int = 1) -> str:
    return f"{BP}/{pid}/versions/{version}/minify"


def apply_url(pid: str, version: int = 1) -> str:
    return f"{BP}/{pid}/versions/{version}/apply-draft"


def test_minify_endpoint_returns_changes_without_saving(client):
    pid = upload_backend(client, "pets", "petstore-swagger2.json").json()["id"]
    r = client.post(minify_url(pid), json={"enrich": ["errors", "examples"]})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["changes"] and body["draft_yaml"].startswith("openapi:")
    assert all(set(c) == {"id", "pointer", "kind", "before", "after"} for c in body["changes"])
    assert any(c["pointer"].endswith("/description") for c in body["changes"])
    assert "Errors: " in body["draft_yaml"]
    # 저장하지 않는다
    assert [v["version"] for v in client.get(f"{BP}/{pid}/versions").json()] == [1]
    # 기본 옵션은 변경이 없다
    assert client.post(minify_url(pid), json={}).json()["changes"] == []
    # 잘못된 옵션·없는 버전·다른 테넌트
    assert client.post(minify_url(pid), json={"enrich": ["bogus"]}).status_code == 422
    assert client.post(minify_url(pid), json={"max_description": 0}).status_code == 422
    assert client.post(minify_url(pid), json={"extra": 1}).status_code == 422
    assert client.post(minify_url(pid, 9), json={}).status_code == 404
    assert client.post(minify_url(pid), json={}, headers=T2).status_code == 404


def test_minify_reorder_is_surface_changed_not_500(client, monkeypatch):
    """P73 2절: 키 순서를 바꾼 초안은 채택 단위가 될 수 없어 changes 를 셈하기 전에 표면 검사가
    422 surface_changed 로 막는다. minify 와 apply-draft 의 minify 갈래 모두 같다."""
    pid = upload_backend(client, "pets", "petstore-swagger2.json").json()["id"]

    def reordered(spec, **_kw):
        out = json.loads(json.dumps(spec))
        out["info"] = dict(reversed(list(out["info"].items())))
        return out

    monkeypatch.setattr(minify_svc, "minify_for_mcp", reordered)
    for r in (
        client.post(minify_url(pid), json={}),
        client.post(apply_url(pid), json={"minify": {}}),
    ):
        assert detail_code(r) == (422, "surface_changed"), r.text
        kinds = {v["kind"] for v in r.json()["detail"]["violations"]}
        assert kinds == {"reordered"}


def test_apply_draft_minify_partial_then_all(client, mongo):
    pid = upload_backend(client, "pets", "petstore-swagger2.json").json()["id"]
    opts = {"enrich": ["errors", "examples"]}
    changes = client.post(minify_url(pid), json=opts).json()["changes"]
    desc = [c for c in changes if c["kind"] == "changed" and c["pointer"].endswith("/description")]
    assert len(desc) >= 3
    pick = [desc[0]["id"], desc[1]["id"]]

    r = client.post(apply_url(pid), json={"minify": opts, "accepted_ids": pick})
    assert r.status_code == 201, r.text
    v2 = r.json()
    assert v2["version"] == 2 and v2["backend_profile_id"] == pid
    assert v2["origin"] == {"kind": "minify", "from_version": 1, "job_id": None, "presets": []}
    v1 = client.get(f"{BP}/{pid}/versions/1").json()
    assert v2["tool_names"] == v1["tool_names"] and v2["kind"] == v1["kind"]
    assert v1["origin"] is None  # 업로드 버전에는 출처가 없다
    assert v2["report"]["ok"] is True
    spec2 = client.get(f"{BP}/{pid}/versions/2/spec").json()
    assert surface.resolve(spec2, desc[0]["pointer"]) == desc[0]["after"]
    assert surface.resolve(spec2, desc[1]["pointer"]) == desc[1]["after"]
    # 채택하지 않은 변경은 원본 값 그대로다
    assert surface.resolve(spec2, desc[2]["pointer"]) == desc[2]["before"]
    assert "minify" not in spec2.get("x-s2o", {})
    assert client.get(f"{BP}/{pid}").json()["latest_version"] == 2

    # 같은 채택을 다시 보내면 최신 버전과 같아 200 무변경
    r = client.post(apply_url(pid), json={"minify": opts, "accepted_ids": pick})
    assert r.status_code == 200, r.text
    assert r.json()["version"] == 2
    assert [v["version"] for v in client.get(f"{BP}/{pid}/versions").json()] == [1, 2]

    # 전부 채택 → v3 (기록 x-s2o.minify 포함)
    r = client.post(apply_url(pid), json={"minify": opts})
    assert r.status_code == 201, r.text
    assert r.json()["version"] == 3 and r.json()["origin"]["kind"] == "minify"
    spec3 = client.get(f"{BP}/{pid}/versions/3/spec").json()
    assert spec3["x-s2o"]["minify"]["counts"]["foldedErrorLines"] >= 1
    assert (
        surface.guard(
            spec_and_result("petstore-swagger2.json")[0], spec3, rules=surface.ALLOW_MINIFY
        )
        == []
    )
    # 다시 전부 → 200 무변경
    assert client.post(apply_url(pid), json={"minify": opts}).status_code == 200

    # 오류: 모르는 id, 변경 없음, 본문
    r = client.post(apply_url(pid), json={"minify": opts, "accepted_ids": ["deadbeef0000"]})
    assert detail_code(r) == (422, "unknown_change")
    assert r.json()["detail"]["ids"] == ["deadbeef0000"]
    r = client.post(apply_url(pid), json={"minify": {}})
    assert detail_code(r) == (422, "no_changes")
    r = client.post(apply_url(pid), json={})
    assert detail_code(r) == (422, "invalid_request")
    r = client.post(apply_url(pid), json={"minify": opts, "job_id": "x"})
    assert detail_code(r) == (422, "invalid_request")
    assert client.post(apply_url(pid), json={"minify": opts, "bogus": 1}).status_code == 422
    assert client.post(apply_url(pid, 9), json={"minify": opts}).status_code == 404
    assert client.post(apply_url(pid), json={"minify": opts}, headers=T2).status_code == 404
    # 감사 action 은 핸들러 이름 apply_draft 다 (성공·422 모두 남는다)
    actions = [e["action"] for e in mongo["audit_events"].find()]
    assert actions.count("apply_draft") >= 3 and "minify_version" in actions


def test_apply_draft_from_refine_job(settings, mongo, fake_clusters, fake_secrets):
    view = {
        "operations": {
            "getPetById": {"summary": "Fetch one pet", "description": "Returns one pet by id."}
        }
    }
    with worker_client(settings, fake_clusters, fake_secrets, FakeProvider([j(view)])) as client:
        client.patch("/settings", json=PROVIDERS)
        pid = upload_backend(client, "pets", "petstore-swagger2.json").json()["id"]
        jid = post_job(client, **refine_body(pid)).json()["id"]
        got = wait_done(client, jid)
        assert got["state"] == "done", got
        changes = got["changes"]
        assert [(c["kind"], c["pointer"]) for c in changes] == [
            ("changed", f"{GET_PET}/summary"),
            ("changed", f"{GET_PET}/description"),
        ]

        # 일부 채택
        r = client.post(apply_url(pid), json={"job_id": jid, "accepted_ids": [changes[0]["id"]]})
        assert r.status_code == 201, r.text
        assert r.json()["version"] == 2
        assert r.json()["origin"] == {
            "kind": "refine",
            "from_version": 1,
            "job_id": jid,
            "presets": ["enrich_descriptions"],
        }
        spec2 = client.get(f"{BP}/{pid}/versions/2/spec").json()
        assert surface.resolve(spec2, changes[0]["pointer"]) == "Fetch one pet"
        assert surface.resolve(spec2, changes[1]["pointer"]) == "Returns a single pet"
        # 전부 채택 → v3, 다시 전부 → 200 무변경
        r = client.post(apply_url(pid), json={"job_id": jid})
        assert r.status_code == 201 and r.json()["version"] == 3
        assert client.post(apply_url(pid), json={"job_id": jid}).status_code == 200
        versions = client.get(f"{BP}/{pid}/versions").json()
        assert [(v["version"], (v["origin"] or {}).get("from_version")) for v in versions] == [
            (1, None),
            (2, 1),
            (3, 1),
        ]

        # 오류: 다른 버전에 적용, 없는 job, 다른 테넌트의 job
        r = client.post(apply_url(pid, 2), json={"job_id": jid})
        assert detail_code(r) == (422, "source_mismatch")
        r = client.post(apply_url(pid), json={"job_id": "nope"})
        assert detail_code(r) == (422, "job_not_found")
        pid2 = upload_backend(client, "pets", "petstore-swagger2.json", headers=T2).json()["id"]
        r = client.post(apply_url(pid2), json={"job_id": jid}, headers=T2)
        assert detail_code(r) == (422, "job_not_found")


def test_apply_draft_job_prechecks(client):
    """워커가 꺼진 앱: queued 인 refine job 은 job_not_done, generate job 은 job_not_refine."""
    client.patch("/settings", json=PROVIDERS)
    pid = upload_backend(client, "calc", "calculator.wsdl").json()["id"]
    queued = post_job(client, **refine_body(pid)).json()["id"]
    r = client.post(apply_url(pid), json={"job_id": queued})
    assert detail_code(r) == (422, "job_not_done")
    gen = post_job(client, input={"text": "x"}).json()["id"]
    r = client.post(apply_url(pid), json={"job_id": gen})
    assert detail_code(r) == (422, "job_not_refine")


def test_apply_draft_rechecks_surface(settings, mongo, fake_clusters, fake_secrets):
    """done 된 job 의 초안이라도 적용 때 표면 보호를 다시 거친다. GridFS 의 초안을 x-soap 이 바뀐
    문서로 바꿔치기하면 422 surface_changed 다."""
    with worker_client(settings, fake_clusters, fake_secrets, FakeProvider(["{}"])) as client:
        client.patch("/settings", json=PROVIDERS)
        pid = upload_backend(client, "calc", "calculator.wsdl").json()["id"]
        jid = post_job(client, **refine_body(pid)).json()["id"]
        got = wait_done(client, jid)
        assert got["state"] == "done" and got["changes"] == []
        # 변경이 없으니 적용할 것도 없다
        r = client.post(apply_url(pid), json={"job_id": jid})
        assert detail_code(r) == (422, "no_changes")

        tampered = json.loads(json.dumps(got["draft"]))
        tampered["paths"]["/operations/Add"]["post"]["x-soap"]["soapAction"] = "http://evil/Add"
        tampered["paths"]["/operations/Add"]["post"]["summary"] = "Adds."
        bucket = gridfs.GridFSBucket(mongo, bucket_name="spec_jobs")
        name = draft_name(jid, 1)
        for f in mongo["spec_jobs.files"].find({"filename": name}):
            bucket.delete(f["_id"])
        bucket.upload_from_stream(
            name, j(tampered).encode(), metadata={"job_id": jid, "role": "draft"}
        )
        r = client.post(apply_url(pid), json={"job_id": jid})
        assert r.status_code == 422, r.text
        detail = r.json()["detail"]
        assert detail["code"] == "surface_changed"
        assert [(v["kind"], v["pointer"]) for v in detail["violations"]] == [
            ("changed", "/paths/~1operations~1Add/post/x-soap/soapAction")
        ]
        # 허용 변경만 채택하면 통과한다
        summary_id = projection.change_id("/paths/~1operations~1Add/post/summary", "changed")
        r = client.post(apply_url(pid), json={"job_id": jid, "accepted_ids": [summary_id]})
        assert r.status_code == 201, r.text
        assert [v["version"] for v in client.get(f"{BP}/{pid}/versions").json()] == [1, 2]
