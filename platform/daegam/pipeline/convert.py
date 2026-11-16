"""감지 → 변환 → 혼합 검사 → verify. 순수 함수, 파일이나 DB를 건드리지 않는다."""

from __future__ import annotations

from dataclasses import dataclass, field
from importlib.metadata import version as _pkg_version
from typing import Any, Literal

from spec2openapi import ConversionError, convert_swagger, convert_wsdl, dump_spec, verify

from daegam.pipeline.detect import UNSUPPORTED_MESSAGE, SourceFormat, detect
from daegam.pipeline.tool_arguments import tool_arguments

S2O_VERSION = _pkg_version("spec2openapi")

HTTP_METHODS = ("get", "put", "post", "delete", "options", "head", "patch", "trace")
MAX_UPLOAD_BYTES = 20 * 1024 * 1024

Kind = Literal["soap", "rest"]
ErrorCode = Literal[
    "too_large", "unsupported", "mixed", "conversion", "verify", "verify_incomplete"
]


class PipelineError(Exception):
    def __init__(self, code: ErrorCode, detail: str, report: dict[str, Any] | None = None):
        super().__init__(f"{code}: {detail}")
        self.code = code
        self.detail = detail
        self.report = report

    def to_dict(self) -> dict[str, Any]:
        body: dict[str, Any] = {"code": self.code, "detail": self.detail}
        if self.report is not None:
            body["report"] = self.report
        return body


@dataclass(frozen=True)
class PipelineResult:
    kind: Kind
    source_format: SourceFormat
    spec: dict[str, Any]
    yaml: str
    report: dict[str, Any]
    tool_count: int
    tool_names: list[str] = field(default_factory=list)
    tool_details: list[dict[str, Any]] = field(default_factory=list)
    s2o_version: str = S2O_VERSION


def iter_operations(spec: dict[str, Any]):
    """(path, method, operation) 순회. HTTP 메서드 키만 오퍼레이션으로 센다."""
    paths = spec.get("paths") or {}
    if not isinstance(paths, dict):
        return
    for path, item in paths.items():
        if not isinstance(item, dict):
            continue
        for method in HTTP_METHODS:
            op = item.get(method)
            if isinstance(op, dict):
                yield path, method, op


def count_kinds(spec: dict[str, Any]) -> tuple[int, int]:
    """(x-soap 오퍼레이션 수, 나머지 REST 오퍼레이션 수)."""
    soap = 0
    rest = 0
    for _path, _method, op in iter_operations(spec):
        if "x-soap" in op:
            soap += 1
        else:
            rest += 1
    return soap, rest


def _mixed_error(soap: int, rest: int) -> PipelineError:
    return PipelineError(
        "mixed",
        f"spec mixes {soap} SOAP (x-soap) and {rest} REST operations; split it into separate specs",
    )


def _tool_name_of(path: str, method: str, op: dict[str, Any]) -> str:
    op_id = op.get("operationId")
    if isinstance(op_id, str) and op_id:
        return op_id
    slug = path.strip("/").replace("/", "_").replace("{", "").replace("}", "")
    return f"{method}_{slug}"


def tool_names_of(spec: dict[str, Any]) -> list[str]:
    """FastMCP가 툴 이름으로 쓰는 operationId. 없으면 method_path 형태로 대체한다."""
    return [_tool_name_of(path, method, op) for path, method, op in iter_operations(spec)]


def tool_details_of(spec: dict[str, Any]) -> list[dict[str, Any]]:
    """툴별 원본 메타. `operation_id` 는 tool_names_of 와 같은 규칙이고
    summary/description 은 스펙 오퍼레이션의 것을 그대로 담는다 (없으면 None). `method` 는 그
    오퍼레이션의 HTTP 메서드(대문자)이고 Tool 효과가 읽는다(P39b 14절 4부). `arguments` 는 FastMCP
    입력 스키마의 인자 경로와 형식이고(`pipeline/tool_arguments.py`) 인자 조건의 검증이 읽는다(P52
    1절). FastMCP 가 스펙을 읽지 못하면 None 이다."""
    arguments = tool_arguments(spec)
    details: list[dict[str, Any]] = []
    for path, method, op in iter_operations(spec):
        summary = op.get("summary")
        description = op.get("description")
        desc = description if isinstance(description, str) and description else None
        details.append(
            {
                "operation_id": _tool_name_of(path, method, op),
                "summary": summary if isinstance(summary, str) and summary else None,
                "description": desc,
                "method": method.upper(),
                "arguments": None if arguments is None else arguments.get((path, method.upper())),
            }
        )
    return details


