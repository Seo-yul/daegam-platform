"""지우는 patch. 부모를 null 로 두거나 리스트 원소를 $delete 해서 잠긴 경로를 지우지 못한다.
렌더와 함께 보면 그 아래에 잠긴 값이 실제로 있을 때만 거부된다."""

from typing import Any

import pytest

from daegam.k8s.merge import (
    AGENT_LOCKED_PATHS,
    LOCKED_PATHS,
    VMCP_LOCKED_PATHS,
    MergeError,
    apply_overrides,
    check_overrides,
    format_path,
)
from tests.helpers.k8s_merge import APPARMOR_ANNOTATION, PLATFORM_CONTAINERS, TEMPLATES


@pytest.mark.parametrize(
    "patch",
    [
        pytest.param({"spec": {"podTemplateSpec": None}}, id="podTemplateSpec-null"),
        pytest.param(
            {"spec": {"podTemplateSpec": {"spec": {"initContainers": None}}}},
            id="initContainers-null",
        ),
        pytest.param({"spec": {"env": None}}, id="env-null"),
        pytest.param({"spec": {"podTemplateSpec": {"spec": {"volumes": None}}}}, id="volumes-null"),
        pytest.param({"spec": None}, id="spec-null"),
        pytest.param({"spec": {"podTemplateSpec": "x"}}, id="podTemplateSpec-scalar"),
    ],
)
def test_wiping_a_parent_cannot_remove_a_locked_path(patch):
    """잠긴 경로의 부모를 null 로 두면 merge 가 그 아래를 통째로 지운다. 잎 경로만 보는 검사는
    잠긴 것보다 얕은 경로를 걸러 내지 못해 스펙을 받아 오는 장치가 한 줄로 사라졌다."""
    with pytest.raises(MergeError) as e:
        check_overrides(patch)
    assert e.value.code == "locked_field"


@pytest.mark.parametrize(
    "containers",
    [
        pytest.param([{"name": "agent", "securityContext": None}], id="securityContext-null"),
        pytest.param([{"name": "agent", "env": None}], id="env-null"),
        pytest.param([{"name": "agent", "volumeMounts": None}], id="volumeMounts-null"),
        pytest.param([{"name": "agent", "$delete": True}], id="container-delete"),
        pytest.param([{"name": "agent", "env": "x"}], id="env-scalar"),
        pytest.param([{"name": "agent", "securityContext": ["x"]}], id="securityContext-list"),
        pytest.param({"name": "agent"}, id="containers-dict"),
    ],
)
def test_wiping_inside_a_keyed_list_cannot_remove_a_locked_path(containers):
    """`wiping_paths` 가 리스트에서 빈손으로 돌아오던 구멍이다. containers[name=agent] 안에서
    securityContext 나 env 를 null 로 두면 잎 검사도 부모 검사도 걸리지 않아 잠긴
    privileged 잠금과 DAEGAM_* env 가 함께 사라졌다. 원소 자체를 $delete 해도 같다. dict 자리의
    리스트와 키 리스트 자리의 dict 도 그 자리를 통째로 갈아 치우므로 지우기다."""
    with pytest.raises(MergeError) as e:
        check_overrides(
            {"spec": {"template": {"spec": {"containers": containers}}}}, locked=AGENT_LOCKED_PATHS
        )
    assert e.value.code == "locked_field"


