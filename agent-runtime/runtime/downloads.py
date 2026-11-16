"""내려받기 응답의 헤더다(P36 10절, D61). 세션 파일(session_files.py)이 이 헤더로 바이트를 낸다.

플랫폼 `api/_download.py` 와 같은 규칙의 두 번째 벌이다. 외부 호출자가 Ingress 로 직접 받을 때 이
헤더가 가고, 플랫폼 프록시는 이 헤더를 넘기지 않고 제 것으로 다시 만든다. 두 쪽 테스트가 표본
`schema/fixtures/chat/download-headers.json` 과 비교해 같은지 본다.

에이전트가 쓴 `.html` 이 문서로 열리면 그 origin 에서 스크립트가 돌므로 어떤 형식도 inline 으로
내지 않는다. 파일 이름은 경로의 마지막 조각에서 CR·LF·따옴표를 뺀 것이다. `filename` 은 인쇄할 수
있는 ASCII 만 남긴 대체 이름이고, 브라우저는 퍼센트 인코딩한 UTF-8 원래 이름 `filename*` 를 먼저
읽는다.
"""

from __future__ import annotations

import bz2
import gzip
import io
import lzma
import re
import tarfile
import unicodedata
import zipfile
from collections.abc import Callable
from typing import Any, BinaryIO
from urllib.parse import quote

OCTET_STREAM = "application/octet-stream"
FALLBACK_NAME = "download"
# 파일 이름에서 빼는 글자다. 헤더를 끊거나(CR·LF) 따옴표 문자열을 닫는다(따옴표)
_UNSAFE_NAME = re.compile(r'[\r\n"]')
# ASCII 대체 이름에서 `_` 로 바꾸는 글자다. 인쇄할 수 있는 ASCII 밖과 따옴표 문자열의
# 이스케이프 글자다
_NOT_PLAIN_ASCII = re.compile(r"[^\x20-\x7e]|\\")


def download_name(path: str) -> str:
    """경로의 마지막 조각에서 CR·LF·따옴표를 뺀 파일 이름이다. 남는 것이 없으면 `download` 다."""
    return _UNSAFE_NAME.sub("", path.rsplit("/", 1)[-1]) or FALLBACK_NAME


def download_headers(path: str, *, media_type: str = OCTET_STREAM) -> dict[str, str]:
    """내려받기 헤더 다섯이다. 형식을 짐작하지 못하게 막고, 캐시하지 않으며, 문서로 열려도
    스크립트가 돌지 않게 sandbox 다."""
    name = download_name(path)
    fallback = _NOT_PLAIN_ASCII.sub("_", name)
    return {
        "Content-Type": media_type,
        "Content-Disposition": (
            f"attachment; filename=\"{fallback}\"; filename*=UTF-8''{quote(name, safe='')}"
        ),
        "X-Content-Type-Options": "nosniff",
        "Cache-Control": "no-store",
        "Content-Security-Policy": "sandbox; default-src 'none'",
    }


# --- 작업 공간 파일의 거절 (P40 6절) -------------------------------------------------------
#
# 열면 코드가 돌 수 있는 형식은 내려받지 않는다. 이름을 먼저 고르고(끝의 점·공백을 떼고 NFC,
# 양방향 제어 글자가 있으면 거절) 차례대로 본다. 끝 확장자가 표에 있으면 거절하고, 매직 바이트가
# 실행 형식이면 거절한다(PE·ELF·Mach-O·.lnk·OLE·VHD·ISO·DMG, 몸 전체에서 찾는 RTF 의 개체). 둘 다
# 지난 것만 zip 과 tar 무리를 한 겹 열어 항목이 표에 맞거나 그 자체가 압축 파일이면 거절하고, 그
# 밖의 압축 형식은 거절한다. 압축을 풀어 읽는 바이트는 합쳐 64MiB, 항목은 2000 개까지이고 넘으면
# 거절한다. 거절은 동기 함수라 부르는 쪽이 이벤트 루프 밖(스레드)에서 부른다. 글 스크립트는
# 받는다(D61). 이것은 바이너리를 늦출 뿐이고 주된 신호는 "작업 공간에서 만든 파일" 표시다.

