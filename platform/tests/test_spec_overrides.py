"""P19 F1~F4: overrides 모드와 채택 (D37).

단위: 뷰 키(backend index, operationId)·노출 집합·투영(새 항목 만들기, None 건너뛰기, unknown)·
표면 규칙(leaf 와 항목 통째, removed·빈 dict 거부)·채택 단위 두 모양·이름 검사 네 코드·조각 뽑기.
API: 사전 검사(invalid_request·empty_instruction·source_not_found)와 job 한 바퀴, 그리고 목록이
오버라이드 job 이 있어도 200 인 것.
재개: 저장된 초안은 뷰가 아니라 조각이라 투영 없이 판정한다. 통과한 초안은 LLM 없이 done 이고,
실패한 초안은 그 위에서 되먹임을 잇는다.
채택: 새 리비전·일부 채택·unknown_change·no_changes·job 상태 검사, 그리고 낡은 채택 판정이
`backends` 만 본다는 것(runtime 만 바뀌었으면 그 편집을 살린 채 얹는다).
"""

from __future__ import annotations

import json

from daegam.services.spec_generator import overrides, projection, refine, surface
from daegam.store.backend_apis import BackendAPIVersion
from daegam.store.mcp_servers import MCPServerRevision
from daegam.store.spec_jobs import Iteration, SpecJob, draft_name, input_name
from tests.helpers.aio import run_stores
from tests.helpers.backend_apis import uploaded_backend
from tests.helpers.consts import JOBS, MS
from tests.helpers.fake_llm import FakeProvider
from tests.helpers.mcp_servers import create_server
from tests.helpers.spec_jobs import (
    LOOP_STORES,
    PROVIDERS,
    j,
    loop_runner,
    post_job,
    wait_done,
    worker,
    worker_client,
)

NAMES = [["Add", "Divide"]]


def version(names, details=()):
    return BackendAPIVersion(
        backend_profile_id="bp",
        version=1,
        kind="soap",
        source_format="wsdl",
        source_ext=".wsdl",
        source_sha256="a",
        output_sha256="b",
        s2o_version="0.6.0",
        tool_count=len(names),
        tool_names=list(names),
        tool_details=list(details),
    )


def frag(tools=(), ovs=None):
    return {
        "backends": [
            {
                "backend_profile_id": "bp",
                "spec_version": 1,
                "tools": list(tools),
                "tool_overrides": dict(ovs or {}),
            }
        ]
    }


# -- 뷰 ---------------------------------------------------------------------------------


def test_view_is_keyed_by_backend_and_operation():
    """평면 map 이면 backend 둘의 같은 operationId 가 한 칸으로 합쳐진다 (D37)."""
    two = {
        "backends": [
            {"backend_profile_id": "a", "spec_version": 1, "tools": [], "tool_overrides": {}},
            {
                "backend_profile_id": "b",
                "spec_version": 1,
                "tools": ["Add"],
                "tool_overrides": {"Add": {"name": "add2"}},
            },
        ]
    }
    view = overrides.extract(two, [["Add", "Divide"], ["Add", "Divide"]])
    assert list(view["backends"]) == ["0", "1"]
    # backend 0 은 tools 가 비어 전부 노출이고, backend 1 은 고른 것만이다
    assert list(view["backends"]["0"]) == ["Add", "Divide"]
    assert list(view["backends"]["1"]) == ["Add"]
    # 같은 operationId 가 backend 마다 따로 산다
    assert view["backends"]["0"]["Add"] == {"name": None, "description": None}
    assert view["backends"]["1"]["Add"] == {"name": "add2", "description": None}


def test_view_drops_operations_the_surface_guard_escapes():
    """표면 보호는 경로에 x-s2o·x-soap 세그먼트가 있으면 규칙을 보기 전에 거부한다. 오버라이드
    pointer 에서는 그 자리가 operationId 라, 그런 이름의 툴을 뷰에 담으면 모델이 무엇을 답해도
    표면 위반이고 되먹임이 "protected field" 라 고칠 수도 없다. 뷰에서 뺀다."""
    names = [["Add", "x-soapAdd", "x-s2o"]]
    view = overrides.extract(frag(), names)
    assert list(view["backends"]["0"]) == ["Add"]

    # 뷰에 없으니 심는 자리도 없다. 원본은 한 글자도 안 바뀐다
    draft = overrides.apply(frag(), view, names).draft
    assert surface.guard(frag(), draft, rules=surface.ALLOW_OVERRIDES) == []


