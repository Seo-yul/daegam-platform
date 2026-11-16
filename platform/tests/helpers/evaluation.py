"""평가를 쓰는 테스트의 도우미 (P45 7절·9절).

`case_doc` 은 사례 문서 하나(rating 을 주면 대화에서 복사한 것)다. `scripted_runtime` 은 질문 글로
턴을 정하는 가짜 런타임이다. `write` 는 쓰기 Tool `Add` 를 부르고 결정이 respond 와 reject 뿐인 승인
대기로 멈추며, `read` 는 읽기 Tool `Lookup` 을 부르고 끝나고, `fail` 은 턴 오류로, `gone` 은 404
`session_not_found` 로 답하고, `refused` 는 런타임이 거절한 Tool 결과를 내며, 그 밖은 글 하나로
끝난다. 결정의 재개는 런타임처럼 대기하던 호출을 같은 id 로 다시 알린 뒤 respond 의 글이나 거부를
결과로 내고 끝난다. `approve` 가 오면 Add 의 백엔드가 불린 것으로 센다(`app.state.backend`).
`app.state.gone_on_decide` 를 켜면 결정이 `session_not_found` 다. 위임 id 는 `app.state.echo_grant`
가 켜져 있을 때 되돌려 준다. 받은 본문과 지운 세션을 남긴다.

픽스처 `evaluated` 는 배포한 Agent 와 그 클러스터에 붙인 가짜 런타임이다(conftest 의
`pytest_plugins` 가 싣는다). `seed_set` 은 세트와 사례를
저장소에 넣고, `new_run` 은 그 사례로 실행 문서를 만들며, `drive` 는 실행기를 한 조각 몬다.
`verdict` 는 내용 없는 판정 하나, `DirectEndpoints` 는 늘 직접 경로를 고르는 채점 모델의 길이다.
"""

from __future__ import annotations

import json
from datetime import timedelta
from typing import Any

import pytest
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, StreamingResponse

from daegam.services.evaluation.cases import case_digest
from daegam.services.llm_endpoint import Endpoint
from daegam.services.turn_person import SESSION_HEADER, TURN_GRANT_HEADER, TURN_ID_HEADER
from daegam.store.base import utcnow
from daegam.store.evaluation import CaseContent, CaseOrigin, EvaluationCase, EvaluationSet
from daegam.store.evaluation_runs import CaseVerdict, EvaluationRun, RunActor, RunCase
from tests.helpers.agent_runtime import deployed_agent
from tests.helpers.previews import HEADER_SUB
from tests.helpers.runtime import runtime_refusal, session_missing, sse

OWNER = "replica-a"
HELD = {"allowed_decisions": ["respond", "reject"]}
CONTENT = CaseContent(messages=["질문"])


def case_doc(
    case_id: str,
    set_id: str = "set-1",
    agent_id: str = "ag-1",
    content: CaseContent = CONTENT,
    *,
    tenant: str = "default",
    rating: str | None = None,
    by: str = HEADER_SUB,
) -> EvaluationCase:
    """사례 하나. rating 을 주면 그 평가에서 복사한 사례다."""
    origin = None if rating is None else CaseOrigin(rating_id=rating, session_id="s1", turn_id="t")
    return EvaluationCase(
        id=case_id,
        tenant=tenant,
        agent_id=agent_id,
        set_id=set_id,
        content=content,
        digest=case_digest(content),
        origin=origin,
        created_by=by,
        updated_by=by,
    )


