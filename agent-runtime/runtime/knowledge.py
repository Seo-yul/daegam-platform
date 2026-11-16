"""knowledge[] 항목을 검색 도구로 만든다(D32).

기동 때 항목마다 `resolve_dsn(store.dsn_env)`(memory 와 같은 규칙)로 DSN 을 풀고, DSN 별로
`AsyncConnectionPool(min 1, max 4)` 하나를 열어(같은 DSN 은 공유) `SELECT 1` 로 연결되는지 본 뒤
임베딩 클라이언트를 만든다. `embedding.provider.kind == "stub"` 이면 플랫폼과 바이트 동일한
`stub_embedding.hash_embedding` 이고, 아니면 langchain `OpenAIEmbeddings` 다. 최상위 `stub` 은
메인 provider 의 것이라 여기서는 보지 않는다.

도구는 `StructuredTool(tool.name, {query}, handle_tool_error)` 하나다. 호출은 `aembed_query` 로
질의를 임베딩하고 `ORDER BY embedding <=> q LIMIT top_k`(2000 차원 위는 인덱스와 같은 `halfvec`
표현식)로 찾은 뒤 `min_score` 로 걸러 `{"results": [{cite, content, source, chunk, score}]}` JSON 을
만든다. 12KB 를 넘으면 뒤 항목부터 뺀다. 테이블이 없으면(`UndefinedTable`) 빈 결과다.

`cite` 는 턴 문맥의 장부(citations.py)가 주는 인용 번호이고 도구 설명 끝의 고정 문장이 모델에게
답에 `[n]` 으로 달라고 알린다(P36 7절). 도구는 `content_and_artifact` 라 모델이 보지 않는
artifact 에 남은 결과마다 출처 `{n, kind, title, url, kb, doc_id, chunk, score}` 를 싣는다. 본문은
싣지 않는다.

임베딩·DB 예외는 도구 코루틴이 `ToolException(code)` 로 바꿔 던지고 `handle_tool_error` 가 그것을
`{"error": {code, message}}` 로 바꿔 `status: error` 인 도구 결과가 된다. langchain `BaseTool.arun`
은 `ToolException` 만 핸들러로 보내고 다른 예외는 다시 던져 턴이 죽기 때문이다. 로그와 메시지에
키·DSN·provider 원문은 싣지 않는다(D14·D25).
"""

from __future__ import annotations

import json
import logging
import os
import time
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

import psycopg
from langchain.tools import ToolRuntime
from langchain_core.embeddings import Embeddings
from langchain_core.tools import BaseTool, StructuredTool, ToolException
from langchain_openai import OpenAIEmbeddings
from psycopg import sql
from psycopg_pool import AsyncConnectionPool
from pydantic import BaseModel, Field

from runtime import llm_http
from runtime.citations import KIND_KNOWLEDGE, CitationLedger, artifact, ledger_of
from runtime.config import AgentConfig, ConfigError
from runtime.dsn import (
    KNOWLEDGE_DB_CODES,
    check_postgres,
    missing_dsn_error,
    resolve_dsn,
)
from runtime.stub_embedding import hash_embedding
from runtime.trace import EMBEDDINGS, ROUTE_DIRECT, ROUTE_GATEWAY, error_kind, signal
from runtime.turn_context import context_field

log = logging.getLogger("daegam.agent-runtime.knowledge")

POOL_MIN_SIZE = 1
POOL_MAX_SIZE = 4
STATEMENT_TIMEOUT_MS = 10_000
# HNSW 는 vector(n) 2000 차원까지다. 그 위는 halfvec 표현식(인덱스와 같은 식)으로 질의한다
HALFVEC_ABOVE_DIMENSIONS = 2000
MAX_RESULT_BYTES = 12 * 1024
CITE_INSTRUCTION = (
    " Each result has a `cite` number. When your answer uses a result, put its number in square"
    " brackets right after the sentence, like [3]. Cite only numbers you received."
)
"""모델에게 인용 번호를 다는 법을 알리는 문장이다(P36 7절). 시스템 프롬프트는 건드리지 않으므로
검색 도구가 없는 에이전트에는 이 문장도 없다."""
DESCRIPTION_SUFFIX = (
    " Results may be fewer than top_k when min_score filters them." + CITE_INSTRUCTION
)

