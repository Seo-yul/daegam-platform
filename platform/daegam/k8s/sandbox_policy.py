"""네임스페이스 정책의 다시 읽기 (P40 4절).

열기는 샌드박스를 만들기 전에, 스윕은 샌드박스가 있는 네임스페이스마다 그 네임스페이스의
NetworkPolicy 를 성질로 본다. 이름이나 전체 모양을 맞대지 않는다. 다른 이가 둔 정책도 샌드박스를
고르면 함께 보아야 하기 때문이다.

- 격리 정책 `daegam-sandbox-isolation` 이 있고, 샌드박스 라벨을 고르며, 두 정책 종류
  (Ingress·Egress)를 다 갖고 규칙이 없다.
- 샌드박스를 고르는 다른 정책은 ingress 규칙도 egress 규칙도 없다. 예외는 그 Agent 의 정책
  `daegam-sandbox-<Agent id>` 하나이고, 그 Agent 파드에서 8080 으로 오는 ingress 만 가져야 한다.
  테넌트 정책 `daegam-tenant-ingress` 가 샌드박스를 빼지 않았으면 이 규칙에 걸린다.
- 샌드박스를 고르지 않는 정책은 보지 않는다. 테넌트 정책에 출처를 더해도 열기를 막지 않는다.

거절은 그 정책의 이름을 싣고(503 `sandbox_namespace_policy`), 클러스터의 판정은 무효로 하지
않는다.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any

from daegam.k8s.templates.agent import selector_labels
from daegam.k8s.templates.sandbox import (
    ISOLATION_POLICY_NAME,
    agent_sandbox_ingress,
    agent_sandbox_policy_name,
    sandbox_labels,
    sandbox_selector,
)
from daegam.store.agents import Agent

POLICY_TYPES = frozenset({"Ingress", "Egress"})


@dataclass(frozen=True)
class PolicyRefusal:
    """열기를 막는 정책과 그 까닭. 까닭은 영어이고 모델에게 도구 오류로 간다."""

    policy: str
    reason: str


def selector_matches(selector: Mapping[str, Any] | None, labels: Mapping[str, str]) -> bool:
    """쿠버네티스 LabelSelector 가 labels 에 맞는가. 빈 셀렉터는 모두에 맞는다. 모르는 연산자는
    맞지 않는 것으로 본다(그 정책은 샌드박스를 고르지 않는 것이 된다). 다시 읽기는 모르는 연산자를
    쓰는 정책을 거절하지 않지만, API 서버가 받는 연산자는 이 넷뿐이다."""
    selector = selector or {}
    for key, value in (selector.get("matchLabels") or {}).items():
        if labels.get(key) != value:
            return False
    for expr in selector.get("matchExpressions") or []:
        key, op = expr.get("key"), expr.get("operator")
        values = set(expr.get("values") or [])
        present = key in labels
        if op == "In":
            ok = present and labels[key] in values
        elif op == "NotIn":
            ok = not present or labels[key] not in values
        elif op == "Exists":
            ok = present
        elif op == "DoesNotExist":
            ok = not present
        else:
            ok = False
        if not ok:
            return False
    return True


def _name(policy: Mapping[str, Any]) -> str:
    return str((policy.get("metadata") or {}).get("name") or "")


def _rules(spec: Mapping[str, Any], field: str) -> list[Any]:
    """정책의 ingress 나 egress 규칙. 빈 규칙(`{}`)도 모두를 여는 규칙이라 센다."""
    return list(spec.get(field) or [])


def check_namespace_policies(
    policies: Iterable[Mapping[str, Any]], *, agent: Agent, labels: Mapping[str, str]
) -> PolicyRefusal | None:
    """한 네임스페이스의 정책을 성질로 본다. labels 는 열려는(스윕이면 이미 있는) 샌드박스 파드의
    라벨이다. 막는 것이 없으면 None 이다."""
    items = list(policies)
    isolation = next((p for p in items if _name(p) == ISOLATION_POLICY_NAME), None)
    if isolation is None:
        return PolicyRefusal(ISOLATION_POLICY_NAME, "the sandbox isolation policy is missing")
    spec = isolation.get("spec") or {}
    selector = spec.get("podSelector")
    if not (selector_matches(selector, sandbox_selector()) and selector_matches(selector, labels)):
        return PolicyRefusal(ISOLATION_POLICY_NAME, "it does not select every sandbox pod")
    if not POLICY_TYPES <= set(spec.get("policyTypes") or []):
        return PolicyRefusal(ISOLATION_POLICY_NAME, "it must close both ingress and egress")
    if _rules(spec, "ingress") or _rules(spec, "egress"):
        return PolicyRefusal(ISOLATION_POLICY_NAME, "it must hold no ingress or egress rule")
    own = agent_sandbox_policy_name(agent.id)
    for policy in items:
        name = _name(policy)
        spec = policy.get("spec") or {}
        if name == ISOLATION_POLICY_NAME or not selector_matches(spec.get("podSelector"), labels):
            continue
        if name == own:
            refusal = _check_agent_policy(name, spec, agent)
            if refusal is not None:
                return refusal
            continue
        if _rules(spec, "ingress") or _rules(spec, "egress"):
            return PolicyRefusal(name, "it selects sandbox pods and allows traffic to or from them")
    return None


def check_agent_namespace(
    policies: Iterable[Mapping[str, Any]], *, agent: Agent, platform_id: str
) -> PolicyRefusal | None:
    """그 Agent 가 열 샌드박스 파드의 라벨로 `check_namespace_policies` 를 본다. 열기와 스윕이
    쓴다. 샌드박스 id 는 라벨 하나일 뿐 어느 셀렉터도 읽지 않으므로 자리표다."""
    labels = sandbox_labels(
        tenant=agent.tenant, agent_id=agent.id, sandbox_id="read-back", platform_id=platform_id
    )
    return check_namespace_policies(policies, agent=agent, labels=labels)


def _check_agent_policy(name: str, spec: Mapping[str, Any], agent: Agent) -> PolicyRefusal | None:
    """Agent 의 정책은 그 Agent 파드에서 8080 으로 오는 ingress 하나만 갖는다."""
    if _rules(spec, "egress"):
        return PolicyRefusal(name, "the Agent's sandbox policy must hold no egress rule")
    if _rules(spec, "ingress") != agent_sandbox_ingress(agent):
        expected = selector_labels(agent)
        return PolicyRefusal(
            name, f"the Agent's sandbox policy may only allow its pods {expected} on port 8080"
        )
    return None
