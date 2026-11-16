"""Knowledge Base 서비스 (D32, P15 F2). 생성·수정·삭제 규칙과 문서 추가·다시 인덱싱·집계.

생성은 입력 검사, provider 키, 모델·차원, Database kind, 비밀번호, 접속,
`capabilities.pgvector` 갱신, `CREATE EXTENSION`, `CREATE TABLE`, Mongo 순이고 어느 단계든
실패하면 `KnowledgeError`(422) 이며 리소스는 남지 않는다. 테이블은 이 자리에서만 만든다. 인덱서가
만들면 두 워커의 `CREATE` 가 경합하기 때문이다. 삭제는 참조, `indexing`, `deleting_at`,
`purging`, `DROP TABLE`, 문서·본문, 리소스 순이고 `DROP` 이 안 되면 `orphaned_table` 로 남긴다.

문서 쓰기(업로드·URL·reindex·chunking PATCH)는 `deleting_at` 이 있으면 409 `deleting` 이고,
`indexing` 문서가 있으면 reindex·삭제·chunking 변경은 409 `indexing` 이다. 워커가 리스를 쥔 채 행을
쓰는 도중 규칙이 바뀌거나 테이블이 사라지는 일을 막는다.

접속 오류 코드는 `classify_connect_error` 그대로이고 `connect_failed` 만 응답에서 `db_unreachable`
로 낸다. 화면 문구가 하나이기 때문이다. detail 은 우리가 만든 한 줄이라 libpq 원문(사용자명)이
없다.
"""

from __future__ import annotations

import hashlib
from typing import Any
from urllib.parse import urlsplit

import psycopg
from pymongo.errors import DuplicateKeyError
from starlette.concurrency import run_in_threadpool

from daegam.config import Settings
from daegam.documents.formats import (
    CONTENT_TYPES,
    MAX_DOCUMENT_BYTES,
    UNKNOWN_CONTENT_TYPE,
    FormatRefused,
    accepted_format,
    format_of,
)
from daegam.error_codes import status_of
from daegam.errors import ServiceError, save_missed
from daegam.services.clusters import ClusterService
from daegam.services.data_services import pgvector_capability, resolve_password
from daegam.services.knowledge import pg
from daegam.services.llm_providers import ProviderError, provider_named, resolve_api_key
from daegam.services.references import KNOWLEDGE_BASE_DELETE, ReferenceService
from daegam.services.tenancy import TenancyService
from daegam.store.base import read_after_miss, utcnow
from daegam.store.data_services import DataService, MongoDataServiceStore
from daegam.store.knowledge import (
    Chunking,
    Embedding,
    KnowledgeBase,
    KnowledgeDocument,
    KnowledgeFiles,
    Limits,
    MongoKnowledgeStore,
    Source,
)
from daegam.store.settings import LLMProvider, MongoPlatformSettingsStore, known_dimensions
from daegam.urlfetch import BlockedURL, Resolver, check_url_async, system_resolver

# Knowledge Base 하나의 문서 수 상한 (422 too_many_documents)
MAX_DOCUMENTS = 200
# 생성의 DDL 문장 타임아웃 (초). verify 5초, 인덱서 120초와 다르다
DDL_STATEMENT_TIMEOUT = 30.0
PGVECTOR_HINT = "확장 vector 를 만들 수 없다. trusted 확장이 아니라 슈퍼유저가 미리 만들어야 한다"


class KnowledgeError(ServiceError):
    """Knowledge Base 규칙 위반. 앱의 전역 핸들러가 응답으로 낸다."""


def _busy(doc: KnowledgeDocument) -> KnowledgeError:
    """워커 손에 있는 문서의 409. `indexing` 이면 indexing, 그 밖(`deleting`·`purging`)은
    deleting 이다. 어느 상태를 막을지는 부르는 쪽이 정한다."""
    if doc.state == "indexing":
        return KnowledgeError(409, "indexing", f"document {doc.name!r} is being indexed")
    return KnowledgeError(409, "deleting", f"document {doc.name!r} is being deleted")


