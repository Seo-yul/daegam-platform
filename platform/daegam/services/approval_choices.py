"""기억한 승인 선택의 규칙, 기억하기, 전달과 정리 (P43 5절, D96).

세션 소유자가 결정과 함께 "다음부터 묻지 않고 승인" 이나 "거부" 를 고르면 사람, Agent, Tool 이름마다
기억한다(`store/approval_choices.py`). 런타임은 그 선택을 턴 문맥에만 두고 판정에 쓴다
(agent-runtime `runtime/tool_safety.py`). 기억한 승인은 바깥 내용이 없는 대화의 정적 규칙만
건너뛰고, 거부는 그 Tool 을 부르지 않는다.

- **규칙(`ChoiceRules`).** 배포된 리비전의 tools 노드가 Tool 이름의 규칙이다. 이름을 주는 노드는
  고른 Tool 이나 배포 때의 핀으로 정하고(`tool_pins.pinned_tools`), 이름을 모르는 노드(접두사 모드의
  Gateway)는 어떤 이름이든 줄 수 있는 것으로 본다. 이름을 주는 모든 노드의 (소스, 승인 결정,
  `rememberable`)을 요약한 것이 `digest` 다. 기억한 승인은 그 노드가 모두 정적 규칙을 두고
  `rememberable` 로 허락했을 때만이고, 거부는 소스의 Tool 이름이면 된다. 모든 Agent 에 내장된
  Tool 이름(파일 Tool 일곱과 `execute`)은 선택을 받지 않는다. 런타임은 그 이름의 소스 Tool 을
  버린다. Agent 에 따라 내장되는 이름(`write_todos`, `task`, `structured_response`)은 소스
  Tool 이면 받고, 그 Agent 에 내장이면 런타임이 그 선택을 쓰지 않는다. Gateway 소스의
  `execute_tool_script` 는 기억한 승인을 받지 않는다. 그 정적 규칙에는 스크립트가 부르는 Gateway
  Tool 의 승인이 합쳐져 있어(P56 6.5절) 기억한 승인이 그것을 건너뛰기 때문이다. 거부는 받는다.
- **기억하기(`to_remember`).** 결정마다의 `remember` 다. approve 는 approve 결정과만, reject 는
  reject 결정과만 짝이다. Tool 이름은 대기 요청에서, 규칙은 배포된 리비전에서 읽는다. 세션 소유자
  자신의 결정이 아니면(소유자를 모름, 승인자의 결정), 대기 요청이 소유자의 것이 아니면, 실행
  세션이나 이유가 `outside` 인 요청의 승인이면, 이미지가 `tool_safety` 를 모르면, 규칙이 허락하지
  않으면 422 `remember_refused` 다. 런타임이 결정을 받은 뒤에 적고 다음 턴부터 쓴다.
- **카드의 선택(`card_options`).** 승인 카드가 요청마다(action_requests 와 같은 순서) 보일 기억할
  수 있는 선택이다. 부른 사람이 기록된 세션 소유자이고 대기 요청이 그의 것이며 이미지가
  `tool_safety` 를 알릴 때만이고, 그 요청이 받는 결정(`review_configs`) 가운데 기억하기가 받을
  것만 낸다. 기억하기와 같은 검사(`_why_not`)를 쓴다. 그 밖이거나 읽지 못하면 None 이고 카드는
  기억하기를 보이지 않는다.
- **전달(`for_turn`).** 턴이 세션 소유자의 이름으로 돌고(`TurnPlan.owner_sub`) 이미지가
  `tool_safety` 를 알릴 때만 `/chat`, 결정, 다시 생성, 실행의 시작 턴 본문에 `approval_choices` 로
  싣는다. 요약이 지금 규칙과 맞는 것만 간다. 실행 세션에는 거부만 간다. 실행 세션은 실행 기록으로
  가리므로 백그라운드 실행을 끈 배포본에 남은 세션도 같다.
- **정리.** 만료는 TTL 인덱스가, 배포가 바꾼 규칙과 없어진 Tool 은 배포 뒤의 걷기(`prune`)가, Agent
  삭제와 소속을 잃은 사람은 그 훅이 지운다.
"""

from __future__ import annotations

import hashlib
import json
import logging
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from pymongo.errors import PyMongoError

