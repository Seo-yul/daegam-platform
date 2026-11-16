"""가드레일이다(D26). agent.json guardrails[] 를 position 별로 건다.

- input: /chat 입구에서 검사한다(InputGuard). max_length·deny_pattern 은 block 이면 422
  guardrail_blocked, flag 면 이벤트만이다. pii_mask 는 block 이면 입구에서 같은 검출기로 막고,
  flag 면 내장 PIIMiddleware(strategy=mask, apply_to_input) 가 모델 호출 전에 가린다. 첨부 글
  (attachments.py)에는 max_length 를 걸지 않는다. 사용자가 친 말의 상한이라 걸면 거의 모든 문서가
  거절되기 때문이다. 위반에는 그 첨부의 이름이 붙는다.
- output: after_model 훅이다(OutputGuard). deny_pattern·max_length 가 걸리면 block 은 응답을
  message 로 바꾸고 턴을 끝내며, flag 는 그대로 두고 이벤트만 낸다. pii_mask 는
  PIIMiddleware(strategy=mask, apply_to_output) 다. 훅은 체크포인트만 고치고 스트림은 모델이 낸
  글을 조각마다 바로 흘리므로, 글을 바꾸는 규칙이 하나라도 있으면 같은 규칙을 OutputFilter 로
  묶어 스트림이 낼 글에도 댄다(stream.py, D65). 글·구조화 결과·서브에이전트의 task 결과 세 길이
  같은 필터를 지난다.
- tool: wrap_tool_call 이다(ToolGuard). 호출 전 인자 문자열과 호출 뒤 결과 본문에 deny_pattern,
  결과 본문에 max_length·pii_mask(mask) 를 적용한다. 결과를 state·스트림에 싣기 전에 고치므로
  가려지지 않은 본문이 클라이언트로 새지 않는다. 결과를 막을 때는 artifact(인용 출처,
  citations.py)도 비워 막힌 결과의 출처가 `citations` 이벤트로 나가지 않게 한다. 검사하는 호출은
  실제로 도는 것이다(`effective_call`). 승인자가 고친 호출은 langchain HITL 이 실행 순간 상태
  키에서 꺼내 바꿔 끼우는데 ToolGuard 는 그 바깥이라 요청의 호출이 모델의 것이기 때문이다. 인자
  규칙은 `ToolGuard.args_violations` 로 떼어 두어 결정 때(hitl.py)도 같은 규칙이 고친 인자를
  본다(D65). injection_check(P43 7절)는 ToolGuard 가 아니라 판정 모델 층(judge.py)이 메인과 모든
  서브에이전트에 건다.

모든 위반은 custom 스트림 신호 guardrail{position, rule, action} 으로 나가 SSE 와 stdout 에 남는다.
"""

from __future__ import annotations

import json
import re
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from langchain.agents.middleware import PIIMiddleware, RedactionRule
from langchain.agents.middleware.types import (
    AgentMiddleware,
    AgentState,
    ToolCallRequest,
    hook_config,
)
from langchain_core.messages import AIMessage, ToolCall, ToolMessage
from langgraph.runtime import Runtime
from langgraph.types import Command

from runtime import signals
from runtime.config import AgentConfig, ConfigError

if TYPE_CHECKING:
    # 타입 주석에만 쓴다. 값은 공개 RedactionRule.resolve() 가 만들므로 런타임 import 가 필요 없고,
    # 이 이름만 langchain.agents.middleware 가 재수출하지 않아 비공개 모듈에서 가져온다.
    from langchain.agents.middleware._redaction import ResolvedRedactionRule

POSITION_INPUT = "input"
POSITION_OUTPUT = "output"
POSITION_TOOL = "tool"

ACTION_BLOCK = "block"
ACTION_FLAG = "flag"
ACTION_MASK = "mask"

# agent.json guardrails[].rules[].kind 다. 분기의 축이라 위치·행동과 같이 어휘로 둔다
RULE_MAX_LENGTH = "max_length"
RULE_DENY_PATTERN = "deny_pattern"
RULE_PII_MASK = "pii_mask"
# 판정 모델이다(P43 7절, judge.py). position tool 에만 오고 ToolGuard 가 아니라 판정 모델 층이
# 다룬다
RULE_INJECTION_CHECK = "injection_check"

# tool 위반이 인자에서 났는지 결과에서 났는지다. 신호의 stage 값이다
STAGE_ARGS = "args"
STAGE_RESULT = "result"

