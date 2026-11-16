"""MCP Server 의 배포 스냅샷 (P50 1절).

MCPServer CR 은 Backend API 의 endpoint·auth·자격증명 Secret·soap:header 값 Secret 과 바인딩한
Database 의 주소·사용자·비밀번호 Secret 을 공유 문서에서 받는다(`render_mcpserver`). 배포는 그 값을
`MCPServer.deployed_inputs` 에 적고, 배포한 문서의 재렌더는 현재 공유 문서 대신 이 스냅샷으로
Backend API·Database 문서를 다시 지어 같은 렌더 함수에 넘긴다. 그래서 공유 문서를 고쳐도
재배포하기 전에는 돌고 있는 서버의 CR 에 반영되지 않는다.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import datetime
from typing import Any

from daegam.store.backend_apis import BackendAPI
from daegam.store.data_services import DataService
from daegam.store.mcp_servers import (
    BackendInputs,
    BindingInputs,
    MCPServerInputs,
    MCPServerRevision,
)
from daegam.store.tenants import Binding

# Backend API·Database 문서와 스냅샷이 같은 이름으로 나눠 갖는 칸
_BACKEND_FIELDS = frozenset(BackendInputs.model_fields)
_BINDING_FIELDS = frozenset(BindingInputs.model_fields) - {"data_service_id"}


def inputs_from_documents(
    rev: MCPServerRevision,
    backends: Sequence[BackendAPI],
    services: Mapping[str, DataService],
    *,
    deploying_since: datetime | None,
) -> MCPServerInputs:
    """배포가 읽은 현재 공유 문서로 만든 스냅샷. backends 는 리비전 backends 순서이고
    deploying_since 는 그 배포의 표시다. 세션 저장소의 TLS(`session_tls`)는 렌더한 뒤에 적는다."""
    return MCPServerInputs(
        backends=[BackendInputs(**bp.model_dump(include=_BACKEND_FIELDS)) for bp in backends],
        bindings=[
            _binding_inputs(services[b.data_service_id]) for b in _first_bindings(rev.bindings)
        ],
        deploying_since=deploying_since,
        soap_headers_secrets={
            bp.id: bp.soap_headers_secret for bp in backends if bp.soap_headers_secret
        },
    )


def snapshot_fits(
    inputs: MCPServerInputs | None, rev: MCPServerRevision, *, deploying_since: datetime | None
) -> bool:
    """스냅샷이 지금 배포의 것이고 핀된 리비전의 backend 순서와 바인딩을 모두 덮는가. 아니면 없는
    것으로 본다. 표시가 다르면 이 스냅샷을 적은 배포 뒤에 다른 배포가 있었던 것이다."""
    if inputs is None or inputs.deploying_since != deploying_since:
        return False
    if [b.id for b in inputs.backends] != [s.backend_profile_id for s in rev.backends]:
        return False
    bound = {b.data_service_id for b in _first_bindings(rev.bindings)}
    return bound <= {b.data_service_id for b in inputs.bindings}


def backend_apis_from(inputs: MCPServerInputs, tenant: str) -> list[BackendAPI]:
    """스냅샷의 Backend API 들. 렌더가 읽는 값 말고는 이름뿐이다."""
    return [
        BackendAPI(
            tenant=tenant,
            display_name=b.name,
            soap_headers_secret=inputs.soap_headers_secrets.get(b.id),
            **b.model_dump(),
        )
        for b in inputs.backends
    ]


def data_services_from(inputs: MCPServerInputs, tenant: str) -> dict[str, DataService]:
    """스냅샷의 바인딩 Database 들. data_service_id 마다 문서다."""
    return {
        b.data_service_id: DataService(
            id=b.data_service_id,
            tenant=tenant,
            display_name=b.name,
            **b.model_dump(include=_BINDING_FIELDS),
        )
        for b in inputs.bindings
    }


def session_tls_of(cr: dict[str, Any]) -> bool:
    """MCPServer 의 세션 저장소가 TLS 로 렌더됐는가 (P61 3절). 배포가 이것을 스냅샷에 적는다."""
    return "tls" in ((cr.get("spec") or {}).get("sessionStorage") or {})


def _first_bindings(bindings: Sequence[Binding]) -> list[Binding]:
    """Database 마다 첫 바인딩. 같은 Database 를 두 env_prefix 로 묶어도 한 번이고, 그 env 는
    어느 접두어로 읽어도 같다."""
    seen: set[str] = set()
    out: list[Binding] = []
    for b in bindings:
        if b.data_service_id not in seen:
            seen.add(b.data_service_id)
            out.append(b)
    return out


def _binding_inputs(ds: DataService) -> BindingInputs:
    return BindingInputs(data_service_id=ds.id, **ds.model_dump(include=_BINDING_FIELDS))
