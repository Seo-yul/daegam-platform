"""최근 로그인 (P33 8절, P53b 6.4절).

오래 가는 자격을 만드는 요청은 사람이 10분(시계 여유 30초) 안에 로그인했어야 한다. local 모드에서
OAuth 연결을 허용할 때는 제어면 토큰의 `iat` 가, oidc 모드에서 개인·서비스 어카운트 토큰을 내거나
연결을 허용할 때는 UI 가 함께 보낸 ID 토큰의 `auth_time` 이 그 로그인의 시각이다. 지나지 못하면 403
`reauth_required` 이고 UI 는 다시 로그인한 뒤 돌아온다.
"""

from __future__ import annotations

from datetime import timedelta

from daegam.auth.oidc import LEEWAY_SECONDS

# 회수 한도와 남용 방어의 재료라 설정으로 흔들지 않는다
RECENT_AUTH = timedelta(minutes=10)
REAUTH_REQUIRED = "reauth_required"
_MINUTES = int(RECENT_AUTH.total_seconds() // 60)
REAUTH_DETAIL = f"sign in again; the last sign-in is older than {_MINUTES} minutes"


def signed_in_recently(at: object, now: float) -> bool:
    """at(epoch 초)이 지금(now)에서 `RECENT_AUTH` 안인가. 앞뒤로 시계 여유를 둔다. 숫자가 아니면
    거짓이다."""
    if isinstance(at, bool) or not isinstance(at, int | float):
        return False
    return now - RECENT_AUTH.total_seconds() - LEEWAY_SECONDS <= at <= now + LEEWAY_SECONDS
