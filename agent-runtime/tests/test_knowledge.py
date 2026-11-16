"""knowledge 검색 도구(D32): 질의 문장, 결과 JSON, 오류가 status: error 도구 결과로, 기동 검사.
인용(P36 7절): 세션 색인의 `cite_seq` 에서 이어지는 번호, 한 턴 같은 조각 같은 번호, 본문 없는
artifact, 설명 문장, 막히지 않은 결과에만 나는 `citations` 이벤트와 서브에이전트가 같은 장부를
쓰는 것.

Postgres 없이 돈다. 가짜 풀이 문장·파라미터를 기록하고 정해 둔 행을 돌려준다.
"""

from __future__ import annotations

import copy
import io
import json
from contextlib import asynccontextmanager
from types import SimpleNamespace
from typing import Any

import httpx2
import openai
import psycopg
import pytest
from langchain_core.messages import ToolMessage
from langchain_core.tools import ToolException

import runtime.dsn as dsn_module
import runtime.knowledge as knowledge_module
from runtime import llm_http
from runtime.citations import CitationLedger
from runtime.config import ConfigError, parse_config
from runtime.events import EventLog
from runtime.guards import ToolGuard, parse_groups
from runtime.knowledge import (
    CITE_INSTRUCTION,
    DESCRIPTION_SUFFIX,
    MAX_RESULT_BYTES,
    KnowledgeTools,
    StubEmbeddings,
    build_embeddings,
    build_search_tool,
    format_tool_error,
    vector_literal,
)
from runtime.long_term import TurnContext
from runtime.stub import StubChatModel
from runtime.stub_embedding import hash_embedding
from runtime.trace import RunTracer
from tests.app_fixtures import (
    CITATIONS_FIXTURE,
    deny_rule,
    guardrail,
    mask_rule,
    sid_of,
    tool_message,
)
from tests.conftest import chat, event_names, events_named, load_fixture, offline_doc, run_app
from tests.pg_fakes import FakePool

TABLE = "daegam_kb_3f2a9c1e5b7d4e8fa6c0b1d2e3f4a5b6"
VECTOR_SQL = (
    "SELECT content, doc_id, chunk_no, meta, 1 - (embedding <=> %s::vector) AS score "
    f'FROM "{TABLE}" ORDER BY embedding <=> %s::vector LIMIT %s'
)
HALFVEC_SQL = (
    "SELECT content, doc_id, chunk_no, meta, "
    "1 - (embedding::halfvec(3072) <=> %s::halfvec(3072)) AS score "
    f'FROM "{TABLE}" ORDER BY embedding::halfvec(3072) <=> %s::halfvec(3072) LIMIT %s'
)


def _row(
    content: str, chunk: int, score: float, name: str = "faq.md", doc_id: str = "doc-1"
) -> tuple[Any, ...]:
    return (content, doc_id, chunk, {"name": name, "source": "upload"}, score)


KB = {"id": "3f2a9c1e5b7d4e8fa6c0b1d2e3f4a5b6", "name": "faq"}


def _runtime(ledger: CitationLedger | None = None) -> Any:
    """ToolNode 가 넣어 주는 ToolRuntime 대신이다. 도구는 문맥의 인용 장부만 읽는다."""
    context = TurnContext(owner_sub="alice", citations=ledger or CitationLedger())
    return SimpleNamespace(context=context)


async def _call(
    tool: Any, query: str = "q", *, ledger: CitationLedger | None = None, call_id: str = "call-1"
) -> ToolMessage:
    args = {"query": query, "runtime": _runtime(ledger)}
    return await tool.ainvoke({"name": tool.name, "args": args, "id": call_id, "type": "tool_call"})


def _results(message: ToolMessage) -> list[dict[str, Any]]:
    return json.loads(message.content)["results"]


def _item(**overrides: Any) -> dict[str, Any]:
    item = copy.deepcopy(load_fixture("knowledge-agent.json")["knowledge"][0])
    for key, value in overrides.items():
        section, _, field = key.partition("__")
        if field:
            item[section][field] = value
        else:
            item[section] = value
    return item


