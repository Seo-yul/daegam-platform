"""사용량 보고 (P37 13절). 시간 묶음만 읽으므로 최대 롤업 주기(2분)만큼 늦다.

날은 요청의 `tz`(IANA, 기본 UTC)로 자르고(`$dateTrunc`, Mongo 7), 묶는 기준은 날·Agent·부른 쪽·
사람·모델이다. 플랫폼 관리자는 테넌트와 경로로도 묶는다. 이름은 읽을 때 붙인다. Agent 는 지금의
표시 이름이고 지워졌으면 행의 `source.name` 이다. 사람은 sub 이고 local 모드면 사용자 이름을 곁에
둔다.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from daegam.services.agent_features import TELEMETRY
from daegam.store.agents import MongoAgentStore
from daegam.store.base import naive_utc
from daegam.store.usage import BUCKET_METRICS, MongoUsageStore
from daegam.store.users import MongoUserStore

# 테넌트 화면의 묶는 기준. 플랫폼 화면은 테넌트·경로가 더해진다
TenantGroup = Literal["day", "agent", "source", "user", "model"]
AdminGroup = Literal["day", "agent", "source", "user", "model", "tenant", "route"]
MAX_RANGE = timedelta(days=400)
MAX_ROWS = 200
# 날마다 금액 순으로 보이는 열쇠 수. 나머지는 "other" 로 묶는다
SERIES_TOP = 5
# 묶는 기준 → 묶음 칸
GROUP_KEYS: dict[str, tuple[str, ...]] = {
    "day": (),
    "agent": ("source_id",),
    "source": ("source_kind", "source_id"),
    "user": ("user_sub",),
    "model": ("provider_kind", "provider_name", "model"),
    "tenant": ("tenant",),
    "route": ("route",),
}


class ReportError(ValueError):
    """기간·시간대·기준이 틀렸다."""


@dataclass(frozen=True)
class UsageQuery:
    since: datetime
    until: datetime
    group: AdminGroup
    tz: str
    tenant: str | None = None


def parse_query(
    since: datetime | None,
    until: datetime | None,
    group: AdminGroup,
    tz: str,
    *,
    tenant: str | None,
) -> UsageQuery:
    """요청의 값을 검사한다. 기간이 없으면 그 시간대의 이번 달이다. 기준은 라우트의 타입이
    거른다."""
    zone = check_zone(tz)
    if since is None:
        local = datetime.now(zone)
        since = local.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    if until is None:
        until = datetime.now(zone) + timedelta(hours=1)
    start, end = naive_utc(since), naive_utc(until)
    if end <= start:
        raise ReportError("until must be after since")
    if end - start > MAX_RANGE:
        raise ReportError(f"the range is at most {MAX_RANGE.days} days")
    return UsageQuery(start, end, group, tz, tenant)


def check_zone(tz: str) -> ZoneInfo:
    """IANA 시간대 이름. 모르는 이름이면 ReportError 다."""
    try:
        return ZoneInfo(tz)
    except (ZoneInfoNotFoundError, ValueError) as e:
        raise ReportError(f"unknown time zone {tz!r}") from e


class UsageReports:
    def __init__(
        self, store: MongoUsageStore, agents: MongoAgentStore, users: MongoUserStore | None
    ):
        self.store = store
        self.agents = agents
        # local 모드의 사용자 이름을 곁에 둔다. 다른 모드는 sub 뿐이다
        self.users = users

    async def report(self, q: UsageQuery) -> dict[str, Any]:
        """`{totals, series, rows, truncated}`. series 는 날마다 금액 순 다섯과 other 다. Agent
        기준은 줄과 series 만 Agent 의 호출이고 합계는 다른 기준과 같다."""
        fields = GROUP_KEYS[q.group]
        groups = await self.store.bucket_groups(
            self._match(q), {f: f"${f}" for f in fields}, day_tz=q.tz
        )
        totals = _sum(groups)
        if q.group == "agent":
            everything = self._match(q, all_sources=True)
            totals = _sum(await self.store.bucket_groups(everything, {}, day_tz=None))
        by_key: dict[tuple[Any, ...], list[dict[str, Any]]] = defaultdict(list)
        by_day: dict[datetime, list[dict[str, Any]]] = defaultdict(list)
        for g in groups:
            by_key[_key(g, fields)].append(g)
            by_day[g["_id"]["day"]].append(g)
        if q.group == "day":
            rows = [{"day": day, **_sum(gs)} for day, gs in sorted(by_day.items())]
        else:
            rows = sorted(
                (_row(fields, key, gs) for key, gs in by_key.items()),
                key=lambda r: r["cost_usd"],
                reverse=True,
            )
        truncated = len(rows) > MAX_ROWS
        rows = rows[:MAX_ROWS]
        if q.group != "day":
            await self._name(q.group, rows, q.tenant)
        series = [_day_series(day, gs, fields) for day, gs in sorted(by_day.items())]
        return {"totals": totals, "series": series, "rows": rows, "truncated": truncated}

    async def unpriced(self, q: UsageQuery) -> list[dict[str, Any]]:
        """가격 규칙이 없는 모델. 플랫폼 사용량 화면이 늘어놓는다."""
        match = {**self._match(q), "unpriced_calls": {"$gt": 0}}
        fields = GROUP_KEYS["model"]
        groups = await self.store.bucket_groups(match, {f: f"${f}" for f in fields}, day_tz=None)
        return [
            {
                **dict(zip(fields, _key(g, fields), strict=True)),
                "calls": g["unpriced_calls"],
                "input_tokens": g["input_tokens"],
                "output_tokens": g["output_tokens"],
            }
            for g in sorted(groups, key=lambda g: g["unpriced_calls"], reverse=True)
        ]

    async def health(self) -> dict[str, Any]:
        """원장 건강. 롤업이 마지막 틱에 센 수와 그 시각, 원장을 쓰지 못하는 옛 이미지의 Agent 수,
        컬렉션 크기다."""
        state = await self.store.get_state()
        old = [
            a.id
            for a in await self.agents.live()
            if a.runtime is None or TELEMETRY not in a.runtime.features
        ]
        return {
            **(state.get("health") or {}),
            "agents_without_telemetry": len(old),
            "collection_bytes": await self.store.collection_sizes(),
            "tick_at": state.get("tick_at"),
        }

    @staticmethod
    def _match(q: UsageQuery, *, all_sources: bool = False) -> dict[str, Any]:
        match: dict[str, Any] = {"hour": {"$gte": q.since, "$lt": q.until}}
        if q.tenant is not None:
            match["tenant"] = q.tenant
        if q.group == "agent" and not all_sources:
            match["source_kind"] = "agent"
        return match

    async def _name(self, group: str, rows: list[dict[str, Any]], tenant: str | None) -> None:
        """이름을 붙인다. Agent 는 지금 이름, 없거나 다른 테넌트의 Agent 면 행의 이름이다."""
        if group in ("agent", "source"):
            ids = [r["source_id"] for r in rows if r.get("source_kind", "agent") == "agent"]
            agents = await self.agents.get_many([i for i in ids if i])
            for r in rows:
                agent = agents.get(r.get("source_id") or "")
                if agent is not None and tenant is not None and agent.tenant != tenant:
                    agent = None
                r["name"] = agent.display_name if agent is not None else r.get("source_name")
        elif group == "user" and self.users is not None:
            users = await self.users.get_many([r["user_sub"] for r in rows if r.get("user_sub")])
            for r in rows:
                user = users.get(r.get("user_sub") or "")
                r["name"] = user.username if user is not None else None
        for r in rows:
            r.setdefault("name", None)
            r.pop("source_name", None)


def _row(
    fields: tuple[str, ...], key: tuple[Any, ...], groups: list[dict[str, Any]]
) -> dict[str, Any]:
    return {
        **dict(zip(fields, key, strict=True)),
        **_sum(groups),
        "source_name": groups[-1].get("source_name"),
    }


def _key(group: dict[str, Any], fields: tuple[str, ...]) -> tuple[Any, ...]:
    return tuple(group["_id"].get(f) for f in fields)


def _sum(groups: Iterable[dict[str, Any]]) -> dict[str, Any]:
    out: dict[str, Any] = dict.fromkeys(BUCKET_METRICS, 0)
    for g in groups:
        for m in BUCKET_METRICS:
            out[m] += g.get(m) or 0
    return out


def _day_series(
    day: datetime, groups: list[dict[str, Any]], fields: tuple[str, ...]
) -> dict[str, Any]:
    """하루의 금액 순 다섯과 나머지."""
    ranked = sorted(groups, key=lambda g: g.get("cost_usd") or 0, reverse=True)
    items = [
        {"key": list(_key(g, fields)), "cost_usd": g.get("cost_usd") or 0, "calls": g["calls"]}
        for g in ranked[:SERIES_TOP]
    ]
    rest = ranked[SERIES_TOP:]
    if rest:
        other = _sum(rest)
        items.append({"key": None, "cost_usd": other["cost_usd"], "calls": other["calls"]})
    return {"day": day, "items": items, "cost_usd": sum(g.get("cost_usd") or 0 for g in groups)}
