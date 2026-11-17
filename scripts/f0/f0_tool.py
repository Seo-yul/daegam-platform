"""P54·P54b F0 의 측정 도구. scripts/f0 의 다른 파일과 함께 F0 측정 동안만 둔다.

플랫폼의 가상 환경에서 돈다:
    uv run --project platform python scripts/f0/f0_tool.py <하위 명령> ...
토큰은 인자가 아니라 환경 F0_TOKEN(허용될 토큰)·F0_REFUSED_TOKEN(거부될 토큰)으로만 받고,
무엇도 토큰을 찍지 않는다. --ip 를 주면 URL 의 호스트 대신 그 IP 에 붙고 Host 머리와 TLS SNI 는
URL 의 호스트다(Traefik 의 LoadBalancer IP 에 못 박으므로 /etc/hosts 가 필요 없다). TLS 인증서는
확인하지 않는다(자체 서명).

  front    --proxy P.json --server S.json [--authz A.json] --timeout SECONDS
           노출 앞단 MCPRemoteProxy(P)·그 MCPAuthzConfig(A)·MCPServer(S)의 산 객체로 P54 5절의
           vMCP 앞단 객체(MCPGroup·MCPServerEntry·header 모드 MCPExternalAuthConfig·
           MCPAuthzConfig·VirtualMCPServer)를 플랫폼의 렌더 함수로 만들어 kubectl apply -f - 가
           읽을 List JSON 으로 찍는다
  probe    --url URL [--ip IP] --out FILE [--rows a,b] [--slow SECONDS]
           F0.1 의 행을 재 FILE 에 JSON 으로 남긴다. 토큰 없는 initialize, PRM 두 경로, 허용
           토큰의 호출과 거부 토큰의 호출, tools/list 전체, 호출 결과와 오류, resources·prompts,
           30초 넘는 호출, ping, 클라이언트가 보내는 알림이다
  compare  A.json B.json --names NAME_A NAME_B
           두 probe 결과를 행마다 견주는 표를 찍는다. 기준(A)이 initialize·tools/list·echo 나
           add 호출에 성공하지 못했으면 BASELINE INVALID 를 찍고 3 으로 끝난다
  derived  --crs FILE --objects FILE
           CR 마다 ToolHive 가 만든 객체(소유자 uid·instance 라벨로 찾는다)와 이름 길이·꼴을
           찍는다. 63자를 넘는 이름과 두 CR 이 같은 이름을 만든 것을 따로 찍는다
  loop     --url URL [--ip IP] --out FILE --stop FILE [--interval SECONDS] [--tool SUFFIX]
           [--args JSON]
           STOP 파일이 생길 때까지 새 세션마다 initialize 와 tools/call 하나를 하고 한 줄씩
           남긴다(F0.2, F1)
  lag      --loop FILE --switch-at EPOCH [--deleted-at EPOCH]
           loop 기록을 바꾼 때 기준으로 줄인다. 옛 앞단의 마지막 답, 새 앞단의 첫 답, 실패한
           호출이다. 바꾸기 전에 된 호출이 없으면 BASELINE INVALID 를 찍고 3 으로 끝난다
  session  --url URL [--ip IP] --ready FILE --go FILE --out FILE [--tool NAME]
           F0.3. MCP Python SDK 의 streamable HTTP 클라이언트로 세션 하나를 열어 부르고 READY
           를 만든 뒤 GO 를 기다려 같은 세션으로 다시 부른다. 본 오류와 SDK 가 스스로 새 세션을
           여는지(HTTP 교환 기록)를 남긴다
  audit    LOG --label NAME
           파드 로그에서 감사 이벤트를 골라 사람·대상·결과를 한 줄씩 찍는다(F0.4)
"""

from __future__ import annotations

import argparse
import asyncio
import http.client
import json
import os
import socket
import ssl
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from urllib.parse import urlsplit

# 이 도구가 initialize 에 싣는 프로토콜 판. 판을 알리는 머리(MCP-Protocol-Version)가 있는 판이다
PROTOCOL = "2025-06-18"
CLIENT_INFO = {"name": "daegam-f0", "version": "0"}
# probe 가 Tool 인자를 채우는 값. 스키마의 properties 에 있는 키만 싣는다
FILL = {"text": "f0-probe", "message": "f0 probe failure", "a": 3, "b": 4}
MISSING_TOOL = "f0_missing_tool"
CELL = 220  # compare 표의 칸에 싣는 글자 수
DNS_LABEL = 63


# ---------- HTTP ----------


@dataclass
class Reply:
    code: int  # 0 은 연결하지 못한 것이다
    headers: dict[str, str] = field(default_factory=dict)
    body: bytes = b""
    seconds: float = 0.0
    error: str | None = None

    def message(self) -> Any:
        """본문이 JSON 이면 그것, SSE 면 마지막 data 줄의 JSON 이다. 없으면 None."""
        text = self.body.decode("utf-8", "replace").strip()
        if not text:
            return None
        if text.startswith(("{", "[")):
            try:
                return json.loads(text)
            except ValueError:
                return None
        out = None
        for line in text.splitlines():
            if line.startswith("data:"):
                try:
                    out = json.loads(line[5:].strip())
                except ValueError:
                    continue
        return out


