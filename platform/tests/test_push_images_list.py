"""`make push-images` 가 이미지 표(scripts/lib/images.sh)의 구성요소를 모두 올리는지 본다.

표에만 넣고 Makefile 의 목록이나 판 변수를 빠뜨리면 릴리스 후보가 그 이미지 없이 나가고,
설치의 digest 고정(scripts/resolve-images.sh)이 그 이미지를 찾지 못한다."""

from __future__ import annotations

import re

from tests.helpers.consts import REPO
from tests.helpers.source import read_source

IMAGES = read_source(REPO / "scripts" / "lib" / "images.sh")
MAKEFILE = read_source(REPO / "Makefile")


def _push_recipe() -> str:
    return MAKEFILE.split("\npush-images:\n", 1)[1].split("\n\n", 1)[0]


def test_push_images_pushes_every_component() -> None:
    components = re.search(r'^COMPONENTS="([^"]+)"', IMAGES, re.M)
    default = re.search(r"^PUSH_IMAGES\s*\?=\s*(.+)$", MAKEFILE, re.M)
    assert components and default
    assert default.group(1).split() == components.group(1).split()


def test_push_images_passes_every_version_variable() -> None:
    names = set(re.findall(r"\$\{(\w+_VERSION):-", IMAGES))
    assert names
    recipe = _push_recipe()
    assert sorted(n for n in names if f"{n}=$({n})" not in recipe) == []