def scripted_runtime() -> FastAPI:
    app = FastAPI()
    app.state.bodies = []
    app.state.deleted = []
    app.state.backend = 0
    app.state.counter = 0
    app.state.echo_grant = True
    app.state.gone_on_decide = False

    def headers(sid: str, body: dict[str, Any]) -> dict[str, str]:
        out = {SESSION_HEADER: sid, TURN_ID_HEADER: f"t-{sid}"}
        grant = body.get("turn_grant")
        if app.state.echo_grant and isinstance(grant, dict) and grant.get("id"):
            out[TURN_GRANT_HEADER] = grant["id"]
        return out

    def done(sid: str, text: str) -> str:
        usage = {"input_tokens": 10, "output_tokens": 5}
        return sse("done", {"session_id": sid, "text": text, "usage": usage})

    def add_call(sid: str) -> str:
        return sse("tool_call", {"id": f"c-{sid}", "name": "Add", "args": {"a": 1, "b": 2}})

    async def turn(sid: str, message: str):
        if message.startswith("write"):
            yield add_call(sid)
            action = {"name": "Add", "args": {"a": 1, "b": 2}, "description": "Add"}
            yield sse(
                "interrupt",
                {"session_id": sid, "action_requests": [action], "review_configs": [HELD]},
            )
            return
        if message.startswith("read"):
            yield sse("tool_call", {"id": f"r-{sid}", "name": "Lookup", "args": {"q": "환불"}})
            yield sse(
                "tool_result",
                {"id": f"r-{sid}", "name": "Lookup", "status": "success", "content": "7일"},
            )
            yield done(sid, "환불은 7일 안에 한다")
            return
        if message.startswith("refused"):
            refusal = json.dumps({"code": "url_blocked", "message": "x"})
            yield sse("tool_call", {"id": f"w-{sid}", "name": "web_fetch", "args": {"url": "u"}})
            yield sse(
                "tool_result",
                {"id": f"w-{sid}", "name": "web_fetch", "status": "error", "content": refusal},
            )
            yield sse("tool_call", {"id": f"g-{sid}", "name": "Lookup", "args": {"q": "x"}})
            yield sse(
                "guardrail",
                {
                    "position": "tool",
                    "rule": "deny_pattern",
                    "action": "block",
                    "tool": "Lookup",
                    "stage": "args",
                },
            )
            yield sse(
                "tool_result",
                {"id": f"g-{sid}", "name": "Lookup", "status": "error", "content": "막았다"},
            )
            yield sse("tool_call", {"id": f"e-{sid}", "name": "Lookup", "args": {"q": "y"}})
            yield sse(
                "tool_result",
                {"id": f"e-{sid}", "name": "Lookup", "status": "error", "content": '{"code": 1}'},
            )
            yield done(sid, "거절됐다")
            return
        if message.startswith("fail"):
            yield sse("error", {"session_id": sid, "code": "turn_timeout", "message": "x"})
            return
        yield done(sid, f"답: {message}")

    @app.post("/chat")
    async def chat(request: Request):
        body = await request.json()
        app.state.bodies.append(("chat", body))
        if body["message"] == "gone":
            return session_missing()
        sid = body.get("session_id")
        if sid is None:
            app.state.counter += 1
            sid = f"s-{app.state.counter}"
        return StreamingResponse(
            turn(sid, body["message"]), media_type="text/event-stream", headers=headers(sid, body)
        )

    async def resumed(sid: str, decision: dict[str, Any]):
        kind = decision["type"]
        if kind == "respond":
            result = {"status": "success", "content": decision["message"]}
        else:
            result = {"status": "error", "content": decision.get("message") or "rejected"}
        # 런타임은 재개한 턴에서 대기하던 호출을 같은 id 로 다시 알린다
        yield add_call(sid)
        yield sse("tool_result", {"id": f"c-{sid}", "name": "Add", **result})
        yield done(sid, f"결과: {result['content']}")

    @app.post("/sessions/{sid}/decisions")
    async def decisions(sid: str, request: Request):
        body = await request.json()
        app.state.bodies.append(("decisions", body))
        if app.state.gone_on_decide:
            return session_missing()
        if any(d["type"] in ("approve", "edit") for d in body["decisions"]):
            app.state.backend += 1
            # 잡힌 이름은 승인할 수 없다(agent-runtime/runtime/hitl.py)
            return runtime_refusal(422, "decision_not_allowed", "x")
        return StreamingResponse(
            resumed(sid, body["decisions"][0]),
            media_type="text/event-stream",
            headers=headers(sid, body),
        )

    @app.post("/sessions/{sid}/cancel", status_code=202)
    async def cancel(sid: str):
        return runtime_refusal(409, "no_turn_in_progress", "x")

    @app.delete("/sessions/{sid}")
    async def delete(sid: str):
        app.state.deleted.append(sid)
        return JSONResponse({"session_id": sid})

    return app


