"""오류 응답 생성 (코어). FastAPI HTTPException 하나를 만드는 것이 전부다.

응답 본문은 `{"detail": {"code", "detail", ...}}` 한 형식이다. 라우터는 api/_common.py 가 재수출하는
`error`·`unauthorized` 와 그 위의 `forbidden`·`not_found` 를 쓰고, 라우터 밖에서 HTTPException 을
만들어야 하는 모듈(names.py, store/agent_instance_tokens.py)은 api/ 에 기대지 않으려고 여기를 직접
쓴다.
"""

from __future__ import annotations

import math
from collections.abc import Callable
from datetime import timedelta
from typing import Any

from fastapi import HTTPException

from daegam import error_codes


def error(status: int, code: str, detail: str, **extra: Any) -> HTTPException:
    """상태 코드와 오류 코드, 사람이 읽는 설명, 부가 필드로 HTTPException 을 만든다."""
    return HTTPException(status, {"code": code, "detail": detail, **extra})


def unauthorized(
    detail: str, *, code: str = "unauthorized", challenge: str = "Bearer"
) -> HTTPException:
    """토큰 인증 실패 401. 리소스의 존재를 토큰 없이 드러내지 않으려고 404 대신 이것을 내는
    자리가 있으므로 detail 에 무엇이 없는지 적지 않는다.

    `challenge` 는 RFC 6750 의 WWW-Authenticate 값이다. 실패 코드가 `unauthorized` 하나뿐인
    토큰 인증은 그냥 `Bearer` 이고, 코드가 여러 가지인 OIDC 검증(api/deps.py)만
    `Bearer error="<code>"` 로 코드를 실어 보낸다."""
    return HTTPException(
        401, {"code": code, "detail": detail}, headers={"WWW-Authenticate": challenge}
    )


def retry_after_header(delay: timedelta) -> dict[str, str]:
    """429 의 `Retry-After` 헤더. RFC 9110 의 delay-seconds(정수 초)이고 1초 아래로 내리지 않는다.
    로그인 잠금과 개인 토큰 발급 상한이 같은 꼴로 내므로 화면은 한 꼴만 읽는다."""
    return {"Retry-After": str(max(1, math.ceil(delay.total_seconds())))}


class ForwardedError(HTTPException):
    """다른 서비스(에이전트 런타임)가 답한 상태와 코드를 그대로 넘기는 HTTPException. 플랫폼의 오류
    코드표 검사는 이것을 건너뛴다. 그 코드의 집은 런타임의 표이고 런타임 테스트가 본다 (P74
    2.2절)."""


def check_registered(status: int, detail: Any) -> None:
    """테스트(`error_codes.STRICT`)에서만 답하는 상태와 코드가 오류 코드표에 맞는지 본다. 코드가
    없는 본문(FastAPI 의 검증 목록, 글자 detail)은 볼 것이 없다."""
    if not error_codes.STRICT or not isinstance(detail, dict) or "code" not in detail:
        return
    code = str(detail["code"])
    if not error_codes.allowed(status, code):
        raise AssertionError(f"{status} {code} 가 오류 코드표(daegam/error_codes.py)와 다르다")


class ServiceError(Exception):
    """서비스 계층의 오류. 라우터는 잡지 않고 앱의 전역 핸들러가 `{detail: to_dict()}` 로 낸다
    (D30). 401 챌린지 머리를 더해야 하는 로컬 인증 라우터만 직접 다시 만든다.

    종류마다 클래스를 따로 두는 것은 호출자가 `except InstanceError` 처럼 좁게 잡으라는 뜻이다.
    모양은 하나여야 응답 형식이 갈라지지 않으므로 여기 한 곳에만 둔다. reconcile 처럼 여러
    서비스를 함께 도는 쪽은 이 기반을 잡아 종류를 늘려도 빠뜨리지 않는다. `headers` 는 응답 헤더다
    (429 의 Retry-After 등). 전역 핸들러가 그대로 싣는다."""

    def __init__(
        self,
        status: int,
        code: str,
        detail: str,
        *,
        headers: dict[str, str] | None = None,
        **extra: Any,
    ):
        super().__init__(detail)
        self.status = status
        self.code = code
        self.detail = detail
        self.headers = headers
        self.extra = extra

    def to_dict(self) -> dict[str, Any]:
        return {"code": self.code, "detail": self.detail, **self.extra}

    @classmethod
    def not_null[T](cls, value: T | None, field: str, *, code: str = "invalid_value") -> T:
        """PATCH 로 보낸 칸에 null 은 못 쓴다. 라우터의 `api/_common.py` `not_null` 과 같은 422
        본문을 이 종류로 낸다. 서비스가 칸을 검사하는 차례 안에서 부르려고 둔다."""
        if value is None:
            raise cls(422, code, f"{field} cannot be null")
        return value


def revision_conflict(
    make: Callable[..., Exception],
    label: str,
    name: str,
    action: str = "save",
    *,
    revision: int | None = None,
) -> Exception:
    """409 `revision_conflict`. 서버가 읽은 뒤 다른 저장이 먼저 지나가 조건부 저장이 맞지 않았다.
    make 는 그 자리의 오류 생성자(`ServiceError` 의 하위 클래스, 서비스의 `_error`, `error`)다.
    인스턴스의 정의 저장과 P68 의 사람의 저장이 함께 쓴다. `revision` 을 주면 지금 저장된 리비전
    번호를 본문에 실어, 호출자가 다시 읽지 않고도 무엇을 기준으로 다시 고칠지 안다 (P48 6.7절)."""
    extra = {} if revision is None else {"revision": revision}
    return make(
        409,
        "revision_conflict",
        f"{label} {name} was saved by another request; reload and retry {action}",
        **extra,
    )


def save_missed(
    make: Callable[..., Exception],
    label: str,
    name: str,
    *,
    found: bool,
    deleting: bool,
    action: str = "save",
    revision: int | None = None,
) -> Exception:
    """사람의 저장이 맞지 않은 뒤 다시 읽은 결과로 고른 답 (P68 2절). 문서가 없으면 404
    `not_found`, 삭제 중이면 409 `deleting`, 그 밖은 409 `revision_conflict` 다. 인스턴스의 정의
    저장도 이것으로 답하고, 다시 읽은 지금 리비전 번호를 `revision` 으로 싣는다 (P48 6.7절)."""
    if not found:
        return make(404, "not_found", f"{label} {name} not found")
    if deleting:
        return make(409, "deleting", f"{label} {name} is being deleted")
    return revision_conflict(make, label, name, action, revision=revision)