DEFAULT_BLOCK_MESSAGE = "요청이 가드레일 규칙에 걸려 처리하지 않았다."

HITL_EDITED_CALLS_KEY = "hitl_edited_tool_calls"
"""langchain HumanInTheLoopMiddleware 가 승인자의 고친 호출을 `{call_id: {name, args}}` 로 두는
상태 키다(1.4.3 부터). 메시지에는 모델의 호출이 그대로 남고 HITL 의 wrap_tool_call 이 실행 순간 이
키에서 꺼내 바꿔 끼운다. 키 이름이 상류와 어긋나면 실행 때 가드가 모델의 인자를 보게 되어
tests/test_hitl.py 의 test_edit_replaces_the_blocked_model_args 가 실패한다."""


def emit_violation(position: str, rule: str, action: str, **extra: Any) -> None:
    """위반 하나를 custom 스트림 신호로 보낸다. 여덟 자리와 판정 모델 층(judge.py)이 같은 모양으로
    낸다."""
    signals.emit(signals.GUARDRAIL, position=position, rule=rule, action=action, **extra)


@dataclass(frozen=True)
class Violation:
    position: str
    rule: str
    action: str
    tool: str | None = None
    """tool 위반의 도구 이름이다."""
    stage: str | None = None
    """tool 위반이 난 자리(STAGE_ARGS·STAGE_RESULT)다."""
    message: str | None = None
    """block 위반의 문구다. flag 에는 없다."""
    attachment: str | None = None
    """첨부 글의 input 위반이면 그 첨부의 이름이다. 관측 이벤트에는 record_bodies 일 때만 싣는다."""

    def to_dict(self) -> dict[str, Any]:
        """SSE·관측 이벤트의 본문이다. 비어 있는 칸은 싣지 않는다."""
        out: dict[str, Any] = {"position": self.position, "rule": self.rule, "action": self.action}
        for key in ("tool", "stage", "message", "attachment"):
            value = getattr(self, key)
            if value is not None:
                out[key] = value
        return out


class GuardrailBlocked(Exception):
    """입구 검사(입력, 결정의 고친 인자)에서 block 위반이 났다. 라우터가 422 guardrail_blocked 로
    옮기고 위반 본문을 관측 이벤트로 남긴다."""

    def __init__(self, violation: Violation) -> None:
        super().__init__(f"{violation.rule}: {violation.message}")
        self.violation = violation
        self.rule = violation.rule
        self.message = violation.message or DEFAULT_BLOCK_MESSAGE


@dataclass(frozen=True)
class Rule:
    kind: str
    max_length: int | None = None
    pattern: re.Pattern[str] | None = None
    pii: tuple[ResolvedRedactionRule, ...] = ()
    pii_regex: str | None = None
    """pii_mask 의 types 에 regex 가 있을 때의 사용자 정의 패턴이다."""
    judge: Mapping[str, Any] | None = None
    """injection_check 의 value(model·max_chars·max_calls)다. 판정 모델 층이 읽는다."""

    def text_violates(self, text: str) -> bool:
        """max_length·deny_pattern 만 본다. pii 는 검출기가 따로 다룬다."""
        if self.kind == RULE_MAX_LENGTH:
            return self.max_length is not None and len(text) > self.max_length
        if self.kind == RULE_DENY_PATTERN:
            return self.pattern is not None and self.pattern.search(text) is not None
        return False

    @property
    def is_pii(self) -> bool:
        return self.kind == RULE_PII_MASK

    def pii_matches(self, text: str) -> bool:
        return any(rule.detector(text) for rule in self.pii)

    def mask(self, text: str) -> tuple[str, bool]:
        changed = False
        for rule in self.pii:
            masked, matches = rule.apply(text)
            if matches:
                text = masked
                changed = True
        return text, changed


@dataclass(frozen=True)
class Group:
    position: str
    rules: tuple[Rule, ...]
    on_violation: str
    message: str

    @property
    def blocking(self) -> bool:
        return self.on_violation == ACTION_BLOCK