@pytest.fixture
def evaluated(client, fake_clusters, mongo):
    """배포한 Agent 와 그 클러스터에 붙인 가짜 런타임이다."""
    agent = deployed_agent(client, fake_clusters, mongo, runtime=False)
    runtime = scripted_runtime()
    agent.fake.service_app = runtime
    return agent, runtime


def seed_set(
    client, agent_id: str, contents: dict[str, CaseContent], *, repeats: int = 1
) -> list[EvaluationCase]:
    """세트 `set-1` 과 그 사례를 넣는다."""
    store = client.app.state.evaluation_sets.store
    item = EvaluationSet(
        id="set-1",
        tenant="default",
        agent_id=agent_id,
        name="평가",
        repeats=repeats,
        created_by=HEADER_SUB,
        updated_by=HEADER_SUB,
    )
    client.portal.call(store.create_set, item)
    cases = [case_doc(cid, item.id, agent_id, c) for cid, c in contents.items()]
    for case in cases:
        client.portal.call(store.reserve_case, item, HEADER_SUB)
        client.portal.call(store.insert_case, case)
    return cases


def new_run(
    client,
    agent_id: str,
    cases: list[EvaluationCase],
    *,
    repeats: int = 1,
    revision: int = 1,
    header_mode: bool = True,
    by: str = HEADER_SUB,
    **kw: Any,
) -> EvaluationRun:
    runs = client.app.state.evaluation_runner.runs
    run = EvaluationRun(
        tenant="default",
        agent_id=agent_id,
        set_id="set-1",
        set_digest="d",
        revision=revision,
        cases=[RunCase(id=c.id, digest=c.digest) for c in cases],
        repeats=repeats,
        by=RunActor(sub=by, username=by),
        header_mode=header_mode,
        **kw,
    )
    return client.portal.call(runs.create, run)


def drive(client, run_id: str, owner: str = OWNER) -> EvaluationRun:
    """실행기를 한 조각 몰고 실행 문서를 다시 읽는다."""
    runner = client.app.state.evaluation_runner
    client.portal.call(runner.drive, run_id, owner, utcnow() + timedelta(minutes=5))
    run = client.portal.call(runner.runs.by_id, run_id)
    assert run is not None
    return run


def take_day(client) -> str | None:
    runs = client.app.state.evaluation_runner.runs
    return client.portal.call(runs.take_day, "default", utcnow(), 5)


def decisions_sent(app: FastAPI) -> list[dict[str, Any]]:
    return [d for kind, body in app.state.bodies if kind == "decisions" for d in body["decisions"]]


def bodies_json(app: FastAPI) -> str:
    return json.dumps([body for _kind, body in app.state.bodies], ensure_ascii=False)


def verdict(
    case: str,
    passed: bool | None,
    *,
    repeat: int = 0,
    state: str = "done",
    counted: bool = True,
    digest: str = "d",
    **more: Any,
) -> CaseVerdict:
    """사례와 반복 하나의 판정. passed 는 확인 항목이 모두 지났는가다."""
    return CaseVerdict(
        case_id=case,
        case_digest=digest,
        repeat=repeat,
        state=state,
        counted=counted,
        checks_passed=passed,
        **more,
    )


class DirectEndpoints:
    """늘 직접 경로를 고르는 채점 모델의 길(`LlmEndpoints` 자리). 고른 목적과 Agent 를 남긴다."""

    def __init__(self) -> None:
        self.asked: list[tuple[str, str | None]] = []

    async def resolve_endpoint(
        self, cfg: Any, *, tenant: str, purpose: str, agent_id: str | None = None
    ) -> Endpoint:
        self.asked.append((purpose, agent_id))
        return Endpoint(None, "key", None, "direct")
