"""첨부 저장소와 모델에게 가는 모양이다(P36 9절, runtime/uploads.py·permissions.py).

저장소 세 구현(실제 Postgres·Redis 는 env 가 있을 때만)과 한 묶음을 받은 차례대로 읽는 것,
`UploadsBackend` 의 ls·read·glob·grep 과 쓰기 거절, `FilesystemPermission` 이 메인과 서브에이전트의
`write_file`·`edit_file`·`delete` 를 막는 것, sweep·세션 지우기가 첨부를 지우고 첫 턴의 다시
생성은 남기는 것, 닫을 때 지우지 못한 첨부를 기록을 지울 때 마저 지우는 것, `AttachmentImages`
가 요청에만 이미지를 붙이는 것, `ChatOpenAI`·`ChatAnthropic` 요청 본문(호출 없이), 이미지를 받지
못하는 폴백 모델의 요청에서 이미지가 자리표시로 바뀌는 것을 본다.
"""

from __future__ import annotations

import base64
import copy
import json
from collections.abc import Sequence
from dataclasses import replace
from datetime import timedelta
from types import SimpleNamespace
from typing import Any

import pytest
from langchain.agents.middleware.types import ModelRequest
from langchain_anthropic import ChatAnthropic
from langchain_core.messages import BaseMessage, HumanMessage
from langchain_openai import ChatOpenAI
from pydantic import PrivateAttr

from runtime import compile as runtime_compile
from runtime import uploads as uploads_module
from runtime.long_term import TurnContext
from runtime.sessions import utcnow
from runtime.stub import IMAGES_SEEN, StubChatModel
from runtime.uploads import (
    DAEGAM_KWARGS_KEY,
    StoredUpload,
    TurnUploads,
    UploadsBackend,
    filled_message,
    filled_request,
    ref_kwargs,
)
from tests.app_fixtures import regenerate, sid_of, tool_message, transcript_items, with_subagent
from tests.attachment_fixtures import TEXT, document, image, png, send, stored
from tests.conftest import events_named, run_app
from tests.store_fixtures import contract_store

# --- 저장소 ---------------------------------------------------------------------------------


@pytest.mark.parametrize("kind", ["memory", "postgres", "redis"])
async def test_stores_keep_bytes_and_meta_per_session(kind) -> None:
    """세 구현이 같은 모양이다. 이미지 바이트(UTF-8 이 아닌 것)도 그대로 돌아오고, 지우면 그
    세션의 것만 사라진다. 고정한 upload_id 와 sid 를 쓰므로 이 테스트만의 저장소에서 돈다."""
    first = [
        stored("u1", format="pdf", pages=3, truncated=True),
        stored("u2", kind="image", name="화면.png", media_type="image/png"),
    ]
    second = [stored("u3", name="다른.pdf", sheets=2)]
    async with contract_store(kind) as store:
        await _round_trip(store.uploads, first, second)


@pytest.mark.parametrize("kind", ["memory", "postgres", "redis"])
async def test_a_batch_reads_back_in_the_order_it_was_received(kind) -> None:
    """한 묶음은 같은 시각이라 저장소가 받은 차례(seq)로 정렬한다. upload_id 는 난수라 차례가
    아니다."""
    now = utcnow()
    batch = [
        replace(stored(upload_id), created_at=now, seq=seq)
        for seq, upload_id in enumerate(["u3", "u1", "u2"])
    ]
    async with contract_store(kind) as store:
        await _round_trip(store.uploads, batch, [])


async def _round_trip(store: Any, first: list[StoredUpload], second: list[StoredUpload]) -> None:
    await store.add("s1", first)
    await store.add("s2", second)
    assert await store.read("s1") == first
    assert await store.read("s2") == second
    await store.delete("s1")
    assert await store.read("s1") == []
    assert await store.read("s2") == second
    await store.delete("s2")
    await store.delete("s2")


# --- /uploads/ 백엔드 -----------------------------------------------------------------------


@pytest.fixture
def backend(monkeypatch) -> UploadsBackend:
    """턴 문맥에 글 첨부 둘과 이미지 하나가 든 백엔드다. 이미지는 파일로 보이지 않는다."""
    files = [
        stored("u1"),
        stored("u2", name="메모.md", path="/uploads/메모.md", format="markdown"),
        stored("u3", kind="image", name="화면.png", media_type="image/png"),
    ]
    context = TurnContext(owner_sub="alice", uploads=TurnUploads(files=files))
    monkeypatch.setattr(uploads_module, "get_runtime", lambda: SimpleNamespace(context=context))
    return UploadsBackend()


