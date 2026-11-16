"""P11 F2: 투영·표면 보호·refine 모드.

단위: 뷰 추출·투영·unknown·properties 순서 보존·허용/보호 표·묶음 나누기·되먹임 대상.
루프(가짜 provider): unknown 되먹임 뒤 done, no_progress 에 unknown 포함, JSON 아닌 응답, 재개
경로의 표면 보호, 묶음 재개, 묶음 분할 호출 수, operationId 프리셋.
API: refine job 의 사전 검사(invalid_request·empty_instruction·source_not_found·source_not_verified·
source_too_large)와 GET 의 mode·source·presets·changes·draft_yaml·renamed.
"""

from __future__ import annotations

import copy
import json

import pytest

from daegam.pipeline import convert as pipeline
from daegam.services.spec_generator import projection, refine, surface
from daegam.services.spec_generator.loop import (
    SpecGeneratorRunner,
)
from daegam.store.spec_jobs import Iteration, SpecJob, draft_name, input_name
from tests.helpers.aio import run_stores
from tests.helpers.backend_apis import fixture_bytes, upload_backend
from tests.helpers.consts import JOBS, T2
from tests.helpers.fake_llm import FakeProvider
from tests.helpers.http import api_client, make_app
from tests.helpers.spec_jobs import (
    GET_PET,
    LOOP_STORES,
    PROVIDERS,
    j,
    loop_runner,
    post_job,
    refine_body,
    wait_done,
    worker,
    worker_client,
)


def spec_of(fixture: str) -> dict:
    res = pipeline.run(fixture_bytes(fixture), filename=fixture)
    return refine.spec_from_yaml(res.yaml.encode("utf-8"))


PETS = spec_of("petstore-swagger2.json")  # 20 op, components.schemas 6개, unit 27개
ORDERS = spec_of("orders.wsdl")  # SOAP. properties 순서가 XSD sequence 다
CALC = spec_of("calculator.wsdl")

ADD_SOAP = "/paths/~1operations~1Add/post"


def violations(original, draft, rules=surface.ALLOW_REFINE):
    return [(v.kind, v.pointer) for v in surface.guard(original, draft, rules=rules)]


def get_op(d):
    return d["paths"]["/pet/{petId}"]["get"]


# -- 투영 -------------------------------------------------------------------------------


def test_extract_view_shape():
    view = projection.extract(PETS)
    assert list(view) == ["info", "operations", "components"]
    assert view["info"] == {"description": PETS["info"]["description"]}
    op = view["operations"]["getPetById"]
    assert op == {
        "summary": "Find pet by ID",
        "description": "Returns a single pet",
        "parameters": {"petId": "ID of pet to return"},
    }
    pet = view["components"]["Pet"]
    assert pet["description"] is None  # 없는 설명은 null 로 보내 채울 수 있게 한다
    # $ref 인 속성(category)은 뷰에 없다. items 는 재귀한다
    assert list(pet["properties"]) == ["id", "name", "photoUrls", "tags", "status"]
    assert pet["properties"]["photoUrls"] == {"description": None, "items": {"description": None}}
    assert pet["properties"]["status"] == {"description": "pet status in the store"}
    # operationId 는 프리셋일 때만
    assert "operationId" not in op
    with_id = projection.extract(PETS, rename=True)["operations"]["getPetById"]
    assert list(with_id) == ["operationId", "summary", "description", "parameters"]
    # unit 부분 추출
    part = projection.extract(PETS, units=[("op", "/pet/{petId}", "get"), ("schema", "Tag")])
    assert list(part) == ["operations", "components"]
    assert list(part["operations"]) == ["getPetById"] and list(part["components"]) == ["Tag"]
    assert len(projection.units_of(PETS)) == 27