REFUSED_EXTENSIONS = frozenset(
    {
        # 실행 파일과 라이브러리
        "exe", "dll", "com", "scr", "msi", "sys", "elf", "so", "dylib", "app",
        # 바로 가기와 실행기
        "lnk", "url", "desktop", "webloc", "appref-ms",
        # Java 묶음
        "jar", "class",
        # 디스크 이미지
        "iso", "img", "vhd", "vhdx", "dmg",
        # 매크로 문서
        "docm", "xlsm", "pptm", "dotm", "xltm", "potm",
    }
)  # fmt: skip
# 한 겹 열어 보는 압축 형식이다
ZIP_EXTENSIONS = frozenset({"zip"})
TAR_EXTENSIONS = frozenset({"tar", "tgz", "tbz", "tbz2", "txz"})
# 열지 않고 거절하는 압축 형식이다
OTHER_ARCHIVE_EXTENSIONS = frozenset(
    {"7z", "rar", "cab", "gz", "bz2", "xz", "zst", "lz", "lz4", "lzma", "z", "arj", "lzh", "ace",
     "cpio", "rpm", "deb", "apk", "xar", "wim"}
)  # fmt: skip
ARCHIVE_EXTENSIONS = ZIP_EXTENSIONS | TAR_EXTENSIONS | OTHER_ARCHIVE_EXTENSIONS
_BIDI_CONTROLS = frozenset(
    "\u061c\u200e\u200f\u202a\u202b\u202c\u202d\u202e\u2066\u2067\u2068\u2069"
)
_EXECUTABLE_MAGIC = (
    b"MZ",  # PE
    b"\x7fELF",
    b"\xfe\xed\xfa\xce", b"\xfe\xed\xfa\xcf", b"\xce\xfa\xed\xfe", b"\xcf\xfa\xed\xfe",  # Mach-O
    b"\xca\xfe\xba\xbe",  # Mach-O fat 과 Java class
    b"L\x00\x00\x00\x01\x14\x02\x00",  # .lnk
    b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1",  # OLE
    b"vhdxfile",
    b"conectix",  # VHD 의 앞 사본
)  # fmt: skip
_ZIP_MAGIC = (b"PK\x03\x04", b"PK\x05\x06", b"PK\x07\x08")
_OTHER_ARCHIVE_MAGIC = (
    b"7z\xbc\xaf\x27\x1c", b"Rar!\x1a\x07", b"MSCF", b"\x1f\x8b", b"BZh", b"\xfd7zXZ\x00",
    b"\x28\xb5\x2f\xfd", b"\x04\x22\x4d\x18", b"xar!",
)  # fmt: skip
# tar 를 감싼 압축과 그것을 푸는 파일이다
_TAR_WRAPPERS: tuple[tuple[bytes, Callable[[BinaryIO], BinaryIO]], ...] = (
    (b"\x1f\x8b", lambda f: gzip.GzipFile(fileobj=f)),
    (b"BZh", bz2.BZ2File),
    (b"\xfd7zXZ\x00", lzma.LZMAFile),
)
# RTF 의 머리와 개체 제어어의 앞부분이다. 개체는 몸 어디에나 올 수 있고 줄바꿈이 제어어를 끊을
# 수 있다
_RTF_HEAD = b"{\\rt"
_RTF_OBJECT = b"\\obj"
# ISO 9660 의 볼륨 서술자 자리다
_ISO_OFFSETS = (0x8001, 0x8801, 0x9001)
# 압축 안 항목에서 읽는 머리의 길이다. ISO 의 서술자까지 닿는다
ENTRY_HEAD_BYTES = 0x9006
# 한 겹 열어 볼 항목 수의 상한이다. 넘으면 거절한다
MAX_ARCHIVE_ENTRIES = 2000
# 압축을 풀어 읽는 바이트 합의 상한이다. 넘으면 압축 폭탄으로 보고 거절한다
MAX_ARCHIVE_READ_BYTES = 64 * 1024 * 1024
# 열어 볼 OOXML 의 매크로·포함 개체 자리다
_OOXML_MARKERS = ("vbaproject.bin", "/embeddings/")
_READ_CHUNK = 1 << 20


