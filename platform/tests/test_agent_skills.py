"""P39 F6: 플랫폼의 Skills 컴파일과 배포 (D64, 5·6·7절).

그래프 검증의 Skill 코드 전부(`skills_filesystem_disabled` 포함), `skill_not_consumable`, 문서 기능
`skills` 의 이름과 키(Skill 이 없으면 옛 문서와 바이트로 같다), 정책을 좁힌 뒤에도 배포된 Agent 의
컴파일과 Skill 받기는 되고 저장은 422 인 것, 상태 경고 `skill_access_revoked`, Skill 경로의 토큰과
핀 scope(다른 Agent 의 토큰 401, 핀에 없는 이름 404)를 본다. 순수 검사는 문맥에 메타를 바로 싣고,
API 경로는 fake 클러스터로 배포한다.
"""

from __future__ import annotations

import json
from typing import Any

import pytest
from jsonschema import Draft202012Validator

from daegam.auth import rbac
from daegam.k8s.templates.agent import FEATURES_HEADER, IMAGE_HEADER
from daegam.services import agent_features
from daegam.services.agent_features import SKILLS, AgentFeatures, finish_document
from daegam.services.agent_graph import GraphContext, check_schema, schema_validator
from daegam.services.agent_skills import PinnedSkill
from daegam.services.skill_format import skill_digest
from daegam.units import MIB
from tests.helpers.agent_runtime import token_of
from tests.helpers.agents import (
    add_sub_agent,
    agent_env,
    compile_spec,
    create_agent,
    deploy_spec,
    issues_of,
    make_ctx,
    node_of,
    template_for,
    template_spec,
)
from tests.helpers.auth import as_principal, bearer
from tests.helpers.consts import AG, IMAGE, SK, T2
from tests.helpers.docs import agent_doc
from tests.helpers.http import detail_code
from tests.helpers.skills import created_skill, revise_skill

SUB = "agent-sub"


# -- 순수 검사 ------------------------------------------------------------------------------


def _skill(skill_id: str, name: str = "", revision: int = 1, **kw: Any) -> PinnedSkill:
    fields = {"latest": revision, "digest": "sha256:" + "0" * 64, "bytes": 100, "scripts": False}
    fields.update(kw)
    return PinnedSkill(id=skill_id, name=name or skill_id, revision=revision, **fields)


def _ctx(*skills: PinnedSkill) -> GraphContext:
    ctx = make_ctx()
    ctx.skill_revisions = {(s.id, s.revision): s for s in skills}
    return ctx


def _attach(spec: dict[str, Any], node_id: str, *pins: tuple[str, int]) -> None:
    node_of(spec, node_id)["data"]["skills"] = [{"id": i, "revision": r} for i, r in pins]


def _with_subs(*names: str) -> dict[str, Any]:
    """메인이 위임하는 서브에이전트들을 단 템플릿이다. 노드 id 는 `agent-<이름>` 이다."""
    spec = template_spec()
    for name in names:
        add_sub_agent(spec, f"agent-{name}", name, model="model-main")
        edge = {"id": f"e-del-{name}", "source": "agent-main", "target": f"agent-{name}"}
        spec["graph"]["edges"].append({**edge, "kind": "delegate"})
    return spec


def _skill_issues(spec: dict[str, Any], ctx: GraphContext) -> list[tuple[str, str, Any]]:
    return [
        (i["code"], i["level"], i["node_id"])
        for i in issues_of(spec, ctx)
        if "skill" in i["code"] or i["code"] == "invalid_node_data"
    ]


def test_attached_skills_pass_and_compile_into_the_feature_keys():
    """메인에 둘, 서브에 하나를 붙인 그래프는 이슈가 없다. 기능 `skills` 를 요구하고 붙이는 함수가
    최상위 `skills[]`(서로 다른 Skill 마다 하나)와 에이전트별 이름을 싣는다. 최종 문서는 스키마를
    지난다."""
    spec = _with_subs("researcher")
    _attach(spec, "agent-main", ("s-a", 1), ("s-b", 2))
    _attach(spec, "agent-researcher", ("s-a", 1))
    ctx = _ctx(_skill("s-a", "web-research"), _skill("s-b", "report-style", 2, bytes=7))
    assert _skill_issues(spec, ctx) == []
    doc = agent_doc(spec=spec)
    wanted = agent_features.agent_features(doc, doc.current, ctx.providers)
    assert wanted == AgentFeatures(
        required=frozenset({SKILLS}), attached=frozenset({agent_features.TOOL_SAFETY})
    )
    base = compile_spec(spec, ctx)
    assert "skills" not in base and "skills" not in base["main"]
    final = finish_document(base, wanted.wanted, ctx, doc.current)
    assert check_schema(final) == []
    assert final["features"] == ["skills", "tool_safety"]
    assert final["main"]["skills"] == ["web-research", "report-style"]
    assert [s["skills"] for s in final["subagents"]] == [["web-research"]]
    assert final["skills"] == [
        {
            "name": "web-research",
            "id": "s-a",
            "revision": 1,
            "digest": "sha256:" + "0" * 64,
            "bytes": 100,
        },
        {
            "name": "report-style",
            "id": "s-b",
            "revision": 2,
            "digest": "sha256:" + "0" * 64,
            "bytes": 7,
        },
    ]
    assert "skills" not in base  # 받은 문서는 그대로다


