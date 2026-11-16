"""턴 위임 서비스의 가짜 (P53, P41). 채팅 라우트의 위임 시험과 실행의 시작 워커 시험이 함께 쓴다."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from daegam.services.turn_grants import GrantPerson, OpenedGrant, OpenOutcome


@dataclass
class FakeGrants:
    """`TurnGrantService` 의 가짜다. refuse 에 든 sub 는 사람 검사를 지나지 못하고, raises 가 있으면
    열기가 그것을 던진다."""

    refuse: set[str] = field(default_factory=set)
    raises: Exception | None = None
    opened: list[dict[str, Any]] = field(default_factory=list)
    attached: list[tuple[str, str]] = field(default_factory=list)
    ended: list[tuple[str, str]] = field(default_factory=list)

    async def open(self, *, agent: Any, person: GrantPerson, **kwargs: Any) -> OpenOutcome:
        self.opened.append({"person": person, **kwargs})
        if self.raises is not None:
            raise self.raises
        if person.sub in self.refuse:
            return OpenOutcome(None, refused="disabled")
        gid = f"g{len(self.opened)}"
        return OpenOutcome(OpenedGrant(gid, {"id": gid, "sub": person.sub}))

    async def attach_session(self, grant_id: str, session_id: str) -> None:
        self.attached.append((grant_id, session_id))

    async def end(self, grant_id: str, reason: str) -> None:
        self.ended.append((grant_id, reason))
