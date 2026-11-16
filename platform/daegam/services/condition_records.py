"""MCP Server 의 파드가 인자 조건을 지키는지의 기록 (P52 4절).

조건은 서버 컨테이너(`mcp-runtime`)가 지킨다. 그 이미지는 `initialize` 의
`capabilities.experimental` 에 `arg_conditions` 를 싣고, 프록시는 그것을 그대로 넘기며 vMCP 는
넘기지 않는다(F0 7번). 그래서 플랫폼은 서버 자신에게 `svc:platform` 으로 묻고, 그 답을 서버와 이미지
digest 와 파드의 리비전의 기록(`MCPServer.conditions_record`)으로 남긴다. 서버의 Cedar 는 그 기록이
모든 파드가 조건을 지킨다고 할 때까지 조건이 있는 Tool 을 누구에게나 닫는다
(`rule_compile.server_policies` 의 `conditions_known`).

- 리컨실 단계 `sweep` 은 인증 재적용보다 먼저 틱마다 돈다. 배포한 리비전에 조건이 있는 서버의
  StatefulSet 파드를 본다. 파드의 리비전은 라벨 `daegam.devops.ai.kr/profile-revision` 이고, 라벨이
  없으면 init 컨테이너가 받는 profile.json 주소의 리비전이다.
- 기록과 다른 digest 로 도는 파드, 리비전을 모르는 파드, 기록의 리비전과 조건이 다른 리비전으로 도는
  파드가 하나라도 있으면 기록을 지운다.
- StatefulSet 이 다 굴러가 모든 파드가 준비됐고 같은 digest 와 배포한 리비전으로 돌면
  `initialize` 를 읽는다. 읽기 전에 기록을 지우므로 읽지 못하면 기록이 없는 채로 닫힌다. 새 기록은
  읽은 뒤 배포가 바뀌지 않았을 때만 쓴다(`deploying_since`·`deployed_revision` 조건). 이미 같은
  digest 와 리비전의 기록이 있으면 묻지 않는다.
- 재배포가 리비전을 옮길 때 기록의 리비전과 조건이 다르면 기록을 지운다(`MCPServerService`). 그래서
  기록이 있으면 그 기록의 리비전 뒤에 배포한 리비전은 모두 조건이 같다. 기록을 쓸 때 모든 파드가 그
  리비전이었으므로, 도는 파드는 모두 기록의 리비전과 같은 조건을 지킨다.
- `conditions_enforced` 가 렌더의 판정이다. 기록이 조건을 지킨다고 하고, 기록의 이미지가 이 문서의
  배포 스냅샷이 렌더하는 이미지와 같고, 기록의 리비전과 배포한 리비전의 조건이 같아야 한다. 이미지나
  조건을 바꾸는 배포는 렌더 때 곧바로 닫히고, 새 파드가 모두 뜬 뒤 스윕이 다시 연다. 첫 배포(기록
  없음), 옛 이미지(`arg_conditions` 없음)와 판을 내린 이미지(기록의 digest 와 다른 파드)는 닫힌다.
- 같은 이미지 참조가 다른 digest 로 바뀐 경우(태그를 옮긴 것)는 그 파드가 digest 를 알린 뒤 첫
  스윕까지 열려 있다. 아직 digest 를 알리지 않은 파드(이미지를 받는 중)는 기록을 지우지 않는다.
  파드가 다시 뜰 때마다 닫았다 열면 프록시가 두 번 굴러가기 때문이다. digest 로 고정한 이미지에는
  이 틈이 없다.
- 기록의 변화는 서버의 규칙 표시(`MCPServerService._rules_mark`)를 바꾸므로 같은 틱의 인증 재적용이
  그 서버를 다시 렌더한다.
"""

from __future__ import annotations

import asyncio
import logging
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from starlette.concurrency import run_in_threadpool

from daegam.k8s.client import KubeClient, parse_service_url
from daegam.k8s.labels import LABEL_PROFILE_REVISION
from daegam.k8s.portforward import bearer_header, bearer_service_client
from daegam.k8s.templates.common import proxy_service_url
from daegam.k8s.templates.mcpserver import FETCH_INIT
from daegam.services.claims import tick_item
from daegam.store.mcp_servers import (
    ConditionsRecord,
    MCPServer,
    MCPServerRevision,
    MongoMCPServerStore,
)
from daegam.store.tool_rules import has_conditions