async def test_vector_query_and_result_shape() -> None:
    pool = FakePool([_row("환불은 7일 안에", 0, 0.83333), _row("배송은 이틀", 3, 0.5)])
    tool = build_search_tool(_item(), pool, StubEmbeddings(64))

    assert tool.name == "search_faq"
    assert tool.description.endswith(DESCRIPTION_SUFFIX)
    assert tool.description.startswith("Search the knowledge base 'faq'")
    assert list(tool.args) == ["query"]

    message = await _call(tool, "환불 기간")
    assert _results(message) == [
        {"cite": 1, "content": "환불은 7일 안에", "source": "faq.md", "chunk": 0, "score": 0.8333},
        {"cite": 2, "content": "배송은 이틀", "source": "faq.md", "chunk": 3, "score": 0.5},
    ]
    (sql_text, params) = pool.calls[0]
    literal = vector_literal(hash_embedding("환불 기간", 64))
    assert sql_text == VECTOR_SQL
    assert params == (literal, literal, 5)


async def test_halfvec_query_above_2000_dimensions() -> None:
    pool = FakePool([_row("a", 0, 0.9)])
    tool = build_search_tool(_item(store__dimensions=3072), pool, StubEmbeddings(3072))
    await _call(tool)
    sql_text, params = pool.calls[0]
    assert sql_text == HALFVEC_SQL
    literal = vector_literal(hash_embedding("q", 3072))
    assert params == (literal, literal, 5)


async def test_min_score_filters_results() -> None:
    pool = FakePool([_row("high", 0, 0.9), _row("low", 1, 0.3), _row("edge", 2, 0.5)])
    tool = build_search_tool(_item(tool__min_score=0.5, tool__top_k=3), pool, StubEmbeddings(64))
    assert [r["content"] for r in _results(await _call(tool))] == ["high", "edge"]
    assert pool.calls[0][1][2] == 3


async def test_results_truncated_to_12kb_from_the_tail() -> None:
    """뒤 항목부터 뺀다. 뺀 결과는 번호를 받지 않아 장부의 번호가 비지 않는다."""
    rows = [_row(f"{i}" + "가" * 3000, i, 0.9 - i / 100) for i in range(6)]
    tool = build_search_tool(_item(tool__top_k=6), FakePool(rows), StubEmbeddings(64))
    ledger = CitationLedger(4)
    message = await _call(tool, ledger=ledger)
    assert len(message.content.encode("utf-8")) <= MAX_RESULT_BYTES
    kept = _results(message)
    assert [r["chunk"] for r in kept] == list(range(len(kept))) and 0 < len(kept) < 6
    assert [r["cite"] for r in kept] == [5 + i for i in range(len(kept))]
    assert [c["n"] for c in message.artifact["citations"]] == [r["cite"] for r in kept]
    assert ledger.seq == 4 + len(kept)


async def test_undefined_table_is_empty_result() -> None:
    pool = FakePool(error=psycopg.errors.UndefinedTable("relation does not exist"))
    tool = build_search_tool(_item(), pool, StubEmbeddings(64))
    message = await _call(tool)
    assert json.loads(message.content) == {"results": []}
    assert message.artifact == {"citations": []}


async def test_db_error_becomes_error_tool_message() -> None:
    """RuntimeError 도 ToolException(db_error) 로 바뀌어 status: error 도구 결과가 된다.
    BaseTool.arun 은 ToolException 만 handle_tool_error 로 보내기 때문이다."""
    pool = FakePool(error=RuntimeError("host=db password=hunter2 dbname=kb"))
    tool = build_search_tool(_item(), pool, StubEmbeddings(64))
    message = await _call(tool)
    assert isinstance(message, ToolMessage) and message.status == "error"
    assert json.loads(message.content) == {"error": {"code": "db_error", "message": "RuntimeError"}}
    assert "hunter2" not in message.content
    # 오류 결과에는 출처가 없다. 번역기는 citations 를 내지 않는다
    assert message.artifact is None
    # 원인 예외가 없으면 message 는 클래스 이름 대신 error 다
    assert json.loads(format_tool_error(ToolException("db_error"))) == {
        "error": {"code": "db_error", "message": "error"}
    }


async def test_embedding_error_becomes_error_tool_message() -> None:
    class Broken(StubEmbeddings):
        async def aembed_query(self, text: str) -> list[float]:
            raise ValueError("Incorrect API key provided: sk-abc")

    pool = FakePool([_row("a", 0, 0.9)])
    tool = build_search_tool(_item(), pool, Broken(64))
    message = await _call(tool, call_id="call-2")
    assert message.status == "error"
    assert json.loads(message.content) == {
        "error": {"code": "embedding_failed", "message": "ValueError"}
    }
    assert "sk-abc" not in message.content
    assert pool.calls == []


