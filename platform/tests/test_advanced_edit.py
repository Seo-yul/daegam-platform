"""services/advanced_edit.py 단위 테스트. 세 인스턴스 서비스가 공유하는 흐름을 fake 클라이언트와
스텁 인스턴스로 검증한다. 라우터를 거치는 회귀는 test_instance_edit.py·test_gateways_lifecycle.py·
test_agent_runtime_advanced_edit.py 가 맡는다."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from datetime import datetime
from typing import Any

import pytest

from daegam.k8s.merge import AGENT_LOCKED_PATHS, IGNORED_BY_OPERATOR, LOCKED_PATHS, Path
from daegam.k8s.templates.common import CONTAINER_SECURITY, FOREIGN_CONTAINER_SECURITY
from daegam.services.advanced_edit import (
    DRIFT_PREFIX,
    MAX_OVERRIDES_BYTES,
    STORED_PREFIX,
    AdvancedEditor,
    api_error_message,
)
from tests.helpers.fake_cluster import FakeClusterClient

API = "toolhive.stacklok.dev/v1alpha1"


class EditError(Exception):
    def __init__(self, status: int, code: str, detail: str, **extra: Any):
        super().__init__(detail)
        self.status = status
        self.code = code
        self.detail = detail
        self.extra = extra


class K8s:
    name = "mcp-calc"
    namespace = "team-a"


class Inst:
    """EditableInstance 스텁."""

    def __init__(self) -> None:
        self.k8s = K8s()
        self.state = "ready"
        self.deleted_at: datetime | None = None
        self.overrides: dict[str, Any] = {}
        self.last_spec_change: datetime | None = None
        self.updated_at = datetime(2026, 1, 1)


def _rendered(kind: str = "MCPServer", name: str = "mcp-calc") -> dict[str, Any]:
    return {
        "apiVersion": API,
        "kind": kind,
        "metadata": {"name": name, "namespace": "team-a"},
        "spec": {"image": "img:1", "replicas": 1},
    }


def _agent_deployment() -> dict[str, Any]:
    """agent 컨테이너의 렌더를 닮은 Deployment. 프로브는 httpGet 이고 preStop·보안 값이 있다."""
    agent = {
        "name": "agent",
        "image": "agent:1",
        "securityContext": CONTAINER_SECURITY,
        "livenessProbe": {"httpGet": {"path": "/healthz", "port": 8080}},
        "lifecycle": {"preStop": {"sleep": {"seconds": 5}}},
    }
    return {
        "apiVersion": "apps/v1",
        "kind": "Deployment",
        "metadata": {"name": "agent-bot", "namespace": "team-a"},
        "spec": {"replicas": 1, "template": {"spec": {"containers": [agent]}}},
    }


def _editor(
    fake: FakeClusterClient,
    saved: list[Any],
    *,
    rendered: Callable[[], dict[str, Any]] = _rendered,
    locked: tuple[Path, ...] = LOCKED_PATHS,
) -> AdvancedEditor[Inst]:
    async def render(_inst: Inst) -> dict[str, Any]:
        return rendered()

    async def client(_inst: Inst) -> FakeClusterClient:
        return fake

    async def save(inst: Inst) -> None:
        saved.append(dict(inst.overrides))

    editor: AdvancedEditor[Inst] = AdvancedEditor(
        kind=rendered()["kind"],
        render=render,
        client=client,
        save=save,
        manifest=lambda inst: manifest(inst),
        locked=lambda _inst: locked,
        error_cls=EditError,
    )

    async def manifest(inst: Inst) -> dict[str, Any]:
        return {"documents": [await editor.document(inst, rendered())]}

    return editor


def test_document_editable_and_locked_shapes():
    fake = FakeClusterClient()
    editor = _editor(fake, [])
    inst = Inst()
    inst.overrides = {"spec": {"replicas": 2}}
    doc = asyncio.run(editor.document(inst, _rendered()))
    assert doc["overrides"] == {"spec": {"replicas": 2}}
    assert doc["final"]["spec"]["replicas"] == 2 and doc["rendered"]["spec"]["replicas"] == 1
    assert "spec.image" in doc["locked"] and doc["ignored_by_operator"] == []
    assert doc["live"] is None and doc["drift"] is None and doc["error"] is None
    # 편집 대상이 아닌 kind 는 overrides 없이 전부 잠긴다. locked 는 바꿀 수 있다
    other = asyncio.run(editor.document(inst, _rendered("Ingress", "expose-x")))
    assert other["overrides"] == {} and other["locked"] == ["*"] and other["final"] is not None
    custom = asyncio.run(editor.document(inst, _rendered("Ingress", "x"), locked=["spec.a"]))
    assert custom["locked"] == ["spec.a"]


def test_document_reports_drift_and_cluster_errors():
    fake = FakeClusterClient()
    saved: list[Any] = []
    editor = _editor(fake, saved)
    inst = Inst()
    asyncio.run(editor.set_overrides(inst, patch={}))
    live = fake.get(API, "MCPServer", "mcp-calc", "team-a")
    live["spec"]["replicas"] = 3
    fake.apply(live)
    doc = asyncio.run(editor.document(inst, _rendered()))
    assert doc["drift"] == ["spec.replicas"] and doc["drift_patch"] == {"spec": {"replicas": 3}}
    # drift=False 면 라이브만 읽는다
    doc = asyncio.run(editor.document(inst, _rendered(), drift=False))
    assert doc["live"] is not None and doc["drift"] is None
    fake.unreachable = True
    doc = asyncio.run(editor.document(inst, _rendered()))
    assert doc["error"].startswith("cluster_unreachable:") and doc["live"] is None


def test_set_overrides_saves_once_after_apply():
    fake = FakeClusterClient()
    saved: list[Any] = []
    editor = _editor(fake, saved)
    inst = Inst()
    out = asyncio.run(editor.set_overrides(inst, patch={"spec": {"replicas": 2}}))
    assert out["diff"] == ["spec.replicas"] and out["validation"] == "schema_only"
    assert saved == [{"spec": {"replicas": 2}}]
    assert inst.last_spec_change is not None and inst.updated_at == inst.last_spec_change
    assert fake.get(API, "MCPServer", "mcp-calc", "team-a")["spec"]["replicas"] == 2
    # 편집한 CR 전체를 주면 diff 로 patch 를 만든다
    edited = _rendered()
    edited["spec"]["replicas"] = 4
    out = asyncio.run(editor.set_overrides(inst, manifest=edited))
    assert out["overrides"] == {"spec": {"replicas": 4}}


def test_saving_the_shown_final_with_a_sidecar_again_passes():
    """편집기는 최종본을 보이고, 최종본의 사이드카에는 apply_overrides 가 건 보안 값이 있다. 그
    자리는 잠겨 있지만 그대로 다시 저장해도 통과하고 overrides 에는 사용자가 쓴 것만 남는다."""
    fake = FakeClusterClient()
    editor = _editor(fake, [])
    inst = Inst()
    pod = {"containers": [{"name": "side", "image": "busybox"}]}
    sidecar = {"spec": {"podTemplateSpec": {"spec": pod}}}
    first = asyncio.run(editor.set_overrides(inst, patch=sidecar))
    side = first["final"]["spec"]["podTemplateSpec"]["spec"]["containers"][0]
    assert side["securityContext"] == FOREIGN_CONTAINER_SECURITY
    again = asyncio.run(editor.set_overrides(inst, manifest=first["final"]))
    assert again["overrides"] == sidecar and again["final"] == first["final"]
    assert inst.overrides == sidecar


def test_set_overrides_rejects_locked_deleting_and_dry_run_failure():
    fake = FakeClusterClient()
    saved: list[Any] = []
    editor = _editor(fake, saved)
    inst = Inst()
    with pytest.raises(EditError) as e:
        asyncio.run(editor.set_overrides(inst, patch={"spec": {"image": "x"}}))
    assert e.value.status == 422 and e.value.code == "locked_field"
    assert e.value.extra["path"] == "spec.image"
    fake.reject_dry_run = lambda obj: "spec.replicas: Invalid value"
    with pytest.raises(EditError) as e:
        asyncio.run(editor.set_overrides(inst, patch={"spec": {"replicas": 9}}))
    assert e.value.status == 422 and e.value.code == "invalid_manifest"
    assert saved == [] and inst.overrides == {}
    inst.state = "deleting"
    with pytest.raises(EditError) as e:
        asyncio.run(editor.set_overrides(inst, patch={}))
    assert e.value.status == 409 and e.value.code == "deleting"
    inst.state = "undeploying"
    with pytest.raises(EditError) as e:
        asyncio.run(editor.set_overrides(inst, patch={}))
    assert e.value.status == 409 and e.value.code == "in_progress"


def test_drift_reset_and_absorb():
    fake = FakeClusterClient()
    saved: list[Any] = []
    editor = _editor(fake, saved)
    inst = Inst()
    with pytest.raises(EditError) as e:
        asyncio.run(editor.drift_absorb(inst))
    assert e.value.status == 409 and e.value.detail == "live MCPServer not found; nothing to absorb"
    asyncio.run(editor.set_overrides(inst, patch={}))
    live = fake.get(API, "MCPServer", "mcp-calc", "team-a")
    live["spec"]["replicas"] = 3
    fake.apply(live)
    m = asyncio.run(editor.drift_reset(inst))
    assert m["documents"][0]["drift"] == []
    assert fake.get(API, "MCPServer", "mcp-calc", "team-a")["spec"]["replicas"] == 1
    live = fake.get(API, "MCPServer", "mcp-calc", "team-a")
    live["spec"]["replicas"] = 5
    fake.apply(live)
    out = asyncio.run(editor.drift_absorb(inst))
    assert out["overrides"] == {"spec": {"replicas": 5}} and out["documents"][0]["drift"] == []
    # 잠긴 필드의 드리프트는 흡수하지 못한다
    live = fake.get(API, "MCPServer", "mcp-calc", "team-a")
    live["spec"]["image"] = "img:2"
    fake.apply(live)
    with pytest.raises(EditError) as e:
        asyncio.run(editor.drift_absorb(inst))
    assert e.value.code == "locked_field"
    assert e.value.detail.startswith(DRIFT_PREFIX)


def test_stored_overrides_on_a_now_locked_path_are_never_sent():
    """overrides 는 저장할 때만 검사한다. 잠금 표가 넓어지기 전에 저장된 것이 지금 잠긴 경로를
    건드리면 적용 경로(`stored_final`)가 422 locked_field 로 멈추고 클러스터에 아무것도 보내지
    않는다. 오류의 path 가 그 경로라 편집기가 그 줄을 가리킨다."""
    fake = FakeClusterClient()
    editor = _editor(fake, [])
    inst = Inst()
    asyncio.run(editor.set_overrides(inst, patch={"spec": {"replicas": 2}}))
    before = fake.get(API, "MCPServer", "mcp-calc", "team-a")
    inst.overrides = {"spec": {"replicas": 2, "serviceAccount": "toolhive-operator"}}
    with pytest.raises(EditError) as e:
        editor.stored_final(inst, _rendered())
    assert e.value.status == 422 and e.value.code == "locked_field"
    assert e.value.extra["path"] == "spec.serviceAccount"
    assert e.value.detail.startswith(STORED_PREFIX)
    # 드리프트 되돌리기·흡수도 같은 길이라 라이브는 그대로다. 흡수는 문서 error 로 새지 않고
    # 422 locked_field 와 그 경로로 나간다
    for action in (editor.drift_reset, editor.drift_absorb):
        with pytest.raises(EditError) as e:
            asyncio.run(action(inst))
        assert e.value.code == "locked_field" and e.value.status == 422
        assert e.value.extra["path"] == "spec.serviceAccount"
    assert fake.get(API, "MCPServer", "mcp-calc", "team-a") == before
    # 편집기는 저장된 것을 그대로 보여 그 줄을 찾게 하고, 드리프트 기준은 만들지 않는다
    doc = asyncio.run(editor.document(inst, _rendered()))
    assert doc["final"]["spec"]["serviceAccount"] == "toolhive-operator"
    assert doc["live"] is not None and doc["drift"] is None
    assert doc["error"].startswith(f"locked_field: {STORED_PREFIX}spec.serviceAccount")
    # 그 줄을 뺀 저장은 지나가고, 그 뒤로는 다시 적용된다
    asyncio.run(editor.set_overrides(inst, patch={"spec": {"replicas": 3}}))
    assert asyncio.run(editor.drift_reset(inst))["documents"][0]["error"] is None


def test_stored_overrides_with_a_volume_source_no_longer_allowed_are_refused():
    """볼륨 허용 목록도 저장된 overrides 에 적용 때 다시 걸린다."""
    fake = FakeClusterClient()
    editor = _editor(fake, [])
    inst = Inst()
    volume = {"name": "data", "persistentVolumeClaim": {"claimName": "d"}}
    inst.overrides = {"spec": {"podTemplateSpec": {"spec": {"volumes": [volume]}}}}
    with pytest.raises(EditError) as e:
        editor.stored_final(inst, _rendered())
    assert e.value.code == "locked_field"
    assert e.value.extra["path"] == (
        "spec.podTemplateSpec.spec.volumes[name=data].persistentVolumeClaim"
    )


def test_a_wipe_is_judged_by_the_render_on_save_and_on_every_stored_path():
    """렌더에 잠긴 값이 없는 자리(httpGet 프로브)를 지우는 overrides 는 저장되고, 저장된 것을 다시
    보는 길(적용·드리프트 되돌리기·흡수)도 렌더로 보아 막히지 않는다. 렌더 없이 보는 `check_stored`
    는 엄격하게 남고 `render_if_wiping` 은 지우는 overrides 일 때만 렌더를 준다. 렌더에 있는 잠긴
    값(preStop 을 담은 lifecycle)을 지우는 것은 렌더와 함께 보아도 막힌다."""
    fake = FakeClusterClient()
    saved: list[Any] = []
    editor = _editor(fake, saved, rendered=_agent_deployment, locked=AGENT_LOCKED_PATHS)
    inst = Inst()
    assert asyncio.run(editor.render_if_wiping(inst)) is None

    def containers(**fields: Any) -> dict[str, Any]:
        return {"spec": {"template": {"spec": {"containers": [{"name": "agent", **fields}]}}}}

    def live_agent() -> dict[str, Any]:
        live = fake.get("apps/v1", "Deployment", "agent-bot", "team-a")
        return live["spec"]["template"]["spec"]["containers"][0]

    patch = containers(livenessProbe=None)
    out = asyncio.run(editor.set_overrides(inst, patch=patch))
    assert saved == [patch] and out["overrides"] == patch
    assert "livenessProbe" not in live_agent() and live_agent()["lifecycle"]
    rendered = asyncio.run(editor.render_if_wiping(inst))
    assert rendered == _agent_deployment()
    editor.check_stored(inst, rendered)
    with pytest.raises(EditError) as e:
        editor.check_stored(inst, None)
    assert e.value.code == "locked_field" and e.value.detail.startswith(STORED_PREFIX)
    assert asyncio.run(editor.drift_reset(inst))["documents"][0]["error"] is None
    live = fake.get("apps/v1", "Deployment", "agent-bot", "team-a")
    live["spec"]["replicas"] = 3
    fake.apply(live)
    out = asyncio.run(editor.drift_absorb(inst))
    assert inst.overrides == {"spec": {**patch["spec"], "replicas": 3}}
    assert out["documents"][0]["drift"] == [] and "livenessProbe" not in live_agent()
    with pytest.raises(EditError) as e:
        asyncio.run(editor.set_overrides(inst, patch=containers(lifecycle=None)))
    assert e.value.code == "locked_field"
    assert e.value.extra["path"] == "spec.template.spec.containers[name=agent].lifecycle"


def test_stored_overrides_skip_the_operator_ignored_paths():
    """무시되는 경로는 보안 경계가 아니다. 오퍼레이터가 어차피 덮어쓰므로 저장된 것이 그 자리를
    건드려도 적용을 막지 않는다."""
    fake = FakeClusterClient()

    async def render(_inst: Inst) -> dict[str, Any]:
        return _rendered()

    async def client(_inst: Inst) -> FakeClusterClient:
        return fake

    async def save(_inst: Inst) -> None:
        return None

    async def manifest(_inst: Inst) -> dict[str, Any]:
        return {}

    editor: AdvancedEditor[Inst] = AdvancedEditor(
        kind="MCPServer",
        render=render,
        client=client,
        save=save,
        manifest=manifest,
        locked=lambda _inst: LOCKED_PATHS,
        ignored=IGNORED_BY_OPERATOR,
        error_cls=EditError,
    )
    inst = Inst()
    pod = {"containers": [{"name": "mcp", "args": ["--x"]}]}
    inst.overrides = {"spec": {"podTemplateSpec": {"spec": pod}}}
    final = editor.stored_final(inst, _rendered())
    assert final["spec"]["podTemplateSpec"]["spec"]["containers"][0]["args"] == ["--x"]
    # 새로 보내는 patch 는 여전히 ignored_by_operator 다
    with pytest.raises(EditError) as e:
        editor.check(inst, inst.overrides)
    assert e.value.code == "ignored_by_operator"


def test_api_error_message():
    class ApiExc(Exception):
        body = '{"message": "spec.replicas: Invalid value"}'

    assert api_error_message(ApiExc("x")) == "spec.replicas: Invalid value"

    class BadBody(Exception):
        body = "not json"

    assert api_error_message(BadBody("boom")) == "BadBody: boom"
    assert api_error_message(ValueError("plain")) == "ValueError: plain"


def test_overrides_over_the_size_limit_are_refused():
    """잠금 검사는 경로 수에 비례하고 리컨실이 틱마다 다시 보므로 저장하는 overrides 의 크기를
    묶는다. 넘으면 422 invalid_manifest 이고 아무것도 저장하거나 적용하지 않는다."""
    fake = FakeClusterClient()
    saved: list[Any] = []
    editor = _editor(fake, saved)
    inst = Inst()
    big = {
        "spec": {"podTemplateSpec": {"metadata": {"annotations": {"a": "x" * MAX_OVERRIDES_BYTES}}}}
    }
    with pytest.raises(EditError) as e:
        asyncio.run(editor.set_overrides(inst, patch=big))
    assert e.value.code == "invalid_manifest" and "limit" in e.value.detail
    assert saved == [] and fake.get(API, "MCPServer", "mcp-calc", "team-a") is None
