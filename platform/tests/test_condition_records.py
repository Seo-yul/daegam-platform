"""P52 F4: 서버의 파드가 인자 조건을 지키는지의 기록과 그것을 따르는 Cedar.

조건이 있는 Tool 은 첫 배포, 옛 이미지, 판을 내린 이미지, 조건이 다른 리비전으로 옮기는 재배포에서
닫히고, 모든 파드가 같은 digest 와 배포한 리비전으로 돌며 `initialize` 가 `arg_conditions` 를 알리면
다음 재적용이 연다. 읽지 못하면 옛 기록이 남지 않는다. profile.json 은 읽기 전용 표시와 조건이 있는
operationId 만 `rules` 로 싣는다.
"""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any

import pytest

from daegam.k8s.labels import LABEL_PROFILE_REVISION
from daegam.services.condition_records import (
    ConditionRecords,
    conditions_enforced,
    pod_images,
    pod_revision,
    record_from,
    unresolved_conditions,
)
from daegam.services.mcp_spec import render_profile_json
from daegam.services.reconcile import Reconciler
from daegam.store.backend_apis import BackendAPI, BackendAPIVersion
from daegam.store.mcp_servers import (
    BackendSelection,
    ConditionsRecord,
    MCPServer,
    MCPServerRevision,
    MCPServerSpec,
)
from daegam.store.tool_rules import ArgCondition, ServerToolRule, ToolCallers
from tests.helpers.cedar_eval import allowed
from tests.helpers.consts import DEFAULT_NS, GV, MS
from tests.helpers.workers import run_data_plane_auth
from tests.test_tool_rules_render import (
    _codes,
    _deployed,
    _policies,
    _put_rules,
    forget_rule_caches,
)

NOW = datetime(2026, 11, 9, tzinfo=UTC)
IMAGE_ID = "registry/daegam-mcp-runtime@sha256:aaa"
# `_deployed` 가 배포하는 서버의 CR 이름. 프록시 러너의 StatefulSet 도 이 이름이다
CR = "mcp-calc-mcp"
COND = {"conditions": [{"path": "a", "op": "max", "value": 10}]}
VMCP = {"sub": "svc:vmcp:gw-a", "tenant": "default", "rules": "x"}
ENFORCES = {"arg_conditions": {"version": 1, "unresolved": {}}}


# -- profile.json -----------------------------------------------------------------------------


def _version(kind: str = "openapi") -> BackendAPIVersion:
    return BackendAPIVersion(
        backend_profile_id="bp",
        version=1,
        kind=kind,
        source_format="openapi",
        source_ext="yaml",
        source_sha256="s",
        output_sha256="o",
        s2o_version="0.7.0",
        tool_count=2,
        tool_names=["Add", "Divide"],
    )


def _api(i: int) -> BackendAPI:
    return BackendAPI(id=f"bp{i}", tenant="default", name=f"calc{i}", display_name=f"calc{i}")


def _selection(i: int, rules: dict[str, ServerToolRule]) -> BackendSelection:
    return BackendSelection(backend_profile_id=f"bp{i}", spec_version=1, tool_rules=rules)


def test_profile_json_carries_marks_and_conditions_only():
    cond = ArgCondition(path="a", op="max", value=10)
    rules = {
        "Add": ServerToolRule(read_only=True, conditions=[cond]),
        "Divide": ServerToolRule(callers=ToolCallers(roles=["developer"])),
    }
    spec = MCPServerSpec(backends=[_selection(0, rules)])
    flat = render_profile_json(spec, [(_api(0), _version())])
    assert flat["rules"] == {
        "Add": {
            "read_only": True,
            "conditions": [{"path": "a", "op": "max", "value": 10, "optional": False}],
        }
    }
    plain = render_profile_json(
        MCPServerSpec(backends=[_selection(0, {"Divide": rules["Divide"]})]),
        [(_api(0), _version())],
    )
    assert "rules" not in plain


def test_profile_json_carries_rules_per_backend():
    spec = MCPServerSpec(
        backends=[
            _selection(0, {"Add": ServerToolRule(read_only=False)}),
            _selection(1, {}),
        ]
    )
    out = render_profile_json(spec, [(_api(0), _version()), (_api(1), _version())])
    assert out["backends"][0]["rules"] == {"Add": {"read_only": False, "conditions": []}}
    assert "rules" not in out["backends"][1]


# -- 순수 판정 ----------------------------------------------------------------------------------