# OpenAIEmbeddings 인자. 플랫폼 services/knowledge/embeddings.py 와 같은 값이다
BATCH_SIZE = 64
TIMEOUT = 60.0
# SDK 재시도는 없다. 호출 id 하나가 HTTP 요청 하나라 다시 부르기는 검색 도구가 새 id 로 한 번 더
# 한다(llm_http.retry_once, P37 2절)
MAX_RETRIES = 0
DIMENSIONS_MODEL_PREFIX = "text-embedding-3"

ERROR_DB = "db_error"
ERROR_EMBEDDING = "embedding_failed"
# 임베딩 provider 의 크레딧이 바닥났다(T20). 모델이 일시 장애로 읽지 않게 따로 가른다
ERROR_QUOTA = "quota_exhausted"


class SearchArgs(BaseModel):
    """검색 도구의 인자다. 질의 문장 하나다."""

    query: str = Field(description="Natural-language query to search the knowledge base for.")


class StubEmbeddings(Embeddings):
    """kind: stub 의 임베딩. 플랫폼 인덱서와 같은 `hash_embedding` 을 써서 e2e 가 성립한다."""

    def __init__(self, dimensions: int):
        self.dimensions = dimensions

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        return [hash_embedding(t, self.dimensions) for t in texts]

    def embed_query(self, text: str) -> list[float]:
        return hash_embedding(text, self.dimensions)

    async def aembed_documents(self, texts: list[str]) -> list[list[float]]:
        return self.embed_documents(texts)

    async def aembed_query(self, text: str) -> list[float]:
        return self.embed_query(text)


def build_embeddings(item: dict[str, Any]) -> Embeddings:
    """knowledge 항목 하나의 임베딩 클라이언트다. 선택 기준은 `embedding.provider.kind` 뿐이다.

    stub 이 아니면 `OpenAIEmbeddings` 이고 키는 `env_key` 가 가리키는 파드 env 에서만 읽는다.
    `dimensions` 인자는 `text-embedding-3*` 에만 보낸다(그 밖의 모델은 인자를 모른다).
    """
    provider = item["embedding"]["provider"]
    model = item["embedding"]["model"]
    dimensions = int(item["store"]["dimensions"])
    if provider["kind"] == "stub":
        return StubEmbeddings(dimensions)
    env_key = provider.get("env_key") or ""
    api_key = os.environ.get(env_key) if env_key else None
    if not api_key:
        raise ConfigError(
            "provider_key_missing",
            f"knowledge[{item['ref']}] 임베딩 provider {provider['name']!r} 의 키 env "
            f"{env_key!r} 가 비어 있다",
        )
    kwargs: dict[str, Any] = {
        "model": model,
        "api_key": api_key,
        "base_url": provider.get("base_url"),
        "check_embedding_ctx_length": False,
        "chunk_size": BATCH_SIZE,
        "max_retries": MAX_RETRIES,
        "timeout": TIMEOUT,
        # 상관 헤더 훅을 단 공유 클라이언트다(P37 2절)
        "http_async_client": llm_http.hooked_async_client(timeout=TIMEOUT),
    }
    if model.startswith(DIMENSIONS_MODEL_PREFIX):
        kwargs["dimensions"] = dimensions
    if provider.get("gateway"):
        # 게이트웨이 경로는 env 의 토큰을 키 자리로 보내고 연결은 5초 안이다 (P38 5절·10절)
        kwargs["timeout"] = llm_http.gateway_timeout(TIMEOUT)
    return OpenAIEmbeddings(**kwargs)


def search_query(table: str, dimensions: int) -> sql.Composed:
    """검색 문장이다. 식별자는 `Identifier` 로만 넣고 점수는 코사인 유사도(`1 - 거리`)다.

    2000 차원 이하는 `vector` 연산이고 그 위는 플랫폼이 만든 halfvec 표현식 인덱스와 같은 식
    `embedding::halfvec(<d>) <=> %s::halfvec(<d>)` 이라야 인덱스를 탄다.
    """
    t = sql.Identifier(table)
    if dimensions <= HALFVEC_ABOVE_DIMENSIONS:
        return sql.SQL(
            "SELECT content, doc_id, chunk_no, meta, 1 - (embedding <=> %s::vector) AS score "
            "FROM {t} ORDER BY embedding <=> %s::vector LIMIT %s"
        ).format(t=t)
    d = sql.Literal(dimensions)
    return sql.SQL(
        "SELECT content, doc_id, chunk_no, meta, "
        "1 - (embedding::halfvec({d}) <=> %s::halfvec({d})) AS score "
        "FROM {t} ORDER BY embedding::halfvec({d}) <=> %s::halfvec({d}) LIMIT %s"
    ).format(t=t, d=d)


