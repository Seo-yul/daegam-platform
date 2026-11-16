"""문서 파싱 서비스 `daegam-docparse` 를 부르는 자리 (D61). 첨부 업로드와 Knowledge 인덱서가 함께
쓴다.

텍스트 형식(txt·md·csv·tsv·json)은 파서가 필요 없어 여기서 바로 UTF-8 로 풀고(잘못된 바이트는
대체 문자) 서비스를 부르지 않는다. 나머지는 `POST /extract?format=&purpose=` 에 본문을 그대로 싣고
따로 둔 토큰(Secret `daegam-docparse-token`)을 Bearer 로 붙인다. 플랫폼은 파서를 import 하지
않는다.

서비스의 422·413 은 코드 그대로 `DocparseError` 다. 429 는 `extract_busy` 이고, 연결 실패·시간 초과·
5xx·토큰 거절·설정 없음은 503 `extract_unavailable` 이다. 둘은 다시 부를 만한 실패(`retryable`)이고
다시 부를지는 부르는 쪽이 정한다. 인덱서는 물러났다 다시 부르고 업로드는 사용자에게 돌려준다.
"""

from __future__ import annotations

from typing import Any

import httpx

from daegam.config import Settings
from daegam.documents import DOCPARSE_PORT, TOKEN_ENV
from daegam.documents.formats import TEXT_FORMATS
from daegam.documents.result import Extracted, ExtractError, limited
from daegam.error_codes import status_of
from daegam.errors import ServiceError

# 서비스의 추출 상한(30초)에 연결과 본문 전송 몫을 더한 호출 상한(초)
CALL_TIMEOUT = 45.0
RETRYABLE_CODES = frozenset({"extract_busy", "extract_unavailable"})


class DocparseError(ServiceError):
    """문서에서 글을 뽑지 못했다. `retryable` 이면 서비스가 바쁘거나 연결할 수 없었다."""

    @property
    def retryable(self) -> bool:
        return self.code in RETRYABLE_CODES


def _unavailable(detail: str) -> DocparseError:
    return DocparseError(503, "extract_unavailable", detail)


def _error_of(response: httpx.Response) -> DocparseError:
    if response.status_code == 429:
        retry_after = response.headers.get("retry-after")
        headers = {"Retry-After": retry_after} if retry_after else None
        return DocparseError(429, "extract_busy", "the document parser is busy", headers=headers)
    if response.status_code not in (413, 422):
        return _unavailable(f"the document parser answered {response.status_code}")
    try:
        body: Any = response.json()
    except ValueError:
        body = {}
    code = body.get("code") if isinstance(body, dict) else None
    detail = body.get("detail") if isinstance(body, dict) else None
    return DocparseError(response.status_code, str(code or "extract_failed"), str(detail or ""))


class DocparseClient:
    def __init__(
        self,
        base_url: str,
        token: str | None,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
    ):
        self.base_url = base_url.rstrip("/")
        self.token = token
        # 테스트가 가짜 서비스를 넣는 자리
        self.transport = transport

    @classmethod
    def from_settings(cls, settings: Settings) -> DocparseClient:
        """주소는 `DAEGAM_DOCPARSE_URL` 이 먼저다. 비우면 클러스터 안이면 플랫폼 네임스페이스의
        Service 이고, 밖(make run)이면 같은 호스트에서 띄운 개발 서버다."""
        if settings.docparse_url:
            base_url = settings.docparse_url
        elif settings.in_cluster:
            base_url = f"http://daegam-docparse.{settings.namespace}.svc:{DOCPARSE_PORT}"
        else:
            base_url = f"http://127.0.0.1:{DOCPARSE_PORT}"
        return cls(base_url, settings.docparse_token)

    async def extract(self, fmt: str, data: bytes, *, purpose: str) -> Extracted:
        """형식 하나의 글. 텍스트 형식은 서비스를 부르지 않는다. 첨부의 글은 서비스가 자른 답도
        여기서 상한에 다시 맞춘다. 상한을 넘는 글은 런타임이 422 로 거절하기 때문이다."""
        if fmt in TEXT_FORMATS:
            return limited(Extracted(data.decode("utf-8", errors="replace")), purpose)
        response = await self._post(fmt, data, purpose)
        if response.status_code != 200:
            raise _error_of(response)
        try:
            return limited(Extracted.from_dict(response.json()), purpose)
        except ValueError as e:
            raise DocparseError(422, "extract_failed", "malformed extraction result") from e
        except ExtractError as e:
            raise DocparseError(status_of(e.code, 422), e.code, e.detail) from e

    async def _post(self, fmt: str, data: bytes, purpose: str) -> httpx.Response:
        if not self.token:
            raise _unavailable(f"{TOKEN_ENV} is not set")
        try:
            async with httpx.AsyncClient(
                base_url=self.base_url, transport=self.transport, timeout=CALL_TIMEOUT
            ) as client:
                return await client.post(
                    "/extract",
                    params={"format": fmt, "purpose": purpose},
                    content=data,
                    headers={"Authorization": f"Bearer {self.token}"},
                )
        except httpx.HTTPError as e:
            raise _unavailable(type(e).__name__) from e


__all__ = ["CALL_TIMEOUT", "DocparseClient", "DocparseError"]