async def test_an_exhausted_embedding_quota_is_its_own_tool_error() -> None:
    """임베딩 provider 의 크레딧이 바닥나면 도구 결과의 코드가 quota_exhausted 다. 모델이 일시
    장애로 읽고 다시 해 보라고 답하지 않게 한다(T20)."""

    class Exhausted(Exception):
        status_code = 429
        code = "insufficient_quota"

    class Broken(StubEmbeddings):
        async def aembed_query(self, text: str) -> list[float]:
            raise Exhausted("You exceeded your current quota")

    tool = build_search_tool(_item(), FakePool([]), Broken(64))
    message = await _call(tool, call_id="call-3")
    assert json.loads(message.content) == {
        "error": {"code": "quota_exhausted", "message": "Exhausted"}
    }


async def test_a_transient_embedding_error_is_retried_once_with_a_new_call_id(
    monkeypatch,
) -> None:
    """일시적인 임베딩 실패는 새 호출 id 로 한 번 더 부른다. 요청마다 트레이서가
    `model_call{operation: embeddings}` 를 내고, 그 스팬 id 가 요청의 상관 헤더와 같다. 사용량은
    글자 수 추정이고 턴 사용량에 들지 않는다(P37 2절)."""
    seen: list[llm_http.CallId] = []

    class Busy(Exception):
        status_code = 503

    class Flaky(StubEmbeddings):
        async def aembed_query(self, text: str) -> list[float]:
            seen.append(llm_http.current_call())
            if len(seen) == 1:
                raise Busy("try later")
            return await super().aembed_query(text)

    monkeypatch.setattr(llm_http, "retry_delay", lambda _e=None: 0)
    out = io.StringIO()
    log = EventLog(parse_config(load_fixture("knowledge-agent.json")), out=out)
    tracer = RunTracer()
    tracer.begin(log, where={}, sources={})
    runtime = SimpleNamespace(context=TurnContext(owner_sub="alice", trace_id=tracer.trace_id))
    tool = build_search_tool(_item(), FakePool([_row("a", 0, 0.9)]), Flaky(64))
    call = {"name": tool.name, "args": {"query": "환불 기한", "runtime": runtime}, "id": "c1"}
    message = await tool.ainvoke({**call, "type": "tool_call"}, config={"callbacks": [tracer]})
    spans = [json.loads(line) for line in out.getvalue().splitlines()]
    embeds = [e for e in spans if e["daegam_event"] == "model_call"]
    assert message.status == "success" and len(seen) == 2
    assert [(e["attempt"], e["status"]) for e in embeds] == [(1, "error"), (2, "ok")]
    assert [e["span_id"] for e in embeds] == [c.span_id for c in seen]
    assert {c.trace_id for c in seen} == {tracer.trace_id}
    assert embeds[0]["error_type"] == "Busy" and embeds[1]["operation"] == "embeddings"
    assert embeds[1]["estimated"] is True and embeds[1]["usage"]["input_tokens"] == 2
    (exec_span,) = [e for e in spans if e["daegam_event"] == "tool_exec"]
    assert {e["parent_span_id"] for e in embeds} == {exec_span["span_id"]}
    assert tracer.usage.input_tokens == 0