logger = logging.getLogger("daegam.condition_records")

# `mcp-runtime` 이 `capabilities.experimental` 에 싣는 키 (`mcp-runtime/runtime/conditions.py`
# `ARG_CONDITIONS`). 값의 `unresolved` 는 Tool 이름마다 풀지 못해 닫은 경로다
ARG_CONDITIONS = "arg_conditions"
# 서버 컨테이너의 이름 (`k8s/templates/mcpserver.py` 의 mcp 컨테이너)
MCP_CONTAINER = "mcp"
# init 컨테이너가 받는 profile.json 주소의 리비전 (`k8s/templates/common.py` `profile_json_url`)
PROFILE_REVISION_RE = re.compile(r"/revisions/(\d+)/profile\.json")
# initialize 를 기다리는 상한(초)과 한 틱에 함께 묻는 서버 수
READ_TIMEOUT = 10.0
READ_CONCURRENCY = 4

# (클러스터 클라이언트, 서버 주소, Bearer) → initialize 의 `capabilities.experimental`
Reader = Callable[[KubeClient, str, str | None], Awaitable[dict[str, Any] | None]]


def runtime_image(doc: MCPServer) -> str | None:
    """배포 스냅샷이 렌더하는 서버 이미지다(`EffectiveConfig.images.runtime_image`)."""
    return ((doc.effective_config or {}).get("images") or {}).get("runtime_image")


def revision_conditions(rev: MCPServerRevision | None) -> dict[str, list[dict[str, Any]]] | None:
    """리비전의 인자 조건이다. `<backend_profile_id>/<operationId>` 마다다. 컨테이너는 operationId
    마다 조건을 지키므로 이름 바꾸기와 상관없다. 리비전이 없으면 None 이다."""
    if rev is None:
        return None
    return {
        f"{sel.backend_profile_id}/{op}": [c.model_dump(mode="json") for c in rule.conditions]
        for sel in rev.backends
        for op, rule in sorted(sel.tool_rules.items())
        if rule.conditions
    }


def conditions_match(doc: MCPServer, a: int | None, b: int | None) -> bool:
    """두 리비전의 인자 조건이 같은가. 어느 하나라도 없으면 아니다."""
    first = revision_conditions(doc.get_revision(a or 0))
    return first is not None and first == revision_conditions(doc.get_revision(b or 0))


def conditions_enforced(doc: MCPServer) -> bool:
    """이 서버의 모든 파드가 배포한 리비전의 인자 조건을 지킨다고 아는가. 렌더가 조건이 있는 Tool 을
    열지 정한다(모듈 머리)."""
    record = doc.conditions_record
    return (
        record is not None
        and record.enforces
        and record.image == runtime_image(doc)
        and conditions_match(doc, record.revision, doc.deployed_revision)
    )


def unresolved_conditions(doc: MCPServer) -> list[str]:
    """배포한 리비전에서 서버가 풀지 못해 닫은 Tool 이름이다. 기록이 그 리비전의 것일 때만이다."""
    record = doc.conditions_record
    if record is None or record.revision != doc.deployed_revision or not conditions_enforced(doc):
        return []
    return sorted(record.unresolved)


def holds_conditions(doc: MCPServer) -> bool:
    """배포한 리비전에 인자 조건이 있는가."""
    rev = doc.deployed
    if rev is None:
        return False
    return any(has_conditions(r) for sel in rev.backends for r in sel.tool_rules.values())


@dataclass(frozen=True)
class PodImages:
    """StatefulSet 파드가 알린 mcp 컨테이너의 이미지와 그 파드의 리비전이다. ids 는 digest 를 알린
    파드의 `imageID`, images 는 그 파드의 이미지 참조, revisions 는 도는 파드의 리비전이고
    unknown_revision 은 리비전을 모르는 파드가 있다는 것이다. settled 는 StatefulSet 이 다 굴러가
    모든 파드가 준비됐고 digest 를 알렸다는 것이다."""

    ids: frozenset[str]
    images: frozenset[str]
    revisions: frozenset[int]
    unknown_revision: bool
    settled: bool

    @property
    def uniform(self) -> tuple[str, str, int] | None:
        """(이미지, digest, 리비전). 모든 파드가 하나로 같을 때만이다."""
        if not self.settled or self.unknown_revision:
            return None
        if len(self.ids) != 1 or len(self.images) != 1 or len(self.revisions) != 1:
            return None
        return next(iter(self.images)), next(iter(self.ids)), next(iter(self.revisions))


