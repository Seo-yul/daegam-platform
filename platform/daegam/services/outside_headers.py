"""외부 MCP 서버의 고정 헤더 (P86 6.1·6.2절, D111 초안).

entry 항목은 만들 때 인증을 고른다. `none` 이거나, 모든 호출에 같은 헤더 하나를 싣는 `header` 다.
헤더 값은 사람이 화면에서 넣고 플랫폼 네임스페이스의 관리 Secret `daegam-outside-header-<항목
id>`(D56 의 꼴)에 둔다. 그래서 항목이나 소비자가 어느 클러스터에 있든 값은 home 클러스터에 있다.
Mongo·응답·로그·감사에는 값이 없고 항목은 `set` 만 안다.

이 모듈은 이름과 값의 검사, 관리 Secret 의 읽기·쓰기·지우기, 항목 없이 남은 관리 Secret 의 정리를
맡는다. 남는 Secret 은 이 칸을 모르는 앞 판이 항목을 지울 때 생긴다 (9절). 플랫폼은 Secret 의 목록을
볼 수 없으므로 쓰기 전에 장부(`store/outside_header_secrets.py`)에 항목을 적고, 정리는 그 장부를
항목과 맞대 이름으로 지운다. 정리는 리컨실 틱마다 돌고, 리컨실은 기동하자마자 첫 틱을 돌므로 기동
때의 정리를 따로 두지 않는다.

**Gateway 의 사본 (6.2절).** header 항목을 백엔드로 쓰는 Gateway 의 Entry 마다 vMCP 네임스페이스에
값의 사본 Secret 과 같은 이름의 headerInjection MCPExternalAuthConfig 를 둔다(`EntryHeaders`).
이름은 토큰 객체와 다른 `entry_header_objects` 가 지어, 토큰을 다루는 코드가 사본을 읽지 않는다. 둘
다 vMCP 가 소유하고 Entry 토큰 라벨을 달아 어느 판의 라벨 정리든 지운다. 렌더할 때마다 관리 Secret
에서 값을 베끼고, 소비자 테넌트가 항목의 consume 경계 밖이거나 관리 Secret 이 없으면 그 Entry 를
헤더 없이 렌더하며, 적용한 뒤 그 렌더가 쓰지 않는 사본을 곧바로 지운다. 소비자의 판에는 항목의 헤더
판(경계 밖이면 `out`)이 표시로 얹혀(`EntryHeaders.marks`) 값을 바꾸거나 경계를 좁히면 재적용이 그
소비자를 다시 렌더한다. 배포 상태가 아닌 Gateway 의 사본은 리컨실의 한 걸음이 지운다
(`GatewayService.drop_stray_header_copies`).
"""

from __future__ import annotations

import logging
import re
import secrets
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from starlette.concurrency import run_in_threadpool

from daegam.auth.hub_access import tenant_in_boundary
from daegam.errors import ServiceError
from daegam.k8s.client import KubeClient
from daegam.k8s.labels import LABEL_ID, LABEL_RESOURCE, stable_hash
from daegam.k8s.templates.common import opaque_secret
from daegam.k8s.templates.svc_token import (
    RESOURCE_GATEWAY_SVC_TOKEN,
    entry_header_objects,
    is_header_object,
    render_external_auth_config,
)
from daegam.secrets import SecretStore, read_secret_key
from daegam.services.gateway_tokens import GatewayTokens
from daegam.services.tool_rules import coded_warning
from daegam.store.base import utcnow
from daegam.store.gateways import Gateway
from daegam.store.hub import HubItem, MongoHubStore
from daegam.store.outside_header_secrets import MongoOutsideHeaderSecretStore
from daegam.store.secret_ledger import SecretLedgerRow, sweep_ledger

logger = logging.getLogger("daegam.hub")

