"""mock OIDC(deploy/dev/mock-oidc)를 인프로세스에 붙이는 테스트가 함께 쓰는 도우미 (P6).

mock 의 앱(`create_mock_oidc`)과 서명 키(`Signer`)를 그대로 내보내고, 테스트 발급자(OIDC_ISSUER)와
audience 로 맞춘 설정(`mock_oidc_config`)을 만든다. 픽스처(`mock_oidc`, `oidc_client` 등)는
conftest.py 가 이것으로 만든다.
"""

from __future__ import annotations

import sys

from tests.helpers.consts import OIDC_AUDIENCE, OIDC_ISSUER, REPO

MOCK_OIDC_DIR = REPO / "deploy" / "dev" / "mock-oidc"
if str(MOCK_OIDC_DIR) not in sys.path:
    sys.path.insert(0, str(MOCK_OIDC_DIR))

from mock_oidc.app import Config as MockOidcConfig  # noqa: E402
from mock_oidc.app import Signer  # noqa: E402
from mock_oidc.app import create_app as create_mock_oidc  # noqa: E402

__all__ = ["MockOidcConfig", "Signer", "create_mock_oidc", "mock_oidc_config"]


def mock_oidc_config(**kw) -> MockOidcConfig:
    return MockOidcConfig(
        issuer=OIDC_ISSUER, audience=OIDC_AUDIENCE, allow_test_tokens=True, token_ttl=600, **kw
    )