def _deref(spec: dict[str, Any], node: Any) -> dict[str, Any] | None:
    """문서 안 `$ref`(`#/...`)만 따라간다. 밖을 가리키거나 풀 수 없으면 None."""
    for _ in range(16):
        if not isinstance(node, dict) or "$ref" not in node:
            break
        ref = node["$ref"]
        if not isinstance(ref, str) or not ref.startswith("#/"):
            return None
        node = spec
        for part in ref[2:].split("/"):
            key = part.replace("~1", "/").replace("~0", "~")
            if not isinstance(node, dict) or key not in node:
                return None
            node = node[key]
    return node if isinstance(node, dict) else None


def _case_clashes(names: list[str]) -> list[list[str]]:
    by_fold: dict[str, set[str]] = {}
    for n in names:
        by_fold.setdefault(n.casefold(), set()).add(n)
    return [sorted(g) for g in by_fold.values() if len(g) > 1]


def _object_clashes(
    spec: dict[str, Any], schema: Any, seen: set[int], depth: int
) -> list[list[str]]:
    """중첩 객체 스키마마다 속성 이름의 대소문자 충돌. seen 은 도구 하나에서 공유해 같은 스키마를
    두 번 세지 않고 순환 참조도 막는다."""
    node = _deref(spec, schema)
    if node is None or id(node) in seen or depth > 8:
        return []
    seen.add(id(node))
    props = node.get("properties")
    out: list[list[str]] = []
    if isinstance(props, dict):
        out += _case_clashes([k for k in props if isinstance(k, str)])
        for child in props.values():
            out += _object_clashes(spec, child, seen, depth + 1)
    items = node.get("items")
    if items is not None:
        out += _object_clashes(spec, items, seen, depth + 1)
    for key in ("allOf", "anyOf", "oneOf"):
        for part in node.get(key) or []:
            out += _object_clashes(spec, part, seen, depth + 1)
    return out


def case_collisions(spec: dict[str, Any]) -> list[tuple[str, list[str]]]:
    """도구마다 인자 이름이 대소문자만 다른 쌍. [(도구 이름, [이름들])].

    FastMCP 는 path·query·header 파라미터와 JSON 본문 객체의 속성을 한 arguments 객체로 편다.
    이름이 똑같으면 접미사로 가르지만 대소문자만 다르면 둘 다 남는다. ToolHive 파서는 요청 본문의
    어느 객체든 이름이 대소문자 접기로 같은 멤버가 둘이면 그 호출을 400 으로 거절한다
    (v0.51.2 pkg/mcp/json_ambiguity.go)."""
    out: list[tuple[str, list[str]]] = []
    for path, method, op in iter_operations(spec):
        item = (spec.get("paths") or {}).get(path) or {}
        top: list[str] = []
        for raw in [*(item.get("parameters") or []), *(op.get("parameters") or [])]:
            p = _deref(spec, raw)
            if p is not None and isinstance(p.get("name"), str):
                top.append(p["name"])
        clashes: list[list[str]] = []
        body = _deref(spec, op.get("requestBody"))
        content = (body or {}).get("content") or {}
        media = content.get("application/json") if isinstance(content, dict) else None
        schema = _deref(spec, (media or {}).get("schema"))
        if schema is not None:
            props = schema.get("properties")
            seen = {id(schema)}
            if isinstance(props, dict):
                top += [k for k in props if isinstance(k, str)]
                for sub in props.values():
                    clashes += _object_clashes(spec, sub, seen, 1)
        clashes = _case_clashes(top) + clashes
        tool = _tool_name_of(path, method, op)
        out += [(tool, names) for names in clashes]
    return out