def _sts(*, replicas: int = 1, ready: int = 1, rolled: bool = True) -> dict[str, Any]:
    return {
        "apiVersion": "apps/v1",
        "kind": "StatefulSet",
        "metadata": {"name": CR, "namespace": DEFAULT_NS, "generation": 2},
        "spec": {"replicas": replicas, "selector": {"matchLabels": {"app": CR}}},
        "status": {
            "observedGeneration": 2,
            "replicas": replicas,
            "readyReplicas": ready,
            "currentRevision": "r1",
            "updateRevision": "r1" if rolled else "r2",
        },
    }


def _pod(
    name: str,
    image_id: str | None = IMAGE_ID,
    *,
    ready: bool = True,
    image: str = "rt:1",
    revision: int | None = 2,
    init: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    state: dict[str, Any] = {"name": "mcp", "ready": ready}
    if image_id:
        state["imageID"] = image_id
    labels = {"app": CR}
    if revision is not None:
        labels[LABEL_PROFILE_REVISION] = str(revision)
    spec: dict[str, Any] = {"containers": [{"name": "mcp", "image": image}]}
    if init is not None:
        spec["initContainers"] = init
    return {
        "apiVersion": "v1",
        "kind": "Pod",
        "metadata": {"name": name, "namespace": DEFAULT_NS, "labels": labels},
        "spec": spec,
        "status": {"phase": "Running", "containerStatuses": [state]},
    }


def test_pod_images_is_uniform_only_when_every_pod_runs_one_ready_digest():
    assert pod_images(_sts(), [_pod("p0")]).uniform == ("rt:1", IMAGE_ID, 2)
    assert pod_images(_sts(rolled=False), [_pod("p0")]).uniform is None
    assert pod_images(_sts(), [_pod("p0", ready=False)]).uniform is None
    assert pod_images(_sts(), [_pod("p0", None)]).uniform is None
    two = pod_images(_sts(replicas=2, ready=2), [_pod("p0"), _pod("p1", "other@sha256:b")])
    assert two.uniform is None and two.ids == {IMAGE_ID, "other@sha256:b"}
    done = {**_pod("p1", "other@sha256:b"), "status": {"phase": "Succeeded"}}
    assert pod_images(_sts(), [_pod("p0"), done]).uniform == ("rt:1", IMAGE_ID, 2)


def test_pod_images_needs_one_known_revision():
    mixed = pod_images(_sts(replicas=2, ready=2), [_pod("p0"), _pod("p1", revision=1)])
    assert mixed.uniform is None and mixed.revisions == {1, 2}
    unknown = pod_images(_sts(), [_pod("p0", revision=None)])
    assert unknown.uniform is None and unknown.unknown_revision


def test_a_pods_revision_falls_back_to_the_profile_url_of_its_init_container():
    """ToolHive 가 라벨을 옮기지 않아도 init 컨테이너가 받는 profile.json 주소에 리비전이 있다."""
    script = (
        "curl -fsS http://p/api/mcp-servers/s1/revisions/7/profile.json -o /config/profile.json"
    )
    init = [{"name": "fetch-spec", "command": ["sh", "-c", script]}]
    assert pod_revision(_pod("p0", revision=None, init=init)) == 7
    assert pod_revision(_pod("p0", revision=3, init=init)) == 3
    assert pod_revision(_pod("p0", revision=None)) is None


@pytest.mark.parametrize(
    ("experimental", "enforces", "unresolved"),
    [
        (ENFORCES, True, {}),
        ({"arg_conditions": {"version": 1, "unresolved": {"Add": ["x"]}}}, True, {"Add": ["x"]}),
        ({}, False, {}),
        (None, False, {}),
        ({"arg_conditions": "yes"}, False, {}),
        ({"arg_conditions": {"unresolved": {}}}, False, {}),
    ],
    ids=["enforces", "unresolved", "old-image", "no-experimental", "not-an-object", "no-version"],
)
def test_record_from_reads_the_report(experimental, enforces, unresolved):
    record = record_from(experimental, image="rt:1", image_id=IMAGE_ID, revision=2, now=NOW)
    assert (record.enforces, record.unresolved) == (enforces, unresolved)


def _rev(revision: int, bound: int | None) -> MCPServerRevision:
    rules = {"Add": ServerToolRule(conditions=[ArgCondition(path="a", op="max", value=bound)])}
    return MCPServerRevision(revision=revision, backends=[_selection(0, rules if bound else {})])


def _doc(record: ConditionsRecord | None, image: str = "rt:1", bounds=(10, 10)) -> MCPServer:
    """리비전 1 과 2 가 bounds 의 조건(Add 의 a 최댓값)을 든 문서다. 배포한 리비전은 2 다."""
    doc = MCPServer(
        tenant="default",
        name="calc",
        display_name="calc",
        spec=MCPServerSpec(backends=[_selection(0, {})]),
        revisions=[_rev(1, bounds[0]), _rev(2, bounds[1])],
        revision=2,
        effective_config={"images": {"runtime_image": image}},
        deployed_revision=2,
    )
    doc.conditions_record = record
    return doc


def test_conditions_are_known_only_for_the_rendered_image():
    record = record_from(ENFORCES, image="rt:1", image_id=IMAGE_ID, revision=2, now=NOW)
    assert conditions_enforced(_doc(record))
    assert not conditions_enforced(_doc(record, image="rt:2"))
    assert not conditions_enforced(_doc(None))
    old = record_from({}, image="rt:1", image_id=IMAGE_ID, revision=2, now=NOW)
    assert not conditions_enforced(_doc(old))


def test_conditions_are_known_only_when_the_records_revision_has_the_same_conditions():
    record = record_from(ENFORCES, image="rt:1", image_id=IMAGE_ID, revision=1, now=NOW)
    assert conditions_enforced(_doc(record, bounds=(10, 10)))
    assert not conditions_enforced(_doc(record, bounds=(1000, 10)))
    missing = record.model_copy(update={"revision": 9})
    assert not conditions_enforced(_doc(missing))


def test_unresolved_names_belong_to_the_deployed_revision():
    report = {"arg_conditions": {"version": 1, "unresolved": {"Add": ["x"]}}}
    record = record_from(report, image="rt:1", image_id=IMAGE_ID, revision=2, now=NOW)
    assert unresolved_conditions(_doc(record)) == ["Add"]
    stale = record.model_copy(update={"revision": 1})
    assert unresolved_conditions(_doc(stale)) == []


# -- 스윕과 렌더 ----------------------------------------------------------------------------------


class _Tokens:
    def __init__(self) -> None:
        self.minted: list[tuple[str, str | None]] = []

    async def platform_token(self, tenant: str, server_id: str | None = None) -> str:
        self.minted.append((tenant, server_id))
        return "platform-token"


class _Reader:
    def __init__(self, answer: dict[str, Any] | None, *, fails: bool = False) -> None:
        self.answer = answer
        self.fails = fails
        self.urls: list[tuple[str, str | None]] = []
        self.before: Any = None

    async def __call__(self, _client: Any, url: str, bearer: str | None) -> dict[str, Any] | None:
        self.urls.append((url, bearer))
        if self.before is not None:
            self.before()
        if self.fails:
            raise ConnectionError("proxy not answering")
        return self.answer


def _records(client, reader: _Reader) -> ConditionRecords:
    state = client.app.state
    service = state.mcp_server_service
    return ConditionRecords(
        service.servers, clients=service.client_for_doc, tokens=_Tokens(), reader=reader
    )


def _sweep(client, records: ConditionRecords) -> dict[str, list[str]]:
    return client.portal.call(lambda: records.sweep(NOW))


def _run_pods(
    fake, image_id: str = IMAGE_ID, *, image: str | None = None, name: str = f"{CR}-0"
) -> None:
    """ToolHive 의 프록시 러너가 만드는 StatefulSet 과 그 파드를 흉내 낸다. 파드의 리비전 라벨은
    지금 MCPServer 의 podTemplateSpec 라벨이다."""
    cr = fake.get(GV, "MCPServer", CR, DEFAULT_NS)
    image = image or cr["spec"]["image"]
    revision = int(cr["spec"]["podTemplateSpec"]["metadata"]["labels"][LABEL_PROFILE_REVISION])
    fake.objects[("apps/v1", "StatefulSet", DEFAULT_NS, CR)] = _sts()
    fake.objects[("v1", "Pod", DEFAULT_NS, name)] = _pod(
        name, image_id, image=image, revision=revision
    )


def _rerender(client) -> None:
    forget_rule_caches(client)
    run_data_plane_auth(client)


def _add_open(fake) -> bool:
    return allowed(_policies(fake, CR), VMCP, "Add")


def test_a_first_deploy_closes_conditioned_tools_until_every_pod_is_known(
    client, fake_clusters, mongo
):
    _cid, _bp, inst, fake = _deployed(client, fake_clusters, {"Add": COND})
    assert not _add_open(fake) and allowed(_policies(fake, CR), VMCP, "Divide")
    reader = _Reader(ENFORCES)
    records = _records(client, reader)
    # 파드가 아직 없으면 묻지 않는다
    assert _sweep(client, records) == {}
    _run_pods(fake)
    assert _sweep(client, records) == {"recorded": [inst["id"]]}
    assert reader.urls == [
        (f"http://mcp-{CR}-proxy.{DEFAULT_NS}.svc.cluster.local:8080/mcp", "platform-token")
    ]
    stored = mongo["mcp_servers"].find_one({"_id": inst["id"]})["conditions_record"]
    assert stored["enforces"] is True and stored["image_id"] == IMAGE_ID
    _rerender(client)
    assert _add_open(fake)
    status = client.get(f"{MS}/{inst['id']}").json()["status"]
    assert "conditions_closed" not in _codes(status)
    # 같은 digest 와 리비전의 기록이 있으면 다시 묻지 않는다
    assert _sweep(client, records) == {}
    assert len(reader.urls) == 1


def test_an_image_without_the_report_keeps_the_tools_closed(client, fake_clusters):
    _cid, _bp, inst, fake = _deployed(client, fake_clusters, {"Add": COND})
    _run_pods(fake)
    assert _sweep(client, _records(client, _Reader({}))) == {"recorded": [inst["id"]]}
    _rerender(client)
    assert not _add_open(fake)
    assert "conditions_closed" in _codes(client.get(f"{MS}/{inst['id']}").json()["status"])


def test_a_pod_on_another_digest_clears_the_record_and_closes_again(client, fake_clusters):
    _cid, _bp, inst, fake = _deployed(client, fake_clusters, {"Add": COND})
    _run_pods(fake)
    records = _records(client, _Reader(ENFORCES))
    _sweep(client, records)
    _rerender(client)
    assert _add_open(fake)
    # 리비전을 모르는 파드는 기록을 지운다. 판을 내린 이미지의 파드가 함께 돌면 지우고, 이미지를
    # 받는 중인 파드는 지우지 않는다
    fake.objects[("v1", "Pod", DEFAULT_NS, f"{CR}-1")] = _pod(f"{CR}-1", None, revision=None)
    assert _sweep(client, records) == {"cleared": [inst["id"]]}
    _sweep(client, records)
    del fake.objects[("v1", "Pod", DEFAULT_NS, f"{CR}-1")]
    assert _sweep(client, records) == {"recorded": [inst["id"]]}
    fake.objects[("v1", "Pod", DEFAULT_NS, f"{CR}-1")] = _pod(f"{CR}-1", None)
    assert _sweep(client, records) == {}
    fake.objects[("v1", "Pod", DEFAULT_NS, f"{CR}-1")] = _pod(f"{CR}-1", "old@sha256:0")
    assert _sweep(client, records) == {"cleared": [inst["id"]]}
    _rerender(client)
    assert not _add_open(fake)
    # 옛 파드가 내려가고 모든 파드가 그 digest 로 돌면 그 이미지에 다시 묻는다
    del fake.objects[("v1", "Pod", DEFAULT_NS, f"{CR}-1")]
    _run_pods(fake, "old@sha256:0")
    old = _records(client, _Reader({}))
    assert _sweep(client, old) == {"recorded": [inst["id"]]}
    _rerender(client)
    assert not _add_open(fake)


def test_a_record_for_another_image_does_not_open_the_tools(client, fake_clusters, mongo):
    """이미지를 바꾸는 배포는 렌더 때 곧바로 닫힌다. 기록의 이미지가 스냅샷의 이미지와 달라서다."""
    _cid, _bp, inst, fake = _deployed(client, fake_clusters, {"Add": COND})
    _run_pods(fake, image="registry/other:9")
    _sweep(client, _records(client, _Reader(ENFORCES)))
    _rerender(client)
    assert not _add_open(fake)


def test_an_unresolved_condition_warns_with_the_tool_name(client, fake_clusters):
    _cid, _bp, inst, fake = _deployed(client, fake_clusters, {"Add": COND})
    _run_pods(fake)
    report = {"arg_conditions": {"version": 1, "unresolved": {"Add": ["a"]}}}
    _sweep(client, _records(client, _Reader(report)))
    _rerender(client)
    warnings = client.get(f"{MS}/{inst['id']}").json()["status"]["warnings"]
    assert any(w.startswith("conditions_unresolved:") and w.endswith("(Add)") for w in warnings)


def test_a_redeploy_from_draft_forgets_the_previous_record(client, fake_clusters, mongo):
    _cid, bp, inst, fake = _deployed(client, fake_clusters, {"Add": COND})
    _run_pods(fake)
    _sweep(client, _records(client, _Reader(ENFORCES)))
    r = client.post(f"{MS}/{inst['id']}/undeploy")
    assert r.status_code in (200, 202), r.text
    stored = mongo["mcp_servers"].find_one({"_id": inst["id"]})
    assert stored["state"] == "draft" and stored["conditions_record"] is not None
    _put_rules(client, bp, inst, {"Add": COND})
    r = client.post(f"{MS}/{inst['id']}/deploy", json={"cluster_id": inst["cluster_id"]})
    assert r.status_code == 201, r.text
    assert mongo["mcp_servers"].find_one({"_id": inst["id"]})["conditions_record"] is None
    assert not _add_open(fake)


def test_a_failed_read_leaves_no_stale_record(client, fake_clusters, mongo):
    """digest 가 바뀐 파드를 다시 읽다 실패하면 옛 기록이 남지 않아 닫힌다."""
    _cid, _bp, inst, fake = _deployed(client, fake_clusters, {"Add": COND})
    _run_pods(fake)
    _sweep(client, _records(client, _Reader(ENFORCES)))
    _rerender(client)
    assert _add_open(fake)
    _run_pods(fake, "same-tag@sha256:moved")
    assert _sweep(client, _records(client, _Reader(None, fails=True))) == {"unread": [inst["id"]]}
    assert mongo["mcp_servers"].find_one({"_id": inst["id"]})["conditions_record"] is None
    _rerender(client)
    assert not _add_open(fake)


def _redeploy(client, bp, inst, rules) -> None:
    _put_rules(client, bp, inst, rules)
    r = client.post(f"{MS}/{inst['id']}/redeploy")
    assert r.status_code == 200, r.text


def test_a_redeploy_with_other_conditions_closes_until_its_pods_are_known(
    client, fake_clusters, mongo
):
    """옛 리비전의 파드는 그 리비전의 조건을 지킨다. 조건이 다른 리비전으로 옮기면 그 파드가 모두
    내려가고 새 리비전의 파드를 다시 읽을 때까지 닫힌다."""
    _cid, bp, inst, fake = _deployed(client, fake_clusters, {"Add": COND})
    _run_pods(fake)
    records = _records(client, _Reader(ENFORCES))
    _sweep(client, records)
    _rerender(client)
    assert _add_open(fake)
    looser = {"conditions": [{"path": "a", "op": "max", "value": 1000}]}
    _redeploy(client, bp, inst, {"Add": looser})
    assert mongo["mcp_servers"].find_one({"_id": inst["id"]})["conditions_record"] is None
    assert not _add_open(fake)
    # 옛 파드가 아직 돈다. 배포한 리비전이 아니므로 읽지 않는다
    assert _sweep(client, records) == {}
    _run_pods(fake)
    assert _sweep(client, records) == {"recorded": [inst["id"]]}
    stored = mongo["mcp_servers"].find_one({"_id": inst["id"]})["conditions_record"]
    assert (
        stored["revision"]
        == mongo["mcp_servers"].find_one({"_id": inst["id"]})["deployed_revision"]
    )
    _rerender(client)
    assert _add_open(fake)


def test_a_redeploy_with_the_same_conditions_keeps_the_tools_open(client, fake_clusters, mongo):
    _cid, bp, inst, fake = _deployed(client, fake_clusters, {"Add": COND})
    _run_pods(fake)
    _sweep(client, _records(client, _Reader(ENFORCES)))
    _redeploy(client, bp, inst, {"Add": COND, "Divide": {"callers": {"roles": ["viewer"]}}})
    assert mongo["mcp_servers"].find_one({"_id": inst["id"]})["conditions_record"] is not None
    assert _add_open(fake)


def test_a_read_during_a_new_deploy_is_not_written(client, fake_clusters, mongo):
    """initialize 를 읽는 동안 배포가 바뀌면 그 답을 적지 않는다."""
    _cid, bp, inst, fake = _deployed(client, fake_clusters, {"Add": COND})
    _run_pods(fake)
    reader = _Reader(ENFORCES)

    def moved() -> None:
        mongo["mcp_servers"].update_one({"_id": inst["id"]}, {"$set": {"deploying_since": NOW}})

    reader.before = moved
    assert _sweep(client, _records(client, reader)) == {}
    assert mongo["mcp_servers"].find_one({"_id": inst["id"]})["conditions_record"] is None


def test_the_conditions_sweep_runs_before_the_reapply():
    """기록이 바뀐 서버를 같은 틱의 인증 재적용이 다시 렌더한다."""

    async def step(_now: Any) -> dict[str, list[str]]:
        return {}

    reconciler = Reconciler(
        None, None, None, auth_reapply=SimpleNamespace(run=step), condition_sweep=step
    )
    names = [name for name, _step in reconciler._steps()]
    assert names.index("conditions") < names.index("auth")


def test_the_app_wires_the_conditions_sweep(client):
    assert client.app.state.reconciler.condition_sweep is not None