MANAGED_PREFIX = "daegam-outside-header-"
# 관리 Secret 과 Gateway 의 사본이 값을 두는 키
VALUE_KEY = "value"
# 관리 Secret 의 라벨. 사람이 클러스터에서 무엇인지 알아보게 단다
RESOURCE_OUTSIDE_HEADER = "outside-header"
MAX_VALUE_BYTES = 8 * 1024
# HTTP 토큰(RFC 9110 5.6.2)의 글자. 길이는 화면과 렌더가 다루기 좋은 만큼이다
_HEADER_NAME = re.compile(r"^[!#$%&'*+.^_`|~0-9A-Za-z-]{1,64}$")
# 고를 수 없는 헤더 이름 (6.1절). ToolHive 의 headerForward 가 거절하는 이름
# (`pkg/transport/middleware/header_forward.go` `RestrictedHeaders`)과 vMCP·MCP·추적 전파가 직접
# 정하는 이름이다. 비교는 소문자로 한다. OTLP 내보내기의 `otlp_export.FIXED_HEADERS` 와 따로인 것은
# 그쪽이 압축(content-encoding)을 정하고 MCP 의 세션·추적 헤더를 모르는 다른 요청이기 때문이다.
# Modern MCP(2026-07-28)의 서버는 아래 셋을 본문과 견주어 다르거나 두 번 오면 -32020 으로 거절한다.
# 고정 헤더로 두면 `mcp-method` 는 모든 요청을 막고(vMCP 의 `server/discover` 부터 막혀 Tool 을 읽지
# 못한다), `mcp-name` 은 Tool 호출을, `mcp-param-*` 는 그 인자를 헤더로 표시한 Tool 의 호출을 막는다
RESERVED_HEADERS: frozenset[str] = frozenset(
    {
        "host",
        "connection",
        "keep-alive",
        "te",
        "trailer",
        "upgrade",
        "http2-settings",
        "proxy-authorization",
        "proxy-authenticate",
        "proxy-connection",
        "transfer-encoding",
        "content-length",
        "forwarded",
        "x-real-ip",
        "content-type",
        "accept",
        "mcp-session-id",
        "mcp-protocol-version",
        "mcp-method",
        "mcp-name",
        "last-event-id",
        "traceparent",
        "tracestate",
        "baggage",
    }
)
RESERVED_PREFIXES: tuple[str, ...] = ("x-forwarded-", "mcp-param-")


@dataclass(frozen=True)
class HeaderInput:
    """만들기의 인증 입력 (6.1절). 검사는 `HubService.create` 가 한다."""

    kind: str
    header_name: str | None = None
    header_value: str | None = None


def managed_name(item_id: str) -> str:
    """항목 하나의 헤더 값을 두는 관리 Secret 이름."""
    return f"{MANAGED_PREFIX}{item_id}"


def managed_labels(item_id: str) -> dict[str, str]:
    return {LABEL_RESOURCE: RESOURCE_OUTSIDE_HEADER, LABEL_ID: item_id}


def reserved(name: str) -> bool:
    low = name.lower()
    return low in RESERVED_HEADERS or low.startswith(RESERVED_PREFIXES)


def check_header_name(name: str | None, error_cls: type[ServiceError]) -> str:
    """헤더 이름. HTTP 토큰이 아니면 422 invalid_value, 고를 수 없는 이름이면 422
    outside_header_reserved 다."""
    name = (name or "").strip()
    if not _HEADER_NAME.match(name):
        raise error_cls(422, "invalid_value", "header_name must be an HTTP header name")
    if reserved(name):
        raise error_cls(
            422,
            "outside_header_reserved",
            f"header {name} is set by the Gateway or by MCP itself",
            header_name=name,
        )
    return name


def _field_char(ch: str) -> bool:
    """RFC 9110 의 field-value 가운데 받는 글자. 보이는 ASCII, 공백, 탭이다."""
    return ch == "\t" or 0x20 <= ord(ch) <= 0x7E


def check_header_value(value: str | None, error_cls: type[ServiceError]) -> str:
    """앞뒤 공백을 뗀 헤더 값. 비었거나, 보이는 ASCII·공백·탭 밖의 글자가 있거나, 8 KiB 를 넘으면
    422 outside_header_value_invalid 다. 값은 오류에 싣지 않는다."""
    value = (value or "").strip()
    if not value or len(value) > MAX_VALUE_BYTES or not all(_field_char(ch) for ch in value):
        raise error_cls(
            422,
            "outside_header_value_invalid",
            f"the header value must be at most {MAX_VALUE_BYTES} bytes of visible ASCII, "
            "spaces and tabs",
        )
    return value


def new_rev() -> str:
    """헤더 판. 값에서 짓지 않는다. 판이 응답과 파드 어노테이션에 나가기 때문이다."""
    return secrets.token_hex(4)


