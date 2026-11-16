"""LLM 게이트웨이의 설치 env 와 기동 거절, 예약 네임스페이스의 기동 경고 (P38 3절·12절).

네임스페이스 예약의 세 입구는 test_namespaces.py 가, 차트가 렌더하는 ConfigMap 의 계약은
test_chart_gateway.py 가 본다.
"""

from __future__ import annotations

import json
import logging
from dataclasses import replace

import pytest

from daegam.auth.signer import LLM_AUDIENCE
from daegam.config import Settings, settings_from_env
from tests.helpers.http import api_client, make_app

DIGEST = "sha256:" + "a" * 64
IMAGES = {
    "gateway_image": f"ghcr.io/agentgateway/agentgateway:v1.5.0@{DIGEST}",
    "ratelimit_image": f"docker.io/envoyproxy/ratelimit:f4e76690@{DIGEST}",
    "redis_image": f"docker.io/library/redis:8.2@{DIGEST}",
    "collector_image": f"otel/opentelemetry-collector-contrib:0.163.2@{DIGEST}",
}


def test_the_gateway_is_off_and_the_address_always_exists(bare_env):
    s = settings_from_env()
    assert (s.llm_gateway, s.llm_gateway_on) == ("off", False)
    assert s.llm_gateway_url == "http://llm-gateway.daegam-llm.svc.cluster.local:8080"
    assert s.reserved_namespaces == {"daegam-system", "daegam-llm"}
    assert s.llm_gateway_images == {} and s.llm_gateway_pdb is False


def test_the_address_follows_the_gateway_namespace(bare_env):
    bare_env.setenv("DAEGAM_LLM_GATEWAY_NAMESPACE", "llm-keys")
    s = settings_from_env()
    assert s.llm_gateway_url == "http://llm-gateway.llm-keys.svc.cluster.local:8080"
    assert "llm-keys" in s.reserved_namespaces


def test_turning_on_reads_the_four_images(bare_env):
    bare_env.setenv("DAEGAM_LLM_GATEWAY", "agentgateway")
    bare_env.setenv("DAEGAM_LLM_GATEWAY_IMAGES", json.dumps(IMAGES))
    bare_env.setenv("DAEGAM_LLM_GATEWAY_PDB", "true")
    s = settings_from_env()
    assert s.llm_gateway_on and s.llm_gateway_images == IMAGES and s.llm_gateway_pdb


@pytest.mark.parametrize(
    ("env", "message"),
    [
        ({"DAEGAM_LLM_GATEWAY": "litellm"}, "off or agentgateway"),
        ({"DAEGAM_LLM_GATEWAY": "agentgateway"}, "needs DAEGAM_LLM_GATEWAY_IMAGES"),
        ({"DAEGAM_LLM_GATEWAY_NAMESPACE": "daegam-system"}, "DNS label other than"),
        ({"DAEGAM_LLM_GATEWAY_NAMESPACE": "kube-llm"}, "DNS label other than"),
        ({"DAEGAM_LLM_GATEWAY_NAMESPACE": "Bad_NS"}, "DNS label other than"),
        ({"DAEGAM_LLM_GATEWAY_URL": "https://gw.example.com"}, "svc"),
        ({"DAEGAM_LLM_GATEWAY_URL": "http://llm-gateway.daegam-llm.svc/v1"}, "svc"),
        ({"DAEGAM_LLM_GATEWAY_URL": "http://llm-gateway.svc:8080"}, "svc"),
        ({"DAEGAM_LLM_GATEWAY_URL": "http://a.b.svc:70000"}, "svc"),
        ({"DAEGAM_LLM_GATEWAY_IMAGES": "{"}, "not valid JSON"),
        (
            {"DAEGAM_LLM_GATEWAY_IMAGES": json.dumps({**IMAGES, "redis_image": "redis:8.2"})},
            "DAEGAM_LLM_GATEWAY_IMAGES",
        ),
        (
            {
                "DAEGAM_LLM_GATEWAY_IMAGES": json.dumps(
                    {**IMAGES, "gateway_image": f"gw$X@{DIGEST}"}
                )
            },
            "DAEGAM_LLM_GATEWAY_IMAGES",
        ),
    ],
)
def test_a_wrong_install_env_stops_startup(bare_env, env, message):
    for key, value in env.items():
        bare_env.setenv(key, value)
    with pytest.raises(ValueError, match=message):
        settings_from_env()


@pytest.mark.parametrize(
    "url",
    [
        "http://llm-gateway.daegam-llm.svc:8080",
        "https://llm-gateway.daegam-llm.svc.cluster.local",
        # P37 e2e 6단계의 게이트웨이 자리 메아리
        "http://llm-echo-gw.daegam-e2e-llm.svc.cluster.local:8080/",
    ],
)
def test_in_cluster_addresses_are_accepted(bare_env, url):
    bare_env.setenv("DAEGAM_LLM_GATEWAY_URL", url)
    assert settings_from_env().llm_gateway_url == url.rstrip("/")


def test_no_other_boundary_takes_the_llm_audience():
    """MCP 계층의 audience 가 daegam-llm 이면 데이터 플레인 토큰이 게이트웨이에 든다 (P38 5절)."""
    with pytest.raises(ValueError, match="LLM"):
        replace(Settings(), mcp_audience=LLM_AUDIENCE)


def test_startup_warns_about_a_tenant_on_the_gateway_namespace(
    settings, fake_clusters, fake_secrets, caplog
):
    """템플릿 daegam-{tenant} 는 테넌트 llm 에게 daegam-llm 을 준다. 예약 전부터 그 테넌트가
    있었으면 기동이 그것을 알린다. 그 테넌트의 네임스페이스 작업은 422 다 (P38 3절)."""
    with api_client(make_app(settings, fake_clusters, fake_secrets)) as client:
        for tenant in ("llm", "ops"):
            r = client.patch(f"/tenants/{tenant}/settings", json={"agent_client_ids": []})
            assert r.status_code == 200, r.text
    app = make_app(settings, fake_clusters, fake_secrets, home_kubecontext="fake-ctx")
    with caplog.at_level(logging.WARNING, logger="daegam.tenancy"), api_client(app):
        pass
    messages = [r.getMessage() for r in caplog.records]
    warned = [m for m in messages if "reserves for the LLM gateway" in m]
    assert warned and warned[0].startswith("tenant llm uses namespace daegam-llm")
    assert not [m for m in warned if m.startswith("tenant ops")]