def _pii_rules(value: dict[str, Any]) -> tuple[ResolvedRedactionRule, ...]:
    out: list[ResolvedRedactionRule] = []
    for pii_type in value["types"]:
        if pii_type == "regex":
            try:
                re.compile(value["regex"])
            except re.error as e:
                raise ConfigError(
                    "guardrail_pattern_invalid", f"pii_mask regex {value['regex']!r}: {e}"
                ) from e
            rule = RedactionRule(pii_type="regex", strategy="mask", detector=value["regex"])
        else:
            rule = RedactionRule(pii_type=pii_type, strategy="mask")
        out.append(rule.resolve())
    return tuple(out)


def _rule(raw: dict[str, Any]) -> Rule:
    kind = raw["kind"]
    if kind == RULE_MAX_LENGTH:
        return Rule(kind=kind, max_length=int(raw["value"]))
    if kind == RULE_DENY_PATTERN:
        try:
            return Rule(kind=kind, pattern=re.compile(raw["value"]))
        except re.error as e:
            raise ConfigError(
                "guardrail_pattern_invalid", f"deny_pattern {raw['value']!r}: {e}"
            ) from e
    if kind == RULE_PII_MASK:
        value = raw["value"]
        regex = value.get("regex") if "regex" in value["types"] else None
        return Rule(kind=kind, pii=_pii_rules(value), pii_regex=regex)
    if kind == RULE_INJECTION_CHECK:
        return Rule(kind=kind, judge=raw["value"])
    raise ConfigError("guardrail_rule_unknown", f"가드레일 규칙 {kind!r} 는 모른다")


def parse_groups(config: AgentConfig) -> list[Group]:
    """injection_check 는 position tool 에만 온다. 다른 자리면 ConfigError 다."""
    groups: list[Group] = []
    for raw in config.guardrails:
        rules = tuple(_rule(r) for r in raw["rules"])
        if raw["position"] != POSITION_TOOL and any(r.kind == RULE_INJECTION_CHECK for r in rules):
            raise ConfigError(
                "guardrail_rule_position",
                f"injection_check 는 position tool 에만 온다({raw['position']!r})",
            )
        groups.append(
            Group(
                position=raw["position"],
                rules=rules,
                on_violation=raw["on_violation"],
                message=raw["message"] or DEFAULT_BLOCK_MESSAGE,
            )
        )
    return groups


class InputGuard:
    """position: input 그룹을 /chat 입구에서 검사한다."""

    def __init__(self, groups: list[Group]) -> None:
        self._groups = [g for g in groups if g.position == POSITION_INPUT]

    @property
    def empty(self) -> bool:
        return not self._groups

    def check(self, message: str, *, attachment: str | None = None) -> list[Violation]:
        """flag 위반 목록을 돌려준다. block 위반이 하나라도 있으면 GuardrailBlocked 다.

        attachment 는 첨부 글을 볼 때 그 이름이다. 길이 규칙을 건너뛰고 위반에 이름을 단다.
        """
        flagged: list[Violation] = []
        for group in self._groups:
            for rule in group.rules:
                if attachment is not None and rule.kind == RULE_MAX_LENGTH:
                    continue
                hit = rule.text_violates(message)
                if rule.is_pii and group.blocking:
                    # flag 면 PIIMiddleware 가 모델 호출 전에 가리므로 여기서는 block 만 본다.
                    hit = rule.pii_matches(message)
                if not hit:
                    continue
                if group.blocking:
                    raise GuardrailBlocked(
                        Violation(
                            POSITION_INPUT,
                            rule.kind,
                            ACTION_BLOCK,
                            message=group.message,
                            attachment=attachment,
                        )
                    )
                flagged.append(
                    Violation(POSITION_INPUT, rule.kind, ACTION_FLAG, attachment=attachment)
                )
        return flagged


class GuardrailPII(PIIMiddleware):
    """내장 PIIMiddleware(strategy=mask) 에 위치와 신호를 붙인 것이다.

    이름에 위치를 넣어 같은 PII 종류를 input·output 양쪽에 걸어도 미들웨어 이름이 겹치지 않는다.
    """

    def __init__(
        self,
        pii_type: str,
        *,
        position: str,
        detector: str | None = None,
        label: str | None = None,
    ) -> None:
        super().__init__(
            pii_type,
            strategy="mask",
            detector=detector,
            apply_to_input=position == POSITION_INPUT,
            apply_to_output=position == POSITION_OUTPUT,
            apply_to_tool_results=False,
        )
        self.position = position
        self._label = label or pii_type

    @property
    def name(self) -> str:
        return f"DaegamPII_{self.position}_{self._label}"

    def before_model(self, state: AgentState[Any], runtime: Runtime[Any]) -> dict[str, Any] | None:
        result = super().before_model(state, runtime)
        if result is not None:
            self._signal()
        return result

    def after_model(self, state: AgentState[Any], runtime: Runtime[Any]) -> dict[str, Any] | None:
        result = super().after_model(state, runtime)
        if result is not None:
            self._signal()
        return result

    def _signal(self) -> None:
        emit_violation(self.position, RULE_PII_MASK, ACTION_MASK, pii_type=self.pii_type)