def test_view_holds_only_editable_fields():
    """원본 summary 는 뷰가 아니라 참고 블록에 있다. 섞으면 모델이 고친 것을 버리게 된다."""
    v = version(["Add", "Divide"], [{"operation_id": "Add", "summary": "adds two"}])
    view = overrides.extract(frag(), NAMES)
    assert view["backends"]["0"]["Add"] == {"name": None, "description": None}
    ctx = overrides.context_of(frag(), NAMES, overrides.details_by_op([v]))
    assert ctx["backends"]["0"]["Add"] == {"summary": "adds two", "description": None}


# -- 투영 -------------------------------------------------------------------------------


def test_apply_skips_null_quietly_and_reports_the_rest():
    """None·빈 문자열은 "그대로 두겠다" 는 정당한 답이라 finding 이 아니다. 되먹이면 모델이 같은
    답을 다시 내 no_progress 로 접힌다 (D37)."""
    applied = overrides.apply(
        frag(), {"backends": {"0": {"Add": {"name": None, "description": ""}}}}, NAMES
    )
    assert applied.unknown == []
    assert applied.draft == frag()

    bad = overrides.apply(
        frag(),
        {"backends": {"0": {"Nope": {"name": "x"}, "Add": {"name": 3, "description": None}}}},
        NAMES,
    )
    assert sorted(bad.unknown) == ["/backends/0/Add/name", "/backends/0/Nope/name"]


# -- 표면 보호 ---------------------------------------------------------------------------


def rules_allow(pointer: str, kind: str) -> bool:
    return surface.ALLOW_OVERRIDES.allows(surface.segments_of(pointer), kind)


def test_allow_overrides_table():
    assert rules_allow("/backends/0/tool_overrides/Add/name", "changed")
    assert rules_allow("/backends/0/tool_overrides/Add/description", "added")
    # 지우기는 사람이 폼에서 한다. 열어 주면 항목 통째 삭제까지 같이 열린다
    assert not rules_allow("/backends/0/tool_overrides/Add/name", "removed")
    # 오버라이드 밖은 무엇도 안 된다
    assert not rules_allow("/backends/0/tools/0", "changed")
    assert not rules_allow("/backends/0/spec_version", "changed")
    assert not rules_allow("/runtime/replicas", "changed")


def test_guard_accepts_whole_entry_but_not_empty_one():
    base = frag()
    ok = overrides.apply(frag(), {"backends": {"0": {"Add": {"name": "addNumbers"}}}}, NAMES).draft
    assert surface.guard(base, ok, rules=surface.ALLOW_OVERRIDES) == []

    # 빈 dict 는 leaf 가 네 세그먼트라 다섯 세그먼트 규칙에 안 맞는다
    empty = json.loads(json.dumps(base))
    empty["backends"][0]["tool_overrides"]["Add"] = {}
    viol = surface.guard(base, empty, rules=surface.ALLOW_OVERRIDES)
    assert [(v.pointer, v.kind) for v in viol] == [("/backends/0/tool_overrides/Add", "added")]

    # 오버라이드 밖을 건드리면 걸린다
    moved = json.loads(json.dumps(base))
    moved["backends"][0]["spec_version"] = 2
    assert surface.guard(base, moved, rules=surface.ALLOW_OVERRIDES)


# -- 채택 단위 ---------------------------------------------------------------------------


def test_changes_have_two_shapes_and_round_trip():
    base = frag(ovs={"Add": {"name": "add1", "description": None}})
    answer = {
        "backends": {
            "0": {
                "Add": {"name": "addNumbers", "description": "설명"},
                "Divide": {"name": "divideNumbers", "description": None},
            }
        }
    }
    applied = overrides.apply(base, answer, NAMES)
    assert applied.unknown == []
    draft = applied.draft
    # 있던 항목은 leaf 를 고친다. 없던 항목은 set_at 이 부모를 만들지 않아 투영이 통째로 만든다
    assert draft["backends"][0]["tool_overrides"]["Add"] == {
        "name": "addNumbers",
        "description": "설명",
    }
    ch = projection.changes(base, draft)
    assert [(c.kind, c.pointer) for c in ch] == [
        ("changed", "/backends/0/tool_overrides/Add/name"),
        ("changed", "/backends/0/tool_overrides/Add/description"),
        # 없던 항목은 leaf 로 쪼개지지 않는다. 이름만 받고 설명은 거절하는 선택이 없다
        ("added", "/backends/0/tool_overrides/Divide"),
    ]
    assert ch[2].after == {"name": "divideNumbers"}
    assert projection.apply_changes(base, ch) == draft
    # 일부만 채택해도 심긴다
    partial = projection.apply_changes(base, [ch[2]])
    assert partial["backends"][0]["tool_overrides"]["Add"]["name"] == "add1"
    assert partial["backends"][0]["tool_overrides"]["Divide"] == {"name": "divideNumbers"}


