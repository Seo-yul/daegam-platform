"""웹 검색의 테넌트 상한 칸 (P39b 12절).

    web_search_counts {_id: "<tenant>:<칸 시작의 분>", count, expires_at}

분 칸과 날 칸을 같은 컬렉션에 둔다. 칸이 끝나면 TTL 이 지운다. 채팅 업로드와 같은 칸 세기
(`store/base.py` `count_in_window`)라 복제본 사이에 값 하나가 지켜진다.
"""

from __future__ import annotations

from datetime import datetime, timedelta

from pymongo.asynchronous.database import AsyncDatabase

from daegam.store.base import count_in_window, ensure_ttl_index

WEB_SEARCH_COUNTS = "web_search_counts"
# 칸이 끝난 뒤 TTL 이 지우기까지 더 남는 시간
COUNT_KEEP = timedelta(minutes=1)


class MongoWebSearchCountStore:
    def __init__(self, db: AsyncDatabase):
        self.coll = db[WEB_SEARCH_COUNTS]

    async def init_schema(self) -> None:
        await ensure_ttl_index(self.coll, "expires_at", 0, "ttl_expires_at")

    async def count(self, tenant: str, now: datetime, window: timedelta) -> tuple[int, datetime]:
        """테넌트의 지금 칸에 하나를 더하고 (수, 칸이 끝나는 시각)을 돌려준다. 분 칸과 날 칸은 칸
        시작이 달라 id 가 겹치지 않는다(날 칸은 00:00 에 시작하고 그 분 칸과 id 가 같다). 그래서 날
        칸의 id 에는 `d:` 를 붙인다."""
        subject = f"d:{tenant}" if window >= timedelta(days=1) else tenant
        return await count_in_window(self.coll, subject, now, window, COUNT_KEEP)