def name_from_url(url: str) -> str:
    """URL 마지막 경로 조각. 비어 있으면 host."""
    parts = urlsplit(url)
    last = parts.path.rstrip("/").rsplit("/", 1)[-1]
    return last or parts.hostname or "document"


def content_type_for(name: str | None) -> str:
    """이름의 확장자로 정한 content type. 모르면 octet-stream 이고 인덱서가 본문을 받은 뒤 다시
    정한다."""
    fmt = format_of(None, name)
    return CONTENT_TYPES[fmt] if fmt else UNKNOWN_CONTENT_TYPE


class KnowledgeService:
    def __init__(
        self,
        store: MongoKnowledgeStore,
        files: KnowledgeFiles,
        data_services: MongoDataServiceStore,
        platform_settings: MongoPlatformSettingsStore,
        tenancy: TenancyService,
        cluster_service: ClusterService,
        settings: Settings,
        references: ReferenceService,
    ):
        self.store = store
        self.files = files
        self.data_services = data_services
        self.platform_settings = platform_settings
        self.tenancy = tenancy
        self.cluster_service = cluster_service
        self.settings = settings
        # 삭제 가드가 Agent 의 현재 리비전과 배포가 핀한 리비전의 참조를 센다 (P75 2절)
        self.references = references
        # URL 검사의 DNS. 테스트가 가짜를 넣는 자리다
        self.resolver: Resolver = system_resolver

    # -- 생성 -----------------------------------------------------------------------

    async def _provider(self, name: str) -> LLMProvider:
        ps = await self.platform_settings.get()
        provider = provider_named(ps.llm_providers, name)
        if provider is None:
            raise KnowledgeError(422, "unknown_provider", f"provider {name!r} is not registered")
        if not provider.supports_embedding:
            raise KnowledgeError(
                422,
                "embedding_not_supported",
                f"provider {name!r} ({provider.kind}) has no embedding API",
            )
        if not provider.is_stub:
            try:
                # K8s Secret 조회는 동기라 스레드풀에서 (이벤트 루프를 막지 않는다)
                await run_in_threadpool(
                    resolve_api_key,
                    provider,
                    self.cluster_service.secret_store(),
                    namespace=self.settings.namespace,
                )
            except ProviderError as e:
                raise KnowledgeError(422, "provider_no_key", f"provider {name!r}: {e.code}") from e
        return provider

    @staticmethod
    def _embedding(provider: LLMProvider, model: str | None, dimensions: int | None) -> Embedding:
        effective = (model or "").strip() or provider.effective_embedding_model
        assert effective is not None  # supports_embedding 을 지났다
        dims = dimensions if dimensions is not None else known_dimensions(effective)
        if dims is None:
            raise KnowledgeError(
                422,
                "dimensions_required",
                f"embedding model {effective!r} is not in the known table; "
                "set embedding.dimensions",
            )
        return Embedding(provider=provider.name, model=effective, dimensions=dims)

    async def _postgres(self, tenant: str, ds_id: str) -> DataService:
        ds = await self.data_services.get(ds_id, tenant=tenant)
        if ds is None:
            raise KnowledgeError(422, "unknown_data_service", f"data service {ds_id} not found")
        if ds.kind != "postgres":
            raise KnowledgeError(
                422,
                "invalid_data_service_kind",
                f"data service {ds.name!r} is {ds.kind}; a knowledge base needs postgres",
            )
        return ds

    async def _password(self, ds: DataService) -> str | None:
        return await resolve_password(ds, self.tenancy, self.cluster_service, err=KnowledgeError)

    @staticmethod
    async def _connect(ds: DataService, password: str | None) -> psycopg.AsyncConnection:
        """생성·삭제의 DDL 접속. 접속 오류는 KnowledgeError(422) 다."""
        return await pg.connect_or_raise(
            ds, password, statement_timeout_seconds=DDL_STATEMENT_TIMEOUT, err=KnowledgeError
        )

    async def create(
        self,
        tenant: str,
        *,
        name: str,
        display_name: str,
        data_service_id: str,
        embedding: dict[str, Any],
        chunking: Chunking | None = None,
        limits: Limits | None = None,
        user_sub: str | None = None,
    ) -> KnowledgeBase:
        """스펙 "생성과 삭제의 순서" 그대로. embedding 은 `{provider, model?, dimensions?}` 다."""
        if await self.store.get_by_name(tenant, name) is not None:
            raise KnowledgeError(409, "name_conflict", f"knowledge base {name!r} already exists")
        provider = await self._provider(str(embedding.get("provider") or ""))
        emb = self._embedding(provider, embedding.get("model"), embedding.get("dimensions"))
        ds = await self._postgres(tenant, data_service_id)
        password = await self._password(ds)
        kb = KnowledgeBase(
            tenant=tenant,
            name=name,
            display_name=display_name,
            data_service_id=ds.id,
            embedding=emb,
            chunking=chunking or Chunking(),
            limits=limits or Limits(),
            updated_by=user_sub,
        )
        target = f"{ds.host}:{ds.port}"
        conn = await self._connect(ds, password)
        async with conn:
            try:
                extensions = await pg.extensions(conn)
            except (psycopg.Error, OSError) as e:
                raise KnowledgeError(422, "query_failed", f"{target}: {type(e).__name__}") from e
            capability = pgvector_capability(extensions)
            if capability is not None and ds.capabilities.get("pgvector") != capability:
                ds.capabilities["pgvector"] = capability
                # 캐시 한 칸만, 읽은 seq 일 때만 쓴다. 그사이 Database 가 바뀌었으면 쓰지 않고
                # 만들기는 그대로 간다 (P68 2절)
                await self.data_services.store_result(ds, {"capabilities.pgvector": capability})
            try:
                await pg.ensure_extension(conn)
            except (psycopg.Error, OSError) as e:
                raise KnowledgeError(
                    422, "pgvector_missing", f"{target}: {PGVECTOR_HINT} ({type(e).__name__})"
                ) from e
            try:
                await pg.create_table(conn, kb.table, emb.dimensions)
            except (psycopg.Error, OSError) as e:
                raise KnowledgeError(
                    422, "query_failed", f"{target}: CREATE TABLE failed ({type(e).__name__})"
                ) from e
        try:
            await self.store.create(kb)
        except DuplicateKeyError as e:
            raise KnowledgeError(
                409, "name_conflict", f"knowledge base {name!r} already exists"
            ) from e
        return kb

    # -- 수정 -----------------------------------------------------------------------

    async def _edit_missed(self, kb: KnowledgeBase) -> Exception:
        """사람의 저장이 맞지 않았을 때의 답 (P68 2절). 다시 읽어 삭제가 시작됐으면 409 deleting,
        그 밖은 그사이 다른 저장이 지나간 것이라 409 revision_conflict 다."""
        fresh, deleting = await read_after_miss(
            lambda: self.store.get(kb.id), lambda fresh: fresh.deleting_at is not None
        )
        return save_missed(
            KnowledgeError, "knowledge base", kb.name, found=fresh is not None, deleting=deleting
        )

    def _check_writable(self, kb: KnowledgeBase) -> None:
        if kb.deleting_at is not None:
            raise KnowledgeError(
                409, "deleting", f"knowledge base {kb.name!r} is being deleted ({kb.state})"
            )

    async def _check_not_indexing(self, kb: KnowledgeBase) -> None:
        n = await self.store.count_documents(kb.id, states=["indexing"])
        if n:
            raise KnowledgeError(
                409, "indexing", f"{n} document(s) are being indexed; try again later"
            )

    async def patch(
        self,
        kb: KnowledgeBase,
        *,
        display_name: str | None = None,
        chunking: Chunking | None = None,
        limits: Limits | None = None,
        user_sub: str | None = None,
    ) -> KnowledgeBase:
        """chunking 이 바뀌면 `indexing` 문서가 있을 때 409 `indexing`, 없으면 모든 문서를
        `pending` 으로 되돌린다."""
        self._check_writable(kb)
        rechunk = chunking is not None and chunking != kb.chunking
        if rechunk:
            await self._check_not_indexing(kb)
        names = {"updated_at", "updated_by"}
        if display_name is not None:
            kb.display_name = display_name
            names.add("display_name")
        if chunking is not None:
            kb.chunking = chunking
            names.add("chunking")
        if limits is not None:
            kb.limits = limits
            names.add("limits")
        kb.updated_at = utcnow()
        kb.updated_by = user_sub
        if not await self.store.save_edit(kb, names):
            raise await self._edit_missed(kb)
        if rechunk:
            await self.store.mark_all_pending(kb.id)
        return kb

    # -- 문서 -----------------------------------------------------------------------

    async def _check_capacity(self, kb: KnowledgeBase) -> None:
        n = await self.store.count_documents(kb.id)
        if n >= MAX_DOCUMENTS:
            raise KnowledgeError(
                422,
                "too_many_documents",
                f"knowledge base {kb.name!r} already has {n} documents (max {MAX_DOCUMENTS})",
            )

    async def add_upload(
        self,
        kb: KnowledgeBase,
        *,
        name: str,
        content_type: str | None,
        data: bytes,
        user_sub: str | None = None,
    ) -> KnowledgeDocument:
        """업로드 문서. 형식은 확장자와 content type 으로 지금 가르고 본문은 GridFS 에 둔 뒤 문서를
        `pending` 으로 넣는다."""
        self._check_writable(kb)
        try:
            fmt = accepted_format(name, content_type, len(data))
        except FormatRefused as e:
            raise KnowledgeError(e.status, e.code, e.detail) from e
        await self._check_capacity(kb)
        stored_type = CONTENT_TYPES[fmt]
        doc = KnowledgeDocument(
            knowledge_base_id=kb.id,
            tenant=kb.tenant,
            name=name,
            source=Source(kind="upload"),
            content_type=stored_type,
            bytes=len(data),
            sha256=hashlib.sha256(data).hexdigest(),
            updated_by=user_sub,
        )
        await self.files.put(kb.id, doc.id, data, stored_type)
        await self.store.add_document(doc)
        return doc

    async def add_url(
        self, kb: KnowledgeBase, *, url: str, name: str | None = None, user_sub: str | None = None
    ) -> KnowledgeDocument:
        """URL 문서. `check_url_async` 를 지난 URL 만 받고 본문은 워커가 `fetch_url` 로 받는다."""
        self._check_writable(kb)
        try:
            checked = await check_url_async(url, resolver=self.resolver)
        except BlockedURL as e:
            raise KnowledgeError(status_of(e.code, 422), e.code, e.detail) from e
        await self._check_capacity(kb)
        doc_name = (name or "").strip() or name_from_url(checked)
        doc = KnowledgeDocument(
            knowledge_base_id=kb.id,
            tenant=kb.tenant,
            name=doc_name,
            source=Source(kind="url", url=checked),
            content_type=content_type_for(doc_name),
            updated_by=user_sub,
        )
        await self.store.add_document(doc)
        return doc

    async def reindex_document(
        self, kb: KnowledgeBase, doc: KnowledgeDocument
    ) -> KnowledgeDocument:
        self._check_writable(kb)
        if doc.state in ("indexing", "deleting", "purging"):
            raise _busy(doc)
        if not await self.store.mark_pending(doc.id):
            await self._raise_transition_lost(kb, doc)
        got = await self.store.get_document(doc.id, kb_id=kb.id)
        return got or doc

    async def reindex_all(self, kb: KnowledgeBase) -> int:
        self._check_writable(kb)
        await self._check_not_indexing(kb)
        return await self.store.mark_all_pending(kb.id)

    async def delete_document(self, kb: KnowledgeBase, doc: KnowledgeDocument) -> KnowledgeDocument:
        """상태를 `deleting` 으로 바꾸고 돌려준다. 행·본문·문서는 워커가 지운다. 실패한 삭제의
        "다시 삭제" 도 같은 경로다."""
        self._check_writable(kb)
        # deleting 은 막지 않는다. 실패한 삭제를 다시 거는 길이다
        if doc.state in ("indexing", "purging"):
            raise _busy(doc)
        if doc.state != "deleting" and not await self.store.mark_deleting(doc.id):
            await self._raise_transition_lost(kb, doc)
        got = await self.store.get_document(doc.id, kb_id=kb.id)
        return got or doc

    async def _raise_transition_lost(self, kb: KnowledgeBase, doc: KnowledgeDocument) -> None:
        """라우터가 읽은 뒤 워커가 문서를 든 경우다. 상태 필터가 갱신을 거부했으니 지금 상태로
        409 를 낸다(`acquire` 가 원자적이라 이 필터 하나로 창이 닫힌다)."""
        now = await self.store.get_document(doc.id, kb_id=kb.id)
        if now is None:
            raise KnowledgeError(404, "not_found", f"document {doc.id} not found")
        raise _busy(now)

    # -- 삭제 -----------------------------------------------------------------------

    async def delete(self, kb: KnowledgeBase) -> dict[str, Any]:
        """참조 409 `in_use`, `indexing` 409, `deleting_at`, `purging`, `DROP TABLE`,
        문서·본문, 리소스 순이다. `DROP` 이 안 되면 `orphaned_table` 로 남기고 `{deleted: false,
        warning: table_not_dropped}` 다. 다시 DELETE 하면 같은 순서를 다시 탄다."""
        await self.references.refuse_any(
            "knowledge-base",
            kb.id,
            KNOWLEDGE_BASE_DELETE,
            KnowledgeError,
            "knowledge base is used by agents",
            every_key=True,
        )
        await self._check_not_indexing(kb)
        if kb.deleting_at is None:
            kb.deleting_at = utcnow()
            kb.updated_at = kb.deleting_at
            # 칸마다 쓴다. 그사이 저장된 사람의 칸을 되돌리지 않는다 (P68 2절)
            await self.store.set_fields(
                kb.id, {"deleting_at": kb.deleting_at, "updated_at": kb.updated_at}
            )
        await self.store.mark_purging(kb.id)
        if not await self._drop_table(kb):
            kb.state = "orphaned_table"
            kb.updated_at = utcnow()
            await self.store.set_fields(kb.id, {"state": kb.state, "updated_at": kb.updated_at})
            return {
                "id": kb.id,
                "deleted": False,
                "table_dropped": False,
                "warning": "table_not_dropped",
            }
        await self.files.delete_kb(kb.id)
        await self.store.delete_documents(kb.id)
        await self.store.delete(kb.id)
        return {"id": kb.id, "deleted": True, "table_dropped": True}

    async def _drop_table(self, kb: KnowledgeBase) -> bool:
        """Database 등록이 이미 없으면 지울 길이 없으므로 성공으로 본다 (Database 삭제는 참조 중이면
        409 라 정상 경로에서는 생기지 않는다)."""
        ds = await self.data_services.get(kb.data_service_id)
        if ds is None:
            return True
        try:
            password = await self._password(ds)
            conn = await self._connect(ds, password)
        except KnowledgeError:
            return False
        try:
            async with conn:
                await pg.drop_table(conn, kb.table)
        except (psycopg.Error, OSError):
            return False
        return True

    # -- 집계 -----------------------------------------------------------------------

    async def summary(self, kb: KnowledgeBase) -> dict[str, Any]:
        """`status` 집계를 붙인 응답 dict."""
        return (await self.summaries([kb]))[0]

    async def summaries(self, kbs: list[KnowledgeBase]) -> list[dict[str, Any]]:
        """여럿의 `summary`. 집계는 aggregate 한 번이라 목록이 길어도 Mongo 왕복이 늘지 않는다."""
        statuses = await self.store.statuses([kb.id for kb in kbs])
        out = []
        for kb in kbs:
            kb.status = statuses[kb.id]
            out.append(kb.model_dump(mode="json"))
        return out


__all__ = [
    "DDL_STATEMENT_TIMEOUT",
    "MAX_DOCUMENTS",
    "MAX_DOCUMENT_BYTES",
    "KnowledgeError",
    "KnowledgeService",
    "content_type_for",
    "name_from_url",
]