class _PinnedHTTPS(http.client.HTTPSConnection):
    """IP 에 붙되 SNI 는 호스트 이름으로 보낸다. 인증서는 보지 않는다."""

    def __init__(self, ip: str, port: int, sni: str, timeout: float):
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        super().__init__(ip, port, timeout=timeout, context=ctx)
        self._sni = sni

    def connect(self) -> None:
        sock = socket.create_connection((self.host, self.port), self.timeout)
        self.sock = self._context.wrap_socket(sock, server_hostname=self._sni)


class Target:
    def __init__(self, url: str, ip: str | None = None, timeout: float = 30.0):
        p = urlsplit(url)
        self.url = url
        self.https = p.scheme == "https"
        self.host = p.hostname or ""
        self.port = p.port or (443 if self.https else 80)
        self.netloc = p.netloc
        self.path = p.path or "/"
        self.origin = f"{p.scheme}://{p.netloc}"
        self.ip = ip or self.host
        self.timeout = timeout

    def _conn(self, timeout: float) -> http.client.HTTPConnection:
        if self.https:
            return _PinnedHTTPS(self.ip, self.port, self.host, timeout)
        return http.client.HTTPConnection(self.ip, self.port, timeout=timeout)

    def request(
        self,
        method: str,
        path: str | None = None,
        body: Any = None,
        headers: dict[str, str] | None = None,
        timeout: float | None = None,
        stream: bool = False,
    ) -> Reply:
        """stream 이면 머리만 읽고 닫는다(GET 의 SSE 스트림)."""
        conn = self._conn(timeout or self.timeout)
        sent = {"Host": self.netloc, **(headers or {})}
        data = json.dumps(body).encode() if body is not None else None
        start = time.monotonic()
        try:
            conn.request(method, path or self.path, body=data, headers=sent)
            res = conn.getresponse()
            raw = b"" if stream else res.read()
            heads = {k.lower(): v for k, v in res.getheaders()}
            return Reply(res.status, heads, raw, round(time.monotonic() - start, 3))
        except (OSError, http.client.HTTPException) as e:
            return Reply(0, {}, b"", round(time.monotonic() - start, 3), f"{type(e).__name__}: {e}")
        finally:
            conn.close()


class Session:
    """MCP streamable HTTP 세션 하나를 손으로 민다. 요청마다 새 연결이다."""

    def __init__(self, target: Target, token: str | None):
        self.target = target
        self.token = token
        self.sid: str | None = None
        self.version: str | None = None
        self.next_id = 1

    def headers(self) -> dict[str, str]:
        h = {"Content-Type": "application/json", "Accept": "application/json, text/event-stream"}
        if self.token:
            h["Authorization"] = f"Bearer {self.token}"
        if self.sid:
            h["Mcp-Session-Id"] = self.sid
        if self.version:
            h["MCP-Protocol-Version"] = self.version
        return h

    def initialize(self) -> Reply:
        r = self.request(
            "initialize",
            {"protocolVersion": PROTOCOL, "capabilities": {}, "clientInfo": CLIENT_INFO},
        )
        self.sid = r.headers.get("mcp-session-id")
        result = (r.message() or {}).get("result") if r.code == 200 else None
        if isinstance(result, dict):
            self.version = result.get("protocolVersion")
        return r

    def request(self, method: str, params: Any = None, timeout: float | None = None) -> Reply:
        msg: dict[str, Any] = {"jsonrpc": "2.0", "id": self.next_id, "method": method}
        self.next_id += 1
        if params is not None:
            msg["params"] = params
        return self.target.request("POST", body=msg, headers=self.headers(), timeout=timeout)

    def notify(self, method: str, params: Any = None) -> Reply:
        msg: dict[str, Any] = {"jsonrpc": "2.0", "method": method}
        if params is not None:
            msg["params"] = params
        return self.target.request("POST", body=msg, headers=self.headers())

    def get_stream(self) -> Reply:
        h = {k: v for k, v in self.headers().items() if k != "Content-Type"}
        h["Accept"] = "text/event-stream"
        return self.target.request("GET", headers=h, timeout=5, stream=True)

    def close(self) -> Reply:
        return self.target.request("DELETE", headers=self.headers(), timeout=10)


def outcome(r: Reply, **extra: Any) -> dict[str, Any]:
    """한 요청의 결과 행. JSON-RPC 답이면 result 나 error 를, 그 밖의 JSON(PRM)이면 본문 그대로를,
    JSON 이 아니면 본문의 앞 400자를 싣는다."""
    row: dict[str, Any] = {"code": r.code, **extra}
    if r.error:
        row["transport_error"] = r.error
    msg = r.message()
    if isinstance(msg, dict) and ("result" in msg or "error" in msg):
        for key in ("result", "error"):
            if key in msg:
                row[key] = msg[key]
    elif msg is not None:
        row["body"] = msg
    elif r.body:
        row["body"] = r.body.decode("utf-8", "replace")[:400]
    return row