from daegam.errors import ServiceError
from daegam.k8s.templates.vmcp import CODE_MODE_TOOL
from daegam.services.agent_code_mode import GATEWAY_SOURCE
from daegam.services.agent_features import TOOL_SAFETY
from daegam.services.tool_effects import UNKNOWN_SOURCE, tools_nodes
from daegam.services.tool_pins import ToolPins, pinned_tools, valid_pins
from daegam.services.toolnames import ALWAYS_BUILT_IN
from daegam.services.turn_person import TurnPlan
from daegam.store.agent_runs import MongoAgentRunStore
from daegam.store.agent_session_owners import MongoAgentSessionOwnerStore
from daegam.store.agents import Agent, AgentSpec
from daegam.store.approval_choices import (
    APPROVE,
    REJECT,
    ApprovalChoice,
    Choice,
    MongoApprovalChoiceStore,
)
from daegam.store.base import utcnow

logger = logging.getLogger(__name__)

REMEMBER_REFUSED = "remember_refused"
REMEMBER_KEY = "remember"
REASON_RULE = "rule"


@dataclass(frozen=True)
class ChoiceRule:
    """Tool 이름 하나의 규칙이다. approve 는 기억한 승인을 받는가다."""

    digest: str
    approve: bool


@dataclass(frozen=True)
class _NodeRule:
    node_id: str
    source: tuple[str, str]
    names: frozenset[str] | None
    approval: Mapping[str, Sequence[str]]
    rememberable: frozenset[str]


@dataclass(frozen=True)
class ChoiceRules:
    """배포된 리비전의 규칙이다(모듈 머리). `of` 로 만든다."""

    nodes: tuple[_NodeRule, ...]

    @classmethod
    def of(cls, rev: AgentSpec, pins: ToolPins) -> ChoiceRules:
        nodes = []
        for node_id, data in tools_nodes(rev):
            names, _ = pinned_tools(data.allow_tools, UNKNOWN_SOURCE, pins.get(node_id))
            nodes.append(
                _NodeRule(
                    node_id=node_id,
                    source=(data.source.kind, data.source.id),
                    names=frozenset(names) if names is not None else None,
                    approval=data.approval,
                    rememberable=frozenset(data.rememberable),
                )
            )
        return cls(tuple(nodes))

    def rule(self, name: str) -> ChoiceRule | None:
        """name 의 규칙이다. 그 이름을 줄 수 있는 노드가 없거나 늘 내장인 이름이면 None 이다."""
        if name in ALWAYS_BUILT_IN:
            return None
        offering = [n for n in self.nodes if n.names is None or name in n.names]
        if not offering:
            return None
        approve = all(
            n.names is not None and name in n.approval and name in n.rememberable for n in offering
        ) and not (name == CODE_MODE_TOOL and any(n.source[0] == GATEWAY_SOURCE for n in offering))
        raw = json.dumps(
            sorted(
                [
                    n.node_id,
                    list(n.source),
                    n.names is None,
                    n.approval.get(name),
                    name in n.rememberable,
                ]
                for n in offering
            ),
            ensure_ascii=False,
        )
        return ChoiceRule(digest=hashlib.sha256(raw.encode()).hexdigest()[:32], approve=approve)


def _refused(why: str) -> ServiceError:
    return ServiceError(422, REMEMBER_REFUSED, f"this decision cannot be remembered: {why}")


def _remembers(decision: Any) -> bool:
    return isinstance(decision, dict) and decision.get(REMEMBER_KEY) is not None


def strip_remember(decisions: Sequence[Any]) -> list[Any]:
    """런타임에 보낼 결정이다. `remember` 는 플랫폼만 읽는다."""
    return [
        {k: v for k, v in d.items() if k != REMEMBER_KEY} if isinstance(d, dict) else d
        for d in decisions
    ]


# 대기 요청을 읽는다. 기억할 것이 있을 때만 부른다
PendingReader = Callable[[], Awaitable[Mapping[str, Any]]]


# 기억할 수 있는 선택이다. 카드는 이 순서로 보인다
CHOICES: tuple[Choice, ...] = (APPROVE, REJECT)


def _tool_name(action: Mapping[str, Any]) -> str:
    return str(action.get("name") or "")


