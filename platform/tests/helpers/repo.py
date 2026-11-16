"""저장소의 파일을 훑는 테스트(호스트 글자, 문서 인용, 설치 벌)가 함께 쓰는 도우미."""

from __future__ import annotations

import subprocess
from collections.abc import Iterable, Iterator

from tests.helpers.consts import REPO


def repo_files() -> list[str]:
    """git 이 추적하거나 곧 추적할 파일의 저장소 기준 경로. 커밋 전의 새 파일도 센다."""
    out = subprocess.run(
        ["git", "ls-files", "-z", "--cached", "--others", "--exclude-standard"],
        cwd=REPO,
        capture_output=True,
        check=True,
    ).stdout.decode()
    return sorted({p for p in out.split("\0") if p and (REPO / p).is_file()})


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
