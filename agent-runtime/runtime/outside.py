"""바깥 내용의 감싸개와 세션의 바깥 표시다(P43 2·3절, D96).

메일, 웹 페이지, 문서처럼 대화 밖에서 온 글이 Tool 결과로 모델에게 갈 때 쓰는 조각이다. 어느 결과가
바깥 내용인지와 그 결과를 감싸는 미들웨어는 tool_safety.py 에 있다.

- **감싸개.** 바깥 글은 `<outside_content source="<tool>">…</outside_content>` 에 든다. 넣기 전에
  보이지 않는 글자를 빼고 감싸개 꼬리표를 가린다(`escape_tags`). HTML 엔티티까지 푸는 것은 웹
  글뿐이다(`escape_untrusted`). MCP 글의 `&amp;` 는 그대로 모델에게 간다. 두 함수는 fetch 서버와
  플랫폼의 `urlfetch.py` 에 있는 같은 이름의 함수와 같은 규칙이다. 웹 Tool 의 글은 서버와 인용
  단계가 `<web_content>` 로 감싸므로(web_citations.py) 여기서 다시 감싸지 않는다. 결과의 artifact
  에는 `untrusted` 로 종류(`outside`, 웹은 `web`)를 단다. 스트림의 `tool_result` 가 그 종류를
  싣는다(citations.py).
- **세션의 표시(`OutsideFlag`).** 턴 문맥에 실려 메인과 서브에이전트가 같은 것을 본다. `flagged` 는
  이 대화가 바깥 내용을 가졌는가이고 턴을 열 때 세션 색인의 `outside_at`, 첨부, 기록에서 다시
  정한다(turn.py). 바깥 결과를 처음 받으면 색인에 `outside_at` 을 한 번 쓰고(`record`), 쓰지 못하면
  예외라 바깥 감싸개가 내용 대신 Tool 오류를 돌려준다. 기본값은 표시된 쪽이고 쓰는 수단도 없다. 턴을
  열지 않고 만든 문맥은 바깥 내용을 가진 대화로 보고 바깥 결과를 내보내지 않는다(fail closed).
- **task 의 몫(`TaskReads`).** 메인의 바깥 감싸개가 task 호출마다 하나를 문맥 변수에 두고, 그 호출
  안의 서브에이전트 바깥 감싸개가 바깥 결과를 받으면 표시한다. 서브에이전트는 task 호출 안에서 같은
  asyncio 문맥으로 돌기 때문이다(F0 3번). 병렬 task 호출은 ToolNode 가 태스크마다 문맥을 복사하므로
  섞이지 않는다.

턴 문맥(long_term.py)이 이것을 싣기 때문에 tool_safety.py 와 long_term.py 를 가져오지 않는다.
"""

from __future__ import annotations

import asyncio
import functools
import html
import re
from collections.abc import Awaitable, Callable, Mapping
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import Any

from langchain_core.messages import ToolMessage
from langgraph.types import Command

from runtime.citations import UNTRUSTED_KEY, UNTRUSTED_OUTSIDE

OUTSIDE_TAG = "outside_content"
WEB_CONTENT_TAG = "web_content"
SUBAGENT_OUTSIDE_KEY = "subagent_outside"
"""메인의 task 결과 artifact 에서 그 서브에이전트가 이 호출 안에 바깥 내용을 받았는가다. 새 판의
task 결과에는 늘 있다. 없는 결과는 옛 판이 쓴 것이다(tool_safety.py 의 기록 훑기)."""

# 꼬리표를 숨기는 보이지 않는 글자다. 플랫폼 urlfetch.py 의 `_INVISIBLE` 과 같은 표다
_INVISIBLE = re.compile(
    r"[\x00-\x08\x0e-\x1b\x7f-\x84\x86-\x9f\u00ad\u034f\u061c\u115f\u1160\u17b4\u17b5"
    r"\u180b-\u180f\u200b-\u200f\u202a-\u202e\u2060-\u206f\u3164\ufe00-\ufe0f\ufeff\uffa0"
    r"\ufff0-\ufff8\U0001bca0-\U0001bca3\U0001d173-\U0001d17a\U000e0000-\U000e0fff]"
)


@functools.lru_cache(maxsize=8)
def _wrapper_tag(tag: str) -> re.Pattern[str]:
    """여는 꼬리표와 닫는 꼬리표(`<` 와 `/` 뒤의 공백 포함)를 대소문자와 상관없이 찾는다."""
    return re.compile(rf"<(\s*/?\s*{re.escape(tag)})", re.IGNORECASE)


