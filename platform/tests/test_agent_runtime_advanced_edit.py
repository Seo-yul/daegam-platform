"""P22 F2: 배포한 Agent 의 manifest 와 Advanced 편집 (D27, D42).

overrides 저장·잠금·재배포 뒤 다시 얹기·드리프트 되돌리기, manifest 의 렌더도 적용한 것과 같은 설정
지문을 내므로 overrides 저장이 그 값을 바꾸지 않는 것, 렌더가 둔 preStop 과 플랫폼 소유 어노테이션의
잠금, 렌더와 함께 보아 프로브는 지울 수 있지만 플랫폼 소유 값은 지울 수 없는 것을 든다. 잠금 표가
넓어지기 전에 저장된 overrides 가 재배포와 provision 을 멈추는 것은 세 자원을 함께 보는
test_stored_overrides.py, 서비스 단위의 흐름은 test_advanced_edit.py 가 본다.
"""

from __future__ import annotations

from typing import Any

import pytest

from daegam.k8s.merge import AGENT_LOCKED_PATHS, MergeError, apply_overrides, check_overrides
from daegam.k8s.templates.agent import (
    AGENT_CONTAINER,
    CONFIG_SHA_ANNOTATION,
    INIT_CONTAINER,
    PRESTOP_SECONDS,
    render_agent_deployment,
)
from tests.helpers.agent_runtime import (
    agent_container,
    agent_deployment,
    deployed_agent,
    minimal_agent_doc,
)
from tests.helpers.docs import effective_config
from tests.helpers.http import detail_code
from tests.helpers.workers import reconciler


def _config_sha(deployment: dict[str, Any]) -> str:
    return deployment["spec"]["template"]["metadata"]["annotations"][CONFIG_SHA_ANNOTATION]


def test_manifest_and_overrides(client, fake_clusters, mongo):
    agent = deployed_agent(client, fake_clusters, mongo, runtime=False)
    fake = agent.fake
    url = agent.url
    r = client.get(f"{url}/manifest")
    assert r.status_code == 200, r.text
    docs = r.json()["documents"]
    assert [d["kind"] for d in docs] == ["Deployment", "Service", "NetworkPolicy"]
    dep = docs[0]
    assert dep["name"] == "agent-support" and dep["live"] is not None and dep["drift"] == []
    assert "spec.template.spec.containers[name=agent].image" in dep["locked"]
    assert dep["ignored_by_operator"] == [] and dep["overrides"] == {}
    assert docs[1]["locked"] == ["*"] and docs[2]["locked"] == ["*"]
    # 렌더가 둔 preStop 은 드리프트가 아니고 편집기의 최종본을 그대로 저장해도 diff 가 비어 있다
    r = client.put(f"{url}/overrides", json={"manifest": dep["final"]})
    assert r.status_code == 200 and r.json()["diff"] == [], r.text
    # 적용하지 않는 렌더도 Secret 의 토큰으로 적용한 것과 같은 설정 지문을 낸다(P35 13절)
    applied_sha = _config_sha(agent_deployment(fake))
    assert _config_sha(dep["rendered"]) == applied_sha

    patch = {
        "spec": {
            "template": {
                "spec": {"containers": [{"name": "agent", "resources": {"limits": {"cpu": "2"}}}]}
            }
        }
    }
    r = client.put(f"{url}/overrides", json={"patch": patch})
    assert r.status_code == 200, r.text
    assert r.json()["diff"] == ["spec.template.spec.containers[name=agent].resources.limits.cpu"]
    live = agent_deployment(fake)
    container = live["spec"]["template"]["spec"]["containers"][0]
    assert container["resources"] == {"limits": {"cpu": "2"}}
    # overrides 저장은 설정 지문을 바꾸지 않는다
    assert _config_sha(live) == applied_sha
    # initContainer·env·볼륨은 키 병합이라 보존된다
    assert [c["name"] for c in live["spec"]["template"]["spec"]["initContainers"]] == [
        INIT_CONTAINER
    ]
    assert [e["name"] for e in container["env"]][:4] == [
        "DAEGAM_AGENT_CONFIG",
        "DAEGAM_TENANT",
        "DAEGAM_TRANSCRIPT_RETENTION_DAYS",
        "DAEGAM_INSTANCE_TOKEN",
    ]
    locked = {
        "spec": {"template": {"spec": {"containers": [{"name": "agent", "image": "evil:latest"}]}}}
    }
    r = client.put(f"{url}/overrides", json={"patch": locked})
    assert detail_code(r) == (422, "locked_field")
    # 재배포 뒤에도 overrides 가 다시 얹힌다
    r = client.post(f"{url}/redeploy")
    assert r.status_code == 200, r.text
    live = agent_deployment(fake)
    assert live["spec"]["template"]["spec"]["containers"][0]["resources"] == {
        "limits": {"cpu": "2"}
    }
    r = client.post(f"{url}/drift/reset")
    assert r.status_code == 200 and r.json()["documents"][0]["overrides"] == patch


def test_advanced_edit_cannot_pin_the_config_sha_annotation():
    """파드 템플릿의 `daegam.devops.ai.kr/*` 어노테이션은 플랫폼 소유 값이다. 파드를 굴리는 설정
    지문을 Advanced 편집이 굳히지 못한다. 서비스 토큰 판은 test_k8s_merge_locks.py 가 세 파드
    템플릿에서 본다."""
    patch = {"spec": {"template": {"metadata": {"annotations": {CONFIG_SHA_ANNOTATION: "0"}}}}}
    with pytest.raises(MergeError) as e:
        check_overrides(patch, locked=AGENT_LOCKED_PATHS, ignored=())
    assert e.value.code == "locked_field"