async def test_gateway_answers_are_their_own_tool_errors_and_not_retried(monkeypatch) -> None:
    """게이트웨이 경로(P38 F5)의 임베딩이 게이트웨이의 한도에 걸리면 도구 결과의 코드가
    llm_gateway_rate_limited 이고 다시 부르지 않는다. 1분 창이라 1·2초 뒤에도 같기 때문이다.
    연결할 수 없는 게이트웨이는 한 번 더 부른 뒤 llm_gateway_unreachable 이다. 직접 경로의 같은
    연결 오류는 embedding_failed 다."""
    monkeypatch.setattr(llm_http, "retry_delay", lambda _e=None: 0)
    calls: list[int] = []
    request = httpx2.Request("POST", "http://llm-gateway/p/e/v1/embeddings")
    limited = openai.RateLimitError(
        "x",
        response=httpx2.Response(429, request=request, headers={"x-ratelimit-remaining": "0"}),
        body="",
    )

    class Raising(StubEmbeddings):
        def __init__(self, error: Exception) -> None:
            super().__init__(64)
            self.error = error

        async def aembed_query(self, text: str) -> list[float]:
            calls.append(1)
            raise self.error

    gateway = _item()
    gateway["embedding"]["provider"]["gateway"] = True
    message = await _call(build_search_tool(gateway, FakePool([]), Raising(limited)))
    assert json.loads(message.content)["error"]["code"] == "llm_gateway_rate_limited"
    assert len(calls) == 1
    refused = openai.APIConnectionError(request=request)
    message = await _call(build_search_tool(gateway, FakePool([]), Raising(refused)))
    assert json.loads(message.content)["error"]["code"] == "llm_gateway_unreachable"
    assert len(calls) == 3
    message = await _call(build_search_tool(_item(), FakePool([]), Raising(refused)))
    assert json.loads(message.content)["error"]["code"] == "embedding_failed"


def test_gateway_embeddings_carry_the_connect_limit(monkeypatch) -> None:
    """게이트웨이 경로의 임베딩 클라이언트는 토큰을 키 자리로 보내고 연결 제한이 5초다."""
    monkeypatch.setenv("DAEGAM_KNOWLEDGE_0_API_KEY", "llm-token")
    provider = {
        "name": "openai-main",
        "kind": "openai",
        "env_key": "DAEGAM_KNOWLEDGE_0_API_KEY",
        "base_url": "http://llm-gateway.daegam-llm.svc.cluster.local:8080/p/openai-main/v1",
        "gateway": True,
    }
    client = build_embeddings(
        _item(
            embedding={"provider": provider, "model": "text-embedding-3-small"},
            store__dimensions=1536,
        )
    )
    assert client.openai_api_key.get_secret_value() == "llm-token"
    assert client.request_timeout == llm_http.gateway_timeout(60)


def test_stub_selected_by_embedding_provider_kind(monkeypatch) -> None:
    """선택 기준은 embedding.provider.kind 이고 최상위 stub 은 보지 않는다."""
    assert isinstance(build_embeddings(_item()), StubEmbeddings)
    assert build_embeddings(_item()).dimensions == 64

    monkeypatch.setenv("DAEGAM_KNOWLEDGE_0_API_KEY", "k")
    openai_provider = {
        "name": "openai-main",
        "kind": "openai",
        "env_key": "DAEGAM_KNOWLEDGE_0_API_KEY",
        "base_url": "http://gateway.local/v1",
    }
    small = build_embeddings(
        _item(
            embedding={"provider": openai_provider, "model": "text-embedding-3-small"},
            store__dimensions=1536,
        )
    )
    assert type(small).__name__ == "OpenAIEmbeddings"
    assert small.model == "text-embedding-3-small" and small.dimensions == 1536
    assert small.check_embedding_ctx_length is False
    # SDK 재시도는 없다. 호출 id 하나가 요청 하나라 검색 도구가 다시 부른다(P37 2절)
    assert small.chunk_size == 64 and small.max_retries == 0 and small.request_timeout == 60
    assert llm_http.has_correlation_hook(small.http_async_client)
    assert small.openai_api_base == "http://gateway.local/v1"

    other = build_embeddings(
        _item(embedding={"provider": openai_provider, "model": "bge-m3"}, store__dimensions=1024)
    )
    assert other.dimensions is None


async def test_open_rejects_missing_provider_key(monkeypatch) -> None:
    monkeypatch.delenv("DAEGAM_KNOWLEDGE_0_API_KEY", raising=False)
    monkeypatch.setenv("DAEGAM_DB_0_URL", "postgresql://u:p@db/kb")
    pool = FakePool()

    async def fake_open_pool(dsn: str) -> FakePool:
        return pool

    monkeypatch.setattr(knowledge_module, "open_pool", fake_open_pool)
    doc = load_fixture("knowledge-agent.json")
    doc["knowledge"][0]["embedding"] = {
        "provider": {
            "name": "openai-main",
            "kind": "openai",
            "env_key": "DAEGAM_KNOWLEDGE_0_API_KEY",
            "base_url": None,
        },
        "model": "text-embedding-3-small",
    }
    doc["knowledge"][0]["store"]["dimensions"] = 1536
    with pytest.raises(ConfigError) as info:
        await KnowledgeTools.open(parse_config(doc))
    assert info.value.code == "provider_key_missing"
    assert "postgresql://" not in info.value.message
    assert pool.closed


