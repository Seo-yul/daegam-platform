"""Teams 채널의 거절 (P47)."""

from __future__ import annotations

from daegam.errors import ServiceError


class ChannelError(ServiceError):
    """채널의 받는 경로와 연결의 거절."""


__all__ = ["ChannelError"]