def tool_named(tools: list[dict[str, Any]], base: str) -> dict[str, Any] | None:
    """이름이 base 이거나 base 로 끝나는 Tool(vMCP 접두를 단 이름). 대소문자를 가리지 않는다(lab 의
    calculator 는 Add, 시험 서버는 add 다)."""
    want = base.lower()
    named = [(str(t.get("name", "")).lower(), t) for t in tools]
    return next((t for n, t in named if n == want), None) or next(
        (t for n, t in named if n.endswith(want)), None
    )


def tool_args(tool: dict[str, Any], slow: float) -> dict[str, Any]:
    props = ((tool.get("inputSchema") or {}).get("properties") or {}).keys()
    values = {**FILL, "seconds": slow}
    return {k: values[k] for k in props if k in values}


def call_row(s: Session, tools: list[dict[str, Any]], base: str, slow: float = 1.0) -> dict:
    tool = tool_named(tools, base)
    if tool is None:
        return {"skipped": f"no tool named *{base}"}
    timeout = slow + 60 if base == "slow" else None
    r = s.request("tools/call", {"name": tool["name"], "arguments": tool_args(tool, slow)}, timeout)
    return outcome(r, tool=tool["name"], seconds=r.seconds)


# ---------- probe / compare ----------

ROWS = (
    "unauthenticated",
    "prm",
    "session",
    "refused",
    "slow",
)


def probe(args: argparse.Namespace) -> int:
    target = Target(args.url, args.ip, timeout=30)
    token = os.environ.get("F0_TOKEN") or None
    refused = os.environ.get("F0_REFUSED_TOKEN") or None
    wanted = set(args.rows.split(",")) if args.rows else set(ROWS)
    rows: dict[str, Any] = {}
    called: dict[str, Any] | None = None

    if "unauthenticated" in wanted:
        r = Session(target, None).initialize()
        rows["initialize without a token"] = {
            "code": r.code,
            "www_authenticate": r.headers.get("www-authenticate"),
        }
    if "prm" in wanted:
        for path in (
            "/.well-known/oauth-protected-resource",
            "/.well-known/oauth-protected-resource/mcp",
        ):
            r = target.request("GET", path=path)
            rows[f"GET {path}"] = outcome(r)
    if "session" in wanted:
        s = Session(target, token)
        r = s.initialize()
        result = (r.message() or {}).get("result") if r.code == 200 else None
        rows["initialize"] = (
            {
                "code": r.code,
                "protocolVersion": result.get("protocolVersion"),
                "serverInfo": result.get("serverInfo"),
                "capabilities": result.get("capabilities"),
                "instructions": result.get("instructions"),
                "session_id": bool(s.sid),
            }
            if isinstance(result, dict)
            else outcome(r)
        )
        rows["notifications/initialized"] = {"code": s.notify("notifications/initialized").code}
        rows["ping"] = outcome(s.request("ping"))
        listed = s.request("tools/list")
        tools = (((listed.message() or {}).get("result") or {}).get("tools")) or []
        rows["tools/list"] = {"code": listed.code, "tools": tools}
        for base in ("echo", "add", "fail"):
            rows[f"tools/call {base}"] = call_row(s, tools, base)
        # 거부될 토큰이 부를 Tool. 허용된 세션이 실제로 본 이름이라 거부와 없는 Tool 을 가른다
        called = (
            tool_named(tools, "echo") or tool_named(tools, "add") or (tools[0] if tools else None)
        )
        rows["tools/call (missing tool)"] = outcome(
            s.request("tools/call", {"name": MISSING_TOOL, "arguments": {}})
        )
        rows["resources/list"] = outcome(s.request("resources/list"))
        rows["resources/templates/list"] = outcome(s.request("resources/templates/list"))
        rows["resources/read f0://note"] = outcome(
            s.request("resources/read", {"uri": "f0://note"})
        )
        rows["prompts/list"] = outcome(s.request("prompts/list"))
        rows["prompts/get greet"] = outcome(
            s.request("prompts/get", {"name": "greet", "arguments": {"name": "f0"}})
        )
        rows["logging/setLevel"] = outcome(s.request("logging/setLevel", {"level": "info"}))
        rows["notifications/cancelled"] = {
            "code": s.notify("notifications/cancelled", {"requestId": 9999, "reason": "f0"}).code
        }
        rows["notifications/roots/list_changed"] = {
            "code": s.notify("notifications/roots/list_changed").code
        }
        stream = s.get_stream()
        rows["GET stream"] = {
            "code": stream.code,
            "content_type": stream.headers.get("content-type"),
            "transport_error": stream.error,
        }
        if "slow" in wanted:
            rows[f"tools/call slow ({args.slow:g}s)"] = call_row(s, tools, "slow", args.slow)
        rows["DELETE session"] = {"code": s.close().code}
    if "refused" in wanted and refused and called is not None:
        s = Session(target, refused)
        r = s.initialize()
        rows["refused: initialize"] = {"code": r.code}
        listed = s.request("tools/list")
        names = [
            t.get("name")
            for t in (((listed.message() or {}).get("result") or {}).get("tools")) or []
        ]
        rows["refused: tools/list"] = {"code": listed.code, "tools": names}
        r = s.request("tools/call", {"name": called["name"], "arguments": tool_args(called, 1.0)})
        rows["refused: tools/call (the allowed session's tool)"] = outcome(r, tool=called["name"])
    elif "refused" in wanted and refused:
        rows["refused: tools/call (the allowed session's tool)"] = {
            "skipped": "the allowed session listed no tool"
        }
    Path(args.out).write_text(json.dumps({"url": args.url, "rows": rows}, indent=1))
    print(f"probe {args.url}: {len(rows)} rows -> {args.out}")
    return 0