# -- 이름 검사 ---------------------------------------------------------------------------


def test_check_names_covers_four_codes():
    vs = [version(["Add", "Divide"])]
    assert overrides.check_names(frag(ovs={"Add": {"name": "add1"}}), vs) == []

    def codes(draft, versions=None):
        return [f["id"] for f in overrides.check_names(draft, versions or vs)]

    # 규칙을 어긴 이름과 56자 초과
    assert codes(frag(ovs={"Add": {"name": "bad-name"}})) == ["overrides.invalid_tool_name:0/Add"]
    assert codes(frag(ovs={"Add": {"name": "a" * 57}})) == ["overrides.invalid_tool_name:0/Add"]
    # 핀한 버전에 없는 operationId. 표면 규칙의 가운데 `*` 는 아무것이나 맞으므로 이것만이 잡는다
    assert codes(frag(ovs={"Nope": {"name": "x"}})) == ["overrides.unknown_tools:0/Nope"]
    # 프로필 전체 겹침
    assert codes(frag(ovs={"Add": {"name": "Divide"}})) == ["overrides.duplicate_tool_name:Divide"]
    # 모양이 깨지면 pydantic 이 아니라 finding 으로 나와야 판정이 안 죽는다
    assert codes({"backends": "nope"}) == ["overrides.invalid_shape"]


def test_check_names_gathers_every_backend():
    """validate_tools 는 첫 문제에서 던진다. backend 별로 직접 걸어야 한 번에 다 돌려준다."""
    two = {
        "backends": [
            {
                "backend_profile_id": "a",
                "spec_version": 1,
                "tools": ["Add"],
                "tool_overrides": {"Add": {"name": "bad-name"}},
            },
            {
                "backend_profile_id": "b",
                "spec_version": 1,
                "tools": ["Add"],
                "tool_overrides": {"Add": {"name": "_x_"}},
            },
        ]
    }
    ids = [f["id"] for f in overrides.check_names(two, [version(["Add"]), version(["Add"])])]
    assert ids == ["overrides.invalid_tool_name:0/Add", "overrides.invalid_tool_name:1/Add"]


def test_presets_are_a_subset_of_the_api_presets():
    """라우터는 refine.Preset 으로 먼저 거른다. 여기 없는 이름은 요청에 실릴 수 없다."""
    assert set(overrides.PRESETS) <= set(refine.PRESETS)


def test_spec_of_drops_revision_metadata():
    """revision·created_at 이 섞이면 낡은 채택 비교가 늘 실패해 채택이 항상 422 가 된다."""
    rev = MCPServerRevision(
        revision=3,
        backends=[{"backend_profile_id": "bp", "spec_version": 1}],
    )
    spec = overrides.spec_of(rev)
    assert "revision" not in spec and "created_at" not in spec
    assert set(spec) == {"backends", "runtime", "bindings"}
    assert overrides.fragment(spec) == {"backends": spec["backends"]}


# -- API --------------------------------------------------------------------------------


def overrides_body(pid: str, **kw) -> dict:
    return {
        "mode": "overrides",
        "source": {"mcp_server_id": pid, "revision": 1},
        "presets": ["enrich_descriptions"],
        **kw,
    }


def test_overrides_job_prechecks(client, mongo):
    client.patch("/settings", json=PROVIDERS)
    bp = uploaded_backend(client)
    pid = create_server(client, bp, tools=["Add"]).json()["id"]

    def code(**body):
        r = post_job(client, **body)
        assert r.status_code == 422, r.text
        return r.json()["detail"]["code"]

    # source 의 모양이 모드를 가른다. refine 모양을 보내면 거절이다
    refine_shape = {"backend_profile_id": bp["id"], "version": 1}
    assert code(**overrides_body(pid), input={"text": "x"}) == "invalid_request"
    assert code(mode="overrides", source=refine_shape) == "invalid_request"
    assert code(mode="overrides", presets=["enrich_descriptions"]) == "invalid_request"
    # refine 전용 프리셋은 이 모드에 뜻이 없다
    assert code(**overrides_body(pid, presets=["normalize_operation_ids"])) == "invalid_request"
    assert code(**overrides_body(pid, presets=[])) == "empty_instruction"
    assert code(**overrides_body("nope")) == "source_not_found"
    assert code(**overrides_body(pid, source={"mcp_server_id": pid, "revision": 9})) == (
        "source_not_found"
    )


