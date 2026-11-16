"""P37 2절: agent-runtime 의 LLM 전송 규칙은 플랫폼 llm_http.py 의 바이트 동일 복사본이다.

호출 id 와 상관 헤더, 사용량 읽기, 글자 수 추정, 다시 보내기와 크레딧 소진 판정이 두 이미지에서 같은
답을 내야 원장이 한 규칙으로 쌓인다. 어긋나면 make verify(플랫폼 테스트)가 막는다. 한쪽을 고쳤으면
다른 쪽에 그대로 복사한다.
"""

from __future__ import annotations

from pathlib import Path

import daegam.llm_http as platform_llm_http
from tests.helpers.source import AGENT_RUNTIME, assert_byte_copy


def test_runtime_llm_http_is_a_byte_copy_of_the_platform_one() -> None:
    assert_byte_copy(Path(platform_llm_http.__file__), AGENT_RUNTIME / "llm_http.py")
