"""P65 2절: 한 클러스터에 묻는 고리는 그 클러스터가 처음 연결할 수 없다고 답하면(시한 포함) 그
클러스터의 남은 일을 이번에는 건너뛴다. preflight 의 점검과 서비스 토큰 스윕이다."""

from __future__ import annotations

import asyncio
from datetime import datetime
from types import SimpleNamespace
from typing import Any

import pytest
import urllib3
from kubernetes.client.rest import ApiException

from daegam.errors import ServiceError
from daegam.k8s.client import (
    ClusterTimeout,
    ClusterUnreachable,
    ResourceNotServed,
    ServiceTimeout,
    cluster_down,
)
from daegam.k8s.preflight import check_cluster
from daegam.services.svc_token_sweep import SvcTokenSweeper
from daegam.store.auth_reapply import LIVE_STATES
from tests.helpers.fake_cluster import FakeClusterClient


class _GoesSilent(FakeClusterClient):
    """/version 에는 답하고 그 뒤 첫 SelfSubjectAccessReview 부터 시한으로 실패한다."""

    def __init__(self) -> None:
        super().__init__()
        self.calls_after_version: list[str] = []

    def can_i(self, *args: Any, **kwargs: Any) -> bool:
        self.calls_after_version.append("can_i")
        raise ClusterTimeout("POST /apis/authorization.k8s.io/v1/...: read timed out after 45 s")

    def get(self, *args: Any, **kwargs: Any) -> Any:
        self.calls_after_version.append("get")
        return super().get(*args, **kwargs)

    def discover(self, group_version: str) -> list[str]:
        self.calls_after_version.append("discover")
        return super().discover(group_version)


def test_preflight_stops_at_the_first_timeout():
    fake = _GoesSilent()
    pf, gates = check_cluster(fake, ["daegam-acme", "daegam-beta"])
    assert fake.calls_after_version == ["can_i"]
    assert not pf.ok and pf.missing == [
        "connect: ClusterTimeout: POST /apis/authorization.k8s.io/v1/...: read timed out after 45 s"
    ]
    assert pf.k8s_version == fake.version()["gitVersion"]
    assert gates.tenant_object_rights is None and gates.operator_read is False


class _AnswersWithErrors(FakeClusterClient):
    """서버는 답하지만 SelfSubjectReview 는 404(1.27), 디스커버리는 400, 목록은 503 이다."""

    def whoami(self) -> dict[str, Any]:
        raise ApiException(status=404, reason="Not Found")

    def discover(self, group_version: str) -> list[str]:
        raise ApiException(status=400, reason="Bad Request")

    def list(self, *args: Any, **kwargs: Any) -> list[dict[str, Any]]:
        raise ApiException(status=503, reason="Service Unavailable")


def test_preflight_keeps_going_when_the_server_answers_with_errors():
    """서버가 답한 4xx·5xx 는 그 점검 하나의 판정이라 남은 점검을 멈추지 않는다."""
    pf, _ = check_cluster(_AnswersWithErrors(), ["daegam-acme"])
    assert not any(m.startswith("connect: ") for m in pf.missing)
    assert any(m.startswith("discovery: ") for m in pf.missing)
    assert pf.rbac


def _wrapped(inner: BaseException) -> ClusterUnreachable:
    try:
        raise inner
    except BaseException as e:
        try:
            raise ClusterUnreachable("wrapped") from e
        except ClusterUnreachable as outer:
            return outer


@pytest.mark.parametrize(
    ("error", "down"),
    [
        (ServiceError(502, "cluster_unreachable", "x"), True),
        (ServiceError(502, "k8s_error", "x"), False),
        (ClusterTimeout("read timed out"), True),
        (ClusterUnreachable("secret not found"), True),
        (urllib3.exceptions.NewConnectionError(None, "refused"), True),
        (ApiException(status=0, reason="ssl"), True),
        (ApiException(status=503), False),
        (ResourceNotServed("resource v1/X not served"), False),
        (ServiceTimeout("GET http://svc/: ReadTimeout"), False),
        (_wrapped(ApiException(status=404)), False),
        (_wrapped(urllib3.exceptions.ProtocolError("reset")), True),
    ],
)
def test_cluster_down_reads_the_chain(error, down):
    assert cluster_down(error) is down
    try:
        try:
            raise error
        except Exception as inner:
            raise RuntimeError("wrapped") from inner
    except RuntimeError as outer:
        assert cluster_down(outer) is down


def test_sweep_skips_the_rest_of_an_unreachable_cluster():
    asked: list[str] = []

    class _Service:
        async def client_for_doc(self, doc):
            asked.append(doc.id)
            if doc.cluster_id == "edge":
                raise ServiceError(502, "cluster_unreachable", "edge is down")
            return object()

    def doc(i: str, cluster: str) -> SimpleNamespace:
        return SimpleNamespace(
            id=i, cluster_id=cluster, state=next(iter(LIVE_STATES)), k8s=object(), tenant="acme"
        )

    sweeper = SvcTokenSweeper.__new__(SvcTokenSweeper)

    async def never_due(*_a: Any) -> bool:
        return False

    sweeper._due = never_due  # type: ignore[method-assign]
    docs = [doc("a", "edge"), doc("b", "edge"), doc("c", "home"), doc("d", "edge")]
    result: dict[str, list[str]] = {"renewed": [], "failed": []}
    down: set[str] = set()
    now = datetime.now()
    asyncio.run(sweeper._sweep("agent", docs, _Service(), lambda _c, _d: None, now, result, down))
    assert asked == ["a", "c"]
    assert result["failed"] == ["a"] and result["skipped"] == ["b", "d"] and down == {"edge"}
