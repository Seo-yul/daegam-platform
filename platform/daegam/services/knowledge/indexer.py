"""Knowledge 인덱서 (D32, P15 F2). 플랫폼 안의 리스 워커다.

Spec Generator 워커와 같은 실행 모델이고 수명·하트비트는 `services/lease_worker.py` 의
`LeaseWorker` 에 함께 있다. 앱 시작 시와 주기(기본 5초)마다 `store.acquire()` 로
`pending`·`indexing`(리스 만료)·`deleting` 문서 하나를 잡고, 잡은 동안 하트비트로 리스를 갱신하며
run 을 돈다. run 은 문서 state 로 갈린다. `indexing` 은 본문, 텍스트, 청크, provider, 임베딩,
Database, 행 교체를 거쳐 `finish_indexed` 이고, `deleting` 은 행과 본문을 지운 뒤
`finish_deleted` 다. 어느 단계가 안 되면 `finish_failed(code, detail)` 로 남고 자동 재시도는
없다(화면의 "다시 인덱싱"·"다시 삭제"). 리스가 남의 것이 되면 `LeaseLost` 로 조용히 물러난다.

텍스트 단계는 이벤트 루프에서 파서를 돌리지 않는다. 문서 파싱 서비스 `daegam-docparse` 를
`purpose: "index"` 로 부르고(D61, `services/docparse.py`) 텍스트 형식만 바로 푼다. 리스 워커라
플랫폼 레플리카마다 한 번에 문서 하나만 뽑는다. 서비스가 바쁘거나(429) 연결할 수 없으면 실패로 두지
않고 문서 리스를 쥔 채 5·15·45초 물러났다 다시 부르며, 그래도 안 되면 `extract_unavailable` 로
실패해 "다시 인덱싱" 을 기다린다.

detail 은 우리가 만든 코드·예외 클래스 이름·HTTP 상태뿐이다. provider 원문에는 마스킹된 키가,
libpq 원문에는 사용자명이 든다. 테이블은 만들지 않는다(POST 가 만들었다). 없으면 `table_missing`
이다.
"""

from __future__ import annotations

import asyncio
import codecs
import hashlib
import logging
from collections.abc import Awaitable, Callable
from typing import Any

import psycopg
import psycopg.errors
from langchain_core.embeddings import Embeddings

from daegam import llm_http
from daegam.config import Settings
from daegam.documents.formats import CONTENT_TYPES, MAX_DOCUMENT_BYTES, format_of
from daegam.documents.result import INDEX
from daegam.k8s.client import ClusterUnreachable
from daegam.services.clusters import ClusterService
from daegam.services.data_services import data_service_password
from daegam.services.docparse import DocparseClient, DocparseError
from daegam.services.knowledge import pg
from daegam.services.knowledge.embeddings import (
    BATCH_SIZE,
    EmbeddingError,
    build_embeddings,
    classify_embedding_error,
)
from daegam.services.knowledge.text import chunk, count_tokens
from daegam.services.lease_worker import LeaseWorker, default_owner
from daegam.services.llm_endpoint import Endpoint, LlmEndpoints
from daegam.services.llm_providers import ProviderError, provider_named
from daegam.services.tenancy import TenancyError, TenancyService
from daegam.services.usage.ledger import CallSite, UsageLedger, metered
from daegam.store.data_services import MongoDataServiceStore
from daegam.store.knowledge import (
    KnowledgeBase,
    KnowledgeDocument,
    KnowledgeFiles,
    LeaseLost,
    MongoKnowledgeStore,
)
from daegam.store.settings import LLMProvider, MongoPlatformSettingsStore
from daegam.store.usage import UsageSource
from daegam.urlfetch import (
    BlockedURL,
    FetchError,
    Resolver,
    fetch_url,
    public_detail,
    system_resolver,
)

logger = logging.getLogger("daegam.knowledge")

# 문서 하나의 청크 상한. Knowledge Base 합계 상한은 kb.limits.max_chunks 다
MAX_CHUNKS_PER_DOCUMENT = 5000
# URL 문서의 본문 상한. 업로드의 413 상한과 같은 값이다
MAX_FETCH_BYTES = MAX_DOCUMENT_BYTES
# 인덱서의 문장 타임아웃 (초). verify 5초, 생성의 DDL 30초와 다르다
STATEMENT_TIMEOUT = 120.0
# 본문 앞부분으로 형식을 짐작할 때 보는 길이 (URL 문서는 fetch 가 content type 을 주지 않는다)
SNIFF_BYTES = 512
# 문서 파싱 서비스가 바쁘거나 연결할 수 없을 때 다시 부르기 전에 물러나는 초
EXTRACT_BACKOFF = (5.0, 15.0, 45.0)

