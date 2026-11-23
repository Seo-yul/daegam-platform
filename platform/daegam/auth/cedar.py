"""vMCP Cedar 인가 정책 (P6 F4a). 테넌트 기본 정책 두 갈래.

1. `permit(principal in THVGroup::"<group>", action, resource);`
   groups claim 이 THVGroup 계층으로 들어가므로 테넌트 Okta 그룹 소속이면 허용한다.
2. `permit(principal, action, resource)
      when { principal has claim_cid && principal.claim_cid == "<cid>" };`
   에이전트 client credentials 토큰에는 groups 가 없고 client id 만 있다. 그 클레임 이름은 플랫폼이
   내는 토큰(local 모드)에서 `cid` 이고, oidc 모드에서는 IdP 마다 다르므로(Keycloak 은 `azp`) 플랫폼
   검증기와 같은 `DAEGAM_OIDC_CLIENT_ID_CLAIM` 이다(`ConsumerAuth.client_id_claim`).
액션은 명시하지 않는다. 실증(docs/archive/reviews/2026-09-02-spike-vmcp-oidc.md 2번) 결과 vMCP 의
액션 어휘도 `Action::"call_tool"` 이고, `tools/call`·`tools/list` 를 액션으로 쓰면 세션이 종료된다.
기본 정책은 전 액션 허용이 맞으므로 생략을 유지한다. 거부되면 tools/list 는 200 + 빈 목록,
tools/call 은 HTTP 403 + JSON-RPC "call denied by authorization policy" 다(ToolHive CallGate,
클라이언트는 예외).
ToolHive 0.51.2 의 Cedar 는 completion/complete·subscriptions/listen 도 대상 프롬프트·리소스의
get_prompt·read_resource 로 인가하고, skills/get 을 get_skill 로 보며, resources/templates/list·
skills/list 응답을 거른다. principal 에는 thv_claim_source 가 붙는다(대감 토큰은 "request").
이 두 정책은 액션도 클레임 출처도 보지 않으므로 누가 허용되는지는 그대로다.

Gateway 표면(P50 13절)에서 끈 기능은 액션을 적은 forbid 로 막는다(`forbid_action_policy`). forbid 는
어느 permit 보다 앞서므로 테넌트 정책과 authz_policy 오버라이드가 무엇이든 그 액션은 거부된다.

ToolHive 는 정책 목록의 원소 하나를 문장 하나로 읽고 그 뒤의 글은 오류 없이 버린다(원소마다
cedar-go `policy.UnmarshalCedar` 한 번, T63). 그래서 사람이 쓴 Cedar 글(authz_policy)은 렌더가 늘
문장마다 원소 하나로 나눈다(`split_statements`). 렌더는 파싱하지
않는다. authz_policy 는 파서에 넘기기 전의 검사(`statement_code`)와 하위 프로세스의
파싱(`services/cedar_check.py`)을 지나야 저장된다.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from typing import Any

_ESCAPE = str.maketrans({"\\": "\\\\", '"': '\\"'})
# 문장을 끝내는 `;` 뒤 같은 줄의 공백. 그 뒤에 줄 주석이 오면 그 문장에 붙인다
_SAME_LINE = re.compile(r"[ \t]*")
# 사람이 쓴 Cedar 글 하나(authz_policy)의 상한(글자). 실제 정책은 문장마다
# 수백 자이고 수십 문장이어도 이 안에 든다. Cedar 엔진은 80KB 가까운 연산자 사슬에서 죽으므로 그
# 아래로 넉넉히 둔다
MAX_POLICY_CHARS = 16 * 1024
# 괄호(`(`·`[`·`{`)가 겹치는 깊이의 상한. Cedar 엔진은 1.4KB 남짓의 겹친 괄호(깊이 700)에서 죽는다.
# 사람이 쓰는 정책은 깊이 열을 넘지 않는다
MAX_BRACKET_DEPTH = 32
# cedar-go 가 문자열과 주석 밖에서 받는 공백. Cedar 엔진은 NBSP·U+3000 같은 공백도 받는다
_SPACES = frozenset(" \t\r\n")
_OPENING = frozenset("([{")
_CLOSING = frozenset(")]}")
# 하위 프로세스(`auth/cedar_worker.py`)가 틀린 문장에 내는 답의 코드
CEDAR_INVALID = "cedar_invalid"
# 주석을 지운 문장에서 forbid 효과를 찾는 글자 검사 (`holds_forbid`)
_FORBID_RE = re.compile(r"\bforbid\s*\(")
_PERMIT_RE = re.compile(r"\bpermit\s*\(")
# Cedar 문자열 글자. 이스케이프한 따옴표를 건너뛴다
_STRING = r'"(?:[^"\\]|\\.)*"'
_STRING_RE = re.compile(rf"({_STRING})")
# THVGroup 엔터티의 이름
_GROUP_NAME_RE = re.compile(rf"THVGroup::({_STRING})")
# 플랫폼이 내는 토큰이 client id 를 싣는 클레임. oidc 모드는 IdP 의 이름을 쓴다(`client_id_policy`)
CLIENT_ID_CLAIM = "cid"
# 정책에 적는 클레임 이름의 마디. Cedar 식별자여야 `principal.claim_<마디>` 로 쓸 수 있다
_CLAIM_SEGMENT = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
# Cedar 의 예약어. 첫 마디는 `claim_` 이 붙어 상관없지만 그 뒤의 마디가 이것이면 cedar-go(v1.8.0)가
# `principal.claim_app.in` 같은 글을 파싱하지 못한다
_CEDAR_RESERVED = frozenset(
    {"true", "false", "if", "then", "else", "in", "like", "has", "is", "__cedar"}
)


class CedarSyntaxError(ValueError):
    """사람이 쓴 Cedar 글이 문장으로 파싱되지 않는다. 메시지에 몇째 문장인지와 파서가 낸 오류가 들어
    있다."""


def _quote(value: str) -> str:
    return '"' + value.translate(_ESCAPE) + '"'


def _group_entity(group: str) -> str:
    return f"THVGroup::{_quote(group)}"


def group_policy(tenant_group: str) -> str:
    return f"permit(principal in {_group_entity(tenant_group)}, action, resource);"


def names_group(policy: str, group: str) -> bool:
    """정책 글이 그 그룹을 THVGroup 으로 적었는가. local 모드의 기동 경고가 Gateway 의 authz_policy
    에서 쓰이지 않는 사용자 정의 그룹을 찾는 데 쓴다 (P32 2절)."""
    return _group_entity(group) in policy


def holds_forbid(policy: str | None) -> bool:
    """정책 글에 forbid 문장이 있는가. 주석 밖의 글에서 `forbid(` 를 찾는 글자 검사이고, 상태 API 가
    턴 위임 토큰(groups 가 없다)을 거절할 수 있는 Gateway 를 늘어놓는 데 쓴다 (P53 3.1절). 그 정책이
    무엇을 거절하는지는 소유자가 읽는다."""
    return any(_FORBID_RE.search(code) for _piece, code in _pieces(policy or "")[0])


def policy_code(texts: Iterable[str | None]) -> list[str]:
    """정책 글들의 문장마다 주석을 공백으로 바꾼 글. 파서를 부르지 않는 글자 검사(`holds_permit`,
    `named_groups`, `reads_claim`, `claim_values`)의 입력이다. 글 하나는 그 안에서만 나눈다."""
    return [code for text in texts for _piece, code in _pieces(text or "")[0]]


def _unquote(literal: str) -> str:
    return literal[1:-1].replace('\\"', '"').replace("\\\\", "\\")


def holds_permit(code: Iterable[str]) -> bool:
    """`policy_code` 의 문장에 permit 이 있는가."""
    return any(_PERMIT_RE.search(c) for c in code)


def named_groups(code: Iterable[str]) -> set[str]:
    """`policy_code` 의 문장이 THVGroup 으로 적은 그룹 이름들."""
    return {_unquote(m.group(1)) for c in code for m in _GROUP_NAME_RE.finditer(c)}


def _claim_expr(name: str) -> str:
    """principal 이 싣는 클레임을 Cedar 에 적은 꼴의 정규식 (`_claim_is`)."""
    head, *rest = claim_path(name)
    return r"\s*\.\s*".join(re.escape(part) for part in [f"claim_{head}", *rest])


def reads_claim(code: Iterable[str], names: Iterable[str]) -> bool:
    """`policy_code` 의 문장이 그 클레임 가운데 하나를 읽는가."""
    exprs = [re.compile(rf"\b{_claim_expr(n)}\b") for n in names]
    return any(e.search(c) for c in code for e in exprs)


def claim_values(code: Iterable[str], name: str) -> set[str]:
    """`policy_code` 의 문장이 그 클레임과 견주는 문자열들. `== "x"`, `.contains("x")`,
    `.containsAny([...])`, `.containsAll([...])` 꼴을 본다."""
    expr = re.compile(
        rf"\b{_claim_expr(name)}\s*(?:==\s*({_STRING})"
        rf"|\.\s*contains(?:Any|All)?\s*\(\s*(\[[^\]]*\]|{_STRING}))"
    )
    return {
        _unquote(literal)
        for c in code
        for m in expr.finditer(c)
        for literal in _STRING_RE.findall(m.group(1) or m.group(2))
    }


def claim_path(name: str) -> tuple[str, ...]:
    """클레임 이름(점 경로면 마디마다)을 Cedar 에 적을 마디로 나눈다. 마디가 Cedar 식별자가 아니거나
    첫 마디 뒤의 마디가 Cedar 예약어면 정책에 적을 수 없으므로 ValueError 다. 설정을 읽을 때 한 번
    보고(`Settings`) 렌더도 이것을 지난다."""
    parts = tuple(name.split("."))
    if not all(_CLAIM_SEGMENT.fullmatch(part) for part in parts) or any(
        part in _CEDAR_RESERVED for part in parts[1:]
    ):
        raise ValueError(
            f"claim name {name!r} must be a Cedar identifier (letters, digits and _, not starting "
            "with a digit), or several joined by dots for a nested claim"
        )
    return parts


def _claim_is(name: str, value: str) -> str:
    """토큰이 클레임 name 을 싣고 그 값이 value 인가. ToolHive 는 클레임을 `claim_<이름>` 으로
    principal 에 싣고 객체 클레임은 레코드가 되며, 클레임은 글이라 문자열로 견준다 (P53 F0 1번).
    점 경로(`a.b`)는 레코드를 따라 내려가고, 마디마다 `has` 로 지킨다. 플랫폼 검증기는 점이 든
    이름을 먼저 맨 위의 키 그대로 찾고 없을 때만 내려가지만, Cedar 는 늘 내려간다. 그래서 점이 든
    키를 맨 위에 그대로 싣는 IdP 에서는 둘이 다른 값을 볼 수 있다."""
    head, *rest = claim_path(name)
    expr = f"principal.claim_{head}"
    guards = [f"principal has claim_{head}"]
    for part in rest:
        guards.append(f"{expr} has {part}")
        expr = f"{expr}.{part}"
    return " && ".join([*guards, f"{expr} == {_quote(value)}"])


def client_id_policy(client_id: str, claim: str = CLIENT_ID_CLAIM) -> str:
    return f"permit(principal, action, resource) when {{ {_claim_is(claim, client_id)} }};"


def tenant_policies(
    tenant_group: str, agent_client_ids: list[str], client_id_claim: str = CLIENT_ID_CLAIM
) -> list[str]:
    """테넌트 그룹 정책 + 에이전트 client id 정책(중복 제거, 순서 유지). client_id_claim 은 그
    토큰이 client id 를 싣는 클레임 이름이다."""
    policies = [group_policy(tenant_group)]
    seen: set[str] = set()
    for cid in agent_client_ids:
        if cid and cid not in seen:
            seen.add(cid)
            policies.append(client_id_policy(cid, client_id_claim))
    return policies


def forbid_action_policy(action: str) -> str:
    """누구에게나 한 액션을 거부한다. 액션 이름은 ToolHive 의 것이다(`read_resource`,
    `get_prompt`)."""
    return f"forbid(principal, action == Action::{_quote(action)}, resource);"


# code mode 를 켠 vMCP 의 힌트 forbid (P56 6.3절 둘째 층). 서버가 `readOnlyHint: true` 를 알리지
# 않는 Tool 은 직접 호출과 스크립트 안의 호출 모두에서 숨고 거절된다. 광고 밖의 이름은 ToolHive 가
# 표시 없이 판정하므로 함께 막힌다. 클레임을 읽지 않고 `has` 뒤에 `==` 만 견주므로 어느 토큰으로도
# 오류 없이 평가된다. 가상 Tool(`execute_tool_script`)은 Cedar 엔티티가 없어 닿지 않는다
READ_ONLY_HINT_FORBID = (
    'forbid(principal, action == Action::"call_tool", resource) '
    "unless { resource has readOnlyHint && resource.readOnlyHint == true };"
)


# 위임 토큰(턴 토큰, P53)이 자기 대상을 적는 값의 꼴이다. 대상 하나에만 묶인다 (P53 1절). 발급
# (`services/turn_grants.py`)과 정책이 이 꼴을 함께 쓴다. MCP Server 의 토큰은 audience 로 묶이므로
# 그 값을 읽는 정책은 아직 없다
GATEWAY_TARGET = "gateway:{}"
MCP_SERVER_TARGET = "mcp-server:{}"
FRONT_TARGET = "expose:{}"


def _bound_to(target: str, tenant: str) -> str:
    """토큰이 이 대상과 이 테넌트에 묶였는가."""
    return f"{_claim_is('bound_to', target)} && {_claim_is('tenant', tenant)}"


def delegation_permit(target: str, tenant: str) -> str:
    """이 대상에 묶인 위임 토큰을 받는다 (P53 3.1절). 위임 토큰에는 groups 도 cid 도 없어
    테넌트의 바탕 permit 이 받지 않으므로 바탕 정책 곁에 둔다."""
    return f"permit(principal, action, resource) when {{ {_bound_to(target, tenant)} }};"


def delegation_forbid(target: str, tenant: str) -> str:
    """이 대상과 테넌트에 묶이지 않은 위임 토큰을 거절한다 (P53 3.1·3.2절). `act` 만 있고
    `bound_to` 가 없는 것도 거절하므로 닫힌 쪽으로 넘어진다. forbid 라 소유자의 어느 permit 이
    받아도 거절된다. 다른 테넌트를 적은 토큰은 소유자의
    permit-all 곁에서 지나가므로(F0 2번) 테넌트도 본다."""
    return (
        "forbid(principal, action, resource) when { "
        "(principal has claim_act || principal has claim_bound_to) && "
        f"!({_bound_to(target, tenant)}) }};"
    )


# Gateway 의 vMCP 가 싣는 서비스 신원의 꼴. 내부 서버의 vMCP 절과 게시된 Gateway 의 소비자 토큰이
# 쓴다
VMCP_SUB_PATTERN = "svc:vmcp:*"
CALL_TOOL_ACTION = 'Action::"call_tool"'


def set_literal(values: Iterable[str]) -> str:
    """문자열의 Cedar 집합 글. 정렬하고 겹친 값을 뺀다."""
    return "[" + ", ".join(_quote(v) for v in sorted(set(values))) + "]"


def _consumer_token(target: str) -> str:
    """이 대상에 묶인 vMCP 의 토큰 (P57 6.1절). `like` 는 늘 문자열인 `claim_sub` 만 읽는다."""
    return (
        f"{_claim_is('bound_to', target)} && principal has claim_sub && "
        f'principal.claim_sub like "{VMCP_SUB_PATTERN}"'
    )


def consumer_forbid(target: str, consumers: Iterable[str]) -> str:
    """게시된 Gateway 가 자기에게 묶인 vMCP 토큰 가운데 지금 소비자(sub 집합) 밖의 것을 거절한다
    (P57 6.2절). 소비자를 떠난 Gateway 나 지운 Gateway 의 토큰 사본은 다음 렌더에 닫힌다. 집합이
    비면(게시를 거둔 뒤 마지막 묶인 토큰이 끝날 때까지) 그런 토큰을 모두 거절한다. F0.2 가
    ToolHive 0.53 에서 쟀다. 소유자 Cedar 에 forbid 를 든 게시된 Gateway 도 빈 집합으로 렌더한다.
    forbid 는 다른 Gateway 를 거쳐 오는 사람을 보지 못하므로 소비자의 호출을 닫는다."""
    names = set(consumers)
    unless = f" unless {{ {set_literal(names)}.contains(principal.claim_sub) }}" if names else ""
    return f"forbid(principal, action, resource) when {{ {_consumer_token(target)} }}{unless};"


def consumer_tools_only(target: str) -> str:
    """게시된 Gateway 가 자기에게 묶인 vMCP 토큰에 Tool 호출 밖의 액션을 모두 거절한다 (P57 6.1절).
    Entry 의 filter 는 Tool 만 가리고 소비자의 울타리도 Tool 만 가두므로, 게시된 Gateway 의
    resources·prompts·completion 은 소비자를 거쳐 나가지 않는다. tools/list 는 Tool 마다 call_tool
    로 판정되므로 그대로다. 소비자 forbid 와 함께, 빈 집합일 때도 렌더한다."""
    return (
        f"forbid(principal, action, resource) when {{ {_consumer_token(target)} }} "
        f"unless {{ action == {CALL_TOOL_ACTION} }};"
    )


def tenant_fence_policies(tenant: str) -> list[str]:
    """내부 MCPServer 의 테넌트 울타리 (P50 11절). 바탕 permit 하나와 `forbid … unless` 하나다.
    그 서버의 테넌트가 낸 서비스 토큰(`sub` 가 `svc:` 로 시작)이나 위임 토큰(`act` 를 실은 것,
    P53)만 지난다. forbid 라 P52 가 바탕 permit 을 규칙으로 바꿔도 울타리로 남는다. 클레임은
    ToolHive 가 `claim_<이름>` 으로 principal 에 싣고, `act` 같은 객체 클레임은 record 다."""
    return [
        "permit(principal, action, resource);",
        "forbid(principal, action, resource) unless { "
        f"{_claim_is('tenant', tenant)} && "
        '((principal has claim_sub && principal.claim_sub like "svc:*") '
        "|| principal has claim_act) };",
    ]


def _line_comment(text: str, i: int, comments: list[tuple[int, int]]) -> int:
    """i 에서 시작하는 줄 주석을 comments 에 적고 그 끝(줄바꿈 앞) 자리를 돌려준다."""
    close = text.find("\n", i)
    end = len(text) if close < 0 else close
    comments.append((i, end))
    return end


def _scan(text: str) -> tuple[list[int], list[tuple[int, int]], str | None]:
    """(문장 끝 자리, 주석 자리, 파서에 넘기지 않을 까닭). 문자열(`\\` 이스케이프, 줄을 넘지
    않는다)과 주석(`//` 줄 주석, `/* */`)은 cedar-go 의 토크나이저와 같게 읽는다. Cedar 문법에서 `;`
    는 문장 끝에만 나오므로 문자열과 주석 밖의 `;` 가 문장을 끝낸다. 끝 자리는 `;` 바로 뒤이고, 같은
    줄에 줄 주석이 이어지면 그 주석의 끝이다. 까닭은 첫 하나만 남긴다. 닫히지 않은 문자열·주석,
    NUL, 문자열과 주석 밖의 다른 공백(cedar-go 가 거절하고 Cedar 엔진은 받는다),
    `MAX_BRACKET_DEPTH` 를 넘는 괄호다."""
    ends: list[int] = []
    comments: list[tuple[int, int]] = []
    problem = "a NUL character" if "\0" in text else None
    depth = 0
    i, n = 0, len(text)
    while i < n:
        ch = text[i]
        if ch == '"':
            i += 1
            while i < n and text[i] not in '"\n':
                i += 2 if text[i] == "\\" else 1
            if i >= n or text[i] == "\n":
                problem = problem or "a string is not closed"
            i += 1
        elif text.startswith("//", i):
            i = _line_comment(text, i, comments)
        elif text.startswith("/*", i):
            close = text.find("*/", i + 2)
            if close < 0:
                problem = problem or "a comment is not closed"
            end = n if close < 0 else close + 2
            comments.append((i, end))
            i = end
        elif ch == ";":
            i = _SAME_LINE.match(text, i + 1).end()
            if text.startswith("//", i):
                i = _line_comment(text, i, comments)
            ends.append(i)
        else:
            if ch.isspace() and ch not in _SPACES:
                problem = problem or f"whitespace U+{ord(ch):04X} outside a string"
            elif ch in _OPENING:
                depth += 1
                if depth > MAX_BRACKET_DEPTH:
                    problem = problem or f"brackets nest deeper than {MAX_BRACKET_DEPTH}"
            elif ch in _CLOSING:
                depth = max(depth - 1, 0)
            i += 1
    return ends, comments, problem


def _pieces(text: str) -> tuple[list[tuple[str, str]], str | None]:
    """([(문장 원문, 주석을 공백으로 바꾼 문장)], 파서에 넘기지 않을 까닭). 주석과 공백, 빈 문장(`;`
    하나)만 있는 조각은 문장이 아니라서 뺀다. `;` 로 끝나지 않은 마지막 조각은 버리지 않고 남겨
    파서가 판정하게 한다."""
    ends, comments, problem = _scan(text)
    blank = list(text)
    for start, end in comments:
        blank[start:end] = " " * (end - start)
    code = "".join(blank)
    out: list[tuple[str, str]] = []
    start = 0
    for end in [*ends, len(text)]:
        body = code[start:end].strip()
        if body.removesuffix(";").strip():
            out.append((text[start:end].strip(), body))
        start = end
    return out, problem


def split_statements(text: str | None) -> list[str]:
    """사람이 쓴 Cedar 글을 문장마다 원소 하나로 나눈다 (T63). 원문 그대로이고 주석도 남는다.
    렌더가 쓰고 파싱하지 않으므로, 저장 검사 전의 글에 틀린 문장이 있으면 그것도 자기 원소가 되고
    오퍼레이터가 그 MCPAuthzConfig 를 거절한다. vMCP 는 앞의 정책으로 돈다."""
    return [piece for piece, _code in _pieces(text or "")[0]]


def statement_code(text: str) -> list[str]:
    """파서에 넘길 문장들. 문장마다 주석을 공백으로 바꾼 글이다. Cedar 엔진이 `/* */` 를 모르기
    때문이다. 넘기기 전에 길이(`MAX_POLICY_CHARS`), 짝 없는 서로게이트와 `_scan` 의 까닭을 보고
    걸리면 CedarSyntaxError 다. 깊이 상한은 괄호(`(`·`[`·`{`)만 센다. 괄호 없는 긴 사슬(`1+1+…`)은
    길이 상한과 하위 프로세스가 맡는다."""
    if len(text) > MAX_POLICY_CHARS:
        raise CedarSyntaxError(f"the text is longer than {MAX_POLICY_CHARS} characters")
    try:
        text.encode()
    except UnicodeEncodeError as e:
        raise CedarSyntaxError("the text holds an unpaired surrogate") from e
    pieces, problem = _pieces(text)
    if problem:
        raise CedarSyntaxError(problem)
    return [code for _piece, code in pieces]


def render_authz_inline(
    policies: list[str], groups_claim: str | None = None, roles_claim: str | None = None
) -> dict[str, Any]:
    """vMCP `incomingAuth.authzConfig` / MCPServer·MCPRemoteProxy `spec.authzConfig` 의 inline
    형태. groups_claim 이 있으면 ToolHive 가 THVGroup 을 그 클레임에서 읽는다(`groupClaimName`).
    roles_claim 이 있으면 그 클레임의 값도 THVGroup 이 된다(`roleClaimName`, P52 3절, F0 2번)."""
    authz: dict[str, Any] = {"type": "inline", "inline": {"policies": list(policies)}}
    if groups_claim:
        authz["groupClaimName"] = groups_claim
    if roles_claim:
        authz["roleClaimName"] = roles_claim
    return authz
