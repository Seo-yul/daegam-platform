"""플랫폼 호스트와 설치 값 글자는 설치 벌의 env 파일에만 둔다 (D58, P82, P85 10절).

설치본의 공개 호스트는 설치 값 파일(`deploy/install.env.example` 의 키를 채운 것, 저장소 밖에
둔다)의 DAEGAM_HOST 한 줄에서 차트 값·데모 Gateway 로 퍼진다. 저장소에는 예시 값만 있고, 예시
호스트도 예시 파일 밖에는 두지 않는다. 이 머신에 채운 파일이 있으면 그 값(호스트, 레지스트리
호스트, 컨텍스트, 데모 Gateway 호스트)이 추적하는 파일에 없는지도 본다. 주석과 문서 문자열도
세고 `docs/` 만 예외다. 로컬 호스트(`deploy/dev/values-local.yaml` 의 `host`)는 제품 코드와
스크립트에서 주석 줄에만 쓸 수 있다. 테스트 경로는 고정 예시 값을 쓰므로 세지 않는다.
"""

from __future__ import annotations

import os
import re
from pathlib import Path

import pytest
import yaml

from tests.helpers.consts import INSTALL_ENV, LOCAL_VALUES, REPO
from tests.helpers.repo import env_file_values, repo_files, repo_lines

# 예시 호스트를 둘 수 있는 곳
EXAMPLE_EXEMPT = (INSTALL_ENV, "docs/")
# 로컬 호스트를 세는 곳. platform/tests·ui/e2e 는 이 밖이고 ui/src/test 는 안에서 뺀다
LOCAL_SCOPE = (
    "charts/",
    "deploy/",
    "Makefile",
    "platform/daegam/",
    "ui/src/",
    "ui/docker/",
    "scripts/",
    "ops/",
)
LOCAL_EXEMPT = (LOCAL_VALUES, "ui/src/test/")
COMMENT_PREFIXES = ("#", "//", "*", "/*")


def _hits(host: str, paths: list[str]) -> list[tuple[str, int, str]]:
    """host 가 든 (경로, 줄, 줄 내용). 앞뒤가 호스트 글자로 이어지면(`api.<host>`) 다른 호스트다."""
    pattern = re.compile(rf"(?<![\w.-]){re.escape(host)}(?![\w-]|\.\w)")
    return [
        (path, lineno, line.strip())
        for path, lineno, line in repo_lines(paths)
        if pattern.search(line)
    ]


def _install_hits(word: str, paths: list[str]) -> list[tuple[str, int, str]]:
    """설치 값 word 가 든 줄. 라벨·애너테이션 키(`<word>/<이름>`)는 세지 않는다. 플랫폼 호스트가
    라벨 도메인과 같은 이름일 수 있기 때문이다. 주소(`://<word>`)는 뒤에 `/` 가 와도 센다."""
    w = re.escape(word)
    pattern = re.compile(rf"(?<=://){w}|(?<![\w.-]){w}(?![\w-]|\.\w|/)")
    return [
        (path, lineno, line.strip())
        for path, lineno, line in repo_lines(paths)
        if pattern.search(line)
    ]


def _report(hits: list[tuple[str, int, str]]) -> str:
    return "\n  ".join(f"{path}:{lineno}  {line[:120]}" for path, lineno, line in hits)


def test_example_public_host_lives_only_in_the_example():
    host = env_file_values(INSTALL_ENV)["DAEGAM_HOST"]
    hits = _hits(host, [p for p in repo_files() if not p.startswith(EXAMPLE_EXEMPT)])
    assert not hits, (
        f"예시 호스트 {host} 는 {INSTALL_ENV} 에만 둔다. 스크립트는 설치 값 파일에서 읽고 "
        "문장은 'install.env 의 DAEGAM_HOST' 로 가리킨다:\n  " + _report(hits)
    )


def _install_file() -> Path | None:
    """scripts/lib/hosts.sh 의 install_env_file 이 찾는 채운 설치 값 파일. 없으면 None 이다."""
    conf = (
        Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config")
        / "daegam"
        / "install.env"
    )
    path = Path(os.environ.get("INSTALL_ENV_FILE") or conf)
    return path if path.is_file() else None


# 공개 트리로 내보내지 않는 경로. 이 안의 글은 운영자의 설치를 그대로 적어도 된다
PRIVATE = ("docs/", ".claude/", "CLAUDE.md")
# 저장소에 두지 않는 설치 값. EXPOSURE_DOMAIN 은 라벨 도메인의 접미사와 겹칠 수 있어 세지 않는다.
# 공개 트리의 누출 검사도 같은 키를 본다
INSTALL_KEYS = ("DAEGAM_HOST", "INSTALL_CONTEXT", "TOUR_GW_HOST")


def test_install_values_stay_out_of_tracked_files():
    # 운영자의 설치 값을 지키는 검사라 docs/ 가 있는 운영자의 저장소에서만 돈다. 공개 트리를
    # 받은 사람이 예시를 채우면 `my-cluster`·`ghcr.io` 같은 흔한 값이 곳곳에 있어 까닭 없이 실패한다
    if not (REPO / "docs").is_dir():
        pytest.skip("docs/ 가 없는 트리다 (공개 내보내기)")
    path = _install_file()
    if path is None:
        pytest.skip("이 머신에 채운 설치 값 파일이 없다")
    values = env_file_values(str(path))
    # 키가 빠진 파일이면 그 값의 누출을 보지 못하고 통과한다
    missing = [k for k in INSTALL_KEYS if not values.get(k)]
    assert not missing, f"{path} 에 {missing} 가 없다. 그 값이 추적하는 파일에 없는지 볼 수 없다"
    words = {values[k] for k in INSTALL_KEYS}
    if values.get("REGISTRY"):
        words.add(values["REGISTRY"].split("/", 1)[0])
    paths = [p for p in repo_files() if not p.startswith(PRIVATE)]
    hits = [hit for word in sorted(words) for hit in _install_hits(word, paths)]
    assert not hits, (
        f"{path} 의 설치 값이 추적하는 파일에 있다. 스크립트는 "
        "scripts/lib/hosts.sh 의 install_env_file 로 읽는다:\n  " + _report(hits)
    )


def test_local_host_appears_only_in_comments():
    host = yaml.safe_load((REPO / LOCAL_VALUES).read_text())["host"]
    paths = [
        p for p in repo_files() if p.startswith(LOCAL_SCOPE) and not p.startswith(LOCAL_EXEMPT)
    ]
    hits = [h for h in _hits(host, paths) if not h[2].startswith(COMMENT_PREFIXES)]
    assert not hits, (
        f"로컬 호스트 {host} 는 {LOCAL_VALUES} 에만 둔다. 차트는 값 host, 스크립트는 "
        "scripts/lib/hosts.sh 의 local_host, Makefile 은 UI_FULL_HOST 를 쓴다:\n  " + _report(hits)
    )
