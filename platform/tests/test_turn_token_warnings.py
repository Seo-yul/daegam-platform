"""턴 위임 토큰의 상태 경고 넷 (P53 7절, B3). Gateway 셋은 위임이 켜진 local 모드에서, Agent 하나는
위임이 켜진 토큰 인증 모드에서 나온다. 데이터베이스 없이 실제 문서 모델로 도는 검사다. 상태 API 를
거치는 검사는 tests/test_turn_gate_reapply.py 에 있다."""

from __future__ import annotations

import asyncio
import dataclasses
from types import SimpleNamespace
from typing import Any

import pytest

from daegam.config import Settings
from daegam.errors import ServiceError
from daegam.k8s.gates import pack_epoch
from daegam.services.agent_features import TURN_TOKENS
from daegam.services.agent_status import (
    TURN_TOKENS_UNSUPPORTED_WARNING,
    AgentStatusView,
    lacks_turn_tokens,
)
from daegam.services.data_plane_auth import KIND_GATEWAY, RENDER_EPOCHS
from daegam.services.gateway_status import (
    TURN_TOKENS_AUTHZ_FORBID,
    TURN_TOKENS_OWNER_CEDAR,
    turn_token_warnings,
)
from daegam.services.gateways import GatewayService
from daegam.store.agents import AgentRuntime
from tests.helpers.agents import template_spec
from tests.helpers.docs import agent_doc, effective_config, gateway_doc
from tests.test_turn_gates import CONFIGMAP

FORBID = 'forbid(principal, action, resource) when { principal.claim_groups.contains("x") };'
PERMIT = 'permit(principal, action, resource) when { principal.claim_tenant == "acme" };'
BOUND = pack_epoch(RENDER_EPOCHS[KIND_GATEWAY], bound=True)
AGENT_IMAGE = effective_config().images.agent_image


def _gateway(**kw: Any):
    """묶임을 싣는 렌더로 배포된 Gateway 다."""
    kw.setdefault("render_epoch", BOUND)
    return gateway_doc("gw-1", state="deployed", **kw)


def _pinned(policy: str | None) -> SimpleNamespace:
    return SimpleNamespace(authz_policy=policy)


def _codes(warnings: list[str]) -> list[str]:
    return [w.split(":", 1)[0] for w in warnings]


def test_a_plain_gateway_has_no_turn_token_warning():
    assert turn_token_warnings(_gateway(), _pinned(PERMIT)) == []
    assert turn_token_warnings(_gateway(), _pinned(None)) == []


def test_each_cause_names_its_own_warning():
    assert _codes(turn_token_warnings(_gateway(overrides=CONFIGMAP), _pinned(None))) == [
        TURN_TOKENS_OWNER_CEDAR
    ]
    assert _codes(turn_token_warnings(_gateway(), _pinned(f"{PERMIT}\n{FORBID}"))) == [
        TURN_TOKENS_AUTHZ_FORBID
    ]


@pytest.mark.parametrize(
    "doc",
    [
        _gateway(overrides=CONFIGMAP),
        _gateway(render_epoch=RENDER_EPOCHS[KIND_GATEWAY]),
    ],
    ids=["owner cedar", "no binding"],
)
def test_a_gateway_that_takes_no_turn_tokens_gets_no_forbid_warning(doc):
    """소유자 Cedar, 묶임 없는 렌더의 Gateway 는 토큰을 받지 않으므로 forbid 가
    위임을 거절할 일이 없다."""
    assert TURN_TOKENS_AUTHZ_FORBID not in _codes(turn_token_warnings(doc, _pinned(FORBID)))


@pytest.mark.parametrize(
    ("mode", "issuer", "on", "local"),
    [
        ("local", None, True, True),
        ("local", None, False, False),
        ("oidc", "https://idp.example", True, False),
        ("header", None, True, False),
    ],
)
def test_the_gateway_warnings_follow_minting_in_local_mode(mode, issuer, on, local):
    settings = dataclasses.replace(
        Settings(),
        auth_mode=mode,
        oidc_issuer=issuer,
        turn_tokens_enabled=on,
        data_plane_issuer="idp",
    )
    service = SimpleNamespace(cluster_service=SimpleNamespace(settings=settings))
    assert GatewayService._turn_tokens_on(service) is local  # type: ignore[arg-type]


