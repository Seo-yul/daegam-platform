"""진입점이다. /config/openapi.yaml 과 /config/profile.json 으로 FastMCP streamable-http 를
:8000/mcp 에 띄운다.

환경변수
  RUNTIME_SPEC_PATH     기본 /config/openapi.yaml
  RUNTIME_PROFILE_PATH  기본 /config/profile.json (없으면 전부 노출)
  RUNTIME_HOST / RUNTIME_PORT / RUNTIME_PATH   기본 0.0.0.0 / 8000 / /mcp
  SPEC2OPENAPI_ENDPOINT / AUTH / USERNAME / PASSWORD / SOAP_HEADERS 등
                        spec2openapi BridgeOptions.from_env 가 읽는다
  DAEGAM_CA_B64         백엔드 TLS 검증에 더할 CA 묶음 (PEM 의 base64, `_verify_with_ca`)
  DAEGAM_BACKEND_<i>_*  backend 가 여럿일 때 backend 마다의 값 (`_backend_options`)

Tool 규칙 (P52 4절). profile.json 의 `rules` 가 Tool 마다 `readOnlyHint` 를 정하고(표시가 없으면
메서드, GET·HEAD 가 읽기다), 인자 조건을 두 자리에서 본다. backend 서버의 미들웨어
(`ConditionMiddleware`)가 `tools/call` 의 arguments 를 보고, 그 backend 의 HTTP 클라이언트에 단
요청 훅(`_ConditionHook`)이 백엔드로 실제 나가는 요청을 본다. 서버는 `initialize` 의
`capabilities.experimental.arg_conditions` 로 조건을 지킨다는 것과 풀지 못한 조건의 경로를 알린다.
프록시는 그것을 그대로 넘기고 vMCP 는 넘기지 않으므로(F0) 플랫폼은 서버 자신에게 묻는다.

클라이언트의 HTTP 머리는 MCP 규약이 쓰는 것만 남기고 들어올 때 버린다(`ClientHeaderFilter`).
FastMCP 의 OpenAPI Tool 은 들어온 요청의 머리를 백엔드 요청에 옮기므로(`get_http_headers`), 그대로
두면 호출자가 정의에 없는 머리(`X-HTTP-Method-Override` 등)나 비어 있는 header 인자를 채울 수 있다.
남긴 규약 머리도 백엔드로 나가는 요청에서는 지운다(`_drop_protocol_headers`).
"""

from __future__ import annotations

import base64
import contextvars
import json
import logging
import os
import re
import ssl
import sys
from pathlib import Path
from typing import Any

from fastmcp import FastMCP  # 아래 경고 필터보다 먼저 fastmcp 를 import 해야 한다
from fastmcp.exceptions import ToolError
from fastmcp.server.middleware import Middleware

from runtime.conditions import (
    ARG_CONDITIONS,
    ARG_CONDITIONS_VERSION,
    ToolGuard,
    closed_guard,
    compile_guard,
)
from runtime.profile import (
    BackendEntry,
    ProfileConfig,
    ProfileError,
    ToolSelection,
    load_profile,
)

log = logging.getLogger("daegam.mcp-runtime")

# 관리자가 준 CA 묶음(Platform Settings `network.ca_bundle`, PEM 의 base64)이다. 플랫폼이 배포 때
# 렌더하고, ToolHive 가 줄바꿈이 든 env 값을 거절하므로 base64 다
CA_ENV = "DAEGAM_CA_B64"
PEM_CERTIFICATE = "-----BEGIN CERTIFICATE-----"
# 표시가 없는 Tool 을 읽기 전용으로 보는 메서드다. 플랫폼 `services/tool_effects.py` 의
# `READ_METHODS` 와 같다. WSDL 오퍼레이션은 POST 라 쓰기다
READ_METHODS = frozenset({"GET", "HEAD"})


def _operation_ids(spec: dict[str, Any]) -> list[str]:
    methods = ("get", "put", "post", "delete", "options", "head", "patch", "trace")
    ids: list[str] = []
    for item in (spec.get("paths") or {}).values():
        if not isinstance(item, dict):
            continue
        for method, op in item.items():
            if method.lower() in methods and isinstance(op, dict) and op.get("operationId"):
                ids.append(op["operationId"])
    return ids