class OutsideHeaderSecrets:
    """플랫폼 네임스페이스의 관리 Secret 과 그 장부. 저장소(home 클러스터)가 없으면 쓰기는 422
    no_secret_store 다(provider 키와 같다, `api/_common.py` `require_secret_store`)."""

    def __init__(
        self,
        secret_store: Callable[[], SecretStore | None],
        *,
        namespace: str,
        ledger: MongoOutsideHeaderSecretStore,
        live_ids: Callable[[list[str]], Awaitable[set[str]]],
    ):
        self.secret_store = secret_store
        self.namespace = namespace
        self.ledger = ledger
        # 정리가 남길 항목 id 를 찾는 조회. 종류를 거르지 않는 허브 저장소의 조회다
        self.live_ids = live_ids

    def require_store(self, error_cls: type[ServiceError]) -> SecretStore:
        store = self.secret_store()
        if store is None:
            raise error_cls(
                422,
                "no_secret_store",
                "the home cluster is not connected, so the header value has nowhere to go",
            )
        return store

    async def read(self, item_id: str) -> str | None:
        """값. 저장소가 없거나 Secret 이나 키가 없으면 None 이다."""
        return await read_secret_key(
            self.secret_store(), managed_name(item_id), VALUE_KEY, namespace=self.namespace
        )

    async def exists(self, item_id: str) -> bool:
        """관리 Secret 에 값이 있는가. 상태 경고가 쓰고 값은 돌려주지 않는다. 플랫폼은 Secret 을
        이름으로 읽을 수만 있어 API 서버가 값까지 보내지만 여기서 버린다."""
        return await self.read(item_id) is not None

    async def write(self, item_id: str, value: str, error_cls: type[ServiceError]) -> None:
        """장부에 먼저 적고 Secret 에 쓴다. 정리가 쓰는 중인 Secret 을 지우지 않는다."""
        store = self.require_store(error_cls)
        await self.ledger.note_write(item_id, utcnow())
        await run_in_threadpool(
            store.put,
            managed_name(item_id),
            {VALUE_KEY: value},
            namespace=self.namespace,
            labels=managed_labels(item_id),
        )

    async def delete(self, item_id: str) -> None:
        """Secret 과 장부의 행을 지운다. 실패는 기록만 하고, 행이 남아 정리가 다음 틱에 마저
        지운다."""
        store = self.secret_store()
        if store is None:
            return
        try:
            await run_in_threadpool(store.delete, managed_name(item_id), namespace=self.namespace)
        except Exception as e:  # noqa: BLE001 - 항목 삭제를 되돌리지 않는다
            logger.warning(
                "outside header Secret of hub item %s not deleted: %s", item_id, type(e).__name__
            )
            return
        await self.ledger.forget(item_id)

    async def sweep(self, now: datetime | None = None) -> dict[str, Any]:
        """장부에 있고 항목이 없는 관리 Secret 을 지운다. 장부의 규칙은
        `store/secret_ledger.py` `sweep_ledger` 다."""
        store = self.secret_store()
        if store is None:
            return {}

        async def delete_row(row: SecretLedgerRow) -> bool:
            await run_in_threadpool(store.delete, managed_name(row.id), namespace=self.namespace)
            return True

        dropped = await sweep_ledger(self.ledger, now or utcnow(), self.live_ids, delete_row)
        if dropped:
            logger.info("outside header Secrets without a hub item deleted: %s", dropped)
        return {"dropped": dropped} if dropped else {}


# Gateway 상태 경고 (6.2절). 백엔드 항목의 헤더 값을 찾지 못해 그 Entry 를 헤더 없이 렌더한다
OUTSIDE_HEADER_MISSING = "outside_header_missing"
OUTSIDE_HEADER_MISSING_TEXT = (
    "the header value of a hub item used as a backend was not found, so the Gateway calls it "
    "without the header"
)


@dataclass(frozen=True)
class HeaderCopy:
    """Entry 하나가 받는 헤더 사본."""

    item_id: str
    name: str  # 사본 Secret 과 MCPExternalAuthConfig 의 이름
    header_name: str
    rev: str
    value: str


@dataclass
class HeaderPlan:
    """렌더 하나의 헤더 사본 (Entry 이름 → 사본). missing 은 값을 찾지 못한 항목의 이름이다."""

    copies: dict[str, HeaderCopy] = field(default_factory=dict)
    missing: list[str] = field(default_factory=list)

    def names(self) -> set[str]:
        return {c.name for c in self.copies.values()}

    def rev(self) -> str | None:
        """vMCP 파드 템플릿 어노테이션 값. 사본이 없으면 None 이라 어노테이션을 달지 않는다."""
        return "-".join(c.rev for _w, c in sorted(self.copies.items())) or None


