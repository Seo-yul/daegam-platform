"""턴 기록 규칙이다(P36 3절). 세션 제목과 턴 시도의 id·번호를 한 곳에서 정한다.

턴마다 런타임이 `turn_id`(uuid4 hex 32자)를 낸다. 피드백, 다시 생성, 인용이 이 값으로 턴을 가리킨다.
결정 재개는 새 턴이 아니라 같은 `turn_id` 와 번호를 이어 쓴다. 새 턴의 번호는 지금처럼 끝난 턴
수(`turns`)에 하나를 더한 값이고, 색인의 `last_turn` 은 오류·중지로 끝난 시도까지 센 가장 최근
시도의 번호다. 재개와 다시 생성은 이 가장 최근 시도의 번호를 쓴다.
시도의 두 칸은 리스를 잡은 직후 색인에 적는다. 파드가 턴 도중에 죽어도 다음 다시 생성이 그 시도의
번호를 읽게 하려는 것이다.

인용 번호(citations.py)는 세션 안에서 이어진다. 턴은 색인의 `cite_seq` 다음 번호부터 나눠 주고,
끝나면 오류나 중지로 끝났어도 마지막 번호를 적는다. 그 턴의 검색 결과도 체크포인트에 남아 다음
턴의 모델이 읽기 때문이다.

턴을 연 사람 메시지에는 id `u-<turn_id>` 를 단다. 체크포인트의 메시지에서 그 턴을 찾을 수 있게
하는 표식이다. 다시 생성은 바꾸는 턴의 번호를 그대로 쓰고 id 는 새로 내며, 버리는 턴의 id 는 입구
질문의 이 표식에서 읽는다(regenerate.py).

시도를 적는 같은 쓰기가 실행 표식(`RunMarker`)도 적는다(P79 3절). 리스 토큰, 시도의 id 와 번호,
시도의 종류(새 말, 재개, 다시 생성), 실행의 trace_id·span_id 와 시작 시각이다. 마감의 색인 쓰기가
같은 문장에서 지우므로, 끊긴 열린 표시와 토큰이 같은 표식이 남아 있으면 그 시도는 마감하지 못한
것이다. 되살리기(turn_repair.py)가 그 표식으로 `turn_end` 를 한 번 낸다.

제목은 LLM 을 부르지 않는다. 새 세션의 첫 메시지에서 글이 있는 첫 줄을 정리해 60자에서 자르고,
이름 바꾸기는 같은 정리를 거친 1~100자다. 정리는 공백이 아닌 제어·서식 문자(Cc·Cf)를 먼저 빼고,
공백을 한 칸으로 줄여 앞뒤를 뗀 뒤 NFC 로 맞추는 차례다. 빼는 것을 먼저 해야 공백 사이에 낀 문자가
빠진 자리에 공백 두 칸이 남지 않는다. 탭과 줄바꿈은 제어 문자이지만 공백이라 한 칸으로 줄어든다.
줄바꿈이나 방향을 뒤집는 문자가 목록 한 줄을 흐트러뜨리지 않게 하려는 것이다. 자동 제목의 표본은
`schema/fixtures/chat/titles.json` 이고 UI 의 같은 규칙(`ui/src/lib/sessionTitle.ts`)도 그 표본을
맞댄다. 제목 상한 셋은 플랫폼과 UI 도 같은 값을 두고 플랫폼 테스트가 맞대므로 이 모듈은 표준
라이브러리만 쓴다.
"""

from __future__ import annotations

import json
import unicodedata
import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from runtime.sessions import SessionRecord

SESSION_TITLE_MAX_CHARS = 100
"""이름 바꾸기가 받는 제목 길이의 상한이다. 검색어 `q` 의 상한도 같다."""
AUTO_TITLE_MAX_CHARS = 60
"""첫 메시지로 채우는 자동 제목의 길이다. 넘으면 잘라 말줄임표를 붙인다."""
SESSION_TITLE_RAW_MAX_CHARS = 1000
"""정리하기 전 제목 본문의 상한이다. 공백과 제어 문자를 빼면 100자 안에 드는 글까지 받는다."""
TITLE_ELLIPSIS = "…"
HUMAN_ID_PREFIX = "u-"
_DROPPED_CATEGORIES = frozenset({"Cc", "Cf"})


def new_turn_id() -> str:
    return uuid.uuid4().hex


def human_message_id(turn_id: str) -> str:
    """턴을 연 사람 메시지의 id 다."""
    return f"{HUMAN_ID_PREFIX}{turn_id}"


def turn_id_of(message_id: str | None) -> str | None:
    """사람 메시지 id 에서 그 턴의 id 를 읽는다. 표식이 없는 옛 세션의 메시지면 None 이다."""
    if message_id is None or not message_id.startswith(HUMAN_ID_PREFIX):
        return None
    return message_id[len(HUMAN_ID_PREFIX) :] or None


def _nfc(text: str) -> str:
    """저장하는 제목과 견주는 검색어를 같은 꼴(NFC)로 맞춘다."""
    return unicodedata.normalize("NFC", text)


def clean_title(text: str) -> str:
    """제목으로 쓸 글이다. 공백이 아닌 제어·서식 문자를 빼고, 공백을 한 칸으로 줄인 뒤 NFC 로
    맞춘다."""
    kept = "".join(ch for ch in text if ch.isspace() or not _dropped(ch))
    return _nfc(" ".join(kept.split()))


