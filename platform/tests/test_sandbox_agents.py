"""코드 실행 Agent 의 플랫폼 쪽이다 (P40 F5, 6절·7절·8절·9절).

그래프의 칸(`AgentData.code_execution`)과 그 읽기, 요구 기능과 `agent.json` 의 칸, 파드의 기계
경로 env, 배포 검사(게이트와 LimitRange 의 최대), 살아 있는 Agent 의 경고, 문서 원본의 전달이다.
클러스터는 대역(fake)이다(`tests/helpers/sandboxes.py`).
"""

from __future__ import annotations

import base64
import dataclasses
from decimal import Decimal
from typing import Any

import pytest

from daegam.k8s.sandbox_capacity import limit_range_max
from daegam.k8s.templates.agent import SANDBOX_URL_ENV, render_agent_deployment
from daegam.services import agent_features
from daegam.services.agent_graph import code_execution_of
from daegam.services.chat_uploads import MAX_ORIGINAL_BYTES_PER_TURN, runtime_attachment
from daegam.services.sandbox_gate import SandboxGateClosed
from daegam.services.sandboxes import SandboxError
from daegam.store.agents import AgentSpec
from daegam.store.base import utcnow
from daegam.store.chat_uploads import DOCUMENT, ChatUpload
from tests.helpers.agent_runtime import env_map, minimal_agent_doc
from tests.helpers.docs import agent_doc, effective_config
from tests.helpers.fake_cluster import FakeClusterClient
from tests.helpers.sandboxes import (
    CLUSTER,
    NS,
    cluster,
    code_exec_spec,
    seed_agent,
    seed_cluster,
)

GIB = 1024**3


def spec(on: Any = True) -> AgentSpec:
    return AgentSpec.model_validate(code_exec_spec(on))


# -- 그래프의 칸과 문서 -------------------------------------------------------------------------


def test_the_typed_field_is_the_one_read() -> None:
    assert code_execution_of(spec(True)) is not None
    assert code_execution_of(spec({"approval": ["approve"]})).approval == ["approve"]
    assert code_execution_of(spec(None)) is None
    assert code_execution_of(spec({"approval": ["nope"]})) is None


def test_code_execution_is_a_required_feature_and_compiles_its_approval() -> None:
    rev = spec({"approval": ["approve", "reject"]})
    wanted = agent_features.agent_features(agent_doc("a"), rev, {})
    assert agent_features.CODE_EXECUTION in wanted.required
    assert (
        agent_features.CODE_EXECUTION
        not in agent_features.agent_features(agent_doc("a"), spec(None), {}).wanted
    )
    out = agent_features.attach_code_execution({"main": {}}, rev)
    assert out["code_execution"] == {"approval": ["approve", "reject"]}
    assert agent_features.attach_code_execution({"main": {}}, spec(None)) == {"main": {}}


def _render(*, sandbox: bool) -> dict[str, Any]:
    dep = render_agent_deployment(
        minimal_agent_doc(),
        effective_config(),
        providers=[],
        data_services={},
        agent_oauth=None,
        client_id_claim="cid",
        groups_claim=None,
        sandbox=sandbox,
    )
    return env_map(dep["spec"]["template"]["spec"]["containers"][0])


def test_only_code_execution_pods_get_the_sandbox_route() -> None:
    env = _render(sandbox=True)
    assert env[SANDBOX_URL_ENV].endswith("/api/agents/ag-1/sandboxes")
    # 끈 Agent 는 예전 렌더 그대로다
    assert SANDBOX_URL_ENV not in _render(sandbox=False)


# -- LimitRange 의 최대 -----------------------------------------------------------------------


def limit_range(ns: str, *items: dict[str, Any]) -> dict[str, Any]:
    return {"metadata": {"namespace": ns}, "spec": {"limits": list(items)}}


def test_limit_range_max_takes_the_smallest_container_or_pod_cap() -> None:
    ranges = [
        limit_range(NS, {"type": "Container", "max": {"memory": "4Gi"}}),
        limit_range(NS, {"type": "Pod", "max": {"memory": "1536Mi"}}),
        limit_range(NS, {"type": "PersistentVolumeClaim", "max": {"storage": "1Gi"}}),
        limit_range("other", {"type": "Container", "max": {"memory": "1Mi"}}),
    ]
    assert limit_range_max(ranges, NS) == 1536 * 1024**2
    assert limit_range_max(ranges, "none") is None
    cpu = [limit_range(NS, {"type": "Container", "max": {"cpu": "500m", "memory": "8Gi"}})]
    assert limit_range_max(cpu, NS, "cpu") == Decimal("0.5")