# MCP 클라이언트가 보내는 머리 가운데 서버가 읽는 것이다. 나머지는 들어올 때 버린다(모듈 머리).
# Modern 서버는 `mcp-method` 가 본문의 method 와, `mcp-name` 이 본문의 이름과 같은지 보고 다르면
# -32020 으로 거절하므로 둘을 남긴다. `mcp-param-*` 는 버린다. 서버가 그 머리를 요구하게 하는
# `x-mcp-header` 표시는 Tool 을 만들 때 걷는다(`_drop_header_marks`)
CLIENT_HEADERS = frozenset(
    {
        b"host",
        b"content-type",
        b"content-length",
        b"accept",
        b"mcp-session-id",
        b"mcp-protocol-version",
        b"mcp-method",
        b"mcp-name",
        b"last-event-id",
    }
)


class ClientHeaderFilter:
    """ASGI 미들웨어다. HTTP 요청의 머리를 `CLIENT_HEADERS` 로 줄인다."""

    def __init__(self, app: Any):
        self.app = app

    async def __call__(self, scope: Any, receive: Any, send: Any) -> None:
        if scope.get("type") == "http":
            headers = [(k, v) for k, v in scope.get("headers") or [] if k.lower() in CLIENT_HEADERS]
            scope = {**scope, "headers": headers}
        await self.app(scope, receive, send)


# `CLIENT_HEADERS` 가운데 백엔드의 정의에 없는 MCP 규약 머리다. `get_http_headers` 는
# mcp-session-id 만 빼고 옮기므로, 나머지는 백엔드로 나가는 요청에서 지운다
PROTOCOL_HEADERS = ("mcp-protocol-version", "mcp-method", "mcp-name", "last-event-id")

# 값이 이름 → 스키마 사전인 JSON Schema 키다. 그 사전의 키는 인자 이름이라 표시로 보지 않는다
_SCHEMA_MAPS = frozenset(
    {"properties", "patternProperties", "$defs", "definitions", "dependentSchemas"}
)
# 값이 스키마가 아니라 데이터인 키다. 그 안은 걷지 않는다
_SCHEMA_DATA = frozenset({"enum", "const", "default", "examples", "example"})


async def _drop_protocol_headers(request: Any) -> None:
    """백엔드 HTTP 클라이언트의 첫 요청 훅이다. 옮겨진 규약 머리를 지운다.
    조건 훅(`_ConditionHook`)은 이 훅 뒤에 돌아 실제로 나가는 머리를 본다."""
    for name in PROTOCOL_HEADERS:
        if name in request.headers:
            del request.headers[name]


def _drop_header_marks(schema: Any) -> None:
    """입력 스키마에서 `x-mcp-header` 표시를 자리에서 걷는다. FastMCP 는 OpenAPI 의 `x-*` 키를
    Tool 스키마로 옮기는데, 표시가 남으면 Modern 서버가 그 인자의 `Mcp-Param-*` 머리를 요구하고
    `ClientHeaderFilter` 는 그 머리를 버리므로 호출이 -32020 으로 거절된다."""
    if isinstance(schema, list):
        for item in schema:
            _drop_header_marks(item)
        return
    if not isinstance(schema, dict):
        return
    schema.pop("x-mcp-header", None)
    for key, value in schema.items():
        if key in _SCHEMA_DATA:
            continue
        if key in _SCHEMA_MAPS and isinstance(value, dict):
            for sub in value.values():
                _drop_header_marks(sub)
        else:
            _drop_header_marks(value)


def http_middleware() -> list[Any]:
    """서버의 HTTP 앱에 거는 ASGI 미들웨어다. `main` 과 시험이 같은 것을 쓴다."""
    from starlette.middleware import Middleware as ASGIMiddleware

    return [ASGIMiddleware(ClientHeaderFilter)]


