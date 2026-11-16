"""Cedar 를 MCPAuthzConfig 로 참조하기 (P50 8절, G8).

vMCP 는 `<vMCP 이름>-authz` 를 참조하고, 그 설정은 받아들여진 뒤에야 vMCP 가 적용된다. 설정이
거부되면 배포가 오퍼레이터의 글로 멈춘다.
"""

from __future__ import annotations

from daegam.services import authz_configs
from daegam.services.authz_configs import authz_verdict
from tests.helpers.consts import GW
from tests.helpers.gateways import (
    create_gateway,
    vmcp_of,
)
from tests.helpers.http import detail_code
from tests.helpers.hub import hub_env

OPEN = {"resources": True, "prompts": True}
CONFIGMAP = {"type": "configMap", "configMap": {"name": "cedar"}}


def test_a_config_is_judged_only_at_its_own_generation():
    live = {
        "metadata": {"generation": 2},
        "status": {"observedGeneration": 1, "conditions": [{"type": "Valid", "status": "True"}]},
    }
    assert authz_verdict(live)[0] is None
    live["status"]["observedGeneration"] = 2
    assert authz_verdict(live) == (True, "")
    live["metadata"]["deletionTimestamp"] = "2026-10-15T00:00:00Z"
    assert authz_verdict(live) == (None, "terminating")


def test_a_config_the_operator_never_judges_stops_the_deploy_as_not_ready(
    client, fake_clusters, monkeypatch
):
    _cid, items, _calc, _orders, _remote, fake = hub_env(client, fake_clusters)
    fake.authz_pending = True
    monkeypatch.setattr(authz_configs, "AUTHZ_POLL_SECONDS", 0.01)
    monkeypatch.setitem(authz_configs.apply_docs.__kwdefaults__, "authz_wait", 0.05)
    p = create_gateway(client, items).json()
    r = client.post(f"{GW}/{p['id']}/deploy", json={})
    assert detail_code(r) == (503, "authz_not_ready")
    assert vmcp_of(fake, "gw-a") is None