def vector_literal(vec: Sequence[float]) -> str:
    """pgvector 가 읽는 `'[a,b,c]'` 문자열. `::vector`·`::halfvec` 캐스트와 함께 쓴다."""
    return "[" + ",".join(repr(float(v)) for v in vec) + "]"


@dataclass(frozen=True)
class Hit:
    """검색 행 하나다. meta 는 인덱서가 적은 `{name, source, url?}` 이다."""

    content: str
    doc_id: str
    chunk: int
    meta: dict[str, Any]
    score: float

    @property
    def title(self) -> str | None:
        return self.meta.get("name")


def kept_hits(rows: Sequence[Sequence[Any]], min_score: float | None) -> list[Hit]:
    """행 `(content, doc_id, chunk_no, meta, score)` 가운데 `min_score` 를 넘는 것이다."""
    hits: list[Hit] = []
    for content, doc_id, chunk_no, meta, score in rows:
        value = float(score)
        if min_score is not None and value < min_score:
            continue
        fields = meta if isinstance(meta, dict) else {}
        hits.append(Hit(content, str(doc_id), chunk_no, fields, round(value, 4)))
    return hits


def render_results(hits: Sequence[Hit], numbers: Sequence[int]) -> str:
    """모델이 보는 결과 JSON 이다. 결과마다 인용 번호 `cite` 를 앞에 둔다."""
    results = [
        {"cite": n, "content": h.content, "source": h.title, "chunk": h.chunk, "score": h.score}
        for h, n in zip(hits, numbers, strict=True)
    ]
    return json.dumps({"results": results}, ensure_ascii=False)


def fit_results(hits: Sequence[Hit], numbers: Sequence[int]) -> tuple[str, int]:
    """결과 JSON 이 12KB 안에 들 때까지 뒤 항목부터 뺀다. (결과 JSON, 남은 앞 항목 수)다."""
    kept = len(hits)
    text = render_results(hits, numbers[:kept])
    while kept and len(text.encode("utf-8")) > MAX_RESULT_BYTES:
        kept -= 1
        text = render_results(hits[:kept], numbers[:kept])
    return text, kept


def format_tool_error(e: ToolException) -> str:
    """`handle_tool_error` 다. 코드는 도구 코루틴이 정한 것이고 message 는 원인 예외의 클래스
    이름뿐이다(provider 원문에는 마스킹된 키가, psycopg 원문에는 사용자명이 든다)."""
    cause = e.__cause__
    message = type(cause).__name__ if cause is not None else "error"
    return json.dumps({"error": {"code": str(e), "message": message}})


