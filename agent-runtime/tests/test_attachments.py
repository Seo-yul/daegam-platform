"""`/chat` 의 첨부다(P36 9절, runtime/attachments.py·image_header.py).

종류·수·크기·base64·매직 바이트·가로세로·이름 규칙, 파일 도구 꺼짐과 이미지를 막은 프로필의 422,
`upload_bytes` 가 호출자가 적은 값이 아니라 저장한 바이트인 것, 세션 합과 이미지 수, 본문 24MiB,
첨부 글에 `max_input_chars`·`max_length` 가 걸리지 않고 `deny_pattern`·`pii_mask` 막기는 걸리는
것, 다시 생성과 고쳐 보내기가 다시 싣는 첨부 글도 가드레일로 검사하는 것, 한 묶음이 받은 차례대로
읽히는 것, `done.attachments_received`, 사람 메시지의 안내와 참조, 전사의 사용자 항목, 고쳐 보내기,
스텁의 블록 읽기를 본다. 저장소와 `/uploads/` 백엔드, 이미지 채우기는 test_uploads.py 가 본다.
"""

from __future__ import annotations

import hashlib
import json
import struct
from typing import Any

import pytest

from runtime import main as runtime_main
from runtime.attachments import (
    MAX_IMAGE_BYTES,
    MAX_NAME_CHARS,
    MAX_SESSION_IMAGES,
    NOTICE_MARK,
    attachment_name,
    notice_of,
    typed_text,
)
from runtime.image_header import ImageHeader, read_header
from runtime.stub import IMAGES_SEEN, StubChatModel
from runtime.uploads import UPLOADS_ROUTE, message_refs
from tests.app_fixtures import (
    checkpoint_humans,
    regenerate,
    sid_of,
    transcript_items,
    with_input_deny,
)
from tests.attachment_fixtures import ATTACHMENTS_FIXTURE, document, image, png, send
from tests.conftest import events_named, run_app
from tests.store_fixtures import pod_store

CARD = "4111 1111 1111 1111"


# --- 이미지 헤더 표본 -------------------------------------------------------------------------


def jpeg(width: int = 16, height: int = 9) -> bytes:
    app0 = b"\xff\xe0" + struct.pack(">H", 16) + b"JFIF\0\x01\x01\0\0\x01\0\x01\0\0"
    sof = b"\xff\xc0" + struct.pack(">HBHHB", 17, 8, height, width, 3) + b"\0" * 9
    return b"\xff\xd8" + app0 + b"\xff\xff" + sof + b"\xff\xd9"


def gif(width: int = 16, height: int = 9) -> bytes:
    return b"GIF89a" + struct.pack("<HH", width, height) + b"\0" * 7


def _riff(chunk: bytes, body: bytes) -> bytes:
    payload = chunk + struct.pack("<I", len(body)) + body
    return b"RIFF" + struct.pack("<I", len(payload) + 4) + b"WEBP" + payload


def webp_lossy(width: int = 16, height: int = 9) -> bytes:
    return _riff(b"VP8 ", b"\0\0\0\x9d\x01\x2a" + struct.pack("<HH", width, height) + b"\0" * 4)


def webp_lossless(width: int = 16, height: int = 9) -> bytes:
    bits = (width - 1) | ((height - 1) << 14)
    return _riff(b"VP8L", b"\x2f" + struct.pack("<I", bits))


def webp_extended(width: int = 16, height: int = 9) -> bytes:
    sides = (width - 1).to_bytes(3, "little") + (height - 1).to_bytes(3, "little")
    return _riff(b"VP8X", b"\0" * 4 + sides)


# --- 헤더와 이름 -----------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("data", "expected"),
    [
        (png(800, 600), ImageHeader("png", 800, 600)),
        (jpeg(1568, 1176), ImageHeader("jpeg", 1568, 1176)),
        (gif(3, 4), ImageHeader("gif", 3, 4)),
        (webp_lossy(320, 240), ImageHeader("webp", 320, 240)),
        (webp_lossless(5, 7), ImageHeader("webp", 5, 7)),
        (webp_extended(9000, 2), ImageHeader("webp", 9000, 2)),
    ],
)
def test_headers_give_the_format_and_size(data: bytes, expected: ImageHeader) -> None:
    assert read_header(data) == expected


