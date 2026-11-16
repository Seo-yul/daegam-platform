"""라우터 공통 헬퍼. 오류 응답 본문은 `{"detail": {"code", "detail", ...}}` 한 형식이다.

서비스 계층 오류(`ServiceError` 자손인 `InstanceError`·`AgentError` 등)는 라우터가 잡지 않는다.
daegam/main.py 의 전역 예외 핸들러가 같은 형식으로 낸다. 라우터 자신이 만드는 오류만 여기 함수로
만든다. `error` 의 구현은 daegam/errors.py 에 있고 여기서 재수출한다. 응답 모델(`schemas/`)과
`_out` 은 리소스마다 다르므로 각 라우터에 둔다.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Sequence
from typing import Any, Protocol

from fastapi import HTTPException, Request, UploadFile
from pydantic import BaseModel

from daegam.errors import error, unauthorized
from daegam.secrets import SecretStore
from daegam.services.references import TargetKind
from daegam.store.agents import MongoAgentStore
from daegam.units import MIB

__all__ = [
    "TENANT_RE",
    "audit_target",
    "check_tenant",
    "error",
    "forbidden",
    "not_found",
    "not_null",
    "or_404",
    "patched_fields",
    "read_capped",
    "references_out",
    "require_secret_store",
    "unauthorized",
]

# 테넌트 이름. 헤더·경로로 들어오는 값이라 K8s 이름 규칙(daegam/names.py)보다 느슨하다
TENANT_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")


def forbidden(detail: str, *, code: str = "forbidden", **extra: Any) -> HTTPException:
    return error(403, code, detail, **extra)


def not_found(detail: str, **extra: Any) -> HTTPException:
    return error(404, "not_found", detail, **extra)


def patched_fields(patch: BaseModel) -> set[str]:
    """PATCH 는 보낸 필드만 바꾼다. `model_fields_set` 이 그 목록이고 비면 422 empty_patch.
    기본값과 "보내지 않음" 을 가르는 유일한 방법이라 일곱 라우터가 같은 규칙을 쓴다."""
    sent = patch.model_fields_set
    if not sent:
        raise error(422, "empty_patch", "no fields to update")
    return set(sent)


def patched_values(
    patch: BaseModel, sent: set[str], *, skip: tuple[str, ...] = ("name",)
) -> dict[str, Any]:
    """보낸 칸과 그 값. 서비스의 수정 메서드가 받는 모양이다. `skip` 은 라우터가 따로 본 칸이고
    `name` 은 바꿀 수 없어 라우터가 같은지만 본다(D24)."""
    return {k: getattr(patch, k) for k in sent if k not in skip}


def not_null[T](value: T | None, field: str, *, code: str = "invalid_value") -> T:
    """PATCH 로 보낸 필드에 null 은 못 쓴다(값을 지우는 뜻이 아니라 실수다). 오류 `code` 는
    리소스마다 이미 다른 값을 쓰고 있어 인자로 받는다."""
    if value is None:
        raise error(422, code, f"{field} cannot be null")
    return value


class AgentBound(Protocol):
    """한 Agent 에 묶인 기록이다. 테넌트를 가로지르는 `/me/...` 목록이 낸다."""

    agent_id: str
    tenant: str


async def with_agent_names[T: AgentBound](
    store: MongoAgentStore, items: Sequence[T], out: Callable[[T], dict[str, Any]]
) -> list[dict[str, Any]]:
    """테넌트를 가로지르는 기록(out 으로 낸 것)에 Agent 이름을 붙인다. Agent 는 한 번에 읽고
    테넌트가 맞는 것만 쓴다. 지운 Agent 의 기록은 이름이 null 이다."""
    agents = await store.get_many({item.agent_id for item in items})

    def named(item: T) -> dict[str, Any]:
        agent = agents.get(item.agent_id)
        name = agent.name if agent is not None and agent.tenant == item.tenant else None
        return {**out(item), "agent_name": name}

    return [named(item) for item in items]


def or_404[T](doc: T | None, what: str, doc_id: str) -> T:
    """스토어에서 못 찾았으면 404. 라우터 일곱이 같은 네 줄을 각자 적고 있었다.

    `what` 은 사람이 읽는 리소스 이름이다("backend api", "mcp server"). 메시지 꼴을 여기 한 곳에
    두면 리소스마다 다르게 적히는 일이 없다."""
    if doc is None:
        raise not_found(f"{what} {doc_id} not found")
    return doc


async def read_capped(file: UploadFile, limit: int) -> bytes:
    """업로드를 limit 바이트까지 읽는다. 한 바이트 더 읽어 넘었으면 413 too_large 다. 상한이 MiB 로
    나눠떨어지지 않으면 바이트로 적는다."""
    data = await file.read(limit + 1)
    if len(data) > limit:
        size = f"{limit // MIB} MiB" if limit % MIB == 0 else f"{limit} bytes"
        raise error(413, "too_large", f"upload is over {size}")
    return data


async def references_out(
    request: Request, kind: TargetKind, resource_id: str, *, tenant: str
) -> dict[str, Any]:
    """`GET /{id}/references` 본문 (D43). 아홉 라우터가 같은 모양을 쓰므로 `_out` 과 달리 여기
    한 곳에 둔다. 읽기 권한과 404 는 부르는 쪽이 자기 리소스의 GET 과 똑같이 먼저 본다."""
    refs = await request.app.state.references.used_by(kind, resource_id, tenant=tenant)
    return {"used_by": [r.model_dump(mode="json") for r in refs]}


def audit_target(request: Request, resource_id: str, tenant: str) -> None:
    """감사 행의 resource_id 와 tenant 를 이 대상의 것으로 둔다. 경로에 id 가 없거나 여럿이고
    테넌트가 없는 라우트가 쓴다(개인 토큰 발급·회수 P32 12절, OAuth 연결 끊기 P33 12절). 그래야
    테넌트 감사 화면에 잡힌다."""
    request.state.audit_resource_id = resource_id
    request.state.tenant = tenant


def audit_read(request: Request) -> None:
    """성공한 읽기도 감사에 남긴다. 경로만으로는 가를 수 없고 호출자에 따라 남길지가 갈리는
    읽기가 쓴다(승인자가 남의 세션 파일을 다운로드하는 자리, P36 10절)."""
    request.state.audit_read = True


def one_line(value: str) -> str | None:
    """앞뒤 공백을 뗀 한 줄 값. 비었거나 공백·제어 문자가 있으면 None 이다. 화면에서 넣는 키와
    Client Secret 이 함께 쓴다. 거절의 코드는 부르는 쪽이 정한다."""
    value = value.strip()
    if not value or any(ch.isspace() or ord(ch) < 32 for ch in value):
        return None
    return value


def require_secret_store(request: Request, provider: str, *, verb: str = "written") -> SecretStore:
    """키 Secret(provider 키, Teams 봇의 Client Secret)을 다룰 저장소. home 클러스터가 없으면 422
    no_secret_store 다.
    키 넣기·연결 확인(`api/settings`)과 스펙 job 생성(`api/spec_generator`)이 같은 응답을 내도록
    여기 한 번 적는다. verb 는 문구의 "written"·"read" 자리다."""
    store = request.app.state.cluster_service.secret_store()
    if store is None:
        raise error(
            422,
            "no_secret_store",
            f"home cluster is not available, so the key Secret cannot be {verb}",
            provider=provider,
        )
    return store


def check_tenant(tenant: str, *, shown: str | None = None) -> str:
    """테넌트 이름. 규칙에 맞지 않으면 400 invalid_tenant.

    경로에서 오면 그 값을 그대로 보고, 헤더에서 오면 정규화 전 원문을 오류 detail 에 보이는
    편이 낫다. 그 원문을 `shown` 으로 넘긴다."""
    if not TENANT_RE.fullmatch(tenant):
        raise error(400, "invalid_tenant", shown if shown is not None else tenant)
    return tenant
