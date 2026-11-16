"""제품 이미지의 바탕이 digest 로 고정돼 있는지와 make base-pins 가 고정을 옮기는 방법 (D55).

바탕을 태그로만 적으면 굽기마다 그날의 바탕을 받아, 코드가 그대로여도 후보의 OS 패키지가 시험한
것과 달라진다. 그래서 제품 Dockerfile 일곱(scripts/lib/images.sh 의 표)의 모든 FROM 은
`이름:태그@sha256:<digest>` 로 적고, 고정은 make base-pins(scripts/lib/base_pins.py)로만 옮긴다.
레지스트리에는 닿지 않는다.
"""

from __future__ import annotations

import hashlib
import json

import pytest
from base_pins import from_refs, main, raw_digest, repin, split_ref, unpinned

from tests.helpers.consts import REPO
from tests.helpers.images import product_dockerfiles
from tests.helpers.source import read_source

PRODUCT_DOCKERFILES = product_dockerfiles()
OLD = "sha256:" + "1" * 64
NEW = "sha256:" + "2" * 64


def test_the_image_table_names_seven_dockerfiles() -> None:
    assert len(PRODUCT_DOCKERFILES) == 7
    assert all(path.is_file() for path in PRODUCT_DOCKERFILES)


@pytest.mark.parametrize("dockerfile", PRODUCT_DOCKERFILES, ids=lambda path: path.parent.name)
def test_every_base_is_pinned_by_digest(dockerfile) -> None:
    text = read_source(dockerfile)

    assert from_refs(text), f"{dockerfile.relative_to(REPO)} 에 FROM 이 없다"
    assert unpinned(text) == [], (
        f"{dockerfile.relative_to(REPO)} 의 바탕이 digest 로 고정돼 있지 않다. "
        "`이름:태그` 로 적고 make base-pins 로 고정한다"
    )


def test_repin_moves_the_digest_and_keeps_the_rest_of_the_line() -> None:
    text = (
        f"FROM --platform=$BUILDPLATFORM node:24-alpine@{OLD} AS build\n"
        "# FROM python:3.12-slim 은 주석이라 건드리지 않는다\n"
        "COPY --from=build /ui/dist /usr/share/nginx/html\n"
        "FROM gcr.io/distroless/cc-debian13:latest\n"
    )
    digests = {"node:24-alpine": NEW, "gcr.io/distroless/cc-debian13:latest": NEW}

    assert repin(text, digests) == (
        f"FROM --platform=$BUILDPLATFORM node:24-alpine@{NEW} AS build\n"
        "# FROM python:3.12-slim 은 주석이라 건드리지 않는다\n"
        "COPY --from=build /ui/dist /usr/share/nginx/html\n"
        f"FROM gcr.io/distroless/cc-debian13:latest@{NEW}\n"
    )


@pytest.mark.parametrize(
    "ref",
    [
        "gcr.io/distroless/cc-debian13",
        f"python@{OLD}",
        "localhost:5000/base",
        "python:3.12@sha256:1",
    ],
)
def test_a_base_without_a_tag_or_with_a_bad_digest_is_refused(ref: str) -> None:
    with pytest.raises(ValueError):
        split_ref(ref)
    assert unpinned(f"FROM {ref}\n") == [ref]


def test_a_registry_port_is_not_a_tag() -> None:
    assert split_ref(f"localhost:5000/base:1.0@{OLD}") == ("localhost:5000/base:1.0", OLD)


def test_main_asks_once_per_tag_and_writes_only_what_moved(tmp_path, capsys) -> None:
    moved = tmp_path / "a.Dockerfile"
    kept = tmp_path / "b.Dockerfile"
    moved.write_text(f"FROM python:3.12-slim@{OLD} AS build\nFROM python:3.12-slim@{OLD}\n")
    kept.write_text(f"FROM python:3.12-slim@{NEW}\n")
    asked: list[str] = []

    def lookup(image: str) -> str:
        asked.append(image)
        return NEW

    assert main([str(moved), str(kept)], lookup) == 0

    assert asked == ["python:3.12-slim"]
    assert (
        moved.read_text() == f"FROM python:3.12-slim@{NEW} AS build\nFROM python:3.12-slim@{NEW}\n"
    )
    assert kept.read_text() == f"FROM python:3.12-slim@{NEW}\n"
    assert capsys.readouterr().out.splitlines()[-1] == "base-pins: FROM 줄 2 개를 옮겼다"


def test_check_writes_nothing_and_fails_on_a_stale_pin(tmp_path, capsys) -> None:
    stale = tmp_path / "a.Dockerfile"
    stale.write_text(f"FROM python:3.12-slim@{OLD}\n")

    assert main([str(stale)], lambda image: NEW, check=True) == 1

    assert stale.read_text() == f"FROM python:3.12-slim@{OLD}\n"
    out = capsys.readouterr().out.splitlines()
    assert "낡음 (sha256:111111111111 -> sha256:222222222222)" in out[0]
    assert out[-1] == "base-pins: 지금 digest 와 다른 FROM 줄이 1 개다. make base-pins 로 옮긴다"


def test_check_passes_when_every_pin_is_current(tmp_path, capsys) -> None:
    current = tmp_path / "a.Dockerfile"
    current.write_text(f"FROM python:3.12-slim@{NEW}\n")

    assert main([str(current)], lambda image: NEW, check=True) == 0
    assert capsys.readouterr().out.splitlines()[-1] == "base-pins: 고정이 모두 지금 digest 다"


def test_a_lowercase_or_indented_from_is_read_too() -> None:
    """Dockerfile 의 지시어는 대소문자를 가리지 않는다. 고정이 빠진 줄이 시험을 지나가면 안 된다."""
    text = "from python:3.12-slim AS build\n  FROM node:24-alpine\n"

    assert unpinned(text) == ["python:3.12-slim", "node:24-alpine"]
    assert repin(text, {"python:3.12-slim": NEW, "node:24-alpine": NEW}) == (
        f"from python:3.12-slim@{NEW} AS build\n  FROM node:24-alpine@{NEW}\n"
    )


def test_main_writes_nothing_when_a_lookup_fails(tmp_path) -> None:
    first = tmp_path / "a.Dockerfile"
    second = tmp_path / "b.Dockerfile"
    first.write_text(f"FROM python:3.12-slim@{OLD}\n")
    second.write_text("FROM node:24-alpine\n")

    def lookup(image: str) -> str:
        if image == "node:24-alpine":
            raise RuntimeError("node:24-alpine 의 digest 를 묻지 못했다")
        return NEW

    with pytest.raises(RuntimeError):
        main([str(first), str(second)], lookup)

    assert first.read_text() == f"FROM python:3.12-slim@{OLD}\n"


def test_the_digest_is_the_sha256_of_the_index_bytes() -> None:
    raw = json.dumps({"schemaVersion": 2, "manifests": [{"digest": OLD}]}).encode()

    assert raw_digest("python:3.12-slim", raw) == "sha256:" + hashlib.sha256(raw).hexdigest()


@pytest.mark.parametrize("raw", [b"not json", b'{"schemaVersion": 2, "layers": []}', b"[]"])
def test_a_single_architecture_manifest_or_garbage_is_refused(raw: bytes) -> None:
    with pytest.raises(RuntimeError):
        raw_digest("python:3.12-slim", raw)
