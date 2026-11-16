"""Knowledge Base API (D32, resource-model.md 8절). 테넌트 범위, scope `knowledge:*`.

규칙은 전부 `KnowledgeService` 에 있고 라우터는 입력을 모델로 받아 넘긴다. `KnowledgeError` 는
main.py 의 전역 핸들러가 응답으로 낸다. 이름은 D24 다. `embedding`·`data_service_id` 는 만든
뒤 바꿀 수 없다(422 `embedding_immutable`·`data_service_immutable`).
"""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends, File, Request, UploadFile, status
from pydantic import BaseModel, ConfigDict, Field

from daegam.api._common import error, not_null, or_404, patched_fields, references_out
from daegam.api.deps import Tenant, User, require_scope
from daegam.api.schemas import ERROR_RESPONSES, too_large_response
from daegam.api.schemas.knowledge import (
    KnowledgeBaseDeleteResponse,
    KnowledgeBaseOut,
    KnowledgeDocumentDeleteResponse,
    KnowledgeDocumentOut,
    KnowledgeReindexResponse,
)
from daegam.api.schemas.references import ReferencesResponse
from daegam.names import check_name_unchanged, validate_display_name, validate_name
from daegam.services.knowledge.service import (
    MAX_DOCUMENT_BYTES,
    KnowledgeService,
)
from daegam.store.knowledge import (
    Chunking,
    KnowledgeBase,
    KnowledgeDocument,
    Limits,
    MongoKnowledgeStore,
)

router = APIRouter(responses=ERROR_RESPONSES, prefix="/knowledge-bases", tags=["knowledge-bases"])
TOO_LARGE_RESPONSE = too_large_response(f"문서가 {MAX_DOCUMENT_BYTES // (1024 * 1024)}MB 를 넘는다")


def get_store(request: Request) -> MongoKnowledgeStore:
    return request.app.state.knowledge


def get_service(request: Request) -> KnowledgeService:
    return request.app.state.knowledge_service


KnowledgeBases = Annotated[MongoKnowledgeStore, Depends(get_store)]
Service = Annotated[KnowledgeService, Depends(get_service)]


class EmbeddingIn(BaseModel):
    """provider 는 Platform Settings 의 provider 이름. model 을 비우면 provider 의 임베딩 모델이고
    dimensions 를 비우면 알려진 모델 표에서 온다."""

    model_config = ConfigDict(extra="forbid")

    provider: str
    model: str | None = None
    dimensions: int | None = Field(default=None, ge=1)


class KnowledgeBaseCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str
    display_name: str | None = None  # 없으면 name (D24)
    data_service_id: str  # kind: postgres Database
    embedding: EmbeddingIn
    chunking: Chunking | None = None
    limits: Limits | None = None


class KnowledgeBasePatch(BaseModel):
    """`display_name`·`chunking`·`limits` 만 바꾼다. `name` 은 저장값과 같아야 하고(422
    name_immutable) `embedding`·`data_service_id` 는 보내면 422 다."""

    model_config = ConfigDict(extra="forbid")

    name: str | None = None
    display_name: str | None = None
    chunking: Chunking | None = None
    limits: Limits | None = None
    embedding: EmbeddingIn | None = None
    data_service_id: str | None = None


class KnowledgeDocumentUrlBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    url: str
    name: str | None = None  # 없으면 URL 마지막 조각


async def _get_or_404(store: MongoKnowledgeStore, tenant: str, kb_id: str) -> KnowledgeBase:
    return or_404(await store.get(kb_id, tenant=tenant), "knowledge base", kb_id)


async def _doc_or_404(
    store: MongoKnowledgeStore, kb: KnowledgeBase, doc_id: str
) -> KnowledgeDocument:
    return or_404(await store.get_document(doc_id, kb_id=kb.id), "document", doc_id)


def _doc_out(doc: KnowledgeDocument) -> dict[str, Any]:
    return doc.model_dump(mode="json")


# -- Knowledge Base -------------------------------------------------------------------------


