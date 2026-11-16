"""배포가 함께 올린 Redis 의 공개 인증서 읽기 (P61 F3).

그 Redis 를 두는 배포는 CA 와 서버 인증서를 플랫폼 네임스페이스의 ConfigMap(`DAEGAM_HOME_REDIS_TLS`
의 `ca_configmap`)에 둔다. 개인 키는 그 ConfigMap 에 없어 플랫폼을 지나지 않는다. 기록에 TLS 끝점을
더하는 기동 단계(`services/shipped_redis.py`)와 서버 인증서의 만료를 보는 home 의 사전 점검
(`ClusterService.preflight`)이 함께 읽는다.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime

from cryptography import x509
from fastapi.concurrency import run_in_threadpool

from daegam.config import ShippedRedisTls
from daegam.k8s.client import KubeClient
from daegam.pem import is_ca_pem
from daegam.store.data_services import REDIS_CA_KEY

logger = logging.getLogger("daegam.shipped_redis")

# ConfigMap 에서 서버 인증서를 담는 키
SERVER_CERT_KEY = "tls.crt"


@dataclass(frozen=True)
class ShippedCa:
    """CA PEM(묶음일 수 있다)과 서버 인증서가 끝나는 때. 서버 인증서를 읽지 못하면 expires_at 은
    None 이다."""

    ca: str
    expires_at: datetime | None


async def read_shipped_ca(
    client: KubeClient | None, namespace: str, shipped: ShippedRedisTls
) -> ShippedCa | None:
    """ConfigMap 을 읽는다. 클라이언트가 없거나, ConfigMap 이 없거나, CA 가 인증서가 아니면 None
    이다. 읽기 오류도 None 으로 알리고 로그를 남긴다. 부르는 쪽이 기동이나 점검을 멈추지 않게
    하려는 것이다."""
    if client is None:
        return None
    try:
        cm = await run_in_threadpool(client.get, "v1", "ConfigMap", shipped.ca_configmap, namespace)
    except Exception as e:  # noqa: BLE001
        logger.warning("configmap %s/%s unreadable: %s", namespace, shipped.ca_configmap, e)
        return None
    data = (cm or {}).get("data") or {}
    ca = is_ca_pem(data.get(REDIS_CA_KEY) or "")
    if ca is None:
        logger.warning(
            "configmap %s/%s holds no CA certificate: the shipped Redis TLS endpoint is not used",
            namespace,
            shipped.ca_configmap,
        )
        return None
    return ShippedCa(ca, _not_after(data.get(SERVER_CERT_KEY)))


def _not_after(pem: str | None) -> datetime | None:
    if not pem:
        return None
    try:
        end = x509.load_pem_x509_certificate(pem.encode()).not_valid_after_utc
    except ValueError:
        return None
    # 저장소의 다른 시각처럼 시간대 없는 UTC 로 둔다(`store/base.py` `utcnow`)
    return end.replace(tzinfo=None)