def build_pii_middleware(groups: list[Group], position: str) -> list[AgentMiddleware]:
    """input(flag 만)·output 의 pii_mask 를 PIIMiddleware 로 만든다. 같은 종류는 한 번만 건다."""
    out: list[AgentMiddleware] = []
    seen: set[str] = set()
    regex_count = 0
    for group in groups:
        if group.position != position:
            continue
        if position == POSITION_INPUT and group.blocking:
            continue
        for rule in group.rules:
            if not rule.is_pii:
                continue
            for resolved in rule.pii:
                if resolved.pii_type == "regex":
                    regex_count += 1
                    out.append(
                        GuardrailPII(
                            "regex",
                            position=position,
                            detector=rule.pii_regex,
                            label=f"regex{regex_count}",
                        )
                    )
                    continue
                if resolved.pii_type in seen:
                    continue
                seen.add(resolved.pii_type)
                out.append(GuardrailPII(resolved.pii_type, position=position))
    return out


class OutputGuard(AgentMiddleware):
    """position: output 의 max_length·deny_pattern 이다. block 이면 응답을 바꾸고 턴을 끝낸다."""

    def __init__(self, groups: list[Group]) -> None:
        super().__init__()
        self._groups = [
            g
            for g in groups
            if g.position == POSITION_OUTPUT and any(not r.is_pii for r in g.rules)
        ]

    @property
    def name(self) -> str:
        return "DaegamOutputGuard"

    @property
    def empty(self) -> bool:
        return not self._groups

    @hook_config(can_jump_to=["end"])
    def after_model(self, state: AgentState[Any], runtime: Runtime[Any]) -> dict[str, Any] | None:
        messages = state["messages"]
        last = next((m for m in reversed(messages) if isinstance(m, AIMessage)), None)
        if last is None:
            return None
        text = last.text
        if not text:
            return None
        for group in self._groups:
            for rule in group.rules:
                if rule.is_pii or not rule.text_violates(text):
                    continue
                if not group.blocking:
                    emit_violation(POSITION_OUTPUT, rule.kind, ACTION_FLAG)
                    continue
                emit_violation(POSITION_OUTPUT, rule.kind, ACTION_BLOCK, message=group.message)
                replaced = last.model_copy(
                    update={"content": group.message, "tool_calls": [], "invalid_tool_calls": []}
                )
                return {"messages": [replaced], "jump_to": "end"}
        return None

    @hook_config(can_jump_to=["end"])
    async def aafter_model(
        self, state: AgentState[Any], runtime: Runtime[Any]
    ) -> dict[str, Any] | None:
        return self.after_model(state, runtime)


@dataclass(frozen=True)
class OutputFilter:
    """글을 바꾸는 position: output 규칙(pii_mask, block 인 max_length·deny_pattern)을 스트림에 낼
    글에 대는 것이다. 그래프 안의 훅과 같은 차례로 돈다. after_model 은 목록 역순이고
    build_guardrails 가 PII 를 OutputGuard 앞에 두므로 막기 검사가 먼저이고 가리기가 그 뒤다. 그래서
    스트림이 낸 글과 체크포인트의 글이 같다. flag 만 있는 규칙은 글을 바꾸지 않아 여기 들지 않는다.
    """

    groups: tuple[Group, ...]

    @classmethod
    def build(cls, groups: list[Group]) -> OutputFilter | None:
        """글을 바꾸는 output 그룹만 모은다. 없으면 None 이고 스트림은 조각을 그대로 흘린다."""
        changing = tuple(
            g
            for g in groups
            if g.position == POSITION_OUTPUT and (g.blocking or any(r.is_pii for r in g.rules))
        )
        return cls(changing) if changing else None

    def apply(self, text: str) -> str:
        """막기 규칙에 걸리면 그 그룹의 문구이고, 아니면 PII 를 가린 글이다."""
        blocked = self.block_message(text)
        return blocked if blocked is not None else self.mask(text)

    def block_message(self, text: str) -> str | None:
        """block 그룹의 max_length·deny_pattern 에 걸린 첫 그룹의 문구다. OutputGuard 와 같은
        차례다."""
        for group in self.groups:
            if group.blocking and any(rule.text_violates(text) for rule in group.rules):
                return group.message
        return None

    def mask(self, text: str) -> str:
        """pii_mask 규칙을 차례로 댄 글이다."""
        for group in self.groups:
            for rule in group.rules:
                if rule.is_pii:
                    text, _ = rule.mask(text)
        return text

    def mask_leaves(self, value: Any) -> Any:
        """구조화 결과의 문자열 잎마다 가린다. dict·list 의 모양은 그대로다."""
        if isinstance(value, str):
            return self.mask(value)
        if isinstance(value, dict):
            return {key: self.mask_leaves(item) for key, item in value.items()}
        if isinstance(value, list):
            return [self.mask_leaves(item) for item in value]
        return value