@pytest.mark.parametrize(
    "data",
    [b"", b"hello", png()[:20], jpeg()[:12], b"RIFF\0\0\0\0WEBPVP8L", b"\xff\xd8\xff\x00junk"],
)
def test_broken_or_unknown_headers_are_not_images(data: bytes) -> None:
    assert read_header(data) is None


def test_names_drop_path_parts_and_control_characters() -> None:
    assert attachment_name("../../etc/passwd") == "passwd"
    assert attachment_name("C:\\Users\\me\\보고서.pdf") == "보고서.pdf"
    assert attachment_name("  분기\u200b\n 보고.pdf ") == "분기 보고.pdf"
    long = attachment_name("가" * 200 + ".xlsx")
    assert len(long) == MAX_NAME_CHARS and long.endswith(".xlsx")


# --- 받아 저장하기 ---------------------------------------------------------------------------


async def test_a_turn_stores_text_and_images_and_tells_the_model(plain_doc) -> None:
    """글은 `/uploads/<이름>.txt` 로, 이미지는 자리표시 줄로 안내되고 참조는 upload_id 만이다.
    upload_bytes 는 저장한 바이트(글의 UTF-8, 이미지는 푼 바이트)다. 스텁은 요청에 채워진 image
    블록을 센다."""
    text = "한글 문서" * 10
    raw = png(1568, 1176)
    attachments = [document(text=text, pages=12, sha256="0" * 64), image(data=raw)]
    async with run_app(plain_doc) as running:
        response, sse = await send(running, attachments)
        assert response.status_code == 200, response.text
        done = events_named(sse, "done")[0]
        assert done["attachments_received"] == 2
        assert done["text"].endswith(IMAGES_SEEN.format(count=1))
        sid = sid_of(sse)
        record = await running.runtime.store.index.get(sid)
        assert record.upload_bytes == len(text.encode("utf-8")) + len(raw)
        human = (await checkpoint_humans(running, sid))[-1]
        assert isinstance(human.content, str), "체크포인트에는 이미지가 없다"
        assert typed_text(human) == "첨부를 봐 줘"
        assert notice_of(human) == NOTICE_MARK + "\n".join(
            [
                f"- 보고서.pdf: {UPLOADS_ROUTE}보고서.pdf.txt "
                f"(text extracted from PDF, 12 pages, {len(text)} characters)",
                "- 화면.png: image attached",
                "Read the files with read_file. Long files take offset and limit.",
            ]
        )
        stored = await running.runtime.store.uploads.read(sid)
        assert message_refs(human) == [u.upload_id for u in stored]
        assert stored[0].sha256 == "0" * 64
        assert stored[1].sha256 == hashlib.sha256(raw).hexdigest()
        detail = (await running.client.get(f"/sessions/{sid}")).json()
        user = detail["transcript"]["items"][0]
        assert user == {
            "kind": "user",
            "text": "첨부를 봐 줘",
            "attachments": [
                {"name": "보고서.pdf", "kind": "text", "bytes": len(text.encode()), "pages": 12},
                {"name": "화면.png", "kind": "image", "bytes": len(raw)},
            ],
            "turn_id": done["turn_id"],
        }
        # 표본(UI 의 되살리기와 목이 읽는다)의 칸 안에 든다
        sample = ATTACHMENTS_FIXTURE["transcript"][0]["attachments"]
        assert {key for item in user["attachments"] for key in item} <= {
            key for item in sample for key in item
        }
        assert done.keys() == ATTACHMENTS_FIXTURE["events"]["done"].keys()
        start = running.observed("turn_start")[0]
        assert start["attachments"] == [
            {"kind": "text", "bytes": len(text.encode()), "sha256": "0" * 64},
            {"kind": "image", "bytes": len(raw), "sha256": stored[1].sha256},
        ], "이름은 record_bodies 일 때만 싣는다"


async def test_turns_without_attachments_report_zero(plain_doc) -> None:
    async with run_app(plain_doc) as running:
        response, sse = await send(running, [])
        assert events_named(sse, "done")[0]["attachments_received"] == 0
        record = await running.runtime.store.index.get(sid_of(sse))
        assert record.upload_bytes == 0