def escape_tags(text: str, tag: str = OUTSIDE_TAG) -> str:
    """보이지 않는 글자를 빼고 감싸개 꼬리표만 `&lt;` 로 바꾼다. 엔티티는 글의 일부라 풀지
    않는다."""
    return _wrapper_tag(tag).sub(lambda m: "&lt;" + m.group(1), _INVISIBLE.sub("", text))


@functools.lru_cache(maxsize=8)
def _dangling_tag(tag: str) -> re.Pattern[str]:
    """글 끝에 걸린, 아직 덜 쓴 꼬리표의 `<` 를 찾는다. 모델은 블록을 이어 붙여 읽으므로 다음 블록이
    나머지를 이으면 꼬리표가 된다."""
    rest = ""
    for ch in reversed(tag):
        rest = f"(?:{re.escape(ch)}{rest})?"
    return re.compile(rf"<(?=\s*/?\s*{rest}\Z)", re.IGNORECASE)


def _escape_block(text: str) -> str:
    """블록 목록에 든 글 하나를 가린다. `escape_tags` 에 더해 블록 끝에 걸린 꼬리표의 앞부분도
    가린다."""
    return _dangling_tag(OUTSIDE_TAG).sub("&lt;", escape_tags(text))


def escape_untrusted(text: str, tag: str = WEB_CONTENT_TAG) -> str:
    """웹에서 온 글을 감싸개 안에 넣을 수 있게 만든다. HTML 엔티티를 먼저 풀어 `&lt;/web_content`
    가 모델에게 꼬리표로 읽히지 않게 한 뒤 `escape_tags` 다."""
    return escape_tags(html.unescape(text), tag)


def _opening(source: str) -> str:
    return f'<{OUTSIDE_TAG} source="{html.escape(source, quote=True)}">'


def wrap_text(text: str, source: str) -> str:
    """바깥 글 하나를 감싼다. source 는 그 결과를 낸 Tool 이름이다."""
    return f"{_opening(source)}\n{escape_tags(text)}\n</{OUTSIDE_TAG}>"


def _wrap_content(content: Any, source: str) -> Any:
    """ToolMessage 의 content 를 감싼다. 글이면 글 하나이고, 블록 목록이면 글 블록을 가리고 앞뒤에
    꼬리표 블록을 둔다(이미지 같은 다른 블록은 그대로다). 글 블록은 블록 끝에 걸친 꼬리표도 가린다
    (`_escape_block`). 메일 한 통이 `</outside` 로 끝나고 다음 통이 `_content>` 로 시작하면 이어
    읽은 글이 감싸개를 닫기 때문이다."""
    if isinstance(content, str):
        return wrap_text(content, source)
    if not isinstance(content, list):
        return content
    blocks: list[Any] = [{"type": "text", "text": f"{_opening(source)}\n"}]
    for block in content:
        if isinstance(block, str):
            blocks.append(_escape_block(block))
        elif isinstance(block, Mapping) and block.get("type") == "text":
            text = str(block.get("text") or "")
            blocks.append({**block, "text": _escape_block(text)})
        else:
            blocks.append(block)
    blocks.append({"type": "text", "text": f"\n</{OUTSIDE_TAG}>"})
    return blocks


def with_marks(artifact: Any, marks: Mapping[str, Any]) -> dict[str, Any]:
    """artifact 에 표시를 더한 것이다. dict 꼴이 아닌 artifact 는 표시로 바꾼다. 모델은 artifact 를
    보지 않고, 표시가 있어야 화면의 배지와 기록 훑기가 바깥 내용을 알아본다. 웹 Tool 의 인용 단계와
    바깥 표시 층이 같이 쓴다."""
    base = dict(artifact) if isinstance(artifact, Mapping) else {}
    return {**base, **marks}


def _mark_message(
    message: ToolMessage, source: str, *, wrap: bool, marks: Mapping[str, Any]
) -> ToolMessage:
    """ToolMessage 하나에 바깥 표시를 단다. wrap 이면 글도 감싼다."""
    update: dict[str, Any] = {"artifact": with_marks(message.artifact, marks)}
    if wrap:
        update["content"] = _wrap_content(message.content, source)
    return message.model_copy(update=update)


def _command_messages(result: Any) -> list[Any] | None:
    """Command 결과(task 와 상태를 고치는 Tool)의 메시지 목록이다. 아니면 None 이다."""
    if not isinstance(result, Command) or not isinstance(result.update, Mapping):
        return None
    messages = result.update.get("messages")
    return messages if isinstance(messages, list) else None