def test_apply_allowed_fields_and_changes():
    view = projection.extract(PETS)
    view["info"]["description"] = "Petstore."
    view["operations"]["getPetById"]["summary"] = "Fetch one pet"
    view["operations"]["getPetById"]["parameters"]["petId"] = "Pet id."
    view["components"]["Pet"]["description"] = "A pet."  # 없던 설명을 채운다
    view["components"]["Pet"]["properties"]["status"]["description"] = "Status."
    view["operations"]["addPet"]["description"] = ""  # 빈 문자열과 null 은 무변경
    view["operations"]["deletePet"]["summary"] = None
    applied = projection.apply(PETS, view)
    assert applied.unknown == []
    assert violations(PETS, applied.draft) == []
    changes = projection.changes(PETS, applied.draft)
    assert [(c.kind, c.pointer) for c in changes] == [
        ("changed", "/info/description"),
        ("changed", f"{GET_PET}/summary"),
        ("changed", f"{GET_PET}/parameters/0/description"),
        ("changed", "/components/schemas/Pet/properties/status/description"),
        ("added", "/components/schemas/Pet/description"),
    ]
    d = applied.draft
    assert d["info"]["description"] == "Petstore."
    assert get_op(d)["parameters"][0]["description"] == "Pet id."
    assert d["components"]["schemas"]["Pet"]["description"] == "A pet."
    assert d["paths"]["/pet"]["post"].get("description") == PETS["paths"]["/pet"]["post"].get(
        "description"
    )
    assert changes[0].id == projection.change_id("/info/description", "changed")
    assert changes[4].before is None and changes[4].after == "A pet."
    # 채택 전부를 다시 심으면 초안과 같고, 일부만 심으면 그것만 바뀐다
    assert surface.diff(projection.apply_changes(PETS, changes), d) == []
    partial = projection.apply_changes(PETS, changes[:1])
    assert partial["info"]["description"] == "Petstore."
    assert "description" not in partial["components"]["schemas"]["Pet"]


def test_apply_drops_unknown_keys_and_non_strings():
    view = {
        "info": {"description": 42},
        "operations": {
            "getPetById": {
                "summary": "x",
                "parameters": {"petId": "id", "ghost": "no"},
                "tags": ["a"],
            },
            "newOp": {"summary": "new"},
        },
        "components": {
            "Pet": {"properties": {"color": {"description": "no"}}},
            "Nope": {"description": "no"},
        },
        "paths": {"/x": {"get": {"summary": "x"}}},
        "servers": [{"url": "http://evil"}],
    }
    applied = projection.apply(PETS, view)
    assert applied.unknown == [
        "/info/description",
        "/operations/getPetById/parameters/ghost",
        "/operations/getPetById/tags",
        "/operations/newOp/summary",
        "/components/Pet/properties/color/description",
        "/components/Nope/description",
        "/paths/~1x/get/summary",
        "/servers",
    ]
    assert violations(PETS, applied.draft) == []
    assert [(c.kind, c.pointer) for c in projection.changes(PETS, applied.draft)] == [
        ("changed", f"{GET_PET}/summary"),
        ("changed", f"{GET_PET}/parameters/0/description"),
    ]
    assert get_op(applied.draft)["parameters"][0]["description"] == "id"
    # 뷰가 객체가 아니면 전부 unknown
    assert projection.apply(PETS, ["x"]).unknown == ["/"]


def test_full_document_response_is_projected_away():
    """가짜 provider 가 문서 전체를 돌려줘도 투영은 허용 필드만 심는다. paths·required·x-soap 변경은
    버려지고 위반은 0 이다."""
    doc = copy.deepcopy(CALC)
    doc["paths"]["/operations/Add"]["post"]["x-soap"]["soapAction"] = "http://evil/Add"
    doc["paths"]["/operations/Add"]["post"]["requestBody"]["required"] = False
    doc["paths"]["/operations/Divide"]["post"]["summary"] = "Divide two numbers"
    doc["paths"]["/operations/Extra"] = {"get": {"operationId": "extra"}}
    applied = projection.apply(CALC, doc)
    assert surface.diff(CALC, applied.draft) == []
    assert violations(CALC, applied.draft) == []
    assert "/openapi" in applied.unknown and "/x-soap/wsdl" in applied.unknown
    assert f"{ADD_SOAP}/x-soap/soapAction" in applied.unknown
    assert "/paths/~1operations~1Extra/get/operationId" in applied.unknown
    assert "/info/description" not in applied.unknown  # 뷰에 있는 필드는 unknown 이 아니다