def _convert(source_format: SourceFormat, content: bytes, doc: Any) -> dict[str, Any]:
    try:
        if source_format == "wsdl":
            return convert_wsdl(content=content, forbid_external=True, huge_tree=False)
        if source_format == "swagger2":
            return convert_swagger(doc)
        if source_format == "openapi3":
            return doc
    except ConversionError as e:
        raise PipelineError("conversion", str(e)) from e
    except Exception as e:  # noqa: BLE001 - 라이브러리 내부 예외를 모두 conversion으로 묶는다
        raise PipelineError("conversion", f"{type(e).__name__}: {e}") from e
    raise PipelineError("unsupported", f"unsupported source format: {source_format}")


def _verify(spec: dict[str, Any]) -> dict[str, Any]:
    return verify(spec, deep=True).to_dict()


@dataclass(frozen=True)
class Inspection:
    """감지·변환·혼합 검사·verify 를 예외 없이 끝까지 돌린 결과. verify/convert API 가 그대로 쓴다.

    error 는 리포트를 만들기 전에 실패한 경우(too_large | unsupported | conversion)다.
    mixed 스펙도 verify 까지 돌려 x-soap.mixed-rest 항목이 리포트에 남게 한다.
    """

    source_format: SourceFormat
    kind: Kind | None
    mixed: bool
    spec: dict[str, Any] | None
    report: dict[str, Any] | None
    tool_names: list[str]
    tool_details: list[dict[str, Any]] = field(default_factory=list)
    error: PipelineError | None = None
    soap_count: int = 0
    rest_count: int = 0

    def failure(self) -> PipelineError | None:
        """finalize 가 던질 오류. 순서는 error, mixed, verify, verify_incomplete 다."""
        if self.error is not None:
            return self.error
        if self.mixed:
            return _mixed_error(self.soap_count, self.rest_count)
        report = self.report
        if report is None:
            return PipelineError("verify", "verify did not run")
        if not report["ok"]:
            failed = [r["id"] for r in report["results"] if r["status"] == "fail"]
            return PipelineError("verify", f"verify failed: {', '.join(failed)}", report)
        if not report["complete"]:
            skipped = [r["id"] for r in report["results"] if r["status"] == "skip"]
            return PipelineError(
                "verify_incomplete",
                "verify skipped checks (missing optional dependency?): " + ", ".join(skipped),
                report,
            )
        return None


def inspect(
    content: bytes, *, filename: str | None = None, max_bytes: int = MAX_UPLOAD_BYTES
) -> Inspection:
    """예외를 던지지 않는다. 실패도 Inspection 으로 돌려준다."""
    if len(content) > max_bytes:
        err = PipelineError("too_large", f"upload is {len(content)} bytes; limit is {max_bytes}")
        return Inspection("unsupported", None, False, None, None, [], error=err)

    source_format, doc = detect(content, filename=filename)
    if source_format == "unsupported":
        err = PipelineError(
            "unsupported", UNSUPPORTED_MESSAGE + (f" ({filename})" if filename else "")
        )
        return Inspection(source_format, None, False, None, None, [], error=err)

    try:
        spec = _convert(source_format, content, doc)
    except PipelineError as e:
        return Inspection(source_format, None, False, None, None, [], error=e)

    soap, rest = count_kinds(spec)
    mixed = bool(soap and rest)
    kind: Kind | None = None if mixed else ("soap" if soap else "rest")
    report = _verify(spec)
    return Inspection(
        source_format=source_format,
        kind=kind,
        mixed=mixed,
        spec=spec,
        report=report,
        tool_names=tool_names_of(spec),
        tool_details=tool_details_of(spec),
        soap_count=soap,
        rest_count=rest,
    )


def finalize(insp: Inspection) -> PipelineResult:
    """Inspection 을 저장 가능한 PipelineResult 로. 실패면 PipelineError."""
    err = insp.failure()
    if err is not None:
        raise err
    assert insp.spec is not None and insp.report is not None and insp.kind is not None
    return PipelineResult(
        kind=insp.kind,
        source_format=insp.source_format,
        spec=insp.spec,
        yaml=dump_spec(insp.spec, fmt="yaml"),
        report=insp.report,
        tool_count=len(insp.tool_names),
        tool_names=insp.tool_names,
        tool_details=insp.tool_details,
    )


def run(content: bytes, *, filename: str | None = None) -> PipelineResult:
    return finalize(inspect(content, filename=filename))