def _rendered_agent() -> dict[str, Any]:
    return render_agent_deployment(
        minimal_agent_doc(),
        effective_config(),
        providers=[],
        data_services={},
        agent_oauth=None,
        client_id_claim="cid",
        groups_claim=None,
    )


def _agent_patch(**fields: Any) -> dict[str, Any]:
    return {"spec": {"template": {"spec": {"containers": [{"name": AGENT_CONTAINER, **fields}]}}}}


def test_advanced_edit_can_drop_the_rendered_probes_of_the_agent():
    """렌더는 agent 컨테이너의 프로브를 httpGet 으로 둔다. 잠긴 exec 가 그 아래 없으므로 렌더와
    함께 보면 프로브를 통째로 지울 수 있고, 최종본의 agent 컨테이너에는 프로브가 없고 preStop 은
    남는다."""
    rendered = _rendered_agent()
    patch = _agent_patch(livenessProbe=None, readinessProbe=None, startupProbe=None)
    check_overrides(patch, locked=AGENT_LOCKED_PATHS, ignored=(), rendered=rendered)
    agent = apply_overrides(rendered, patch)["spec"]["template"]["spec"]["containers"][0]
    assert agent["name"] == AGENT_CONTAINER
    assert not {"livenessProbe", "readinessProbe", "startupProbe"} & agent.keys()
    assert agent["lifecycle"] == {"preStop": {"sleep": {"seconds": PRESTOP_SECONDS}}}


def test_dropping_the_rendered_probe_survives_redeploy_provision_and_the_audit(
    client, fake_clusters, mongo
):
    """렌더한 agent 컨테이너의 프로브는 httpGet 이라 잠긴 exec 가 없다. 그 프로브를 통째로 지우는
    overrides 는 저장되고, 저장된 overrides 를 다시 보는 재배포·리컨실 provision·드리프트 되돌리기가
    렌더로 보아 막히지 않는다. Deployment 에는 프로브가 없는 채로 남고 리컨실 감사도 세지 않는다."""
    agent = deployed_agent(client, fake_clusters, mongo, runtime=False)
    fake = agent.fake
    url = agent.url
    r = client.put(f"{url}/overrides", json={"patch": _agent_patch(livenessProbe=None)})
    assert r.status_code == 200, r.text
    assert "livenessProbe" not in agent_container(fake)
    assert "readinessProbe" in agent_container(fake)

    r = client.post(f"{url}/redeploy")
    assert r.status_code == 200, r.text
    assert "livenessProbe" not in agent_container(fake)
    mongo["agents"].update_one({"_id": agent.id}, {"$set": {"state": "failed"}})
    doc = client.portal.call(client.app.state.agents.get, agent.id)
    assert client.portal.call(client.app.state.agent_service.provision, doc).state == "deployed"
    assert "livenessProbe" not in agent_container(fake)
    r = client.post(f"{url}/drift/reset")
    assert r.status_code == 200 and r.json()["documents"][0]["error"] is None
    app = client.app
    rec = reconciler(client, agents=(app.state.agents, app.state.agent_service))
    assert "overrides_refused" not in client.portal.call(rec.run_once)
    # 렌더에 있는 preStop 을 지우는 것은 여전히 막힌다
    r = client.put(f"{url}/overrides", json={"patch": _agent_patch(lifecycle=None)})
    assert detail_code(r) == (422, "locked_field")
    assert r.json()["detail"]["path"] == "spec.template.spec.containers[name=agent].lifecycle"


@pytest.mark.parametrize(
    "patch",
    [
        pytest.param(_agent_patch(lifecycle=None), id="lifecycle-null"),
        pytest.param(_agent_patch(lifecycle={"preStop": None}), id="preStop-null"),
        pytest.param(_agent_patch(**{"$delete": True}), id="container-delete"),
        pytest.param({"spec": {"template": {"spec": {"containers": None}}}}, id="containers-null"),
        pytest.param(_agent_patch(env=None), id="env-null"),
        pytest.param(_agent_patch(volumeMounts=None), id="volumeMounts-null"),
        pytest.param(_agent_patch(securityContext=None), id="securityContext-null"),
        pytest.param({"spec": {"template": {"spec": {"initContainers": None}}}}, id="init-null"),
        pytest.param({"spec": {"template": {"spec": {"volumes": None}}}}, id="volumes-null"),
        pytest.param(
            {"spec": {"template": {"spec": {"securityContext": None}}}}, id="pod-security-null"
        ),
        pytest.param({"spec": {"template": {"metadata": None}}}, id="template-metadata-null"),
        pytest.param({"spec": {"template": None}}, id="template-null"),
        # 타입을 바꾸는 값도 그 자리를 통째로 갈아 치운다
        pytest.param(
            {"spec": {"template": {"spec": {"containers": {"name": AGENT_CONTAINER}}}}},
            id="containers-dict",
        ),
        pytest.param(_agent_patch(securityContext=["x"]), id="securityContext-list"),
        pytest.param({"spec": {"template": {"metadata": {"labels": ["x"]}}}}, id="labels-list"),
    ],
)
def test_wiping_rendered_platform_values_of_the_agent_is_refused_with_the_render(patch):
    """렌더와 함께 보아도 렌더에 있는 플랫폼 소유 값을 지우는 patch 는 거부된다. preStop 을 담은
    lifecycle, agent 컨테이너 자체와 그 env·마운트·보안 값, 설정 init·볼륨, 파드 보안 값, 파드
    템플릿의 라벨·어노테이션이다. null 뿐 아니라 dict 자리의 리스트와 키 리스트 자리의 dict 도
    같다."""
    with pytest.raises(MergeError) as e:
        check_overrides(patch, locked=AGENT_LOCKED_PATHS, ignored=(), rendered=_rendered_agent())
    assert e.value.code == "locked_field"