def test_properties_order_is_preserved():
    """SOAP properties 순서는 XSD sequence 다. 뷰의 순서를 뒤집어 돌려줘도 원본 순서가 남는다."""
    item = projection.extract(ORDERS)["components"]["Item"]
    assert list(item["properties"]) == ["sku", "quantity", "price", "gift"]
    reply = {
        "components": {
            "Item": {
                "description": "An order line.",
                "properties": {
                    k: {**item["properties"][k], "description": f"{k} field"}
                    for k in reversed(item["properties"])
                },
            }
        }
    }
    applied = projection.apply(ORDERS, reply)
    assert applied.unknown == []
    props = applied.draft["components"]["schemas"]["Item"]["properties"]
    assert list(props) == ["sku", "quantity", "price", "gift"]
    assert props["gift"]["description"] == "gift field"
    assert violations(ORDERS, applied.draft) == []
    # 순서가 실제로 바뀐 문서는 정렬 없이 비교하므로 어떤 규칙으로도 위반이다
    reordered = copy.deepcopy(ORDERS)
    p = reordered["components"]["schemas"]["Item"]["properties"]
    reordered["components"]["schemas"]["Item"]["properties"] = dict(reversed(list(p.items())))
    expected = [("reordered", "/components/schemas/Item/properties")]
    assert violations(ORDERS, reordered) == expected
    assert violations(ORDERS, reordered, surface.ALLOW_MINIFY) == expected
    assert violations(ORDERS, reordered, surface.ALLOW_REFINE_RENAME) == expected


def test_operation_id_only_with_rename_preset():
    view = {"operations": {"getPetById": {"operationId": "getPet"}}}
    dropped = projection.apply(PETS, view)
    assert dropped.unknown == ["/operations/getPetById/operationId"]
    assert surface.diff(PETS, dropped.draft) == []
    applied = projection.apply(PETS, view, rename=True)
    assert applied.unknown == []
    assert get_op(applied.draft)["operationId"] == "getPet"
    assert violations(PETS, applied.draft) == [("changed", f"{GET_PET}/operationId")]
    assert violations(PETS, applied.draft, surface.ALLOW_REFINE_RENAME) == []
    assert violations(PETS, applied.draft, surface.ALLOW_MINIFY) == [
        ("changed", f"{GET_PET}/operationId")
    ]
    assert projection.renamed(PETS, applied.draft) == [{"from": "getPetById", "to": "getPet"}]
    # operationId 가 겹치면 뷰 키가 `id (METHOD path)` 로 갈라져 되먹임에서 가리킬 수 있다
    dup = projection.apply(
        PETS, {"operations": {"getPetById": {"operationId": "addPet"}}}, rename=True
    )
    keys = projection.op_keys(dup.draft)
    assert keys[("/pet", "post")] == "addPet (POST /pet)"
    assert keys[("/pet/{petId}", "get")] == "addPet (GET /pet/{petId})"


