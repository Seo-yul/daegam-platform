"""이미지 헤더에서 형식과 가로세로를 읽는다(P36 9절, D61).

첨부 이미지는 풀지 않는다. 디코더는 C 로 된 큰 공격면이고, 런타임은 바이트를 저장했다가
모델 요청에 그대로 싣기만 하기 때문이다. 대신 헤더만 읽어 매직 바이트가 선언한 `media_type`
과 맞는지, 가로세로가 상한 안인지 본다. PNG 는 IHDR, JPEG 는 SOF 표지, GIF 는 논리 화면,
WebP 는 VP8·VP8L·VP8X 조각이다. 헤더를 끝까지 읽지 못하거나 모양이 틀리면 이미지가 아닌
것으로 본다. 이 모듈은 표준 라이브러리만 쓴다.
"""

from __future__ import annotations

import struct
from dataclasses import dataclass

PNG = "png"
JPEG = "jpeg"
GIF = "gif"
WEBP = "webp"

_PNG_MAGIC = b"\x89PNG\r\n\x1a\n"
_GIF_MAGICS = (b"GIF87a", b"GIF89a")
_JPEG_MAGIC = b"\xff\xd8\xff"
# 길이 칸 없이 홀로 서는 JPEG 표지다(RST0~7, SOI, EOI, TEM)
_JPEG_STANDALONE = frozenset({*range(0xD0, 0xDA), 0x01})
# 가로세로를 싣는 JPEG 프레임 시작 표지다. C4(DHT)·C8(JPG)·CC(DAC)는 SOF 가 아니다
_JPEG_SOF = frozenset({*range(0xC0, 0xD0)} - {0xC4, 0xC8, 0xCC})
# VP8 키 프레임의 시작 부호다
_VP8_START = b"\x9d\x01\x2a"
_VP8L_SIGNATURE = 0x2F
_WEBP_DIMENSION_MASK = 0x3FFF


@dataclass(frozen=True)
class ImageHeader:
    format: str
    width: int
    height: int


def read_header(data: bytes) -> ImageHeader | None:
    """헤더가 말하는 형식과 가로세로다. 모르는 형식이거나 헤더가 깨졌으면 None 이다."""
    try:
        if data.startswith(_PNG_MAGIC):
            return _png(data)
        if data.startswith(_GIF_MAGICS):
            width, height = struct.unpack_from("<HH", data, 6)
            return ImageHeader(GIF, width, height)
        if data.startswith(_JPEG_MAGIC):
            return _jpeg(data)
        if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
            return _webp(data)
    except (struct.error, IndexError):
        return None
    return None


def _png(data: bytes) -> ImageHeader | None:
    if data[12:16] != b"IHDR":
        return None
    width, height = struct.unpack_from(">II", data, 16)
    return ImageHeader(PNG, width, height)


def _jpeg(data: bytes) -> ImageHeader | None:
    """SOI 뒤의 조각을 차례로 건너뛰며 처음 만나는 SOF 의 가로세로를 읽는다."""
    pos = 2
    while pos + 4 <= len(data):
        if data[pos] != 0xFF:
            return None
        marker = data[pos + 1]
        if marker == 0xFF:
            pos += 1  # 채움 바이트다
            continue
        if marker in _JPEG_STANDALONE:
            pos += 2
            continue
        (length,) = struct.unpack_from(">H", data, pos + 2)
        if marker in _JPEG_SOF:
            height, width = struct.unpack_from(">HH", data, pos + 5)
            return ImageHeader(JPEG, width, height)
        if length < 2:
            return None
        pos += 2 + length
    return None


def _webp(data: bytes) -> ImageHeader | None:
    chunk = data[12:16]
    if chunk == b"VP8 ":
        if data[23:26] != _VP8_START:
            return None
        width, height = struct.unpack_from("<HH", data, 26)
        return ImageHeader(WEBP, width & _WEBP_DIMENSION_MASK, height & _WEBP_DIMENSION_MASK)
    if chunk == b"VP8L":
        if data[20] != _VP8L_SIGNATURE:
            return None
        (bits,) = struct.unpack_from("<I", data, 21)
        width = (bits & _WEBP_DIMENSION_MASK) + 1
        height = ((bits >> 14) & _WEBP_DIMENSION_MASK) + 1
        return ImageHeader(WEBP, width, height)
    if chunk == b"VP8X" and len(data) >= 30:
        width = int.from_bytes(data[24:27], "little") + 1
        height = int.from_bytes(data[27:30], "little") + 1
        return ImageHeader(WEBP, width, height)
    return None
