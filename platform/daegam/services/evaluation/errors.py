"""평가 서비스의 오류 (P45). 전역 핸들러가 플랫폼 형식으로 낸다."""

from __future__ import annotations

from daegam.errors import ServiceError


class EvaluationError(ServiceError):
    """평가 서비스 오류."""
