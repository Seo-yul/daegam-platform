"""Gateway 의 code mode 판정 (P56 6.2·6.3절).

code mode 는 Gateway 가 광고하는 Tool 이 모두 읽기일 때만 켠다. 판정은 광고 이름마다의 효과
(`services/tool_effects.py` `gateway_source`)를 필터를 지난 이름으로 읽고, 읽기를 P43 처럼 센다
(`tool_effects.known_read`, 웹 Tool 은 빼고): 효과가 `read` 이고 백엔드 종류를 아는 이름이다. 저장·
미리보기·배포·재배포는 판정이 거절하면 422 이고(6.2절, `GatewayValidator.require_code_mode`), 렌더는
판정이 거절하면 code mode 를 빼고 경고한다(6.3절 셋째 층, `CodeModeState`). 판정의 지문(광고 이름과
그 효과)은 렌더 표시가 읽는다.

거절 순서는 Gateway 의 설정(노출, 소유자 Cedar)을 먼저, 백엔드의 이름(모름, 예약 이름)을 그다음,
효과를 마지막에 본다. 앞의 것은 백엔드를 고쳐도 풀리지 않기 때문이다.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable
from dataclasses import dataclass, field

from daegam.k8s.gates import Version, toolhive_at_least, toolhive_has
from daegam.k8s.templates.vmcp import CODE_MODE_TOOL
from daegam.services.tool_effects import EFFECT_READ, EFFECT_WEB, SourceTools
from daegam.services.tool_rules import coded_warning
from daegam.store.clusters import Cluster

# F0 이 code mode 를 잰 ToolHive 판 (P56 6.2절). 그 아래이거나 모르면 code mode 를 렌더하지 않는다
CODE_MODE_VERSION: Version = (0, 53, 0)

CODE_MODE_NOT_READ_ONLY = "code_mode_not_read_only"
CODE_MODE_UNKNOWN_NAMES = "code_mode_unknown_names"
CODE_MODE_RESERVED_NAME = "code_mode_reserved_name"
CODE_MODE_EXPOSED = "code_mode_exposed"
CODE_MODE_OWNER_CEDAR = "code_mode_owner_cedar"
CODE_MODE_TOOLHIVE_OLD = "code_mode_toolhive_old"

# 판정이 걸은 이름의 까닭 (422 본문의 names 값). 백엔드 종류가 정하는 것은 그 종류이고, 나머지는
# 효과다
CAUSE_WRITE = "write"
CAUSE_WEB = "web"
CAUSE_IMAGE_UNMARKED = "image_unmarked"
# 효과는 읽기이나 백엔드 종류를 모르는 이름 (P43 4절). 게시된 백엔드 아래가 이름을 모를 때다
CAUSE_UNKNOWN_KIND = "unknown_kind"
# 광고 이름이 code mode 의 가상 Tool 과 같다
CAUSE_RESERVED = "reserved"
_KIND_CAUSES = frozenset({"remote", "entry"})

# 렌더가 code mode 를 뺀 까닭 (상태 경고의 `cause:`). 판정의 거절 코드마다 하나이고, 판정은
# 받아들였는데 ToolHive 가 낮거나 모르면 `toolhive` 다. 화면은 tools 와 toolhive 의 문장을 따로
# 든다(13절)
CAUSE_TOOLS = "tools"
CAUSE_TOOLHIVE = "toolhive"
CLOSED_CAUSES = {
    CODE_MODE_NOT_READ_ONLY: CAUSE_TOOLS,
    CODE_MODE_UNKNOWN_NAMES: "unknown_names",
    CODE_MODE_RESERVED_NAME: "reserved_name",
    CODE_MODE_EXPOSED: "exposed",
    CODE_MODE_OWNER_CEDAR: "owner_cedar",
}


@dataclass(frozen=True)
class CodeModeVerdict:
    """판정 하나. code 가 None 이면 code mode 를 켤 수 있다. names 는 걸린 광고 이름마다의
    까닭이고, unknown 은 이름을 모르는 백엔드(허브 항목 이름)다. effects 는 지문의 재료(광고 이름
    마다의 효과)다."""

    code: str | None = None
    detail: str = ""
    names: dict[str, str] = field(default_factory=dict)
    unknown: tuple[str, ...] = ()
    effects: tuple[tuple[str, str], ...] = ()

    @property
    def ok(self) -> bool:
        return self.code is None

    def error(self) -> dict[str, object]:
        """거절의 422 본문 재료. `error_cls(422, **verdict.error())` 처럼 쓴다."""
        if self.code is None:
            raise ValueError("the verdict allows code mode; there is no error to report")
        body: dict[str, object] = {"code": self.code, "detail": self.detail}
        if self.names:
            body["names"] = dict(self.names)
        if self.unknown:
            body["backends"] = list(self.unknown)
        return body

    def fingerprint(self) -> str:
        """판정의 지문. 광고 이름과 효과, 그리고 판정 코드에서 짓는다. 서버나 항목의 표시가 바뀌면
        효과가 바뀌어 지문이 바뀐다(6.3절)."""
        raw = json.dumps({"code": self.code, "effects": list(self.effects)}, sort_keys=True)
        return hashlib.sha256(raw.encode()).hexdigest()[:16]


def cause_of(name: str, tools: SourceTools) -> str:
    """읽기가 아닌 이름의 까닭. 웹 Tool, `remote`·`entry` 항목의 Tool, 관리자가 읽기로 표시하지 않은
    `image` 항목의 Tool, 백엔드 종류를 모르는 읽기, 그 밖의 쓰기(서버 주인의 표시나 메서드)다."""
    effect, origin = tools.effect(name), tools.origins.get(name)
    if effect == EFFECT_WEB:
        return CAUSE_WEB
    if origin in _KIND_CAUSES:
        return origin
    if origin == "image":
        return CAUSE_IMAGE_UNMARKED
    if effect == EFFECT_READ and origin is None:
        return CAUSE_UNKNOWN_KIND
    return CAUSE_WRITE


def reads(name: str, tools: SourceTools) -> bool:
    """판정이 읽기로 세는 이름. 효과가 `read` 이고 백엔드 종류를 안다(`tool_effects.known_read` 에서
    웹 Tool 을 뺀 것)."""
    return tools.effect(name) == EFFECT_READ and tools.origins.get(name) is not None


def code_mode_verdict(
    tools: SourceTools,
    *,
    exposed: bool,
    owner_cedar: bool,
    unknown_backends: Iterable[str] = (),
) -> CodeModeVerdict:
    """code mode 를 켤 수 있는가 (6.2절). tools 는 배포할(또는 배포한) 스냅샷의 광고 이름과
    효과이고, unknown_backends 는 그 스냅샷에서 이름을 모르는 백엔드다."""
    if exposed:
        return CodeModeVerdict(
            CODE_MODE_EXPOSED,
            "code mode cannot be turned on for an exposed Gateway, since every person the exposed "
            "vMCP authenticates could run scripts there",
        )
    if owner_cedar:
        return CodeModeVerdict(
            CODE_MODE_OWNER_CEDAR,
            "this Gateway runs Cedar saved in its overrides, so code mode cannot be turned on",
        )
    unknown = tuple(sorted(set(unknown_backends)))
    if tools.names is None:
        listed = ", ".join(unknown) if unknown else "a backend"
        return CodeModeVerdict(
            CODE_MODE_UNKNOWN_NAMES,
            f"the Tool names of {listed} are unknown, so this Gateway's Tools cannot be checked; "
            "refresh those hub items' Tools or select their Tools",
            unknown=unknown,
        )
    names = tools.names
    effects = tuple((n, tools.effect(n)) for n in names)
    if CODE_MODE_TOOL in names:
        return CodeModeVerdict(
            CODE_MODE_RESERVED_NAME,
            f"this Gateway offers a Tool named {CODE_MODE_TOOL}, which ToolHive refuses to serve "
            "beside code mode's own; select that backend's other Tools or use prefix conflict "
            "resolution",
            names={CODE_MODE_TOOL: CAUSE_RESERVED},
            effects=effects,
        )
    not_read = {n: cause_of(n, tools) for n in names if not reads(n, tools)}
    if not_read:
        return CodeModeVerdict(
            CODE_MODE_NOT_READ_ONLY,
            "code mode needs every Tool this Gateway offers to be read-only; these are not: "
            + ", ".join(f"{n} ({c})" for n, c in sorted(not_read.items())),
            names=not_read,
            effects=effects,
        )
    return CodeModeVerdict(effects=effects)


@dataclass(frozen=True)
class CodeModeState:
    """code mode 를 켠 리비전을 렌더할 때의 판정 (6.3절 셋째 층). 판정이 받아들이고 클러스터의
    ToolHive 가 0.53 이상일 때만 `codeMode` 와 힌트 forbid 를 렌더한다. tools 는 그 판정이 읽은
    광고 이름과 효과다."""

    verdict: CodeModeVerdict
    toolhive: bool
    tools: SourceTools

    @property
    def on(self) -> bool:
        return self.verdict.ok and self.toolhive

    @property
    def cause(self) -> str | None:
        """끈 까닭. 켰으면 None 이다(`CLOSED_CAUSES`, `CAUSE_TOOLHIVE`)."""
        if self.on:
            return None
        if not self.verdict.ok:
            return CLOSED_CAUSES.get(self.verdict.code or "", self.verdict.code)
        return CAUSE_TOOLHIVE

    def mark(self) -> str:
        """렌더 표시(`auth_rev`)에 얹는 값. 서버나 항목의 표시, ToolHive 판이 바뀌면 바뀐다."""
        return f"{self.cause or 'on'}:{self.verdict.fingerprint()}"


# 상태 경고 (6.3절). 화면은 코드로 문장을 그리고 괄호에서 까닭과 이름을 읽는다
CODE_MODE_CLOSED = "code_mode_closed"
CODE_MODE_TOOLS_HIDDEN = "code_mode_tools_hidden"
CODE_MODE_WARNINGS = {
    CODE_MODE_CLOSED: "code mode is off: the definition turns it on, but this render leaves it out",
    CODE_MODE_TOOLS_HIDDEN: (
        "with code mode on, this Gateway hides and refuses Tools whose server does not report "
        "them read-only"
    ),
}


def closed_warning(state: CodeModeState) -> str:
    """`code_mode_closed` 한 줄. 괄호에 까닭을 싣고, 걸린 Tool 이 있으면 그 이름을, 이름을 모르는
    백엔드가 까닭이면 그 백엔드를 싣는다."""
    detail = f"cause: {state.cause}"
    if state.verdict.names:
        detail += "; tools: " + ", ".join(sorted(state.verdict.names))
    elif state.verdict.unknown:
        detail += "; backends: " + ", ".join(state.verdict.unknown)
    return coded_warning(CODE_MODE_CLOSED, CODE_MODE_WARNINGS[CODE_MODE_CLOSED], detail)


def hidden_warning(names: Iterable[str]) -> str:
    """`code_mode_tools_hidden` 한 줄. 괄호에 숨은 이름을 싣는다."""
    detail = ", ".join(sorted(names))
    return coded_warning(CODE_MODE_TOOLS_HIDDEN, CODE_MODE_WARNINGS[CODE_MODE_TOOLS_HIDDEN], detail)


def code_mode_toolhive_ok(cluster: Cluster) -> bool:
    """클러스터의 ToolHive 가 code mode 를 렌더하는 판인가. 판을 모르거나 읽지 못하면 아니다."""
    return toolhive_at_least(cluster, CODE_MODE_VERSION)


def toolhive_refusal(cluster: Cluster) -> dict[str, object] | None:
    """code mode 를 켠 Gateway 를 배포할 수 없는 클러스터면 422 본문 재료다 (6.2절, 배포만).
    배포할 수 있으면 None 이다."""
    if code_mode_toolhive_ok(cluster):
        return None
    has = toolhive_has(cluster)
    return {
        "code": CODE_MODE_TOOLHIVE_OLD,
        "detail": f"code mode needs ToolHive 0.53.0; this cluster has {has}",
        "needed": "ToolHive 0.53.0",
        "found": has,
    }