def call_message(result: ToolMessage | Command[Any], call_id: str) -> ToolMessage | None:
    """Tool 결과에서 그 호출의 ToolMessage 다. Command 면 그 안의 같은 호출 id 메시지이고 없으면
    None 이다."""
    if isinstance(result, ToolMessage):
        return result
    for message in _command_messages(result) or ():
        if isinstance(message, ToolMessage) and message.tool_call_id == call_id:
            return message
    return None


def map_tool_message(
    result: ToolMessage | Command[Any],
    call_id: str,
    change: Callable[[ToolMessage], ToolMessage],
) -> ToolMessage | Command[Any]:
    """Tool 결과의 그 호출 ToolMessage 를 change 로 바꾼다. Command 면 그 안의 메시지를 바꾼
    Command 다. 바깥 표시 층과 판정 모델 층(judge.py)이 같이 쓴다."""
    if isinstance(result, ToolMessage):
        return change(result)
    messages = _command_messages(result)
    if messages is None:
        return result
    changed = [
        change(m) if isinstance(m, ToolMessage) and m.tool_call_id == call_id else m
        for m in messages
    ]
    return Command(
        graph=result.graph,
        update={**result.update, "messages": changed},
        resume=result.resume,
        goto=result.goto,
    )


def mark_result(
    result: ToolMessage | Command[Any],
    call_id: str,
    source: str,
    *,
    wrap: bool = True,
    marks: Mapping[str, Any] | None = None,
) -> ToolMessage | Command[Any]:
    """Tool 결과에 바깥 표시를 단다. Command 면(task 와 상태를 고치는 Tool) 그 호출의 ToolMessage 를
    고친다. marks 를 주지 않으면 `untrusted: outside` 다."""
    if marks is None:
        marks = {UNTRUSTED_KEY: UNTRUSTED_OUTSIDE}
    return map_tool_message(
        result, call_id, lambda m: _mark_message(m, source, wrap=wrap, marks=marks)
    )


class OutsideMarkUnavailable(RuntimeError):
    """세션의 바깥 표시를 쓸 수단이 없다. 턴을 열지 않고 만든 문맥이다."""


@dataclass
class OutsideFlag:
    """이 대화가 바깥 내용을 가졌는가와 그것을 색인에 쓰는 수단이다. 턴 문맥에 실린다.

    flagged 는 판정(tool_safety.py)이 읽고, 바깥 결과를 받으면 곧바로 참이 되어 같은 턴의 다음
    판정이 본다. stored 는 색인에 `outside_at` 이 이미 있는가다. write 는 그것을 쓰는 함수이고 턴을
    열 때 turn.py 가 리스의 토큰으로 묶어 넣는다. 기본값은 표시된 쪽이다(모듈 머리)."""

    flagged: bool = True
    stored: bool = False
    write: Callable[[], Awaitable[None]] | None = None
    _lock: asyncio.Lock = field(default_factory=asyncio.Lock, repr=False, compare=False)

    async def record(self) -> None:
        """바깥 결과 하나를 받았다. 턴의 표시를 세우고 색인의 표시를 한 번 쓴다. 쓰지 못하면 예외다.
        한꺼번에 받은 결과들이 함께 쓰지 않게 잠근다."""
        self.flagged = True
        if self.stored:
            return
        if self.write is None:
            raise OutsideMarkUnavailable("no index writer for the outside mark")
        async with self._lock:
            if self.stored:
                return
            await self.write()
            self.stored = True


@dataclass
class TaskReads:
    """task 호출 하나 안에서 서브에이전트가 바깥 결과를 받았는가다(모듈 머리)."""

    read: bool = False


_TASK_READS: ContextVar[TaskReads | None] = ContextVar("daegam_task_reads", default=None)


def open_task_reads() -> tuple[TaskReads, Any]:
    """task 호출 하나의 몫을 문맥 변수에 둔다. (몫, 되돌릴 토큰)이다."""
    reads = TaskReads()
    return reads, _TASK_READS.set(reads)


def close_task_reads(token: Any) -> None:
    _TASK_READS.reset(token)


def note_task_read() -> None:
    """이 문맥을 감싼 task 호출이 있으면 그 호출의 서브에이전트가 바깥 결과를 받았다고 적는다."""
    reads = _TASK_READS.get()
    if reads is not None:
        reads.read = True
