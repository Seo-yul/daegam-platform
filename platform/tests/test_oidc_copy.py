"""D27: agent-runtime 의 OIDC 검증기는 플랫폼 auth/oidc.py 의 바이트 동일 복사본이다.

두 디렉터리가 lock 과 이미지 컨텍스트를 따로 가져 공용 패키지 대신 복사본을 두었고, 어긋나면
make verify(플랫폼 테스트)가 막는다. 한쪽을 고쳤으면 다른 쪽에 그대로 복사한다.
"""

from __future__ import annotations

from pathlib import Path

import daegam.auth.oidc as platform_oidc
from tests.helpers.source import AGENT_RUNTIME, assert_byte_copy


def test_runtime_oidc_is_a_byte_copy_of_the_platform_one() -> None:
    assert_byte_copy(Path(platform_oidc.__file__), AGENT_RUNTIME / "oidc.py")
