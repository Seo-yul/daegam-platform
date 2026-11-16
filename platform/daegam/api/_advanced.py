"""Advanced 편집(D20) 라우터 공통. MCP·Gateway·Agent 인스턴스 셋이 같은 오버라이드 본문을 받는다.

`OverridesBody` 클래스 자체는 각 라우터에 남는다. 리소스마다 무엇의 전체 문서인지(MCPServer CR /
Deployment)를 docstring 으로 말하고 OpenAPI 컴포넌트도 셋으로 갈려 있어 UI 타입이 그 이름에
묶여 있기 때문이다. 셋이 글자 그대로 똑같이 하던 입력 검사만 여기 있다.
"""

from __future__ import annotations

from typing import Any, Protocol

import yaml

from daegam import yamlsafe
from daegam.api._common import error

__all__ = ["OverridesInput", "overrides_manifest"]


class OverridesInput(Protocol):
    """오버라이드 요청 본문. patch(RFC 7386) / manifest(전체 dict) / yaml(전체 YAML) 중 하나다."""

    patch: dict[str, Any] | None
    manifest: dict[str, Any] | None
    yaml: str | None


def overrides_manifest(body: OverridesInput) -> dict[str, Any] | None:
    """셋 중 정확히 하나가 왔는지 보고 전체 문서를 돌려준다. patch 만 왔으면 None 이다.

    셋이 아니면 422 invalid_value, YAML 이 깨졌거나 매핑이 아니면 422 invalid_yaml."""
    given = [k for k in ("patch", "manifest", "yaml") if getattr(body, k) is not None]
    if len(given) != 1:
        raise error(422, "invalid_value", "send exactly one of patch, manifest, yaml")
    if body.yaml is None:
        return body.manifest
    try:
        manifest = yamlsafe.safe_load(body.yaml)
    except yaml.YAMLError as e:
        raise error(422, "invalid_yaml", str(e)) from e
    if not isinstance(manifest, dict):
        raise error(422, "invalid_yaml", "YAML must be a single mapping")
    return manifest