async def test_open_shares_pool_per_dsn_and_checks_select_1(monkeypatch) -> None:
    monkeypatch.setenv("DAEGAM_DB_0_URL", "postgresql://u:p@db/kb")
    monkeypatch.setenv("DAEGAM_DB_1_URL", "postgresql://u:p@other/kb")
    opened: list[FakePool] = []

    async def fake_open_pool(dsn: str) -> FakePool:
        opened.append(FakePool())
        return opened[-1]

    monkeypatch.setattr(knowledge_module, "open_pool", fake_open_pool)
    doc = load_fixture("knowledge-agent.json")
    second = copy.deepcopy(doc["knowledge"][0])
    second["ref"] = "kb-policy"
    second["tool"]["name"] = "search_policy"
    third = copy.deepcopy(second)
    third["ref"] = "kb-other"
    third["tool"]["name"] = "search_other"
    third["store"]["dsn_env"] = "DAEGAM_DB_1_URL"
    doc["knowledge"] += [second, third]
    doc["main"]["knowledge"] = ["kb-faq", "kb-policy"]
    doc["subagents"][0]["knowledge"] = ["kb-other"]

    tools = await KnowledgeTools.open(parse_config(doc))
    assert len(opened) == 2
    assert [call[0] for call in opened[0].calls] == ["SELECT 1"]
    assert [t.name for t in tools.tools_for(["kb-faq", "kb-policy", "kb-other"])] == [
        "search_faq",
        "search_policy",
        "search_other",
    ]
    assert [t.name for t in tools.tools_for(["kb-policy", "kb-faq"])] == [
        "search_policy",
        "search_faq",
    ]
    assert tools.tools_for([]) == []
    await tools.close()
    assert all(pool.closed for pool in opened)


@pytest.mark.parametrize(
    ("rejects", "code"),
    [(False, "knowledge_db_unreachable"), (True, "knowledge_db_rejected")],
)
async def test_open_reports_a_failed_db_and_closes_its_pool(
    monkeypatch, rejects: bool, code: str
) -> None:
    """풀은 기다리지 않고 열리므로 연결 실패는 연결 확인에서 드러난다. 연결할 수 없으면
    knowledge_db_unreachable, 접속이 거부되면 knowledge_db_rejected 이고 둘 다 기동이 기다린다(T22,
    T23). 연 풀은 닫고 원문(DSN)은 싣지 않는다."""
    monkeypatch.setenv("DAEGAM_DB_0_URL", "postgresql://u:secret@db/kb")
    pool = FakePool(failing=True)

    async def fake_open_pool(dsn: str) -> FakePool:
        return pool

    async def postgres_rejects(conninfo: str) -> bool:
        return rejects

    monkeypatch.setattr(knowledge_module, "open_pool", fake_open_pool)
    monkeypatch.setattr(dsn_module, "_postgres_rejects", postgres_rejects)
    with pytest.raises(ConfigError) as info:
        await KnowledgeTools.open(parse_config(load_fixture("knowledge-agent.json")))
    assert info.value.code == code
    assert "secret" not in info.value.message
    assert pool.closed


async def test_open_requires_dsn(monkeypatch) -> None:
    monkeypatch.delenv("DAEGAM_DB_0_URL", raising=False)
    monkeypatch.delenv("DAEGAM_DB_0_HOST", raising=False)
    with pytest.raises(ConfigError) as info:
        await KnowledgeTools.open(parse_config(load_fixture("knowledge-agent.json")))
    assert info.value.code == "knowledge_dsn_missing"


@asynccontextmanager
async def _knowledge_app(
    monkeypatch, pool: FakePool, doc: dict[str, Any] | None = None, **app: Any
):
    """가짜 풀로 검색 도구를 연 앱이다. 문서를 안 주면 표본 knowledge-agent 에서 memory 를 뺀
    것이다."""
    monkeypatch.setenv("DAEGAM_DB_0_URL", "postgresql://u:p@db/kb")

    async def fake_open_pool(dsn: str) -> FakePool:
        return pool

    monkeypatch.setattr(knowledge_module, "open_pool", fake_open_pool)
    if doc is None:
        doc = offline_doc("knowledge-agent.json")
    tools = await KnowledgeTools.open(parse_config(doc))
    try:
        async with run_app(doc, knowledge=tools, **app) as running:
            yield running
    finally:
        await tools.close()


