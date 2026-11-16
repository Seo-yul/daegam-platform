"""Spec Generator 루프가 부르는 LLM provider 의 테스트 대역 (P75, D87). 플랫폼 꾸러미가 아니라
테스트 곁에 둔다."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from daegam.services.llm_providers import Message, ProviderError, Usage


@dataclass
class FakeProvider:
    """고정 응답 시퀀스. 항목이 Exception 이면 그 호출에서 던진다.
    시퀀스가 끝나면 마지막 응답을 반복한다."""

    responses: list[str | Exception]
    name: str = "fake"
    model: str = "fake-1"
    usage_per_call: Usage = field(
        default_factory=lambda: {"input_tokens": 1000, "output_tokens": 500}
    )
    calls: list[dict[str, Any]] = field(default_factory=list)

    async def complete(
        self, system: str, messages: list[Message], *, max_tokens: int
    ) -> tuple[str, Usage]:
        i = len(self.calls)
        self.calls.append({"system": system, "messages": list(messages), "max_tokens": max_tokens})
        if not self.responses:
            raise ProviderError("empty_response", "fake provider has no responses")
        item = self.responses[min(i, len(self.responses) - 1)]
        if isinstance(item, Exception):
            raise item
        return item, dict(self.usage_per_call)