def _cell(value: Any) -> str:
    text = json.dumps(value, sort_keys=True, ensure_ascii=False)
    text = text.replace("|", "\\|")
    return text if len(text) <= CELL else text[: CELL - 3] + "..."


def baseline_problems(rows: dict[str, Any]) -> list[str]:
    """기준 쪽(A)이 실제로 답했는지. 기준이 실패한 채 견주면 두 쪽이 같이 실패해도 "같다" 가
    된다."""
    problems = []
    init = rows.get("initialize") or {}
    if init.get("code") != 200:
        problems.append(f"initialize -> {init.get('code')}")
    listed = rows.get("tools/list") or {}
    if listed.get("code") != 200 or not listed.get("tools"):
        problems.append(f"tools/list -> {listed.get('code')} with no tools")
    calls = [
        v
        for k, v in rows.items()
        if k in ("tools/call echo", "tools/call add") and isinstance(v, dict) and "skipped" not in v
    ]
    if not any(
        c.get("code") == 200
        and isinstance(c.get("result"), dict)
        and not c["result"].get("isError")
        for c in calls
    ):
        problems.append("no tools/call of echo or add succeeded")
    return problems


def compare(args: argparse.Namespace) -> int:
    """표를 찍는다. 기준(A)이 답하지 않았으면 그것을 먼저 찍고 3 으로 끝난다."""
    a = json.loads(Path(args.a).read_text())["rows"]
    b = json.loads(Path(args.b).read_text())["rows"]
    name_a, name_b = args.names
    problems = baseline_problems(a) if "initialize" in a else []
    if problems:
        print(f"BASELINE INVALID ({name_a}): {'; '.join(problems)}\n")
    order = list(a) + [k for k in b if k not in a]
    print(f"| row | same | {name_a} | {name_b} |")
    print("|---|---|---|---|")
    differing = []
    for row in order:
        va, vb = a.get(row), b.get(row)
        same = _strip_timing(va) == _strip_timing(vb)
        if not same:
            differing.append(row)
        print(f"| {row} | {'yes' if same else 'NO'} | {_cell(va)} | {_cell(vb)} |")
    for row in differing:
        print(
            f"\n### {row}\n{name_a}: {json.dumps(a.get(row), sort_keys=True, ensure_ascii=False)}"
        )
        print(f"{name_b}: {json.dumps(b.get(row), sort_keys=True, ensure_ascii=False)}")
    print(f"\n{len(differing)} of {len(order)} rows differ: {', '.join(differing) or '-'}")
    return 3 if problems else 0


def _strip_timing(value: Any) -> Any:
    if isinstance(value, dict):
        return {k: _strip_timing(v) for k, v in value.items() if k != "seconds"}
    if isinstance(value, list):
        return [_strip_timing(v) for v in value]
    return value


# ---------- front ----------


def _docs(path: str) -> list[dict[str, Any]]:
    """JSON 하나나 이어 붙인 JSON 여럿(kubectl get -o json 을 이어 쓴 파일)을 읽는다. List 는
    풀어 낸다."""
    text = Path(path).read_text()
    dec = json.JSONDecoder()
    out: list[dict[str, Any]] = []
    i = 0
    while True:
        while i < len(text) and text[i].isspace():
            i += 1
        if i >= len(text):
            break
        obj, i = dec.raw_decode(text, i)
        is_list = str(obj.get("kind", "")).endswith("List")
        out.extend((obj.get("items") or []) if is_list else [obj])
    return out


