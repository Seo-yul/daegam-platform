"""verify 리포트와 형식 감지 결과에서 판정(verdict)을 만든다.

`POST /backend-apis/verify` 의 응답과 `POST /backend-apis` 의 수락 규칙이 같은
`compute_verdict` 를 쓴다. 여기서 error 가 하나라도 나오면 업로드는 반드시 422 다.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from typing import Any, Literal

from daegam.pipeline.convert import case_collisions
from daegam.pipeline.detect import UNSUPPORTED_MESSAGE, SourceFormat

ItemLevel = Literal["error", "warn", "info"]
VerdictLevel = Literal["ok", "warn", "error"]
# 항목 문장에 든 값. 화면이 code 와 이것으로 화면 언어의 문장을 만든다 (P49 6절)
ItemArgs = dict[str, str | list[str]]

_SEVERITY: dict[str, int] = {"info": 0, "warn": 1, "error": 2}
_STATUS_LEVEL: dict[str, ItemLevel] = {"fail": "error", "warn": "warn", "skip": "info"}

# spec2openapi 0.7.0 체크 id 21개 → (최소 level, code).
# 결과 status 가 fail 이면 error, warn 이면 warn 이 기본이고,
# 표의 level 이 그보다 높으면 표가 이긴다.
# skip 은 표와 무관하게 info 다. 표에 없는 id 는 status 그대로 간다.
# x-soap.mixed-rest 는 라이브러리에서는 warn 이지만 F2 가 혼합 스펙을 거절하므로 error 로 올린다.
CHECK_LEVELS: dict[str, tuple[ItemLevel, str]] = {
    # document
    "document.mapping": ("error", "document.mapping"),
    "document.openapi3": ("error", "document.openapi3"),
    "document.has-paths": ("error", "document.has-paths"),
    "document.has-operations": ("error", "document.has-operations"),
    # tool-name
    "tool-name.present": ("error", "tool-name.present"),
    "tool-name.safe": ("error", "tool-name.safe"),
    "tool-name.normalized": ("warn", "tool-name.normalized"),
    "tool-name.unique": ("error", "tool-name.unique"),
    "tool-name.normalization-collision": ("error", "tool-name.normalization-collision"),
    # tool-description
    "tool-description.present": ("warn", "tool-description.present"),
    # x-soap (version 은 soapVersion 이 없으면 warn, 값이 틀리면 fail 을 낸다.
    # fail 은 status 로 이미 error 가 되므로 표에는 warn 쪽만 적는다)
    "x-soap.version": ("warn", "x-soap.version"),
    "x-soap.endpoint": ("warn", "x-soap.endpoint"),
    "x-soap.input-element": ("error", "x-soap.input-element"),
    "x-soap.output-element": ("warn", "x-soap.output-element"),
    "x-soap.refs": ("error", "x-soap.refs"),
    "x-soap.choice": ("error", "x-soap.choice"),
    "x-soap.substitution": ("error", "x-soap.substitution"),
    "x-soap.mixed-rest": ("error", "x-soap.mixed-rest"),
    # deep (선택 의존성. 없으면 skip → info)
    "openapi.schema-valid": ("error", "openapi.schema-valid"),
    "fastmcp.roundtrip": ("error", "fastmcp.roundtrip"),
    "fastmcp.tool-materialized": ("error", "fastmcp.tool-materialized"),
}

# 형식 감지·변환 단계에서 오는 항목
FORMAT_ITEMS: dict[str, tuple[ItemLevel, str, str]] = {
    "swagger2": ("info", "converted-from-swagger2", "converted from Swagger 2.0"),
    "wsdl": ("info", "converted-from-wsdl", "converted from WSDL"),
    "unsupported": ("error", "unsupported-format", UNSUPPORTED_MESSAGE),
}
# 파이프라인 오류 코드 → verdict code (report 가 없는 실패)
PIPELINE_ERROR_CODES: dict[str, str] = {
    "too_large": "too-large",
    "unsupported": "unsupported-format",
    "conversion": "conversion-failed",
    "mixed": "x-soap.mixed-rest",
}
MIXED_MESSAGE = "spec mixes x-soap and plain REST operations; split it into separate specs"
# 스펙에서 플랫폼이 직접 찾는 항목 (verify 체크가 아니다)
CASE_COLLISION_CODE = "tool-args.case-collision"


@dataclass(frozen=True)
class VerdictItem:
    level: ItemLevel
    code: str
    message: str
    count: int = 1
    # message 에 든 값. 라이브러리가 쓴 항목은 비어 있고, 화면은 그때 message 를 그대로 보인다
    args: ItemArgs = field(default_factory=dict, hash=False)

    def to_dict(self) -> dict[str, Any]:
        return {
            "level": self.level,
            "code": self.code,
            "message": self.message,
            "count": self.count,
            # 얼린 항목의 dict 를 응답과 저장 문서가 함께 들고 가지 않게 사본을 준다
            "args": dict(self.args),
        }


@dataclass(frozen=True)
class Verdict:
    level: VerdictLevel
    items: tuple[VerdictItem, ...]

    @property
    def codes(self) -> set[str]:
        return {i.code for i in self.items}

    def to_dict(self) -> dict[str, Any]:
        return {"level": self.level, "items": [i.to_dict() for i in self.items]}


def level_for_check(check_id: str, status: str) -> ItemLevel | None:
    """체크 하나의 (id, status) 에 줄 level. pass 면 None (항목을 만들지 않는다)."""
    if status == "pass":
        return None
    if status == "skip":
        return "info"
    base = _STATUS_LEVEL.get(status, "error")
    entry = CHECK_LEVELS.get(check_id)
    if entry is None:
        return base
    floor = entry[0]
    return floor if _SEVERITY[floor] > _SEVERITY[base] else base


def code_for_check(check_id: str) -> str:
    entry = CHECK_LEVELS.get(check_id)
    return entry[1] if entry else check_id


class _Collector:
    """같은 code 는 하나로 접고 count 를 센다. 메시지와 인자는 처음 본 것을 쓴다."""

    def __init__(self) -> None:
        self._items: dict[str, VerdictItem] = {}

    def add(self, level: ItemLevel, code: str, message: str) -> None:
        self.add_item(VerdictItem(level=level, code=code, message=message))

    def add_item(self, item: VerdictItem) -> None:
        prev = self._items.get(item.code)
        if prev is None:
            self._items[item.code] = item
            return
        level = item.level if _SEVERITY[item.level] > _SEVERITY[prev.level] else prev.level
        self._items[item.code] = replace(prev, level=level, count=prev.count + 1)

    def has(self, code: str) -> bool:
        return code in self._items

    def build(self) -> Verdict:
        items = list(self._items.values())
        # 심각한 것부터. 같은 level 안에서는 처음 본 순서.
        items.sort(key=lambda i: -_SEVERITY[i.level])
        level: VerdictLevel = "ok"
        if any(i.level == "error" for i in items):
            level = "error"
        elif any(i.level == "warn" for i in items):
            level = "warn"
        return Verdict(level=level, items=tuple(items))


def compute_verdict(
    source_format: SourceFormat | str,
    report: dict[str, Any] | None,
    mixed: bool,
    *,
    error: tuple[str, str] | None = None,
    extra: tuple[VerdictItem, ...] = (),
) -> Verdict:
    """형식 감지 결과 + verify 리포트 + 혼합 여부에서 판정을 만든다.

    `error` 는 리포트가 만들어지기 전에 파이프라인이 실패한 경우의 (code, detail) 이다
    (too_large / unsupported / conversion). report 가 None 이면 그 실패만 항목이 된다.
    `extra` 는 플랫폼이 스펙에서 직접 찾은 항목이다.
    """
    c = _Collector()

    if error is not None:
        err_code, detail = error
        c.add("error", PIPELINE_ERROR_CODES.get(err_code, err_code), detail)
        if err_code == "too_large":
            # 크기 초과는 감지조차 안 했으므로 형식 항목을 붙이지 않는다
            return c.build()

    fmt = FORMAT_ITEMS.get(str(source_format))
    if fmt is not None and not c.has(fmt[1]) and (error is None or fmt[0] == "error"):
        # "변환됨" info 는 실제로 변환이 끝났을 때만 붙인다
        c.add(*fmt)

    for r in (report or {}).get("results") or []:
        level = level_for_check(r.get("id", ""), r.get("status", ""))
        if level is None:
            continue
        c.add(level, code_for_check(r["id"]), r.get("message") or r["id"])

    if mixed and not c.has("x-soap.mixed-rest"):
        # F2 의 자체 혼합 감지와 x-soap.mixed-rest 는 같은 결과여야 한다. 리포트에 없을 때만 보탠다.
        c.add("error", "x-soap.mixed-rest", MIXED_MESSAGE)

    for item in extra:
        c.add_item(item)

    return c.build()


def case_collision_items(spec: dict[str, Any] | None) -> tuple[VerdictItem, ...]:
    """도구 인자 이름이 대소문자만 다르면 경고. ToolHive 프록시·vMCP 가 그 인자를 함께 보낸 호출을
    400 으로 막는다. 인자 이름은 도구 오버라이드로 바꿀 수 없으므로 거부하지 않고 알리기만 한다.
    args 는 도구 이름(tool)과 인자 이름 목록(names)이다. 화면은 이름을 이어 화면 언어로
    문장을 만든다."""
    if not spec:
        return ()
    return tuple(
        VerdictItem(
            "warn",
            CASE_COLLISION_CODE,
            f"{tool}: 인자 {' · '.join(names)} 는 대소문자만 다릅니다. ToolHive 는 이 인자들을 "
            "함께 보낸 호출을 400 Invalid Request 로 거절합니다",
            args={"tool": tool, "names": list(names)},
        )
        for tool, names in case_collisions(spec)
    )


def verdict_of(insp: Any) -> Verdict:
    """`pipeline.convert.Inspection` 에서 바로 판정을 만든다."""
    err = insp.error
    return compute_verdict(
        insp.source_format,
        insp.report,
        insp.mixed,
        error=(err.code, err.detail) if err is not None else None,
        extra=case_collision_items(insp.spec) if err is None else (),
    )