def test_surface_rules_table():
    """docs/guide/spec-refine.md 3절의 허용·거절 표(D25). refine 과 minify 규칙에서 각각 통과하는지
    본다."""
    base = copy.deepcopy(PETS)
    get_op(base)["parameters"][0]["example"] = 7
    get_op(base)["x-custom"] = "op-level"
    base["components"]["schemas"]["Pet"]["x-vendor"] = {"a": 1}
    base["components"]["schemas"]["Pet"]["x-pattern"] = "keep"
    base["components"]["schemas"]["Pet"]["properties"]["name"]["example"] = "rex"
    base["x-s2o"] = {"lossy": []}

    def check(name, fn, *, refine_ok, minify_ok):
        d = copy.deepcopy(base)
        fn(d)
        assert (violations(base, d) == []) is refine_ok, name
        assert (violations(base, d, surface.ALLOW_MINIFY) == []) is minify_ok, name

    pet = lambda d: d["components"]["schemas"]["Pet"]  # noqa: E731
    check(
        "description",
        lambda d: get_op(d).__setitem__("description", "x"),
        refine_ok=True,
        minify_ok=True,
    )
    check(
        "summary", lambda d: get_op(d).__setitem__("summary", "x"), refine_ok=True, minify_ok=True
    )
    check(
        "info.description",
        lambda d: d["info"].__setitem__("description", "x"),
        refine_ok=True,
        minify_ok=True,
    )
    check(
        "response description",
        lambda d: get_op(d)["responses"]["200"].__setitem__("description", "x"),
        refine_ok=True,
        minify_ok=True,
    )
    check(
        "property description added",
        lambda d: pet(d)["properties"]["name"].__setitem__("description", "x"),
        refine_ok=True,
        minify_ok=True,
    )
    check(
        "description removed",
        lambda d: get_op(d).pop("description"),
        refine_ok=True,
        minify_ok=True,
    )
    check("tags", lambda d: get_op(d)["tags"].append("x"), refine_ok=False, minify_ok=False)
    check("tags removed", lambda d: get_op(d).pop("tags"), refine_ok=False, minify_ok=False)
    check(
        "param required",
        lambda d: get_op(d)["parameters"][0].__setitem__("required", False),
        refine_ok=False,
        minify_ok=False,
    )
    check(
        "param schema type",
        lambda d: get_op(d)["parameters"][0]["schema"].__setitem__("type", "string"),
        refine_ok=False,
        minify_ok=False,
    )
    check(
        "new operation",
        lambda d: d["paths"].__setitem__("/x", {"get": {"operationId": "x"}}),
        refine_ok=False,
        minify_ok=False,
    )
    check("operation removed", lambda d: d["paths"].pop("/pet"), refine_ok=False, minify_ok=False)
    check(
        "info.title",
        lambda d: d["info"].__setitem__("title", "x"),
        refine_ok=False,
        minify_ok=False,
    )
    check(
        "servers",
        lambda d: d["servers"].__setitem__(0, {"url": "http://evil"}),
        refine_ok=False,
        minify_ok=False,
    )
    check(
        "operationId",
        lambda d: get_op(d).__setitem__("operationId", "getPet"),
        refine_ok=False,
        minify_ok=False,
    )
    check(
        "param example removed",
        lambda d: get_op(d)["parameters"][0].pop("example"),
        refine_ok=False,
        minify_ok=True,
    )
    check(
        "param example hoisted",
        lambda d: get_op(d)["parameters"][0]["schema"].__setitem__(
            "example", get_op(d)["parameters"][0].pop("example")
        ),
        refine_ok=False,
        minify_ok=True,
    )
    check(
        "param example changed",
        lambda d: get_op(d)["parameters"][0].__setitem__("example", 8),
        refine_ok=False,
        minify_ok=False,
    )
    check(
        "schema example removed",
        lambda d: pet(d)["properties"]["name"].pop("example"),
        refine_ok=False,
        minify_ok=True,
    )
    check(
        "schema example changed",
        lambda d: pet(d)["properties"]["name"].__setitem__("example", "max"),
        refine_ok=False,
        minify_ok=False,
    )
    check(
        "schema example added",
        lambda d: pet(d)["properties"]["id"].__setitem__("example", 1),
        refine_ok=False,
        minify_ok=False,
    )
    check(
        "foreign x-* in schema removed",
        lambda d: pet(d).pop("x-vendor"),
        refine_ok=False,
        minify_ok=True,
    )
    check(
        "keep-list x-* in schema removed",
        lambda d: pet(d).pop("x-pattern"),
        refine_ok=False,
        minify_ok=False,
    )
    check(
        "op-level x-* removed",
        lambda d: get_op(d).pop("x-custom"),
        refine_ok=False,
        minify_ok=False,
    )
    check(
        "x-s2o minify record",
        lambda d: d["x-s2o"].__setitem__("minify", {"counts": {"a": 1}}),
        refine_ok=False,
        minify_ok=True,
    )
    check(
        "x-s2o other key",
        lambda d: d["x-s2o"].__setitem__("lossy", ["x"]),
        refine_ok=False,
        minify_ok=False,
    )
    check("x-s2o removed", lambda d: d.pop("x-s2o"), refine_ok=False, minify_ok=False)

    # 루트 x-s2o 가 없던 문서에 minify 가 기록을 새로 쓰는 경우(subtree 추가)는 leaf 단위로 판정한다
    no_s2o = copy.deepcopy(PETS)
    no_s2o.pop("x-s2o", None)
    d = copy.deepcopy(no_s2o)
    d["x-s2o"] = {
        "minify": {"counts": {"foldedErrorLines": 2}, "folded": {"getPetById": ["errors"]}}
    }
    assert violations(no_s2o, d, surface.ALLOW_MINIFY) == []
    assert violations(no_s2o, d) == [("added", "/x-s2o")]
    d["x-s2o"]["lossy"] = []
    assert violations(no_s2o, d, surface.ALLOW_MINIFY) == [("added", "/x-s2o")]

    # x-soap 하위는 description 이라도 보호다 (우선순위 1)
    d = copy.deepcopy(CALC)
    d["paths"]["/operations/Add"]["post"]["x-soap"]["soapAction"] = "x"
    assert violations(CALC, d) == [("changed", f"{ADD_SOAP}/x-soap/soapAction")]
    assert violations(CALC, d, surface.ALLOW_MINIFY) == [
        ("changed", f"{ADD_SOAP}/x-soap/soapAction")
    ]
    d = copy.deepcopy(CALC)
    d["paths"]["/operations/Add"]["post"]["x-soap"]["description"] = "x"
    assert violations(CALC, d) == [("added", f"{ADD_SOAP}/x-soap/description")]
    assert violations(CALC, d, surface.ALLOW_MINIFY) == [
        ("added", f"{ADD_SOAP}/x-soap/description")
    ]


