"""LLM 게이트웨이 동기화 시험의 틀 (P38 4절·8절).

fake 클러스터의 게이트웨이 네임스페이스, 키를 든 fake Secret 저장소, 시험용 Mongo 의 상태 문서와
Platform Settings 로 `LlmGateway` 를 만든다. 파드마다의 리비전은 `Rig.revs` 가 답하고 시계는
`Rig.tick` 이 옮긴다. test_llm_gateway_sync.py 와 test_llm_gateway_export.py 가 쓴다.
"""

from __future__ import annotations

import dataclasses
import json
from datetime import datetime, timedelta
from types import SimpleNamespace
from typing import Any

from daegam.auth.service_token import InternalIssuer
from daegam.auth.signer import PlatformSigner
from daegam.config import Settings
from daegam.k8s.client import decode_secret_data
from daegam.k8s.templates import llm_gateway as tpl
from daegam.services.llm_gateway import LlmGateway
from daegam.store.llm_gateway import MongoLlmGatewayStore
from daegam.store.llm_revocations import MongoLlmRevocationStore
from daegam.store.settings import MongoPlatformSettingsStore
from tests.helpers.fake_cluster import FakeClusterClient
from tests.helpers.fake_secrets import FakeSecretStore

NS = "daegam-llm"
PLATFORM_NS = "daegam-system"
DIGEST = "sha256:" + "c" * 64
IMAGES = {
    "gateway_image": f"ghcr.io/agentgateway/agentgateway:v1.5.0@{DIGEST}",
    "ratelimit_image": f"docker.io/envoyproxy/ratelimit:f4e76690@{DIGEST}",
    "redis_image": f"docker.io/library/redis:8.2@{DIGEST}",
    "collector_image": f"otel/opentelemetry-collector-contrib:0.163.2@{DIGEST}",
}
T0 = datetime(2026, 11, 9, 9, 0, 0)
HOME_ID = "home-cluster-id"
PROVIDERS = [
    {"name": "openai", "kind": "openai", "api_key_secret": "k-openai"},
    {
        "name": "echo",
        "kind": "openai",
        "api_key_secret": "k-echo",
        "base_url": "http://llm-echo.daegam-e2e-llm.svc.cluster.local:8080/v1",
    },
    # 플랫폼 네임스페이스를 부르는 provider 는 렌더하지 않는다
    {
        "name": "inside",
        "kind": "openai",
        "api_key_secret": "k-in",
        "base_url": "http://daegam-platform.daegam-system.svc:9000/v1",
    },
]


@dataclasses.dataclass
class Rig:
    gw: LlmGateway
    fake: FakeClusterClient
    secrets: FakeSecretStore
    store: MongoLlmGatewayStore
    ps: MongoPlatformSettingsStore
    now: list[datetime]
    revs: dict[str, str | None]

    def tick(self, seconds: float = 60) -> None:
        self.now[0] += timedelta(seconds=seconds)

    def obj(self, api_version: str, kind: str, name: str, namespace: str | None = NS) -> Any:
        """fake 의 객체. 루프 위에서 읽으므로 메서드가 아니라 저장된 dict 를 본다."""
        return self.fake.objects.get((api_version, kind, namespace, name))

    def config(self) -> dict[str, str]:
        secret = self.obj("v1", "Secret", tpl.CONFIG_SECRET)
        assert secret is not None
        return decode_secret_data(secret["data"])

    def rev(self) -> str:
        return json.loads(self.config()["config.yaml"])["binds"][1]["listeners"][0]["routes"][0][
            "policies"
        ]["directResponse"]["body"]


def gateway_namespace(*, labels: dict[str, str] | None = None) -> FakeClusterClient:
    fake = FakeClusterClient()
    fake.objects[("v1", "Namespace", None, NS)] = {
        "apiVersion": "v1",
        "kind": "Namespace",
        "metadata": {
            "name": NS,
            "labels": {"daegam.devops.ai.kr/component": "llm-gateway", **(labels or {})},
        },
    }
    return fake


def gateway_pod(
    fake: FakeClusterClient, name: str, *, ready: bool = True, deleted: datetime | None = None
) -> None:
    pod = fake.add_pod(NS, name, {tpl.APP_LABEL: tpl.GATEWAY})
    pod["status"]["conditions"] = [{"type": "Ready", "status": "True" if ready else "False"}]
    if deleted is not None:
        pod["metadata"]["deletionTimestamp"] = deleted.isoformat() + "Z"


async def make_rig(
    db, fake: FakeClusterClient, *, on: bool = True, settings: Settings | None = None
) -> Rig:
    settings = settings or Settings(
        llm_gateway="agentgateway" if on else "off",
        llm_gateway_images=IMAGES,
        platform_issuer="http://daegam-platform.daegam-system.svc:9000",
    )
    secrets = FakeSecretStore()
    for p in PROVIDERS:
        secrets.secrets[(PLATFORM_NS, p["api_key_secret"])] = {"api_key": f"sk-{p['name']}"}
    ps = MongoPlatformSettingsStore(db)
    await ps.update({"llm_providers": PROVIDERS})
    store = MongoLlmGatewayStore(db)
    issuer = InternalIssuer(
        issuer=settings.platform_issuer_url, human_issuer="x", mcp_audience="daegam-mcp"
    )
    issuer.attach(PlatformSigner.generate())

    async def home() -> SimpleNamespace:
        return SimpleNamespace(id=HOME_ID)

    clusters = SimpleNamespace(home_client=lambda: fake, secret_store=lambda: secrets, home=home)
    now = [T0]
    revs: dict[str, str | None] = {}

    async def read_rev(_client: Any, _ns: str, pod: dict[str, Any]) -> str | None:
        return revs.get(pod["metadata"]["name"])

    revocations = MongoLlmRevocationStore(db)
    gw = LlmGateway(
        settings,
        ps,
        store,
        clusters,
        issuer,
        read_rev=read_rev,
        revocations=revocations,
        clock=lambda: now[0],
    )
    return Rig(gw, fake, secrets, store, ps, now, revs)
