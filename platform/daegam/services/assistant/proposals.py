"""청지기의 Agent 정의 제안 (P48 6.1·6.2·6.6절).

`create_agent` 와 `update_agent` 는 제안 도구다. 루프가 부르면 바로 저장하지 않고 여기서 검사한 뒤
사람의 결정을 기다린다. 검사와 저장은 도구 호출과 같은 길(`ToolExecutor.request`)로 플랫폼 앱을 턴의
신원으로 부르므로 청지기는 사람보다 더 하지 못한다. 짜 맞추기·비교·먼저 볼 것은
`services/agent_changes.py` 의 순수 함수다.

- **검사(`Proposals.check`).** 한 구간의 한도, 인자 크기(64 KiB), 바꾸기의 대상 읽기(404·403 은 도구
  결과), 최신 리비전 위의 짜 맞추기, `POST /api/agents/validate`(agent:write 가 없으면 403 이 도구
  결과다), 그리고 넘지 못하는 선이다. 넘으면 사람 표시가 있을 때 `person_marks_only`, 아니면
  `proposal_invalid` 한 결과에 문제와 표시의 자리를 4KB 안에 맞춰 싣는다. 지나면 카드에 그릴 제안
  덩어리와 예약 칸의 값을 돌려준다.
- **예약 칸(`PROPOSAL_KEY`).** 기다리는 호출의 저장된 인자에 루프가 넣는 칸이다. 제안 id, 지문,
  기준 리비전, 카드가 센 먼저 볼 것의 열쇠를 담는다. 지문(`proposal_digest`)은 도구 이름, 예약 칸을
  뺀 인자, 기준 리비전, 제안 id 와 그 열쇠로 짓는다. 그래서 같은 인자를 다시 낸 제안도 지문이
  다르고 오래된 카드가 새 제안을 결정하지 못한다. 모델이 보낸 같은 이름의 칸은 루프가 빼고, 어떤
  도구 본문도 이 칸을 읽지 않는다(`request_of` 는 표의 본문 칸만 보낸다). 세션 문서는 새 칸을 얻지
  않고 칸은 그 호출과 함께 사라진다.
- **승인(`Proposals.approve`).** 기준 리비전 위에 다시 짜 맞추고 사람 표시와 먼저 볼 것을 다시 센다.
  Tool 의 백엔드 종류나 효과가 그사이 바뀌어 넘는 것이 생기면 그 거절을, 카드에 없던 먼저 볼 것이
  나오면 `proposal_changed` 를 도구 결과로 주고 저장하지 않는다. 아니면 만들기는 `POST /api/agents`,
  바꾸기는 기준 리비전을 `expect_revision` 에 실은 PUT 이고 `layout` 은 보내지 않는다. 저장이 다시
  검증하므로 그사이 바뀐 문맥(지운 provider 같은 것)은 422 invalid_graph 로 돌아온다.
- **정의 읽기(`Proposals.read`, `get_agent_definition`).** 패치할 것을 개요 하나와 노드 하나씩으로
  읽는다. 결과는 4KB 로 자르지 않고 64 KiB 까지 모델에 주며(`ToolResult.exempt`), data 가 그보다 큰
  노드는 `node_too_large` 다. 개요도 그 안에 들도록 목록을 덜어 낸다. 화면과 전사에는 4KB 로 자른
  사본이 간다.
- **한 구간의 한도(`span_usage`, 6.6절).** 사람의 두 메시지 사이에는 카드에서 거절된 제안이 셋까지,
  통째로 준 읽기와 제안 인자를 합쳐 256 KiB 까지다. 넘으면 다음 제안과 정의 읽기는 `proposal_limit`
  이다. 둘 다 그 사람의 마지막 메시지 뒤의 문맥에서 세므로 따로 둘 상태가 없다.
- **다시 읽기(`Proposals.view`).** 기다리는 제안을 저장해 둔 기준 리비전 위에 다시 짜 맞춘다.
  화면(캔버스)이 읽는 것이라 검증은 부르는 쪽이 감사 행을 남기지 않는 판으로 넘긴다.
"""

from __future__ import annotations

import copy
import json
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from typing import Any, Literal
from urllib.parse import quote

from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, ToolMessage
from pydantic import ValidationError