def edited_call(call: ToolCall, edited: Mapping[str, Any] | None) -> ToolCall:
    """승인자가 고친 호출이 같은 id 로 있으면 그 이름과 인자를 얹은 호출이고 없으면 그대로다.

    `edited` 는 HITL 상태 키의 값(`{call_id: {name, args}}`)이다. 실행 때(effective_call)와 재개
    턴이 다시 알리는 tool_call(stream.py)이 같이 쓴다.
    """
    call_id = call.get("id")
    action = edited.get(call_id) if edited and call_id else None
    if not isinstance(action, Mapping):
        return call
    return {**call, "name": action["name"], "args": action["args"]}


def error_result(call: Mapping[str, Any], text: str) -> ToolMessage:
    """도구를 부르지 않고 그 호출에 돌려주는 오류 결과다. 가드의 막기, 서브에이전트 시한, 웹 가드의
    거절이 쓴다."""
    return ToolMessage(content=text, name=call["name"], tool_call_id=call["id"], status="error")


def refusal_body(code: str, message: str, **extra: Any) -> str:
    """런타임 Tool 오류의 글이다. `{code, message}` JSON 이고 extra 의 None 이 아닌 칸을 더한다.
    거절(`refusal_result`)과 판정 모델 층의 막힌 결과(judge.py)가 같이 쓴다."""
    body: dict[str, Any] = {"code": code, "message": message}
    body.update({key: value for key, value in extra.items() if value is not None})
    return json.dumps(body, ensure_ascii=False)


def refusal_result(call: Mapping[str, Any], code: str, message: str, **extra: Any) -> ToolMessage:
    """런타임이 호출을 거절한 오류 결과다. 글은 `refusal_body` 다. 웹 가드(web_guard.py)와 바깥
    표시·거절 층(tool_safety.py)이 쓴다."""
    return error_result(call, refusal_body(code, message, **extra))


def effective_call(request: ToolCallRequest) -> ToolCall:
    """실제로 돌 호출이다. HITL 이 실행 순간 바꿔 끼울 고친 호출이 상태에 있으면 그것이고 아니면
    요청의 호출이다."""
    state = request.state if isinstance(request.state, Mapping) else {}
    return edited_call(request.tool_call, state.get(HITL_EDITED_CALLS_KEY))


