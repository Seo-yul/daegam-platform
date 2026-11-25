"""원격 프록시의 역할 없는 ServiceAccount (P50 10절, G10).

플랫폼이 렌더하는 MCPRemoteProxy(MCP Server 의 옛 노출 앞단)는 테넌트 네임스페이스의 ServiceAccount
`daegam-remote-proxy`(`services/tenancy.py`)로 돈다. 그 계정에는 Role 이 없어 오퍼레이터가
프록시마다 만들던, 네임스페이스의 Secret 을 읽는 Role 이 필요 없다(F0 실측).
게이트(`Feature.ROLELESS_SA`)를 지나지 못한 클러스터의 프록시는 오늘의 꼴(serviceAccount 없음)로
렌더한다. 전에 만든 허브 remote 항목과 그 복제본, proxy 모드 게시의 프록시는 렌더하지 않으므로
적용돼 있는 꼴 그대로 돌고, 옮기기 단계가 그 프록시와 RoleBinding 을 지운다
(`services/entry_conversion.py`).

오퍼레이터는 spec.serviceAccount 를 받은 프록시의 옛 `<프록시>-remote-proxy-runner`
ServiceAccount 와 Role 을 지우지 않는다. 플랫폼은 그 RoleBinding 만 지운다(지금 권한으로 되는
일이다). 묶이지 않은 Role 과 계정은 아무것도 하지 못한 채 남는다.
"""

from __future__ import annotations

import logging
from typing import Any

from starlette.concurrency import run_in_threadpool

from daegam.k8s.client import RBAC_API, KubeClient
from daegam.k8s.gates import Feature, passing_features
from daegam.k8s.labels import LABEL_TENANT
from daegam.k8s.templates.common import owned_by
from daegam.k8s.templates.remote_proxy import runner_binding_name
from daegam.services.tenancy import (
    REMOTE_PROXY_SERVICE_ACCOUNT,
    remote_proxy_service_account_manifest,
)
from daegam.store.clusters import Cluster

logger = logging.getLogger("daegam.proxy_accounts")


def proxy_service_account(cluster: Cluster | None) -> str | None:
    """원격 프록시가 쓸 ServiceAccount. 게이트를 지나지 못했으면(또는 클러스터를 모르면) None 이라
    오늘의 꼴이다."""
    if cluster is None or Feature.ROLELESS_SA not in passing_features(cluster):
        return None
    return REMOTE_PROXY_SERVICE_ACCOUNT


async def apply_proxy_doc(client: KubeClient, doc: dict[str, Any]) -> dict[str, Any]:
    """문서 하나를 적용한다. 역할 없는 ServiceAccount 로 도는 MCPRemoteProxy 면 그 계정이 있는지
    먼저 보고, 없으면 만들어 본다. 만들지 못하면 serviceAccount 를 빼고(오늘의 꼴) 적용한다. 없는
    계정을 가리키면 프록시가 뜨지 못하기 때문이다. 계정으로 돌면 오퍼레이터가 전에 만든 그 프록시의
    RoleBinding 을 지운다. 이름으로 찾고 그 프록시가 소유한 것인지 확인한다."""
    meta = doc.get("metadata") or {}
    account = (
        (doc.get("spec") or {}).get("serviceAccount")
        if doc.get("kind") == "MCPRemoteProxy"
        else None
    )
    if account and not await _ensure_account(client, meta):
        doc = {**doc, "spec": {k: v for k, v in doc["spec"].items() if k != "serviceAccount"}}
        account = None
    applied = await run_in_threadpool(client.apply, doc)
    if account:
        await drop_runner_binding(client, meta["name"], meta["namespace"])
    return applied


async def _ensure_account(client: KubeClient, meta: dict[str, Any]) -> bool:
    """프록시의 네임스페이스에 원격 프록시 계정이 있는가. 없으면 테넌트 객체처럼 만든다."""
    namespace = meta["namespace"]
    live = await run_in_threadpool(
        client.get, "v1", "ServiceAccount", REMOTE_PROXY_SERVICE_ACCOUNT, namespace
    )
    if live is not None:
        return True
    tenant = (meta.get("labels") or {}).get(LABEL_TENANT, "")
    try:
        await run_in_threadpool(
            client.apply, remote_proxy_service_account_manifest(tenant, namespace)
        )
    except Exception as e:  # noqa: BLE001 - 계정을 못 만들면 오늘의 꼴로 둔다
        logger.warning("remote proxy account in %s not ensured: %s", namespace, e)
        return False
    return True


def proxy_account_user(namespace: str) -> str:
    return f"system:serviceaccount:{namespace}:{REMOTE_PROXY_SERVICE_ACCOUNT}"


def proxy_account_can_read(client: KubeClient, namespace: str) -> bool:
    """원격 프록시 계정이 그 네임스페이스의 Secret 이나 ConfigMap 을 읽을 수 있는가. 플랫폼은 그
    계정에 Role 을 묶지 않으므로 참이면 누군가 묶은 것이다."""
    user = proxy_account_user(namespace)
    return any(
        client.can_user(user, verb, resource, namespace)
        for resource in ("secrets", "configmaps")
        for verb in ("get", "list")
    )


async def drop_runner_binding(client: KubeClient, name: str, namespace: str) -> bool:
    binding = runner_binding_name(name)
    live = await run_in_threadpool(client.get, RBAC_API, "RoleBinding", binding, namespace)
    if not owned_by(live, "MCPRemoteProxy", name):
        return False
    return await run_in_threadpool(client.delete, RBAC_API, "RoleBinding", binding, namespace)