async def test_backend_lists_reads_globs_and_greps_text_attachments(backend) -> None:
    listed = await backend.als("/")
    assert [e["path"] for e in listed.entries] == ["/메모.md", "/보고서.pdf.txt"]
    assert listed.entries[1]["size"] == len(TEXT.encode("utf-8"))
    read = await backend.aread("/보고서.pdf.txt", offset=1, limit=1)
    assert read.file_data["content"].rstrip("\n") == "환불은 7일 안에 한다"
    assert (read.start_line, read.end_line, read.total_lines) == (2, 2, 3)
    assert (await backend.aread("/화면.png")).error, "이미지는 파일이 아니다"
    globbed = await backend.aglob("*.txt")
    assert [m["path"] for m in globbed.matches] == ["/보고서.pdf.txt"]
    assert (await backend.aglob("*.csv")).matches == []
    grepped = await backend.agrep("7일")
    assert {(m["path"], m["line"]) for m in grepped.matches} == {
        ("/보고서.pdf.txt", 2),
        ("/메모.md", 2),
    }


async def test_backend_refuses_every_write(backend) -> None:
    assert "read-only" in (await backend.awrite("/새.txt", "x")).error
    assert "read-only" in (await backend.aedit("/보고서.pdf.txt", "7일", "8일")).error
    assert "read-only" in (await backend.adelete("/보고서.pdf.txt")).error
    assert backend.upload_files([("/a.txt", b"x")])[0].error == "permission_denied"
    assert (await backend.aread("/보고서.pdf.txt")).file_data["content"] == TEXT


# --- 파일 도구와 쓰기 막기 ------------------------------------------------------------------


async def test_the_agent_reads_uploads_and_cannot_write_them(files_doc) -> None:
    """스텁이 read_file 로 첨부 글을 읽고, write_file·edit_file·delete 는 도구 자리에서 막힌다."""
    path = "/uploads/보고서.pdf.txt"
    async with run_app(files_doc) as running:
        _, sse = await send(
            running, [document(text=TEXT)], tool_message("read_file", file_path=path)
        )
        result = events_named(sse, "tool_result")[0]
        assert result["status"] == "success" and "환불은 7일 안에 한다" in result["content"]
        sid = sid_of(sse)
        for message in (
            tool_message("write_file", file_path=path, content="바꾼 글"),
            tool_message("edit_file", file_path=path, old_string="7일", new_string="8일"),
            tool_message("delete", file_path=path),
        ):
            _, sse = await send(running, [], message, session_id=sid)
            result = events_named(sse, "tool_result")[0]
            assert result["status"] == "error"
            assert "permission denied for write" in result["content"], result
        stored_now = await running.runtime.store.uploads.read(sid)
        assert stored_now[0].text == TEXT


async def test_subagents_inherit_the_write_block(files_doc) -> None:
    """서브에이전트도 메인과 같은 규칙을 받는다. 메인이 task 로 맡긴 쓰기가 서브에이전트의 도구
    자리에서 막힌다."""
    with_subagent(files_doc)
    inner = tool_message("write_file", file_path="/uploads/보고서.pdf.txt", content="x")
    outer = tool_message("task", description=inner, subagent_type="calc")
    async with run_app(files_doc) as running:
        _, sse = await send(running, [document()], outer)
        done = events_named(sse, "done")[0]
        assert "permission denied for write on /uploads/보고서.pdf.txt" in done["text"]


# --- 지우기 ---------------------------------------------------------------------------------


async def test_closing_and_deleting_a_session_remove_its_attachments(plain_doc) -> None:
    """세션이 닫히면 체크포인트와 함께 첨부가 지워지고 전사의 이름·크기는 남는다. 첫 턴의 다시
    생성은 스레드를 지워도 첨부를 남겨 다시 실은 참조가 그대로 읽힌다."""
    async with run_app(plain_doc) as running:
        store = running.runtime.store
        _, sse = await send(running, [image()])
        first = sid_of(sse)
        _, again = await regenerate(running, first)
        done = events_named(again, "done")[0]
        assert done["text"].endswith(IMAGES_SEEN.format(count=1))
        assert len(await store.uploads.read(first)) == 1
        assert await store.close_expired(utcnow() + timedelta(days=1)) == [first]
        assert await store.uploads.read(first) == []
        items = await transcript_items(running, first)
        assert items[0]["attachments"][0]["name"] == "화면.png"

        _, sse = await send(running, [document()])
        second = sid_of(sse)
        response = await running.client.delete(f"/sessions/{second}", params={"user_sub": "alice"})
        assert response.status_code == 200
        assert await store.uploads.read(second) == []


async def test_attachments_a_failed_close_left_go_with_the_records(plain_doc, monkeypatch) -> None:
    """닫을 때 첨부를 지우지 못해도 보존 기간이 지나 기록을 지울 때 마저 지운다. 닫힌 세션은 다시
    닫히지 않으므로 그 자리가 아니면 첨부가 남는다."""
    async with run_app(plain_doc) as running:
        store = running.runtime.store
        _, sse = await send(running, [document()])
        sid = sid_of(sse)
        delete = store.uploads.delete

        async def broken(session_id: str) -> None:
            raise ConnectionError(f"{session_id} 첨부를 지우지 못했다")

        monkeypatch.setattr(store.uploads, "delete", broken)
        later = utcnow() + timedelta(days=1)
        assert await store.close_expired(later) == [sid]
        assert len(await store.uploads.read(sid)) == 1
        monkeypatch.setattr(store.uploads, "delete", delete)
        assert await store.purge_records(later) == [sid]
        assert await store.uploads.read(sid) == []


