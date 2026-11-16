"""함께 올린 Redis 의 평문 포트를 닫아도 되는지 본다 (P61 5절 5단계).

    kubectl exec deploy/daegam-platform -c platform -- python -m daegam.shipped_redis_check

그 Redis 를 두는 배포가 평문 포트를 닫기 전과 뒤에 플랫폼 파드에서 부른다. 그 파드의 설정과 Mongo 를
그대로 쓴다. 결과는 JSON 한 줄 `{"ready": bool, "blockers": [...]}` 이고
(`services/redis_tls_consumers.py` `plain_close_blockers`), 닫아도 되면 0, 막는 것이 있으면 3,
확인하지 못하면 1 로 끝난다. 읽기만 한다. 한도 1Gi 의 파드 안에서 돌므로 앱 전체가 아니라 설정과
그 확인이 읽는 스토어만 들인다.
"""

from __future__ import annotations

import asyncio
import json
import sys

from daegam.config import settings_from_env
from daegam.services.redis_tls_consumers import plain_close_blockers
from daegam.store.agents import MongoAgentStore
from daegam.store.backend_apis import MongoBackendAPIStore
from daegam.store.clusters import HOME_CLUSTER_NAME, MongoClusterStore
from daegam.store.data_services import MongoDataServiceStore
from daegam.store.mcp_servers import MongoMCPServerStore

# 막는 것이 있을 때의 끝 코드. 확인하지 못한 1 과 가른다
BLOCKED = 3


async def _main() -> int:
    settings = settings_from_env()
    # 플랫폼과 같은 Mongo 연결 설정을 쓰려고 그 클라이언트를 빌린다(`wiring.build_stores`)
    mongo = MongoBackendAPIStore(settings.mongo_url, settings.mongo_db)
    try:
        clusters = MongoClusterStore(mongo.db)
        blockers = await plain_close_blockers(
            settings.home_redis_tls,
            await clusters.get_by_name(HOME_CLUSTER_NAME),
            clusters,
            MongoDataServiceStore(mongo.db),
            MongoAgentStore(mongo.db),
            MongoMCPServerStore(mongo.db),
        )
    finally:
        await mongo.close()
    print(json.dumps({"ready": not blockers, "blockers": blockers}, ensure_ascii=False))
    return BLOCKED if blockers else 0


if __name__ == "__main__":
    try:
        sys.exit(asyncio.run(_main()))
    except Exception as e:  # noqa: BLE001
        print(json.dumps({"ready": False, "error": f"{type(e).__name__}: {e}"}, ensure_ascii=False))
        sys.exit(1)
