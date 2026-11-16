"""비서 도구 실행기 (P14 F1a).

플랫폼 API 도구는 같은 프로세스의 ASGI 앱을 `httpx.ASGITransport` 로 부른다. 앱을 `via_app` 으로
감싸 ASGI scope state 에 `via="assistant/<session_id>"`, `request_id="<턴 request_id>.<n>"`,
`principal` 을 넣으므로 감사 미들웨어는 `via` 를 남기고 `require_user` 는 검증을 건너뛴다. 헤더는
테넌트 하나뿐이고 `Authorization` 은 넣지 않는다(토큰은 넘기지도 저장하지도 않는다, D31). 인가는
라우터의 `require_scope` 가 그대로 하므로 권한 상승이 없다.

4xx·5xx 는 예외가 아니라 `status="error"` 도구 결과다. 모델이 그것을 보고 스스로 고친다.
`raise_app_exceptions=False` 라 라우터의 미처리 예외도 500 결과가 되고, 실행기 자신의 예외(HTTP 가
아닌 것)도 `call` 안에서 잡아 `executor_error` 결과가 된다. 비밀 필드(JSON pointer)에 값이 있으면
API 를 부르지 않고 `secret_in_chat` 을 돌려준다. 결과 본문은 4KB 로 자른다(세션 문서 크기).

클라이언트 전용 도구: `navigate` 는 이벤트만 내고, `read_guide` 는 `knowledge/` 파일을 읽고,
`ask_user` 는 턴 루프가 다루므로 여기서는 실행되지 않는다. `fetch_url` 은 SSRF 검사를 지난 공개
URL 만 가져온다. 그 content-type 문지기와 본문 판정, 세션 파일 이름 짓기는 `fetch.py` 에 있고
`fetch_url` 이 그것을 부른다.

파일은 넷으로 나뉜다. 결과 자르기(`truncate`), 실행기(`ToolExecutor`), 인자 되돌리기·비밀
(`request_of`·`mask_secrets`), 응답 매핑(`result_of`)이다.

가져온 데이터는 모델에 원문으로 주지 않는다(D31). 본문 전체는 세션 `files` 에 `fetched-<n>.<ext>`
로 남기고 모델에는 우리가 만든 값 `{ref, url, content_type, bytes, kind, format, operation_count?}`
만 돌려준다. 남의 문서에 있는 문자열은 title·description·server URL 을 포함해 한 글자도 담지
않으므로 그 안의 지시문이 모델을 움직일 수 없다. `kind`·`format`·`operation_count` 는 `classify`
가 우리 파서로 판정한 값이고 판정에 실패하면 `unknown` 이다. 본문은 `$file:<name>` 참조로만 흐르고
그 참조가 `create_backend_api.spec_text` 나 `verify_spec.text` 로 오면 실행기가 저장된 본문으로
되살린다. 사용자가 채팅에 붙여 넣은 텍스트는 그대로 온다.
"""

from __future__ import annotations

import copy
import json
import re
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal
from urllib.parse import quote

import httpx
from starlette.concurrency import run_in_threadpool
from starlette.types import ASGIApp, Receive, Scope, Send

from daegam import urlfetch
from daegam.auth.principal import Principal
from daegam.config import Settings
from daegam.services.assistant.catalog import RESOURCE_ROUTES, ToolSpec
from daegam.services.assistant.fetch import (
    ContentTypeGate,
    ContentTypeRejected,
    classify,
    file_name_for,
    safe_content_type,
)
from daegam.services.assistant.schema import MULTIPART, ToolDef
from daegam.services.toolnames import TOOL_NAME_RE
from daegam.store.assistant_sessions import FILES_MAX_BYTES, FetchedFile, files_size
from daegam.units import json_bytes