def test_change_ids_and_pointer_helpers():
    assert projection.change_id("/a", "changed") == projection.change_id("/a", "changed")
    assert projection.change_id("/a", "changed") != projection.change_id("/a", "added")
    assert surface.join("paths", "/pet/{petId}", "get") == GET_PET
    assert surface.segments_of(GET_PET) == ("paths", "/pet/{petId}", "get")
    assert surface.resolve(PETS, f"{GET_PET}/parameters/0/name") == "petId"
    doc = {"a": {"b": [1, 2]}}
    surface.set_at(doc, "/a/b/2", 3)
    surface.set_at(doc, "/a/c", "x")
    surface.delete_at(doc, "/a/b/0")
    assert doc == {"a": {"b": [2, 3], "c": "x"}}
    # 1 과 true, 1 과 1.0 은 다른 값이다
    assert [d.kind for d in surface.diff({"a": 1}, {"a": True})] == ["changed"]
    assert [d.kind for d in surface.diff({"a": [1, 2]}, {"a": [1]})] == ["removed"]


def test_changes_refuse_reordered_keys():
    """P73 2절: 표면 검사가 먼저 막는 `reordered` 가 채택 단위까지 오면 검사를 건너뛴 것이라
    오류로 멈춘다."""
    doc = {"a": 1, "b": 2}
    with pytest.raises(ValueError, match="reordered"):
        projection.changes(doc, {"b": 2, "a": 1})
    assert [c.kind for c in projection.changes(doc, {"a": 1, "b": 3})] == ["changed"]


def test_split_batches_and_units_for_findings():
    units = projection.units_of(PETS)
    assert refine.split_batches(PETS, rename=False) == [units]  # 5.4KB 는 40KB 안이다
    batches = refine.split_batches(PETS, rename=False, max_bytes=1, batch_ops=7)
    assert [len(b) for b in batches] == [7, 7, 7, 6]
    assert batches[0][0] == ("info",) and batches[0][1][0] == "op" and batches[3][-1][0] == "schema"
    assert [u for b in batches for u in b] == units

    b = batches[0]
    findings = [
        refine.unknown_finding("/operations/getPetById/parameters/ghost"),
        {"id": "tool-name.safe", "message": "", "location": "paths./pet/{petId}.get"},
        refine.surface_finding(
            surface.Diff("/components/schemas/Pet/description", "changed", None, "x")
        ),
        {"id": "x-soap.refs", "message": "", "location": "components.schemas.Tag.properties.id"},
    ]
    assert refine.units_for_findings(PETS, findings, b) == [
        ("op", "/pet/{petId}", "get"),
        ("schema", "Pet"),
        ("schema", "Tag"),
    ]
    # unit 으로 못 옮기는 항목이 있으면 묶음 전체
    assert refine.units_for_findings(PETS, [refine.finding_from_id("output.json")], b) == b
    assert refine.units_for_findings(PETS, [], b) == b
    assert refine.finding_from_id("refine.unknown:/x")["id"] == "refine.unknown:/x"
    assert refine.finding_from_id("tool-name.safe") is None


def test_presets_match_their_prompt_texts():
    """라우터는 refine.Preset 으로 받고 프롬프트는 PRESET_INSTRUCTIONS 로 만든다. 표에 없는
    이름은 instruction_block 이 조용히 빼므로 둘이 같아야 한다."""
    assert set(refine.PRESETS) == set(refine.PRESET_INSTRUCTIONS)
    assert refine.RENAME_PRESET in refine.PRESETS


def test_prompts():
    view = projection.extract(CALC)
    p = refine.build_first_prompt(
        view,
        presets=["korean_descriptions", "bogus"],
        instruction=" 짧게. " + "x" * 300,
        batch=(2, 3),
    )
    assert "part 2 of 3" in p
    assert refine.PRESET_INSTRUCTIONS["korean_descriptions"] in p
    assert "짧게. " + "x" * (refine.INSTRUCTION_MAX_CHARS - 4) in p and "x" * 250 not in p
    assert '"Add"' in p and '"x-soap"' not in p
    assert "part" not in refine.build_first_prompt(view, presets=[], instruction=None)
    fb = refine.build_feedback_prompt(
        [refine.unknown_finding("/operations/Nope/summary")],
        {"operations": {"Add": view["operations"]["Add"]}},
    )
    assert "[refine.unknown:/operations/Nope/summary] at /operations/Nope/summary" in fb
    assert '"Divide"' not in fb and "Do not add keys" in fb


