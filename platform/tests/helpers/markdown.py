"""Markdown 문서를 읽는 테스트(문서 인용)가 쓰는 제목 규칙."""

from __future__ import annotations

import re

HEADING = re.compile(r"^(#{1,6})[ \t]+(.+?)(?:[ \t]+#+)?[ \t]*$", re.M)
"""ATX 제목 줄. 첫 묶음은 # 의 수(수준), 둘째는 제목 글이다."""
