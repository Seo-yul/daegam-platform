"""임베딩 클라이언트 (D32). provider 행 하나에서 langchain `Embeddings` 를 만든다.

openai 는 `OpenAIEmbeddings(check_embedding_ctx_length=False, chunk_size=64, max_retries=0,
timeout=60)` 이다. SDK 재시도는 끈다. 호출 id 하나가 HTTP 요청 하나여야 하므로(P37 2절) 다시
부르기는 배치마다 인덱서가 한다. `check_embedding_ctx_length` 를 끄는 이유는 기본값이 문자열 대신
tiktoken 토큰 id 배열을 보내 OpenAI 가 아닌 서버가 그것을 읽지 못하기 때문이고, 청크가 4000 자
이하라 상한 검사는 필요 없다. `dimensions` 는 `text-embedding-3*` 에만 보낸다. stub 은 런타임과
같은 해시 임베딩이다.

오류는 코드와 클래스 이름·HTTP 상태만 남긴다. provider 원문에는 마스킹된 키가 들어 있다.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import openai
from langchain_core.embeddings import Embeddings
from langchain_openai import OpenAIEmbeddings

from daegam import llm_http
from daegam.services.knowledge.stub_embedding import hash_embedding

BATCH_SIZE = 64
TIMEOUT = 60.0
MAX_RETRIES = 0
# dimensions 인자를 받는 모델 계열. 그 밖은 첫 임베딩의 길이로 검사한다
# (embedding_dimension_mismatch)
DIMENSIONS_MODEL_PREFIX = "text-embedding-3"


class EmbeddingError(Exception):
    """임베딩 실패. detail 은 우리가 만든 한 줄이고 provider 원문은 없다."""

    def __init__(self, code: str, detail: str):
        super().__init__(f"{code}: {detail}")
        self.code = code
        self.detail = detail


class StubEmbeddings(Embeddings):
    """kind: stub 의 임베딩. 인덱서와 런타임이 같은 `hash_embedding` 을 써서 e2e 가 성립한다."""

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


def build_embeddings(
    kind: str,
    *,
    api_key: str | None,
    model: str,
    dimensions: int,
    base_url: str | None = None,
    http_async_client: Any = None,
    bearer: Callable[[], str] | None = None,
    gateway: bool = False,
) -> Embeddings:
    """provider kind 별 클라이언트. `http_async_client` 는 테스트가 목 transport 를 끼우는
    자리다. 게이트웨이 경로(P38 6절)는 요청마다 토큰을 싣는 bearer 훅을 달고 연결 제한이 5초다."""
    if kind == "stub":
        return StubEmbeddings(dimensions)
    if kind == "openai":
        kwargs: dict[str, Any] = {
            "model": model,
            "api_key": api_key,
            "base_url": base_url,
            "check_embedding_ctx_length": False,
            "chunk_size": BATCH_SIZE,
            "max_retries": MAX_RETRIES,
            "timeout": llm_http.gateway_timeout(TIMEOUT) if gateway else TIMEOUT,
        }
        if model.startswith(DIMENSIONS_MODEL_PREFIX):
            kwargs["dimensions"] = dimensions
        # 상관 헤더 훅 (P37 2절). 준 클라이언트(테스트의 목)에도 단다
        kwargs["http_async_client"] = llm_http.hooked_async_client(
            http_async_client,
            base_url=base_url if bearer is not None else None,
            timeout=TIMEOUT,
            bearer=bearer,
        )
        return OpenAIEmbeddings(**kwargs)
    raise EmbeddingError("embedding_not_supported", f"provider kind {kind!r} has no embedding API")


def classify_embedding_error(e: Exception, *, gateway: bool = False) -> EmbeddingError:
    """`openai.APIStatusError` 면 클래스 이름과 상태 코드, 아니면 클래스 이름만. 원문은 버린다.
    크레딧 소진은 다시 해도 같으므로 quota_exhausted 로 가른다(T20). 게이트웨이 경로면 그다음이
    게이트웨이의 세 코드다(P38 7절)."""
    if (code := llm_http.gateway_error(e, gateway=gateway)) is not None:
        return EmbeddingError(code, type(e).__name__)
    if isinstance(e, openai.APIStatusError):
        code = "quota_exhausted" if llm_http.quota_exhausted(e) else "embedding_failed"
        return EmbeddingError(code, f"{type(e).__name__} {e.status_code}")
    return EmbeddingError("embedding_failed", type(e).__name__)