# -- 루프 (가짜 provider) ----------------------------------------------------------------


async def make_refine_job(store, files, spec: dict, *, presets=(), instruction=None) -> SpecJob:
    job = SpecJob(
        tenant="default",
        provider="oai",
        mode="refine",
        presets=list(presets),
        instruction=instruction,
        source={"backend_profile_id": "bp", "version": 1},
        input_ref="",
    )
    job.input_ref = await files.put(
        input_name(job.id), json.dumps(spec).encode(), job_id=job.id, role="input"
    )
    await store.create(job)
    return job


def test_refine_non_json_keeps_base_and_feeds_back_whole_batch(settings, mongo):
    async def main(store, files, ps):
        fake = FakeProvider(["Sure, here you go.", "{}"])
        runner = await loop_runner(settings, ps, fake)
        job = await make_refine_job(store, files, PETS, presets=["fill_parameter_docs"])
        await worker(store, files, runner, "w1").tick()
        got = await store.get(job.id)
        assert got.state == "done"
        assert got.iterations[0].failed_ids == ["output.json"]
        # JSON 이 아니면 base(원본)를 그대로 초안으로 남긴다
        assert surface.diff(PETS, json.loads(await files.get(draft_name(job.id, 1)))) == []
        p2 = fake.calls[1]["messages"][0]["content"]
        assert "[output.json]" in p2 and '"getPetById"' in p2 and '"components"' in p2

    run_stores(settings, LOOP_STORES, main)


def test_refine_max_iterations_per_batch(settings, mongo):
    async def main(store, files, ps):
        # 반복마다 다른 unknown 이라 no_progress 는 아니고, 묶음당 상한에 걸린다
        fake = FakeProvider(
            [j({"operations": {"a": {"summary": "x"}}}), j({"operations": {"b": {"x": 1}}})]
        )
        runner = await loop_runner(settings, ps, fake, max_iterations=2)
        job = await make_refine_job(store, files, CALC, presets=["enrich_descriptions"])
        await worker(store, files, runner, "w1").tick()
        got = await store.get(job.id)
        assert got.state == "failed" and got.error["code"] == "max_iterations"
        assert "batch 1/1" in got.error["detail"] and len(fake.calls) == 2

    run_stores(settings, LOOP_STORES, main)


def test_refine_resume_violating_draft_is_not_done(settings, mongo):
    """재개 경로도 같은 검사를 거친다. x-soap 이 바뀐 초안은 failed_ids 가 비어 있어도 done 이
    아니고, 위반이 error_ids 에 들어가므로 no_progress 로 끝난다."""

    async def main(store, files, ps):
        fake = FakeProvider(["{}"])
        runner = await loop_runner(settings, ps, fake)
        job = await make_refine_job(store, files, CALC, presets=["enrich_descriptions"])
        await store.acquire("dead", lease_seconds=-1)
        tampered = copy.deepcopy(CALC)
        tampered["paths"]["/operations/Add"]["post"]["x-soap"]["soapAction"] = "http://evil/Add"
        ref = await files.put(
            draft_name(job.id, 1), j(tampered).encode(), job_id=job.id, role="draft"
        )
        await store.push_iteration(
            job.id,
            "dead",
            Iteration(n=1, draft_ref=ref, failed_ids=[], batch=1, batch_total=1),
            usage_total={},
        )
        await worker(store, files, runner, "w2").tick()
        got = await store.get(job.id)
        assert got.state == "failed" and got.error["code"] == "no_progress"
        violation = f"surface.changed:{ADD_SOAP}/x-soap/soapAction"
        assert violation in got.error["detail"]
        assert [it.failed_ids for it in got.iterations[1:]] == [[violation], [violation]]
        assert f"[{violation}]" in fake.calls[0]["messages"][0]["content"]

    run_stores(settings, LOOP_STORES, main)