def front(args: argparse.Namespace) -> int:
    # 플랫폼의 렌더 함수다. 이 명령만 플랫폼 패키지를 읽는다
    from daegam.k8s.labels import (
        LABEL_ID,
        LABEL_PROFILE_REVISION,
        LABEL_RESOURCE,
        LABEL_TENANT,
        ensure_labels,
    )
    from daegam.k8s.templates.common import TOOLHIVE_API, owner_ref
    from daegam.k8s.templates.svc_token import (
        SVC_TOKEN_REV_ANNOTATION,
        render_external_auth_config,
    )
    from daegam.k8s.templates.vmcp import (
        LABEL_GATEWAY_PROFILE,
        anonymous_incoming_auth,
        render_entry,
        render_group,
        render_vmcp,
        vmcp_incoming_auth,
    )
    from daegam.store.clusters import ClusterScheduling

    proxy = _docs(args.proxy)[0]
    server = _docs(args.server)[0]
    authz = _docs(args.authz)[0] if args.authz else None
    meta = server["metadata"]
    labels = meta.get("labels") or {}
    tenant, sid = labels[LABEL_TENANT], labels[LABEL_ID]
    ns = meta["namespace"]
    k8s = meta["name"]
    name = f"front-{k8s}"
    resource = "mcp-instance-front"  # P54 5절의 리소스 값
    owner = owner_ref("MCPServer", k8s, meta["uid"])
    spec = proxy["spec"]
    secret = None
    if spec.get("externalAuthConfigRef"):
        # 앞단의 bearer 설정이 가리키는 Secret(svc-expose-<k8s.name>)을 그대로 쓴다
        secret = spec["externalAuthConfigRef"]["name"]
    docs: list[dict[str, Any]] = [
        render_group(
            tenant=tenant,
            instance_id=sid,
            name=name,
            namespace=ns,
            description=f"vMCP front of {k8s} (P54 F0)",
            owner=owner,
        )
    ]
    entry = render_entry(
        tenant=tenant,
        instance_id=sid,
        name=name,
        namespace=ns,
        group=name,
        url=spec["remoteUrl"],
        transport=spec.get("transport") or "streamable-http",
        hub_item_id="",
        owner=owner,
        external_auth_config=name if secret else None,
    )
    entry["metadata"]["labels"].pop("daegam.devops.ai.kr/hub-item", None)
    docs.append(entry)
    if secret:
        docs.append(
            render_external_auth_config(
                name=name,
                namespace=ns,
                secret_name=secret,
                mode="header",
                tenant=tenant,
                resource=resource,
                id=sid,
                owner=owner,
            )
        )
    authz_ref = None
    if authz is not None:
        authz_ref = {"name": f"{name}-authz"}
        docs.append(
            {
                "apiVersion": authz.get("apiVersion", TOOLHIVE_API),
                "kind": "MCPAuthzConfig",
                "metadata": {
                    "name": authz_ref["name"],
                    "namespace": ns,
                    "ownerReferences": [owner],
                },
                "spec": authz["spec"],
            }
        )
    if spec.get("oidcConfigRef"):
        incoming = vmcp_incoming_auth(
            oidc_ref=dict(spec["oidcConfigRef"]),
            policies=[],
            groups_claim=None,
            authz_ref=authz_ref,
        )
    else:
        incoming = anonymous_incoming_auth(authz_ref=authz_ref)
    if authz_ref is None and spec.get("authzConfig"):
        # 참조가 없는 앞단(G8 게이트 밖)의 인라인 Cedar 를 그대로 옮긴다
        incoming["authzConfig"] = spec["authzConfig"]
    rev = (spec.get("podTemplateSpec") or {}).get("metadata", {}).get("annotations", {})
    placement = SimpleNamespace(
        images=SimpleNamespace(pull_secret=None), scheduling=ClusterScheduling()
    )
    vmcp = render_vmcp(
        tenant=tenant,
        instance_id=sid,
        profile_id=sid,
        revision=int(labels.get(LABEL_PROFILE_REVISION) or 1),
        name=name,
        namespace=ns,
        group=name,
        backends=[{"workload": name}],
        failure_mode="best_effort",
        effective=placement,  # type: ignore[arg-type]  # pod_placement 가 읽는 두 칸만 있다
        conflict="priority",
        incoming_auth=incoming,
        pod_annotations={SVC_TOKEN_REV_ANNOTATION: rev[SVC_TOKEN_REV_ANNOTATION]}
        if SVC_TOKEN_REV_ANNOTATION in rev
        else None,
        audit=True,
        timeouts={name: f"{args.timeout + 5}s"},
        static_backends={name: name} if secret else {},
    )
    for where in (vmcp["metadata"], vmcp["spec"]["podTemplateSpec"]["metadata"]):
        where["labels"][LABEL_RESOURCE] = resource
        where["labels"].pop(LABEL_GATEWAY_PROFILE, None)
    vmcp["metadata"]["ownerReferences"] = [owner]
    docs.append(vmcp)
    # 다섯 객체 모두 S 의 id 와 앞단의 리소스 값을 단다 (P54 5절)
    for doc in docs:
        ensure_labels(doc, tenant=tenant, resource=resource, id=sid)
    print(json.dumps({"apiVersion": "v1", "kind": "List", "items": docs}, indent=1))
    return 0


# ---------- derived ----------

TOOLHIVE_LABELS = ("app.kubernetes.io/instance", "toolhive-name")


