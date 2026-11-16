"""규칙으로 정리(minify): spec2openapi `minify_for_mcp` 를 부르고 채택 단위 changes 를 만든다 (D25).

LLM 을 부르지 않는다. 결정적이라 같은 원본·옵션이면 같은 초안과 같은 change id 가 나온다.
apply-draft 는 클라이언트가 보낸 초안이 아니라 옵션으로 다시 계산한 changes 중 채택분만 심는다.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from spec2openapi import ConversionError
from spec2openapi.minify import minify_for_mcp

from daegam.services.spec_generator import projection
from daegam.services.spec_generator.projection import Change


class MinifyError(ValueError):
    """옵션이나 문서가 minify 에 맞지 않는다 (spec2openapi ConversionError 를 감싼다)."""


@dataclass(frozen=True)
class MinifyOptions:
    enrich: tuple[str, ...] = ()
    max_description: int | None = None
    drop_value_examples: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "enrich": list(self.enrich),
            "max_description": self.max_description,
            "drop_value_examples": self.drop_value_examples,
        }


def minify_draft(spec: dict[str, Any], options: MinifyOptions) -> dict[str, Any]:
    """규칙으로 만든 초안. 원본은 건드리지 않는다."""
    try:
        return minify_for_mcp(
            spec,
            enrich=tuple(options.enrich),
            max_description=options.max_description,
            drop_value_examples=options.drop_value_examples,
        )
    except ConversionError as e:
        raise MinifyError(str(e)) from e


def run_minify(spec: dict[str, Any], options: MinifyOptions) -> tuple[dict[str, Any], list[Change]]:
    """(초안, changes). 표면 검사 없이 셈한다. 라우트는 표면 검사를 먼저 하는
    `services/backend_apis.py` 의 같은 이름을 쓴다."""
    draft = minify_draft(spec, options)
    return draft, projection.changes(spec, draft)
