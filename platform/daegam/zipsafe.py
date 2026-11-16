"""남이 준 zip 을 여는 유일한 문. zip 폭탄과 이름 장난을 문에서 막는다 (D61, P39 와 한 벌).

`zipfile` 은 중앙 디렉터리가 적은 크기를 믿고 끝까지 푼다. 수 MB 가 수 GB 로 풀리는 폭탄이 파드
메모리를 다 쓴다. `read_zip` 은 선언 크기를 믿지 않고 항목은 `max_entry+1`, 합은 남은 몫+1
바이트까지만 읽어 넘으면 그 자리에서 멈춘다. 압축률은 항목마다 실제로 푼 길이로 잰다. 반복이 많은
작은 XML 은 잘 압축되어 100배를 넘기도 하므로 `RATIO_GRACE_BYTES` 를 넘게 풀린 항목만 본다.

이름은 `ZipContents.files` 의 키로만 쓰고 디스크에 풀지 않지만 꼴을 먼저 거른다. 절대 경로,
`.`·`..`·빈 조각, 역슬래시, NUL, 겹치는 이름, 심볼릭 링크, 암호화 항목은 거절하고 디렉터리
항목(이름이 `/` 로 끝나고 데이터가 없는 것)은 건너뛴다. `skip` 이 참을 돌려준 항목은 이름만
검사하고 풀지 않으며 `skipped` 에 남는다.

실패는 `ArchiveError(code)` 하나이고 코드는 `archive_too_large`, `archive_encrypted`,
`archive_symlink`, `archive_path_invalid`, `archive_invalid` 다섯이다. 상한과 HTTP 모양은 부르는
쪽이 정한다.
"""

from __future__ import annotations

import io
import stat
import zipfile
import zlib
from collections.abc import Callable
from dataclasses import dataclass

# 이 크기 이하로 풀린 항목은 압축률을 보지 않는다
RATIO_GRACE_BYTES = 100 * 1024
# 일반 목적 비트 0 이 켜져 있으면 암호화 항목이다(APPNOTE 4.4.4)
_ENCRYPTED_FLAG = 0x1
# 손상된 압축 자료와 모르는 압축 방식이 내는 예외
_READ_ERRORS = (zipfile.BadZipFile, zlib.error, EOFError, NotImplementedError, OSError)


class ArchiveError(Exception):
    """zip 을 열지 않는다. `code` 가 까닭이다."""

    def __init__(self, code: str, detail: str):
        super().__init__(f"{code}: {detail}")
        self.code = code
        self.detail = detail


@dataclass(frozen=True)
class ZipContents:
    files: dict[str, bytes]
    skipped: tuple[str, ...]


def _checked_name(info: zipfile.ZipInfo, seen: set[str]) -> str:
    """항목 이름의 꼴. `orig_filename` 은 zipfile 이 NUL 뒤를 자르기 전의 이름이다."""
    raw = info.orig_filename
    if "\0" in raw or "\\" in raw or raw.startswith("/"):
        raise ArchiveError("archive_path_invalid", f"entry name {raw!r} is not a relative path")
    name = info.filename
    if any(part in ("", ".", "..") for part in name.removesuffix("/").split("/")):
        raise ArchiveError("archive_path_invalid", f"entry name {name!r} has an empty or dot part")
    if name in seen:
        raise ArchiveError("archive_path_invalid", f"entry name {name!r} appears twice")
    seen.add(name)
    if stat.S_ISLNK(info.external_attr >> 16):
        raise ArchiveError("archive_symlink", f"entry {name!r} is a symbolic link")
    return name


def _is_directory(info: zipfile.ZipInfo) -> bool:
    if not info.filename.endswith("/"):
        return False
    if info.file_size:
        raise ArchiveError("archive_path_invalid", f"directory entry {info.filename!r} has data")
    return True


def _read_entry(archive: zipfile.ZipFile, info: zipfile.ZipInfo, budget: int) -> bytes:
    """선언 크기와 상관없이 `budget+1` 바이트까지만 푼다."""
    if info.flag_bits & _ENCRYPTED_FLAG:
        raise ArchiveError("archive_encrypted", f"entry {info.filename!r} is encrypted")
    try:
        with archive.open(info) as f:
            return f.read(budget + 1)
    except _READ_ERRORS as e:
        raise ArchiveError("archive_invalid", type(e).__name__) from e


def _check_ratio(info: zipfile.ZipInfo, size: int, max_ratio: float) -> None:
    if size > RATIO_GRACE_BYTES and size > max_ratio * max(info.compress_size, 1):
        raise ArchiveError(
            "archive_too_large", f"entry {info.filename!r} expands more than {max_ratio:g} times"
        )


def read_zip(
    data: bytes,
    *,
    max_entries: int,
    max_total: int,
    max_entry: int,
    max_ratio: float,
    skip: Callable[[str], bool] | None = None,
) -> ZipContents:
    """zip 본문을 {이름: 바이트} 로 푼다. 디렉터리 항목은 빼고 `skip` 한 항목은 풀지 않는다."""
    try:
        archive = zipfile.ZipFile(io.BytesIO(data))
    except _READ_ERRORS as e:
        raise ArchiveError("archive_invalid", type(e).__name__) from e
    with archive:
        infos = archive.infolist()
        if len(infos) > max_entries:
            raise ArchiveError("archive_too_large", f"more than {max_entries} entries")
        files: dict[str, bytes] = {}
        skipped: list[str] = []
        seen: set[str] = set()
        total = 0
        for info in infos:
            name = _checked_name(info, seen)
            if _is_directory(info):
                continue
            if skip is not None and skip(name):
                skipped.append(name)
                continue
            budget = min(max_entry, max_total - total)
            body = _read_entry(archive, info, budget)
            if len(body) > budget:
                raise ArchiveError("archive_too_large", f"entry {name!r} exceeds the size limit")
            _check_ratio(info, len(body), max_ratio)
            total += len(body)
            files[name] = body
    return ZipContents(files=files, skipped=tuple(skipped))


__all__ = ["RATIO_GRACE_BYTES", "ArchiveError", "ZipContents", "read_zip"]