def test_without_skills_the_document_is_byte_for_byte_the_old_one():
    ctx = make_ctx()
    spec = template_spec()
    doc = agent_doc(spec=spec)
    wanted = agent_features.agent_features(doc, doc.current, ctx.providers)
    assert wanted == AgentFeatures(attached=frozenset({agent_features.TOOL_SAFETY}))
    base = compile_spec(spec, ctx)
    # 바깥 표시를 모르는 이미지가 받는 문서다
    older = wanted.wanted - {agent_features.TOOL_SAFETY}
    final = finish_document(base, older, ctx, doc.current)
    assert json.dumps(final, sort_keys=False) == json.dumps(base, sort_keys=False)


def test_skills_need_the_file_tools_of_the_main_agent():
    spec = _with_subs("researcher")
    _attach(spec, "agent-main", ("s-a", 1))
    _attach(spec, "agent-researcher", ("s-a", 1))
    node_of(spec, "agent-main")["data"]["builtin_tools"] = {"filesystem": False}
    assert _skill_issues(spec, _ctx(_skill("s-a"))) == [
        ("skills_filesystem_disabled", "error", "agent-main"),
        ("skills_filesystem_disabled", "error", "agent-researcher"),
    ]


def test_too_many_skills_per_agent_and_per_graph():
    many = [_skill(f"s-{i:02}") for i in range(21)]
    spec = template_spec()
    _attach(spec, "agent-main", *((s.id, 1) for s in many[:11]))
    assert _skill_issues(spec, _ctx(*many)) == [("too_many_skills", "error", "agent-main")]
    # 에이전트마다 열 개 안이어도 그래프 전체의 서로 다른 Skill 이 스물을 넘으면 표식 없는 오류다
    spec = _with_subs("one", "two")
    _attach(spec, "agent-main", *((s.id, 1) for s in many[:10]))
    _attach(spec, "agent-one", *((s.id, 1) for s in many[10:20]))
    _attach(spec, "agent-two", (many[20].id, 1))
    assert _skill_issues(spec, _ctx(*many)) == [("too_many_skills", "error", None)]


def test_one_skill_is_pinned_at_one_revision_and_names_do_not_collide():
    spec = _with_subs("researcher")
    _attach(spec, "agent-main", ("s-a", 1), ("s-b", 1))
    _attach(spec, "agent-researcher", ("s-a", 2))
    ctx = _ctx(
        _skill("s-a", "report", latest=2), _skill("s-a", "report", 2), _skill("s-b", "report")
    )
    assert _skill_issues(spec, ctx) == [
        ("skill_behind", "warning", "agent-main"),
        ("skill_name_conflict", "error", "agent-main"),
        ("skill_revision_mismatch", "error", "agent-researcher"),
    ]


def test_unknown_skill_or_revision():
    spec = template_spec()
    _attach(spec, "agent-main", ("s-a", 3), ("s-gone", 1))
    issues = [i for i in issues_of(spec, _ctx(_skill("s-a"))) if i["code"] == "unknown_skill"]
    assert [(i["node_id"], "'s-a' r3" in i["message"]) for i in issues] == [
        ("agent-main", True),
        ("agent-main", False),
    ]


def test_skills_too_large_counts_distinct_pins_and_the_expanded_copies():
    big = [_skill(f"s-{i}", bytes=2 * MIB) for i in range(3)]
    spec = template_spec()
    _attach(spec, "agent-main", *((s.id, 1) for s in big))
    assert _skill_issues(spec, _ctx(*big)) == [("skills_too_large", "error", None)]
    # 서로 다른 핀의 합은 4 MiB 이지만 다섯 에이전트에 펼치면 20 MiB 다
    spec = _with_subs("a", "b", "c", "d")
    for node_id in ("agent-main", "agent-a", "agent-b", "agent-c", "agent-d"):
        _attach(spec, node_id, *((s.id, 1) for s in big[:2]))
    issues = [i for i in issues_of(spec, _ctx(*big)) if i["code"] == "skills_too_large"]
    assert len(issues) == 1 and "written for every agent" in issues[0]["message"]