# -- Agent ---------------------------------------------------------------------------------------


def _settings(**kw: Any) -> Settings:
    return dataclasses.replace(
        Settings(), **{"auth_mode": "local", "platform_issuer": "http://daegam.test", **kw}
    )


def _spec(*, gateway: bool = True, mcp: bool = False) -> dict[str, Any]:
    """Gateway gw-1 과, 원하면 MCP Server mcp-1 을 Tool 대상으로 둔 템플릿 spec. Gateway 를 빼면
    MCP Server 가 없을 때 tools 노드가 없다."""
    spec = template_spec(source_id="gw-1")
    graph = spec["graph"]
    if mcp:
        graph["nodes"].append(
            {
                "id": "tools-mcp",
                "type": "tools",
                "data": {"source": {"kind": "mcp_instance", "id": "mcp-1"}, "allow_tools": ["x"]},
            }
        )
        graph["edges"].append(
            {"id": "e-mcp", "source": "agent-main", "target": "tools-mcp", "kind": "tools"}
        )
    if not gateway:
        graph["nodes"] = [n for n in graph["nodes"] if n["id"] != "tools-calculator"]
        graph["edges"] = [e for e in graph["edges"] if e["target"] != "tools-calculator"]
    return spec


def _agent(
    features: tuple[str, ...] | None = ("cancel",),
    *,
    image: str = AGENT_IMAGE,
    state: str = "deployed",
    **spec: bool,
):
    runtime = None if features is None else AgentRuntime(image=image, features=list(features))
    return agent_doc(
        spec=_spec(**spec),
        state=state,
        effective_config=effective_config().model_dump(),
        runtime=runtime,
    )


def test_an_agent_whose_runtime_lacks_the_capability_warns():
    assert lacks_turn_tokens(_agent(), _settings())


@pytest.mark.parametrize(
    ("case", "settings", "agent"),
    [
        ("capability reported", {}, _agent((TURN_TOKENS,))),
        ("no record yet", {}, _agent(None)),
        ("record of another image", {}, _agent(image="registry.example/other:1")),
        ("not deployed", {}, _agent(state="draft")),
        ("minting off", {"turn_tokens_enabled": False}, _agent()),
        ("header mode", {"auth_mode": "header"}, _agent()),
        ("no tool sources", {}, _agent(gateway=False)),
        (
            "oidc, Gateway sources only",
            {"auth_mode": "oidc", "oidc_issuer": "https://idp.example", "data_plane_issuer": "idp"},
            _agent(),
        ),
    ],
)
def test_no_warning_where_the_agent_would_not_mint_anyway(case, settings, agent):
    assert not lacks_turn_tokens(agent, _settings(**settings)), case


def test_oidc_mode_warns_for_an_agent_with_an_mcp_server_source():
    oidc = _settings(auth_mode="oidc", oidc_issuer="https://idp.example")
    assert lacks_turn_tokens(_agent(mcp=True), oidc)


def _view(settings: Settings) -> AgentStatusView:
    async def none(*_a: Any) -> list[str]:
        return []

    async def set_phase(*_a: Any) -> None:
        return None

    return AgentStatusView(
        settings=settings,
        skills=SimpleNamespace(access_revoked=none),  # type: ignore[arg-type]
        agents=SimpleNamespace(set_phase=set_phase),  # type: ignore[arg-type]
        proxy=None,  # type: ignore[arg-type]
        client_for_doc=None,  # type: ignore[arg-type]
        with_pod_waiting=None,  # type: ignore[arg-type]
        pin_warnings=none,
        error_cls=ServiceError,
    )


@pytest.mark.parametrize(("features", "shown"), [(("cancel",), True), ((TURN_TOKENS,), False)])
def test_the_agent_status_finish_adds_the_warning(features, shown):
    out = asyncio.run(_view(_settings()).finish(_agent(features), {"warnings": [], "phase": None}))
    assert (TURN_TOKENS_UNSUPPORTED_WARNING in out["warnings"]) is shown