class _TooMuch(Exception):
    """압축을 풀어 읽은 바이트나 항목이 상한을 넘었다."""


class _Budget:
    """압축을 풀어 읽을 수 있는 남은 바이트다."""

    def __init__(self, limit: int = MAX_ARCHIVE_READ_BYTES) -> None:
        self.left = limit

    def take(self, n: int) -> None:
        self.left -= n
        if self.left < 0:
            raise _TooMuch


class _Counted(io.RawIOBase):
    """읽는 바이트를 예산에서 덜어 내는 읽기다. 압축을 푸는 파일을 감싼다."""

    def __init__(self, inner: BinaryIO, budget: _Budget) -> None:
        self.inner = inner
        self.budget = budget

    def readable(self) -> bool:
        return True

    def readinto(self, buffer: Any) -> int:
        data = self.inner.read(len(buffer))
        self.budget.take(len(data))
        buffer[: len(data)] = data
        return len(data)


def normalized_name(name: str) -> str | None:
    """거절을 보기 전의 이름이다. 끝의 점과 공백을 떼고 NFC 로 맞춘다. 양방향 제어 글자가 있으면
    None 이다(거절)."""
    if any(ch in _BIDI_CONTROLS for ch in name):
        return None
    return unicodedata.normalize("NFC", name).rstrip(". ")


def _extensions(name: str) -> list[str]:
    """이름의 확장자들이다. `a.tar.gz` 는 `["tar", "gz"]` 이고 소문자다."""
    return [part.lower() for part in name.split(".")[1:]]


def _rtf_object(content: bytes) -> bool:
    """RTF 이고 몸 어디엔가 개체 제어어가 있는가. 줄바꿈을 떼고 찾는다."""
    if not content.startswith(_RTF_HEAD):
        return False
    return _RTF_OBJECT in content.replace(b"\r", b"").replace(b"\n", b"")


def _executable(content: bytes) -> bool:
    """매직 바이트가 실행 형식이거나 코드를 품은 형식인가(압축 형식은 따로 본다)."""
    head = content[:ENTRY_HEAD_BYTES]
    if head.startswith(_EXECUTABLE_MAGIC) or _rtf_object(content):
        return True
    if any(head[o : o + 5] == b"CD001" for o in _ISO_OFFSETS):
        return True
    # VHD 의 바닥글과 DMG 의 koly 는 끝에서 512 바이트 앞에 있다
    tail = content[-512:]
    return len(content) >= 512 and (tail.startswith(b"conectix") or tail.startswith(b"koly"))


def _tar_magic(head: bytes) -> bool:
    return head[257:262] == b"ustar"


def _member_bytes(member: BinaryIO, budget: _Budget) -> bytes:
    """압축 안 항목에서 보는 바이트다. 머리만 읽고, RTF 면 개체를 찾으려고 끝까지 읽는다. 읽은
    만큼 예산에서 던다."""
    head = member.read(ENTRY_HEAD_BYTES)
    budget.take(len(head))
    if not head.startswith(_RTF_HEAD):
        return head
    chunks = [head]
    while chunk := member.read(_READ_CHUNK):
        budget.take(len(chunk))
        chunks.append(chunk)
    return b"".join(chunks)


