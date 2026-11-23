"""첫 명령 전의 자기 점검과 그 끝 코드 (P40 3절, 8절).

`/proc/self/uid_map` 이 같은 uid 로 옮기는 줄을 품으면(user namespace 가 없으면) 실패다. 플랫폼이 준
대상이 하나라도 닿아도 실패다. 결과는 끝 코드만 싣는다. 코드가 끝 코드를 꾸밀 수는 없기 때문이다.
종료 메시지는 보이려고 쓸 뿐 아무것도 정하지 않는다.

- 86: 점검 자체를 하지 못했다. 그 열기만 503 으로 실패하고 아무것도 무효로 하지 않는다.
- 87: 첫 점검(준비 전, 제자리 재시작 뒤 준비 전 포함)에서 uid map 이 그대로이거나 대상이 닿았다.
  대상이 닿은 첫 점검은 `FIRST_SETTLE` 초 안에서 다시 돈다. 새 파드의 IP 에 노드의 NetworkPolicy
  규칙이 놓이기 전에 컨테이너가 먼저 뜰 수 있기 때문이다(k3s 의 정책 컨트롤러는 파드가 생긴 뒤에
  규칙을 맞춘다). 닿은 뒤의 통과는 대상이 하나도 닿지 않은 온전한 바퀴가 두 번 잇따른 것뿐이고,
  그 전에는 듣지 않으므로 명령이 돌지 않는다. 그 시간이 지나도 그렇지 못하면 87 이다. 개발용 점검
  생략(SANDBOX_UNCHECKED)은 다시 돌지 않는다.
- 88: 5분마다의 뒤 점검이 실패했다. 무엇으로 실패했든(점검을 하지 못한 것도) 명령을 거절하고 끝난다.
  명령이 닿을 수 있던 프로세스가 낸 것일 수 있어 그 파드만 끝내고, 플랫폼이 사용자 코드 없는
  프로브를 돌린다.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from dataclasses import dataclass, field
from pathlib import Path

from daegam_sandbox.config import Target
from daegam_sandbox.probes import Outcome, probe_all

EXIT_COULD_NOT_RUN = 86
EXIT_FIRST_FAILED = 87
EXIT_LATER_FAILED = 88

PASSED = "passed"
IDENTITY = "identity"
REACHED = "reached"
ERROR = "error"

# 대상이 닿은 첫 점검을 다시 돌리는 시간(초), 바퀴 사이의 쉼, 닿은 뒤 통과에 필요한 잇따른
# 깨끗한 바퀴 수. 마지막 바퀴는 FIRST_SETTLE 이 다하기 직전에 시작할 수 있어, 가장 늦은 끝은
# FIRST_SETTLE 에 한 바퀴의 길이를 더한 때다. 한 바퀴는 대상이 probes.CONCURRENCY 개 이하면
# 가장 긴 대상 시한(기본 2초, 많아야 10초)이고 그보다 많으면 그만큼 겹쳐 길어진다. 플랫폼이
# 준비를 기다리는 시간(platform/daegam/k8s/sandbox_pods.py 의 READY_WAIT, 30초)을 넘기면 그
# 열기는 무효 없이 준비 실패로 끝난다. 듣기 전이므로 명령은 돌지 않는다
FIRST_SETTLE = 10.0
SETTLE_PAUSE = 1.0
CLEAN_ROUNDS = 2

# 쿠버네티스가 받는 종료 메시지는 4096 바이트까지다
MESSAGE_LIMIT = 4000

log = logging.getLogger("daegam_sandbox")


@dataclass
class Verdict:
    result: str
    reason: str = ""
    outcomes: list[Outcome] = field(default_factory=list)
    # 이 결과가 나오기까지 돈 바퀴 수. 첫 점검만 1 을 넘는다
    rounds: int = 1

    @property
    def passed(self) -> bool:
        return self.result == PASSED

    def message(self, check: str) -> str:
        """종료 메시지 한 줄(JSON). 보이려는 것이고 아무것도 정하지 않는다."""
        reached = [o.target.label() for o in self.outcomes if o.reached]
        skipped = [o.target.label() for o in self.outcomes if o.skipped]
        doc = {"check": check, "result": self.result, "reason": self.reason[:300]}
        doc["reached"], doc["skipped"] = reached, skipped
        if self.rounds > 1:
            doc["rounds"] = self.rounds
        text = json.dumps(doc, ensure_ascii=False)
        while len(text.encode()) > MESSAGE_LIMIT and (doc["reached"] or doc["skipped"]):
            key = "reached" if len(doc["reached"]) >= len(doc["skipped"]) else "skipped"
            doc[key] = doc[key][: len(doc[key]) // 2]
            doc["trimmed"] = True
            text = json.dumps(doc, ensure_ascii=False)
        return text


def uid_map_is_identity(text: str) -> bool:
    """uid_map 이 어느 uid 든 같은 uid 로 옮기면 참이다. 호스트의 `0 0 4294967295` 가 그렇다.
    비었거나 꼴이 틀리면 ValueError 다."""
    lines = [line.split() for line in text.splitlines() if line.strip()]
    if not lines:
        raise ValueError("uid_map 이 비었다")
    for fields in lines:
        if len(fields) != 3 or not all(f.isdigit() for f in fields):
            raise ValueError(f"uid_map 의 줄 꼴이 아니다: {' '.join(fields)}")
        inside, outside, count = (int(f) for f in fields)
        if count and inside == outside:
            return True
    return False


async def run_check(uid_map: Path, targets: tuple[Target, ...] | None, reason: str = "") -> Verdict:
    """점검 한 번. targets 가 None 이면(env 가 틀렸으면) reason 을 들고 error 다."""
    try:
        identity = uid_map_is_identity(uid_map.read_text(encoding="ascii"))
    except (OSError, ValueError, UnicodeDecodeError) as e:
        return Verdict(ERROR, f"uid_map: {e}")
    if identity:
        return Verdict(IDENTITY, "uid_map 이 uid 를 그대로 옮긴다(user namespace 없음)")
    if targets is None:
        return Verdict(ERROR, reason)
    outcomes = await probe_all(targets)
    if any(o.reached for o in outcomes):
        return Verdict(REACHED, "막혀야 할 대상에 닿았다", outcomes)
    if any(o.error for o in outcomes):
        return Verdict(ERROR, "점검 소켓을 만들지 못한 대상이 있다", outcomes)
    return Verdict(PASSED, "", outcomes)


async def run_first_check(
    uid_map: Path,
    targets: tuple[Target, ...] | None,
    reason: str = "",
    *,
    settle: float = FIRST_SETTLE,
    pause: float = SETTLE_PAUSE,
) -> Verdict:
    """첫 점검. 첫 바퀴가 대상에 닿지 않았으면 그 결과가 답이다(통과, uid map, 점검을 하지 못함
    모두 다시 돌지 않는다). 닿았으면 settle 초 안에서 pause 를 두고 다시 돌고, 대상이 하나도 닿지
    않은 바퀴가 `CLEAN_ROUNDS` 번 잇따라야 통과다. 한 번 닿은 뒤에는 그 뒤 바퀴가 점검을 하지
    못해도 결과가 86 으로 바뀌지 않는다. 시간이 다하면 마지막으로 닿은 바퀴의 결과(87)다.
    settle 이 0 이하면 한 바퀴뿐이다."""
    verdict = await run_check(uid_map, targets, reason)
    if verdict.result != REACHED or settle <= 0:
        return verdict
    deadline = time.monotonic() + settle
    reached, rounds, clean = verdict, 1, 0
    while time.monotonic() + pause < deadline:
        await asyncio.sleep(pause)
        rounds += 1
        verdict = await run_check(uid_map, targets, reason)
        if verdict.passed:
            clean += 1
            if clean >= CLEAN_ROUNDS:
                verdict.rounds = rounds
                return verdict
            continue
        clean = 0
        if verdict.result == REACHED:
            reached = verdict
    reached.rounds = rounds
    return reached


def exit_code(verdict: Verdict, *, first: bool) -> int | None:
    """실패한 점검의 끝 코드. 통과면 None 이다. 뒤 점검은 무엇으로 실패했든 88 이다."""
    if verdict.passed:
        return None
    if not first:
        return EXIT_LATER_FAILED
    return EXIT_COULD_NOT_RUN if verdict.result == ERROR else EXIT_FIRST_FAILED


def write_termination(path: Path, text: str) -> None:
    """종료 메시지를 쓴다. 쓰지 못해도 끝 코드는 그대로이므로 기록만 남긴다."""
    try:
        path.write_text(text + "\n", encoding="utf-8")
    except OSError as e:
        log.warning("termination message not written: %s", e)