# 지금 도는 `tools/call` 의 지킴이다. 미들웨어가 호출마다 두고 요청 훅이 읽는다. 지킴이가 없는 Tool
# 의 호출은 `NO_GUARD` 이고, 아무것도 없으면 미들웨어를 거치지 않은 호출이다
_CURRENT: contextvars.ContextVar[ToolGuard | None] = contextvars.ContextVar(
    "daegam_condition_guard", default=None
)
NO_GUARD = ToolGuard()


class ConditionMiddleware(Middleware):
    """Tool 이름마다의 인자 조건을 `tools/call` 앞에서 본다. 어긋나면 그 인자를 이름 댄 Tool
    오류이고 다음 단계(백엔드를 부르는 Tool)는 돌지 않는다. 지나면 그 지킴이를 두고 다음 단계를
    부른다. 그 Tool 이 백엔드로 보내는 요청은 요청 훅이 다시 본다. backend 서버마다 하나이고, 여러
    backend 를 합친 서버에서도 mount 한 서버의 미들웨어가 그 서버의 Tool 호출마다 돈다."""

    def __init__(self, guards: dict[str, ToolGuard]):
        self.guards = guards

    async def on_call_tool(self, context: Any, call_next: Any) -> Any:
        guard = self.guards.get(context.message.name)
        if guard is not None:
            refusal = guard.refusal(context.message.arguments or {})
            if refusal is not None:
                raise ToolError(refusal)
        token = _CURRENT.set(guard or NO_GUARD)
        try:
            return await call_next(context)
        finally:
            _CURRENT.reset(token)


def _path_pattern(path: str) -> re.Pattern[str]:
    """OpenAPI 경로 틀을 요청 경로의 끝에 맞는 정규식으로 바꾼다. 인자 자리는 `/` 가 없는 한
    토막이다(FastMCP 는 인자 값의 `/` 를 %2F 로 쓴다)."""
    parts = re.split(r"(\{[^}]+\})", path)
    body = "".join("[^/]+" if p.startswith("{") else re.escape(p) for p in parts)
    return re.compile(body + "$")


class _ConditionHook:
    """backend 하나의 HTTP 클라이언트에 다는 요청 훅이다. 백엔드로 나가는 요청을 지금 호출의
    지킴이로 판정하고, 어긋나면 Tool 오류로 보내지 않는다. 미들웨어를 거치지 않은 호출(지킴이를
    모른다)이 조건이 있는 Tool 의 라우트로 나가면 막는다."""

    def __init__(self) -> None:
        self.routes: list[tuple[str, re.Pattern[str], ToolGuard]] = []

    async def __call__(self, request: Any) -> None:
        guard = _CURRENT.get()
        if guard is None:
            path = request.url.raw_path.split(b"?", 1)[0].decode("ascii", errors="replace")
            for method, pattern, closed in self.routes:
                if request.method == method and pattern.search(path):
                    guard = closed
                    break
            else:
                return
        refusal = guard.outgoing_refusal(request)
        if refusal is not None:
            raise ToolError(refusal)


def _report(guards: dict[str, ToolGuard]) -> dict[str, Any]:
    """`capabilities.experimental.arg_conditions` 의 값이다. 풀지 못한 조건은 Tool 이름마다의
    경로다. 조건이 없어도 실어 이 이미지가 조건을 지킨다는 것을 알린다."""
    unresolved = {name: list(g.unresolved) for name, g in sorted(guards.items()) if g.unresolved}
    return {"version": ARG_CONDITIONS_VERSION, "unresolved": unresolved}