def _entry_refused(name: str, content: bytes) -> bool:
    """압축 안 항목 하나가 표에 맞거나 그 자체가 압축 파일인가."""
    clean = normalized_name(name.rsplit("/", 1)[-1])
    if clean is None:
        return True
    extensions = _extensions(clean)
    last = extensions[-1] if extensions else ""
    if last in REFUSED_EXTENSIONS or last in ARCHIVE_EXTENSIONS:
        return True
    head = content[:ENTRY_HEAD_BYTES]
    archive = head.startswith(_ZIP_MAGIC) or head.startswith(_OTHER_ARCHIVE_MAGIC)
    return archive or _tar_magic(head) or _executable(content)


def _zip_refused(content: bytes) -> bool:
    budget = _Budget()
    try:
        with zipfile.ZipFile(io.BytesIO(content)) as archive:
            entries = archive.infolist()
            if len(entries) > MAX_ARCHIVE_ENTRIES:
                return True
            for entry in entries:
                lowered = f"/{entry.filename.lower()}"
                if lowered == "/meta-inf/manifest.mf" or any(m in lowered for m in _OOXML_MARKERS):
                    return True
                if entry.is_dir():
                    continue
                with archive.open(entry) as member:
                    seen = _member_bytes(member, budget)
                if _entry_refused(entry.filename, seen):
                    return True
    except (_TooMuch, zipfile.BadZipFile, zipfile.LargeZipFile, OSError, RuntimeError,
            NotImplementedError, EOFError, ValueError):  # fmt: skip
        # 읽지 못하는 zip(암호, 모르는 압축 방식, 깨짐)은 속을 볼 수 없어 거절한다
        return True
    return False


def _tar_stream(content: bytes, budget: _Budget) -> BinaryIO:
    """tar 의 바이트를 차례로 읽는 파일이다. 감싼 압축을 풀며 읽은 만큼 예산에서 던다."""
    raw: BinaryIO = io.BytesIO(content)
    for magic, opener in _TAR_WRAPPERS:
        if content.startswith(magic):
            raw = opener(raw)
            break
    return io.BufferedReader(_Counted(raw, budget))


def _tar_refused(content: bytes) -> bool:
    budget = _Budget()
    try:
        # 흐름으로 연다. 항목을 건너뛰는 읽기도 예산을 지난다
        with tarfile.open(fileobj=_tar_stream(content, budget), mode="r|") as archive:
            for count, member in enumerate(archive, start=1):
                if count > MAX_ARCHIVE_ENTRIES:
                    return True
                if not member.isfile():
                    continue
                extracted = archive.extractfile(member)
                seen = _member_bytes(extracted, budget) if extracted is not None else b""
                if _entry_refused(member.name, seen):
                    return True
    except (_TooMuch, tarfile.TarError, OSError, EOFError, ValueError, lzma.LZMAError):
        return True
    return False


def refused_download(name: str, content: bytes) -> bool:
    """작업 공간의 파일을 내려받지 않을 것인가(P40 6절 표). 끝 확장자, 실행 매직, 압축의 속, 그 밖의
    압축 형식 차례다. 압축을 푸므로 이벤트 루프 밖에서 부른다."""
    clean = normalized_name(name)
    if clean is None or not clean:
        return True
    extensions = _extensions(clean)
    last = extensions[-1] if extensions else ""
    if last in REFUSED_EXTENSIONS or _executable(content):
        return True
    head = content[:ENTRY_HEAD_BYTES]
    # `a.tar.gz` 처럼 tar 를 감싼 압축은 tar 무리다
    if last in TAR_EXTENSIONS or extensions[-2:-1] == ["tar"] or _tar_magic(head):
        return _tar_refused(content)
    if last in ZIP_EXTENSIONS or head.startswith(_ZIP_MAGIC):
        return _zip_refused(content)
    return last in OTHER_ARCHIVE_EXTENSIONS or head.startswith(_OTHER_ARCHIVE_MAGIC)