def _dropped(ch: str) -> bool:
    return unicodedata.category(ch) in _DROPPED_CATEGORIES


def title_from_message(message: str) -> str | None:
    """새 세션의 제목이다. 첫 메시지에서 글이 있는 첫 줄을 쓰고 60자를 넘으면 자른다."""
    for line in message.splitlines():
        title = clean_title(line)
        if title:
            if len(title) > AUTO_TITLE_MAX_CHARS:
                return title[:AUTO_TITLE_MAX_CHARS] + TITLE_ELLIPSIS
            return title
    return None


def checked_title(raw: str) -> str | None:
    """이름 바꾸기의 제목이다. 정리한 뒤 1~100자가 아니면 None 이다."""
    title = clean_title(raw)
    return title if 0 < len(title) <= SESSION_TITLE_MAX_CHARS else None


def search_key(text: str) -> str:
    """제목 검색이 견주는 글이다. NFC 로 맞추고 대소문자를 접는다."""
    return _nfc(text).casefold()


def like_pattern(q: str) -> str:
    """제목에 q 가 드는지 보는 ILIKE 무늬다(Postgres 색인). LIKE 메타문자(\\·%·_)는 글자 그대로
    견주게 앞에 역슬래시를 단다. 저장된 제목처럼 NFC 로 맞춘다."""
    escaped = _nfc(q).replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return f"%{escaped}%"


def title_matches(title: str | None, q: str | None) -> bool:
    """검색어 `q` 가 제목 안에 드는가. `q` 가 없으면 모두 든다."""
    if q is None:
        return True
    return title is not None and search_key(q) in search_key(title)


@dataclass(frozen=True)
class TurnAttempt:
    """턴 시도 하나의 번호와 id 다."""

    number: int
    turn_id: str

    @classmethod
    def next(cls, record: SessionRecord) -> TurnAttempt:
        """새 턴이다. 번호는 끝난 턴 수에 하나를 더하고 id 는 새로 낸다."""
        return cls(record.turns + 1, new_turn_id())

    @classmethod
    def resumed(cls, record: SessionRecord) -> TurnAttempt:
        """결정 재개다. 보류로 끝난 턴, 곧 가장 최근 시도의 번호와 id 를 이어 쓴다. interrupt 를
        내다 끊긴 턴은 보류로 남지만 끝난 턴 수에 들지 않으므로 번호는 다시 생성과 같은 규칙으로
        읽는다. id 가 없는 옛 세션이면 새로 낸다."""
        return cls(_latest_attempt_number(record), record.turn_id or new_turn_id())

    @classmethod
    def regenerated(cls, record: SessionRecord) -> TurnAttempt:
        """다시 생성이다. 바꾸는 턴, 곧 가장 최근 시도의 번호를 쓰고 id 는 새로 낸다."""
        return cls(_latest_attempt_number(record), new_turn_id())

    def index_fields(self, record: SessionRecord) -> dict[str, Any]:
        """색인에 적을 시도 칸이다. 색인이 이미 같은 값이면 비어 있다."""
        if record.turn_id == self.turn_id and record.last_turn == self.number:
            return {}
        return {"turn_id": self.turn_id, "last_turn": self.number}


# 실행 표식의 시도 종류다(P79 3절). 되살리기는 새 말의 시도만 사용자 항목을 다시 세운다
RUN_CHAT = "chat"
RUN_RESUME = "resume"
RUN_REGENERATE = "regenerate"


@dataclass(frozen=True)
class RunMarker:
    """시도 하나의 실행 표식이다(P79 3절). 시도 기록이 적고 마감이 지운다."""

    token: str
    turn_id: str
    turn: int
    kind: str
    trace_id: str
    span_id: str
    start: datetime

    def to_json(self) -> str:
        """Redis 해시 칸 `turn_run` 의 값이다. 시각은 isoformat 이다."""
        return json.dumps(
            {
                "token": self.token,
                "turn_id": self.turn_id,
                "turn": self.turn,
                "kind": self.kind,
                "trace_id": self.trace_id,
                "span_id": self.span_id,
                "start": self.start.isoformat(),
            }
        )

    @classmethod
    def from_json(cls, raw: str) -> RunMarker:
        data = json.loads(raw)
        return cls(
            token=data["token"],
            turn_id=data["turn_id"],
            turn=int(data["turn"]),
            kind=data["kind"],
            trace_id=data["trace_id"],
            span_id=data["span_id"],
            start=datetime.fromisoformat(data["start"]),
        )

    def end_fields(self) -> dict[str, Any]:
        """되살리기가 내는 `turn_end` 의 시도와 실행 칸이다. 시작 시각은 isoformat 이다."""
        return {
            "turn": self.turn,
            "turn_id": self.turn_id,
            "trace_id": self.trace_id,
            "span_id": self.span_id,
            "start": self.start.isoformat(timespec="milliseconds"),
        }


def cite_seq_fields(record: SessionRecord, seq: int) -> dict[str, Any]:
    """색인에 적을 인용 번호 칸이다. 이 턴이 번호를 나눠 주지 않았으면 비어 있다."""
    return {"cite_seq": seq} if seq != record.cite_seq else {}


def _latest_attempt_number(record: SessionRecord) -> int:
    """가장 최근 시도의 번호다. 시도 번호가 없는 옛 세션은 끝난 턴 수이고 둘 다 없으면 1 이다."""
    return max(record.last_turn, record.turns, 1)
