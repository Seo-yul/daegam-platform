"""리컨실 루프. fake 클러스터로 MCP Server 의 네 갈래(deploying·failed → provision, undeploying →
undeploy, deleting → delete) 재처리와 리스 경합, Gateway·Agent 의 실패한 배포 재시도를 확인한다.

오래 멈춘 문서를 만들려고 Mongo 의 updated_at 을 되돌리지 않는다. 틱 시각(`run_once(now=)`)을
stale_after 너머로 민다(tests/helpers/workers.py 의 stale_tick). 배경 리컨실러는 정해진 시간 동안
잠드는 대신 첫 회차가 돌 때까지 기다린다."""

from __future__ import annotations

import logging
import time
from datetime import timedelta

from daegam.services.reconcile import LEASE_NAME
from daegam.store.base import utcnow
from tests.helpers.agent_runtime import agent_deployment
from tests.helpers.agents import agent_env, create_agent, deploy_agent, template_for
from tests.helpers.consts import AG, DEFAULT_NS, MS
from tests.helpers.http import api_client, deployed, detail_code, make_app
from tests.helpers.mcp_servers import clone_and_deploy, mcp_server_env
from tests.helpers.workers import reconciler, stale_tick

# 배경 리컨실러의 첫 회차를 기다리는 상한(초). 병렬 실행으로 CPU 가 바빠도 넉넉하다
FIRST_RUN_TIMEOUT = 10.0
IDLE = {"created": [], "undeployed": [], "deleted": [], "failed": []}


def test_reconcile_retries_agents(client, fake_clusters, mongo, caplog):
    cid, ds, gw = agent_env(client, mongo)
    fake = fake_clusters.by_name["edge"]
    fake.unreachable = True
    p = create_agent(client, template_for(gw, ds)).json()
    r = deploy_agent(client, p["id"])
    assert detail_code(r) == (502, "cluster_unreachable")
    inst = client.get(f"{AG}/{p['id']}").json()
    # 배포 경로의 실패는 failed 로 간다 (해제·삭제는 자기 상태를 지킨다, D42). 리컨실은 failed 도
    # 같은 provision 갈래로 다시 몬다
    assert inst["state"] == "failed" and inst["last_error"].startswith("cluster_unreachable")
    fake.unreachable = False
    app = client.app
    rec = reconciler(client, agents=(app.state.agents, app.state.agent_service))
    assert client.portal.call(rec.run_once) == IDLE
    # 클러스터가 아직 죽어 있으면 실패는 한 줄 경고여야 한다. AgentError 는 MCPServerError 의
    # 하위가 아니라서, 공용 기반을 잡지 않으면 정상적인 재시도가 스택 트레이스로 찍힌다
    fake.unreachable = True
    with caplog.at_level(logging.WARNING, logger="daegam.reconcile"):
        result = stale_tick(client, rec)
    assert result["failed"] == [inst["id"]], (result, caplog.text)
    records = [r for r in caplog.records if r.name == "daegam.reconcile"]
    seen = [(r.levelname, r.exc_info is not None) for r in records]
    assert records and all(r.levelno == logging.WARNING for r in records), (
        f"정상적인 재시도가 WARNING 이 아니다: {seen}"
    )
    assert not any(r.exc_info for r in records), "재시도에 스택 트레이스가 붙었다"
    fake.unreachable = False
    result = stale_tick(client, rec)
    assert result["created"] == [inst["id"]], (result, caplog.text)
    out = client.get(f"{AG}/{inst['id']}").json()
    assert out["state"] == "deployed" and out["last_error"] is None
    # 토큰 Secret 이 없었으므로 새 토큰을 발급했다 (옛 토큰은 회수)
    assert fake.get("v1", "Secret", out["k8s"]["token_secret"], DEFAULT_NS)["stringData"]["token"]
    assert mongo["agent_instance_tokens"].count_documents({"agent_instance_id": inst["id"]}) >= 1
    # 삭제 도중 클러스터가 죽으면 deleting 으로 남고 reconcile 이 마저 지운다
    fake.unreachable = True
    assert client.delete(f"{AG}/{inst['id']}").status_code == 502
    fake.unreachable = False
    result = stale_tick(client, rec)
    assert result["deleted"] == [inst["id"]], (result, caplog.text)
    assert agent_deployment(fake) is None
    assert client.get(f"{AG}/{inst['id']}").status_code == 404