# -- 배포 검사와 경고 --------------------------------------------------------------------------


@pytest.fixture
def fake(fake_clusters) -> FakeClusterClient:
    client = FakeClusterClient()
    fake_clusters.by_name[CLUSTER] = client
    return client


def test_deploy_checks_the_gate_and_the_limit_range(client, mongo, fake) -> None:
    service = client.app.state.sandboxes
    unchecked = seed_cluster(mongo, fake, cluster(verdict=False))
    with pytest.raises(SandboxGateClosed) as closed:
        client.portal.call(service.deploy_check, unchecked, NS)
    assert closed.value.reason == "unchecked"
    mongo["clusters"].delete_many({})
    open_ = seed_cluster(mongo, fake, cluster())
    service.lists.invalidate(open_.id)
    client.portal.call(service.deploy_check, open_, NS)
    fake.apply(
        {
            "apiVersion": "v1",
            "kind": "LimitRange",
            "metadata": {"name": "caps", "namespace": NS},
            "spec": {"limits": [{"type": "Container", "max": {"memory": "1Gi"}}]},
        }
    )
    service.lists.invalidate(open_.id)
    with pytest.raises(SandboxError) as refused:
        client.portal.call(service.deploy_check, open_, NS)
    assert (refused.value.status, refused.value.code) == (409, "sandbox_limit_range")
    # CPU 의 최대가 샌드박스의 한도(1)보다 작아도 같은 거절이다. 그런 LimitRange 는 모든 열기를
    # 막는다
    fake.apply(
        {
            "apiVersion": "v1",
            "kind": "LimitRange",
            "metadata": {"name": "caps", "namespace": NS},
            "spec": {"limits": [{"type": "Container", "max": {"cpu": "500m", "memory": "8Gi"}}]},
        }
    )
    service.lists.invalidate(open_.id)
    with pytest.raises(SandboxError) as cpu:
        client.portal.call(service.deploy_check, open_, NS)
    assert (cpu.value.code, cpu.value.extra.get("resource")) == ("sandbox_limit_range", "cpu")


def test_live_agents_warn_of_a_closed_gate(client, mongo, fake) -> None:
    service = client.app.state.sandboxes
    c = seed_cluster(mongo, fake, cluster(verdict=False))
    doc = seed_agent(mongo, c)
    [warning] = client.portal.call(service.gate_warnings, doc)
    assert warning.startswith("sandbox_gate_closed:") and "(reason: unchecked)" in warning
    off = seed_agent(mongo, c, aid="ag-off", token="other", on=None)
    assert client.portal.call(service.gate_warnings, off) == []


def test_the_override_shows_on_live_agents(client, mongo, fake, fake_clusters, monkeypatch) -> None:
    service = client.app.state.sandboxes
    settings = dataclasses.replace(service.settings, sandbox_allow_unchecked=True)
    monkeypatch.setattr(service, "settings", settings)
    fake_clusters.by_name["home"] = fake
    c = seed_cluster(mongo, fake, cluster("home", verdict=False))
    doc = seed_agent(mongo, c)
    warnings = client.portal.call(service.gate_warnings, doc)
    assert [w.split(":", 1)[0] for w in warnings] == ["sandbox_override"]


# -- 문서 원본 (6절) --------------------------------------------------------------------------


def _upload(original: bytes | None) -> ChatUpload:
    now = utcnow()
    return ChatUpload(
        tenant="acme",
        agent_id="ag-1",
        user_sub="alice",
        name="a.pdf",
        kind=DOCUMENT,
        format="pdf",
        media_type="application/pdf",
        bytes=len(original or b""),
        sha256="0" * 64,
        content="글".encode(),
        original=original,
        created_at=now,
        expires_at=now,
    )


def test_originals_ride_only_when_asked() -> None:
    upload = _upload(b"%PDF")
    assert "original" not in runtime_attachment(upload)
    sent = runtime_attachment(upload, original=True)
    assert base64.b64decode(sent["original"]) == b"%PDF"
    assert "original" not in runtime_attachment(_upload(None), original=True)
    assert MAX_ORIGINAL_BYTES_PER_TURN == 8 * 1024 * 1024
