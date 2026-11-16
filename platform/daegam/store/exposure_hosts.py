"""공개 host 의 선점 기록 (P70, D82).

노출 host 는 플랫폼 전체에서 살아 있는 배포 하나의 것이다(OAuth 의 resource 가 테넌트 하나로 풀리는
전제, D59). 문서 하나가 host 하나이고 `_id` 가 `normalize_host` 를 거친 host 다. 선점·nonce·지우기의
규칙은 `store/name_claims.py` 에 있다.
"""

from __future__ import annotations

from daegam.store.name_claims import MongoNameClaimStore, NameClaim

EXPOSURE_HOSTS = "exposure_hosts"


class HostClaim(NameClaim):
    """host 하나의 선점. `id` 가 host 다."""

    kind: str  # mcp_server | gateway | agent (data_plane_auth 의 KIND_*)


class MongoExposureHostStore(MongoNameClaimStore[HostClaim]):
    model = HostClaim
    collection = EXPOSURE_HOSTS
