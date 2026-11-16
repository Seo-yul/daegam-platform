"""서버가 쓰는 작은 HTTP/1.1 (P40 3절).

런타임은 새 연결마다 `/proof` 로 증명을 받은 뒤 그 연결을 다시 쓰므로 keep-alive 를 받는다. 본문은
Content-Length 로만 받고 chunked 는 받지 않는다. 길이 한도는 경로마다 다르다. 요청 하나를 다 읽은
뒤에야 답하고, 파이프라이닝은 하지 않는다.
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass, field

MAX_LINE = 8 * 1024
MAX_HEADERS = 64
# 첫 바이트가 온 뒤 머리와 본문을 다 읽기까지의 시간
READ_TIMEOUT = 120.0

REASONS = {
    200: "OK",
    400: "Bad Request",
    401: "Unauthorized",
    404: "Not Found",
    405: "Method Not Allowed",
    411: "Length Required",
    413: "Content Too Large",
    431: "Request Header Fields Too Large",
    500: "Internal Server Error",
    501: "Not Implemented",
    503: "Service Unavailable",
}


class HttpError(Exception):
    """요청을 읽다 멈췄다. 답하고 연결을 닫는다."""

    def __init__(self, status: int, code: str) -> None:
        super().__init__(code)
        self.status = status
        self.code = code


@dataclass
class Request:
    method: str
    path: str
    version: str
    headers: dict[str, str] = field(default_factory=dict)
    body: bytes = b""

    @property
    def keep_alive(self) -> bool:
        connection = self.headers.get("connection", "").lower()
        if self.version == "HTTP/1.0":
            return connection == "keep-alive"
        return connection != "close"


async def _line(reader: asyncio.StreamReader) -> bytes:
    try:
        line = await reader.readuntil(b"\r\n")
    except asyncio.LimitOverrunError as e:
        raise HttpError(431, "line_too_long") from e
    if len(line) > MAX_LINE:
        raise HttpError(431, "line_too_long")
    return line[:-2]


async def read_request(reader: asyncio.StreamReader, body_limit) -> Request | None:
    """요청 하나. 다음 요청을 기다리다 연결이 닫혔으면 None 이다. body_limit 은 경로 -> 바이트
    한도다."""
    try:
        first = await reader.readuntil(b"\r\n")
    except asyncio.IncompleteReadError as e:
        if not e.partial:
            return None
        raise HttpError(400, "truncated") from e
    except asyncio.LimitOverrunError as e:
        raise HttpError(431, "line_too_long") from e
    async with asyncio.timeout(READ_TIMEOUT):
        return await _rest(reader, first[:-2], body_limit)


async def _rest(reader: asyncio.StreamReader, first: bytes, body_limit) -> Request:
    parts = first.decode("latin-1").split(" ")
    if len(parts) != 3 or parts[2] not in ("HTTP/1.1", "HTTP/1.0"):
        raise HttpError(400, "bad_request_line")
    method, target, version = parts
    path = target.split("?", 1)[0]
    headers: dict[str, str] = {}
    while True:
        line = await _line(reader)
        if not line:
            break
        if len(headers) >= MAX_HEADERS:
            raise HttpError(431, "too_many_headers")
        name, sep, value = line.decode("latin-1").partition(":")
        name = name.strip().lower()
        if not sep or not name or " " in name:
            raise HttpError(400, "bad_header")
        if name in headers and name in ("content-length", "authorization", "transfer-encoding"):
            raise HttpError(400, "duplicate_header")
        headers[name] = value.strip()
    if "transfer-encoding" in headers:
        raise HttpError(501, "chunked_not_supported")
    raw_length = headers.get("content-length", "0")
    if not raw_length.isdigit() or len(raw_length) > 12:
        raise HttpError(400, "bad_content_length")
    length = int(raw_length)
    if length > body_limit(path):
        raise HttpError(413, "body_too_large")
    body = await reader.readexactly(length) if length else b""
    return Request(method, path, version, headers, body)


def response(status: int, payload: dict, *, keep_alive: bool) -> bytes:
    body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    head = (
        f"HTTP/1.1 {status} {REASONS.get(status, 'Error')}\r\n"
        "Content-Type: application/json\r\n"
        f"Content-Length: {len(body)}\r\n"
        "Cache-Control: no-store\r\n"
        f"Connection: {'keep-alive' if keep_alive else 'close'}\r\n\r\n"
    )
    return head.encode("latin-1") + body
