"""OAuth 라우트의 감사 재료 (P33 18절).

등록·토큰·회수는 인증 전 경로이고, 동의 화면의 세 호출은 요청 핸들 안에 클라이언트가 있다. 어느
쪽이든 감사 행이 누가 어느 클라이언트로 어느 테넌트에 불렀는지 말하도록, 서비스가 확인한 만큼
`OAuthAudit` 을 채우고 라우터가 응답이 오류여도 그것을 `request.state` 로 옮긴다. 확인하지 못한 칸은
비어 남는다.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass
class OAuthAudit:
    """`action` 은 행동 이름(없으면 핸들러 이름), `sub` 는 인증 전 경로가 확인한 주체, `client_ref`
    는 감사의 `client_id` 칸, `tenant` 는 연결이나 리소스의 테넌트다."""

    action: str | None = None
    sub: str | None = None
    client_ref: str | None = None
    tenant: str | None = None
