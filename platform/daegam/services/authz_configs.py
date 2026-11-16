"""MCPAuthzConfig 를 먼저 적용하고 받아들여지기를 기다린다 (P50 8절, G8).

설정이 틀리면 vMCP·앞단은 앞의 정책으로 계속 돈다. 그래서 배포·다시 렌더하기·인증 재적용은 설정을
먼저 적용하고, 오퍼레이터가 그 세대를 보고 `Valid=True` 를 적을 때까지 기다린 뒤 그것을 참조하는 CR
을 적용한다. `Valid=False` 면 오퍼레이터의 글로 배포를 멈춘다. 지워지는 중(Terminating)인 설정은
그 CR 이 사라질 때까지 남으므로(F0) 기다리다가 시간이 다 되면 다음 시도로 미룬다.
"""

from __future__ import annotations

import asyncio
import time
from typing import Any

from starlette.concurrency import run_in_threadpool

from daegam.errors import ServiceError
from daegam.k8s.client import KubeClient
from daegam.k8s.templates.authz import AUTHZ_CONFIG_KIND, authz_config_name
from daegam.k8s.templates.common import TOOLHIVE_API
from daegam.services.proxy_accounts import apply_proxy_doc

# 설정이 받아들여지기를 기다리는 시간(초)과 다시 보는 간격. 인증 재적용은 한 틱에 여러 문서를 돌므로
# 짧게 기다리고, 못 기다린 문서는 백오프 뒤에 다시 본다
AUTHZ_WAIT_SECONDS = 30.0
REAPPLY_AUTHZ_WAIT_SECONDS = 5.0
AUTHZ_POLL_SECONDS = 0.5


def authz_verdict(live: dict[str, Any] | None) -> tuple[bool | None, str]:
    """(받아들였는가, 오퍼레이터의 글). 지금 세대를 아직 보지 않았거나 지워지는 중이면 None 이다."""
    if live is None:
        return None, "not found"
    meta = live.get("metadata") or {}
    if meta.get("deletionTimestamp"):
        return None, "terminating"
    status = live.get("status") or {}
    if status.get("observedGeneration") != meta.get("generation"):
        return None, "not observed yet"
    for cond in status.get("conditions") or []:
        if cond.get("type") == "Valid":
            return cond.get("status") == "True", str(cond.get("message") or "")
    return None, "no Valid condition"


async def wait_authz_valid(
    client: KubeClient,
    doc: dict[str, Any],
    error_cls: type[ServiceError],
    *,
    timeout: float = AUTHZ_WAIT_SECONDS,
) -> None:
    meta = doc["metadata"]
    deadline = time.monotonic() + timeout
    while True:
        live = await run_in_threadpool(
            client.get, doc["apiVersion"], AUTHZ_CONFIG_KIND, meta["name"], meta["namespace"]
        )
        ok, message = authz_verdict(live)
        if ok is True:
            return
        if ok is False:
            raise error_cls(
                422, "authz_invalid", f"MCPAuthzConfig {meta['name']} was refused: {message}"
            )
        if time.monotonic() >= deadline:
            raise error_cls(
                503, "authz_not_ready", f"MCPAuthzConfig {meta['name']} is not valid yet: {message}"
            )
        await asyncio.sleep(AUTHZ_POLL_SECONDS)


async def apply_in_order(
    client: KubeClient,
    docs: list[dict[str, Any]],
    error_cls: type[ServiceError],
    *,
    wait: float = AUTHZ_WAIT_SECONDS,
) -> None:
    """문서를 순서대로 적용한다. MCPAuthzConfig 를 적용하면 받아들여질 때까지 기다린 뒤 다음으로
    간다. 원격 프록시는 오퍼레이터가 전에 만든 RoleBinding 을 지운다(`apply_proxy_doc`)."""
    for doc in docs:
        await apply_proxy_doc(client, doc)
        if doc.get("kind") == AUTHZ_CONFIG_KIND:
            await wait_authz_valid(client, doc, error_cls, timeout=wait)


async def apply_docs(
    client: KubeClient,
    docs: list[dict[str, Any]],
    final: dict[str, Any],
    error_cls: type[ServiceError],
    *,
    authz_wait: float = AUTHZ_WAIT_SECONDS,
) -> dict[str, Any]:
    """렌더한 묶음을 순서대로 적용하고, 마지막 CR 자리에는 overrides 를 얹은 final
    (`AdvancedEditor.stored_final`)을 적용해 그 결과를 돌려준다. final 은 부르는 쪽이 먼저
    만들므로 저장된 overrides 가 거부되면 아무것도 보내지 않는다. 앞의 MCPAuthzConfig 는 받아들여질
    때까지 기다린 뒤 다음으로 간다(`apply_in_order`). Gateway 의 vMCP 와 MCP Server 의 MCPServer 가
    쓴다."""
    await apply_in_order(client, docs[:-1], error_cls, wait=authz_wait)
    return await run_in_threadpool(client.apply, final)


async def delete_authz_config(client: KubeClient, cr_name: str, namespace: str) -> None:
    """그 CR 의 `<CR 이름>-authz` 를 이름으로 지운다. 설정은 CR 보다 먼저 적용되므로 첫 적용이 그
    사이에 멈췄거나 소유자 없이 만들었으면 남는다. 없으면 아무 일도 없다."""
    await run_in_threadpool(
        client.delete, TOOLHIVE_API, AUTHZ_CONFIG_KIND, authz_config_name(cr_name), namespace
    )
