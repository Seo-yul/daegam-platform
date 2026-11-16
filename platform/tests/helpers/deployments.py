"""오퍼레이터가 만드는 Deployment 의 롤아웃을 흉내 내는 테스트가 함께 쓰는 도우미.

`deployment` 는 파드 템플릿이 어노테이션을 싣고 다 굴러갔거나 옛 파드가 남은 Deployment 를 짓고,
`put_deployment` 는 그것을 fake 클러스터에 둔다. 프록시 롤아웃(`proxy_caught_up`)과 서버가 소비자의
토큰 판 롤아웃을 기다리는 것(P50 9절)이 쓴다.
"""

from __future__ import annotations

from typing import Any

from tests.helpers.consts import DEFAULT_NS
from tests.helpers.fake_cluster import FakeClusterClient


def deployment(
    name: str,
    annotations: dict[str, str],
    *,
    labels: dict[str, str] | None = None,
    namespace: str = DEFAULT_NS,
    rolled: bool = True,
) -> dict[str, Any]:
    """파드 템플릿이 annotations 와 labels 를 싣는 Deployment. rolled 가 아니면 옛 파드가 남아
    replicas 가 2 다."""
    template = {"annotations": annotations, "labels": labels or {}}
    return {
        "apiVersion": "apps/v1",
        "kind": "Deployment",
        "metadata": {"name": name, "namespace": namespace, "generation": 1},
        "spec": {"replicas": 1, "template": {"metadata": template}},
        "status": {
            "observedGeneration": 1,
            "replicas": 1 if rolled else 2,
            "updatedReplicas": 1,
            "availableReplicas": 1,
        },
    }


def put_deployment(fake: FakeClusterClient, dep: dict[str, Any]) -> None:
    """dep 를 적용하고 그 세대를 관찰한 status 를 싣는다. 적용은 세대를 올리고 status 를 두기
    때문이다."""
    applied = fake.apply(dep)
    status = {**dep["status"], "observedGeneration": applied["metadata"]["generation"]}
    meta = dep["metadata"]
    fake.set_status("apps/v1", "Deployment", meta["name"], meta["namespace"], status)