SEARCH = tool_message("search_faq", query="환불")


async def test_stub_turn_calls_search_tool_and_error_keeps_turn_alive(monkeypatch) -> None:
    """그래프 안에서: 스텁 모델이 search_faq 를 부르고 결과가 ToolMessage 로 돌아온다. DB 오류는
    status: error 도구 결과가 되고 턴은 done 으로 끝난다. 오류 결과에는 citations 가 없다. 앱을
    닫아도 검색 도구의 풀은 연 쪽이 닫는다."""
    monkeypatch.setenv("DAEGAM_DB_0_URL", "postgresql://u:p@db/kb")
    pool = FakePool([_row("환불은 7일 안에", 0, 0.9)])

    async def fake_open_pool(dsn: str) -> FakePool:
        return pool

    monkeypatch.setattr(knowledge_module, "open_pool", fake_open_pool)
    doc = offline_doc("knowledge-agent.json")
    tools = await KnowledgeTools.open(parse_config(doc))
    try:
        async with run_app(doc, knowledge=tools) as running:
            start = running.observed("runtime_start")[0]
            assert start["tools"] == ["search_faq"]

            _, sse = await chat(running, SEARCH)
            names = event_names(sse)
            assert "tool_call" in names and "tool_result" in names and names[-1] == "done"
            result = events_named(sse, "tool_result")[0]
            assert result["name"] == "search_faq" and result["status"] == "success"
            done = events_named(sse, "done")[0]
            assert '"환불은 7일 안에"' in done["text"]

            pool.error = RuntimeError("boom")
            _, sse = await chat(running, SEARCH)
            result = events_named(sse, "tool_result")[0]
            assert result["status"] == "error"
            assert event_names(sse)[-1] == "done"
            assert events_named(sse, "citations") == []
            done = events_named(sse, "done")[0]
            assert '"code": "db_error"' in done["text"]
        assert not pool.closed
    finally:
        await tools.close()


# ---- 인용(P36 7절) ------------------------------------------------------------------------


def test_cite_numbers_continue_from_the_session_and_repeat_within_the_turn() -> None:
    """번호는 세션의 마지막 번호 다음부터이고, 한 턴 안의 같은 조각은 두 번째 검색에서도 같은
    번호다. 겹친 조각이 한 목록 안에 두 번 있어도 같은 번호다. preview 는 장부를 바꾸지 않는다."""
    searcher = knowledge_module.KnowledgeSearch(_item(), FakePool(), StubEmbeddings(64))
    ledger = CitationLedger(7)
    first = [_row("a", 0, 0.9), _row("b", 3, 0.8)]
    second = [_row("b", 3, 0.8), _row("c", 5, 0.7), _row("b2", 3, 0.6, doc_id="doc-2")]

    text, _ = searcher.cited(first, ledger)
    assert [r["cite"] for r in json.loads(text)["results"]] == [8, 9]
    assert ledger.preview([("knowledge", KB["id"], "doc-9", 0)]) == [10]
    text, source = searcher.cited(second, ledger)
    assert [r["cite"] for r in json.loads(text)["results"]] == [9, 10, 11]
    assert [c["n"] for c in source["citations"]] == [9, 10, 11]
    assert ledger.seq == 11
    assert ledger.preview([("k", 1), ("k", 1), ("k", 2)]) == [12, 12, 13]


async def test_artifact_carries_the_source_without_the_body() -> None:
    """모델이 보지 않는 artifact 에 출처가 실린다. 가림을 거치지 않는 자리라 본문은 없고 칸은 표본의
    citations 항목과 같다."""
    row = ("환불은 7일 안에", "doc-1", 4, {"name": "환불.md", "url": "https://x.test/r"}, 0.71234)
    tool = build_search_tool(_item(), FakePool([row]), StubEmbeddings(64))
    message = await _call(tool, ledger=CitationLedger(2))
    (item,) = message.artifact["citations"]
    fixture_item = CITATIONS_FIXTURE["events"]["citations"]["items"][0]
    assert item.keys() == fixture_item.keys()
    assert item == {
        "n": 3,
        "kind": "knowledge",
        "title": "환불.md",
        "url": "https://x.test/r",
        "kb": KB,
        "doc_id": "doc-1",
        "chunk": 4,
        "score": 0.7123,
    }
    assert "환불은 7일 안에" not in json.dumps(message.artifact, ensure_ascii=False)