# --- 모델에게 가는 모양 ---------------------------------------------------------------------


def _image_message() -> tuple[HumanMessage, TurnUploads]:
    picture = stored("u9", kind="image", name="화면.png", media_type="image/png")
    message = HumanMessage("이 화면을 봐 줘", additional_kwargs=ref_kwargs(["u9"]))
    return message, TurnUploads(files=[picture])


def test_the_request_gets_the_image_and_loses_the_reference() -> None:
    message, uploads = _image_message()
    filled = filled_message(message, uploads)
    assert filled.content[0] == {"type": "text", "text": "이 화면을 봐 줘"}
    assert filled.content[1] == {
        "type": "image",
        "base64": base64.b64encode(png()).decode(),
        "mime_type": "image/png",
    }
    assert DAEGAM_KWARGS_KEY not in filled.additional_kwargs
    assert message.content == "이 화면을 봐 줘", "원래 메시지는 그대로다"
    plain = HumanMessage("첨부 없음")
    assert filled_message(plain, uploads) is plain
    request = ModelRequest(
        model=StubChatModel(),
        messages=[plain],
        runtime=SimpleNamespace(context=TurnContext(owner_sub="a", uploads=uploads)),
    )
    assert filled_request(request) is request


def test_provider_request_bodies_carry_the_image_without_the_reference() -> None:
    """두 provider 의 요청 본문을 호출 없이 만든다. 표준 image 블록이 각자의 꼴로 바뀌고
    `additional_kwargs.daegam` 은 실리지 않는다."""
    message, uploads = _image_message()
    filled = filled_message(message, uploads)
    data = base64.b64encode(png()).decode()

    openai = ChatOpenAI(model="gpt-4.1", api_key="sk-test")._get_request_payload([filled])
    parts = openai["messages"][0]["content"]
    assert {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{data}"}} in parts
    assert DAEGAM_KWARGS_KEY not in json.dumps(openai)

    anthropic = ChatAnthropic(model="claude-sonnet-4-5", api_key="sk-test")._get_request_payload(
        [filled]
    )
    blocks = anthropic["messages"][0]["content"]
    source = next(b for b in blocks if b["type"] == "image")["source"]
    assert source == {"type": "base64", "media_type": "image/png", "data": data}
    assert DAEGAM_KWARGS_KEY not in json.dumps(anthropic)


class RecordingModel(StubChatModel):
    """받은 요청의 메시지를 적어 두는 스텁이다."""

    _seen: list[list[BaseMessage]] = PrivateAttr(default_factory=list)

    @property
    def seen(self) -> list[list[BaseMessage]]:
        return self._seen

    async def _agenerate(self, messages: Any, stop: Any = None, run_manager: Any = None, **kw):
        self._seen.append(list(messages))
        return await super()._agenerate(messages, stop, run_manager, **kw)


def _blocks(messages: Sequence[BaseMessage], kind: str) -> list[dict[str, Any]]:
    human = next(m for m in messages if isinstance(m, HumanMessage))
    content = human.content if isinstance(human.content, list) else []
    return [b for b in content if isinstance(b, dict) and b.get("type") == kind]


async def test_a_fallback_that_cannot_see_images_gets_a_placeholder(plain_doc, monkeypatch) -> None:
    """이미지 채우기는 폴백·재시도의 안쪽이라 시도마다 채우고, 그 안쪽의 deepagents
    UnsupportedContentMiddleware 가 실제로 부르는 폴백 모델의 프로필로 자리표시를 넣는다."""
    fallback_spec = copy.deepcopy(plain_doc["main"]["model"])
    fallback_spec["name"] = "stub-fallback"
    plain_doc["main"]["fallback"] = fallback_spec
    plain_doc["main"]["limits"]["retries"] = 0
    main = RecordingModel(fail_first=10)
    fallback = RecordingModel(
        model_name="stub-fallback", profile={"max_input_tokens": 200_000, "image_inputs": False}
    )
    build = runtime_compile.build_chat_model

    def build_chat_model(spec: dict[str, Any], **kwargs: Any):
        return fallback if spec.get("name") == "stub-fallback" else build(spec, **kwargs)

    monkeypatch.setattr(runtime_compile, "build_chat_model", build_chat_model)
    async with run_app(plain_doc, model=main) as running:
        response, sse = await send(running, [image()])
        assert response.status_code == 200, response.text
        assert events_named(sse, "model_fallback")
        done = events_named(sse, "done")[0]
    assert len(_blocks(main.seen[0], "image")) == 1, "메인 모델의 시도에는 이미지가 있다"
    assert _blocks(fallback.seen[0], "image") == []
    placeholder = _blocks(fallback.seen[0], "text")[-1]["text"]
    assert "does not support image content (image/png)" in placeholder
    assert IMAGES_SEEN.format(count=1) not in done["text"]