@pytest.mark.parametrize(
    "patch, locked",
    [
        pytest.param({"spec": {"resourceOverrides": None}}, LOCKED_PATHS, id="resourceOverrides"),
        pytest.param(
            {"spec": {"resourceOverrides": {"proxyDeployment": None}}},
            LOCKED_PATHS,
            id="proxyDeployment",
        ),
        pytest.param({"spec": {"secrets": None}}, LOCKED_PATHS, id="secrets"),
        pytest.param({"spec": {"sessionStorage": None}}, LOCKED_PATHS, id="sessionStorage"),
        pytest.param(
            {"spec": {"podTemplateSpec": {"metadata": None}}}, LOCKED_PATHS, id="pod-metadata"
        ),
        pytest.param(
            {"spec": {"podTemplateSpec": {"metadata": {"labels": None}}}},
            VMCP_LOCKED_PATHS,
            id="vmcp-pod-labels",
        ),
        pytest.param({"spec": {"config": None}}, VMCP_LOCKED_PATHS, id="vmcp-config"),
        pytest.param(
            {
                "spec": {
                    "podTemplateSpec": {"spec": {"containers": [{"name": "vmcp", "$delete": 1}]}}
                }
            },
            VMCP_LOCKED_PATHS,
            id="vmcp-container-delete",
        ),
        pytest.param(
            {"spec": {"template": {"metadata": {"annotations": None}}}},
            AGENT_LOCKED_PATHS,
            id="agent-pod-annotations",
        ),
        pytest.param(
            {"spec": {"template": {"spec": {"containers": [{"name": "agent", "ports": None}]}}}},
            AGENT_LOCKED_PATHS,
            id="agent-ports",
        ),
        pytest.param(
            {"spec": {"template": {"metadata": {"labels": ["daegam.devops.ai.kr/id"]}}}},
            AGENT_LOCKED_PATHS,
            id="agent-template-labels-list",
        ),
    ],
)
def test_wiping_a_parent_cannot_remove_a_newly_locked_path(patch, locked):
    """새로 잠근 경로도 부모를 null 로 두거나 원소를 $delete 해서 지우지 못한다
    (`wiping_paths`·`covers_any`). 플랫폼의 pod 라벨·서비스 토큰 판·세션 저장소가 한 줄로 사라지는
    것을 막는다."""
    with pytest.raises(MergeError) as e:
        check_overrides(patch, locked=locked, ignored=())
    assert e.value.code == "locked_field"


# -- 렌더를 보는 지우기 검사 ------------------------------------------------------------------


def _template(template_key: str, part: str, value: Any) -> dict[str, Any]:
    """파드 템플릿의 spec 이나 metadata 자리에 value 를 둔 문서. 렌더와 patch 가 같은 꼴이다."""
    return {"spec": {template_key: {part: value}}}


PROBES = ("livenessProbe", "readinessProbe", "startupProbe")


@pytest.mark.parametrize("template_key, locked, name", PLATFORM_CONTAINERS)
def test_dropping_a_rendered_http_probe_passes_only_with_the_render(template_key, locked, name):
    """exec 프로브 잠금은 새로 적지 못하게 막는 것이다. 렌더한 프로브가 httpGet 이면 그 아래 잠긴
    값이 없으므로 렌더와 함께 보면 프로브를 통째로 지울 수 있고 최종본에 프로브가 없다. 렌더가
    exec 프로브를 두었으면 지우지 못한다. 렌더 없이 보는 검사는 그대로 엄격하다."""
    http = {"httpGet": {"path": "/healthz", "port": 8080}, "periodSeconds": 10}
    container = {"name": name, "image": "rt:1", **dict.fromkeys(PROBES, http)}
    rendered = _template(template_key, "spec", {"containers": [container]})
    patch = _template(
        template_key, "spec", {"containers": [{"name": name, **dict.fromkeys(PROBES)}]}
    )
    check_overrides(patch, locked=locked, ignored=(), rendered=rendered)
    final = apply_overrides(rendered, patch)
    assert final["spec"][template_key]["spec"]["containers"] == [{"name": name, "image": "rt:1"}]
    with pytest.raises(MergeError) as e:
        check_overrides(patch, locked=locked, ignored=())
    assert e.value.code == "locked_field"
    exec_probe = {"name": name, "livenessProbe": {"exec": {"command": ["check"]}}}
    with pytest.raises(MergeError) as e:
        check_overrides(
            patch,
            locked=locked,
            ignored=(),
            rendered=_template(template_key, "spec", {"containers": [exec_probe]}),
        )
    assert e.value.code == "locked_field" and "livenessProbe.exec" in e.value.detail
    assert format_path(e.value.path).endswith(f"containers[name={name}].livenessProbe")


SECRET_ENV = {"name": "K", "valueFrom": {"secretKeyRef": {"name": "s", "key": "k"}}}