MAX_CONTENT_BYTES = 4096
INTERNAL_BASE_URL = "http://assistant.internal"
KNOWLEDGE_DIR = Path(__file__).parent / "knowledge"
TOPIC_RE = re.compile(r"^[a-z][a-z0-9_-]{0,63}$")
SECRET_MASK = "***"
FILE_REF_PREFIX = "$file:"
# `$file:` 참조를 받는 본문 필드 이름. 스펙 본문이 가는 자리다(`create_backend_api` 의
# `spec_text`, `verify_spec` 의 `text`). 변환 도구는 결과가 남의 문서 원문 그대로라 표에서 빠졌다
FILE_ARGS = frozenset({"spec_text", "text"})
Status = Literal["success", "error"]


@dataclass(frozen=True)
class TurnContext:
    """턴 하나의 신원. 실행기가 이것으로 via·request_id·principal 과 테넌트 헤더를 채운다."""

    principal: Principal
    tenant: str
    session_id: str
    request_id: str
    turn: int


@dataclass
class ToolResult:
    """도구 호출 결과 하나. content 는 4KB 를 넘으면 그 앞부분(JSON 문자열)만 남고 truncated 가
    참이다. `file` 은 `fetch_url` 이 세션 `files` 에 남길 (이름, 본문+출처) 이고 루프가 저장한다."""

    status: Status
    content: Any
    resource: dict[str, Any] | None = None
    event: dict[str, Any] | None = None
    truncated: bool = False
    file: tuple[str, FetchedFile] | None = None
    http_status: int | None = None
    """API 도구가 받은 HTTP 상태. 턴 루프의 401 판정은 content 모양이 아니라 이것을 본다."""
    exempt: bool = False
    """4KB 로 자르지 않고 모델에 통째로 주는 결과(정의 읽기, P48 6.2절). 화면 이벤트와 전사에는 4KB
    로 자른 사본이 간다(`loop.TurnLoop._emit_result`)."""

    @property
    def text(self) -> str:
        """ToolMessage·SSE 에 싣는 문자열."""
        return self.content if isinstance(self.content, str) else dumps(self.content)

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {"status": self.status, "content": self.content}
        if self.truncated:
            d["truncated"] = True
        if self.resource is not None:
            d["resource"] = self.resource
        if self.event is not None:
            d["event"] = self.event
        return d


def dumps(value: Any) -> str:
    """도구 결과·문맥·SSE 에 싣는 JSON 문자열. 한글은 그대로 두고 JSON 이 모르는 형은 str 이다."""
    return json.dumps(value, ensure_ascii=False, default=str)


def error_result(code: str, detail: str, **extra: Any) -> ToolResult:
    return ToolResult("error", {"code": code, "detail": detail, **extra})


# -- 결과 자르기 ---------------------------------------------------------------------------

KEEP_KEYS = ("id", "name", "display_name", "state", "revision", "version", "code", "detail")
"""잘라도 남기는 최상위 키. 스텁의 `$<n>.id` 참조와 모델의 다음 호출이 이것에 기댄다."""

KEEP_LISTS = ("tool_names", "items", "dropped_keys")
"""잘라도 목록째 남기는 키(항목은 줄인다). `verify_spec` 의 `tool_names` 가 사라지면 모델이 MCP
프로필에 넣을 도구를 고를 수 없다."""

SHRINK_STR = 800
SHRINK_ITEMS = 40
"""1 단계에서 남기는 문자열 길이와 목록 항목 수. 설명·오류 문장이 뜻을 잃지 않을 만큼이다."""

KEEP_LIST_STR = 120
KEEP_LIST_ITEMS = 60
"""`KEEP_LISTS` 는 더 짧게, 대신 더 많이 남긴다. 항목이 `tool_names` 같은 짧은 이름이라 60개가
1 단계의 40개보다 작고, 모델은 그중에서 골라야 하므로 길이보다 개수가 중요하다."""

ITEM_STR = 200
"""목록 응답의 항목(dict)에서 남기는 문자열 길이."""

SMALL_STR = 100
SMALL_ITEMS = 10
"""목록 항목이 dict 가 아닐 때와 마지막 손질(dict·list·문자열 어느 것도 아닌 content)."""


