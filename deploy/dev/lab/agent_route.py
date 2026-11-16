"""e2e_agent.sh STEP 9r 가 쓰는 API 서버 경로 실측 CLI (P35 14절).

플랫폼은 직행이 아닌 클러스터(원격 클러스터, kubecontext 로컬 개발)의 런타임을
`bearer_service_client` 의 pods/portforward 터널로 부른다. API 서버 services/proxy 는
Authorization 을 지우기 때문이다(D41). 이 CLI 는 kubecontext 클라이언트로 같은 함수를 불러
`/metrics` 와 채팅 한 턴(Add)을 보고, 다음 턴을 도중에 끊는다. 턴은 요청과 떨어져 끝까지
가므로(D92) 곧바로 연 같은 세션의 턴은 409 turn_in_progress 이고, 가벼운 읽기
(`?view=progress`)가 도는 동안 결과를 받은 Add 카드를 보이며, 턴이 끝나면 running 이 거짓이고
전사가 done 으로 끝난다. 호출마다 플랫폼 프록시처럼 문맥을 새로 열고 닫으므로, 끊기는 브라우저가
떠난 스트림을 플랫폼이 닫는 것과 같은 닫기다.

실행: uv run --project platform python deploy/dev/lab/agent_route.py
        [--context CTX] NAMESPACE SERVICE TOKEN_SECRET
  NAMESPACE·SERVICE 는 Agent 런타임의 Service 이고 인스턴스 토큰은 TOKEN_SECRET 의
  token 키에서 읽는다.
stdout 에 JSON 한 객체를 찍는다. 단정이 하나라도 틀리면 까닭을 stderr 에 찍고 1 로 끝난다.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import sys
import time
from collections.abc import AsyncIterator
from typing import Any

import httpx2

from daegam.k8s.client import ClusterClient
from daegam.k8s.portforward import bearer_service_client
from daegam.k8s.templates import AGENT_PORT
from daegam.k8s.templates.agent import TOKEN_KEY
from daegam.k8s.templates.svc_token import secret_value
from daegam.store.clusters import Cluster, ClusterConnection

# 이 CLI 가 여는 세션의 소유자다. 플랫폼 프록시 경로가 본문에 싣는 user_sub 자리다
ROUTE_SUB = "e2e-route"
# 스텁 모델은 메시지 안 JSON 조각의 도구를 부른다. {"sleep": n} 이면 n 초 기다린 뒤 부르고,
# {"after": n} 이면 도구 결과를 받은 뒤 n 초 기다려 답한다(runtime/stub.py)
ADD_MESSAGE = json.dumps({"tool": "Add", "args": {"a": 3, "b": 4}})
DETACHED_MESSAGE = json.dumps({"sleep": 6, "tool": "Add", "args": {"a": 5, "b": 6}, "after": 5})
DETACHED_RESULT = "11"
CUT_AFTER_SECONDS = 2.0
# 끊은 뒤 턴이 끝나야 하는 상한이다. 스텁이 기다리는 11초에 마감의 몫을 넉넉히 더했다
DONE_DEADLINE_SECONDS = 40.0
POLL_SECONDS = 0.5
TIMEOUT = 30.0  # 연결·쓰기와 JSON 조회
READ_TIMEOUT = 120.0  # 채팅 SSE 읽기. 스텁 턴은 몇 초면 끝난다
TURN_IN_PROGRESS = "turn_in_progress"
# Cluster 모델이 요구하는 칸이다. 런타임을 부르는 길에는 쓰이지 않는다
UNUSED_INTERNAL_URL = "http://unused.invalid"


class RouteCheckFailed(Exception):
    """단정 하나가 틀렸다. main 이 까닭을 찍고 1 로 끝낸다."""


def expect(ok: bool, message: str) -> None:
    if not ok:
        raise RouteCheckFailed(message)


def sse_events(text: str) -> list[tuple[str, dict[str, Any]]]:
    """SSE 본문을 (이벤트, data) 목록으로 푼다."""
    events: list[tuple[str, dict[str, Any]]] = []
    for block in text.split("\n\n"):
        name, data = None, []
        for line in block.splitlines():
            if line.startswith("event: "):
                name = line.removeprefix("event: ")
            elif line.startswith("data: "):
                data.append(line.removeprefix("data: "))
        if name:
            events.append((name, json.loads("\n".join(data)) if data else {}))
    return events


def first(events: list[tuple[str, dict[str, Any]]], name: str) -> dict[str, Any]:
    return next((data for event, data in events if event == name), {})


class Route:
    """런타임 Service 하나로 가는 길이다. 호출마다 플랫폼 프록시(AgentRuntimeProxy)처럼
    bearer_service_client 문맥을 새로 연다."""

    def __init__(self, client: ClusterClient, namespace: str, service: str, token: str) -> None:
        self.client = client
        self.namespace = namespace
        self.service = service
        self.token = token

    @property
    def via(self) -> str:
        return self.client.service_route(self.namespace, self.service, AGENT_PORT).via

    def _open(
        self, read_timeout: float = TIMEOUT
    ) -> contextlib.AbstractAsyncContextManager[tuple[httpx2.AsyncClient, str]]:
        return bearer_service_client(
            self.client,
            self.namespace,
            self.service,
            AGENT_PORT,
            bearer=self.token,
            timeout=TIMEOUT,
            read_timeout=read_timeout,
        )

    async def get(self, path: str, **params: str) -> Any:
        async with self._open() as (hc, base):
            r = await hc.get(f"{base}{path}", params=params)
        expect(r.status_code == 200, f"GET {path} -> {r.status_code}: {r.text[:200]}")
        return r.json()

    @staticmethod
    def _chat_body(message: str, session_id: str | None) -> dict[str, Any]:
        return {"message": message, "session_id": session_id, "user_sub": ROUTE_SUB}

    async def chat(self, message: str) -> list[tuple[str, dict[str, Any]]]:
        """새 세션의 한 턴을 끝까지 읽는다."""
        async with self._open(READ_TIMEOUT) as (hc, base):
            r = await hc.post(f"{base}/chat", json=self._chat_body(message, None))
        expect(r.status_code == 200, f"POST /chat -> {r.status_code}: {r.text[:200]}")
        return sse_events(r.text)

    @contextlib.asynccontextmanager
    async def _stream(self, message: str, session_id: str) -> AsyncIterator[None]:
        async with (
            self._open(READ_TIMEOUT) as (hc, base),
            hc.stream("POST", f"{base}/chat", json=self._chat_body(message, session_id)) as r,
        ):
            expect(r.status_code == 200, f"POST /chat (cut) -> {r.status_code}")
            yield

    async def cut_chat(self, message: str, session_id: str) -> float:
        """턴을 열고 CUT_AFTER_SECONDS 뒤 응답·클라이언트·터널을 한꺼번에 닫는다. 끊은
        시각(monotonic)을 돌려준다."""
        async with self._stream(message, session_id):
            await asyncio.sleep(CUT_AFTER_SECONDS)
            cut_at = time.monotonic()
        return cut_at

    async def refused_turn(self, message: str, session_id: str) -> None:
        """같은 세션의 다음 턴이 409 turn_in_progress 여야 한다. 끊긴 턴이 아직 돈다."""
        async with self._open() as (hc, base):
            r = await hc.post(f"{base}/chat", json=self._chat_body(message, session_id))
        expect(
            r.status_code == 409 and TURN_IN_PROGRESS in r.text,
            f"the next turn right after the cut -> {r.status_code}: {r.text[:200]}",
        )

    async def follow(self, session_id: str, since: float) -> dict[str, float]:
        """끊긴 턴을 가벼운 읽기로 따라간다. 도는 동안 결과를 받은 Add 카드가 전사에 붙고, 끝나면
        running 이 거짓이고 전사가 done 으로 끝나야 한다. 끊은 뒤 결과가 보인 초와 끝난 초다."""
        saved: float | None = None
        seen_running = False
        while True:
            body = await self.get(f"/sessions/{session_id}", user_sub=ROUTE_SUB, view="progress")
            elapsed = round(time.monotonic() - since, 1)
            items = (body.get("transcript") or {}).get("items") or []
            if body["session"].get("running"):
                seen_running = True
                if saved is None and result_saved(items):
                    saved = elapsed
            else:
                last = items[-1].get("kind") if items else None
                expect(seen_running, f"{session_id} did not read running after the cut")
                expect(saved is not None, f"the Add result of {session_id} showed only at done")
                expect(last == "done", f"the cut turn of {session_id} ended with {last!r}")
                return {"saved_in_seconds": saved or 0.0, "done_in_seconds": elapsed}
            expect(
                elapsed < DONE_DEADLINE_SECONDS,
                f"the cut turn of {session_id} still runs {DONE_DEADLINE_SECONDS:g}s after the cut",
            )
            await asyncio.sleep(POLL_SECONDS)


def result_saved(items: list[dict[str, Any]]) -> bool:
    """전사에 결과를 받은 Add 카드가 있는가. 끊긴 턴의 결과다."""
    return any(
        i.get("kind") == "tool_call"
        and DETACHED_RESULT in str((i.get("result") or {}).get("content"))
        for i in items
    )


def open_route(context: str, namespace: str, service: str, token_secret: str) -> Route:
    """kubecontext 클라이언트와 토큰 Secret 의 인스턴스 토큰으로 길을 만든다."""
    cluster = Cluster(
        name="e2e-route",
        connection=ClusterConnection(mode="kubecontext", kubecontext=context),
        platform_internal_url=UNUSED_INTERNAL_URL,
    )
    client = ClusterClient.from_cluster(cluster, None)
    token = secret_value(client.get("v1", "Secret", token_secret, namespace), TOKEN_KEY)
    if not token:
        raise RouteCheckFailed(f"secret {namespace}/{token_secret} has no {TOKEN_KEY!r} key")
    return Route(client, namespace, service, token)


async def run(route: Route) -> dict[str, Any]:
    expect(route.via == "apiserver", f"the kubecontext route is {route.via!r}, not 'apiserver'")
    metrics = await route.get("/metrics")
    events = await route.chat(ADD_MESSAGE)
    names = [name for name, _ in events]
    result = str(first(events, "tool_result").get("content"))
    expect(names[-1:] == ["done"] and "7" in result, f"Add turn: {names} tool_result={result!r}")
    session_id = first(events, "done")["session_id"]
    cut_at = await route.cut_chat(DETACHED_MESSAGE, session_id)
    await route.refused_turn(ADD_MESSAGE, session_id)
    followed = await route.follow(session_id, cut_at)
    return {
        "via": route.via,
        "protocols": (metrics.get("tools") or {}).get("protocols"),
        "chat": {"session_id": session_id, "events": names, "tool_result": result},
        "cut": {"after_seconds": CUT_AFTER_SECONDS, **followed},
    }


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("--context", default="orbstack")
    ap.add_argument("namespace")
    ap.add_argument("service")
    ap.add_argument("token_secret")
    a = ap.parse_args(argv[1:])
    try:
        route = open_route(a.context, a.namespace, a.service, a.token_secret)
        out = asyncio.run(run(route))
    except RouteCheckFailed as e:
        print(f"agent_route: {e}", file=sys.stderr)
        return 1
    print(json.dumps(out, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