def test_scripts_and_newer_revisions_only_warn():
    spec = template_spec()
    _attach(spec, "agent-main", ("s-a", 1))
    ctx = _ctx(_skill("s-a", scripts=True, latest=3))
    assert _skill_issues(spec, ctx) == [
        ("skill_scripts_inert", "warning", "agent-main"),
        ("skill_behind", "warning", "agent-main"),
    ]


@pytest.mark.parametrize(
    "skills",
    [
        [{"id": "s-a", "revision": 1}, {"id": "s-a", "revision": 2}],
        [{"id": "s-a", "revision": 0}],
        [{"id": "", "revision": 1}],
        [{"id": "s-a", "revision": 1, "pinned": True}],
    ],
    ids=["duplicate", "revision", "empty_id", "extra_key"],
)
def test_malformed_skills_are_invalid_node_data(skills):
    spec = template_spec()
    node_of(spec, "agent-main")["data"]["skills"] = skills
    assert _skill_issues(spec, _ctx(_skill("s-a"), _skill("s-a", revision=2))) == [
        ("invalid_node_data", "error", "agent-main")
    ]


# -- API: 붙이기, 배포, Skill 받기 ----------------------------------------------------------


def _share(client, skill: dict, **access: Any) -> None:
    r = client.put(f"{SK}/{skill['id']}/access", json=access, headers=T2)
    assert r.status_code == 200, r.text


def _skill_spec(gw: dict, ds: dict, main: list[dict], sub: list[dict]) -> dict[str, Any]:
    """게이트웨이와 Database 를 채운 템플릿에 서브 하나를 달고 Skill 을 붙인다."""
    spec = template_for(gw, ds)
    add_sub_agent(spec, SUB, "researcher", model="model-main")
    edge = {"id": "e-del", "source": "agent-main", "target": SUB, "kind": "delegate"}
    spec["graph"]["edges"].append(edge)
    _attach(spec, "agent-main", *((s["id"], s["revision"]) for s in main))
    _attach(spec, SUB, *((s["id"], s["revision"]) for s in sub))
    return spec


def _init_headers(token: str) -> dict[str, str]:
    return {**bearer(token), FEATURES_HEADER: "skills", IMAGE_HEADER: IMAGE}


def test_a_skill_without_consume_is_refused_and_an_invisible_one_is_unknown(client, mongo):
    _cid, ds, gw = agent_env(client, mongo)
    theirs = created_skill(client, "theirs", headers=T2)
    _share(client, theirs, view={"tenants": ["default"]})
    spec = _skill_spec(gw, ds, [theirs], [])
    for r in (client.post(f"{AG}/validate", json=spec), create_agent(client, spec)):
        assert detail_code(r) == (422, "skill_not_consumable")
        assert r.json()["detail"]["skill_id"] == theirs["id"]
    # 보이지 않는 Skill 은 없는 것과 같다
    _share(client, theirs)
    r = create_agent(client, spec)
    assert detail_code(r) == (422, "invalid_graph")
    assert {i["code"] for i in r.json()["detail"]["issues"]} == {"unknown_skill"}
    # 같은 테넌트의 Skill 이라도 consume 은 주체마다 다르다. manage 도 consume 을 주므로 함께 좁힌다
    mine = created_skill(client, "mine")
    admins = {"roles": ["tenant-admin"]}
    client.put(f"{SK}/{mine['id']}/access", json={"consume": admins, "manage": admins})
    with as_principal(client, "dev", rbac.DEVELOPER):
        r = create_agent(client, _skill_spec(gw, ds, [mine], []))
        assert detail_code(r) == (422, "skill_not_consumable")


def test_a_pin_past_the_latest_revision_is_unknown(client, mongo):
    """최신보다 큰 번호는 저장 중이거나 멈춘 저장이 남긴 리비전이라 나중에 다른 내용으로 채워질 수
    있다. 그 번호를 핀한 그래프는 `unknown_skill` 이다."""
    _cid, ds, gw = agent_env(client, mongo)
    skill = created_skill(client, "mine")
    orphan = mongo["skill_revisions"].find_one({"skill_id": skill["id"], "revision": 1})
    orphan.update({"_id": "orphan-r2", "revision": 2})
    mongo["skill_revisions"].insert_one(orphan)
    spec = _skill_spec(gw, ds, [{**skill, "revision": 2}], [])
    r = client.post(f"{AG}/validate", json=spec)
    assert r.status_code == 200, r.text
    assert [i["code"] for i in r.json()["issues"] if i["level"] == "error"] == ["unknown_skill"]
    # 최신 번호의 핀은 그대로 지난다
    r = client.post(f"{AG}/validate", json=_skill_spec(gw, ds, [skill], []))
    assert r.json()["ok"], r.text