class KnowledgeSearch:
    """항목 하나의 검색이다. 도구 코루틴은 `search` 를 감싼 함수다(langchain 이 코루틴의 타입
    힌트를 읽으므로 callable 인스턴스가 아니라 함수여야 한다)."""

    def __init__(self, item: dict[str, Any], pool: Any, embeddings: Embeddings) -> None:
        self.ref = str(item["ref"])
        self.kb = {"id": str(item["base"]["id"]), "name": str(item["base"]["name"])}
        self.top_k = int(item["tool"]["top_k"])
        self.min_score = item["tool"]["min_score"]
        self._pool = pool
        self._embeddings = embeddings
        self._query = search_query(item["store"]["table"], int(item["store"]["dimensions"]))
        embedding = item["embedding"]
        # daegam.embeddings 에 싣는 provider 와 경로다(P37 2절)
        provider = embedding["provider"]
        self._provider = {"name": provider["name"], "kind": provider["kind"]}
        self._route = ROUTE_GATEWAY if provider.get("gateway") else ROUTE_DIRECT
        self._model = embedding["model"]

    async def search(
        self, query: str, ledger: CitationLedger, trace_id: str | None = None
    ) -> tuple[str, dict[str, Any]]:
        """찾아 번호를 단 결과 JSON 과 출처 artifact 다. trace_id 는 이 턴의 실행이다."""
        return self.cited(await self._rows(query, trace_id), ledger)

    def cited(
        self, rows: Sequence[Sequence[Any]], ledger: CitationLedger
    ) -> tuple[str, dict[str, Any]]:
        """행에 인용 번호를 단다. 12KB 에 맞춰 줄인 뒤 남은 결과만 장부에 적어 번호가 비지
        않는다."""
        hits = kept_hits(rows, self.min_score)
        keys = [(KIND_KNOWLEDGE, self.kb["id"], h.doc_id, h.chunk) for h in hits]
        text, kept = fit_results(hits, ledger.preview(keys))
        numbers = ledger.assign(keys[:kept])
        sources = [self._source(h, n) for h, n in zip(hits[:kept], numbers, strict=True)]
        return text, artifact(sources)

    def _source(self, hit: Hit, n: int) -> dict[str, Any]:
        """artifact 의 출처 항목이다. 본문은 싣지 않는다(가림을 거치지 않는 자리다)."""
        return {
            "n": n,
            "kind": KIND_KNOWLEDGE,
            "title": hit.title,
            "url": hit.meta.get("url"),
            "kb": self.kb,
            "doc_id": hit.doc_id,
            "chunk": hit.chunk,
            "score": hit.score,
        }

    async def _rows(self, query: str, trace_id: str | None) -> list[Any]:
        vector = await self._embed(query, trace_id)
        literal = vector_literal(vector)
        try:
            async with self._pool.connection() as conn:
                cur = await conn.execute(self._query, (literal, literal, self.top_k))
                rows = await cur.fetchall()
        except psycopg.errors.UndefinedTable:
            # Knowledge Base 가 지워졌거나 아직 만들어지지 않았다. 도구는 빈 결과로 답한다
            rows = []
        except Exception as e:  # noqa: BLE001  psycopg.Error 를 포함해 전부 도구 오류로 접는다
            log.warning("knowledge[%s] 검색 실패: %s", self.ref, type(e).__name__)
            raise ToolException(ERROR_DB) from e
        return rows

    async def _embed(self, query: str, trace_id: str | None) -> list[float]:
        """질의 하나의 임베딩. 일시적인 실패면 한 번 더 부른다. 시도마다 호출 id 가 따로이고
        요청이 끝날 때마다 트레이서에 `daegam.embeddings` 로 알린다(P37 2절)."""

        async def attempt(n: int) -> list[float]:
            started = time.monotonic()
            with llm_http.call_scope(trace_id) as call:
                try:
                    vector = await self._embeddings.aembed_query(query)
                except Exception as e:
                    await self._signal(call, query, started, n, error=e)
                    raise
            await self._signal(call, query, started, n)
            return vector

        try:
            return await llm_http.retry_once(attempt, gateway=self._route == ROUTE_GATEWAY)
        except Exception as e:  # noqa: BLE001  openai.OpenAIError 를 포함해 전부 도구 오류로 접는다
            log.warning("knowledge[%s] 임베딩 실패: %s", self.ref, type(e).__name__)
            code = llm_http.gateway_error(e, gateway=self._route == ROUTE_GATEWAY)
            quota = llm_http.quota_exhausted(e)
            raise ToolException(code or (ERROR_QUOTA if quota else ERROR_EMBEDDING)) from e

    async def _signal(
        self,
        call: llm_http.CallId,
        query: str,
        started: float,
        attempt: int,
        *,
        error: BaseException | None = None,
    ) -> None:
        await signal(
            EMBEDDINGS,
            {
                "span_id": call.span_id,
                "provider": self._provider,
                "route": self._route,
                "model": self._model,
                "input_tokens": llm_http.estimate_tokens([query]),
                "latency_ms": round((time.monotonic() - started) * 1000, 1),
                "attempt": attempt,
                "error_type": None if error is None else error_kind(error),
            },
        )