def _shrink(value: Any, *, max_str: int = SHRINK_STR, max_items: int = SHRINK_ITEMS) -> Any:
    """긴 문자열과 긴 목록을 줄인다. JSON 모양은 지킨다."""
    if isinstance(value, str):
        return value if len(value) <= max_str else value[:max_str] + "…"
    if isinstance(value, list):
        return [_shrink(v, max_str=max_str, max_items=max_items) for v in value[:max_items]]
    if isinstance(value, dict):
        return {k: _shrink(v, max_str=max_str, max_items=max_items) for k, v in value.items()}
    return value


def _keep_only(value: dict[str, Any], *, scalars: bool = False) -> dict[str, Any]:
    """`KEEP_KEYS` 와 `status.phase` 만(scalars=True 면 짧은 최상위 스칼라도) 남긴다."""
    kept: dict[str, Any] = {}
    for k, v in value.items():
        if isinstance(v, list) and k in KEEP_LISTS:
            kept[k] = _shrink(v, max_str=KEEP_LIST_STR, max_items=KEEP_LIST_ITEMS)
            continue
        if isinstance(v, (dict, list)):
            continue
        if k in KEEP_KEYS or (scalars and (not isinstance(v, str) or len(v) <= 200)):
            kept[k] = v
    status = value.get("status")
    if isinstance(status, dict) and "phase" in status:
        kept["status"] = {"phase": status["phase"]}
    return kept