def test_deployed_agent_gets_its_skills_until_the_policy_narrows(client, fake_clusters, mongo):
    """다른 테넌트가 연 Skill 과 자기 Skill 을 붙여 배포한다. init 은 기능 `skills` 로 받은 문서의
    핀마다 Skill 을 받는다. 정책이 좁아져도 받기는 되고 상태가 경고하며, 다음 저장은 422 다."""
    _cid, ds, gw = agent_env(client, mongo)
    theirs = created_skill(client, "theirs", headers=T2, **{"scripts/run.sh": "echo hi"})
    _share(client, theirs, view={"tenants": ["default"]}, consume={"tenants": ["default"]})
    mine = created_skill(client, "mine", **{"references/tone.md": "짧게 쓴다."})
    _p, inst = deploy_spec(client, _skill_spec(gw, ds, [mine, theirs], [mine]))
    assert "runtime_features_unknown" in " ".join(inst["warnings"])
    fake = fake_clusters.by_name["edge"]
    token = token_of(fake, inst)
    url = f"{AG}/{inst['id']}"

    # 이미지가 skills 를 모르면 init 받기가 멈춘다
    r = client.get(f"{url}/agent.json", headers=bearer(token))
    assert r.status_code == 409 and r.json()["detail"]["missing"] == ["skills"]
    document = client.get(f"{url}/agent.json", headers=_init_headers(token)).json()
    assert document["features"] == ["skills"]
    assert document["main"]["skills"] == ["mine", "theirs"]
    assert document["subagents"][0]["skills"] == ["mine"]
    assert [(s["name"], s["revision"], s["digest"]) for s in document["skills"]] == [
        ("mine", 1, mine["latest"]["digest"]),
        ("theirs", 1, theirs["latest"]["digest"]),
    ]

    bundle = client.get(f"{url}/skills/mine", headers=bearer(token))
    assert bundle.status_code == 200, bundle.text
    _check_bundle(bundle.json(), document["skills"][0])
    assert {f["path"] for f in bundle.json()["files"]} == {"SKILL.md", "references/tone.md"}

    # 정책을 좁혀도 핀은 배포 때 허락받은 것이라 받기와 컴파일은 그대로다
    _share(client, theirs, view={"tenants": ["default"]})
    assert client.get(f"{url}/agent.json", headers=_init_headers(token)).json() == document
    bundle = client.get(f"{url}/skills/theirs", headers=bearer(token))
    assert bundle.status_code == 200
    _check_bundle(bundle.json(), document["skills"][1])
    assert "skill_access_revoked: theirs" in client.get(url).json()["status"]["warnings"]
    # 다음 저장과 재배포는 422 다. 정의를 바꾸지 않은 저장은 검증을 하지 않는다
    agent = client.get(url).json()
    spec = {"graph": agent["graph"], "service": agent["service"]}
    node_of(spec, "agent-main")["data"]["system_prompt"] = "바꾼다."
    r = client.put(url, json={"name": "support", **spec})
    assert detail_code(r) == (422, "skill_not_consumable")
    r = client.post(f"{url}/redeploy")
    assert detail_code(r) == (422, "skill_not_consumable")


def _check_bundle(bundle: dict[str, Any], pin: dict[str, Any]) -> None:
    """`$defs/skill_bundle` 모양이고 digest 가 agent.json 의 핀과 같다."""
    schema = schema_validator().schema
    Draft202012Validator({"$ref": "#/$defs/skill_bundle", "$defs": schema["$defs"]}).validate(
        bundle
    )
    assert (bundle["name"], bundle["id"], bundle["revision"]) == (
        pin["name"],
        pin["id"],
        pin["revision"],
    )
    files = [(f["path"], f["content"]) for f in bundle["files"]]
    assert skill_digest(files) == bundle["digest"] == pin["digest"]


def test_skill_route_needs_this_agents_token_and_a_pinned_name(client, fake_clusters, mongo):
    _cid, ds, gw = agent_env(client, mongo)
    skill = created_skill(client, "mine")
    _p, inst = deploy_spec(client, _skill_spec(gw, ds, [skill], []))
    _p2, other = deploy_spec(client, _skill_spec(gw, ds, [], []), "other")
    fake = fake_clusters.by_name["edge"]
    url = f"{AG}/{inst['id']}/skills/mine"
    assert client.get(url).status_code == 401
    r = client.get(url, headers=bearer(token_of(fake, other)))
    assert r.status_code == 401
    mine = bearer(token_of(fake, inst))
    assert client.get(url, headers=mine).status_code == 200
    # 핀에 없는 이름과 핀하지 않은 리비전은 없는 것이다
    r = client.get(f"{AG}/{inst['id']}/skills/theirs", headers=mine)
    assert detail_code(r) == (404, "not_found")
    assert revise_skill(client, skill, "# v2\n", expect=1).status_code == 201
    bundle = client.get(url, headers=mine).json()
    assert bundle["revision"] == 1 and bundle["digest"] == skill["latest"]["digest"]
