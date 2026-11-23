"""플랫폼의 백그라운드 작업을 손으로 한 번씩 돌리는 테스트가 함께 쓰는 도우미.

테스트 설정은 작업 루프를 끄므로(conftest 의 `_test_settings`) 테스트가 앱의 상태에서 작업을 꺼내
한 회차씩 부른다. Mongo 클라이언트가 앱의 이벤트 루프에 묶여 있어 TestClient 의 portal 로 돈다.
"""

from __future__ import annotations

from datetime import datetime

from fastapi.testclient import TestClient

from daegam.services import data_plane_auth
from daegam.services.audit_collector import AuditCollector
from daegam.services.budget import Budget
from daegam.services.reconcile import Reconciler
from daegam.store.base import utcnow
from daegam.store.leases import Lease
from tests.helpers.consts import TIME_MARGIN


class OneItemBudget(Budget):
    """한 회차에 항목 하나만 보게 하는 몫. 첫 확인만 시간이 남아 있다."""

    def __init__(self, seconds: float):
        super().__init__(seconds)
        self.checks = 0

    def out_of_time(self) -> bool:
        self.checks += 1
        return self.checks > 1


def reconciler(client: TestClient, holder: str = "pod-a", **kw) -> Reconciler:
    """client 의 앱이 쓰는 MCP Server 스토어·서비스와 리스로 만든 리컨실러다. 루프는 돌지 않고
    테스트가 run_once 로 회차를 하나씩 돌린다. Gateway·Agent 는 kw 로 더한다."""
    app = client.app
    return Reconciler(
        app.state.mcp_servers,
        app.state.mcp_server_service,
        Lease(app.state.backend_apis.db, holder),
        **kw,
    )


def reconcile_tick(client: TestClient, now: datetime) -> dict[str, list[str]]:
    """앱의 리컨실러(`app.state.reconciler`) 한 회차다. 틱 시각은 테스트가 정한다."""
    rec = client.app.state.reconciler
    return client.portal.call(lambda: rec.run_once(now=now))


def stale_tick(client: TestClient, rec: Reconciler) -> dict[str, list[str]]:
    """틱 시각을 rec.stale_after 너머로 민 한 회차다. 오래 멈춘 문서를 만들려고 Mongo 의 updated_at
    을 되돌리지 않는다."""
    return client.portal.call(lambda: rec.run_once(now=utcnow() + rec.stale_after + TIME_MARGIN))


def raise_epoch(monkeypatch, kind: str) -> int:
    """kind 의 렌더 번호를 하나 올린 코드처럼 두고 올린 번호를 돌려준다 (P50 1절). 같은 사전을
    재적용과 배포 경로가 함께 읽는다."""
    epoch = data_plane_auth.RENDER_EPOCHS[kind] + 1
    monkeypatch.setitem(data_plane_auth.RENDER_EPOCHS, kind, epoch)
    return epoch


def run_data_plane_auth(client, now=None) -> dict[str, list[str]]:
    """인증 재적용(`DataPlaneAuth.run`) 한 회차의 결과다 (P32 8절)."""
    auth = client.app.state.data_plane_auth
    return client.portal.call(lambda: auth.run(now=now))


def fallbacks(client, cluster_id: str) -> dict[str, int]:
    """상태 API 가 그 클러스터에서 기능마다 센 대체 형태의 문서 수 (P50 2절). 없으면 빈 사전이다."""
    r = client.get("/settings/data-plane-auth")
    assert r.status_code == 200, r.text
    return r.json()["fallbacks"].get(cluster_id, {})


def run_clone_sweep(client) -> dict[str, list[str]]:
    """공유 복제본 스윕(리컨실의 shared 단계, `SharedClones.sweep`) 한 회차의 결과다 (P50 1절)."""
    return client.portal.call(client.app.state.shared_clones.sweep)


def run_entry_conversion(client) -> dict[str, list[str]]:
    """프록시를 두던 허브 항목 옮기기(리컨실의 conversion 단계, `EntryConversion.run`) 한 회차의
    결과다."""
    return client.portal.call(client.app.state.data_plane_auth.entry_conversion.run)


def audit_collector(client, holder="pod-a", **kw) -> AuditCollector:
    """client 의 앱이 쓰는 스토어와 holder 의 리스로 만든 감사 수집기. 루프는 돌지 않고 테스트가
    run_once 로 회차를 하나씩 돌린다."""
    app = client.app
    # 앱처럼 Agent 이벤트를 TelemetryWriter 로 넣는다(평가 훅 포함)
    kw.setdefault("telemetry", app.state.telemetry_writer)
    kw.setdefault("pushes", app.state.agent_telemetry)
    return AuditCollector(
        app.state.audit,
        app.state.mcp_servers,
        app.state.clusters,
        app.state.cluster_service,
        Lease(app.state.backend_apis.db, holder),
        **kw,
    )
