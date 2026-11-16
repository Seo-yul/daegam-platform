"""제품 이미지 표(scripts/lib/images.sh)를 읽는 시험 도우미 (D53, D55).

스크립트는 구성요소와 Dockerfile 을 그 표에서만 읽는다. 시험도 같은 표에서 읽어, 이미지를 더하고
시험의 목록을 빠뜨리는 일이 없게 한다.
"""

from __future__ import annotations

import re
from pathlib import Path

from tests.helpers.consts import REPO
from tests.helpers.source import read_source

# images.sh 의 spec 줄: `구성요소) echo "이미지 컨텍스트 Dockerfile 판"`
_SPEC = re.compile(r'^\s*[\w-]+\) echo "\S+ \S+ (\S+Dockerfile) ', re.M)


def product_dockerfiles() -> tuple[Path, ...]:
    """표의 차례대로 제품 Dockerfile 의 경로."""
    return tuple(
        REPO / path for path in _SPEC.findall(read_source(REPO / "scripts" / "lib" / "images.sh"))
    )
