"""P38 묶음 A 의 약속을 지키는 Agent 렌더 스냅샷 (P38 Tests).

게이트웨이를 끈 배포에서 Agent 파드 템플릿은 이 단계 앞과 한 글자도 다르지 않아야 한다. 여기 렌더는
입력이 모두 고정이라 언제 돌려도 같다. `platform/tests/fixtures/agent-pod-templates-before-p38.json`
은 이 단계 앞 커밋에서 이 함수가 낸 값이고 `test_llm_gateway_off.py` 가 지금 렌더와 맞댄다.
"""

from __future__ import annotations

from typing import Any

from daegam.k8s.templates.agent import (
    render_agent_deployment,
    render_agent_policy,
    render_agent_service,
    render_agent_token_secret,
)
from daegam.store.settings import LLMProvider
from tests.helpers.agent_runtime import minimal_agent_doc
from tests.helpers.docs import effective_config

OPENAI = LLMProvider(name="openai-main", kind="openai", api_key_secret="keys-openai")
ANTHROPIC = LLMProvider(name="claude", kind="anthropic", api_key_secret="keys-claude")
OPENROUTER = LLMProvider(name="router", kind="openrouter", api_key_secret="keys-router")
STUB = LLMProvider(name="local-stub", kind="stub")
PLATFORM_NS = "daegam-system"


def _deployment(inst: Any, **kw: Any) -> dict[str, Any]:
    return render_agent_deployment(
        inst,
        effective_config(),
        data_services={},
        agent_oauth=None,
        client_id_claim="cid",
        groups_claim=None,
        **kw,
    )


def snapshot() -> dict[str, Any]:
    """이름 → 렌더. Deployment 는 파드 템플릿과 함께 통째로 싣는다."""
    plain = minimal_agent_doc()
    keyed = minimal_agent_doc(
        llm_env={
            "openai-main": "DAEGAM_LLM_OPENAI_MAIN_API_KEY",
            "claude": "DAEGAM_LLM_CLAUDE_API_KEY",
        }
    )
    return {
        "deployment_no_providers": _deployment(plain, providers=[]),
        "deployment_providers": _deployment(
            keyed, providers=[OPENAI, ANTHROPIC, OPENROUTER, STUB], instance_token="inst-token"
        ),
        "deployment_knowledge": _deployment(
            keyed,
            providers=[ANTHROPIC],
            knowledge_providers=[(0, OPENAI), (1, OPENAI)],
            instance_token="inst-token",
        ),
        "deployment_gateway_token_telemetry": _deployment(
            plain,
            providers=[OPENAI],
            gateway_token=True,
            telemetry_ingest=True,
            instance_token="inst-token",
        ),
        "token_secret": render_agent_token_secret(plain, "inst-token", mcp_token="m.c.p"),
        "token_secret_gateway": render_agent_token_secret(
            plain, "inst-token", mcp_token="m.c.p", gateway_token="g.w"
        ),
        "service": render_agent_service(plain),
        "policy": render_agent_policy(plain, PLATFORM_NS),
    }