EmbeddingsFactory = Callable[..., Embeddings]
_tiktoken_warned = False


class RunFailed(Exception):
    """run 의 한 단계가 실패했다. 워커가 `finish_failed(code, detail)` 로 접는다."""

    def __init__(self, code: str, detail: str | None = None):
        super().__init__(f"{code}: {detail}" if detail else code)
        self.code = code
        self.detail = detail


def count_batch(kind: str, texts: list[str]) -> tuple[int, bool]:
    """(토큰, 추정인가). openai 는 tiktoken cl100k_base 로 세고 stub 은 0 이다. tiktoken 이 BPE
    파일을 못 받아 예외가 나면(플랫폼 파드에 egress 가 없을 수 있다) 글자 수 규칙
    (`llm_http.estimate_tokens`)으로 떨어지고 추정이라 표시한다(경고 한 줄)."""
    global _tiktoken_warned
    if kind != "openai":
        return 0, False
    try:
        return count_tokens(texts), False
    except Exception as e:  # noqa: BLE001 - 네트워크·파일 예외의 종류가 많다
        if not _tiktoken_warned:
            _tiktoken_warned = True
            logger.warning(
                "tiktoken 인코딩을 읽지 못해 토큰을 글자 수/%d 로 추정한다 (%s)",
                llm_http.CHARS_PER_TOKEN,
                type(e).__name__,
            )
        return llm_http.estimate_tokens(texts), True


def sniff_content_type(name: str | None, data: bytes) -> str | None:
    """URL 문서의 content type. 이름의 확장자가 먼저이고, 없으면 본문 앞부분(PDF 매직·HTML 태그·JSON
    괄호)으로 짐작한다. 그 밖은 UTF-8 로 읽히고 NUL 이 없는 본문만 text/plain 이고, 아니면 None
    (업로드의 `unsupported_format` 과 같은 규칙. zip·이미지를 텍스트로 풀어 임베딩하지 않는다)."""
    fmt = format_of(None, name)
    if fmt is not None:
        return CONTENT_TYPES[fmt]
    head = data[:SNIFF_BYTES]
    if head.startswith(b"%PDF"):
        return CONTENT_TYPES["pdf"]
    lowered = head.lstrip().lower()
    if lowered.startswith((b"<!doctype html", b"<html")) or b"<html" in lowered:
        return CONTENT_TYPES["html"]
    if lowered.startswith((b"{", b"[")):
        return CONTENT_TYPES["json"]
    if b"\x00" in head:
        return None
    # 앞부분이 멀티바이트 글자 한가운데서 잘릴 수 있다. 증분 디코더는 final=False 로 끝의 불완전한
    # 글자를 남겨 두고 나머지가 UTF-8 인지 본다. 고정 길이로 자르면 경계가 어긋난다
    try:
        codecs.getincrementaldecoder("utf-8")().decode(head, final=False)
    except UnicodeDecodeError:
        return None
    return CONTENT_TYPES["text"]