def test_overrides_job_feeds_findings_back(settings, mongo, fake_clusters, fake_secrets):
    bad = {"backends": {"0": {"Add": {"name": "bad-name", "description": None}}}}
    good = {"backends": {"0": {"Add": {"name": "addNumbers", "description": None}}}}
    fake = FakeProvider([j(bad), j(good)])
    with worker_client(settings, fake_clusters, fake_secrets, fake) as client:
        client.patch("/settings", json=PROVIDERS)
        bp = uploaded_backend(client)
        pid = create_server(client, bp, tools=["Add"]).json()["id"]
        jid = post_job(client, **overrides_body(pid)).json()["id"]

        got = wait_done(client, jid)
        assert got["state"] == "done", got
        assert [it["failed_ids"] for it in got["iterations"]] == [
            ["overrides.invalid_tool_name:0/Add"],
            [],
        ]
        # 되먹임 프롬프트가 finding 을 실어 보낸다
        assert "invalid_tool_name" in fake.calls[1]["messages"][0]["content"]


# -- 재개 -------------------------------------------------------------------------------


async def seed_resumable(store, files, saved: dict, failed_ids: list[str]):
    """죽은 파드가 반복 1 을 남기고 리스가 만료된 overrides job. saved 는 저장된 초안(조각)이다."""
    job = SpecJob(
        tenant="default",
        provider="oai",
        mode="overrides",
        presets=["enrich_descriptions"],
        source={"mcp_server_id": "ms", "revision": 1},
        input_ref="",
    )
    inp = {"spec": frag(), "versions": [version(NAMES[0]).model_dump(mode="json")]}
    job.input_ref = await files.put(
        input_name(job.id), j(inp).encode(), job_id=job.id, role="input"
    )
    await store.create(job)
    await store.acquire("dead", lease_seconds=-1)
    ref = await files.put(draft_name(job.id, 1), j(saved).encode(), job_id=job.id, role="draft")
    await store.push_iteration(
        job.id, "dead", Iteration(n=1, draft_ref=ref, failed_ids=failed_ids), usage_total={}
    )
    return job, ref


def test_resume_when_last_overrides_draft_already_passes(settings, mongo):
    """저장된 초안을 뷰로 다시 투영하면 `/backends` 가 unknown 이 되어 영영 done 이 되지 않았다."""

    async def main(store, files, ps):
        fake = FakeProvider([j({"backends": {}})])
        runner = await loop_runner(settings, ps, fake)
        saved = frag(ovs={"Add": {"name": "addNumbers"}})
        job, ref = await seed_resumable(store, files, saved, [])
        assert await worker(store, files, runner, "w2").tick() == 1
        got = await store.get(job.id)
        assert got.state == "done", got.error
        assert got.draft_ref == ref
        assert fake.calls == []  # LLM 을 다시 부르지 않는다

    run_stores(settings, LOOP_STORES, main)


def test_resume_continues_feedback_on_the_saved_draft(settings, mongo):
    """실패한 초안은 원본이 아니라 그 초안 위에서 이어 간다. 앞 반복에서 고친 이름이 남는다."""

    async def main(store, files, ps):
        good = {"backends": {"0": {"Divide": {"name": "divideNumbers", "description": None}}}}
        fake = FakeProvider([j(good)])
        runner = await loop_runner(settings, ps, fake)
        saved = frag(ovs={"Add": {"name": "addNumbers"}, "Divide": {"name": "bad-name"}})
        failed = ["overrides.invalid_tool_name:0/Divide"]
        job, _ref = await seed_resumable(store, files, saved, failed)
        assert await worker(store, files, runner, "w2").tick() == 1
        got = await store.get(job.id)
        assert got.state == "done", got.error
        assert [it.n for it in got.iterations] == [1, 2]
        # 되먹임은 저장된 초안의 현재 값과 진짜 finding 만 싣는다
        fb = fake.calls[0]["messages"][0]["content"]
        assert "[overrides.invalid_tool_name:0/Divide]" in fb
        assert '"addNumbers"' in fb
        assert "overrides.unknown:" not in fb
        draft = json.loads(await files.get(got.draft_ref))
        assert draft["backends"][0]["tool_overrides"] == {
            "Add": {"name": "addNumbers"},
            "Divide": {"name": "divideNumbers"},
        }

    run_stores(settings, LOOP_STORES, main)