def _verify_with_ca(verify: bool, trust_env: bool) -> bool | ssl.SSLContext:
    """백엔드 호출에 넘길 TLS 검증 값. CA_ENV 가 있으면 httpx2 가 기본으로 믿는 루트(trust_env 면
    SSL_CERT_FILE, 아니면 시스템 저장소)에 그 CA 를 더한 문맥이다. SSL_CERT_FILE 을 바꾸면
    이미지의 루트를 대신하므로 더한다. 검증을 껐거나 CA 가 없으면 verify 그대로이고, 읽을 수 없는
    CA 는 로그만 남기고 기본 루트로 돈다."""
    raw = os.environ.get(CA_ENV, "")
    if not verify or not raw:
        return verify
    import httpx2

    ctx = httpx2.create_ssl_context(verify=True, trust_env=trust_env)
    try:
        pem = base64.b64decode(raw, validate=True).decode("ascii")
        ctx.load_verify_locations(cadata=pem)
    except (ValueError, ssl.SSLError) as e:
        log.warning(
            "%s is not a base64-encoded PEM certificate bundle; "
            "calling backends without the extra CA: %s",
            CA_ENV,
            e,
        )
        return verify
    # 사설 백엔드가 여전히 실패할 때 CA 가 파드에 닿았는지 로그로 먼저 본다
    log.info("trusting %d extra CA certificate(s) from %s", pem.count(PEM_CERTIFICATE), CA_ENV)
    return ctx


def _pod_options() -> Any:
    """파드 전체의 전송 설정을 env 로 읽는다(`BridgeOptions.from_env`). TLS 검증에는 관리자가 준
    CA 를 더한다(`_verify_with_ca`)."""
    from spec2openapi.bridge import BridgeOptions

    options = BridgeOptions.from_env()
    options.verify = _verify_with_ca(options.verify, options.trust_env)
    return options


def build_server(
    spec: dict[str, Any], profile: ProfileConfig, options: Any | None = None
) -> FastMCP:
    """backend 하나짜리 서버에 헬스 라우트를 붙여 돌려준다. 스펙과 평면 profile 로 만든다.
    서빙은 하지 않는다 (테스트가 인메모리로 붙는다)."""
    mcp = _build_backend_server(spec, profile, options)
    _add_health_routes(mcp)
    return mcp