def derived(args: argparse.Namespace) -> int:
    crs = _docs(args.crs)
    objects = _docs(args.objects)
    print("| CR | derived kind | derived name | length | pattern |")
    print("|---|---|---|---|---|")
    seen: dict[tuple[str, str, str], str] = {}
    long_names: list[str] = []
    clashes: list[str] = []
    for cr in crs:
        cm = cr["metadata"]
        cr_id = f"{cr['kind']} {cm['name']} ({len(cm['name'])})"
        found = []
        for o in objects:
            om = o["metadata"]
            if om.get("namespace") != cm.get("namespace"):
                continue
            owned = any(ref.get("uid") == cm["uid"] for ref in om.get("ownerReferences") or [])
            labelled = any((om.get("labels") or {}).get(k) == cm["name"] for k in TOOLHIVE_LABELS)
            if owned or labelled:
                found.append(o)
        if not found:
            print(f"| {cr_id} | - | (none found) | - | - |")
        for o in sorted(found, key=lambda o: (o["kind"], o["metadata"]["name"])):
            oname = o["metadata"]["name"]
            pattern = oname.replace(cm["name"], "{cr}")
            print(f"| {cr_id} | {o['kind']} | {oname} | {len(oname)} | {pattern} |")
            key = (o["kind"], cm["namespace"], oname)
            if key in seen and seen[key] != cm["name"]:
                clashes.append(f"{o['kind']} {oname} from {seen[key]} and {cm['name']}")
            seen.setdefault(key, cm["name"])
            if len(oname) > DNS_LABEL:
                long_names.append(f"{o['kind']} {oname} ({len(oname)})")
    print(f"\nnames over {DNS_LABEL} characters: {'; '.join(long_names) or 'none'}")
    print(f"names two CRs derive alike: {'; '.join(clashes) or 'none'}")
    return 0


# ---------- loop / lag ----------


def loop(args: argparse.Namespace) -> int:
    target = Target(args.url, args.ip, timeout=10)
    token = os.environ.get("F0_TOKEN") or None
    stop = Path(args.stop)
    with open(args.out, "a") as out:
        while not stop.exists():
            started = time.time()
            s = Session(target, token)
            r = s.initialize()
            result = (r.message() or {}).get("result") if r.code == 200 else None
            rec: dict[str, Any] = {
                "t": round(started, 3),
                "init": r.code,
                "server": ((result or {}).get("serverInfo") or {}).get("name"),
            }
            if r.error:
                rec["err"] = r.error
            if r.code == 200:
                s.notify("notifications/initialized")
                # vMCP 는 접두를 단 이름일 수 있어 세션마다 목록에서 이름을 찾는다
                listed = s.request("tools/list")
                tools = ((listed.message() or {}).get("result") or {}).get("tools") or []
                tool = tool_named(tools, args.tool) or {"name": args.tool}
                c = s.request(
                    "tools/call", {"name": tool["name"], "arguments": json.loads(args.args)}
                )
                msg = c.message() or {}
                rec["call"] = c.code
                rec["ok"] = (
                    c.code == 200
                    and "error" not in msg
                    and not (msg.get("result") or {}).get("isError")
                )
                if not rec["ok"]:
                    rec["detail"] = (c.error or c.body.decode("utf-8", "replace"))[:200]
                s.close()
            else:
                rec["ok"] = False
            out.write(json.dumps(rec) + "\n")
            out.flush()
            time.sleep(max(0.0, args.interval - (time.time() - started)))
    return 0


def _no_answer(code: int | None) -> bool:
    """답을 받지 못했거나(0, 없음) 5xx 를 받았는가."""
    return not code or code >= 500


def _cut_at_switch(
    r: dict[str, Any], in_flight: dict[str, Any] | None, switch: float, old: set[str]
) -> bool:
    """실패한 기록 r 이 넘김이 끊은 호출인가. 기준은 lag 의 주석에 있다."""
    if r["init"] != 200:
        return r is in_flight and _no_answer(r["init"])
    if r.get("call") == 404:
        return False
    if r is in_flight:
        return _no_answer(r.get("call"))
    return r["t"] >= switch and r.get("server") in old and _no_answer(r.get("call"))