class KnowledgeIndexer(LeaseWorker[KnowledgeDocument]):
    task_prefix = "knowledge-indexer"
    tick_label = "knowledge indexer tick"
    item_label = "knowledge doc"
    log = logger

    def __init__(
        self,
        store: MongoKnowledgeStore,
        files: KnowledgeFiles,
        tenancy: TenancyService,
        cluster_service: ClusterService,
        platform_settings: MongoPlatformSettingsStore,
        data_services: MongoDataServiceStore,
        settings: Settings,
        *,
        docparse: DocparseClient,
        owner: str | None = None,
        interval: float = 5.0,
        lease_seconds: float = 90.0,
        usage_ledger: UsageLedger | None = None,
        endpoints: LlmEndpoints | None = None,
    ):
        super().__init__(owner=owner, interval=interval, lease_seconds=lease_seconds)
        # 배치마다 원장에 남긴다 (P37 7절). 없으면(테스트) 남기지 않는다
        self.usage_ledger = usage_ledger
        self.store = store
        self.files = files
        self.tenancy = tenancy
        self.cluster_service = cluster_service
        self.platform_settings = platform_settings
        self.data_services = data_services
        self.settings = settings
        # 테스트가 가짜를 넣는 자리. DNS·바깥 HTTP·임베딩 클라이언트, 문서 파싱 서비스와 그것을 다시
        # 부르기 전의 대기. 문서 파싱 클라이언트는 첨부 업로드와 같은 것을 받는다(wiring.py)
        self.resolver: Resolver = system_resolver
        self.fetch_transport: Any = None
        self.embeddings_factory: EmbeddingsFactory = build_embeddings
        self.docparse = docparse
        # 문서마다 임베딩의 길을 고른다 (P38 6절). 없으면(테스트) 늘 직접 경로다
        self.endpoints = endpoints or LlmEndpoints(settings, cluster_service.secret_store)
        self.sleep: Callable[[float], Awaitable[None]] = asyncio.sleep

    # -- LeaseWorker ----------------------------------------------------------------

    async def acquire(self) -> KnowledgeDocument | None:
        return await self.store.acquire(self.owner, self.lease_seconds)

    async def process(self, item: KnowledgeDocument) -> None:
        await self.run(item)

    async def renew(self, item_id: str) -> bool:
        return await self.store.renew(item_id, self.owner, self.lease_seconds)

    # -- 문서 하나 -----------------------------------------------------------------

    async def run(self, doc: KnowledgeDocument) -> None:
        """문서 하나. state 가 `deleting` 이면 삭제 run, 아니면(`indexing`) 인덱싱 run 이다."""
        async with self.heartbeating(doc.id):
            try:
                if doc.state == "deleting":
                    await self._run_delete(doc)
                else:
                    await self._run_index(doc)
                return
            except LeaseLost:
                logger.warning("knowledge doc %s: lease lost, another worker took over", doc.id)
                return
            except RunFailed as e:
                code, detail = e.code, e.detail
            except asyncio.CancelledError:
                raise
            except Exception as e:  # noqa: BLE001 - 예상 밖 실패도 문서에 남긴다
                logger.exception("knowledge doc %s: run failed", doc.id)
                code, detail = "runner_error", type(e).__name__
            try:
                await self.store.finish_failed(doc.id, self.owner, code, detail)
            except LeaseLost:
                logger.warning("knowledge doc %s: lease lost before finish", doc.id)

    # -- 인덱싱 run ----------------------------------------------------------------

    async def _kb(self, doc: KnowledgeDocument) -> KnowledgeBase:
        kb = await self.store.get(doc.knowledge_base_id)
        if kb is None:
            raise RunFailed("knowledge_base_missing", doc.knowledge_base_id)
        return kb

    async def _body(self, kb: KnowledgeBase, doc: KnowledgeDocument) -> tuple[bytes, str]:
        """1 단계. 본문이 GridFS 에 있으면 그것, URL 문서인데 없으면 받아서 둔다. 업로드 문서의
        본문이 없으면 `body_missing` 이다."""
        data = await self.files.get(kb.id, doc.id)
        if data is not None:
            return data, doc.content_type
        if doc.source.kind != "url" or not doc.source.url:
            raise RunFailed("body_missing", "document body is not in storage")
        try:
            data = await fetch_url(
                doc.source.url,
                max_bytes=MAX_FETCH_BYTES,
                resolver=self.resolver,
                transport=self.fetch_transport,
            )
        except (BlockedURL, FetchError) as e:
            # detail 은 우리가 쓴 문장뿐이다. urlfetch 의 detail 에는 남의 서버가 정한 문자열
            # (리다이렉트 Location 등)이 들어 문서·API·비서 모델 문맥으로 흐른다
            raise RunFailed(e.code, public_detail(e)) from e
        content_type = sniff_content_type(doc.name, data)
        if content_type is None:
            raise RunFailed(
                "unsupported_format", "the name and the body do not tell a supported format"
            )
        # 리스 검사(set_body)가 먼저다. Knowledge Base 삭제가 리스를 걷은 뒤 본문을 올리면 고아
        # 파일이 남는다. 여기서 LeaseLost 면 본문을 올리지 않는다
        await self.store.set_body(
            doc.id,
            self.owner,
            sha256=hashlib.sha256(data).hexdigest(),
            size=len(data),
            content_type=content_type,
        )
        await self.files.put(kb.id, doc.id, data, content_type)
        return data, content_type

    async def _extract(self, fmt: str, data: bytes) -> str:
        """문서 파싱 서비스가 바쁘거나 연결할 수 없으면 리스를 쥔 채 물러났다 다시 부른다."""
        delays = iter(EXTRACT_BACKOFF)
        while True:
            try:
                return (await self.docparse.extract(fmt, data, purpose=INDEX)).text
            except DocparseError as e:
                if not e.retryable:
                    raise RunFailed(e.code, e.detail) from e
                delay = next(delays, None)
                if delay is None:
                    raise RunFailed("extract_unavailable", e.detail) from e
                await self.sleep(delay)

    async def _text(self, name: str, content_type: str, data: bytes) -> str:
        """2 단계."""
        fmt = format_of(content_type, name)
        if fmt is None:
            raise RunFailed("unsupported_format", content_type)
        text = await self._extract(fmt, data)
        if not text.strip():
            raise RunFailed("empty_document", "no text could be extracted")
        return text

    async def _chunks(self, kb: KnowledgeBase, doc: KnowledgeDocument, text: str) -> list[str]:
        """3 단계. 문서 상한과 Knowledge Base 합계 상한(이 문서의 이전 chunks 는 뺀다)."""
        pieces = chunk(text, kb.chunking.size, kb.chunking.overlap)
        if len(pieces) > MAX_CHUNKS_PER_DOCUMENT:
            raise RunFailed(
                "too_many_chunks",
                f"{len(pieces)} chunks exceed the per-document limit {MAX_CHUNKS_PER_DOCUMENT}",
            )
        status = await self.store.status(kb.id)
        total = status.chunks - doc.chunks + len(pieces)
        if total > kb.limits.max_chunks:
            raise RunFailed(
                "too_many_chunks",
                f"knowledge base would have {total} chunks (limit {kb.limits.max_chunks})",
            )
        return pieces

    async def _embeddings(self, kb: KnowledgeBase) -> tuple[LLMProvider, Endpoint, Embeddings]:
        """4 단계. provider 해소, 길(P38 6절)과 클라이언트. (provider, 길, client)."""
        ps = await self.platform_settings.get()
        provider = provider_named(ps.llm_providers, kb.embedding.provider)
        if provider is None:
            raise RunFailed("unknown_provider", kb.embedding.provider)
        try:
            # 문서마다 한 번 고른다. 직접 경로의 Secret 읽기는 스레드풀에서 돈다
            endpoint = await self.endpoints.resolve_endpoint(
                provider, tenant=kb.tenant, purpose="knowledge_indexer"
            )
        except ProviderError as e:
            raise RunFailed("provider_no_key", e.code) from e
        try:
            client = self.embeddings_factory(
                provider.kind,
                api_key=endpoint.api_key or None,
                model=kb.embedding.model,
                dimensions=kb.embedding.dimensions,
                base_url=endpoint.base_url,
                bearer=endpoint.bearer,
                gateway=endpoint.gateway,
            )
        except EmbeddingError as e:
            raise RunFailed(e.code, e.detail) from e
        return provider, endpoint, client

    async def _embed(
        self,
        kb: KnowledgeBase,
        site: CallSite,
        client: Embeddings,
        pieces: list[str],
        *,
        gateway: bool,
    ) -> tuple[list[list[float]], int]:
        """5 단계. (벡터, 토큰 수). 64 개씩이고 배치마다 원장 행이 하나다. 첫 배치의 길이가 차원과
        다르면 `embedding_dimension_mismatch`. gateway 는 게이트웨이 경로인가다."""
        vectors: list[list[float]] = []
        tokens = 0
        for start in range(0, len(pieces), BATCH_SIZE):
            batch = pieces[start : start + BATCH_SIZE]
            out, counted = await self._embed_batch(site, client, batch, gateway=gateway)
            if start == 0 and out and len(out[0]) != kb.embedding.dimensions:
                raise RunFailed(
                    "embedding_dimension_mismatch",
                    f"provider returned {len(out[0])} dimensions, "
                    f"knowledge base expects {kb.embedding.dimensions}",
                )
            vectors.extend(out)
            tokens += counted
        return vectors, tokens

    async def _embed_batch(
        self, site: CallSite, client: Embeddings, batch: list[str], *, gateway: bool
    ) -> tuple[list[list[float]], int]:
        """배치 하나의 (벡터, 토큰 수). 일시적인 실패면 한 번 더 부르고, 시도마다 호출 id 가
        따로다 (P37 2절). 게이트웨이의 한도·토큰 거절은 다시 부르지 않는다 (P38 7절)."""

        async def attempt(_n: int) -> tuple[list[list[float]], int]:
            async with metered(self.usage_ledger, site) as meter:
                out = await client.aembed_documents(batch)
                tokens, estimated = count_batch(site.provider.kind, batch)
                meter.report({"input_tokens": tokens}, estimated=estimated)
            return out, tokens

        try:
            return await llm_http.retry_once(attempt, sleep=self.sleep, gateway=gateway)
        except Exception as e:  # noqa: BLE001 - provider 예외의 종류가 많다. 코드로 접는다
            err = classify_embedding_error(e, gateway=gateway)
            raise RunFailed(err.code, err.detail) from e

    def _site(
        self, kb: KnowledgeBase, doc: KnowledgeDocument, provider: LLMProvider, endpoint: Endpoint
    ) -> CallSite:
        """인덱싱 한 번이 trace 하나다. 부른 쪽은 Knowledge Base, 일은 문서다 (P37 7절)."""
        return CallSite(
            tenant=kb.tenant,
            source=UsageSource(kind="knowledge_indexer", id=kb.id, name=kb.name),
            provider=provider,
            model=kb.embedding.model,
            operation="embeddings",
            ref=doc.id,
            base_url=endpoint.base_url,
        )

    async def _connect(self, kb: KnowledgeBase) -> psycopg.AsyncConnection:
        """6 단계의 앞. Database 비밀번호와 접속. 실패 코드는 `classify_connect_error` 그대로이고
        `connect_failed` 만 `db_unreachable` 이다."""
        ds = await self.data_services.get(kb.data_service_id)
        if ds is None:
            raise RunFailed("unknown_data_service", kb.data_service_id)
        try:
            password = await data_service_password(ds, self.tenancy, self.cluster_service)
        except TenancyError as e:
            raise RunFailed(e.code, e.detail) from e
        except ClusterUnreachable as e:
            raise RunFailed("cluster_unreachable", type(e).__name__) from e
        try:
            return await pg.connect(ds, password, statement_timeout_seconds=STATEMENT_TIMEOUT)
        except (psycopg.Error, OSError) as e:
            code, detail = pg.classify_connect_error(e)
            raise RunFailed(pg.response_code(code), f"{ds.host}:{ds.port}: {detail}") from e

    async def _run_index(self, doc: KnowledgeDocument) -> None:
        kb = await self._kb(doc)
        data, content_type = await self._body(kb, doc)
        text = await self._text(doc.name, content_type, data)
        pieces = await self._chunks(kb, doc, text)
        provider, endpoint, client = await self._embeddings(kb)
        site = self._site(kb, doc, provider, endpoint)
        vectors, tokens = await self._embed(kb, site, client, pieces, gateway=endpoint.gateway)
        meta: dict[str, Any] = {"name": doc.name, "source": doc.source.kind}
        if doc.source.url:
            meta["url"] = doc.source.url
        rows = [
            (n, content, vec, meta)
            for n, (content, vec) in enumerate(zip(pieces, vectors, strict=True))
        ]
        conn = await self._connect(kb)
        async with conn:
            try:
                if not await pg.table_exists(conn, kb.table):
                    raise RunFailed(
                        "table_missing", f"table {kb.table} does not exist; it was dropped"
                    )
                await pg.replace_chunks(conn, kb.table, doc.id, rows)
            except psycopg.errors.UndefinedTable as e:
                raise RunFailed("table_missing", f"table {kb.table} does not exist") from e
            except (psycopg.Error, OSError) as e:
                raise RunFailed("query_failed", type(e).__name__) from e
        await self.store.finish_indexed(doc.id, self.owner, chunks=len(rows), tokens=tokens)

    # -- 삭제 run ------------------------------------------------------------------

    async def _run_delete(self, doc: KnowledgeDocument) -> None:
        """행 삭제(테이블 없음은 정상), 본문 삭제(NoFile 정상), `finish_deleted` 순이다."""
        kb = await self.store.get(doc.knowledge_base_id)
        if kb is not None:
            conn = await self._connect(kb)
            async with conn:
                try:
                    await pg.delete_doc_rows(conn, kb.table, doc.id)
                except psycopg.errors.UndefinedTable:
                    pass
                except (psycopg.Error, OSError) as e:
                    raise RunFailed("query_failed", type(e).__name__) from e
        await self.files.delete_doc(doc.knowledge_base_id, doc.id)
        await self.store.finish_deleted(doc.id, self.owner)


__all__ = [
    "EXTRACT_BACKOFF",
    "MAX_CHUNKS_PER_DOCUMENT",
    "MAX_FETCH_BYTES",
    "STATEMENT_TIMEOUT",
    "KnowledgeIndexer",
    "RunFailed",
    "default_owner",
    "sniff_content_type",
]
