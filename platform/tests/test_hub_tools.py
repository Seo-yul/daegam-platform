"""허브 도구 이름 해석기 (D49). 원본에서 계산하고 질의 수는 항목 수와 무관하다.

TestClient 호출은 블로킹이라 이벤트 루프 스레드에서 부를 수 없다. HTTP 준비는 밖에서 하고
`client.portal.call` 로는 스토어·해석기의 await 만 감싼다.
"""

from __future__ import annotations

from daegam.services.hub_tools import HubToolNames
from tests.helpers.consts import GW
from tests.helpers.gateways import gateway_up
from tests.helpers.hub import hub_env


def _resolve(client, ids):
    """항목 id 들을 읽어 해석 결과를 돌려준다."""
    st = client.app.state
    resolver = HubToolNames(st.mcp_servers, st.gateways, st.backend_apis)

    async def _run():
        items = await st.hub_items.get_many_unscoped(list(ids))
        return await resolver.resolve(list(items.values()))

    return client.portal.call(_run)


def test_query_count_does_not_grow_with_the_number_of_items(client, fake_clusters, mongo):
    """N+1 이 조용히 돌아오는 자리다. 항목이 하나든 넷이든 질의는 셋이다. 원본이 없으면
    "모른다"(None)다. 빈 목록으로 돌려주면 `plan_conflicts` 가 이름 규칙을 통째로 `prefix` 로
    떨어뜨려 배포된 도구 이름이 전부 바뀐다."""
    cid, items, calc, orders, remote, fake = hub_env(client, fake_clusters)
    gw = gateway_up(client, items, name="a")
    pub = client.post(f"{GW}/{gw['id']}/publish", json={}).json()
    ids = [items["calculator"]["id"], items["orders"]["id"], pub["id"], remote["id"]]

    st = client.app.state
    counts: list[int] = []

    def measure(subset):
        resolver = HubToolNames(st.mcp_servers, st.gateways, st.backend_apis)
        n = 0

        def wrap(fn):
            async def inner(*a, **k):
                nonlocal n
                n += 1
                return await fn(*a, **k)

            return inner

        resolver.mcp_servers = _Spy(st.mcp_servers, "get_many", wrap)
        resolver.gateways = _Spy(st.gateways, "get_many", wrap)
        resolver.backend_apis = _Spy(st.backend_apis, "get_versions", wrap)

        async def _run():
            docs = await st.hub_items.get_many_unscoped(list(subset))
            return await resolver.resolve(list(docs.values()))

        client.portal.call(_run)
        return n

    counts.append(measure([ids[0]]))
    counts.append(measure(ids))
    assert counts == [3, 3], f"질의 수가 항목 수를 따라간다: {counts}"

    mongo["mcp_servers"].delete_one({"_id": calc["id"]})
    assert _resolve(client, [ids[0]])[ids[0]].tool_names is None


class _Spy:
    """한 메서드만 감싸고 나머지는 원본에 위임한다."""

    def __init__(self, inner, name, wrap):
        self._inner = inner
        setattr(self, name, wrap(getattr(inner, name)))

    def __getattr__(self, name):
        return getattr(self._inner, name)
