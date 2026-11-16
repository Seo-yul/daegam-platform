"""남이 준 zip 을 여는 문 `read_zip` (D61, P39 와 한 벌). 항목 수·합·항목 크기·압축률, 선언 크기를
속인 항목, 암호화·심볼릭 링크·절대 경로·`..`·빈 조각·역슬래시·NUL·겹치는 이름, 디렉터리 항목과
`skip`. 코드마다 P36 의 HTTP 대응은 `test_documents.py` 가 본다."""

from __future__ import annotations

import io
import os
import stat
import warnings
import zipfile

import pytest

from daegam.zipsafe import RATIO_GRACE_BYTES, ArchiveError, read_zip
from tests.helpers.documents import set_central_flags, zip_of

LIMITS = {"max_entries": 10, "max_total": 4096, "max_entry": 2048, "max_ratio": 100}


def _read(data: bytes, **kw):
    return read_zip(data, **{**LIMITS, **kw})


def _code(data: bytes, **kw) -> str:
    with pytest.raises(ArchiveError) as e:
        _read(data, **kw)
    return e.value.code


def _zip_with(*infos: tuple[zipfile.ZipInfo | str, bytes]) -> bytes:
    buf = io.BytesIO()
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")  # 겹치는 이름을 쓰면 zipfile 이 경고한다
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as archive:
            for info, body in infos:
                archive.writestr(info, body)
    return buf.getvalue()


def _declare_size(data: bytes, size: int) -> bytes:
    """중앙 디렉터리의 풀린 크기 칸을 바꿔 선언 크기를 속인다."""
    out = bytearray(data)
    at = out.find(b"PK\x01\x02")
    out[at + 24 : at + 28] = size.to_bytes(4, "little")
    return bytes(out)


def test_reads_files_skips_directories_and_reports_skipped():
    data = _zip_with(("dir/", b""), ("dir/a.txt", b"alpha"), ("__MACOSX/x", b"junk"))
    got = _read(data, skip=lambda name: name.startswith("__MACOSX/"))
    assert got.files == {"dir/a.txt": b"alpha"}
    assert got.skipped == ("__MACOSX/x",)


def test_entry_count_total_and_entry_size():
    assert _code(zip_of({f"f{n}": b"x" for n in range(11)})) == "archive_too_large"
    assert _code(zip_of({"big": os.urandom(2049)})) == "archive_too_large"
    assert _code(zip_of({"a": os.urandom(2000), "b": os.urandom(2000), "c": os.urandom(200)})) == (
        "archive_too_large"
    )
    assert _read(zip_of({"a": os.urandom(2048), "b": os.urandom(2048)})).files.keys() == {"a", "b"}


def test_ratio_is_measured_per_entry_after_the_grace_size():
    roomy = {"max_total": 4 * RATIO_GRACE_BYTES, "max_entry": 4 * RATIO_GRACE_BYTES}
    small = zip_of({"zeros": b"\0" * RATIO_GRACE_BYTES})  # 압축률은 높지만 작다
    assert _read(small, **roomy).files["zeros"] == b"\0" * RATIO_GRACE_BYTES
    bomb = zip_of({"zeros": b"\0" * (2 * RATIO_GRACE_BYTES)})
    assert _code(bomb, **roomy) == "archive_too_large"


def test_a_lying_declared_size_does_not_buy_more_than_the_limit_plus_one():
    """선언 크기를 크게 속이면 상한+1 에서 멈추고, 작게 속이면 CRC 가 어긋나 열지 않는다."""
    body = os.urandom(8192)
    assert _code(_declare_size(zip_of({"x": body}), 2**31), max_total=10**6) == (
        "archive_too_large"
    )
    assert _code(_declare_size(zip_of({"x": body}), 16), max_total=10**6, max_entry=10**5) == (
        "archive_invalid"
    )


def test_encrypted_and_symlink_entries():
    assert _code(set_central_flags(zip_of({"x": b"s"}), "x", 0x1)) == "archive_encrypted"
    link = zipfile.ZipInfo("link")
    link.external_attr = (stat.S_IFLNK | 0o777) << 16
    assert _code(_zip_with((link, b"/etc/passwd"))) == "archive_symlink"


@pytest.mark.parametrize(
    "name", ["/etc/passwd", "../up", "a/../b", "./a", "a//b", "a\\b", "a/./b", "dir/../"]
)
def test_bad_names(name):
    assert _code(_zip_with((name, b"x"))) == "archive_path_invalid"


def test_nul_in_a_stored_name_and_duplicate_names():
    named = bytes(zip_of({"aXb.txt": b"x"})).replace(b"aXb.txt", b"a\0b.txt")
    assert _code(named) == "archive_path_invalid"
    assert _code(_zip_with(("same", b"1"), ("same", b"2"))) == "archive_path_invalid"


def test_a_directory_entry_with_data_is_refused():
    folder = zipfile.ZipInfo("dir/")
    assert _code(_zip_with((folder, b"payload"))) == "archive_path_invalid"