def _container(
    items: list[dict[str, Any]] | None, name: str = MCP_CONTAINER
) -> dict[str, Any] | None:
    return next((c for c in items or [] if c.get("name") == name), None)


def pod_revision(pod: dict[str, Any]) -> int | None:
    """파드가 도는 리비전이다. 라벨이 먼저이고, ToolHive 가 라벨을 옮기지 않았으면 init 컨테이너가
    받는 profile.json 주소에서 읽는다. 둘 다 없으면 None 이다."""
    label = ((pod.get("metadata") or {}).get("labels") or {}).get(LABEL_PROFILE_REVISION)
    if isinstance(label, str) and label.isdigit():
        return int(label)
    init = _container((pod.get("spec") or {}).get("initContainers"), FETCH_INIT) or {}
    words = [str(w) for w in [*(init.get("command") or []), *(init.get("args") or [])]]
    found = {int(m) for w in words for m in PROFILE_REVISION_RE.findall(w)}
    return next(iter(found)) if len(found) == 1 else None


def pod_images(sts: dict[str, Any], pods: list[dict[str, Any]]) -> PodImages:
    """StatefulSet 과 그 파드에서 `PodImages` 를 읽는다. 끝난 파드(Succeeded·Failed)는 보지
    않는다."""
    spec, status = sts.get("spec") or {}, sts.get("status") or {}
    meta = sts.get("metadata") or {}
    wanted = int(spec.get("replicas") if spec.get("replicas") is not None else 1)
    rolled = (
        (status.get("observedGeneration") or 0) >= (meta.get("generation") or 0)
        and status.get("currentRevision") == status.get("updateRevision")
        and int(status.get("readyReplicas") or 0) == wanted
        and int(status.get("replicas") or 0) == wanted
    )
    ids: set[str] = set()
    images: set[str] = set()
    revisions: set[int] = set()
    unknown = False
    every = True
    running = 0
    for pod in pods:
        if (pod.get("status") or {}).get("phase") in ("Succeeded", "Failed"):
            continue
        running += 1
        revision = pod_revision(pod)
        if revision is None:
            unknown = True
        else:
            revisions.add(revision)
        state = _container((pod.get("status") or {}).get("containerStatuses"))
        image_id = (state or {}).get("imageID") or ""
        if not image_id:
            every = False
            continue
        ids.add(image_id)
        container = _container((pod.get("spec") or {}).get("containers"))
        images.add((container or {}).get("image") or "")
        every = every and bool((state or {}).get("ready"))
    settled = rolled and every and running == wanted and wanted > 0
    return PodImages(
        ids=frozenset(ids),
        images=frozenset(images),
        revisions=frozenset(revisions),
        unknown_revision=unknown,
        settled=settled,
    )


async def read_experimental(
    client: KubeClient, url: str, bearer: str | None
) -> dict[str, Any] | None:
    """서버의 `initialize` 를 읽어 `capabilities.experimental` 을 낸다. 클러스터 안 주소는 클러스터
    클라이언트가 정한 경로로 간다(`bearer_service_client`)."""
    from daegam.mcp_client import server_experimental

    parts = parse_service_url(url)
    if parts is None:
        return await server_experimental(url, timeout=READ_TIMEOUT)
    namespace, service, port, path = parts
    async with bearer_service_client(
        client, namespace, service, port, headers=bearer_header(bearer), timeout=READ_TIMEOUT
    ) as (hc, base_url):
        return await server_experimental(f"{base_url}{path}", timeout=READ_TIMEOUT, http_client=hc)


def record_from(
    experimental: dict[str, Any] | None, *, image: str, image_id: str, revision: int, now: datetime
) -> ConditionsRecord:
    """initialize 의 답을 기록으로 옮긴다. `arg_conditions` 가 없거나 꼴이 틀리면 조건을 지키지
    않는 이미지다."""
    value = (experimental or {}).get(ARG_CONDITIONS)
    enforces = isinstance(value, dict) and isinstance(value.get("version"), int)
    unresolved: dict[str, list[str]] = {}
    raw = value.get("unresolved") if enforces else None
    if isinstance(raw, dict):
        for name, paths in raw.items():
            if isinstance(name, str) and isinstance(paths, list):
                unresolved[name] = [p for p in paths if isinstance(p, str)]
    return ConditionsRecord(
        image=image,
        image_id=image_id,
        enforces=enforces,
        revision=revision,
        unresolved=unresolved,
        observed_at=now,
    )


