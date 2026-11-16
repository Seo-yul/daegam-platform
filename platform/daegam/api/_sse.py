"""끊기는 순간 본문을 닫는 SSE 응답 (D65).

Starlette 는 연결이 끊기면 응답 태스크 그룹만 취소하고 body_iterator 를 닫지 않는다. 끊김이 send
도중에 오면 본문은 읽다 만 채 남는다. 그동안 런타임은 연결이 끊긴 줄 모르고 프록시의 클라이언트·터널
문맥도 열린 채다. 런타임의 같은 이름 클래스와 꼴이 같다. 두 배포물이 코드를 나누지 않아 플랫폼 몫은
API 계층에 따로 둔다.
"""

from __future__ import annotations

import anyio
from starlette.responses import StreamingResponse
from starlette.types import Receive, Scope, Send


class ClosingStreamingResponse(StreamingResponse):
    """응답이 끝나는 길마다 본문을 같은 태스크에서 닫는 SSE 응답이다. finally 에서 막이 안에 aclose
    를 불러 본문의 마감(프록시 연결·터널 닫기)이 곧바로 돈다. 다 읽고 끝난 본문에는 aclose 가
    아무것도 하지 않는다."""

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        try:
            await super().__call__(scope, receive, send)
        finally:
            await self.close_body()

    async def close_body(self) -> None:
        """본문을 막이 안에서 닫는다. 응답을 만든 뒤 내기 전에 라우트가 실패하면 라우트가 부른다."""
        aclose = getattr(self.body_iterator, "aclose", None)
        if aclose is not None:
            with anyio.CancelScope(shield=True):
                await aclose()
