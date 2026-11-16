"""Agent 캔버스의 템플릿 (P72 2.1절). `GET /agents/templates` 가 그대로 내고 캔버스가 불러온다."""

from __future__ import annotations

import copy
from typing import Any

TEMPLATE_ID = "support-agent"

# "단일 상담 에이전트". agent-runtime/schema/fixtures/support-agent.json 과 같은 그래프다. provider·
# 도구 소스·Database 는 테넌트마다 다르므로 비워 두고 validate 가 그 자리를 표시한다. 모델은 모델
# 노드 하나이고 메인 에이전트가 붙는다(D28)
_SUPPORT_AGENT: dict[str, Any] = {
    "id": TEMPLATE_ID,
    "display_name": "단일 상담 에이전트",
    "description": (
        "입구, 메인 에이전트 하나와 그 모델 노드, Gateway Tool 소스 하나(나눗셈은 승인 뒤 실행), "
        "Postgres 메모리, 입력 가드레일 하나로 이뤄진 상담 에이전트다. provider 와 Tool 소스, "
        "Database 를 선택하면 바로 배포할 수 있다."
    ),
    "graph": {
        "nodes": [
            {
                "id": "entry",
                "type": "entry",
                "data": {
                    "session_ttl_seconds": 3600,
                    "max_turns": 50,
                    "rate_limit": {"per_user_per_minute": 20},
                    "max_concurrent_sessions": 100,
                    "max_input_chars": 4000,
                    "greeting": "안녕하세요. 무엇을 도와드릴까요?",
                },
            },
            {
                "id": "agent-main",
                "type": "agent",
                "data": {
                    "role": "main",
                    "name": "Agent",
                    "description": "고객 상담을 맡는 메인 에이전트다.",
                    "system_prompt": (
                        "너는 고객 상담 에이전트다. 계산이 필요하면 Tool 을 쓰고, 나눗셈은 승인을 "
                        "받은 "
                        "뒤에만 실행한다. 모르는 것은 모른다고 답한다."
                    ),
                    "context": {
                        "compaction": {"trigger": {"fraction": 0.85}, "keep": {"fraction": 0.1}}
                    },
                    "max_tool_calls_per_turn": 8,
                    "turn_timeout_seconds": 120,
                    "retries": 2,
                    "response_format": "text",
                    "language": "ko",
                    "builtin_tools": {"filesystem": True},
                },
            },
            {
                "id": "model-main",
                "type": "model",
                "data": {
                    "name": "기본 모델",
                    "provider": "",
                    "model": None,
                    "max_output_tokens": 2048,
                    "reasoning": {"effort": "medium"},
                    "temperature": None,
                    "fallback": None,
                },
            },
            {
                "id": "tools-calculator",
                "type": "tools",
                "data": {
                    "source": {"kind": "gateway_instance", "id": ""},
                    "allow_tools": ["Add", "Divide"],
                    "approval": {"Divide": ["approve", "reject"]},
                    "timeouts": {"Divide": 30},
                },
            },
            {
                "id": "memory",
                "type": "memory",
                "data": {"checkpointer": {"data_service_id": ""}},
            },
            {
                "id": "guardrail-input",
                "type": "guardrail",
                "data": {
                    "position": "input",
                    "rules": [
                        {"kind": "max_length", "value": 4000},
                        {"kind": "pii_mask", "value": {"types": ["credit_card"]}},
                    ],
                    "on_violation": "block",
                    "message": "카드 번호 같은 민감 정보는 보낼 수 없다. 지우고 다시 보내 달라.",
                },
            },
        ],
        "edges": [
            {"id": "e-entry", "source": "entry", "target": "agent-main", "kind": "entry"},
            {"id": "e-model", "source": "agent-main", "target": "model-main", "kind": "model"},
            {
                "id": "e-tools",
                "source": "agent-main",
                "target": "tools-calculator",
                "kind": "tools",
            },
            {"id": "e-memory", "source": "agent-main", "target": "memory", "kind": "memory"},
            {
                "id": "e-guardrail",
                "source": "agent-main",
                "target": "guardrail-input",
                "kind": "guardrail",
            },
        ],
    },
    "layout": {
        # 포트 방향(D28)을 그대로 따른다. 메모리·모델은 에이전트 위(메모리가 왼쪽, 모델이 오른쪽),
        # MCP 는 오른쪽, 가드레일은 아래다. 어긋나게 두면 간선이 카드를 돌아 나가며 서로 넘는다
        "entry": {"x": 80, "y": 220},
        "agent-main": {"x": 400, "y": 220},
        "memory": {"x": 256, "y": 60},
        "model-main": {"x": 544, "y": 60},
        "tools-calculator": {"x": 780, "y": 220},
        "guardrail-input": {"x": 400, "y": 420},
    },
    "service": {
        "budget": {"session_tokens": 200000, "daily_tokens": None},
        "observability": {"record_bodies": False},
        "evaluation": None,
    },
}


def templates() -> list[dict[str, Any]]:
    """캔버스 템플릿. 지금은 하나다."""
    return [copy.deepcopy(_SUPPORT_AGENT)]
