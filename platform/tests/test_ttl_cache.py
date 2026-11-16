"""파드 로컬 TTL 캐시 (daegam/ttl_cache.py)."""

from __future__ import annotations

from daegam.ttl_cache import TTLCache


def test_values_live_for_the_ttl_and_then_vanish():
    now = [100.0]
    cache: TTLCache[str, int] = TTLCache(30.0, clock=lambda: now[0])
    assert cache.get("a") is None
    cache.put("a", 1)
    now[0] = 129.9
    assert cache.get("a") == 1
    now[0] = 130.0
    assert cache.get("a") is None
    # 다시 넣으면 그때부터 다시 센다
    cache.put("a", 2)
    now[0] = 159.0
    assert cache.get("a") == 2


def test_pop_and_clear_forget_values():
    cache: TTLCache[str, int] = TTLCache(30.0)
    cache.put("a", 1)
    cache.put("b", 2)
    cache.pop("a")
    cache.pop("missing")
    assert (cache.get("a"), cache.get("b")) == (None, 2)
    cache.clear()
    assert cache.get("b") is None