def test_refine_resume_continues_from_next_batch(settings, mongo):
    """죽은 파드가 묶음 1 을 끝냈으면 새 워커는 묶음 2 만 부르고 묶음 1 의 결과를 유지한다."""

    async def main(store, files, ps):
        fake = FakeProvider(["{}"])
        # unit 27개 → [20, 7]
        runner = await loop_runner(settings, ps, fake, view_max_bytes=1, batch_ops=20)
        job = await make_refine_job(store, files, PETS, presets=["enrich_descriptions"])
        await store.acquire("dead", lease_seconds=-1)
        done1 = copy.deepcopy(PETS)
        done1["info"]["description"] = "Petstore."
        ref = await files.put(draft_name(job.id, 1), j(done1).encode(), job_id=job.id, role="draft")
        await store.push_iteration(
            job.id,
            "dead",
            Iteration(n=1, draft_ref=ref, failed_ids=[], batch=1, batch_total=2),
            usage_total={},
        )
        assert await worker(store, files, runner, "w2").tick() == 1
        got = await store.get(job.id)
        assert got.state == "done", got.error
        assert [(it.n, it.batch, it.batch_total) for it in got.iterations] == [(1, 1, 2), (2, 2, 2)]
        assert len(fake.calls) == 1
        p = fake.calls[0]["messages"][0]["content"]
        assert "part 2 of 2" in p
        assert '"getPetById"' not in p and '"createUser"' in p and '"Pet"' in p
        assert json.loads(await files.get(got.draft_ref))["info"]["description"] == "Petstore."

    run_stores(settings, LOOP_STORES, main)


def test_refine_batches_call_count(settings, mongo):
    async def main(store, files, ps):
        fake = FakeProvider(["{}"])
        runner = await loop_runner(settings, ps, fake, view_max_bytes=1, batch_ops=7)
        job = await make_refine_job(store, files, PETS, presets=["enrich_descriptions"])
        assert await worker(store, files, runner, "w1").tick() == 1
        got = await store.get(job.id)
        assert got.state == "done", got.error
        assert [(it.n, it.batch, it.batch_total) for it in got.iterations] == [
            (1, 1, 4),
            (2, 2, 4),
            (3, 3, 4),
            (4, 4, 4),
        ]
        assert len(fake.calls) == 4
        assert "part 1 of 4" in fake.calls[0]["messages"][0]["content"]
        assert "part 4 of 4" in fake.calls[3]["messages"][0]["content"]
        assert got.usage_total == {"input_tokens": 4000, "output_tokens": 2000}

        # 뷰가 상한 안이면 묶음 하나, 호출 하나
        fake2 = FakeProvider(["{}"])
        runner.provider_factory = lambda cfg, endpoint: fake2
        runner.view_max_bytes = refine.REFINE_VIEW_MAX_BYTES
        job2 = await make_refine_job(store, files, PETS, presets=["enrich_descriptions"])
        await worker(store, files, runner, "w1").tick()
        got2 = await store.get(job2.id)
        assert got2.state == "done"
        assert [(it.batch, it.batch_total) for it in got2.iterations] == [(1, 1)]
        assert "part" not in fake2.calls[0]["messages"][0]["content"]

    run_stores(settings, LOOP_STORES, main)


# -- API -------------------------------------------------------------------------------


def test_refine_job_through_api(settings, mongo, fake_clusters, fake_secrets):
    view = {"operations": {"getPetById": {"summary": "Fetch one pet", "operationId": "getPet"}}}
    with worker_client(settings, fake_clusters, fake_secrets, FakeProvider([j(view)])) as client:
        assert isinstance(client.app.state.spec_worker.runner, SpecGeneratorRunner)
        client.patch("/settings", json=PROVIDERS)
        pid = upload_backend(client, "pets", "petstore-swagger2.json").json()["id"]
        presets = ["enrich_descriptions", "normalize_operation_ids"]
        r = post_job(client, **refine_body(pid, presets=presets, instruction=" Short. "))
        assert r.status_code == 202, r.text
        body = r.json()
        jid = body["id"]
        assert body["mode"] == "refine" and body["state"] == "queued"
        assert body["source"] == {"backend_profile_id": pid, "version": 1}
        assert body["presets"] == presets and body["instruction"] == "Short."
        # 입력은 서버가 GridFS 원본을 JSON 으로 바꿔 넣는다
        doc = mongo["spec_jobs"].find_one({"_id": jid})
        assert doc["input_ref"] == f"{jid}/input.json" and doc["input_size"] > 10_000

        got = wait_done(client, jid)
        assert got["state"] == "done", got
        assert [(it["batch"], it["batch_total"]) for it in got["iterations"]] == [(1, 1)]
        assert got["draft"]["paths"]["/pet/{petId}"]["get"]["summary"] == "Fetch one pet"
        assert {(c["kind"], c["pointer"]) for c in got["changes"]} == {
            ("changed", f"{GET_PET}/summary"),
            ("changed", f"{GET_PET}/operationId"),
        }
        assert all(c["id"] and c["before"] and c["after"] for c in got["changes"])
        assert got["draft_yaml"].startswith("openapi:") and "Fetch one pet" in got["draft_yaml"]
        assert got["renamed"] == [{"from": "getPetById", "to": "getPet"}]
        assert got["verdict"]["level"] == "ok"
        # 목록에는 mode·source 만 있고 changes 는 없다
        listed = client.get(JOBS).json()[0]
        assert listed["mode"] == "refine" and listed["source"]["backend_profile_id"] == pid
        assert "changes" not in listed and "draft" not in listed
        # generate job 의 GET 에는 refine 필드가 null 이다
        r = post_job(client, input={"text": "pets"})
        assert r.status_code == 202
        g = client.get(f"{JOBS}/{r.json()['id']}").json()
        assert g["mode"] == "generate" and g["source"] is None and g["presets"] == []
        assert g["changes"] is None and g["draft_yaml"] is None and g["renamed"] is None


