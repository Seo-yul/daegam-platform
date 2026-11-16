"""배포 게이트의 판정 (P45 12절). 배포 경로(`services/agents.py`, `services/agent_rollback.py`)와
게이트(`gate.py`)가 함께 쓴다. 게이트 모듈이 되돌리기 모듈을 읽으므로 따로 둔다."""

from __future__ import annotations

from dataclasses import dataclass, field

from daegam.services.agent_graph import Issue


@dataclass
class GateOutcome:
    """판정. run 은 게이트가 받아들인 실행, skipped 는 건너뛴 이유다. 둘 다 없으면 게이트가 걸리지
    않았다. warnings 는 배포의 경고다(평가를 끈 설치의 `evaluation_gate_off`)."""

    run: str | None = None
    skipped: str | None = None
    warnings: list[Issue] = field(default_factory=list)


__all__ = ["GateOutcome"]