from daegam.k8s.labels import versioned_hash
from daegam.services.agent_changes import (
    PatchError,
    apply_patch,
    attention,
    attention_key,
    compare,
    first_error,
    node_of,
    outline,
    problem,
    refusals,
)
from daegam.services.assistant.catalog import AG, GW, KB, MS, SK
from daegam.services.assistant.tools import (
    MAX_CONTENT_BYTES,
    Refused,
    ToolExecutor,
    ToolResult,
    TurnContext,
    error_of,
    error_result,
    path_segment,
    payload_of,
    result_of,
)
from daegam.store.agents import AgentSpec
from daegam.store.base import new_id
from daegam.units import KIB, json_bytes

PROPOSAL_KEY = "_proposal"
"""기다리는 제안 호출의 저장된 인자에 루프가 넣는 칸(모듈 머리). 화면은 이 칸을 인자로 보이지 않고
결정에 `proposal_digest` 로 되돌려 보낸다(`ui/src/lib/assistant/decisions.ts`)."""
PROPOSAL_MAX_BYTES = 64 * KIB
"""제안 하나의 인자 상한이자 정의 읽기 하나의 상한이다(6.2절, 6.6절)."""
DIGEST_VERSION = "p48.2"
"""제안 지문을 짓는 규칙의 판(`proposal_digest`). 규칙을 바꾸면 올린다. 기다리던 제안의 지문은 판이
다르면 맞지 않으므로 그 카드는 409 proposal_changed 를 받고 새 제안을 기다린다."""
CARD_REJECTED = "the user rejected this call in the approval card"
"""사람이 카드에서 거절한 호출의 detail. 루프가 쓴다. 한 구간의 거절 수는 결과의 `card` 칸으로
센다."""
CANVAS_SAVED = (
    "the user saved their own version of this proposal on the canvas; this call saved nothing"
)
"""사람이 캔버스에서 제 판을 저장한 뒤 그 제안 호출을 닫는 거절의 detail(6.5절). 카드 거절이
아니라서 `card` 칸이 없고 한 구간의 거절 수에 들지 않는다."""
DEFINITION_TOOL = "get_agent_definition"
SPAN_REJECTIONS = 3
"""사람의 두 메시지 사이에 카드에서 거절된 제안의 상한(6.6절)."""
SPAN_BYTES = 256 * KIB
"""사람의 두 메시지 사이에 통째로 준 정의 읽기와 제안 인자를 합친 상한(6.6절)."""

# 카드에 붙일 이름을 읽는 상세 경로. 이름은 그 문서의 것이고 모델이 쓴 글이 아니다(6.4절)
NAME_PATHS = {
    "mcp_instance": f"{MS}/{{}}",
    "gateway_instance": f"{GW}/{{}}",
    "knowledge": f"{KB}/{{}}",
    "skill": f"{SK}/{{}}",
}

ProposalKind = Literal["create", "update"]
Review = Callable[[AgentSpec], Awaitable[Mapping[str, Any]]]
"""짜 맞춘 정의의 검증. `POST /api/agents/validate` 의 답과 같은 모양(issues, tool_effects,
tool_origins)이다."""


def proposal_of(args: Mapping[str, Any] | None) -> dict[str, Any] | None:
    """저장된 인자의 예약 칸. 없으면 None 이다."""
    value = (args or {}).get(PROPOSAL_KEY)
    return value if isinstance(value, dict) else None


def without_proposal(args: Mapping[str, Any] | None) -> dict[str, Any]:
    """예약 칸을 뺀 인자. 모델이 보낸 같은 이름의 칸도 이것으로 빠진다."""
    return {k: v for k, v in (args or {}).items() if k != PROPOSAL_KEY}


def proposal_digest(
    name: str,
    args: Mapping[str, Any],
    base_revision: int | None,
    proposal_id: str,
    attention_keys: list[str],
) -> str:
    """제안의 지문(모듈 머리). 결정이 이 값을 되돌려 보내야 한다. 판은 `DIGEST_VERSION` 이다."""
    content = {
        "tool": name,
        "args": without_proposal(args),
        "base_revision": base_revision,
        "proposal_id": proposal_id,
        "attention": attention_keys,
    }
    return versioned_hash(content, DIGEST_VERSION)


