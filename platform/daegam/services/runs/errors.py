"""백그라운드 실행의 거절 (P41). 서비스와 결정이 함께 쓴다."""

from __future__ import annotations

from daegam.errors import ServiceError


class RunError(ServiceError):
    """실행과 스케줄의 거절."""


__all__ = ["RunError"]