def _build_backend_server(
    spec: dict[str, Any], profile: ToolSelection, options: Any | None = None
) -> FastMCP:
    """backend 하나짜리 FastMCP 서버다 (헬스 라우트 없음)."""
    from fastmcp.server.providers.openapi import MCPType
    from mcp_types import ToolAnnotations
    from spec2openapi import from_openapi_spec, spec_has_soap

    is_soap = spec_has_soap(spec)
    if profile.soap != is_soap:
        log.warning(
            "profile.soap=%s but spec %s x-soap; transport follows the spec",
            profile.soap,
            "has" if is_soap else "has no",
        )
    if not is_soap and profile.auth_kind == "wsse":
        raise ProfileError(
            "wsse_unsupported_for_rest",
            "auth_kind 'wsse' is only supported for SOAP (x-soap) specs",
        )

    options = options or _pod_options()
    # 환경변수(SPEC2OPENAPI_ENDPOINT/AUTH)가 있으면 그것이 우선이고 profile.json 은 그 다음이다.
    # 둘 다 플랫폼이 넣지만 env 는 P1 매니페스트와 호환되는 명시적 주입 경로다.
    if not options.endpoint and profile.endpoint:
        options.endpoint = profile.endpoint
    if not options.auth and profile.auth_kind != "none":
        options.auth = profile.auth_kind

    known = _operation_ids(spec)
    tools = set(profile.tools)
    if tools:
        missing = sorted(tools - set(known))
        if missing:
            log.warning("profile.tools not in spec (ignored): %s", missing)
    for op in profile.tool_overrides:
        if op not in known:
            log.warning("tool_overrides key %r is not an operationId in the spec", op)
    rules = profile.rules
    for op in rules:
        if op not in known:
            log.warning("rules key %r is not an operationId in the spec", op)
    # Tool 이름 → 인자 조건, operationId → 조건을 단 Tool 이름, 나가는 요청을 보는 훅
    guards: dict[str, ToolGuard] = {}
    guarded: dict[str, str] = {}
    hook = _ConditionHook()
    hooked: list[Any] = []
    # 규약 머리를 지우는 훅을 단 클라이언트. Tool 마다가 아니라 클라이언트마다 한 번 단다
    stripped: list[Any] = []

    def route_map_fn(route: Any, default: Any) -> Any:
        # FastMCP 가 라우트마다 부르는 콜백이다. default 는 쓰지 않지만 시그니처가 요구한다.
        # FastMCP RouteMap 은 method/path/tags 만 보므로 operationId 서브셋은 여기서 거른다.
        # tools 가 비어 있으면 제외하지 않는다. 런타임에 툴이 생기지 않으므로 보안 경계다.
        if tools and route.operation_id not in tools:
            return MCPType.EXCLUDE
        return None

    descriptions = profile.descriptions

    def mcp_component_fn(route: Any, component: Any) -> None:
        # FastMCP 가 만든 컴포넌트마다 부르는 콜백이다. 자리에서 고치고 아무것도 돌려주지 않는다.
        # FastMCP 는 이 콜백의 예외를 경고로만 남기고 Tool 을 그대로 둔다. 그래서 조건을 맨 먼저
        # 걸고, 풀다 실패하면 닫힌 지킴이를 건다. 아래 `missing` 검사가 지킴이 없이 남은 Tool 을
        # 서버를 띄우기 전에 막는다
        op = route.operation_id or ""
        _drop_header_marks(component.parameters)
        # 규약 머리를 지우는 훅은 조건 훅보다 앞에 둔다.
        # 조건 훅이 실제로 나가는 머리를 보게 하려는 것이다
        backend_client = getattr(component, "_client", None)
        if backend_client is not None and not any(c is backend_client for c in stripped):
            backend_client.event_hooks["request"].insert(0, _drop_protocol_headers)
            stripped.append(backend_client)
        rule = rules.get(op)
        if rule is not None and rule.conditions:
            paths = [c.path for c in rule.conditions]
            try:
                guard = compile_guard(rule.conditions, component.parameters, route)
                # 나가는 요청을 볼 훅을 그 Tool 의 HTTP 클라이언트에 단다(FastMCP 4.0.3 의
                # OpenAPITool._client). 달 수 없으면 조건을 지킬 수 없어 닫는다
                client = getattr(component, "_client", None)
                if client is None:
                    guard = closed_guard(paths)
                elif not any(c is client for c in hooked):
                    client.event_hooks["request"].append(hook)
                    hooked.append(client)
                hook.routes.append((str(route.method).upper(), _path_pattern(route.path), guard))
            except Exception:  # noqa: BLE001 - 풀지 못한 조건은 Tool 을 닫는다
                log.exception("could not compile the argument conditions of %s", op)
                guard = closed_guard(paths)
            guards[component.name] = guard
            guarded[op] = component.name
        read_only = rule.read_only if rule is not None else None
        if read_only is None:
            read_only = str(route.method).upper() in READ_METHODS
        component.annotations = ToolAnnotations(readOnlyHint=read_only)
        desc = descriptions.get(op)
        if desc:
            component.description = desc
        # outputSchema 광고를 걷는다. 스펙이 응답 스키마를 선언해도 REST 백엔드가 JSON 을 준다는
        # 보장이 없고, FastMCP OpenAPI 툴은 response.json() 이 실패하면(text/plain, 204 등)
        # structured_content 없이 텍스트만 돌려준다. outputSchema 를 광고한 툴이 structuredContent
        # 없이 답하면 MCP 규약대로 클라이언트(mcp 1.x·2.x 모두)가 RuntimeError 를 내고, 이것은
        # isError 툴 오류가 아니라 클라이언트 예외라 에이전트 턴이 통째로 죽는다.
        # 잃는 정보는 없다. spec2openapi 가 validate_output=False 로 넘기므로 광고되던 스키마는
        # {"type":"object","additionalProperties":true} 뿐이고, JSON 응답의 structured_content 는
        # output_schema 가 None 이어도 그대로 실린다(dict 가 아니면 {"result": ...} 로 감싼다).
        component.output_schema = None

    mcp = from_openapi_spec(
        spec,
        options=options,
        route_map_fn=route_map_fn,
        mcp_names=profile.mcp_names or None,
        mcp_component_fn=mcp_component_fn,
    )
    # Tool 이 된 operationId 가운데 조건이 있는데 지킴이가 없는 것. 콜백이 조건을 걸기 전에 실패한
    # 것이라 열린 채 띄우지 않는다
    missing = sorted(
        op
        for op, rule in rules.items()
        if rule.conditions and op in known and (not tools or op in tools) and op not in guarded
    )
    if missing:
        raise ProfileError(
            "conditions_not_applied",
            f"argument conditions could not be applied to {', '.join(missing)}",
        )
    if guards:
        mcp.add_middleware(ConditionMiddleware(guards))
    mcp.experimental_capabilities[ARG_CONDITIONS] = _report(guards)
    return mcp