@pytest.mark.parametrize(
    "part, rendered, patch, refused",
    [
        pytest.param(
            "spec",
            {"containers": [{"name": "side", "ports": [{"containerPort": 80, "hostPort": 80}]}]},
            {"containers": [{"name": "side", "ports": None}]},
            True,
            id="ports-null-over-hostPort",
        ),
        pytest.param(
            "spec",
            {"containers": [{"name": "side", "ports": [{"containerPort": 80}]}]},
            {"containers": [{"name": "side", "ports": None}]},
            False,
            id="ports-null",
        ),
        pytest.param(
            "spec",
            {"containers": [{"name": "side", "ports": [{"containerPort": 80, "hostPort": 80}]}]},
            {"containers": [{"name": "side", "ports": [{"containerPort": 80, "$delete": True}]}]},
            True,
            id="port-delete-over-hostPort",
        ),
        pytest.param(
            "spec",
            {"containers": [{"name": "side", "ports": [{"containerPort": 80}]}]},
            {"containers": [{"name": "side", "ports": [{"containerPort": 80, "$delete": True}]}]},
            False,
            id="port-delete",
        ),
        pytest.param(
            "spec",
            {"containers": [{"name": "side", "env": [SECRET_ENV]}]},
            {"containers": [{"name": "side", "env": None}]},
            True,
            id="env-null-over-secretKeyRef",
        ),
        pytest.param(
            "spec",
            {"containers": [{"name": "side", "env": [{"name": "TZ", "value": "UTC"}]}]},
            {"containers": [{"name": "side", "env": None}]},
            False,
            id="env-null",
        ),
        pytest.param(
            "spec",
            {"containers": [{"name": "side", "securityContext": {"privileged": False}}]},
            {"containers": None},
            True,
            id="containers-null-over-privileged",
        ),
        pytest.param(
            "spec",
            {"containers": [{"name": "side", "image": "busybox"}]},
            {"containers": None},
            False,
            id="containers-null",
        ),
        pytest.param(
            "spec",
            {
                "initContainers": [
                    {"name": "warm", "securityContext": {"capabilities": {"drop": ["ALL"]}}}
                ]
            },
            {"initContainers": [{"name": "warm", "$delete": True}]},
            True,
            id="init-delete-over-capabilities",
        ),
        pytest.param(
            "spec",
            {"initContainers": [{"name": "warm", "securityContext": {"runAsUser": 1000}}]},
            {"initContainers": [{"name": "warm", "$delete": True}]},
            False,
            id="init-delete",
        ),
        pytest.param(
            "spec",
            {"volumes": [{"name": "h", "hostPath": {"path": "/x"}}]},
            {"volumes": None},
            True,
            id="volumes-null-over-hostPath",
        ),
        pytest.param(
            "spec",
            {"volumes": [{"name": "cache", "emptyDir": {}}]},
            {"volumes": None},
            False,
            id="volumes-null",
        ),
        pytest.param(
            "metadata",
            {"annotations": {"daegam.devops.ai.kr/svc-token-rev": "1", "team": "a"}},
            {"annotations": None},
            True,
            id="annotations-null-over-platform",
        ),
        pytest.param(
            "metadata",
            {"annotations": {APPARMOR_ANNOTATION: "runtime/default"}},
            {"annotations": None},
            True,
            id="annotations-null-over-apparmor",
        ),
        pytest.param(
            "metadata",
            {"annotations": {"prometheus.io/scrape": "true"}},
            {"annotations": None},
            False,
            id="annotations-null",
        ),
    ],
)
@pytest.mark.parametrize("template_key, locked", TEMPLATES)
def test_wiping_under_wildcard_locks_follows_the_render(
    template_key, locked, part, rendered, patch, refused
):
    """와일드카드 잠금(`containers[*]`·`ports[*]`·`env[*]`·`volumes[*]`·`daegam.devops.ai.kr/*`)
    아래를 지우는 patch 는 렌더에서 그 자리에 잠긴 값이 실제로 있을 때만 거부된다. 어느 이름의
    컨테이너·포트·볼륨·어노테이션에도 걸리는지 함께 본다. 렌더 없이 보면 모두 거부된다."""
    body = _template(template_key, part, patch)
    with pytest.raises(MergeError):
        check_overrides(body, locked=locked, ignored=())
    render = _template(template_key, part, rendered)
    if not refused:
        check_overrides(body, locked=locked, ignored=(), rendered=render)
        return
    with pytest.raises(MergeError) as e:
        check_overrides(body, locked=locked, ignored=(), rendered=render)
    assert e.value.code == "locked_field" and "would remove" in e.value.detail
