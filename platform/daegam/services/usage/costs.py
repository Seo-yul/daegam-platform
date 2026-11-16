"""원장 행의 금액 합 (P37 7절). 턴과 세션의 Traces, 실행 기록, 평가 결과가 함께 쓴다.

금액은 값을 매긴 행만 더한다. 고른 호출이 모두 가격 규칙이 없으면 합은 0 이 아니라 None 이다. 화면이
"값 없음" 과 0 원을 가르게 하려는 것이다.
"""

from __future__ import annotations

from typing import Any

from daegam.store.usage import MongoUsageStore


def priced_sum(group: dict[str, Any]) -> float | None:
    """`record_sums` 묶음 하나의 금액. 호출이 모두 가격 규칙이 없으면 None 이다."""
    return None if group.get("unpriced_calls", 0) >= group.get("calls", 0) else group["cost_usd"]


async def priced_sums(
    usage: MongoUsageStore, match: dict[str, Any], key: str, values: list[str]
) -> dict[str, dict[str, Any]]:
    """match 가 고른 원장 행을 key 의 값(values 가운데)마다 더한다. 묶음의 `cost_usd` 는
    `priced_sum` 이다. 행이 없는 값은 없다."""
    if not values:
        return {}
    sums = await usage.record_sums({**match, key: {"$in": values}}, key)
    return {g["_id"]: {**g, "cost_usd": priced_sum(g)} for g in sums}


__all__ = ["priced_sum", "priced_sums"]