class ToolGuard(AgentMiddleware):
    """position: tool 이다. 인자와 결과에 deny_pattern, 결과에 max_length·pii_mask(mask)."""

    def __init__(self, groups: list[Group]) -> None:
        super().__init__()
        self._groups = [g for g in groups if g.position == POSITION_TOOL]

    @property
    def name(self) -> str:
        return "DaegamToolGuard"

    @property
    def empty(self) -> bool:
        return not self._groups

    def args_violations(self, name: str, args: Mapping[str, Any]) -> list[Violation]:
        """호출 인자에 대는 deny_pattern 규칙이다. 걸린 차례대로 위반이고 block 이 나오면 거기서
        끝난다. 신호를 내지 않는다. 실행 때(wrap)와 결정 때(hitl.py 의 고친 인자)가 같이 쓴다."""
        args_text = json.dumps(dict(args), ensure_ascii=False, default=str)
        out: list[Violation] = []
        for group in self._groups:
            for rule in group.rules:
                if rule.kind != RULE_DENY_PATTERN or not rule.text_violates(args_text):
                    continue
                action = ACTION_BLOCK if group.blocking else ACTION_FLAG
                message = group.message if group.blocking else None
                out.append(
                    Violation(
                        POSITION_TOOL,
                        rule.kind,
                        action,
                        tool=name,
                        stage=STAGE_ARGS,
                        message=message,
                    )
                )
                if group.blocking:
                    return out
        return out

    def _check_args(self, call: ToolCall) -> ToolMessage | None:
        """위반을 신호로 내고 block 이면 그 문구를 오류 결과로 돌려준다."""
        for violation in self.args_violations(call["name"], call.get("args") or {}):
            emit_violation(**violation.to_dict())
            if violation.action == ACTION_BLOCK:
                return error_result(call, violation.message)
        return None

    def _check_result(self, name: str, result: Any) -> Any:
        if not isinstance(result, ToolMessage):
            return result
        text = result.text
        if not text:
            return result
        for group in self._groups:
            for rule in group.rules:
                if rule.is_pii:
                    masked, changed = rule.mask(text)
                    if changed:
                        emit_violation(
                            POSITION_TOOL, rule.kind, ACTION_MASK, tool=name, stage=STAGE_RESULT
                        )
                        text = masked
                        result = result.model_copy(update={"content": masked})
                    continue
                if not rule.text_violates(text):
                    continue
                if not group.blocking:
                    emit_violation(
                        POSITION_TOOL, rule.kind, ACTION_FLAG, tool=name, stage=STAGE_RESULT
                    )
                    continue
                emit_violation(
                    POSITION_TOOL,
                    rule.kind,
                    ACTION_BLOCK,
                    tool=name,
                    stage=STAGE_RESULT,
                    message=group.message,
                )
                # artifact 의 인용 출처도 막힌 결과의 것이라 함께 비운다
                return result.model_copy(
                    update={"content": group.message, "status": "error", "artifact": None}
                )
        return result

    def wrap_tool_call(
        self,
        request: ToolCallRequest,
        handler: Callable[[ToolCallRequest], ToolMessage | Command[Any]],
    ) -> ToolMessage | Command[Any]:
        call = effective_call(request)
        blocked = self._check_args(call)
        if blocked is not None:
            return blocked
        return self._check_result(call["name"], handler(request))

    async def awrap_tool_call(
        self,
        request: ToolCallRequest,
        handler: Callable[[ToolCallRequest], Awaitable[ToolMessage | Command[Any]]],
    ) -> ToolMessage | Command[Any]:
        call = effective_call(request)
        blocked = self._check_args(call)
        if blocked is not None:
            return blocked
        return self._check_result(call["name"], await handler(request))


@dataclass
class Guardrails:
    input: InputGuard
    tool: ToolGuard
    """도구 가드레일이다. 그래프의 미들웨어이자 결정의 고친 인자 검사기다. 비어 있어도 둔다."""
    middleware: list[AgentMiddleware]
    output_filter: OutputFilter | None = None
    """스트림에 낼 글에 대는 출력 필터다. 글을 바꾸는 output 규칙이 없으면 None 이다."""
    masks: tuple[Rule, ...] = ()
    """위치와 상관없는 pii_mask 규칙 전부다. 관측 이벤트의 본문이 지난다(events.py, P37)."""


def build_guardrails(config: AgentConfig, groups: list[Group] | None = None) -> Guardrails:
    """agent.json guardrails[] 를 입구 검사기, 그래프 미들웨어, 스트림의 출력 필터로 나눈다. 이미 푼
    groups 를 주면 다시 풀지 않는다(compile.py 가 판정 모델 층과 나눠 쓴다)."""
    if groups is None:
        groups = parse_groups(config)
    middleware: list[AgentMiddleware] = []
    middleware.extend(build_pii_middleware(groups, POSITION_INPUT))
    middleware.extend(build_pii_middleware(groups, POSITION_OUTPUT))
    output = OutputGuard(groups)
    if not output.empty:
        middleware.append(output)
    tool = ToolGuard(groups)
    if not tool.empty:
        middleware.append(tool)
    return Guardrails(
        input=InputGuard(groups),
        tool=tool,
        middleware=middleware,
        output_filter=OutputFilter.build(groups),
        masks=tuple(rule for group in groups for rule in group.rules if rule.is_pii),
    )
