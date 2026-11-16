"""게시된 Gateway 를 토큰 인증의 백엔드로 쓴다 (P57 bundle A, docs/plan/phase-57-token-exchange.md
6.1·6.2·6.4절, D110).

데이터 플레인이 플랫폼을 믿으면(local, platform 인 oidc) Gateway C 는 자기 테넌트의 게시된 Gateway P
를 백엔드로 쓴다. 두 쪽이 있다.

- **C 쪽.** P 의 Entry 는 P 에 묶인 1년짜리 토큰(`bound_to` `gateway:<P>`, groups 없음)을 싣는다.
  C 는 P 가 저장한 규칙 출처를 자기 광고 이름으로 컴파일하고, 그때만 토큰이 P 의 규칙 지문을
  싣는다. 스냅샷 행이 그 둘을 한 번에 읽어 둔다(`resolve`). C 는 P 의 이름을 자기 테넌트로
  울타리 치고, Entry 의 filter 를 스냅샷의 이름에서 `execute_tool_script` 를 뺀 것으로 고정한다.
- **P 쪽.** P 의 렌더는 지금 소비자(배포 스냅샷이 P 의 항목을 쓰는 Gateway 의 vMCP)를 이름 대고, 그
  밖의 vMCP 토큰을 거절한다(`plan`). 게시를 거둔 뒤에도 마지막으로 낸 묶인 토큰이 끝날 때까지
  (`bound_until` 과 검증기의 시계 여유) 빈 집합으로 거절한다. 소유자 Cedar 에 forbid 가 있으면
  집합을 비워 소비자의 호출을 모두 닫는다. 소비자 토큰에는 Tool 호출만 연다.

판의 표시와 경고도 여기서 짓는다. 저장과 렌더는 `services/gateways.py` 의 서비스가 한다.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta

from daegam.auth.cedar import holds_forbid
from daegam.auth.oidc import LEEWAY_SECONDS
from daegam.auth.service_token import service_sub
from daegam.k8s.labels import stable_hash
from daegam.k8s.templates.vmcp import published_pins
from daegam.services.advertised_names import gateway_exposed_name
from daegam.services.hub import proxied
from daegam.services.tool_rules import coded_warning
from daegam.services.turn_gates import carries_binding, owner_cedar
from daegam.store.base import utcnow
from daegam.store.gateways import Gateway, GatewayBackendSnapshot, MongoGatewayStore
from daegam.store.hub import PUBLISHED_KIND, HubItem, MongoHubStore
from daegam.store.tool_rules import AnyRule, GatewayToolRule, ToolCallers, has_caller_rule

# 상태 warnings[] 의 코드 (6.4절). 화면은 코드로 화면 언어의 말을 그린다
PUBLISHED_OWNER_FORBID = "published_owner_forbid"
PUBLISHED_BACKEND_CLOSED = "published_backend_closed"
PUBLISHED_CONSUMER_PENDING = "published_consumer_pending"
PUBLISHED_NAMES_UNKNOWN = "published_names_unknown"
WARNINGS: dict[str, str] = {
    PUBLISHED_OWNER_FORBID: (
        "this Gateway's Authz policy holds a forbid statement, so every call through a Gateway "
        "that uses it as a backend is refused"
    ),
    PUBLISHED_BACKEND_CLOSED: (
        "a published Gateway used as a backend refuses this Gateway's calls: it or a published "
        "Gateway below it holds a forbid statement, or it was not rendered to accept them yet"
    ),
    PUBLISHED_CONSUMER_PENDING: (
        "a published Gateway used as a backend has not been rendered again to accept this "
        "Gateway, so its Tools cannot be called for now; Agents deployed meanwhile do not see "
        "them until they are deployed again"
    ),
    PUBLISHED_NAMES_UNKNOWN: (
        "the Tool names of a published Gateway used as a backend were unknown at deploy, so it "
        "is not called"
    ),
}
# 게시를 거둔 뒤 마지막 묶인 토큰이 끝나고도 검증기가 아직 받을 수 있는 시간
BOUND_LEEWAY = timedelta(seconds=LEEWAY_SECONDS)


def published_warning(code: str, detail: str | None = None) -> str:
    return coded_warning(code, WARNINGS[code], detail)


def owner_forbids(gw: Gateway) -> bool:
    """게시된 Gateway 의 배포 리비전 authz_policy 에 forbid 문장이 있거나(`holds_forbid`,
    클레임을 읽지 않는 forbid 도 센다) 소유자 Cedar(`owner_cedar`)가 있는가 (6.2절). forbid 는 다른
    Gateway 를 거쳐 오는 사람을 이름으로 보지 못하므로 그런 Gateway 는 백엔드가 될 수 없다."""
    rev = gw.deployed or gw.current
    return owner_cedar(gw) or holds_forbid(rev.authz_policy)


async def published_source(gateways: MongoGatewayStore, item: HubItem | None) -> Gateway | None:
    """게시 항목의 원본 Gateway. 게시 항목이 아니거나 원본이 없으면 None 이다."""
    if item is None or item.kind != PUBLISHED_KIND or not item.source.gateway_instance_id:
        return None
    return await gateways.get(item.source.gateway_instance_id)


def consumer_sub(gw: Gateway) -> str | None:
    """Gateway 의 vMCP 가 묶인 토큰에 싣는 서비스 신원. 배포 쪽이 없으면 None 이다."""
    return service_sub("vmcp", gw.k8s.name) if gw.k8s is not None else None


def caller_sources(sources: Iterable[dict[str, AnyRule]]) -> list[dict[str, ToolCallers]]:
    """규칙 출처에서 누가 부를지의 규칙만 남긴 것 (6.2절). 인자 조건은 서버가 스스로 지키므로
    소비자로 옮기지 않는다. 규칙이 없는 출처는 뺀다."""
    out = []
    for source in sources:
        kept = {n: r.callers.normalized() for n, r in source.items() if has_caller_rule(r)}
        if kept:
            out.append(kept)
    return out


def as_rules(sources: Sequence[dict[str, ToolCallers]]) -> list[dict[str, AnyRule]]:
    """저장된 출처를 컴파일할 규칙으로 되돌린다."""
    return [{n: GatewayToolRule(callers=c) for n, c in s.items()} for s in sources]


def fence_names(doc: Gateway) -> list[str]:
    """묶인 토큰을 싣는 게시된 백엔드에 이 Gateway 가 광고하는 이름. 테넌트 울타리가 이 이름을
    가둔다 (6.1절)."""
    resolution, overrides = doc.deployed_conflict_resolution, doc.tool_overrides
    return [
        gateway_exposed_name(b.workload, n, resolution, overrides)
        for b in doc.deployed_backends
        if b.bound_gateway_id
        for n in published_pins(b)
    ]


@dataclass(frozen=True)
class ConsumerPlan:
    """게시된 Gateway 의 렌더가 이름 댈 소비자 (6.2절). closed 면 소유자 forbid 때문에 비웠다."""

    consumers: tuple[str, ...]
    closed: bool

    def mark(self) -> str:
        """판에 얹는 표시. 집합이나 닫힘이 바뀌면 그 Gateway 가 다시 렌더된다."""
        return stable_hash([list(self.consumers), self.closed], 16)


class PublishedBackends:
    """게시된 백엔드의 두 쪽 사실을 읽는다. trusted 는 데이터 플레인이 플랫폼을 믿는가다."""

    def __init__(
        self,
        *,
        gateways: MongoGatewayStore,
        hub_items: MongoHubStore,
        trusted: Callable[[], bool],
        now: Callable[[], datetime] = utcnow,
    ):
        self.gateways = gateways
        self.hub_items = hub_items
        self.trusted = trusted
        self.now = now

    # -- P 쪽 ----------------------------------------------------------------------------

    async def plan(self, p: Gateway) -> ConsumerPlan | None:
        """P 의 렌더가 이름 댈 소비자. 플랫폼을 믿지 않으면, 또는 게시하지 않았고 P 에 묶인 토큰이
        더는 살아 있을 수 없으면 None 이다. 소비자는 배포 스냅샷이 P 의 게시 항목을 쓰는 draft 아닌
        같은 테넌트 Gateway 이고(배포 중, 실패, 내리는 중 포함), 정의만 쓰는 것은 들지 않는다."""
        if not self.trusted():
            return None
        items = await self.hub_items.published_of_instance(p.id)
        if not items and not self._bound_live(p):
            return None
        if owner_forbids(p):
            return ConsumerPlan((), True)
        users = await self.gateways.deployed_over([i.id for i in items])
        subs = {
            sub
            for gw in users
            if gw.tenant == p.tenant and gw.id != p.id and (sub := consumer_sub(gw))
        }
        return ConsumerPlan(tuple(sorted(subs)), False)

    def _bound_live(self, p: Gateway) -> bool:
        """P 에 묶여 나간 토큰을 검증기가 아직 받을 수 있는가."""
        return p.bound_until is not None and self.now() < p.bound_until + BOUND_LEEWAY

    # -- C 쪽 ----------------------------------------------------------------------------

    async def _bindable(self, row: GatewayBackendSnapshot) -> Gateway | None:
        """묶인 토큰을 받을 수 있는 게시된 행의 원본 Gateway. 플랫폼을 믿지 않거나, 고정할 이름이
        없거나, proxy 모드 게시이거나(그 앞단은 토큰을 넘기지 않는다), 원본이 소유자 Cedar 로 마지막
        렌더에 머물면 None 이다. 저장의 검사(`GatewayValidator.check_published`)와 같은 조건이라,
        header 모드에서 배포한 소비자를 재적용이 렌더할 때도 그런 행에는 토큰을 내지 않는다."""
        if row.kind != PUBLISHED_KIND or not self.trusted() or not published_pins(row):
            return None
        item = await self.hub_items.get_unscoped(row.hub_item_id)
        if item is None or proxied(item):
            return None
        p = await published_source(self.gateways, item)
        if p is None or owner_cedar(p):
            return None
        return p

    async def source_of(self, row: GatewayBackendSnapshot) -> Gateway | None:
        """게시된 행의 원본 Gateway."""
        return await published_source(
            self.gateways, await self.hub_items.get_unscoped(row.hub_item_id)
        )

    async def bound(self, doc: Gateway) -> bool:
        """C 의 다음 렌더가 묶인 토큰을 하나라도 싣는가. 렌더 앞의 판(`marks`)이 렌더 뒤의 판과 같은
        답을 내도록 `resolve` 와 같은 조건(`_bindable`)으로 본다."""
        for b in doc.deployed_backends:
            if await self._bindable(b) is not None:
                return True
        return False

    async def resolve(self, doc: Gateway, *, compile_rules: bool) -> None:
        """C 의 게시된 행마다 묶인 Gateway(`bound_gateway_id`)와, compile_rules 면 그 Gateway 가
        저장한 규칙 출처와 지문을 P 의 문서 한 번 읽기로 적는다 (6.1·6.2절). 그래서 토큰의 지문은 C
        가 컴파일한 규칙보다 새롭지 않다. 토큰을 받지 못하는 행(`_bindable`)의 Entry 는 토큰 없이
        렌더되고 P 가 401 로 답한다."""
        for b in doc.deployed_backends:
            if b.kind != PUBLISHED_KIND:
                continue
            b.bound_gateway_id = None
            b.rules_digest = None
            b.published_sources = None
            p = await self._bindable(b)
            if p is None or p.tenant != doc.tenant:
                continue
            b.bound_gateway_id = p.id
            render = p.published_render
            if compile_rules and render is not None and render.digest:
                b.rules_digest = render.digest
                b.published_sources = [dict(s) for s in render.sources]

    async def current_digest(self, row: GatewayBackendSnapshot) -> str | None:
        """게시된 행의 원본이 지금 저장한 규칙 지문. 판과 `rules_digest_behind` 가 행의 것과
        견준다. 행이 아직 묶이지 않았어도(배포가 막 적은 스냅샷) 허브 항목으로 원본을 찾으므로,
        배포가 적은 판과 다음 틱의 판이 같다. 토큰을 받지 못할 행은 None 이다."""
        p = await self._bindable(row)
        render = p.published_render if p is not None else None
        return render.digest if render is not None else None

    async def marks(self, doc: Gateway) -> dict[str, str]:
        """판에 얹는 게시된 백엔드의 표시 (6.1·6.2절). 소비자 C 에는 묶인 토큰을 싣는 동안 표시가
        붙고, 게시된 P 에는 소비자 집합과 닫힘의 표시가 붙는다. 그래서 header 모드에서 배포한 C 는
        토큰 인증으로 바꾼 뒤 다시 렌더되고, P 는 게시, 게시 해제, 소비자의 드나듦, bound_until 의
        끝에 재적용의 속도로 다시 렌더된다. 이 표시를 모르는 이전 릴리스는 판이 달라 둘 다 표시 없는
        렌더로 되돌린다."""
        out: dict[str, str] = {}
        if await self.bound(doc):
            out["bound"] = "1"
        plan = await self.plan(doc)
        if plan is not None:
            out["consumers"] = plan.mark()
        return out

    # -- 경고 ----------------------------------------------------------------------------

    async def warnings(self, doc: Gateway) -> list[str]:
        """이 Gateway 의 게시된 백엔드 경고 (6.4절). 플랫폼을 믿을 때만이다."""
        if not self.trusted() or doc.state == "draft":
            return []
        out: list[str] = []
        if owner_forbids(doc) and await self.hub_items.published_of_instance(doc.id):
            out.append(published_warning(PUBLISHED_OWNER_FORBID))
        unknown: list[str] = []
        closed: list[str] = []
        pending: list[str] = []
        me = consumer_sub(doc)
        for b in doc.deployed_backends:
            if b.kind != PUBLISHED_KIND:
                continue
            if not published_pins(b):
                unknown.append(b.hub_item_name)
                continue
            p = await self.source_of(b)
            if p is None:
                continue
            if await self._closed_below(p):
                closed.append(b.hub_item_name)
            elif p.published_render is None or me not in p.published_render.consumers:
                pending.append(b.hub_item_name)
        for code, names in (
            (PUBLISHED_NAMES_UNKNOWN, unknown),
            (PUBLISHED_BACKEND_CLOSED, closed),
            (PUBLISHED_CONSUMER_PENDING, pending),
        ):
            if names:
                out.append(published_warning(code, ", ".join(sorted(names))))
        return out

    async def _closed_below(self, p: Gateway) -> bool:
        """P 나 그 아래 게시된 Gateway 가 소비자의 호출을 닫는가. 소유자 forbid 를 들었거나, 돌 수
        있는 렌더가 위임 묶임을 싣지 않아 묶인 토큰을 403 으로 거절한다(`carries_binding`)."""
        stack, seen = [p], set()
        while stack:
            gw = stack.pop()
            if gw.id in seen:
                continue
            seen.add(gw.id)
            if owner_forbids(gw) or not carries_binding(gw):
                return True
            for b in gw.deployed_backends:
                if b.kind == PUBLISHED_KIND and (below := await self.source_of(b)) is not None:
                    stack.append(below)
        return False
