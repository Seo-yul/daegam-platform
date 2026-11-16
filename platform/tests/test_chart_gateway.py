"""대감 차트의 LLM 게이트웨이 값 (P85 8절, P38 3절).

게이트웨이는 기본으로 꺼져 있다. 켜면 차트가 게이트웨이 네임스페이스와 플랫폼의 Role·RoleBinding 을
keep 으로 렌더하고, 이미지 넷은 모드와 상관없이 ConfigMap 에 적는다. 거절하는 값은
test_chart_guards.py 의 BAD_VALUES 가 스키마·lint·템플릿 받침으로 본다.
"""

from __future__ import annotations

import json

import pytest

from daegam.config import settings_from_env
from daegam.store.llm_gateway import LlmGatewayImages
from tests.helpers.chart import (
    CHART,
    OFFLINE,
    chart_copy,
    config_data,
    helm,
    live_cluster,
    named,
    render,
    render_error,
    render_template,
    values_file,
)
from tests.helpers.consts import PLATFORM_NS

ON = {"llmGateway.mode": "agentgateway"}
KEEP = {"helm.sh/resource-policy": "keep"}
MARK = "daegam.devops.ai.kr/component"


def _gateway_objects(docs: list[dict]) -> list[tuple[str, str]]:
    return [
        (d["kind"], d["metadata"]["name"])
        for d in docs
        if d["metadata"]["name"] in ("daegam-llm", "daegam-llm-gateway")
    ]


def test_the_gateway_is_off_by_default_and_renders_nothing_in_its_namespace(bare_env):
    """기본 값의 플랫폼은 게이트웨이를 끈 채 기동하고, 차트는 게이트웨이 네임스페이스를 만들지
    않는다."""
    docs = render()
    assert _gateway_objects(docs) == []
    data = config_data(docs)
    assert (data["DAEGAM_LLM_GATEWAY"], data["DAEGAM_LLM_GATEWAY_NAMESPACE"]) == (
        "off",
        "daegam-llm",
    )
    for key, value in data.items():
        bare_env.setenv(key, value)
    settings = settings_from_env()
    assert (settings.llm_gateway, settings.llm_gateway_on) == ("off", False)
    assert settings.reserved_namespaces == {PLATFORM_NS, "daegam-llm"}
    assert settings.llm_gateway_pdb is True


def test_turning_it_on_renders_the_namespace_and_the_role_to_keep(bare_env):
    docs = render(sets=ON | {"llmGateway.pdb.enabled": False})
    namespace = named(docs, "Namespace", "daegam-llm")
    assert namespace["metadata"]["labels"][MARK] == "llm-gateway"
    assert namespace["metadata"]["labels"]["pod-security.kubernetes.io/enforce"] == "restricted"
    # 테넌트 라벨이 없어야 플랫폼이 이 네임스페이스에 게이트웨이를 렌더한다
    assert "daegam.devops.ai.kr/tenant" not in namespace["metadata"]["labels"]
    role = named(docs, "Role", "daegam-llm-gateway")
    binding = named(docs, "RoleBinding", "daegam-llm-gateway")
    for obj in (namespace, role, binding):
        assert obj["metadata"]["annotations"] == KEEP
    assert binding["subjects"] == [
        {"kind": "ServiceAccount", "name": "daegam-platform", "namespace": PLATFORM_NS}
    ]
    # 네임스페이스가 그 안의 객체보다 먼저 적용된다
    assert _gateway_objects(docs)[0] == ("Namespace", "daegam-llm")
    for key, value in config_data(docs).items():
        bare_env.setenv(key, value)
    settings = settings_from_env()
    assert settings.llm_gateway_on and settings.llm_gateway_pdb is False
    assert settings.llm_gateway_url == "http://llm-gateway.daegam-llm.svc.cluster.local:8080"