# -- 채택 (F4) ---------------------------------------------------------------------------


ADD_ONLY = {"backends": {"0": {"Add": {"name": "addNumbers", "description": "두 수를 더한다"}}}}


def apply_url(pid: str) -> str:
    return f"{MS}/{pid}/apply-draft"


def ready(client):
    """프로필 하나를 만들고 overrides job 을 done 까지 돌린다. (Backend API, profile id, job id)
    다."""
    client.patch("/settings", json=PROVIDERS)
    bp = uploaded_backend(client)
    pid = create_server(client, bp, tools=["Add"]).json()["id"]
    jid = post_job(client, **overrides_body(pid)).json()["id"]
    assert wait_done(client, jid)["state"] == "done"
    return bp, pid, jid


def put_profile(client, pid, bp, **extra):
    body = {
        "name": "calc-mcp",
        "backend_profile_id": bp["id"],
        "spec_version": 1,
        "tools": ["Add"],
        **extra,
    }
    return client.put(f"{MS}/{pid}", json=body)


def test_apply_draft_job_checks(settings, mongo, fake_clusters, fake_secrets):
    with worker_client(
        settings, fake_clusters, fake_secrets, FakeProvider([j(ADD_ONLY)])
    ) as client:
        bp, pid, jid = ready(client)

        def code(body, pid_=None):
            r = client.post(apply_url(pid_ or pid), json=body)
            assert r.status_code == 422, r.text
            return r.json()["detail"]["code"]

        assert code({"job_id": "nope"}) == "job_not_found"
        # 모르는 채택 id 는 서버가 changes 를 다시 계산하기 때문에 잡힌다
        assert code({"job_id": jid, "accepted_ids": ["deadbeef0000"]}) == "unknown_change"
        # 다른 프로필의 job 은 받지 않는다
        other = create_server(client, bp, name="calc-mcp2", tools=["Add"]).json()["id"]
        assert code({"job_id": jid}, other) == "source_mismatch"
        # refine job 을 보내면 모드가 다르다
        rid = post_job(
            client,
            mode="refine",
            source={"backend_profile_id": bp["id"], "version": 1},
            presets=["enrich_descriptions"],
        ).json()["id"]
        assert code({"job_id": rid}) in ("job_not_overrides", "job_not_done")


def test_apply_draft_rejects_a_stale_backends_edit(settings, mongo, fake_clusters, fake_secrets):
    """낡은 채택 판정은 현재 리비전의 backends 와 job 이 본 것을 견준다. 핀한 리비전과 견주면
    리비전이 불변 스냅샷이라 영원히 같아서 보호가 사라진다 (D37)."""
    with worker_client(
        settings, fake_clusters, fake_secrets, FakeProvider([j(ADD_ONLY)])
    ) as client:
        bp, pid, jid = ready(client)
        assert put_profile(client, pid, bp, tools=["Add", "Divide"]).status_code == 200
        r = client.post(apply_url(pid), json={"job_id": jid})
        assert r.status_code == 422, r.text
        assert r.json()["detail"]["code"] == "source_mismatch"


def test_apply_draft_keeps_a_runtime_only_edit(settings, mongo, fake_clusters, fake_secrets):
    """비교 범위가 backends 뿐이라 replicas 만 바꾼 편집은 거부하지 않고 함께 살린다 (D37)."""
    with worker_client(
        settings, fake_clusters, fake_secrets, FakeProvider([j(ADD_ONLY)])
    ) as client:
        bp, pid, jid = ready(client)
        assert put_profile(client, pid, bp, runtime={"replicas": 3}).status_code == 200

        r = client.post(apply_url(pid), json={"job_id": jid})
        assert r.status_code == 201, r.text
        got = r.json()
        assert got["runtime"]["replicas"] == 3
        assert got["backends"][0]["tool_overrides"]["Add"]["name"] == "addNumbers"


def test_apply_draft_with_nothing_to_apply(settings, mongo, fake_clusters, fake_secrets):
    """모델이 전부 null 로 답하면 초안이 원본과 같다. 채택할 것이 없다."""
    nothing = {"backends": {"0": {"Add": {"name": None, "description": None}}}}
    with worker_client(settings, fake_clusters, fake_secrets, FakeProvider([j(nothing)])) as client:
        _bp, pid, jid = ready(client)
        assert client.get(f"{JOBS}/{jid}").json()["changes"] == []
        r = client.post(apply_url(pid), json={"job_id": jid})
        assert r.status_code == 422, r.text
        assert r.json()["detail"]["code"] == "no_changes"
