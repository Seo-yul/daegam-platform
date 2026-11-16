"""도구 계약 파일(`schema/tools.json`)을 읽는다. 플랫폼도 같은 파일을 이미지에 담아 읽는다."""

from __future__ import annotations

import json
from functools import cache
from pathlib import Path
from typing import Any

CONTRACT = Path(__file__).resolve().parents[1] / "schema" / "tools.json"


@cache
def contract() -> dict[str, Any]:
    return json.loads(CONTRACT.read_text(encoding="utf-8"))


def tool_spec(name: str) -> dict[str, Any]:
    return next(tool for tool in contract()["tools"] if tool["name"] == name)


def model_header() -> str:
    """모델이 받는 웹 도구 결과의 고정 첫 줄."""
    return contract()["model_header"]