def test_the_namespace_value_moves_the_objects_and_the_platform_setting():
    docs = render(sets=ON | {"llmGateway.namespace": "llm-keys"})
    named(docs, "Namespace", "llm-keys")
    for kind in ("Role", "RoleBinding"):
        assert named(docs, kind, "daegam-llm-gateway")["metadata"]["namespace"] == "llm-keys"
    assert config_data(docs)["DAEGAM_LLM_GATEWAY_NAMESPACE"] == "llm-keys"


def test_the_images_are_written_whatever_the_mode():
    """끈 뒤에도 옮겨 가지 않은 Agent 가 있으면 플랫폼이 게이트웨이를 두므로 이미지가 늘 있다."""
    off = config_data(render())["DAEGAM_LLM_GATEWAY_IMAGES"]
    on = config_data(render(sets=ON))["DAEGAM_LLM_GATEWAY_IMAGES"]
    assert off == on
    images = LlmGatewayImages.model_validate(json.loads(off))
    assert images.registry_prefix is None and images.pull_policy == "IfNotPresent"
    assert images.ref(images.collector_image).startswith("ghcr.io/open-telemetry/")


def test_the_gateway_template_shows_alone_as_the_gate_applies_it():
    """e2e-llm-gateway 는 helm template --show-only 로 이것만 찍어 적용한다."""
    docs = render_template("llm-gateway.yaml", sets=ON)
    assert [d["kind"] for d in docs] == ["Namespace", "Role", "RoleBinding"]


@pytest.mark.parametrize(
    ("labels", "refused"),
    [({"daegam.devops.ai.kr/tenant": "acme"}, True), ({MARK: "llm-gateway"}, False)],
)
def test_a_tenant_namespace_is_not_taken(tmp_path, labels, refused):
    """산 네임스페이스에 테넌트 라벨이 있으면 멈춘다. 플랫폼은 그 네임스페이스에 게이트웨이를
    렌더하지 않기 때문이다. 이미 게이트웨이의 것이면 그대로 렌더한다."""
    live = {("Namespace", "daegam-llm"): {"metadata": {"name": "daegam-llm", "labels": labels}}}
    chart = chart_copy(tmp_path, live_cluster(live))
    if refused:
        assert "carries the tenant label" in render_error(sets=ON, chart=chart)
    else:
        named(render(sets=ON, chart=chart), "Namespace", "daegam-llm")


@pytest.mark.parametrize(
    "namespace", ["daegam-system", "default", "kube-llm", "toolhive-system", "daegam-probe"]
)
@pytest.mark.parametrize("mode", ["off", "agentgateway"])
def test_a_namespace_of_the_platform_or_kubernetes_is_refused_by_name(namespace, mode):
    """플랫폼 설정에 늘 적히므로 꺼져 있어도 막는다. 스키마가 아니라 템플릿이 막아 고칠 말을
    준다. helm lint 는 템플릿의 fail 을 INFO 줄로만 남긴다."""
    error = render_error(sets={"llmGateway.mode": mode, "llmGateway.namespace": namespace})
    assert f'llmGateway.namespace "{namespace}" is a namespace of the platform' in error, error
    assert "give the gateway a namespace of its own" in error


def test_a_bare_off_says_how_to_write_it(tmp_path):
    """YAML 의 따옴표 없는 off 는 false 다. 스키마는 그것을 받고 템플릿이 고칠 말을 준다."""
    bare = tmp_path / "bare.yaml"
    bare.write_text("llmGateway:\n  mode: off\n")
    for values in ((bare,), (bare, values_file(tmp_path, {"target": "remote"}))):
        assert 'write mode: "off"' in render_error(*values)
    lint = helm(
        "lint", "--strict", str(CHART), "-n", PLATFORM_NS, "-f", str(OFFLINE), "-f", str(bare)
    )
    # lint 는 템플릿의 fail 을 INFO 줄로만 남기고 통과한다 (B1 기록 1절)
    assert lint.returncode == 0 and "[INFO] Fail: llmGateway.mode: a bare off" in lint.stderr