def fit(content: dict[str, Any], *lists: str, max_bytes: int = MAX_CONTENT_BYTES) -> dict[str, Any]:
    """목록 칸을 가장 긴 것의 뒤에서부터 덜어 결과를 max_bytes 안에 맞춘다. 덜어 낸 수는 `omitted`
    다. 결과가 잘리면 목록이 통째로 빠지므로(`truncate`) 모델이 한 번에 고칠 수 있게 먼저 맞춘다."""
    out = {**content, **{k: list(content.get(k) or []) for k in lists}}
    omitted = 0
    while json_bytes(out) > max_bytes:
        longest = max(lists, key=lambda k: len(out[k]))
        if not out[longest]:
            break
        out[longest].pop()
        omitted += 1
        out["omitted"] = omitted
    return out


def refusal_result(problems: list[dict[str, Any]], marks: list[dict[str, Any]]) -> ToolResult:
    """넘지 못하는 선의 결과 하나. 사람 표시가 있으면 `person_marks_only` 다(6.2절 4)."""
    if marks:
        code = "person_marks_only"
        detail = (
            "read_only, rememberable and outside marks are set by a person on the canvas; "
            "drop them and propose again"
        )
    else:
        code = "proposal_invalid"
        detail = "fix these problems and propose again"
    content = {"code": code, "detail": detail, "issues": problems, "marks": marks}
    return ToolResult("error", fit(content, "issues", "marks"))


def _invalid(issues: list[dict[str, Any]]) -> Refused:
    return Refused(refusal_result(issues, []))


@dataclass
class Draft:
    """짜 맞춘 제안 하나. 만들기는 기준이 없고 모든 노드를 더하거나 바꾼 것으로 본다."""

    kind: ProposalKind
    name: str
    composed: AgentSpec
    touched: set[str]
    base: AgentSpec | None = None
    agent_id: str | None = None
    display_before: str | None = None
    display_after: str | None = None
    base_revision: int | None = None
    latest_revision: int | None = None
    deployed_revision: int | None = None


@dataclass
class Checked:
    """검사의 끝. 거절(refusal)이거나, 예약 칸의 값(reserved)과 카드의 덩어리(block)다."""

    refusal: ToolResult | None = None
    reserved: dict[str, Any] = field(default_factory=dict)
    block: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class SpanUsage:
    """사람의 마지막 메시지 뒤의 카드 거절 수와, 통째로 준 읽기와 제안 인자의 바이트."""

    rejections: int = 0
    bytes: int = 0


def span_usage(messages: list[BaseMessage], proposal_tools: set[str]) -> SpanUsage:
    """문맥에서 한 구간의 쓰임을 센다(모듈 머리). 요약은 사람의 메시지가 아니다."""
    start = 0
    for i, m in enumerate(messages):
        if isinstance(m, HumanMessage) and not m.additional_kwargs.get("daegam_summary"):
            start = i + 1
    rejections = used = 0
    for m in messages[start:]:
        if isinstance(m, AIMessage):
            used += sum(
                json_bytes(without_proposal(c.get("args")))
                for c in m.tool_calls
                if c["name"] in proposal_tools
            )
        elif isinstance(m, ToolMessage) and m.name == DEFINITION_TOOL and m.status == "success":
            used += len(m.text.encode("utf-8"))
        elif isinstance(m, ToolMessage) and m.name in proposal_tools and m.status == "error":
            try:
                content = json.loads(m.text)
            except ValueError:
                continue
            if isinstance(content, dict) and content.get("card") is True:
                rejections += 1
    return SpanUsage(rejections, used)


def span_limit(usage: SpanUsage, *, adding: int = 0) -> ToolResult | None:
    """한 구간의 한도를 넘으면 `proposal_limit` 결과다. adding 은 이제 더할 바이트다."""
    if usage.rejections >= SPAN_REJECTIONS:
        reason = f"{usage.rejections} proposals were rejected on the card"
    elif usage.bytes + adding > SPAN_BYTES:
        reason = f"definition reads and proposals reached {SPAN_BYTES} bytes"
    else:
        return None
    return error_result(
        "proposal_limit",
        f"{reason} since the person's last message; ask the person to say in a new message what "
        "to change, then read and propose again",
    )


def review_label(n: int) -> str:
    """제안 검사의 검증이 감사 행에 남기는 호출 번호. 같은 호출의 저장(`<n>`)과 가른다."""
    return f"{n}-check"


