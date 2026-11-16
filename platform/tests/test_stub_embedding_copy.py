"""D32: agent-runtime 의 스텁 임베딩은 플랫폼 services/knowledge/stub_embedding.py 의 바이트 동일
복사본이다.

인덱싱한 쪽(플랫폼)과 검색하는 쪽(런타임)이 다른 값을 내면 e2e 가 성립하지 않는다. 두 디렉터리가
lock 과 이미지 컨텍스트를 따로 가져 공용 패키지 대신 복사본을 두었고, 어긋나면 make verify(플랫폼
테스트)가 막는다. 한쪽을 고쳤으면 다른 쪽에 그대로 복사한다.
"""

from __future__ import annotations

from pathlib import Path

import daegam.services.knowledge.stub_embedding as platform_stub
from tests.helpers.source import AGENT_RUNTIME, assert_byte_copy


def test_runtime_stub_embedding_is_a_byte_copy_of_the_platform_one() -> None:
    assert_byte_copy(Path(platform_stub.__file__), AGENT_RUNTIME / "stub_embedding.py")
