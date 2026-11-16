"""스크립트가 레지스트리에 GET 하는 주소 (D53).

resolve-images.sh·image-report.sh 의 파이썬 조각이 scripts/lib/registry_auth.py 의
`Registry` 로 요청을 만든다. 도커처럼 이 컴퓨터의 레지스트리만 평문 http 다. 그래서
스크립트를 로컬의 버리는 registry:2 에 대고 시험할 수 있다. 다른 레지스트리는 docker login
자격이 실린 요청이므로 늘 https 다. 자격 증명은 키체인을 읽지 않도록 고정 값으로 바꿔 둔다.
"""

from __future__ import annotations

import pytest
import registry_auth
from registry_auth import Registry

AUTH = "dXNlcjpzZWNyZXQ="  # user:secret 의 base64


@pytest.fixture(autouse=True)
def _no_keychain(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(registry_auth, "basic_auth", lambda host: AUTH)


@pytest.mark.parametrize(
    ("registry", "url"),
    [
        ("registry.example.com/daegam", "https://registry.example.com/v2/daegam/a/tags/list"),
        ("10.0.0.5:5000/daegam", "https://10.0.0.5:5000/v2/daegam/a/tags/list"),
        ("127.0.0.1:5058/daegam", "http://127.0.0.1:5058/v2/daegam/a/tags/list"),
        ("localhost:5000/daegam", "http://localhost:5000/v2/daegam/a/tags/list"),
        ("[::1]:5000/daegam", "http://[::1]:5000/v2/daegam/a/tags/list"),
    ],
)
def test_only_a_registry_on_this_machine_is_plain_http(registry: str, url: str) -> None:
    reg = Registry(registry)

    req = reg.request(f"/v2/{reg.project}/a/tags/list")

    assert req.full_url == url
    assert req.get_header("Authorization") == f"Basic {AUTH}"