def _why_not(
    rules: ChoiceRules, action: Mapping[str, Any], choice: Choice, *, run: bool
) -> str | None:
    """그 대기 요청의 Tool 에 choice 를 기억할 수 없는 까닭이다. 기억할 수 있으면 None 이다."""
    name = _tool_name(action)
    rule = rules.rule(name)
    if rule is None:
        return f"{name!r} is not a tool of a tool source"
    if choice == REJECT:
        return None
    if action.get("reason") != REASON_RULE:
        return "this call was asked because the chat holds outside content"
    if not rule.approve:
        return f"the approval rule of {name!r} may not be remembered"
    if run:
        return "approvals are not remembered in background runs"
    return None


def _allowed_decisions(pending: Mapping[str, Any], name: str) -> Sequence[str]:
    """그 요청이 받는 결정이다. 설정이 없으면 승인과 거부다(화면의 `normalizePending` 과 같다)."""
    for config in pending.get("review_configs") or []:
        if isinstance(config, dict) and config.get("action_name") == name:
            return config.get("allowed_decisions") or []
    return CHOICES


class ApprovalChoiceService:
    def __init__(
        self,
        store: MongoApprovalChoiceStore,
        runs: MongoAgentRunStore,
        owners: MongoAgentSessionOwnerStore,
        *,
        days: int,
        now: Callable[[], datetime] = utcnow,
    ):
        self.store = store
        self.runs = runs
        self.owners = owners
        self.days = days
        self.now = now

    @staticmethod
    def rules(doc: Agent) -> ChoiceRules | None:
        """배포된 리비전의 규칙이다. 배포되지 않았으면 None 이다."""
        rev = doc.deployed
        return ChoiceRules.of(rev, valid_pins(doc)) if rev is not None else None

    @classmethod
    def _supported_rules(cls, doc: Agent) -> ChoiceRules | None:
        """이미지가 `tool_safety` 를 알리는 배포된 Agent 의 규칙이다. 그 밖은 None 이다. 미리 보기는
        아무것도 기억하지 않으므로 None 이다 (P44b 7절)."""
        if doc.preview is not None or not doc.image_reports(TOOL_SAFETY):
            return None
        return cls.rules(doc)

    async def _in_run(self, doc: Agent, session_id: str | None) -> bool:
        """백그라운드 실행의 세션인가. 실행을 끈 배포본에 남은 실행 세션도 실행 기록으로 가린다."""
        if session_id is None:
            return False
        return await self.runs.for_session(doc.tenant, doc.id, session_id) is not None

    async def for_turn(self, plan: TurnPlan, *, run: bool = False) -> list[dict[str, str]] | None:
        """그 턴의 본문에 실을 `approval_choices` 다. 실을 것이 없으면 None 이다(모듈 머리). run 은
        라우트가 이미 실행 세션으로 안 것이다."""
        return await self._deliver(plan.doc, plan.owner_sub, plan.session_id, run=run)

    async def for_run_start(self, doc: Agent, sub: str) -> list[dict[str, str]] | None:
        """실행의 시작 턴에 실을 선택이다. 세션이 아직 없고 거부만 간다. 거부는 막기만 하므로
        실행이 Agent 의 이름으로 돌아도 묶인 사람의 것을 싣는다."""
        return await self._deliver(doc, sub, None, run=True)

    async def _deliver(
        self, doc: Agent, sub: str | None, session_id: str | None, *, run: bool
    ) -> list[dict[str, str]] | None:
        if sub is None or not doc.image_reports(TOOL_SAFETY):
            return None
        live = await self.store.live(sub, doc.id, self.now())
        rules = self._supported_rules(doc) if live else None
        if rules is None:
            return None
        rejects_only = run or await self._in_run(doc, session_id)
        out = [
            {"tool": c.tool, "choice": c.choice}
            for c in live
            if (rule := rules.rule(c.tool)) is not None
            and rule.digest == c.digest
            and not (rejects_only and c.choice == APPROVE)
        ]
        return out or None

    async def to_remember(
        self, plan: TurnPlan, decisions: Sequence[Any], pending: PendingReader
    ) -> list[ApprovalChoice]:
        """결정의 `remember` 를 검사하고 런타임이 결정을 받은 뒤 적을 선택이다. 받을 수 없으면 422
        remember_refused 다(모듈 머리). 대기 요청은 소유자를 확인한 뒤에만 읽는다."""
        wanted = [(i, d) for i, d in enumerate(decisions) if _remembers(d)]
        if not wanted:
            return []
        owner = plan.confirms
        if owner is None:
            raise _refused("only the session's owner can remember a decision")
        doc = plan.doc
        rules = self._supported_rules(doc)
        if rules is None and doc.preview is not None:
            raise _refused("a preview remembers no choices")
        if rules is None:
            raise _refused("the Agent's runtime image does not support remembered choices")
        waiting = await pending()
        if waiting.get("user_sub") != owner.sub:
            raise _refused("the pending request is not the session owner's")
        actions = waiting.get("action_requests") or []
        approves = any(d[REMEMBER_KEY] == APPROVE for _, d in wanted)
        run = approves and await self._in_run(doc, plan.session_id)
        now = self.now()
        out: list[ApprovalChoice] = []
        for index, decision in wanted:
            choice: Choice = decision[REMEMBER_KEY]
            if choice not in (APPROVE, REJECT) or decision.get("type") != choice:
                raise _refused("remember must match the decision (approve or reject)")
            if index >= len(actions):
                raise _refused("the decision has no pending request")
            action = actions[index]
            if (why := _why_not(rules, action, choice, run=run)) is not None:
                raise _refused(why)
            name = _tool_name(action)
            rule = rules.rule(name)
            assert rule is not None, "_why_not refuses a name without a rule"
            if any(c.tool == name and c.choice != choice for c in out):
                raise _refused(f"the decisions remember both approve and reject for {name!r}")
            out.append(
                ApprovalChoice(
                    tenant=doc.tenant,
                    sub=owner.sub,
                    agent_id=doc.id,
                    tool=name,
                    choice=choice,
                    digest=rule.digest,
                    created_at=now,
                    expires_at=now + timedelta(days=self.days),
                )
            )
        return out

    async def card_options(
        self, doc: Agent, session_id: str, caller_sub: str, pending: Mapping[str, Any]
    ) -> list[list[Choice]] | None:
        """승인 카드가 요청마다 보일 기억할 수 있는 선택이다(모듈 머리). 읽지 못하면 None 이다. 대기
        요청은 보이므로 카드만 기억하기 없이 선다."""
        rules = self._supported_rules(doc)
        if rules is None or pending.get("user_sub") != caller_sub:
            return None
        try:
            record = await self.owners.get(doc.id, session_id)
            if record is None or record.sub != caller_sub:
                return None
            run = await self._in_run(doc, session_id)
        except PyMongoError as e:
            logger.warning("agent %s: remember options not read: %s", doc.id, e)
            return None
        actions = pending.get("action_requests") or []
        return [
            [
                c
                for c in CHOICES
                if c in _allowed_decisions(pending, _tool_name(a))
                and _why_not(rules, a, c, run=run) is None
            ]
            for a in actions
        ]

    async def save(self, choices: Sequence[ApprovalChoice]) -> None:
        """런타임이 결정을 받은 뒤에 적는다. 실패는 턴을 막지 않는다(사람이 다시 고르면 된다)."""
        for choice in choices:
            try:
                await self.store.put(choice)
            except Exception as e:  # noqa: BLE001 - 결정은 이미 런타임에 갔다
                logger.warning("approval choice for %s not saved: %s", choice.tool, e)

    async def prune(self, doc: Agent) -> int:
        """배포 뒤에 규칙이 바뀌었거나 Tool 이 없어진 선택을 지운다."""
        rules = self.rules(doc)
        stale = [
            c.id
            for c in await self.store.for_agent(doc.id)
            if rules is None or (r := rules.rule(c.tool)) is None or r.digest != c.digest
        ]
        return await self.store.delete_ids(stale) if stale else 0

    async def mine(self, sub: str) -> list[ApprovalChoice]:
        """그 사람의 아직 끝나지 않은 선택이다(계정 화면)."""
        return await self.store.list_mine(sub, self.now())

    async def forget_mine(self, sub: str, choice_id: str) -> ApprovalChoice | None:
        return await self.store.delete_mine(sub, choice_id)

    async def forget_agent(self, agent_id: str) -> int:
        return await self.store.delete_for_agent(agent_id)

    async def forget_person(self, sub: str, tenant: str | None = None) -> int:
        """그 사람의 선택을 지운다. tenant 를 주면 소속을 잃은 그 테넌트의 것만이다."""
        return await self.store.delete_for_person(sub, tenant)