class EntryHeaders:
    """Gateway 의 헤더 사본 계획, 렌더, 표시, 정리 (6.2절). k8s_name 은 그 Gateway 의 vMCP
    이름이다."""

    def __init__(self, *, hub_items: MongoHubStore, secrets: OutsideHeaderSecrets):
        self.hub_items = hub_items
        self.secrets = secrets

    async def _backends(self, doc: Gateway) -> list[tuple[Any, HubItem | None]]:
        """헤더를 싣는 배포 스냅샷 백엔드와 그 항목(지금 문서). 그런 백엔드가 없으면 읽지 않는다."""
        rows = [b for b in doc.deployed_backends if b.auth_kind == "header"]
        if not rows:
            return []
        items = await self.hub_items.get_many_unscoped([b.hub_item_id for b in rows])
        return [(b, items.get(b.hub_item_id)) for b in rows]

    @staticmethod
    def _inside(doc: Gateway, item: HubItem | None) -> bool:
        return (
            item is not None
            and item.header_auth is not None
            and tenant_in_boundary(doc.tenant, item, "consume")
        )

    async def plan(self, doc: Gateway, k8s_name: str) -> HeaderPlan:
        """지금 렌더할 사본. 소비자 테넌트가 항목의 consume 경계 안이고 관리 Secret 에 값이 있는
        Entry 만 받는다. 경계 밖이면 헤더 없이 렌더하고, 값이 없으면 헤더 없이 렌더하며 경고한다."""
        out = HeaderPlan()
        names = entry_header_objects(k8s_name, doc.deployed_backends)
        for b, item in await self._backends(doc):
            if not self._inside(doc, item):
                continue
            assert item is not None and item.header_auth is not None
            auth = item.header_auth
            value = await self.secrets.read(item.id)
            if value is None or not auth.header_name or not auth.rev:
                out.missing.append(item.name)
                continue
            out.copies[b.workload] = HeaderCopy(
                item_id=item.id,
                name=names[b.workload],
                header_name=auth.header_name,
                rev=auth.rev,
                value=value,
            )
        return out

    async def stray(self, doc: Gateway, k8s_name: str) -> list[str]:
        """이 Gateway 가 둘 수 없게 된 사본의 이름. 항목이 없거나 consume 경계 밖인 Entry 의
        것이다."""
        names = entry_header_objects(k8s_name, doc.deployed_backends)
        return sorted(
            names[b.workload]
            for b, item in await self._backends(doc)
            if not self._inside(doc, item)
        )

    async def marks(self, doc: Gateway) -> dict[str, str]:
        """소비자 판의 표시 (6.2절). 헤더를 싣는 백엔드가 있으면 Entry 마다 항목의 헤더 판(경계
        밖이거나 항목이 없으면 `out`)을 해시한 값 하나다. 그런 백엔드가 없으면 비어 판이 오늘과
        같다."""
        rows = await self._backends(doc)
        if not rows:
            return {}
        marks = {
            b.workload: (item.header_auth.rev or "")  # type: ignore[union-attr]
            if self._inside(doc, item)
            else "out"
            for b, item in rows
        }
        return {"outside_headers": stable_hash(sorted(marks.items()), 16)}

    @staticmethod
    def docs(
        plan: HeaderPlan,
        *,
        namespace: str,
        tenant: str,
        gateway_id: str,
        owner: dict[str, Any] | None,
        values: bool,
    ) -> list[dict[str, Any]]:
        """사본마다 (Secret,) MCPExternalAuthConfig. values 는 적용하는 렌더뿐이다. 매니페스트와
        미리 보기는 값을 싣지 않는다."""
        out: list[dict[str, Any]] = []
        for _w, c in sorted(plan.copies.items()):
            if values:
                out.append(
                    opaque_secret(
                        c.name,
                        namespace,
                        {VALUE_KEY: c.value},
                        tenant=tenant,
                        resource=RESOURCE_GATEWAY_SVC_TOKEN,
                        id=gateway_id,
                        owner=owner,
                    )
                )
            out.append(
                render_external_auth_config(
                    name=c.name,
                    namespace=namespace,
                    secret_name=c.name,
                    mode="header",
                    tenant=tenant,
                    resource=RESOURCE_GATEWAY_SVC_TOKEN,
                    id=gateway_id,
                    owner=owner,
                    header_name=c.header_name,
                    secret_key=VALUE_KEY,
                )
            )
        return out

    @staticmethod
    async def drop_left_out(
        client: KubeClient, *, k8s_name: str, namespace: str, plan: HeaderPlan, labeled: set[str]
    ) -> None:
        """적용한 렌더가 쓰지 않는 이 vMCP 의 헤더 사본을 곧바로 지운다. 경계 밖으로 빠진 테넌트에
        값이 남지 않게 한다 (6.5절). 토큰 객체와 달리 vMCP 가 다 굴러가기를 기다리지 않는다. 옛
        파드는 이미 env 로 읽었고, 굴러가는 동안 다시 뜨는 옛 파드는 어차피 새 파드로 바뀐다."""
        kept = plan.names()
        stale = sorted(n for n in labeled if is_header_object(k8s_name, n) and n not in kept)
        if stale:
            await GatewayTokens.drop_objects(client, namespace, stale)

    async def warnings(self, doc: Gateway) -> list[str]:
        """값을 찾지 못한 항목의 경고 `outside_header_missing` (6.4절). 있는지만 본다."""
        missing = [
            item.name
            for _b, item in await self._backends(doc)
            if item is not None
            and self._inside(doc, item)
            and not await self.secrets.exists(item.id)
        ]
        if not missing:
            return []
        text = OUTSIDE_HEADER_MISSING_TEXT
        return [coded_warning(OUTSIDE_HEADER_MISSING, text, ", ".join(missing))]