async def test_names_that_overlap_in_the_session_get_a_number(plain_doc) -> None:
    async with run_app(plain_doc) as running:
        _, sse = await send(running, [document(), document(), document("메모.md", format="text")])
        sid = sid_of(sse)
        _, again = await send(running, [document(), image("메모.md.txt")], session_id=sid)
        assert again[-1][0] == "done"
        stored = await running.runtime.store.uploads.read(sid)
        assert [(u.name, u.path) for u in stored] == [
            ("보고서.pdf", "/uploads/보고서.pdf.txt"),
            ("보고서 (2).pdf", "/uploads/보고서 (2).pdf.txt"),
            ("메모.md", "/uploads/메모.md"),
            ("보고서 (3).pdf", "/uploads/보고서 (3).pdf.txt"),
            ("메모.md.txt", "/uploads/메모.md.txt"),
        ]
        # 한 묶음은 같은 시각이고 받은 차례(seq)가 저장소의 읽는 차례를 정한다
        assert len({u.created_at for u in stored[:3]}) == 1
        assert [u.seq for u in stored] == [0, 1, 2, 0, 1]


async def test_a_numbered_name_stays_within_the_limit(plain_doc) -> None:
    """겹치는 이름에 ` (2)` 를 붙여도 120자 안이고 확장자는 남는다."""
    name = "가" * (MAX_NAME_CHARS - len(".pdf")) + ".pdf"
    async with run_app(plain_doc) as running:
        _, sse = await send(running, [document(name), document(name)])
        stored = await running.runtime.store.uploads.read(sid_of(sse))
        numbered = "가" * (MAX_NAME_CHARS - len(" (2).pdf")) + " (2).pdf"
        assert [u.name for u in stored] == [name, numbered]
        assert len(numbered) == MAX_NAME_CHARS


async def test_an_edited_question_keeps_the_attachments(plain_doc) -> None:
    """고쳐 보내면 친 글만 바뀌고 안내와 참조는 그대로다. 전사의 사용자 항목도 첨부를 든다."""
    async with run_app(plain_doc) as running:
        _, sse = await send(running, [document(), image()])
        sid = sid_of(sse)
        before = (await checkpoint_humans(running, sid))[-1]
        _, regenerated = await regenerate(running, sid, message="다시 봐 줘")
        assert events_named(regenerated, "done")[0]["text"].endswith(IMAGES_SEEN.format(count=1))
        after = (await checkpoint_humans(running, sid))[-1]
        assert typed_text(after) == "다시 봐 줘"
        assert notice_of(after) == notice_of(before)
        assert message_refs(after) == message_refs(before)
        users = [i for i in await transcript_items(running, sid) if i["kind"] == "user"]
        assert [u["text"] for u in users] == ["다시 봐 줘"]
        assert [a["name"] for a in users[0]["attachments"]] == ["보고서.pdf", "화면.png"]


# --- 입구 검사 -------------------------------------------------------------------------------


def _oversized_image() -> dict[str, Any]:
    return image(data=png(pad=MAX_IMAGE_BYTES))


@pytest.mark.parametrize(
    ("attachments", "status", "code"),
    [
        ([document(f"{n}.pdf") for n in range(6)], 422, "too_many_attachments"),
        ([image(f"{n}.png") for n in range(5)], 422, "too_many_attachments"),
        ([document("")], 422, "invalid_attachment"),
        ([document("a/..")], 422, "invalid_attachment"),
        ([document("a\x00b.pdf")], 422, "invalid_attachment"),
        ([document(format="doc")], 422, "invalid_attachment"),
        ([document(text="")], 422, "invalid_attachment"),
        ([document(text="가" * 500_001)], 422, "invalid_attachment"),
        ([document(sha256="ABC")], 422, "invalid_attachment"),
        ([{**image(), "data": "이건 base64 가 아니다"}], 422, "invalid_attachment"),
        ([image(data=jpeg())], 422, "invalid_attachment"),
        ([image(media_type="image/bmp")], 422, "invalid_attachment"),
        ([image(data=png(8001, 10))], 422, "invalid_attachment"),
        ([image(data=png(0, 10))], 422, "invalid_attachment"),
        ([image(data=b"not an image")], 422, "invalid_attachment"),
        ([_oversized_image()], 422, "invalid_attachment"),
    ],
)
async def test_bad_attachments_are_refused_before_a_session_opens(
    plain_doc, attachments: list[dict[str, Any]], status: int, code: str
) -> None:
    async with run_app(plain_doc) as running:
        response, _ = await send(running, attachments)
        assert response.status_code == status, response.text
        assert response.json()["code"] == code
        assert await running.runtime.store.index.list() == [], "세션을 만들지 않는다"


