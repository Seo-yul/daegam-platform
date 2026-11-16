"""바깥 표시와 판정을 턴으로 보는 테스트가 같이 쓰는 문서와 앱이다(P43, P41 9절). 가짜 MCP 서버의
Add 하나를 바깥 Tool 이나 안쪽 Tool 로 둔다.

승인을 받으려면 memory 노드나 기능 in_pod_state 가 있어야 하므로 문서에는 표본의 memory 노드를 두고
저장소는 파드 안의 것을 넘긴다(`safety_app`)."""

from __future__ import annotations

import copy
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

from runtime.sessions import SessionStore
from tests.app_fixtures import tool_message
from tests.conftest import RunningApp, chat, load_fixture, local_doc, run_app
from tests.store_fixtures import pod_store

ADD = tool_message("Add", a=1, b=2)
RULE = {"Add": ["approve", "reject"]}


def turns_doc(
    url: str,
    *,
    inside: tuple[str, ...] = (),
    read: tuple[str, ...] = (),
    approval: dict[str, list[str]] | None = None,
    memory: bool = True,
    feature: bool = True,
    in_pod_state: bool = False,
) -> dict[str, Any]:
    """가짜 MCP 서버의 Add 하나를 가진 문서다. 기본은 Add 가 바깥 내용이고 쓰기다. in_pod_state 면
    미리 보기처럼 기능 in_pod_state 도 싣는다."""
    doc = local_doc(url)
    source = doc["tools"][0]
    source["approval"] = approval or {}
    if feature:
        doc["features"] = ["tool_safety", *(["in_pod_state"] if in_pod_state else [])]
        source["inside"] = list(inside)
        source["read"] = list(read)
    doc["memory"] = load_fixture("support-agent.json")["memory"] if memory else None
    return doc


def with_outside_source(doc: dict[str, Any]) -> dict[str, Any]:
    """Add 가 바깥인 둘째 소스 `tools-outside` 를 더한다. 같은 서버를 다른 표시로 붙인 것이다."""
    outside = copy.deepcopy(doc["tools"][0])
    outside.update(ref="tools-outside", inside=[], read=[], approval={})
    doc["tools"].append(outside)
    return doc


def without_memory(doc: dict[str, Any]) -> dict[str, Any]:
    return {**doc, "memory": None}


@asynccontextmanager
async def safety_app(
    doc: dict[str, Any], *, store: SessionStore | None = None, **app: Any
) -> AsyncIterator[RunningApp]:
    """memory 노드를 둔 doc 을 파드 안 저장소로 띄운다. store 를 주면 그것을 나눠 쓴다(파드 둘)."""
    if store is not None:
        async with run_app(doc, store=store, **app) as running:
            yield running
        return
    async with (
        pod_store(without_memory(doc)) as opened,
        run_app(doc, store=opened, **app) as running,
    ):
        yield running


async def send(
    running: RunningApp, message: str = ADD, **body: Any
) -> list[tuple[str, dict[str, Any]]]:
    response, sse = await chat(running, message, **body)
    assert response.status_code == 200, response.text
    return sse


def interrupt_of(sse: list[tuple[str, dict[str, Any]]]) -> dict[str, Any]:
    event, data = sse[-1]
    assert event == "interrupt", [e for e, _ in sse]
    return data


def choose(tool: str, choice: str) -> list[dict[str, str]]:
    return [{"tool": tool, "choice": choice}]
