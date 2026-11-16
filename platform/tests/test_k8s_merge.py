"""F7a 키 기반 병합·diff. 잠금은 test_k8s_merge_locks.py, 지우는 patch 는
test_k8s_merge_render_wipe.py 가 본다."""

import copy

import pytest

from daegam.k8s.merge import KEYED_LISTS, compose, diff, merge


def _cr():
    return {
        "apiVersion": "toolhive.stacklok.dev/v1beta1",
        "kind": "MCPServer",
        "metadata": {
            "name": "mcp-x",
            "namespace": "ns",
            "labels": {"daegam.devops.ai.kr/id": "i1", "app": "a"},
        },
        "spec": {
            "image": "rt:1",
            "mcpPort": 8000,
            "transport": "streamable-http",
            "env": [
                {"name": "SPEC2OPENAPI_ENDPOINT", "value": "http://a"},
                {"name": "CACHE_HOST", "value": "h"},
            ],
            "secrets": [{"name": "s1", "key": "password", "targetEnvName": "P"}],
            "podTemplateSpec": {
                "spec": {
                    "volumes": [{"name": "spec", "emptyDir": {}}, {"name": "tmp", "emptyDir": {}}],
                    "initContainers": [{"name": "fetch-spec", "image": "curl", "args": ["x"]}],
                    "containers": [
                        {
                            "name": "mcp",
                            "imagePullPolicy": "IfNotPresent",
                            "volumeMounts": [
                                {"name": "spec", "mountPath": "/config"},
                                {"name": "tmp", "mountPath": "/tmp"},
                            ],
                            "env": [{"name": "A", "value": "1"}],
                        }
                    ],
                }
            },
        },
    }


def test_non_keyed_list_is_replaced():
    base = {
        "spec": {
            "podTemplateSpec": {
                "spec": {"initContainers": [{"name": "fetch-spec", "args": ["a", "b"]}]}
            }
        }
    }
    out = merge(
        base,
        {
            "spec": {
                "podTemplateSpec": {
                    "spec": {"initContainers": [{"name": "fetch-spec", "args": ["c"]}]}
                }
            }
        },
    )
    assert out["spec"]["podTemplateSpec"]["spec"]["initContainers"][0]["args"] == ["c"]


def test_diff_roundtrip_keyed():
    base = _cr()
    target = copy.deepcopy(base)
    mcp = target["spec"]["podTemplateSpec"]["spec"]["containers"][0]
    mcp["resources"] = {"limits": {"cpu": "2"}}
    mcp["env"].append({"name": "B", "value": "2"})
    target["spec"]["env"] = [e for e in target["spec"]["env"] if e["name"] != "CACHE_HOST"]
    target["spec"]["env"].append({"name": "X", "value": "x"})
    target["spec"]["podTemplateSpec"]["spec"]["containers"].append(
        {"name": "sidecar", "image": "b"}
    )
    target["spec"]["replicas"] = 2
    del target["spec"]["transport"]
    target["metadata"]["labels"]["app"] = "b"

    patch = diff(base, target)
    assert patch == {
        "metadata": {"labels": {"app": "b"}},
        "spec": {
            "transport": None,
            "env": [{"name": "CACHE_HOST", "$delete": True}, {"name": "X", "value": "x"}],
            "podTemplateSpec": {
                "spec": {
                    "containers": [
                        {
                            "name": "mcp",
                            "resources": {"limits": {"cpu": "2"}},
                            "env": [{"name": "B", "value": "2"}],
                        },
                        {"name": "sidecar", "image": "b"},
                    ]
                }
            },
            "replicas": 2,
        },
    }
    assert merge(base, patch) == target
    assert base == _cr()  # 원본 불변
    assert diff(base, base) == {}
    assert diff(target, merge(base, patch)) == {}


def test_compose_keeps_nulls_and_deletes():
    p1 = {
        "spec": {"replicas": 2, "env": [{"name": "A", "value": "1"}]},
        "metadata": {"labels": {"x": "1"}},
    }
    p2 = {
        "spec": {"env": [{"name": "A", "$delete": True}, {"name": "B", "value": "2"}]},
        "metadata": {"labels": {"x": None}},
    }
    out = compose(p1, p2)
    assert out == {
        "spec": {
            "replicas": 2,
            "env": [{"name": "A", "$delete": True}, {"name": "B", "value": "2"}],
        },
        "metadata": {"labels": {"x": None}},
    }
    # 합친 patch 를 base 에 얹으면 순서대로 얹은 것과 같다
    base = {
        "spec": {"env": [{"name": "A", "value": "0"}]},
        "metadata": {"labels": {"x": "0", "y": "1"}},
    }
    assert merge(base, out) == merge(merge(base, p1), p2)


def test_keyed_lists_cover_both_pod_roots_identically():
    """KEYED_LISTS 는 파드 스펙 규칙을 두 뿌리에 붙여 만든다. 한 뿌리에만 키 리스트가 생기면 그
    워크로드의 병합·diff 가 조용히 달라지므로 예전에 손으로 적던 표와 같은지 본다."""
    pod_rules = {
        ("containers",): "name",
        ("initContainers",): "name",
        ("volumes",): "name",
        ("imagePullSecrets",): "name",
        ("containers", "*", "env"): "name",
        ("containers", "*", "volumeMounts"): "name",
        ("containers", "*", "ports"): "containerPort",
        ("initContainers", "*", "env"): "name",
        ("initContainers", "*", "volumeMounts"): "name",
        ("initContainers", "*", "ports"): "containerPort",
    }
    expected = {
        ("spec", "env"): "name",
        ("spec", "volumes"): "name",
    }
    for root in (("spec", "podTemplateSpec", "spec"), ("spec", "template", "spec")):
        expected.update({(*root, *rest): key for rest, key in pod_rules.items()})
    assert KEYED_LISTS == expected


def test_merge_keyed_list_onto_missing_base_list():
    """키 리스트 자리에 base 가 없으면 빈 리스트에 얹는다. 키 없는 리스트는 통째로 바꾼다."""
    patch = {"spec": {"env": [{"name": "A", "value": "1"}, {"name": "B", "$delete": True}]}}
    assert merge({"spec": {}}, patch) == {"spec": {"env": [{"name": "A", "value": "1"}]}}
    assert merge({"spec": {"args": ["a"]}}, {"spec": {"args": ["b"]}}) == {"spec": {"args": ["b"]}}


TWO_KEYS = [
    {"name": "creds", "key": "password", "targetEnvName": "P"},
    {"name": "creds", "key": "username", "targetEnvName": "U"},
]


@pytest.mark.parametrize(
    "live",
    [
        pytest.param([TWO_KEYS[0], {**TWO_KEYS[1], "targetEnvName": "OTHER"}], id="retarget"),
        pytest.param([TWO_KEYS[0]], id="delete"),
        pytest.param([TWO_KEYS[1], TWO_KEYS[0]], id="reorder"),
    ],
)
def test_spec_secrets_drift_is_the_whole_list(live):
    """spec.secrets 는 키 리스트가 아니다. 0.52 의 list-map 키가 (name, key)라 한 Secret 의 두
    항목을 name 으로 가르면 하나를 놓친다. 그래서 어느 변화든 리스트 통째가 드리프트다."""
    rendered = {"spec": {"secrets": copy.deepcopy(TWO_KEYS)}}
    assert diff(rendered, {"spec": {"secrets": live}}) == {"spec": {"secrets": live}}
