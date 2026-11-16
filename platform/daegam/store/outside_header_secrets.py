"""외부 서버 고정 헤더의 관리 Secret 을 가진 허브 항목의 장부 (P86 6.1절).

outside_header_secrets {_id: 허브 항목 id, written_at, swept_at?}

관리 Secret 은 플랫폼 네임스페이스에 있고, 플랫폼은 그 Secret 을 이름으로만 읽는다. 장부의 꼴과
정리의 규칙은 `store/secret_ledger.py` 에 있다. 관리 Secret 에 쓰기 전에 그 항목을 여기 적는다
(`OutsideHeaderSecrets.write`).
"""

from __future__ import annotations

from daegam.store.secret_ledger import MongoSecretLedger

OUTSIDE_HEADER_SECRETS = "outside_header_secrets"


class MongoOutsideHeaderSecretStore(MongoSecretLedger):
    collection = OUTSIDE_HEADER_SECRETS


__all__ = ["OUTSIDE_HEADER_SECRETS", "MongoOutsideHeaderSecretStore"]
