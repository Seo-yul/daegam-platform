"""세션 토큰 예산(F2c): 스레드 상태 누적, 초과 시 턴 종료 error{budget_exceeded}, 이후 /chat 429."""

from __future__ import annotations

import copy

from runtime.budget import TOKENS_KEY, usage_tokens
from runtime.llm_http import usage_from_metadata
from runtime.stream import thread_config
from runtime.trace import Usage
from tests.app_fixtures import sid_of
from tests.conftest import chat, event_names, events_named, run_app
from tests.store_fixtures import pod_store


def test_usage_tokens_sums_input_and_output() -> None:
    assert usage_tokens(None) == 0
    assert usage_tokens({}) == 0
    assert usage_tokens({"input_tokens": 3, "output_tokens": 4, "total_tokens": 7}) == 7


def test_turn_usage_counts_cache_reads_apart_from_input() -> None:
    """캐시에서 읽은 입력은 input_tokens 안의 몫이다. done·전사가 따로 싣는다. cache 쓰기와
    reasoning 도 따로 싣는다(P37)."""
    usage = Usage()
    usage.add(
        usage_from_metadata(
            {
                "input_tokens": 8_000,
                "output_tokens": 40,
                "total_tokens": 8_040,
                "input_token_details": {"cache_read": 7_168, "cache_creation": 512},
                "output_token_details": {"reasoning": 16},
            }
        )
    )
    usage.add(usage_from_metadata({"input_tokens": 200, "output_tokens": 10, "total_tokens": 210}))
    assert usage.to_dict() == {
        "input_tokens": 8_200,
        "output_tokens": 50,
        "total_tokens": 8_250,
        "cached_input_tokens": 7_168,
        "cache_write_tokens": 512,
        "reasoning_tokens": 16,
    }


async def test_session_budget_ends_turn_and_rejects_next_chat(support_doc) -> None:
    support_doc["service"]["budget"]["session_tokens"] = 1
    async with run_app(support_doc) as running:
        response, sse = await chat(running, '{"tool": "Add", "args": {"a": 1, "b": 2}}')
        assert response.status_code == 200
        # 첫 모델 호출이 예산을 넘어 툴은 돌지 않고 error 로 끝난다
        names = event_names(sse)
        assert names[-1] == "error" and "done" not in names and "tool_result" not in names
        error = sse[-1][1]
        assert error["code"] == "budget_exceeded" and "1" in error["message"]
        sid = error["session_id"]

        # 색인에는 쓴 토큰이 남고 세션은 닫히지 않는다
        row = (await running.client.get(f"/sessions/{sid}")).json()["session"]
        assert row["status"] == "active" and row["usage"]["input_tokens"] > 0
        # 스레드 상태에도 누적값이 있다(체크포인트와 같이 저장된다)
        snapshot = await running.runtime.agent.graph.aget_state(thread_config(sid))
        assert snapshot.values[TOKENS_KEY] > 1

        # 같은 세션의 다음 턴은 입구에서 429 다
        response, _ = await chat(running, "다시", session_id=sid)
        assert response.status_code == 429
        assert response.json()["code"] == "session_budget_exceeded"

        # 새 세션은 열 수 있다
        response, sse = await chat(running, "새로")
        assert response.status_code == 200 and sse[-1][0] == "error"

        budget = running.observed("budget")
        assert budget[0]["scope"] == "session" and budget[0]["action"] == "turn_ended"
        assert budget[0]["used"] > budget[0]["limit"] == 1 and budget[0]["session_id"] == sid
        assert budget[1]["action"] == "rejected" and budget[1]["session_id"] == sid
        ends = running.observed("turn_end")
        assert ends[0]["status"] == "budget_exceeded"
        assert running.ended("budget_exceeded") == 2
        assert running.rejections() == ["session_budget_exceeded"]


async def test_session_budget_accumulates_across_turns_and_pods(support_doc) -> None:
    """예산이 넉넉한 파드에서 쓴 토큰이 스레드 상태로 남아 다른 파드의 판정에 들어간다."""
    support_doc["main"]["tools"] = []
    async with pod_store(support_doc) as store:
        roomy = copy.deepcopy(support_doc)
        roomy["service"]["budget"]["session_tokens"] = 1_000_000
        async with run_app(roomy, store=store) as pod_a:
            _, sse = await chat(pod_a, "첫 턴")
            done = events_named(sse, "done")[0]
            sid = done["session_id"]
            first = done["usage"]["input_tokens"] + done["usage"]["output_tokens"]
            assert first > 0
            snapshot = await pod_a.runtime.agent.graph.aget_state(thread_config(sid))
            assert snapshot.values[TOKENS_KEY] == first

        tight = copy.deepcopy(support_doc)
        tight["service"]["budget"]["session_tokens"] = first + 1
        async with run_app(tight, store=store) as pod_b:
            # 색인 누적(first) 은 아직 한도 아래라 입구를 지나지만 이번 턴이 누적을 넘긴다
            response, sse = await chat(pod_b, "둘째 턴", session_id=sid)
            assert response.status_code == 200
            assert sse[-1][0] == "error" and sse[-1][1]["code"] == "budget_exceeded"
            budget = pod_b.observed("budget")[0]
            assert budget["used"] > first + 1 == budget["limit"]
            response, _ = await chat(pod_b, "셋째 턴", session_id=sid)
            assert response.status_code == 429
            assert response.json()["code"] == "session_budget_exceeded"


async def test_no_budget_means_no_middleware(support_doc) -> None:
    support_doc["service"]["budget"]["session_tokens"] = None
    async with run_app(support_doc) as running:
        assert running.runtime.agent.session_budget is None
        _, sse = await chat(running, "hi")
        sid = sid_of(sse, "done")
        snapshot = await running.runtime.agent.graph.aget_state(thread_config(sid))
        assert TOKENS_KEY not in snapshot.values
