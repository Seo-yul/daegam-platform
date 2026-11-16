"""바이트 단위(KiB·MiB)와 JSON 의 바이트 크기. 새로 적는 상한은 이것을 쓴다. 이 모듈보다 먼저 쓴
코드에는 아직 `1024 * 1024` 를 직접 적은 곳이 남아 있다."""

from __future__ import annotations

import json
from typing import Any

KIB = 1024
MIB = 1024 * KIB


def json_bytes(value: Any) -> int:
    """JSON 으로 쓴 크기(UTF-8 바이트). 한글은 그대로 두고 JSON 이 모르는 형은 str 이다. 비서의
    도구 결과 자르기와 Agent 제안의 상한이 같은 잣대로 잰다."""
    return len(json.dumps(value, ensure_ascii=False, default=str).encode("utf-8"))