async def test_every_image_format_with_a_matching_header_is_accepted(plain_doc) -> None:
    images = [
        image("a.jpg", jpeg(), "image/jpeg"),
        image("b.gif", gif(), "image/gif"),
        image("c.webp", webp_lossless(), "image/webp"),
        image("d.webp", webp_extended(), "image/webp"),
    ]
    async with run_app(plain_doc) as running:
        response, sse = await send(running, images)
        assert response.status_code == 200, response.text
        assert events_named(sse, "done")[0]["attachments_received"] == 4


async def test_documents_need_the_filesystem_and_images_a_capable_model(plain_doc) -> None:
    plain_doc["main"]["builtin_tools"]["filesystem"] = False
    # harness profile 등록은 모델 키마다 합집합이라 다른 테스트의 stub:stub 에 숨김을 남기지 않는다
    plain_doc["main"]["model"]["name"] = "stub-no-files"
    async with run_app(plain_doc) as running:
        response, _ = await send(running, [document()])
        assert response.status_code == 422
        assert response.json()["code"] == "attachments_need_filesystem"
        # 이미지는 파일 도구 없이 된다
        response, sse = await send(running, [image()])
        assert response.status_code == 200 and sse[-1][0] == "done"
    blind = StubChatModel(profile={"max_input_tokens": 200_000, "image_inputs": False})
    async with run_app(plain_doc, model=blind) as running:
        response, _ = await send(running, [image()])
        assert response.status_code == 422 and response.json()["code"] == "images_unsupported"
        assert running.rejections() == ["images_unsupported"]


