"""image 항목의 Secret env 값을 두는 Secret(`store/hub.py` `secret_env_name`)의 장부 (P55 5절).

hub_env_secrets {_id: 허브 항목 id, written_at, swept_at?, cluster_id, namespace}

그 Secret 은 항목의 클러스터, 항목의 테넌트 네임스페이스에 있고, 플랫폼은 거기서 Secret 의 목록을 볼
수 없다(`daegam-tenant-objects` 에 `list` 가 없다). 만들기는 Secret 을 문서보다 먼저 쓰므로 그
사이에 멈춘 만들기가 Secret 을 남길 수 있다. 그래서 Secret 에 쓰기 전에 그 항목과 자리를 여기 적고,
리컨실의 정리가 항목 없는 행의 Secret 을 이름으로 지운다. 장부의 꼴과 규칙은
`store/secret_ledger.py` 에 있다.
"""

from __future__ import annotations

from daegam.store.secret_ledger import MongoSecretLedger

HUB_ENV_SECRETS = "hub_env_secrets"


class MongoHubEnvSecretStore(MongoSecretLedger):
    collection = HUB_ENV_SECRETS


__all__ = ["HUB_ENV_SECRETS", "MongoHubEnvSecretStore"]
