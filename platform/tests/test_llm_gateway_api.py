"""LLM 게이트웨이의 상태 API 와 한도 저장, 켠 게이트웨이의 주소 검사 (P38 7절·11절)."""

from __future__ import annotations

import pytest

from tests.helpers.auth import bearer
from tests.helpers.http import api_client, detail_code, make_app

DIGEST = "sha256:" + "d" * 64
IMAGES = {
    "gateway_image": f"ghcr.io/agentgateway/agentgateway:v1.5.0@{DIGEST}",
    "ratelimit_image": f"docker.io/envoyproxy/ratelimit:f4e76690@{DIGEST}",
    "redis_image": f"docker.io/library/redis:8.2@{DIGEST}",
    "collector_image": f"otel/opentelemetry-collector-contrib:0.163.2@{DIGEST}",
}
OPENAI = {"name": "openai", "kind": "openai", "api_key_secret": "k"}


def test_the_status_shows_the_switch_address_and_namespace(client):
    r = client.get("/settings/llm-gateway")
    assert r.status_code == 200, r.text
    body = r.json()
    assert (body["switch"], body["namespace"]) == ("off", "daegam-llm")
    assert body["url"] == "http://llm-gateway.daegam-llm.svc.cluster.local:8080"
    assert body["clashing_tenants"] == [] and body["providers"] == []
    assert body["ready_at"] is None and body["applied_rev"] is None


def test_the_status_counts_across_tenants_so_only_platform_admins_read_it(oidc, token):
    r = oidc.get("/settings/llm-gateway", headers=bearer(token("u", "acme-admins")))
    assert r.status_code == 403, r.text


def test_limits_are_saved_under_the_platform_settings_rules(client):
    assert client.patch("/settings", json={"llm_providers": [OPENAI]}).status_code == 200
    limits = {"tenant_tokens_per_minute": 1000, "tenants": {"acme": 50}, "providers": {"openai": 9}}
    r = client.patch("/settings", json={"llm_gateway": {"limits": limits}})
    assert r.status_code == 200, r.text
    assert client.get("/settings").json()["llm_gateway"]["limits"] == limits


@pytest.mark.parametrize(
    ("limits", "code"),
    [
        ({"providers": {"nope": 5}}, "unknown_provider"),
        ({"tenants": {"bad tenant": 5}}, "invalid_value"),
    ],
)
def test_an_unknown_provider_or_a_bad_tenant_is_refused(client, limits, code):
    r = client.patch("/settings", json={"llm_gateway": {"limits": limits}})
    assert detail_code(r) == (422, code)


@pytest.mark.parametrize("value", [0, 1_000_000_001])
def test_limits_stay_in_range(client, value):
    r = client.patch(
        "/settings", json={"llm_gateway": {"limits": {"tenant_tokens_per_minute": value}}}
    )
    assert r.status_code == 422, r.text


def test_with_the_gateway_on_a_provider_address_is_checked_on_write(
    settings, fake_clusters, fake_secrets
):
    """켠 게이트웨이는 모든 키를 들므로 플랫폼 네임스페이스를 부르는 provider 를 저장하지 않는다."""
    app = make_app(
        settings, fake_clusters, fake_secrets, llm_gateway="agentgateway", llm_gateway_images=IMAGES
    )
    inside = {**OPENAI, "base_url": "http://daegam-platform.daegam-system.svc:9000/v1"}
    with api_client(app) as c:
        r = c.patch("/settings", json={"llm_providers": [inside]})
        assert detail_code(r) == (422, "invalid_value")
        assert "reserved" in r.json()["detail"]["detail"]
        assert c.patch("/settings", json={"llm_providers": [OPENAI]}).status_code == 200


def test_with_the_gateway_off_the_address_is_not_checked_on_write(client):
    inside = {**OPENAI, "base_url": "http://daegam-platform.daegam-system.svc:9000/v1"}
    assert client.patch("/settings", json={"llm_providers": [inside]}).status_code == 200
