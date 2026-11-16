"""`make e2e-chat` 의 묶음 B 가 업로드하는 표본 파일을 만든다 (P36 F13, D61).

문서 표본은 플랫폼 테스트의 표본 만들기(`platform/tests/helpers/documents.py`)를 그대로
쓴다. 형식마다 글을 뽑는 데 필요한 조각만 담은 작은 꾸러미이고, 글마다 실행 표지 MARK 를 넣어
뽑은 글에서 찾게 한다. 개발 도구가 테스트 모듈을 경로로 들이는 것은 일부러 고른 꼴이다. 표본
만들기를 `platform/daegam` 으로 옮기면 테스트 표본이 제품 이미지에 실리고, 사본을 두면 플랫폼
테스트와 e2e 의 표본이 서로 어긋날 수 있다.

실행: uv run --project platform python deploy/dev/lab/chat_samples.py samples OUT_DIR MARK
        형식 여섯(docx·pdf·xlsx·hwpx·txt·png)을 OUT_DIR 에 쓰고 파일 이름을 한 줄에 하나 찍는다.
        pdf 는 2쪽, xlsx 는 2시트다
      uv run --project platform python deploy/dev/lab/chat_samples.py docx OUT_FILE MARK BYTES
        크기가 정확히 BYTES 인 docx 다. 압축하지 않은 본문을 한글 문단으로 채운다
        (설치본 갈래의 10MiB 업로드)
      uv run --project platform python deploy/dev/lab/chat_samples.py hangul OUT_FILE CHARS
        한글 CHARS 자의 UTF-8 글이다. 한 자가 3바이트라 세션의 첨부 상한을 적은 업로드로
        채운다
"""

from __future__ import annotations

import argparse
import struct
import sys
import zipfile
import zlib
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "platform"))

from tests.helpers.documents import (  # noqa: E402 - 플랫폼 테스트 패키지를 경로에 둔 뒤에 읽는다
    hp_p,
    make_docx,
    make_hwpx,
    make_pdf,
    make_xlsx,
    varied_text,
    w_p,
)

PNG_MAGIC = b"\x89PNG\r\n\x1a\n"
PNG_SIZE = (32, 18)
# 큰 docx 의 문단 하나에 넣는 글자 수. libxml2 는 글 마디 하나가 10MB 를 넘으면 거절한다
PARAGRAPH_CHARS = 1000


def _png_chunk(kind: bytes, data: bytes) -> bytes:
    return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data))


def png(width: int, height: int) -> bytes:
    """한 색으로 칠한 RGB PNG. 런타임은 IHDR 로 가로세로를 재고 모델은 그림으로 받는다."""
    ihdr = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    rows = (b"\0" + b"\x33\x66\x99" * width) * height
    return (
        PNG_MAGIC
        + _png_chunk(b"IHDR", ihdr)
        + _png_chunk(b"IDAT", zlib.compress(rows))
        + _png_chunk(b"IEND", b"")
    )


def samples(mark: str) -> dict[str, bytes]:
    """파일 이름 → 바이트. 업로드는 이 차례로 한다."""
    return {
        "계약서.docx": make_docx(w_p(f"계약서 {mark}") + w_p("환불은 7일 안에 한다.")),
        "환불규정.pdf": make_pdf(f"Refund policy {mark}", "Refunds within 7 days"),
        "매출.xlsx": make_xlsx(
            {
                "1분기": [["월", "매출"], ["1월", 120], [mark, 0]],
                "2분기": [["월", "매출"], ["4월", 150]],
            }
        ),
        "회의록.hwpx": make_hwpx([hp_p(f"회의록 {mark}") + hp_p("다음 회의는 금요일이다.")]),
        "notes.txt": f"메모 {mark}\n고객에게 다시 연락한다.\n".encode(),
        "screen.png": png(*PNG_SIZE),
    }


def sized_docx(mark: str, size: int) -> bytes:
    """크기가 정확히 size 바이트인 docx. 압축하지 않으면 꾸러미 크기가 본문 바이트만큼 늘므로 머리
    문단만 둔 꾸러미로 고정 몫을 재고, 남는 바이트를 한글 문단과 ASCII 문단 하나로 채운다."""
    head = w_p(f"대용량 {mark}")
    stored = zipfile.ZIP_STORED
    room = size - len(make_docx(head, compression=stored))
    tail_min = len(w_p("").encode())
    if room < tail_min:
        raise SystemExit(f"{size} bytes is smaller than the smallest document")
    paragraph = w_p(varied_text(PARAGRAPH_CHARS))
    step = len(paragraph.encode())
    count = (room - tail_min) // step
    tail = "x" * (room - tail_min - count * step)
    data = make_docx(head + paragraph * count + w_p(tail), compression=stored)
    assert len(data) == size, (len(data), size)
    return data


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="command", required=True)
    one = sub.add_parser("samples")
    one.add_argument("out_dir", type=Path)
    one.add_argument("mark")
    big = sub.add_parser("docx")
    big.add_argument("out_file", type=Path)
    big.add_argument("mark")
    big.add_argument("bytes", type=int)
    text = sub.add_parser("hangul")
    text.add_argument("out_file", type=Path)
    text.add_argument("chars", type=int)
    args = parser.parse_args(argv)

    if args.command == "samples":
        args.out_dir.mkdir(parents=True, exist_ok=True)
        for name, data in samples(args.mark).items():
            (args.out_dir / name).write_bytes(data)
            print(name)
    elif args.command == "docx":
        args.out_file.write_bytes(sized_docx(args.mark, args.bytes))
    else:
        args.out_file.write_text(varied_text(args.chars), encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
