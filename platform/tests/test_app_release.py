"""버린 플랫폼 앱이 메모리에서 풀리는지 본다.

테스트는 시험마다 앱을 새로 만든다. 앱을 클로저로 잡은 엔드포인트가 있으면 버린 앱이 쌓인다.
까닭은 `daegam/api_docs/__init__.py` 의 `openapi_json` 주석에 있다. 앱은 순환 참조로 엮여 있어
gc 가 돌아야 풀린다.
"""

from __future__ import annotations

import gc
import weakref

from tests.helpers.http import api_client, make_app


def test_a_dropped_app_is_freed(settings, mongo, fake_clusters, fake_secrets):
    app = make_app(settings, fake_clusters, fake_secrets)
    with api_client(app) as c:
        # 없는 경로는 모든 라우터를 지나므로 라우터마다 라우트 상태를 다 만든다
        assert c.get("/no-such-route").status_code == 404
        assert c.get("/openapi.json").status_code == 200
    ref = weakref.ref(app)
    del app, c
    gc.collect()
    assert ref() is None
