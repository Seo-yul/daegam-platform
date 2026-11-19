"""저장소의 파일을 훑는 테스트(호스트 글자, 문서 인용, 설치 벌)가 함께 쓰는 도우미."""

from __future__ import annotations

import subprocess
from collections.abc import Iterable, Iterator

from tests.helpers.consts import REPO

# 개발 폴더를 작업 트리로 함께 쓰는 다른 저장소의 git 디렉터리를 적는 설정 키. 문서와 운영자의
# 파일은 그 저장소가 추적하고, 이 저장소는 그 경로를 info/exclude 로 뺀다. 설정이 없으면(CI,
# 이 저장소만 받은 클론) 이 저장소의 파일만 본다
SHARED_GIT_DIR_KEY = "daegam.sharedGitDir"


def _ls_files(*git: str) -> set[str]:
    out = subprocess.run(
        ["git", *git, "ls-files", "-z", "--cached", "--others", "--exclude-standard"],
        cwd=REPO,
        capture_output=True,
        check=True,
    ).stdout.decode()
    return {p for p in out.split("\0") if p}


def repo_files() -> list[str]:
    """git 이 추적하거나 곧 추적할 파일의 저장소 기준 경로. 커밋 전의 새 파일도 센다. 개발
    폴더를 함께 쓰는 저장소가 있으면(`daegam.sharedGitDir`) 그 저장소의 파일 가운데 이 트리에
    있는 것도 센다. worktree 에는 그 파일이 없으므로 CI 와 같다."""
    paths = _ls_files()
    shared = subprocess.run(
        ["git", "config", "--get", SHARED_GIT_DIR_KEY], cwd=REPO, capture_output=True, text=True
    ).stdout.strip()
    if shared:
        paths |= _ls_files(f"--git-dir={shared}", f"--work-tree={REPO}")
    return sorted(p for p in paths if (REPO / p).is_file())


def repo_lines(paths: Iterable[str]) -> Iterator[tuple[str, int, str]]:
    """paths 의 (경로, 줄 번호, 줄). UTF-8 로 읽지 못하는 바이트는 버린다."""
    for path in paths:
        text = (REPO / path).read_bytes().decode("utf-8", errors="ignore")
        for lineno, line in enumerate(text.splitlines(), 1):
            yield path, lineno, line


def env_file_values(env_file: str) -> dict[str, str]:
    """env 파일(저장소 기준 경로)의 key=value 줄. 빈 줄과 # 주석 줄은 건너뛴다. 렌더 스크립트가
    읽는 규칙과 같다."""
    lines = (REPO / env_file).read_text(encoding="utf-8").splitlines()
    return dict(
        line.split("=", 1) for line in lines if line and not line.startswith("#") and "=" in line
    )