def trace_of(runtime: Any) -> str | None:
    """이 턴의 실행 트레이스 id 다. 턴 문맥에 실려 온다. 문맥 없이 부른 자리(테스트)는 None 이다."""
    trace_id = context_field(runtime, "trace_id")
    return trace_id if isinstance(trace_id, str) else None


def build_search_tool(item: dict[str, Any], pool: Any, embeddings: Embeddings) -> BaseTool:
    """검색 도구다. ToolNode 가 `runtime` 에 턴 문맥을 넣어 주고 도구는 거기서 인용 장부를 꺼낸다.
    모델이 보는 인자는 `query` 하나다."""
    tool = item["tool"]
    searcher = KnowledgeSearch(item, pool, embeddings)

    async def search(query: str, runtime: ToolRuntime) -> tuple[str, dict[str, Any]]:
        return await searcher.search(query, ledger_of(runtime), trace_of(runtime))

    return StructuredTool.from_function(
        coroutine=search,
        name=tool["name"],
        description=str(tool["description"]) + DESCRIPTION_SUFFIX,
        args_schema=SearchArgs,
        handle_tool_error=format_tool_error,
        response_format="content_and_artifact",
    )


async def open_pool(dsn: str) -> AsyncConnectionPool:
    """DSN 하나의 풀이다. autocommit 이고 검색 문장의 statement_timeout 은 10초다."""
    pool = AsyncConnectionPool(
        dsn,
        open=False,
        min_size=POOL_MIN_SIZE,
        max_size=POOL_MAX_SIZE,
        kwargs={"autocommit": True, "options": f"-c statement_timeout={STATEMENT_TIMEOUT_MS}"},
    )
    await pool.open()
    return pool


class KnowledgeTools:
    """knowledge[] 전부의 검색 도구와 그 풀이다. `open` 으로 만들고 종료 때 `close` 한다."""

    def __init__(self, tools_by_ref: dict[str, BaseTool], pools: list[Any]) -> None:
        self._tools = tools_by_ref
        self._pools = pools

    @classmethod
    async def open(cls, config: AgentConfig) -> KnowledgeTools:
        """항목마다 DSN 을 풀고 풀을 연 뒤(같은 DSN 은 공유) SELECT 1 로 연결되는지 보고 임베딩
        클라이언트와 도구를 만든다. 어느 단계든 실패하면 연 풀을 다 닫고 ConfigError 를 낸다."""
        pools: dict[str, Any] = {}
        tools: dict[str, BaseTool] = {}
        try:
            for item in config.knowledge:
                dsn = _dsn_for(item)
                pool = pools.get(dsn)
                if pool is None:
                    pool = await _open_checked(item, dsn)
                    pools[dsn] = pool
                tools[item["ref"]] = build_search_tool(item, pool, build_embeddings(item))
        except BaseException:
            for pool in pools.values():
                await pool.close()
            raise
        return cls(tools, list(pools.values()))

    def tools_for(self, refs: list[str]) -> list[BaseTool]:
        """ref 순서대로의 검색 도구다. 모르는 ref 는 check_references 가 이미 막았다."""
        return [self._tools[ref] for ref in refs]

    async def close(self) -> None:
        for pool in self._pools:
            await pool.close()
        self._pools = []


def _dsn_for(item: dict[str, Any]) -> str:
    dsn_env = item["store"]["dsn_env"]
    dsn = resolve_dsn(dsn_env)
    if not dsn:
        raise missing_dsn_error(
            "knowledge_dsn_missing", f"knowledge[{item['ref']}].store.dsn_env", dsn_env
        )
    return dsn


async def _open_checked(item: dict[str, Any], dsn: str) -> Any:
    """DSN 하나의 풀을 열고 연결되는지 본다. 아직 연결할 수 없으면 knowledge_db_unreachable 이라
    기동이 memory 와 같이 기다리고(T22), 접속이 거부됐으면 knowledge_db_rejected 다(T23)."""
    subject = f"knowledge[{item['ref']}] 의 Database({item['store']['dsn_env']})"
    pool = await open_pool(dsn)
    try:
        await check_postgres(pool, subject, KNOWLEDGE_DB_CODES)
    except BaseException:
        # 다시 열기 도중 앱이 닫혀 취소될 때도 닫는다(T22)
        await pool.close()
        raise
    return pool