def _json_object_env(name: str) -> dict[str, Any] | None:
    """env 의 JSON 객체. 없거나 비었으면 None 이고, JSON 객체가 아니면 경고를 남기고 None 이다.
    backend 하나일 때 spec2openapi 가 `SPEC2OPENAPI_SOAP_HEADERS` 를 읽는 규칙과 같다. 값은 로그에
    남기지 않는다(비밀일 수 있다)."""
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return None
    try:
        value = json.loads(raw)
    except json.JSONDecodeError:
        log.warning("env %s is not valid JSON; ignoring", name)
        return None
    if not isinstance(value, dict):
        log.warning("env %s must be a JSON object; ignoring", name)
        return None
    return value


def _backend_options(index: int, entry: BackendEntry, pod: Any | None = None) -> Any:
    """backend 순번 env (DAEGAM_BACKEND_<i>_*) 를 먼저 보고 없으면 profile.json 항목으로
    BridgeOptions 를 만든다. 접속·자격·타임아웃처럼 backend 하나의 값은 전역 SPEC2OPENAPI_* env 를
    쓰지 않는다(한 backend 로 새면 안 된다). 호출 타임아웃은 profile.json 항목의 `timeout_seconds`
    이고, 없으면 BridgeOptions 의 기본값이다. SOAP 머리 값(soap_headers)은 DAEGAM_BACKEND_<i>_
    SOAP_HEADERS 의 JSON 객체이고, 플랫폼이 Backend API 의 `soap_headers_secret` 에서 렌더한다(T60).

    TLS 검증(SPEC2OPENAPI_VERIFY)과 환경 신뢰(SPEC2OPENAPI_TRUST_ENV)는 파드 전체의 전송 설정이라
    backend 하나일 때와 같은 이름·기본값으로 읽어 모든 backend 에 건다 (P76 F4). `pod` 는 서버가
    한 번 읽은 그 값이고, 없으면 여기서 읽는다. 관리자가 준 CA 도 검증 값에 함께
    든다(`_verify_with_ca`)."""
    from spec2openapi.bridge import BridgeOptions

    pod = pod or _pod_options()
    prefix = f"DAEGAM_BACKEND_{index}_"
    auth = os.environ.get(prefix + "AUTH") or (
        entry.auth_kind if entry.auth_kind != "none" else None
    )
    options = BridgeOptions(
        endpoint=os.environ.get(prefix + "ENDPOINT") or entry.endpoint,
        username=os.environ.get(prefix + "USERNAME") or None,
        password=os.environ.get(prefix + "PASSWORD") or None,
        auth=auth,
        verify=pod.verify,
        trust_env=pod.trust_env,
        soap_headers=_json_object_env(prefix + "SOAP_HEADERS"),
    )
    if entry.timeout_seconds is not None:
        options.timeout = entry.timeout_seconds
    return options


