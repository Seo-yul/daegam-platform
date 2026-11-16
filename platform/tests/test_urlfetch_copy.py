"""P39b 10절: web-tools 의 SSRF 규칙은 플랫폼 daegam/urlfetch.py 의 바이트 동일 복사본이다.

D27·D32 처럼 두 구성요소가 lock 과 이미지 컨텍스트를 따로 가져 공용 패키지 대신 복사본을 둔다.
어긋나면 make verify(플랫폼 테스트)가 막는다. 한쪽을 고쳤으면 다른 쪽에 그대로 복사한다.
"""

from __future__ import annotations

from pathlib import Path

import daegam.urlfetch as platform_urlfetch
from tests.helpers.source import WEB_TOOLS, assert_byte_copy


def test_web_tools_urlfetch_is_a_byte_copy_of_the_platform_one() -> None:
    assert_byte_copy(Path(platform_urlfetch.__file__), WEB_TOOLS / "urlfetch.py")