def test_refine_job_prechecks(client, mongo):
    client.patch("/settings", json=PROVIDERS)
    pid = upload_backend(client, "calc", "calculator.wsdl").json()["id"]

    def code(r):
        assert r.status_code == 422, r.text
        return r.json()["detail"]["code"]

    # generate 필드와 refine 필드는 섞을 수 없다
    assert code(post_job(client, **refine_body(pid), input={"text": "x"})) == "invalid_request"
    assert (
        code(post_job(client, mode="refine", presets=["enrich_descriptions"])) == "invalid_request"
    )
    assert (
        code(post_job(client, input={"text": "x"}, presets=["enrich_descriptions"]))
        == "invalid_request"
    )
    assert code(post_job(client, input={"text": "x"}, instruction="x")) == "invalid_request"
    assert code(post_job(client, **refine_body(pid, presets=[]))) == "empty_instruction"
    assert (
        code(post_job(client, **refine_body(pid, presets=[], instruction="   ")))
        == "empty_instruction"
    )
    assert post_job(client, **refine_body(pid, presets=["bogus"])).status_code == 422
    assert post_job(client, **refine_body(pid, instruction="x" * 201)).status_code == 422
    assert (
        post_job(
            client, **refine_body(pid, source={"backend_profile_id": pid, "version": 0})
        ).status_code
        == 422
    )
    # 없는 프로필·버전, 다른 테넌트
    assert code(post_job(client, **refine_body("nope"))) == "source_not_found"
    assert (
        code(post_job(client, **refine_body(pid, source={"backend_profile_id": pid, "version": 9})))
        == "source_not_found"
    )
    assert code(post_job(client, headers=T2, **refine_body(pid))) == "source_not_found"
    # 지시만 있어도 된다
    r = post_job(client, **refine_body(pid, presets=[], instruction="Korean please"))
    assert r.status_code == 202, r.text
    assert r.json()["presets"] == [] and r.json()["instruction"] == "Korean please"

    # 원본 verdict 에 error 가 있으면 막힌다. 저장된 버전에는 error 가 없으므로 문서를 직접 고친다
    versions = mongo["backend_api_versions"]
    results = versions.find_one({"backend_profile_id": pid, "version": 1})["report"]["results"]
    idx = next(i for i, x in enumerate(results) if x["id"] == "tool-description.present")
    versions.update_one(
        {"backend_profile_id": pid, "version": 1},
        {"$set": {f"report.results.{idx}.status": "fail"}},
    )
    r = post_job(client, **refine_body(pid))
    assert code(r) == "source_not_verified"
    assert r.json()["detail"]["verdict"]["level"] == "error"
    # warn 은 통과한다
    versions.update_one(
        {"backend_profile_id": pid, "version": 1},
        {"$set": {f"report.results.{idx}.status": "warn"}},
    )
    assert post_job(client, **refine_body(pid)).status_code == 202


def test_refine_source_too_large(settings, mongo, fake_clusters, fake_secrets):
    app = make_app(settings, fake_clusters, fake_secrets, spec_job_input_max_bytes=1000)
    with api_client(app) as client:
        client.patch("/settings", json=PROVIDERS)
        pid = upload_backend(client, "pets", "petstore-swagger2.json").json()["id"]
        r = post_job(client, **refine_body(pid))
        assert r.status_code == 422, r.text
        detail = r.json()["detail"]
        assert detail["code"] == "source_too_large"
        assert detail["limit"] == 1000 and detail["size"] > 1000
        assert "1000" in detail["detail"] and str(detail["size"]) in detail["detail"]
        # 작은 원본은 통과한다
        cid = upload_backend(client, "calc", "calculator.wsdl").json()["id"]
        assert post_job(client, **refine_body(cid)).status_code == 202