def truncate(result: ToolResult, max_bytes: int = MAX_CONTENT_BYTES) -> ToolResult:
    """content 가 max_bytes 를 넘으면 단계적으로 줄이고 truncated 를 켠다. JSON 은 깨뜨리지 않고
    상한은 반드시 지킨다.

    dict 는 긴 값·목록 줄이기, 짧은 스칼라와 `KEEP_KEYS` 만, `KEEP_KEYS` 와 `status.phase` 만
    순서다. list 는 항목을 `KEEP_KEYS` 로 줄이고 그래도 크면 개수를 반으로 줄여 간다.
    문자열은 앞부분만 남긴다. 그래서 4KB 를 넘는 인스턴스 상세·목록도 `id`·`name` 은 남아 스텁의
    `$<n>.id` 와 모델의 다음 호출이 이어진다.
    """
    if json_bytes(result.content) <= max_bytes:
        return result
    result.truncated = True
    content = result.content
    if isinstance(content, str):
        result.content = content.encode("utf-8")[:max_bytes].decode("utf-8", errors="ignore")
        return result
    if isinstance(content, dict):
        dropped = sorted(
            k for k, v in content.items() if isinstance(v, (dict, list)) and k not in KEEP_LISTS
        )
        stages = [
            _shrink(content),
            {**_keep_only(content, scalars=True), "dropped_keys": dropped},
            {**_keep_only(content), "dropped_keys": dropped},
            _keep_only(content),
        ]
        for stage in stages:
            if json_bytes(stage) <= max_bytes:
                result.content = stage
                return result
        result.content = {"id": content.get("id"), "truncated_keys": len(content)}
        return result
    if isinstance(content, list):
        items = [
            _shrink(_keep_only(v), max_str=ITEM_STR)
            if isinstance(v, dict)
            else _shrink(v, max_str=SMALL_STR)
            for v in content
        ]
        # 항목 하나가 남았는데도 크면 반감이 제자리라 멈춘다. 그때는 빈 목록으로 떨어진다
        while len(items) > 1 and json_bytes(items) > max_bytes:
            items = items[: len(items) // 2]
        if json_bytes(items) > max_bytes:
            items = []
        result.content = {"items": items, "total": len(content), "truncated_items": True}
        return result
    result.content = _shrink(content, max_str=SMALL_STR, max_items=SMALL_ITEMS)
    return result


# -- 실행기 -------------------------------------------------------------------------------


def via_app(app: ASGIApp, state: Mapping[str, Any]) -> ASGIApp:
    """scope state 에 via·request_id·principal 을 넣고 앱을 부르는 ASGI 래퍼.

    인프로세스 호출만 state 를 채울 수 있으므로 외부 요청은 `via` 를 위조하지 못한다."""

    async def wrapped(scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "http":
            scope.setdefault("state", {}).update(state)
        await app(scope, receive, send)

    return wrapped


def navigate(to: Any) -> ToolResult:
    ok = (
        isinstance(to, str)
        and to.startswith("/")
        and not to.startswith("//")
        and "://" not in to
        and not any(c.isspace() for c in to)
    )
    if not ok:
        return error_result("invalid_path", "to must be an in-app path like /mcp-servers/<id>")
    return ToolResult("success", {"navigated": to}, event={"type": "navigate", "to": to})


class ToolExecutor:
    def __init__(
        self,
        asgi_app: ASGIApp,
        tool_defs: list[ToolDef],
        *,
        settings: Settings,
        resolver: urlfetch.Resolver = urlfetch.system_resolver,
        fetch_transport: Any = None,
        knowledge_dir: Path = KNOWLEDGE_DIR,
    ) -> None:
        """resolver·fetch_transport 는 테스트가 DNS 와 바깥 HTTP 를 가짜로 바꾸는 자리다."""
        self.app = asgi_app
        self.defs = {d.name: d for d in tool_defs}
        self.settings = settings
        self.resolver = resolver
        self.fetch_transport = fetch_transport
        self.knowledge_dir = knowledge_dir

    # -- 진입 ------------------------------------------------------------------------

    async def call(
        self,
        tool_name: str,
        args: Mapping[str, Any] | None,
        *,
        ctx: TurnContext,
        n: int,
        files: Mapping[str, FetchedFile] | None = None,
    ) -> ToolResult:
        """도구 하나를 실행한다. 어떤 실패도 예외가 아니라 결과다. n 은 턴 안의 호출 번호이고
        files 는 세션의 `$file:` 본문이다."""
        tool = self.defs.get(tool_name)
        if tool is None:
            return truncate(error_result("unknown_tool", f"no tool named {tool_name!r}"))
        if args is not None and not isinstance(args, Mapping):
            return truncate(error_result("invalid_arguments", "arguments must be a JSON object"))
        args = dict(args or {})
        files = files or {}
        try:
            if tool.spec.local:
                result = await self._local(tool.spec, args, files)
            else:
                result = await self._api(tool, args, files, ctx=ctx, n=n)
        except Exception as e:  # noqa: BLE001 - 실행기의 버그도 모델에게는 도구 결과다
            result = error_result("executor_error", f"{type(e).__name__}: {e}")
        return truncate(result)

    # -- 플랫폼 API ----------------------------------------------------------------------

    async def _api(
        self,
        tool: ToolDef,
        args: dict[str, Any],
        files: Mapping[str, FetchedFile],
        *,
        ctx: TurnContext,
        n: int,
    ) -> ToolResult:
        hits = secret_hits(tool, args)
        if hits:
            return error_result(
                "secret_in_chat",
                "secrets are not accepted in chat; leave the field empty and let the user "
                "fill it in on the screen",
                fields=hits,
            )
        missing = resolve_file_refs(tool, args, files)
        if missing:
            return error_result(
                "unknown_file",
                f"no fetched file named {missing!r}; call fetch_url first",
                files=sorted(files),
            )
        try:
            method, url, query, request = request_of(tool, args)
        except Refused as e:
            return e.result
        resp = await self.request(method, url, ctx=ctx, n=n, params=query, **request)
        return result_of(tool.spec, resp)

    async def request(
        self, method: str, url: str, *, ctx: TurnContext, n: int | str, **request: Any
    ) -> httpx.Response:
        """플랫폼 앱을 이 턴의 신원으로 부른다. 도구 호출과 비서가 제안을 검사하며 읽는 것이 같은
        길이다(`proposals.py`). via·request_id·principal 은 scope state 로만 간다. n 은 request_id
        의 호출 번호이고 제안의 검사는 `<n>-check` 로 같은 호출의 저장과 가른다."""
        state = {
            "via": f"assistant/{ctx.session_id}",
            "request_id": f"{ctx.request_id}.{n}",
            "principal": ctx.principal,
        }
        transport = httpx.ASGITransport(app=via_app(self.app, state), raise_app_exceptions=False)
        async with httpx.AsyncClient(transport=transport, base_url=INTERNAL_BASE_URL) as client:
            return await client.request(
                method, url, headers={self.settings.tenant_header: ctx.tenant}, **request
            )

    # -- 클라이언트 전용 -------------------------------------------------------------------

    async def _local(
        self, spec: ToolSpec, args: dict[str, Any], files: Mapping[str, FetchedFile]
    ) -> ToolResult:
        if spec.name == "navigate":
            return navigate(args.get("to"))
        if spec.name == "read_guide":
            return self.read_guide(args.get("topic"))
        if spec.name == "ask_user":
            return error_result(
                "ask_user_not_executable",
                "ask_user is answered by the user through the turn loop, not executed",
            )
        if spec.name == "use_tools":
            return error_result(
                "use_tools_not_executable",
                "use_tools changes the loop's tool set and is handled by the turn loop",
            )
        if spec.name == "get_agent_definition":
            return error_result(
                "definition_read_not_executable",
                "get_agent_definition is read by the turn loop under the session's budget",
            )
        if spec.name == "fetch_url":
            return await self.fetch_url(args.get("url"), files)
        return error_result("unknown_tool", f"no local tool named {spec.name!r}")

    def topics(self) -> list[str]:
        if not self.knowledge_dir.is_dir():
            return []
        return sorted(p.stem for p in self.knowledge_dir.glob("*.md"))

    def read_guide(self, topic: Any) -> ToolResult:
        topics = self.topics()
        if not isinstance(topic, str) or not TOPIC_RE.fullmatch(topic) or topic not in topics:
            return error_result("unknown_topic", f"no guide for {topic!r}", topics=topics)
        text = (self.knowledge_dir / f"{topic}.md").read_text(encoding="utf-8")
        return ToolResult("success", {"topic": topic, "text": text})

    async def fetch_url(
        self, url: Any, files: Mapping[str, FetchedFile] | None = None
    ) -> ToolResult:
        """공개 URL 의 본문을 가져와 세션 파일로 남긴다. content-type 은 응답 헤더를 보고 본문을
        읽기 전에 거른다. 모델에게는 본문 대신 우리가 판정한 값만 돌려준다(D31)."""
        files = files or {}
        if not isinstance(url, str) or not url.strip():
            return error_result("missing_argument", "url is required")
        # 이름 풀기와 차단 판정은 fetch_url 이 한 번 한다. 그 BlockedURL 은 아래에서 같은 코드다
        checked = urlfetch.normalize_url(url)
        gate = ContentTypeGate(self.fetch_transport or httpx.AsyncHTTPTransport())
        try:
            body = await urlfetch.fetch_url(
                checked,
                max_bytes=self.settings.max_upload_bytes,
                resolver=self.resolver,
                transport=gate,
            )
        except ContentTypeRejected as e:
            # 거절된 content-type 은 남의 서버가 준 임의 문자열이라 토큰 모양일 때만 싣는다
            return error_result(
                "fetch_content_type",
                f"content-type {safe_content_type(e.content_type or '')} is not text, json, "
                "yaml or xml",
            )
        except (urlfetch.BlockedURL, urlfetch.FetchError) as e:
            # detail 에 리다이렉트 Location 처럼 남의 서버가 정한 문자열이 섞인다. 밖에 낼 문장은
            # urlfetch 가 정한다(`public_detail`). Knowledge 인덱서와 같은 어휘를 쓴다
            return error_result(e.code, urlfetch.public_detail(e), url=checked)
        try:
            text = body.decode("utf-8")
        except UnicodeDecodeError:
            return error_result("fetch_not_text", "body is not UTF-8 text")
        if files_size(files) + len(body) > FILES_MAX_BYTES:
            return error_result(
                "files_full",
                f"session files would exceed {FILES_MAX_BYTES} bytes; start a new session",
                files=sorted(files),
            )
        main = gate.main_type or ""
        name = file_name_for(files, checked, main)
        return ToolResult(
            "success",
            {
                "ref": f"{FILE_REF_PREFIX}{name}",
                "url": checked,
                "content_type": safe_content_type(main),
                "bytes": len(body),
                **await run_in_threadpool(classify, body),
            },
            file=(name, FetchedFile(text=text, url=checked)),
        )


# -- 인자 되돌리기·비밀 ---------------------------------------------------------------------


def resolve_file_refs(
    tool: ToolDef, args: dict[str, Any], files: Mapping[str, FetchedFile]
) -> str | None:
    """`$file:<name>` 참조를 저장된 본문으로 바꾼다(제자리). 없는 이름이면 그 이름을 돌려준다."""
    for arg, prop in tool.body_params.items():
        if prop not in FILE_ARGS:
            continue
        value = args.get(arg)
        if isinstance(value, str) and value.startswith(FILE_REF_PREFIX):
            name = value[len(FILE_REF_PREFIX) :].strip()
            if name not in files:
                return name
            args[arg] = files[name].text
    return None


def file_refs(args: Mapping[str, Any] | None) -> list[str]:
    """인자에 든 `$file:<이름>` 참조의 이름. 루프의 승인 상향이 이것을 본다.

    최상위 문자열 인자만 본다. `resolve_file_refs` 가 되살리는 자리도 최상위뿐이라 여기서 못 보는
    참조는 본문으로 바뀌지도 않는다. 이름이 세션에 있는지는 따지지 않는다. 없는 이름이면 실행기가
    `unknown_file` 로 답할 뿐이고, 규칙을 "참조가 있으면" 하나로 두는 편이 읽기 쉽다."""
    out: list[str] = []
    for value in (args or {}).values():
        if isinstance(value, str) and value.startswith(FILE_REF_PREFIX):
            name = value[len(FILE_REF_PREFIX) :].strip()
            if name:
                out.append(name)
    return out


def secret_paths(tool: ToolDef) -> list[tuple[str, list[str]]]:
    """표의 secret_fields pointer 를 (pointer, 인자 경로) 로. 첫 조각은 본문 필드 이름이라 body_
    접두가 붙은 인자 이름으로 되돌린다."""
    out: list[tuple[str, list[str]]] = []
    for pointer in tool.spec.secret_fields:
        parts = [p.replace("~1", "/").replace("~0", "~") for p in pointer.split("/")[1:]]
        if not parts:
            continue
        arg = next((a for a, prop in tool.body_params.items() if prop == parts[0]), parts[0])
        out.append((pointer, [arg, *parts[1:]]))
    return out


def _walk(value: Any, path: list[str]) -> Any:
    for seg in path:
        if isinstance(value, Mapping):
            value = value.get(seg)
        elif isinstance(value, list) and seg.isdigit() and int(seg) < len(value):
            value = value[int(seg)]
        else:
            return None
    return value


def _present(value: Any) -> bool:
    return value is not None and value != ""


def secret_hits(tool: ToolDef, args: Mapping[str, Any]) -> list[str]:
    """secret_fields 의 JSON pointer 가운데 값이 온 것."""
    return [pointer for pointer, path in secret_paths(tool) if _present(_walk(args, path))]


def mask_secrets(tool: ToolDef, args: Mapping[str, Any]) -> dict[str, Any]:
    """secret_fields 자리에 값이 있으면 `***` 로 바꾼 깊은 사본. 이벤트·전사·문맥·pending 에 넣기
    전에 쓴다. 실행기는 원본으로 판정한다. 비밀 필드가 없는 도구는 얕은 사본이다."""
    paths = [path for _, path in secret_paths(tool) if _present(_walk(args, path))]
    if not paths:
        return dict(args)
    masked: dict[str, Any] = copy.deepcopy(dict(args))
    for path in paths:
        parent: Any = masked
        for seg in path[:-1]:
            parent = parent[int(seg)] if isinstance(parent, list) else parent[seg]
        last = path[-1]
        if isinstance(parent, list):
            parent[int(last)] = SECRET_MASK
        else:
            parent[last] = SECRET_MASK
    return masked


def _multipart(tool: ToolDef, args: Mapping[str, Any]) -> dict[str, Any]:
    """Backend API 생성: spec_text 는 file 파트, 나머지는 Form 이다. filename 은 호출자가
    확인했다."""
    files = {"file": (str(args["filename"]), str(args.get("spec_text") or "").encode("utf-8"))}
    data = {
        prop: str(args[arg])
        for arg, prop in tool.body_params.items()
        if prop not in ("spec_text", "filename") and args.get(arg) is not None
    }
    return {"files": files, "data": data}


class Refused(Exception):
    """API 를 부르지 않거나 그 답으로 멈추고 결과로 답한다. 담은 결과가 모델이 받을 답이다. 인자가
    규칙에 어긋날 때(`request_of`)와 Agent 제안의 검사가 멈출 때(`proposals.py`) 쓴다."""

    def __init__(self, result: ToolResult) -> None:
        super().__init__(result.text)
        self.result = result


def path_segment(name: str, value: Any) -> str:
    """경로 값 하나를 URL 조각으로. 비었으면 missing_argument, `/` 가 들면 invalid_argument 로
    `Refused` 를 올린다.

    `/` 를 막는 이유는 `httpx.ASGITransport` 가 `scope["path"]` 를 디코드하기 때문이다. 값 안의
    `/` 는 인코딩해도 경로 구분자가 되어 표에 없는 라우트(세션 목록·리비전·manifest)에 닿는다.
    id·이름·버전에는 `/` 가 없다."""
    if value is None or value == "":
        raise Refused(error_result("missing_argument", f"{name} is required"))
    if "/" in str(value):
        raise Refused(
            error_result("invalid_argument", f"{name} must not contain '/'", value=str(value))
        )
    return quote(str(value), safe="")


def request_of(
    tool: ToolDef, args: dict[str, Any]
) -> tuple[str, str, dict[str, Any], dict[str, Any]]:
    """API 도구 하나를 요청 재료 `(method, url, query, httpx 인자)` 로 바꾼다.

    표가 정한 자리대로 인자를 경로·쿼리·본문(또는 multipart)에 나눈다. 규칙에 어긋나는 인자는
    `Refused` 로 결과를 올리고 호출자는 API 를 부르지 않는다. 경로 값의 규칙은 `path_segment` 다."""
    method, path = tool.spec.key  # type: ignore[misc]  # API 도구는 None 이 아니다
    path_values = {name: path_segment(name, args.get(name)) for name in tool.path_params}
    query = {
        name: args[name] for name in tool.query_params if name in args and args[name] is not None
    }
    request: dict[str, Any] = {}
    if tool.body_media == MULTIPART:
        if not args.get("filename"):
            raise Refused(error_result("missing_argument", "filename is required"))
        request.update(_multipart(tool, args))
    elif tool.body_media is not None:
        request["json"] = {prop: args[arg] for arg, prop in tool.body_params.items() if arg in args}
    return method, path.format(**path_values), query, request


# -- 응답 매핑 -----------------------------------------------------------------------------


def payload_of(resp: httpx.Response) -> Any:
    """응답 본문. JSON 이면 그 값, 아니면 글이고 비었으면 빈 글이다."""
    if not resp.content:
        return ""
    try:
        return resp.json()
    except ValueError:
        return resp.text


def error_content(status: int, payload: Any) -> dict[str, Any]:
    """플랫폼 오류 본문 `{detail: {code, detail, ...}}` 을 `{status, code, detail, ...}` 로 편다."""
    detail = payload.get("detail") if isinstance(payload, Mapping) else None
    if isinstance(detail, Mapping):
        rest = {k: v for k, v in detail.items() if k not in ("code", "detail")}
        return {
            "status": status,
            "code": str(detail.get("code") or "error"),
            "detail": detail.get("detail", ""),
            **rest,
        }
    if isinstance(detail, list):
        return {"status": status, "code": "validation_error", "detail": detail}
    code = "internal_error" if status >= 500 else "error"
    text = payload if isinstance(payload, str) else dumps(payload)
    return {"status": status, "code": code, "detail": text[:500]}


def resource_of(spec: ToolSpec, payload: Any) -> dict[str, Any] | None:
    """응답에 id 와 name 이 있고 표에 resource_kind 가 있으면 링크 카드용 resource.

    표시 이름이 K8s 이름과 다르면 `display_name` 도 싣는다. 카드는 사람이 붙인 이름을
    앞세운다(D24)."""
    if spec.resource_kind is None or not isinstance(payload, Mapping):
        return None
    rid, name = payload.get("id"), payload.get("name")
    if not isinstance(rid, str) or not isinstance(name, str):
        return None
    route = RESOURCE_ROUTES[spec.resource_kind]
    out = {"kind": spec.resource_kind, "id": rid, "name": name, "url": f"{route}/{rid}"}
    display = payload.get("display_name")
    if isinstance(display, str) and display and display != name:
        out["display_name"] = display
    return out


def sanitize_foreign_strings(payload: Any) -> Any:
    """응답 안에서 남의 문서가 정한 문자열을 걷는다.

    D31 "가져온 데이터는 모델에 원문으로 주지 않는다" 를 결과 쪽에서 지킨다.

    스펙에서 뽑은 도구 이름(`tool_names`)은 `operationId` 그대로라 이름이 아니라 문장일 수
    있고(`pipeline/convert.py` `_tool_name_of`), `tool_details` 는 summary·description 원문이다.
    이름은 MCP 프로필이 실제로 받는 모양(`services/toolnames.TOOL_NAME_RE`)만 남기고 나머지는 수만
    알린다. 걸러진 이름은 MCP 프로필에 넣어도 422 라 모델이 볼 이유가 없다.
    """
    if isinstance(payload, list):
        # 목록 응답(`list_backend_versions` 등)도 같은 필드를 항목마다 갖는다
        return [sanitize_foreign_strings(item) for item in payload]
    if not isinstance(payload, dict):
        return payload
    out = dict(payload)
    items = out.get("items")
    if isinstance(items, list):
        out["items"] = [sanitize_foreign_strings(item) for item in items]
    names = out.get("tool_names")
    if isinstance(names, list):
        kept = [n for n in names if isinstance(n, str) and TOOL_NAME_RE.fullmatch(n)]
        if len(kept) != len(names):
            out["tool_names_dropped"] = len(names) - len(kept)
        out["tool_names"] = kept
    details = out.get("tool_details")
    if isinstance(details, list):
        out.pop("tool_details")
        out["tool_details_dropped"] = len(details)
    return out


def error_of(resp: httpx.Response, payload: Any) -> ToolResult | None:
    """4xx·5xx 답의 오류 결과. 성공이면 None 이다."""
    if resp.status_code < 400:
        return None
    return ToolResult(
        "error", error_content(resp.status_code, payload), http_status=resp.status_code
    )


def result_of(spec: ToolSpec, resp: httpx.Response) -> ToolResult:
    payload = payload_of(resp)
    failed = error_of(resp, payload)
    if failed is not None:
        return failed
    resource = resource_of(spec, payload)
    return ToolResult(
        "success",
        sanitize_foreign_strings(payload),
        resource=resource,
        http_status=resp.status_code,
    )


__all__ = [
    "FILE_ARGS",
    "FILE_REF_PREFIX",
    "INTERNAL_BASE_URL",
    "KEEP_KEYS",
    "KNOWLEDGE_DIR",
    "MAX_CONTENT_BYTES",
    "SECRET_MASK",
    "Refused",
    "ToolExecutor",
    "ToolResult",
    "TurnContext",
    "dumps",
    "error_content",
    "error_of",
    "error_result",
    "file_refs",
    "mask_secrets",
    "navigate",
    "path_segment",
    "payload_of",
    "request_of",
    "resolve_file_refs",
    "resource_of",
    "result_of",
    "sanitize_foreign_strings",
    "secret_hits",
    "secret_paths",
    "truncate",
    "via_app",
]