@router.post(
    "",
    response_model=KnowledgeBaseOut,
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(require_scope("knowledge:write"))],
)
async def create_knowledge_base(
    tenant: Tenant, user: User, service: Service, body: KnowledgeBaseCreate
) -> dict[str, Any]:
    """Database 에 접속해 pgvector 확장·벡터 테이블을 만들고 리소스를 저장한다. 어느 단계든 실패하면
    422 이고 리소스는 생기지 않는다."""
    name = validate_name(body.name)
    display_name = validate_display_name(body.display_name, default=name)
    kb = await service.create(
        tenant,
        name=name,
        display_name=display_name,
        data_service_id=body.data_service_id,
        embedding=body.embedding.model_dump(),
        chunking=body.chunking,
        limits=body.limits,
        user_sub=user.sub,
    )
    return await service.summary(kb)


@router.get(
    "",
    response_model=list[KnowledgeBaseOut],
    dependencies=[Depends(require_scope("knowledge:read"))],
)
async def list_knowledge_bases(
    tenant: Tenant, store: KnowledgeBases, service: Service
) -> list[dict[str, Any]]:
    return await service.summaries(await store.list(tenant))


@router.get(
    "/{kb_id}",
    response_model=KnowledgeBaseOut,
    dependencies=[Depends(require_scope("knowledge:read"))],
)
async def get_knowledge_base(
    tenant: Tenant, store: KnowledgeBases, service: Service, kb_id: str
) -> dict[str, Any]:
    return await service.summary(await _get_or_404(store, tenant, kb_id))


@router.get(
    "/{kb_id}/references",
    response_model=ReferencesResponse,
    dependencies=[Depends(require_scope("knowledge:read"))],
)
async def knowledge_base_references(
    tenant: Tenant, store: KnowledgeBases, kb_id: str, request: Request
) -> dict[str, Any]:
    """이 Knowledge Base 의 참조 (D43). knowledge 노드로 가리키는 Agent 다."""
    kb = await _get_or_404(store, tenant, kb_id)
    return await references_out(request, "knowledge-base", kb.id, tenant=tenant)


@router.patch(
    "/{kb_id}",
    response_model=KnowledgeBaseOut,
    dependencies=[Depends(require_scope("knowledge:write"))],
)
async def patch_knowledge_base(
    tenant: Tenant,
    user: User,
    store: KnowledgeBases,
    service: Service,
    kb_id: str,
    patch: KnowledgeBasePatch,
) -> dict[str, Any]:
    """`chunking` 이 바뀌면 모든 문서가 `pending` 으로 돌아간다(`indexing` 문서가 있으면 409)."""
    kb = await _get_or_404(store, tenant, kb_id)
    sent = patched_fields(patch)
    if "embedding" in sent:
        raise error(
            422,
            "embedding_immutable",
            "embedding cannot be changed after creation; create a new knowledge base instead",
        )
    if "data_service_id" in sent:
        raise error(
            422,
            "data_service_immutable",
            "data_service_id cannot be changed after creation; create a new knowledge base",
        )
    if "name" in sent:
        check_name_unchanged(not_null(patch.name, "name", code="invalid_name"), kb.name)
    display_name: str | None = None
    if "display_name" in sent:
        display_name = validate_display_name(
            not_null(patch.display_name, "display_name", code="invalid_display_name"),
            default=kb.name,
        )
    if "chunking" in sent:
        not_null(patch.chunking, "chunking")
    if "limits" in sent:
        not_null(patch.limits, "limits")
    kb = await service.patch(
        kb,
        display_name=display_name,
        chunking=patch.chunking,
        limits=patch.limits,
        user_sub=user.sub,
    )
    return await service.summary(kb)


@router.delete(
    "/{kb_id}",
    response_model=KnowledgeBaseDeleteResponse,
    response_model_exclude_none=True,
    dependencies=[Depends(require_scope("knowledge:write"))],
)
async def delete_knowledge_base(
    tenant: Tenant, store: KnowledgeBases, service: Service, kb_id: str
) -> dict[str, Any]:
    """참조(409 in_use)·인덱싱 중(409 indexing)이 없으면 테이블·문서·본문·리소스를 지운다. 테이블을
    못 지우면 `{deleted: false, warning: table_not_dropped}` 이고 다시 DELETE 할 수 있다."""
    kb = await _get_or_404(store, tenant, kb_id)
    return await service.delete(kb)


# -- 문서 ---------------------------------------------------------------------------------


@router.get(
    "/{kb_id}/documents",
    response_model=list[KnowledgeDocumentOut],
    dependencies=[Depends(require_scope("knowledge:read"))],
)
async def list_knowledge_documents(
    tenant: Tenant, store: KnowledgeBases, kb_id: str
) -> list[dict[str, Any]]:
    kb = await _get_or_404(store, tenant, kb_id)
    return [_doc_out(d) for d in await store.list_documents(kb.id)]


