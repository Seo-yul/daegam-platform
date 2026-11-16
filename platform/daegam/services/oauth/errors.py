"""OAuth 명세의 오류 (P33 1절).

MCP 클라이언트가 부르는 라우트는 RFC 6749·7591·7009 의 모양 `{error, error_description}` 으로
답한다. 그 모양은 여기 `OAuthError` 하나에서만 만들고, 라우터의 변환 함수 하나가 응답으로 바꾼다.
`ServiceError` 의 자손이므로 라우터가 잡지 않은 것은 전역 핸들러가 플랫폼 모양으로 낸다. 화면이
부르는 라우트가 그 길을 쓴다. 설명에는 받은 값을 넣지 않는다.

`audit_code` 는 응답의 `error` 와 다른 코드로 감사 행에 남길 때만 준다. 갱신 토큰의 재사용은 응답이
언제나 `invalid_grant` 이지만 감사는 가족 회수(`refresh_reuse`)와 유예 거절(`refresh_reuse_grace`)을
가른다 (18절).
"""

from __future__ import annotations

from daegam.errors import ServiceError

INVALID_CLIENT = "invalid_client"
INVALID_CLIENT_METADATA = "invalid_client_metadata"
INVALID_GRANT = "invalid_grant"
INVALID_REDIRECT_URI = "invalid_redirect_uri"
INVALID_REQUEST = "invalid_request"
INVALID_SCOPE = "invalid_scope"
UNSUPPORTED_RESPONSE_TYPE = "unsupported_response_type"
UNSUPPORTED_GRANT_TYPE = "unsupported_grant_type"
# RFC 8707. resource 가 없거나 노출한 Gateway·MCP Server 하나로 풀리지 않는다 (6절)
INVALID_TARGET = "invalid_target"
# OIDC. prompt=none 이다. 동의는 늘 사람이 한다
INTERACTION_REQUIRED = "interaction_required"
ACCESS_DENIED = "access_denied"
# RFC 6749 4.1.2.1. 잠든 인가 서버(P53b 6.5절)가 새 인가와 동의 허용에 답한다. 토큰 엔드포인트에는
# 명세의 코드가 없어 같은 글자를 503 과 Retry-After 로 낸다
TEMPORARILY_UNAVAILABLE = "temporarily_unavailable"
# 표준 코드가 아니다. CIMD 문서를 받지 못했다는 것이고 동의 화면이 정해진 문구로 바꾼다 (16절)
CLIENT_METADATA_UNAVAILABLE = "client_metadata_unavailable"


class OAuthError(ServiceError):
    """`code` 가 OAuth 의 `error`, `detail` 이 `error_description` 이다."""

    def __init__(
        self,
        code: str,
        description: str,
        *,
        status: int = 400,
        audit_code: str | None = None,
        headers: dict[str, str] | None = None,
    ):
        super().__init__(status, code, description, headers=headers)
        self.audit_code = audit_code

    def body(self) -> dict[str, str]:
        return {"error": self.code, "error_description": self.detail}
