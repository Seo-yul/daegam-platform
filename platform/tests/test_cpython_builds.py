"""CPython 빌드의 판 기록 (P58, D70).

기록은 scripts/cpython-record.sh(make cpython-record)가 쓰고 scripts/lib/image_report.py 가 읽는다.
여기서는 기록을 만드는 순수 함수(scripts/lib/cpython_builds.py)를 본다. 기록이 지금 핀을
따라가는지는 test_python_image_copy.py 가 본다."""

from __future__ import annotations

from cpython_builds import (
    JUDGED,
    PLATFORMS,
    cache_name,
    dockerfile_pins,
    downloads,
    dump_builds,
    file_of,
    load_builds,
    normalize,
    parse_sums,
    release_of,
    sums_url,
)

URL = (
    "https://releases.astral.sh/github/python-build-standalone/releases/download/20261003/"
    "cpython-3.12.15%2B20261003-{arch}-unknown-linux-{libc}-install_only_stripped.tar.gz"
)


def _entry(
    arch: str, *, libc: str = "gnu", version: str = "3.12.15", variant: str = "default"
) -> dict:
    return {
        "key": f"cpython-{version}-linux-{arch}-{libc}",
        "version": version,
        "implementation": "cpython",
        "os": "linux",
        "libc": libc,
        "variant": variant,
        "arch": arch,
        "url": URL.format(arch=arch, libc=libc),
    }


def test_pins_come_from_the_uv_install_line_and_the_cpython_arg() -> None:
    text = (
        "FROM python:3.12-slim-trixie AS build\n"
        "RUN pip install --no-cache-dir uv==0.12.23\n"
        "ARG CPYTHON_VERSION=3.12.15\n"
    )

    assert dockerfile_pins(text) == ("0.12.23", "3.12.15")
    assert dockerfile_pins("FROM nginxinc/nginx-unprivileged:1.30.5-alpine3.24\n") is None


def test_downloads_keep_the_gnu_default_builds_of_one_family() -> None:
    uv_list = [
        _entry("x86_64"),
        _entry("x86_64_v3"),
        _entry("x86_64", libc="musl"),
        _entry("x86_64", variant="freethreaded"),
        _entry("x86_64", version="3.12.14"),
        _entry("aarch64"),
    ]

    keys = [d["key"] for d in downloads(uv_list, "3.12.15", "x86_64")]

    assert keys == ["cpython-3.12.15-linux-x86_64-gnu", "cpython-3.12.15-linux-x86_64_v3-gnu"]
    assert [d["key"] for d in downloads(uv_list, "3.12.15", "aarch64")] == [
        "cpython-3.12.15-linux-aarch64-gnu"
    ]


def test_release_and_checksum_file_come_from_the_download_url() -> None:
    url = URL.format(arch="aarch64", libc="gnu")

    assert release_of(url) == "20261003"
    assert sums_url(url).endswith("/releases/download/20261003/SHA256SUMS")
    assert (
        file_of(url)
        == "cpython-3.12.15+20261003-aarch64-unknown-linux-gnu-install_only_stripped.tar.gz"
    )
    assert parse_sums("abc  a.tar.gz\n\ndef  b.tar.gz\n") == {"a.tar.gz": "abc", "b.tar.gz": "def"}


def test_the_archive_uv_keeps_is_named_by_the_sha256_prefix() -> None:
    url = URL.format(arch="aarch64", libc="gnu")

    assert cache_name(url, "0123456789abcdef") == (
        "012345678-cpython-3.12.15-20261003-aarch64-unknown-linux-gnu-install_only_stripped.tar.gz"
    )


def test_versions_are_normalized_as_the_purls_use_them() -> None:
    raw = {
        "openssl": "OpenSSL 3.5.9 29 Sep 2026",
        "expat": "expat_2.8.5",
        "sqlite": "3.53.1",
        "zlib": "1.3.2",
    }

    assert normalize(raw) == {
        "openssl": "3.5.9",
        "expat": "2.8.5",
        "sqlite": "3.53.1",
        "zlib": "1.3.2",
    }


def test_a_record_round_trips_in_a_stable_order(tmp_path) -> None:
    entries = {
        ("0.12.23", "3.12.15", platform): {
            "uv": "0.12.23",
            "cpython": "3.12.15",
            "platform": platform,
        }
        for platform in reversed(list(PLATFORMS))
    }
    path = tmp_path / "builds.json"
    path.write_text(dump_builds(entries), encoding="utf-8")

    assert load_builds(str(path)) == entries
    assert dump_builds(load_builds(str(path))) == path.read_text(encoding="utf-8")
    assert load_builds(str(tmp_path / "missing.json")) == {}


def test_the_judged_set_starts_with_cpython() -> None:
    """image_report.bundled 는 판 줄에서 cpython 을 따로 적고 나머지를 이 순서로 적는다."""
    assert JUDGED[0] == "cpython"