def test_description_asks_the_model_to_cite() -> None:
    """설명 끝의 고정 문장이 번호를 대괄호로 달라고 알린다. 시스템 프롬프트는 건드리지 않는다."""
    tool = build_search_tool(_item(), FakePool(), StubEmbeddings(64))
    assert tool.description.endswith(CITE_INSTRUCTION)
    assert "like [3]" in CITE_INSTRUCTION and "Cite only numbers you received" in CITE_INSTRUCTION


async def test_citations_follow_the_search_result_and_continue_across_turns(monkeypatch) -> None:
    """막히지 않은 검색 결과 바로 뒤에 citations 가 나고 칸은 표본과 같다. 전사의 도구 카드가 같은
    출처를 들고, 턴이 끝나면 색인의 cite_seq 가 마지막 번호다. 다음 턴은 그 다음 번호부터다."""
    pool = FakePool([_row("환불은 7일 안에", 0, 0.9), _row("배송은 이틀", 3, 0.5)])
    events = CITATIONS_FIXTURE["events"]
    async with _knowledge_app(monkeypatch, pool) as running:
        _, sse = await chat(running, SEARCH)
        names = event_names(sse)
        assert names[names.index("tool_result") + 1] == "citations"
        result, cited = events_named(sse, "tool_result")[0], events_named(sse, "citations")[0]
        assert cited.keys() == events["citations"].keys()
        assert cited["call_id"] == result["id"]
        assert [c["n"] for c in cited["items"]] == [1, 2]
        content = json.loads(result["content"])["results"]
        fixture_content = json.loads(events["tool_result"]["content"])["results"]
        assert [r.keys() for r in content] == [r.keys() for r in fixture_content]
        sid = sid_of(sse, "done")
        assert (await running.runtime.store.index.get(sid)).cite_seq == 2

        detail = (await running.client.get(f"/sessions/{sid}")).json()
        card = next(i for i in detail["transcript"]["items"] if i["kind"] == "tool_call")
        fixture_card = next(i for i in CITATIONS_FIXTURE["transcript"] if i["kind"] == "tool_call")
        assert card.keys() == fixture_card.keys()
        assert card["citations"] == cited["items"]

        _, sse = await chat(running, SEARCH, session_id=sid)
        assert [c["n"] for c in events_named(sse, "citations")[0]["items"]] == [3, 4]
        assert (await running.runtime.store.index.get(sid)).cite_seq == 4


async def test_a_turn_reads_cite_seq_after_taking_the_lease(monkeypatch) -> None:
    """리스 전에 읽은 색인이 낡아도 번호는 리스 안에서 다시 읽은 cite_seq 다음부터다. 그 사이에 끝난
    턴이 나눠 준 번호와 겹치지 않는다."""
    pool = FakePool([_row("환불은 7일 안에", 0, 0.9), _row("배송은 이틀", 3, 0.5)])
    async with _knowledge_app(monkeypatch, pool) as running:
        _, sse = await chat(running, SEARCH)
        sid = sid_of(sse, "done")
        index = running.runtime.store.index
        claim = index.claim_turn

        async def claim_after_another_turn(session_id: str, **kw: Any) -> bool:
            await index.update(session_id, cite_seq=7)
            return await claim(session_id, **kw)

        monkeypatch.setattr(index, "claim_turn", claim_after_another_turn)
        _, sse = await chat(running, SEARCH, session_id=sid)
        assert [c["n"] for c in events_named(sse, "citations")[0]["items"]] == [8, 9]
        assert (await index.get(sid)).cite_seq == 9


class FailAfterToolModel(StubChatModel):
    """도구 결과를 받은 다음 호출에서 실패한다. 검색한 뒤에 오류로 끝나는 턴을 만든다."""

    async def _agenerate(self, messages: Any, stop: Any = None, run_manager: Any = None, **kw: Any):
        if messages and isinstance(messages[-1], ToolMessage):
            raise RuntimeError("검색 뒤에 실패한다")
        return await super()._agenerate(messages, stop, run_manager, **kw)