def test_lease_is_exclusive_and_released(client, fake_clusters, mongo, caplog):
    cid, bp, ds, ms = mcp_server_env(client)
    fake = fake_clusters.by_name["edge"]
    fake.unreachable = True
    assert clone_and_deploy(client, ms).status_code == 502
    inst_id = deployed(client, MS)[0]["id"]
    fake.unreachable = False
    a = reconciler(client, holder="pod-a")
    b = reconciler(client, holder="pod-b")
    # run_once 는 reconcile 의 리스(mcp-instances)를 못 잡으면 아무것도 하지 않는다
    result = stale_tick(client, a)
    assert result["created"] == [inst_id], (result, caplog.text)
    assert client.portal.call(b.run_once) == IDLE  # a 가 쥐고 있다
    assert mongo["reconcile_leases"].find_one({"_id": LEASE_NAME})["holder"] == "pod-a"
    # 만료되면 b 가 잡는다
    mongo["reconcile_leases"].update_one(
        {"_id": LEASE_NAME}, {"$set": {"expires_at": utcnow() - timedelta(seconds=1)}}
    )
    assert client.portal.call(b.run_once) == IDLE  # 할 일은 없지만 리스는 잡았다
    assert mongo["reconcile_leases"].find_one({"_id": LEASE_NAME})["holder"] == "pod-b"
    # stop 은 자기 리스만 지운다
    client.portal.call(a.stop)
    assert mongo["reconcile_leases"].find_one({"_id": LEASE_NAME})["holder"] == "pod-b"
    client.portal.call(b.stop)
    assert mongo["reconcile_leases"].find_one({"_id": LEASE_NAME}) is None


def test_the_overrides_audit_judges_a_wipe_by_the_render(client, fake_clusters, mongo, caplog):
    """부모를 지우는 overrides 는 적용 경로처럼 렌더로 본다. 렌더에 잠긴 값이 없는 자리(mcp
    컨테이너의 프로브)를 지우는 것은 세지 않고, 렌더에 있는 보안 값을 지우는 것은 센다. 렌더하지
    못하면 렌더 없이 보아 지나는 것은 그대로 두고, 막히는 것은 판정하지 않고 따로 세며 로그는 잠금
    표가 아니라 렌더 실패를 적는다. 적용도 같은 렌더 오류로 멈추기 때문이다."""
    cid, bp, ds, ms = mcp_server_env(client)
    inst = clone_and_deploy(client, ms).json()
    rec = reconciler(client)

    def audit(**fields) -> dict:
        pod = {"containers": [{"name": "mcp", **fields}]}
        overrides = {"spec": {"podTemplateSpec": {"spec": pod}}}
        mongo["mcp_servers"].update_one({"_id": inst["id"]}, {"$set": {"overrides": overrides}})
        return client.portal.call(rec.run_once)

    assert "overrides_refused" not in audit(livenessProbe=None)
    assert audit(securityContext=None)["overrides_refused"] == [inst["id"]]
    # 핀된 리비전이 없어 렌더하지 못한다. 렌더 없이 보아도 지나는 overrides 는 조용하다
    mongo["mcp_servers"].update_one({"_id": inst["id"]}, {"$set": {"deployed_revision": 99}})
    quiet = audit(resources={"limits": {"cpu": "1"}})
    assert "overrides_refused" not in quiet and "overrides_unrendered" not in quiet
    with caplog.at_level(logging.WARNING, logger="daegam.reconcile"):
        result = audit(livenessProbe=None)
    assert "overrides_refused" not in result
    assert result["overrides_unrendered"] == [inst["id"]]
    [line] = [m for m in caplog.messages if "render failed" in m]
    assert inst["id"] in line and "lock table" not in line


def test_reconciler_runs_in_lifespan_when_enabled(
    settings, mongo, fake_clusters, fake_secrets, caplog
):
    app = make_app(
        settings,
        fake_clusters,
        fake_secrets,
        reconcile_enabled=True,
        reconcile_interval=0.05,
        pod_id="pod-live",
    )
    with api_client(app) as c:
        rec = c.app.state.reconciler
        deadline = time.monotonic() + FIRST_RUN_TIMEOUT
        while rec.runs == 0:
            # 메시지는 실패할 때만 만든다. 리스를 누가 쥐었는지와 루프의 예외 로그다
            assert time.monotonic() < deadline, (
                mongo["reconcile_leases"].find_one({"_id": LEASE_NAME}),
                caplog.text,
            )
            time.sleep(0.01)
        lease = mongo["reconcile_leases"].find_one({"_id": LEASE_NAME})
        assert lease["holder"] == "pod-live", (lease, rec.last_result)
    assert mongo["reconcile_leases"].find_one({"_id": LEASE_NAME}) is None