def build_composite_server(
    specs: list[dict[str, Any]], profile: ProfileConfig, options: list[Any] | None = None
) -> FastMCP:
    """backends 형식: backend 별 FastMCP 서버를 만들어 프리픽스 없이 mount 로 합성한다.

    FastMCP 4.0.x 의 mount(server, namespace=None) 는 마운트된 서버의 툴을 원래 이름
    그대로 노출하고 요청을 실시간 전달한다. 이름 유일성은 플랫폼이 저장 시 보장한다."""
    if len(specs) != len(profile.backends):
        raise ProfileError("invalid_profile", "specs and profile.backends length mismatch")
    parent = FastMCP(name="daegam-mcp-runtime")
    # 파드 전체의 전송 설정(TLS 검증·환경 신뢰)은 서버마다 한 번 읽어 모든 backend 에 넘긴다
    pod = None if options else _pod_options()
    unresolved: dict[str, list[str]] = {}
    for i, (spec, entry) in enumerate(zip(specs, profile.backends, strict=True)):
        opts = options[i] if options else _backend_options(i, entry, pod)
        child = _build_backend_server(spec, entry, opts)
        # mount 한 서버의 experimental 은 부모의 initialize 에 실리지 않으므로 부모가 합쳐 싣는다
        unresolved.update(child.experimental_capabilities[ARG_CONDITIONS]["unresolved"])
        parent.mount(child)
    parent.experimental_capabilities[ARG_CONDITIONS] = {
        "version": ARG_CONDITIONS_VERSION,
        "unresolved": dict(sorted(unresolved.items())),
    }
    _add_health_routes(parent)
    return parent


def _add_health_routes(mcp: FastMCP) -> None:
    """ToolHive proxyrunner 가 `GET /` 로 헬스체크를 보낸다. 404 로그 소음을 없애려고
    `/` 와 `/healthz` 에 200 {"status": "ok"} 를 붙인다 (FastMCP custom_route)."""
    from starlette.requests import Request
    from starlette.responses import JSONResponse

    async def health(_: Request) -> JSONResponse:
        return JSONResponse({"status": "ok"})

    for path in ("/", "/healthz"):
        mcp.custom_route(path, methods=["GET"], include_in_schema=False)(health)


def load_spec(path: str | Path) -> dict[str, Any]:
    import yaml

    p = Path(path)
    try:
        data = yaml.safe_load(p.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as e:
        raise ProfileError("invalid_spec", f"cannot load spec {p}: {e}") from e
    if not isinstance(data, dict) or "paths" not in data:
        raise ProfileError("invalid_spec", f"{p} is not an OpenAPI document")
    return data


def load_backend_specs(profile: ProfileConfig, profile_path: str | Path) -> list[dict[str, Any]]:
    """backends 항목의 spec 파일을 profile.json 과 같은 디렉터리에서 읽는다."""
    base = Path(profile_path).parent
    return [load_spec(base / entry.spec) for entry in profile.backends]


def main() -> int:
    logging.basicConfig(level=os.environ.get("RUNTIME_LOG_LEVEL", "INFO"), stream=sys.stderr)
    spec_path = os.environ.get("RUNTIME_SPEC_PATH", "/config/openapi.yaml")
    profile_path = os.environ.get("RUNTIME_PROFILE_PATH", "/config/profile.json")
    host = os.environ.get("RUNTIME_HOST", "0.0.0.0")
    port = int(os.environ.get("RUNTIME_PORT", "8000"))
    path = os.environ.get("RUNTIME_PATH", "/mcp")

    try:
        profile = load_profile(profile_path)
        if profile.backends:
            specs = load_backend_specs(profile, profile_path)
            mcp = build_composite_server(specs, profile)
            log.info(
                "serving %d backends (%s) on http://%s:%d%s",
                len(specs),
                ", ".join(
                    f"{s.get('info', {}).get('title', '?')} tools={entry.tools or 'all'}"
                    for s, entry in zip(specs, profile.backends, strict=True)
                ),
                host,
                port,
                path,
            )
        else:
            spec = load_spec(spec_path)
            mcp = build_server(spec, profile)
            log.info(
                "serving %s (%d operations, tools=%s) on http://%s:%d%s",
                spec.get("info", {}).get("title", "?"),
                len(_operation_ids(spec)),
                profile.tools or "all",
                host,
                port,
                path,
            )
    except ProfileError as e:
        log.error("profile error %s", e)
        return 3
    mcp.run(
        transport="http",
        host=host,
        port=port,
        path=path,
        show_banner=False,
        middleware=http_middleware(),
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
