"""컨테이너 자원(쿠버네티스 `resources`)과 LimitRange 의 검증, 그리고 합치기 (D54).

home Cluster 의 기본 자원과 LimitRange 는 환경
변수(`DAEGAM_HOME_RESOURCES`·`DAEGAM_HOME_LIMIT_RANGE`)로 오고, 리소스(Profile·Gateway 리비전)가 준
자원은 그 기본값을 키 단위로 덮는다. 모든 컨테이너에 requests/limits 를 요구하는 클러스터에서는 값이
빠지거나 틀린 파드가 규칙에 막혀서야 드러난다. 그래서 틀린 값은 기동이나 배포에서 먼저 거절한다.
quantity 는 쿠버네티스 규칙(`kubernetes.utils.parse_quantity`)으로 읽고 비교한다. ToolHive CRD 는
cpu·memory 를 문자열로만 받으므로 숫자 quantity 도 받지 않는다.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any

from kubernetes.utils import parse_quantity

from daegam.errors import ServiceError

REQUIRED_RESOURCES = ("cpu", "memory")
LIMIT_RANGE_SECTIONS = ("default", "defaultRequest", "max", "min", "maxLimitRequestRatio")


class InvalidResources(ServiceError):
    """합친 자원이 쿠버네티스가 받지 않는 꼴이다(requests 가 limits 를 넘는 등). 배포가 422 다."""

    def __init__(self, detail: str):
        super().__init__(422, "invalid_resources", detail)


def _quantity(value: Any, where: str) -> Decimal:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{where} must be a quantity string such as '100m' or '128Mi'")
    try:
        return parse_quantity(value)
    except ValueError as e:
        raise ValueError(f"{where} {value!r} is not a Kubernetes quantity") from e


def check_order(resources: dict[str, Any], where: str) -> None:
    """requests 가 limits 를 넘지 않는지. 둘 다 적힌 자원만 본다."""
    requests = resources.get("requests") or {}
    limits = resources.get("limits") or {}
    for name in sorted(set(requests) & set(limits)):
        req = _quantity(requests[name], f"{where}.requests.{name}")
        lim = _quantity(limits[name], f"{where}.limits.{name}")
        if req > lim:
            raise ValueError(
                f"{where}.requests.{name} ({requests[name]}) exceeds "
                f"{where}.limits.{name} ({limits[name]})"
            )


def validate_complete_resources(value: Any, *, where: str) -> dict[str, dict[str, str]]:
    """requests 와 limits 가 둘 다 있고, 각각 cpu·memory 를 문자열 quantity 로 가지며, requests 가
    limits 를 넘지 않는 값. 그 밖의 최상위 키는 받지 않는다(`request` 같은 오타를 조용히 넘기지
    않는다)."""
    if not isinstance(value, dict) or set(value) != {"requests", "limits"}:
        raise ValueError(f"{where} must have exactly the keys requests and limits")
    out: dict[str, dict[str, str]] = {}
    for part in ("requests", "limits"):
        section = value[part]
        if not isinstance(section, dict):
            raise ValueError(f"{where}.{part} must be an object")
        missing = [name for name in REQUIRED_RESOURCES if name not in section]
        if missing:
            raise ValueError(f"{where}.{part} is missing {', '.join(missing)}")
        for name, quantity in section.items():
            _quantity(quantity, f"{where}.{part}.{name}")
        out[part] = dict(section)
    check_order(out, where)
    return out


def validate_limit_range(value: Any, *, where: str) -> dict[str, Any]:
    """LimitRange 의 spec. `limits` 가 비어 있지 않고, 항목마다 `type` 이 있으며, 자원 절의 값이
    문자열 quantity 이고, `defaultRequest` 가 `default` 를 넘지 않는다."""
    if not isinstance(value, dict) or not isinstance(value.get("limits"), list):
        raise ValueError(f"{where} must be a LimitRange spec with a limits list")
    if not value["limits"] or set(value) != {"limits"}:
        raise ValueError(f"{where} must have exactly one key, a non-empty limits list")
    for i, item in enumerate(value["limits"]):
        at = f"{where}.limits[{i}]"
        if not isinstance(item, dict) or not item.get("type"):
            raise ValueError(f"{at} needs a type (Container, Pod or PersistentVolumeClaim)")
        for key in set(item) - {"type"}:
            if key not in LIMIT_RANGE_SECTIONS:
                raise ValueError(f"{at}.{key} is not a LimitRange field")
            section = item[key]
            if not isinstance(section, dict):
                raise ValueError(f"{at}.{key} must be an object")
            for name, quantity in section.items():
                _quantity(quantity, f"{at}.{key}.{name}")
        check_order(
            {"requests": item.get("defaultRequest") or {}, "limits": item.get("default") or {}},
            at,
        )
    return value


def merge_resources(
    base: dict[str, Any] | None, override: dict[str, Any] | None
) -> dict[str, Any] | None:
    """리소스가 준 자원이 기본값을 `requests`·`limits` 의 키 단위로 덮는다. `{"limits": {"cpu":
    "1"}}` 만 준 값이 기본값의 나머지 한도를 잃지 않게 하려는 것이다."""
    if not override:
        return base
    if not base:
        return override
    out: dict[str, Any] = {}
    for part in sorted(set(base) | set(override)):
        b, o = base.get(part), override.get(part)
        if isinstance(b, dict) and isinstance(o, dict):
            out[part] = {**b, **o}
        else:
            out[part] = o if o is not None else b
    return out


def effective_resources(
    base: dict[str, Any] | None, override: dict[str, Any] | None
) -> dict[str, Any] | None:
    """합친 뒤 requests 가 limits 를 넘으면 배포를 422 로 막는다."""
    merged = merge_resources(base, override)
    if merged:
        try:
            check_order(merged, "resources")
        except ValueError as e:
            raise InvalidResources(str(e)) from e
    return merged