async def test_a_failed_turn_still_records_the_numbers_it_gave(monkeypatch) -> None:
    """검색한 뒤 오류로 끝난 턴도 마지막 번호를 색인에 적는다. 그 결과가 체크포인트에 남아 다음
    턴의 모델이 읽으므로 다음 턴은 그 다음 번호부터다."""
    doc = offline_doc("knowledge-agent.json")
    doc["main"]["limits"]["retries"] = 0
    pool = FakePool([_row("환불은 7일 안에", 0, 0.9)])
    async with _knowledge_app(monkeypatch, pool, doc, model=FailAfterToolModel()) as running:
        _, sse = await chat(running, SEARCH)
        assert [c["n"] for c in events_named(sse, "citations")[0]["items"]] == [1]
        sid = sid_of(sse, "error")
        assert (await running.runtime.store.index.get(sid)).cite_seq == 1
        _, sse = await chat(running, SEARCH, session_id=sid)
        assert [c["n"] for c in events_named(sse, "citations")[0]["items"]] == [2]


async def test_a_blocked_result_carries_no_citations(monkeypatch) -> None:
    """ToolGuard 가 결과를 막으면 status error 이고 citations 가 없다. 막은 결과는 artifact 도
    비어 번역기 쪽 판정이 어긋나도 출처가 나가지 않는다."""
    doc = offline_doc("knowledge-agent.json")
    doc["guardrails"] = [guardrail("tool", [deny_rule("환불")], "block", "막았습니다")]
    pool = FakePool([_row("환불은 7일 안에", 0, 0.9)])
    async with _knowledge_app(monkeypatch, pool, doc) as running:
        _, sse = await chat(running, SEARCH)
        result = events_named(sse, "tool_result")[0]
        assert (result["status"], result["content"]) == ("error", "막았습니다")
        assert events_named(sse, "citations") == []

    guard = ToolGuard(parse_groups(parse_config(doc)))
    call = {"name": "search_faq", "args": {"query": "q"}, "id": "c1"}
    request = SimpleNamespace(tool_call=call, state={})
    found = ToolMessage(
        content="환불은 7일 안에",
        tool_call_id="c1",
        name="search_faq",
        artifact={"citations": [{}]},
    )

    async def handler(_request: Any) -> ToolMessage:
        return found

    blocked = await guard.awrap_tool_call(request, handler)
    assert (blocked.status, blocked.artifact) == ("error", None)


async def test_masked_text_does_not_come_back_through_the_citations(monkeypatch) -> None:
    """도구 결과의 PII 가림은 본문만 고친다. 출처에는 본문이 없으므로 가린 글이 citations 로
    되살아나지 않고, 가려진 결과에도 출처는 난다."""
    doc = offline_doc("knowledge-agent.json")
    doc["guardrails"] = [guardrail("tool", [mask_rule(r"\d{3}-\d{4}")], "flag")]
    pool = FakePool([_row("고객센터 555-1234 로 연락", 0, 0.9)])
    async with _knowledge_app(monkeypatch, pool, doc) as running:
        response, sse = await chat(running, SEARCH)
        assert "555-1234" not in response.text
        assert [c["n"] for c in events_named(sse, "citations")[0]["items"]] == [1]


async def test_subagent_search_numbers_from_the_same_ledger(monkeypatch) -> None:
    """서브에이전트도 같은 턴 문맥을 받는다. 서브에이전트의 검색이 세션의 다음 번호를 받고 그
    번호가 색인에 남으며, citations 에 서브에이전트 표시(agent)가 붙는다."""
    doc = offline_doc("knowledge-agent.json")
    doc["subagents"][0]["knowledge"] = ["kb-faq"]
    pool = FakePool([_row("환불은 7일 안에", 0, 0.9)])
    task = {"tool": "task", "args": {"description": SEARCH, "subagent_type": "summarizer"}}
    async with _knowledge_app(monkeypatch, pool, doc) as running:
        _, sse = await chat(running, SEARCH)
        sid = sid_of(sse, "done")
        _, sse = await chat(running, json.dumps(task), session_id=sid)
        (cited,) = events_named(sse, "citations")
        assert cited.get("agent") and [c["n"] for c in cited["items"]] == [2]
        assert (await running.runtime.store.index.get(sid)).cite_seq == 2