@dataclass
class Proposals:
    """제안의 검사·승인·읽기·다시 읽기. 실행기의 앱을 턴의 신원으로 부른다."""

    executor: ToolExecutor

    async def _get(self, ctx: TurnContext, n: int, path: str) -> dict[str, Any]:
        resp = await self.executor.request("GET", path, ctx=ctx, n=n)
        payload = payload_of(resp)
        failed = error_of(resp, payload)
        if failed is not None:
            raise Refused(failed)
        return payload if isinstance(payload, dict) else {}

    async def read(
        self, ctx: TurnContext, n: int, args: Mapping[str, Any], usage: SpanUsage
    ) -> ToolResult:
        """`get_agent_definition`. 최신 리비전의 개요, 또는 node_id 의 노드 하나다(모듈 머리). 한
        구간의 예산이 남았을 때만 읽고, 읽은 것이 예산을 넘기면 주지 않는다."""
        limit = span_limit(usage, adding=1)
        if limit is not None:
            return limit
        try:
            agent_id = path_segment("agent_id", args.get("agent_id"))
            agent = await self._get(ctx, n, f"{AG}/{agent_id}")
        except Refused as e:
            return e.result
        node_id = args.get("node_id")
        if not node_id:
            content = fit(outline(agent), "nodes", "edges", max_bytes=PROPOSAL_MAX_BYTES)
        else:
            node = node_of(agent, str(node_id))
            if node is None:
                ids = [n.get("id") for n in (agent.get("graph") or {}).get("nodes") or []]
                return error_result(
                    "unknown_node", f"no node {node_id!r} in the saved revision", nodes=ids
                )
            # 패치의 교체 상한(`apply_patch`)과 같은 잣대로 data 를 잰다
            size = json_bytes(node["data"])
            if size > PROPOSAL_MAX_BYTES:
                return error_result(
                    "node_too_large",
                    f"node {node_id!r} holds {size} bytes of data, above {PROPOSAL_MAX_BYTES}; it "
                    "cannot be read whole, so a proposal cannot replace it. The person edits it "
                    "on the canvas",
                )
            content = {"agent_id": agent.get("id"), "revision": agent.get("revision"), "node": node}
        return span_limit(usage, adding=json_bytes(content)) or ToolResult(
            "success", content, exempt=True
        )

    async def review(self, ctx: TurnContext, n: int, spec: AgentSpec) -> dict[str, Any]:
        """짜 맞춘 정의의 검증(`POST /api/agents/validate`). 이슈와 Tool 효과·백엔드 종류다."""
        body = spec.model_dump(mode="json")
        resp = await self.executor.request(
            "POST", f"{AG}/validate", ctx=ctx, n=review_label(n), json=body
        )
        payload = payload_of(resp)
        failed = error_of(resp, payload)
        if failed is not None:
            raise Refused(failed)
        return payload if isinstance(payload, dict) else {}

    async def draft(
        self,
        ctx: TurnContext,
        n: int,
        name: str,
        args: Mapping[str, Any],
        *,
        base_revision: int | None = None,
    ) -> Draft:
        """제안을 정의로 짜 맞춘다. 바꾸기는 base_revision(없으면 최신)의 리비전 위다."""
        if name == "create_agent":
            return self._creation(args)
        agent_id = path_segment("agent_id", args.get("agent_id"))
        agent = await self._get(ctx, n, f"{AG}/{agent_id}")
        latest = agent.get("revision")
        base_revision = base_revision or latest
        source = agent
        if base_revision != latest:
            source = await self._get(ctx, n, f"{AG}/{agent_id}/revisions/{base_revision}")
        base = AgentSpec.model_validate(
            {"graph": source.get("graph") or {}, "service": source.get("service") or {}}
        )
        try:
            composed, touched = apply_patch(base, args, node_cap=PROPOSAL_MAX_BYTES)
        except PatchError as e:
            raise _invalid(e.issues) from e
        before = agent.get("display_name")
        after = args.get("set_display_name")
        return Draft(
            kind="update",
            name=str(agent.get("name") or ""),
            composed=composed,
            touched=touched,
            base=base,
            agent_id=agent.get("id"),
            display_before=before,
            display_after=after if isinstance(after, str) and after else before,
            base_revision=base_revision,
            latest_revision=latest,
            deployed_revision=agent.get("deployed_revision"),
        )

    def _creation(self, args: Mapping[str, Any]) -> Draft:
        name = args.get("name")
        if not isinstance(name, str) or not name:
            raise _invalid([problem("missing_argument", "name is required")])
        try:
            spec = AgentSpec.model_validate(
                {"graph": args.get("graph") or {}, "service": args.get("service") or {}}
            )
        except ValidationError as e:
            raise _invalid([problem("invalid_arguments", first_error(e))]) from e
        display = args.get("display_name")
        return Draft(
            kind="create",
            name=name,
            composed=spec,
            touched={node.id for node in spec.graph.nodes},
            display_after=display if isinstance(display, str) and display else name,
        )

    @staticmethod
    def _refused(draft: Draft, review: Mapping[str, Any], *, errors: bool) -> ToolResult | None:
        """넘지 못하는 선. errors 가 거짓이면 검증 오류는 보지 않는다(승인 뒤 저장이 다시 본다)."""
        problems, marks = refusals(
            draft.base,
            draft.composed,
            draft.touched,
            issues=(review.get("issues") or []) if errors else [],
            origins=review.get("tool_origins") or {},
        )
        return refusal_result(problems, marks) if problems or marks else None

    async def check(
        self, ctx: TurnContext, n: int, name: str, args: Mapping[str, Any], usage: SpanUsage
    ) -> Checked:
        """카드에 보내기 전의 검사(모듈 머리)."""
        limit = span_limit(usage)
        if limit is not None:
            return Checked(refusal=limit)
        size = json_bytes(args)
        if size > PROPOSAL_MAX_BYTES:
            return Checked(
                refusal=error_result(
                    "proposal_too_large",
                    f"the proposal is {size} bytes; the limit is {PROPOSAL_MAX_BYTES}. Change "
                    "fewer nodes at once",
                )
            )
        try:
            draft = await self.draft(ctx, n, name, args)
            review = await self.review(ctx, n, draft.composed)
        except Refused as e:
            return Checked(refusal=e.result)
        refused = self._refused(draft, review, errors=True)
        if refused is not None:
            return Checked(refusal=refused)
        found = attention(draft.base, draft.composed, review.get("tool_effects") or {})
        proposal_id = new_id()
        keys = [attention_key(m) for m in found]
        reserved = {
            "id": proposal_id,
            "digest": proposal_digest(name, args, draft.base_revision, proposal_id, keys),
            "base_revision": draft.base_revision,
            "attention": keys,
        }
        block = await self.block(ctx, n, proposal_id, draft, review, found)
        return Checked(reserved=reserved, block=block)

    async def approve(
        self, ctx: TurnContext, n: int, name: str, stored: Mapping[str, Any]
    ) -> ToolResult:
        """사람이 승인한 제안을 저장한다(모듈 머리). stored 는 예약 칸이 든 저장된 인자다."""
        reserved = proposal_of(stored) or {}
        args = without_proposal(stored)
        try:
            draft = await self.draft(
                ctx, n, name, args, base_revision=reserved.get("base_revision")
            )
            review = await self.review(ctx, n, draft.composed)
        except Refused as e:
            return e.result
        refused = self._refused(draft, review, errors=False)
        if refused is not None:
            return refused
        shown = set(reserved.get("attention") or [])
        found = attention(draft.base, draft.composed, review.get("tool_effects") or {})
        new = [m for m in found if attention_key(m) not in shown]
        if new:
            return error_result(
                "proposal_changed",
                "the platform found changes to check that the card did not show; nothing was "
                "saved. Propose again so the person sees the new card",
                attention=sorted({m["code"] for m in new}),
            )
        if draft.kind == "create":
            return await self.executor.call(name, args, ctx=ctx, n=n)
        body = {
            "name": draft.name,
            "display_name": draft.display_after,
            **draft.composed.model_dump(mode="json"),
            "expect_revision": draft.base_revision,
        }
        agent_id = path_segment("agent_id", draft.agent_id)
        resp = await self.executor.request("PUT", f"{AG}/{agent_id}", ctx=ctx, n=n, json=body)
        return result_of(self.executor.defs[name].spec, resp)

    async def view(
        self,
        ctx: TurnContext,
        n: int,
        name: str,
        stored: Mapping[str, Any],
        review: Review,
    ) -> dict[str, Any]:
        """기다리는 제안 호출을 다시 짜 맞춘 것(`GET /api/assistant/sessions/{id}/proposal`, 6.1절).
        최신이 아니라 저장해 둔 기준 리비전 위이고, 최신 리비전은 따로 알려 캔버스가 오래된 제안을
        말하게 한다. 검증은 부르는 쪽이 준다(화면의 읽기가 청지기의 감사 행을 남기지 않게).
        대상을 읽지 못하면 `Refused` 가 그 결과를 들고 올라간다."""
        reserved = proposal_of(stored) or {}
        args = without_proposal(stored)
        draft = await self.draft(ctx, n, name, args, base_revision=reserved.get("base_revision"))
        checked = await review(draft.composed)
        found = attention(draft.base, draft.composed, checked.get("tool_effects") or {})
        proposal_id = str(reserved.get("id") or "")
        return {
            "proposal_id": proposal_id,
            "kind": draft.kind,
            "agent_id": draft.agent_id,
            "name": draft.name,
            "display_name": draft.display_after,
            "base_revision": draft.base_revision,
            "latest_revision": draft.latest_revision,
            **draft.composed.model_dump(mode="json"),
            "proposal": await self.block(ctx, n, proposal_id, draft, checked, found),
        }

    async def block(
        self,
        ctx: TurnContext,
        n: int,
        proposal_id: str,
        draft: Draft,
        review: Mapping[str, Any],
        found: list[dict[str, Any]],
    ) -> dict[str, Any]:
        """카드와 캔버스가 그리는 제안 덩어리(6.4절). 소스·지식·Skill 의 이름은 그 문서에서
        읽는다."""
        warnings = [
            {"code": i.get("code"), "node_id": i.get("node_id"), "message": i.get("message")}
            for i in review.get("issues") or []
            if i.get("level") == "warning"
        ]
        return {
            "proposal_id": proposal_id,
            "kind": draft.kind,
            "agent": {
                "id": draft.agent_id,
                "name": draft.name,
                "display_name": draft.display_after,
            },
            "base_revision": draft.base_revision,
            "deployed_revision": draft.deployed_revision,
            "changes": compare(
                draft.base,
                draft.composed,
                display_before=draft.display_before,
                display_after=draft.display_after,
            ),
            "attention": await self._named(ctx, n, found),
            "warnings": warnings,
        }

    async def _named(
        self, ctx: TurnContext, n: int, found: list[dict[str, Any]]
    ) -> list[dict[str, Any]]:
        """먼저 볼 것에 소스·지식·Skill 의 이름을 붙인 사본. 읽지 못하면 이름 없이 둔다."""
        names: dict[str, str | None] = {}

        async def name_of(kind: str, ref_id: Any) -> str | None:
            if kind not in NAME_PATHS or not isinstance(ref_id, str) or not ref_id or "/" in ref_id:
                return None
            path = NAME_PATHS[kind].format(quote(ref_id, safe=""))
            if path not in names:
                try:
                    doc = await self._get(ctx, n, path)
                except Refused:
                    doc = {}
                label = doc.get("display_name") or doc.get("name")
                names[path] = label if isinstance(label, str) else None
            return names[path]

        out: list[dict[str, Any]] = []
        for mark in copy.deepcopy(found):
            for key, kind in (("source", None), ("knowledge", "knowledge"), ("skill", "skill")):
                ref = mark.get(key)
                if not isinstance(ref, dict):
                    continue
                label = await name_of(kind or str(ref.get("kind")), ref.get("id"))
                if label is not None:
                    ref["name"] = label
            out.append(mark)
        return out


__all__ = [
    "CANVAS_SAVED",
    "CARD_REJECTED",
    "DEFINITION_TOOL",
    "DIGEST_VERSION",
    "PROPOSAL_KEY",
    "PROPOSAL_MAX_BYTES",
    "SPAN_BYTES",
    "SPAN_REJECTIONS",
    "Checked",
    "Draft",
    "Proposals",
    "Review",
    "SpanUsage",
    "fit",
    "proposal_digest",
    "proposal_of",
    "refusal_result",
    "review_label",
    "span_limit",
    "span_usage",
    "without_proposal",
]