def lag(args: argparse.Namespace) -> int:
    recs = sorted(
        (json.loads(line) for line in Path(args.loop).read_text().splitlines() if line.strip()),
        key=lambda r: r["t"],
    )
    switch = args.switch_at
    before = [r for r in recs if r["t"] < switch]
    after = [r for r in recs if r["t"] >= switch]
    old = {r["server"] for r in before if r.get("server")}
    print(f"calls: {len(recs)} ({len(before)} before the switch, {len(after)} after)")
    print(f"answered before the switch by: {sorted(old) or '-'}")
    names: dict[str, int] = {}
    for r in after:
        key = f"{r.get('server')} init={r['init']} call={r.get('call')} ok={r['ok']}"
        names[key] = names.get(key, 0) + 1
    for key, n in sorted(names.items()):
        print(f"  after: {n} x {key}")
    old_after = [r["t"] for r in after if r.get("server") in old]
    new_after = [r["t"] for r in after if r.get("server") and r["server"] not in old]
    if not old:
        # 바꾸기 전에 답한 앞단이 없으면 옛 앞단과 새 앞단을 가를 수 없다
        print("old and new front: not told apart (no answer before the switch)")
    elif old_after:
        print(f"last answer from the old front: +{max(old_after) - switch:.2f}s after the switch")
    else:
        print("last answer from the old front: none after the switch")
    if old and new_after:
        print(f"first answer from the new front: +{min(new_after) - switch:.2f}s after the switch")
    elif old:
        print("first answer from the new front: none")
    failed = [r for r in recs if not r["ok"]]
    print(f"failed calls: {len(failed)}")
    baseline_ok = any(r["ok"] for r in before)
    # 옛 앞단에서 연 세션의 호출이 바꾼 뒤 실패한 것은 세션이 바꾸기에 걸친 것이다. 끊긴 호출과
    # 따로 센다
    straddled = [
        r for r in failed if r["t"] >= switch and r["init"] == 200 and r.get("server") in old
    ]
    print(f"  of which the session opened on the old front and its call failed: {len(straddled)}")
    # 막대(P54 12절 F0.2)가 보는 수다. 바꾼 뒤에 시작해 새 앞단에서 열린 세션의 실패만 센다. 바꾸기
    # 전의 실패와 바꾸기에 걸친 세션(F0.3 의 주제, 릴리스 노트)은 넘김이 끊은 호출이 아니다
    dropped = [r for r in failed if r["t"] >= switch and r not in straddled]
    print(f"dropped calls after the switch: {len(dropped)}")
    # 넘김이 끊은 호출도 센다. 루프는 세션을 하나씩 열고 switch_at 은 고치기 전에
    # 읽으므로, 바꾸던 때 돌던 세션은 switch_at 앞의 마지막 기록이다. 그 세션의
    # initialize 나 호출이 답을 받지 못했거나(0) 5xx 를 받았으면 끊긴 것이다. 바꾼
    # 뒤에 옛 앞단에서 열린 세션의 호출도 같다. 새 앞단이 그 세션을 모르는 404 는
    # 설계대로라(F0.3) 세지 않는다
    in_flight = before[-1] if before else None
    cut = [r for r in failed if _cut_at_switch(r, in_flight, switch, old)]
    print(f"calls cut at the switch: {len(cut)}")
    for r in failed:
        print(
            f"  {r['t'] - switch:+.2f}s init={r['init']} server={r.get('server')} "
            f"call={r.get('call')} {r.get('err') or r.get('detail') or ''}"
        )
    late_failed = [r["t"] - switch for r in failed if r["t"] >= switch]
    if late_failed:
        # 되돌린 설치에서 503 이 이어진 때처럼 실패가 몰린 창이다
        print(
            f"failure window after the switch: +{min(late_failed):.1f}s "
            f"to +{max(late_failed):.1f}s ({len(late_failed)} calls)"
        )
    if args.deleted_at:
        late = [r for r in recs if r["t"] >= args.deleted_at and not r["ok"]]
        since = args.deleted_at - switch
        print(f"failed calls after the old front was deleted (+{since:.1f}s): {len(late)}")
    if not baseline_ok:
        # 바꾸기 전에 한 번도 되지 않았으면 그 뒤의 실패는 바꾼 탓인지 알 수 없다
        print("BASELINE INVALID: no call succeeded before the switch")
        return 3
    return 0


# ---------- session (F0.3) ----------


def session(args: argparse.Namespace) -> int:
    return asyncio.run(_session(args))


async def _session(args: argparse.Namespace) -> int:
    import httpx2
    from mcp import ClientSession
    from mcp.client.streamable_http import streamable_http_client

    target = Target(args.url, args.ip)
    url = f"{'https' if target.https else 'http'}://{target.ip}:{target.port}{target.path}"
    exchanges: list[dict[str, Any]] = []
    steps: list[dict[str, Any]] = []
    started = time.time()

    async def on_response(response: Any) -> None:
        req = response.request
        method = None
        try:
            body = json.loads(req.content or b"null")
            method = body.get("method") if isinstance(body, dict) else None
        except (ValueError, RuntimeError):
            pass
        exchanges.append(
            {
                "t": round(time.time() - started, 2),
                "http": req.method,
                "rpc": method,
                "sent_session": req.headers.get("mcp-session-id") is not None,
                "status": response.status_code,
                "got_session": response.headers.get("mcp-session-id") is not None,
            }
        )

    def client() -> Any:
        headers = {"Host": target.netloc}
        token = os.environ.get("F0_TOKEN")
        if token:
            headers["Authorization"] = f"Bearer {token}"
        return httpx2.AsyncClient(
            headers=headers,
            timeout=httpx2.Timeout(30.0),
            verify=False,
            event_hooks={"response": [on_response]},
        )

    async def call(s: Any, label: str) -> None:
        t = time.time()
        try:
            res = await asyncio.wait_for(
                s.call_tool(args.tool, {"text": f"f0-session {label}"}), timeout=45
            )
            is_error = bool(getattr(res, "isError", getattr(res, "is_error", False)))
            texts = [getattr(c, "text", None) for c in res.content]
            steps.append({"step": label, "ok": not is_error, "content": texts})
        except BaseException as e:  # noqa: BLE001 - 무엇이 올라왔는지가 측정값이다
            steps.append({"step": label, "ok": False, "error": f"{type(e).__name__}: {e}"[:400]})
        steps[-1]["seconds"] = round(time.time() - t, 2)

    try:
        async with streamable_http_client(url, http_client=client()) as streams:
            async with ClientSession(streams[0], streams[1]) as s:
                init = await asyncio.wait_for(s.initialize(), timeout=30)
                info = getattr(init, "serverInfo", None) or getattr(init, "server_info", None)
                steps.append({"step": "initialize", "server": getattr(info, "name", None)})
                await call(s, "call before the switch")
                Path(args.ready).write_text("ready\n")
                waited = 0.0
                while not Path(args.go).exists() and waited < 900:
                    await asyncio.sleep(0.5)
                    waited += 0.5
                for i in (1, 2, 3):
                    await call(s, f"call {i} after the switch, same session")
    except BaseException as e:  # noqa: BLE001 - 세션이 닫히며 올린 것도 기록한다
        steps.append({"step": "session closed with", "error": f"{type(e).__name__}: {e}"[:400]})
    Path(args.ready).write_text("ready\n")
    try:
        async with streamable_http_client(url, http_client=client()) as streams:
            async with ClientSession(streams[0], streams[1]) as s:
                init = await asyncio.wait_for(s.initialize(), timeout=30)
                info = getattr(init, "serverInfo", None) or getattr(init, "server_info", None)
                steps.append(
                    {"step": "new session: initialize", "server": getattr(info, "name", None)}
                )
                await call(s, "new session: call")
    except BaseException as e:  # noqa: BLE001
        steps.append({"step": "new session failed", "error": f"{type(e).__name__}: {e}"[:400]})
    out = {"url": args.url, "steps": steps, "http": exchanges}
    Path(args.out).write_text(json.dumps(out, indent=1))
    for st in steps:
        print(json.dumps(st, ensure_ascii=False))
    print("http exchanges (t, method, rpc, sent session id, status, got session id):")
    for ex in exchanges:
        print(
            f"  {ex['t']:>7} {ex['http']:<6} {ex['rpc'] or '-':<28} sent={ex['sent_session']} "
            f"{ex['status']} got={ex['got_session']}"
        )
    return 0