@router.post(
    "/{kb_id}/documents",
    response_model=KnowledgeDocumentOut,
    status_code=status.HTTP_201_CREATED,
    responses=TOO_LARGE_RESPONSE,
    dependencies=[Depends(require_scope("knowledge:write"))],
)
async def upload_knowledge_document(
    tenant: Tenant,
    user: User,
    store: KnowledgeBases,
    service: Service,
    kb_id: str,
    file: Annotated[UploadFile, File()],
) -> dict[str, Any]:
    """파일(txt·md·csv·tsv·json·html·pdf·docx·pptx·xlsx·hwpx, 10MB)을 업로드한다. 곧 인덱싱한다."""
    kb = await _get_or_404(store, tenant, kb_id)
    data = await file.read(MAX_DOCUMENT_BYTES + 1)
    doc = await service.add_upload(
        kb,
        name=file.filename or "document",
        content_type=file.content_type,
        data=data,
        user_sub=user.sub,
    )
    return _doc_out(doc)


@router.post(
    "/{kb_id}/documents/url",
    response_model=KnowledgeDocumentOut,
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(require_scope("knowledge:write"))],
)
async def add_knowledge_document_url(
    tenant: Tenant,
    user: User,
    store: KnowledgeBases,
    service: Service,
    kb_id: str,
    body: KnowledgeDocumentUrlBody,
) -> dict[str, Any]:
    """공개 URL 문서를 더한다. 사설·클러스터 주소는 422 `url_*` 이고 본문은 워커가 받는다."""
    kb = await _get_or_404(store, tenant, kb_id)
    doc = await service.add_url(kb, url=body.url, name=body.name, user_sub=user.sub)
    return _doc_out(doc)


@router.post(
    "/{kb_id}/reindex",
    response_model=KnowledgeReindexResponse,
    dependencies=[Depends(require_scope("knowledge:write"))],
)
async def reindex_knowledge_base(
    tenant: Tenant, store: KnowledgeBases, service: Service, kb_id: str
) -> dict[str, Any]:
    """모든 문서를 `pending` 으로 되돌린다(`indexing` 문서가 있으면 409)."""
    kb = await _get_or_404(store, tenant, kb_id)
    n = await service.reindex_all(kb)
    return {"id": kb.id, "pending": n}


@router.get(
    "/{kb_id}/documents/{doc_id}",
    response_model=KnowledgeDocumentOut,
    dependencies=[Depends(require_scope("knowledge:read"))],
)
async def get_knowledge_document(
    tenant: Tenant, store: KnowledgeBases, kb_id: str, doc_id: str
) -> dict[str, Any]:
    kb = await _get_or_404(store, tenant, kb_id)
    return _doc_out(await _doc_or_404(store, kb, doc_id))


@router.post(
    "/{kb_id}/documents/{doc_id}/reindex",
    response_model=KnowledgeDocumentOut,
    dependencies=[Depends(require_scope("knowledge:write"))],
)
async def reindex_knowledge_document(
    tenant: Tenant, store: KnowledgeBases, service: Service, kb_id: str, doc_id: str
) -> dict[str, Any]:
    """문서 하나를 `pending` 으로 되돌린다(`indexing` 이면 409)."""
    kb = await _get_or_404(store, tenant, kb_id)
    doc = await _doc_or_404(store, kb, doc_id)
    doc = await service.reindex_document(kb, doc)
    return _doc_out(doc)


@router.delete(
    "/{kb_id}/documents/{doc_id}",
    response_model=KnowledgeDocumentDeleteResponse,
    dependencies=[Depends(require_scope("knowledge:write"))],
)
async def delete_knowledge_document(
    tenant: Tenant, store: KnowledgeBases, service: Service, kb_id: str, doc_id: str
) -> dict[str, Any]:
    """상태를 `deleting` 으로 바꾼다. 행·본문·문서는 워커가 지운다(`indexing` 이면 409). 실패한
    삭제의 "다시 삭제" 도 같은 호출이다."""
    kb = await _get_or_404(store, tenant, kb_id)
    doc = await _doc_or_404(store, kb, doc_id)
    doc = await service.delete_document(kb, doc)
    return {"id": doc.id, "state": doc.state}
