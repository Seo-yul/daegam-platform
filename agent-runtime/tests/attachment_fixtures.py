"""첨부를 싣는 테스트들이 함께 쓰는 도우미다(P36 9절).

PNG 헤더 표본, `/chat` 본문의 이미지·글 첨부와 그것을 실어 보내는 턴, 저장소의 첨부 하나, 첨부가 든
턴의 전사 표본이다.
"""

from __future__ import annotations

import base64
import struct
from typing import Any

import httpx

from runtime.sessions import utcnow
from runtime.uploads import StoredUpload
from tests.conftest import RunningApp, chat, load_fixture

TEXT = "첫 줄\n환불은 7일 안에 한다\n끝 줄"
"""글 첨부에서 뽑은 글이다."""

ATTACHMENTS_FIXTURE = load_fixture("chat/attachments.json")
"""첨부가 든 턴의 done 이벤트와 전사 표본이다(P36 9절). 사용자 항목이 첨부의 이름·종류·크기와
쪽·시트 수, 잘림을 든다."""


def png(width: int = 16, height: int = 9, *, pad: int = 0) -> bytes:
    ihdr = struct.pack(">II", width, height) + b"\x08\x06\x00\x00\x00"
    return b"\x89PNG\r\n\x1a\n" + struct.pack(">I", 13) + b"IHDR" + ihdr + b"\0" * (4 + pad)


def image(name: str = "화면.png", data: bytes | None = None, media_type: str = "image/png"):
    raw = png() if data is None else data
    return {
        "kind": "image",
        "name": name,
        "media_type": media_type,
        "data": base64.b64encode(raw).decode(),
    }


def document(name: str = "보고서.pdf", text: str = "환불은 7일 안에 한다.", **extra: Any):
    return {"kind": "text", "name": name, "format": "pdf", "text": text, **extra}


async def send(
    running: RunningApp,
    attachments: list[dict[str, Any]],
    message: str = "첨부를 봐 줘",
    *,
    session_id: str | None = None,
) -> tuple[httpx.Response, list[tuple[str, dict[str, Any]]]]:
    """attachments 를 실은 /chat 한 턴이다. 빈 목록도 그대로 싣는다."""
    return await chat(running, message, session_id=session_id, attachments=attachments)


def stored(
    upload_id: str,
    *,
    kind: str = "text",
    name: str = "보고서.pdf",
    path: str | None = None,
    **extra: Any,
) -> StoredUpload:
    """저장소의 첨부 하나다. 경로를 주지 않으면 글은 뽑은 글(`.txt`), 이미지는 이름 그대로다."""
    content = png() if kind == "image" else TEXT.encode("utf-8")
    path = path or (f"/uploads/{name}.txt" if kind == "text" else f"/uploads/{name}")
    return StoredUpload(
        upload_id=upload_id,
        kind=kind,
        name=name,
        path=path,
        size=len(content),
        sha256="0" * 64,
        content=content,
        created_at=utcnow(),
        **extra,
    )
