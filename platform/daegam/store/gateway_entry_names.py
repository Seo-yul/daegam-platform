"""Gateway 의 MCPServerEntry 이름의 선점 기록 (D82).

Entry 이름은 Gateway 의 이름과 허브 항목의 이름을 하이픈으로 이은 것이라 한 네임스페이스의 두
Gateway 가 같은 이름을 만들 수 있다. 문서 하나가 (클러스터, 네임스페이스, Entry 이름) 하나이고
`_id` 가 셋을 이은 것이다. 선점·nonce·지우기의 규칙은 `store/name_claims.py` 에 있다.
"""

from __future__ import annotations

from daegam.store.name_claims import MongoNameClaimStore, NameClaim

ENTRY_NAMES = "gateway_entry_names"


def entry_claim_id(cluster_id: str, namespace: str, name: str) -> str:
    """선점 기록의 `_id`. 네임스페이스와 Entry 이름은 DNS 이름이라 `/` 가 없다."""
    return f"{cluster_id}/{namespace}/{name}"


class EntryNameClaim(NameClaim):
    """Entry 이름 하나의 선점. `resource_id` 가 Gateway 문서 id 다."""

    cluster_id: str
    namespace: str
    name: str


class MongoEntryNameStore(MongoNameClaimStore[EntryNameClaim]):
    model = EntryNameClaim
    collection = ENTRY_NAMES
