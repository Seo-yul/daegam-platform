"""경로 풀이: 세 뿌리와 /tmp, `..`·심볼릭 링크·NUL·길이 거절 (P40 3절)."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from daegam_sandbox.config import Layout
from daegam_sandbox.paths import MAX_PATH, PathRefused, Resolver


@pytest.fixture
def resolver(tmp_path: Path) -> Resolver:
    (tmp_path / "workspace" / "uploads").mkdir(parents=True)
    (tmp_path / "workspace" / "skills").mkdir()
    (tmp_path / "tmp").mkdir()
    return Resolver(Layout(tmp_path))


def real(path: Path) -> Path:
    return Path(os.path.realpath(path))


def test_the_roots_map_into_the_workspace(resolver: Resolver, tmp_path: Path) -> None:
    ws = real(tmp_path / "workspace")
    assert resolver.resolve("/workspace/a.csv") == ("/workspace/a.csv", ws / "a.csv")
    assert resolver.resolve("/uploads/a.csv") == ("/uploads/a.csv", ws / "uploads" / "a.csv")
    assert resolver.resolve("/skills/s/SKILL.md")[1] == ws / "skills" / "s" / "SKILL.md"
    assert resolver.resolve("notes/x.txt") == ("/workspace/notes/x.txt", ws / "notes" / "x.txt")
    assert resolver.resolve("/workspace//./b")[0] == "/workspace/b"
    tmp_edit = resolver.resolve("/tmp/.deepagents_edit_1_old")[1]
    assert tmp_edit == real(tmp_path / "tmp") / ".deepagents_edit_1_old"


@pytest.mark.parametrize(
    "path",
    [
        "/workspace/../etc/passwd",
        "/workspace/a/../../x",
        "..",
        "/etc/passwd",
        "/proc/1/environ",
        "/workspace/a\0b",
        "/workspace/" + "a" * MAX_PATH,
        "",
        "/",
        None,
        7,
    ],
)
def test_escapes_and_bad_paths_are_refused(resolver: Resolver, path: object) -> None:
    with pytest.raises(PathRefused):
        resolver.resolve(path)


def test_symlinks_that_leave_the_root_are_refused(resolver: Resolver, tmp_path: Path) -> None:
    ws = tmp_path / "workspace"
    (ws / "out").symlink_to("/etc")
    (ws / "inside").symlink_to(ws / "uploads")
    (tmp_path / "tmp" / "to-ws").symlink_to(ws)
    with pytest.raises(PathRefused):
        resolver.resolve("/workspace/out/passwd")
    # 작업 공간 안을 가리키는 링크는 받는다. /tmp 에서 작업 공간으로 가는 링크는 /tmp 를 벗어난다
    assert resolver.resolve("/workspace/inside/a")[1] == real(ws / "uploads") / "a"
    with pytest.raises(PathRefused):
        resolver.resolve("/tmp/to-ws/a")


def test_heads_narrow_the_roots(resolver: Resolver) -> None:
    with pytest.raises(PathRefused):
        resolver.resolve("/tmp/a", heads=("workspace",))