# ---------- audit ----------

AUDIT_KEYS = ("logged_at", "type", "outcome", "component")


def audit(args: argparse.Namespace) -> int:
    n = 0
    for line in Path(args.log).read_text(errors="replace").splitlines():
        if '"audit_id"' not in line and '"auditId"' not in line:
            continue
        start = line.find("{")
        try:
            d = json.loads(line[start:])
        except ValueError:
            continue
        row = {k: d.get(k) for k in AUDIT_KEYS}
        subjects = d.get("subjects") or {}
        row["subjects"] = {
            k: subjects.get(k) for k in ("user", "user_id", "client_name") if k in subjects
        }
        row["target"] = d.get("target")
        extra = (d.get("metadata") or {}).get("extra") or {}
        row["extra"] = {
            k: v for k, v in extra.items() if k not in ("request_data", "response_data")
        }
        if d.get("delegation"):
            row["delegation"] = d["delegation"]
        print(f"{args.label}: {json.dumps(row, sort_keys=True, ensure_ascii=False)}")
        n += 1
    print(f"{args.label}: {n} audit events")
    return 0


# ---------- main ----------


def main(argv: list[str]) -> int:
    p = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    sub = p.add_subparsers(dest="cmd", required=True)

    f = sub.add_parser("front")
    f.add_argument("--proxy", required=True)
    f.add_argument("--server", required=True)
    f.add_argument("--authz")
    f.add_argument("--timeout", type=int, required=True)
    f.set_defaults(fn=front)

    pr = sub.add_parser("probe")
    pr.add_argument("--url", required=True)
    pr.add_argument("--ip")
    pr.add_argument("--out", required=True)
    pr.add_argument("--rows", default="")
    pr.add_argument("--slow", type=float, default=35.0)
    pr.set_defaults(fn=probe)

    c = sub.add_parser("compare")
    c.add_argument("a")
    c.add_argument("b")
    c.add_argument("--names", nargs=2, default=["a", "b"])
    c.set_defaults(fn=compare)

    d = sub.add_parser("derived")
    d.add_argument("--crs", required=True)
    d.add_argument("--objects", required=True)
    d.set_defaults(fn=derived)

    lp = sub.add_parser("loop")
    lp.add_argument("--url", required=True)
    lp.add_argument("--ip")
    lp.add_argument("--out", required=True)
    lp.add_argument("--stop", required=True)
    lp.add_argument("--interval", type=float, default=0.2)
    lp.add_argument("--tool", default="echo")
    lp.add_argument("--args", default='{"text": "f0-loop"}')
    lp.set_defaults(fn=loop)

    lg = sub.add_parser("lag")
    lg.add_argument("--loop", required=True)
    lg.add_argument("--switch-at", type=float, required=True)
    lg.add_argument("--deleted-at", type=float)
    lg.set_defaults(fn=lag)

    se = sub.add_parser("session")
    se.add_argument("--url", required=True)
    se.add_argument("--ip")
    se.add_argument("--ready", required=True)
    se.add_argument("--go", required=True)
    se.add_argument("--out", required=True)
    se.add_argument("--tool", default="echo")
    se.set_defaults(fn=session)

    au = sub.add_parser("audit")
    au.add_argument("log")
    au.add_argument("--label", required=True)
    au.set_defaults(fn=audit)

    args = p.parse_args(argv[1:])
    return int(args.fn(args) or 0)


if __name__ == "__main__":
    sys.exit(main(sys.argv))