class ConditionRecords:
    """리컨실 단계 `sweep` 이다(모듈 머리). clients 는 문서의 클러스터 클라이언트를, tokens 는
    `svc:platform` 토큰을 낸다(`SvcTokens.platform_token`)."""

    def __init__(
        self,
        servers: MongoMCPServerStore,
        *,
        clients: Callable[[MCPServer], Awaitable[KubeClient]],
        tokens: Any,
        reader: Reader = read_experimental,
    ):
        self.servers = servers
        self.clients = clients
        self.tokens = tokens
        self.reader = reader

    async def sweep(self, now: datetime) -> dict[str, list[str]]:
        """결과 키는 기록을 새로 쓴 서버(`recorded`), 지운 서버(`cleared`), 읽지 못한 서버
        (`unread`)다. 한 서버의 실패는 다른 서버를 막지 않는다."""
        out: dict[str, list[str]] = {}
        docs = [
            d
            for d in await self.servers.live_with_rules()
            if d.state == "deployed" and d.k8s is not None and holds_conditions(d)
        ]
        gate = asyncio.Semaphore(READ_CONCURRENCY)

        async def one(doc: MCPServer) -> None:
            async with gate:
                await tick_item()
                try:
                    verdict = await self.observe(doc, now)
                except Exception as e:  # noqa: BLE001 - 한 서버의 실패가 틱을 막지 않는다
                    logger.info("conditions record of mcp server %s not read: %s", doc.id, e)
                    verdict = "unread"
                if verdict is not None:
                    out.setdefault(verdict, []).append(doc.id)

        await asyncio.gather(*(one(d) for d in docs))
        return {key: sorted(ids) for key, ids in out.items()}

    async def observe(self, doc: MCPServer, now: datetime) -> str | None:
        """서버 하나를 본다. 기록을 바꿨으면 그 까닭(`recorded`·`cleared`)이고 아니면 None 이다."""
        assert doc.k8s is not None
        client = await self.clients(doc)
        sts = await run_in_threadpool(
            client.get, "apps/v1", "StatefulSet", doc.k8s.name, doc.k8s.namespace
        )
        if sts is None:
            return None
        selector = ((sts.get("spec") or {}).get("selector") or {}).get("matchLabels") or {}
        if not selector:
            return None
        pods = await run_in_threadpool(client.list, "v1", "Pod", doc.k8s.namespace, selector)
        seen = pod_images(sts, pods)
        record = doc.conditions_record
        stale = record is not None and (
            bool(seen.ids - {record.image_id})
            or seen.unknown_revision
            or any(not conditions_match(doc, r, record.revision) for r in seen.revisions)
        )
        uniform = seen.uniform
        if uniform is None or uniform[2] != doc.deployed_revision:
            # 아직 굴러가는 중이다. 기록과 다르게 도는 파드가 있으면 지우고, 모두 배포한 리비전이
            # 되면 다시 읽는다
            if stale:
                await self._clear(doc)
                return "cleared"
            return None
        image, image_id, revision = uniform
        if record is not None and not stale:
            if record.image_id == image_id and record.revision == revision:
                return None
        if record is not None:
            # 읽기 전에 지운다. 읽지 못하면 옛 기록이 남지 않고 닫힌다
            await self._clear(doc)
        bearer = await self.tokens.platform_token(doc.tenant, doc.id)
        url = proxy_service_url(doc.k8s.name, doc.k8s.namespace)
        experimental = await self.reader(client, url, bearer)
        fresh = record_from(
            experimental, image=image, image_id=image_id, revision=revision, now=now
        )
        written = await self.servers.set_conditions_record(
            doc.id,
            fresh,
            expect_revision=doc.deployed_revision,
            expect_deployment=doc.deploying_since,
        )
        if not written:
            # 읽는 동안 배포가 바뀌었다. 다음 틱이 새 배포의 파드를 본다
            return "cleared" if record is not None else None
        doc.conditions_record = fresh
        return "recorded"

    async def _clear(self, doc: MCPServer) -> None:
        doc.conditions_record = None
        await self.servers.set_conditions_record(doc.id, None)