async def test_session_total_and_image_count(plain_doc) -> None:
    """memory 노드가 없으면 세션 합이 2MiB 다. 세션 이미지는 8개까지다. 넘은 요청은 저장하지 않고
    합도 그대로다."""
    half = image(data=png(pad=MAX_IMAGE_BYTES // 2 - 100))
    async with run_app(plain_doc) as running:
        _, sse = await send(running, [half])
        sid = sid_of(sse)
        _, again = await send(running, [half], session_id=sid)
        assert again[-1][0] == "done"
        before = (await running.runtime.store.index.get(sid)).upload_bytes
        response, _ = await send(running, [half, image("b.png")], session_id=sid)
        assert response.status_code == 413
        assert response.json()["code"] == "attachments_too_large"
        assert (await running.runtime.store.index.get(sid)).upload_bytes == before
        assert len(await running.runtime.store.uploads.read(sid)) == 2

    async with run_app(plain_doc) as running:
        four = [image(f"{n}.png") for n in range(4)]
        _, sse = await send(running, four)
        sid = sid_of(sse)
        _, again = await send(running, four, session_id=sid)
        assert again[-1][0] == "done"
        response, _ = await send(running, [image("9.png")], session_id=sid)
        assert response.status_code == 413
        stored = await running.runtime.store.uploads.read(sid)
        assert len(stored) == MAX_SESSION_IMAGES


async def test_one_request_over_the_session_total_opens_no_session(plain_doc) -> None:
    big = document(text="가" * 400_000)
    async with run_app(plain_doc) as running:
        response, _ = await send(running, [big, {**big, "name": "둘.pdf"}])
        assert response.status_code == 413
        assert await running.runtime.store.index.list() == []


async def test_a_body_over_the_limit_is_refused(plain_doc, monkeypatch) -> None:
    """Content-Length 헤더가 말하는 길이가 넘으면 읽기 전에, 그 헤더 없이 흘러 와도 읽다가 413
    payload_too_large 다."""
    monkeypatch.setattr(runtime_main, "MAX_CHAT_BODY_BYTES", 2048)
    body = json.dumps({"message": "가" * 2000, "user_sub": "alice"}).encode()

    async def chunks():
        for at in range(0, len(body), 512):
            yield body[at : at + 512]

    async with run_app(plain_doc) as running:
        declared = await running.client.post(
            "/chat", content=body, headers={"content-type": "application/json"}
        )
        streamed = await running.client.post(
            "/chat", content=chunks(), headers={"content-type": "application/json"}
        )
        small = await running.client.post("/chat", json={"message": "짧다", "user_sub": "alice"})
    for response in (declared, streamed):
        assert response.status_code == 413
        assert response.json()["code"] == "payload_too_large"
    assert small.status_code == 200


# --- 가드레일 -------------------------------------------------------------------------------


async def test_attachment_text_skips_length_rules_but_not_deny_and_pii(plain_doc) -> None:
    """입력 길이(4000자)와 가드레일 max_length(4000)는 첨부 글에 걸리지 않는다. pii_mask 막기는
    걸리고 422 와 위반에 첨부 이름이 붙는다. 관측 이벤트에는 이름이 없다."""
    async with run_app(plain_doc) as running:
        response, sse = await send(running, [document(text="긴 글 " * 5000)])
        assert response.status_code == 200 and sse[-1][0] == "done"
        response, _ = await send(running, [document("카드.pdf", text=f"카드 {CARD}")])
        assert response.status_code == 422
        body = response.json()
        assert body["code"] == "guardrail_blocked" and body["attachment"] == "카드.pdf"
        assert body["rule"] == "pii_mask"
        events = running.observed("guardrail")
        assert events[-1]["rule"] == "pii_mask" and "attachment" not in events[-1]


SECRET_MEMO = document("메모.pdf", text="기밀 자료")
FLAGGED_MEMO = {
    "position": "input",
    "rule": "deny_pattern",
    "action": "flag",
    "attachment": "메모.pdf",
}


async def test_a_flagged_attachment_names_itself_in_the_prelude(plain_doc) -> None:
    doc = with_input_deny(plain_doc, "기밀", "flag")
    doc["service"]["observability"]["record_bodies"] = True
    async with run_app(doc) as running:
        response, sse = await send(running, [SECRET_MEMO])
        assert response.status_code == 200
        assert events_named(sse, "guardrail") == [FLAGGED_MEMO]
        events = running.observed("guardrail")
        assert events[-1]["attachment"] == "메모.pdf", "record_bodies 면 이름을 싣는다"
        # turn_start 의 첨부 이름도 본문처럼 pii_mask 를 지난다
        assert running.observed("turn_start")[-1]["attachments"][0]["name"] == "메모.pdf"


async def test_regenerating_checks_the_attachments_sent_again(plain_doc, monkeypatch) -> None:
    """다시 생성과 고쳐 보내기도 다시 싣는 질문의 글 첨부를 입력 가드레일로 검사한다. flag 위반은
    다시 돌 때마다 앞머리에 첨부 이름과 함께 남는다. 검사하려고 읽은 첨부를 턴 문맥에 실어 저장소는
    한 번만 읽는다."""
    async with run_app(with_input_deny(plain_doc, "기밀", "flag")) as running:
        _, sse = await send(running, [SECRET_MEMO])
        sid = sid_of(sse)
        uploads = running.runtime.store.uploads
        reads: list[str] = []
        read = uploads.read

        async def counted(session_id: str) -> Any:
            reads.append(session_id)
            return await read(session_id)

        monkeypatch.setattr(uploads, "read", counted)
        for message in (None, "다시 봐 줘"):
            response, again = await regenerate(running, sid, message=message)
            assert response.status_code == 200, response.text
            assert events_named(again, "guardrail") == [FLAGGED_MEMO]
        assert reads == [sid, sid]


async def test_a_redeployed_input_guard_blocks_the_attachments_sent_again(plain_doc) -> None:
    """재배포로 강화한 막기 규칙은 그 전에 받은 첨부 글에도 적용된다. 그대로 보내든 고쳐 보내든
    422 에 첨부 이름이 붙는다."""
    async with pod_store(plain_doc) as store:
        async with run_app(plain_doc, store=store) as before:
            _, sse = await send(before, [SECRET_MEMO])
            sid = sid_of(sse)
        async with run_app(with_input_deny(plain_doc, "기밀", "block"), store=store) as blocked:
            for message in (None, "다시 봐 줘"):
                response, _ = await regenerate(blocked, sid, message=message)
                assert response.status_code == 422, response.text
                refused = response.json()
                assert refused["code"] == "guardrail_blocked"
                assert refused["attachment"] == "메모.pdf"
